"use client";

import { useState, useMemo, useEffect, useRef, useCallback } from "react";
import { X, NotebookText, ChevronDown, ChevronRight, Loader2, Wrench, Download, Image as ImageIcon, FileText, FileCode, FileType, Copy, Check, Maximize2, Minimize2 } from "lucide-react";
import { LoaderPinwheelIcon, type LoaderPinwheelIconHandle } from "@/components/ui/loader-pinwheel";
import { useThreadRuntime } from "@assistant-ui/react";
import { MarkdownText } from "@/components/MarkdownRenderer";
import { useFocusTrap } from "@/lib/a11y/use-focus-trap";
import { useExportImage, suggestImageFilename } from "@/lib/export/use-export-image";
import {
  exportReportAsHtml,
  exportReportAsMarkdown,
  exportReportAsPdf,
} from "@/lib/reports/exporters";
import { getReport, type Report } from "@/lib/runtime-client";
import { getToken } from "@/lib/auth";
import { formatOutput, abbreviateByLines } from "@/lib/format-output";

interface ReportPanelProps {
  messageId: string;
  /** Optional report_id — set when the panel was opened from a ReportCard
   *  for an async-generated report. In that case the current assistant
   *  message may only carry a <report-pending> marker (no <report-body>
   *  yet), so we fetch the canonical report from the REST API.
   */
  reportId?: string;
  onClose: () => void;
  /** When true, the panel fills the row (chat collapsed) and the body is
   *  rendered in a centered, capped-width "reading mode". Default (false) is
   *  the resizable slideout that sits beside the chat — unchanged behavior. */
  fullBleed?: boolean;
  /** Toggle between slideout (split) and full-width reading mode. */
  onToggleFullBleed?: () => void;
}

const REPORT_BODY_RE = /^<report-body>([\s\S]*)<\/report-body>$/;
const THINK_RE = /^<think[^>]*>\n?([\s\S]*?)\n?<\/think>$/;
const ARTIFACT_RE = /^<artifact>([\s\S]*?)<\/artifact>$/;
const TOOL_RE = /^<report-tool>([\s\S]*?)<\/report-tool>$/;

type ToolInfo = { name: string; input: Record<string, unknown>; output?: string; tool_trace?: ToolInfo[]; duration_s?: number };

/** Convert a backend ReportTrace (`tool_name`, `tool_trace`) into the
 *  ReportPanel's ToolInfo shape (`name`, `tool_trace`). The two structures
 *  are identical except for the rename — done at the API boundary so the
 *  rest of the panel doesn't need to branch on which source produced the
 *  trace.
 */
function toToolInfo(t: { tool_name?: string; name?: string; input?: Record<string, unknown>; output?: string; tool_trace?: unknown[]; duration_s?: number }): ToolInfo {
  const nested = Array.isArray(t.tool_trace)
    ? t.tool_trace.map((x) => toToolInfo(x as Parameters<typeof toToolInfo>[0]))
    : undefined;
  return {
    name: t.tool_name || t.name || "tool",
    input: (t.input || {}) as Record<string, unknown>,
    output: t.output,
    tool_trace: nested,
    duration_s: t.duration_s,
  };
}

function extractFromMessage(msg: { content: ReadonlyArray<{ type: string; text?: string }> }) {
  let reasoning = "";
  let reportBody = "";
  let isGenerating = true;
  let reportTitle = "Generating report…";
  const tools: ToolInfo[] = [];

  for (const part of msg.content) {
    if (part.type !== "text" || !part.text) continue;

    const thinkMatch = part.text.match(THINK_RE);
    if (thinkMatch) {
      if (reasoning) reasoning += "\n";
      reasoning += thinkMatch[1];
      continue;
    }

    const toolMatch = part.text.match(TOOL_RE);
    if (toolMatch) {
      try {
        const parsed = JSON.parse(toolMatch[1]);
        tools.push(_normalizeToolInfo(parsed));
      } catch { /* ignore */ }
      continue;
    }

    const reportMatch = part.text.match(REPORT_BODY_RE);
    if (reportMatch) {
      reportBody += reportMatch[1];
      continue;
    }

    const artifactMatch = part.text.match(ARTIFACT_RE);
    if (artifactMatch) {
      try {
        const meta = JSON.parse(artifactMatch[1]);
        if (!meta.generating) {
          isGenerating = false;
          if (meta.title) reportTitle = meta.title;
        }
      } catch { /* ignore */ }
      continue;
    }
  }

  // Extract title from report body if available
  if (reportBody) {
    const titleMatch = reportBody.match(/^#\s+(.+)$/m);
    if (titleMatch) reportTitle = titleMatch[1].trim();

    // Check for in-progress indicator from incremental Memory saves
    if (reportBody.includes("[Report generation in progress...]")) {
      isGenerating = true;
      reportTitle = reportTitle || "Generating report…";
    }
  }

  return { reasoning, reportBody, isGenerating, reportTitle, tools };
}

/** Normalize tool data from various sources (live stream, Memory save) into ToolInfo. */
function _normalizeToolInfo(raw: Record<string, unknown>): ToolInfo {
  const name = (raw.name || raw.tool_name || "unknown") as string;
  const input = (raw.input ?? {}) as Record<string, unknown>;
  const duration_s = raw.duration_s as number | undefined;

  // Parse output — may be a JSON string wrapping {response, tool_trace}
  // Recursively unescape up to 3 levels (leaf → orchestrator → supervisor)
  let output = "";
  let tool_trace: ToolInfo[] | undefined;

  const rawOutput = raw.output;
  if (typeof rawOutput === "string" && rawOutput) {
    // Recursively parse until we get an object or can't parse further
    let parsed: unknown = rawOutput;
    for (let i = 0; i < 3; i++) {
      if (typeof parsed !== "string") break;
      try { parsed = JSON.parse(parsed); } catch { break; }
    }

    if (typeof parsed === "object" && parsed !== null) {
      const obj = parsed as Record<string, unknown>;
      // Extract clean output from response wrapper
      const cleanData = obj.response || obj.data || "";
      if (typeof cleanData === "string") {
        output = cleanData;
      } else if (typeof cleanData === "object") {
        output = JSON.stringify(cleanData);
      }
      if (Array.isArray(obj.tool_trace)) {
        tool_trace = obj.tool_trace.map((t: Record<string, unknown>) => _normalizeToolInfo(t));
      }
      // If no clean data extracted, use the pretty-printed object
      if (!output) {
        output = JSON.stringify(obj, null, 2);
      }
    } else {
      output = typeof parsed === "string" ? parsed : rawOutput;
    }
  }

  // Also check for tool_trace directly on the raw object (Memory save format)
  if (!tool_trace && Array.isArray(raw.tool_trace)) {
    tool_trace = (raw.tool_trace as Record<string, unknown>[]).map((t) => _normalizeToolInfo(t));
  }

  return { name, input, output: output || undefined, tool_trace, duration_s };
}

/* ── PanelToolBlock — collapsible tool call for the artifact panel ── */

function PanelToolBlock({ tool, isStreaming }: { tool: ToolInfo; isStreaming?: boolean }) {
  const [open, setOpen] = useState(false);
  const [outputExpanded, setOutputExpanded] = useState(false);

  const hasOutput = !!tool.output;
  const hasInput = tool.input && Object.keys(tool.input).length > 0;
  const hasNested = tool.tool_trace && tool.tool_trace.length > 0;
  const duration = tool.duration_s != null ? `${tool.duration_s}s` : "";

  const formattedOutput = tool.output ? formatOutput(tool.output) : "";
  const outputView = abbreviateByLines(formattedOutput);
  const outputShown = outputExpanded ? formattedOutput : outputView.abbreviated;

  return (
    <div
      className="rounded-lg my-2 overflow-hidden text-xs"
      style={{ border: "1px solid var(--accent-border-subtle)", background: "var(--accent-bg-faint)" }}
    >
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2 w-full px-3 py-2 text-left"
        style={{ color: "var(--text-muted)" }}
        aria-expanded={open}
      >
        {isStreaming && !hasOutput ? (
          <Loader2 className="h-3 w-3 shrink-0 animate-spin" style={{ color: "var(--accent)" }} />
        ) : (
          <Wrench className="h-3 w-3 shrink-0" style={{ color: "var(--accent)" }} />
        )}
        <span className="font-medium truncate" style={{ color: "var(--accent)" }}>
          {isStreaming && !hasOutput ? `Calling ${tool.name}…` : tool.name}
        </span>
        {duration && <span className="opacity-60">({duration})</span>}
        {hasNested && (
          <span className="text-[10px] opacity-50">
            ({tool.tool_trace!.length} sub-call{tool.tool_trace!.length > 1 ? "s" : ""})
          </span>
        )}
        {open
          ? <ChevronDown className="h-3 w-3 ml-auto shrink-0" />
          : <ChevronRight className="h-3 w-3 ml-auto shrink-0" />
        }
      </button>
      {open && (
        <div className="px-3 pb-3 space-y-2">
          {hasInput && (
            <div>
              <div className="text-[10px] font-medium uppercase tracking-wider mb-1" style={{ color: "var(--text-muted)", opacity: 0.7 }}>Input</div>
              <pre className="overflow-x-auto text-xs leading-relaxed whitespace-pre-wrap" style={{ color: "var(--text-muted)" }}>
                {JSON.stringify(tool.input, null, 2)}
              </pre>
            </div>
          )}
          {hasNested && (
            <div>
              <div className="text-[10px] font-medium uppercase tracking-wider mb-1" style={{ color: "var(--text-muted)", opacity: 0.7 }}>Sub-agent calls</div>
              {tool.tool_trace!.map((nested, i) => (
                <PanelToolBlock key={`${nested.name}-${i}`} tool={nested} />
              ))}
            </div>
          )}
          {formattedOutput ? (
            <div>
              <div
                className="flex items-center justify-between mb-1"
                style={{ color: "var(--text-muted)", opacity: 0.7 }}
              >
                <span className="text-[10px] font-medium uppercase tracking-wider">
                  Output {outputView.truncated && `(${outputView.totalLines} lines)`}
                </span>
                {outputView.truncated && (
                  <button
                    onClick={() => setOutputExpanded((e) => !e)}
                    className="text-[10px] font-medium hover:underline"
                    style={{ color: "var(--accent)" }}
                  >
                    {outputExpanded ? "Show less" : "Show all"}
                  </button>
                )}
              </div>
              <pre className="overflow-x-auto text-xs leading-relaxed whitespace-pre-wrap" style={{ color: "var(--text-secondary)", maxHeight: 200, overflowY: "auto" }}>
                {outputShown}
              </pre>
            </div>
          ) : isStreaming ? (
            <div className="flex items-center gap-2 text-xs" style={{ color: "var(--text-muted)" }}>
              <Loader2 className="h-3 w-3 animate-spin" />
              Running…
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}

const MIN_WIDTH = 360;
const MAX_WIDTH_RATIO = 0.6;
const DEFAULT_WIDTH = 480;

export function ReportPanel({ messageId, reportId, onClose, fullBleed = false, onToggleFullBleed }: ReportPanelProps) {
  const threadRuntime = useThreadRuntime();
  const scrollRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const spinnerRef = useRef<LoaderPinwheelIconHandle>(null);
  const panelRef = useFocusTrap<HTMLDivElement>(true, onClose);
  const exportImage = useExportImage();
  // Poll message state to get live updates during streaming
  const [tick, setTick] = useState(0);
  const [panelWidth, setPanelWidth] = useState(() => {
    if (typeof window === "undefined") return DEFAULT_WIDTH;
    const saved = localStorage.getItem("artifact-panel-width");
    return saved ? Math.max(MIN_WIDTH, Math.min(Number(saved), window.innerWidth * MAX_WIDTH_RATIO)) : DEFAULT_WIDTH;
  });
  const [isDragging, setIsDragging] = useState(false);

  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    setIsDragging(true);
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";

    // Resize via DIRECT DOM writes during the drag; commit React state ONCE on
    // mouseup. Setting state per mousemove re-rendered the whole panel — and
    // through it ReactMarkdown + every SmartTable chart (each a Recharts
    // ResponsiveContainer with a ResizeObserver) — ~60x/second. Oscillating
    // the handle stacked those observer/setState cascades until React threw
    // "Maximum update depth exceeded" and the app was dead until refresh.
    let liveWidth = 0;
    const onMouseMove = (ev: MouseEvent) => {
      liveWidth = Math.max(MIN_WIDTH, Math.min(window.innerWidth - ev.clientX, window.innerWidth * MAX_WIDTH_RATIO));
      if (panelRef.current) panelRef.current.style.width = `${liveWidth}px`;
    };
    const onMouseUp = () => {
      document.removeEventListener("mousemove", onMouseMove);
      document.removeEventListener("mouseup", onMouseUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      setIsDragging(false);
      if (liveWidth) {
        localStorage.setItem("artifact-panel-width", String(liveWidth));
        setPanelWidth(liveWidth);
      }
    };
    document.addEventListener("mousemove", onMouseMove);
    document.addEventListener("mouseup", onMouseUp);
  }, [panelRef]);

  // When the panel was opened from an async-report ReportCard, the message
  // content only has a <report-pending> marker until the background worker
  // writes the final memory event. We fall back to fetching the report
  // from the REST API so the panel isn't stuck on "Generating report…".
  const [fetchedReport, setFetchedReport] = useState<Report | null>(null);

  useEffect(() => {
    if (!reportId) {
      setFetchedReport(null);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const r = await getReport(reportId, "", getToken);
        if (!cancelled) setFetchedReport(r);
      } catch (err) {
        // Panel still renders from the message transcript if the fetch fails.
        console.error("ReportPanel: failed to fetch report", err);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [reportId]);

  const messageDerived = useMemo(() => {
    const messages = threadRuntime.getState().messages;
    const msg = messages.find((m) => m.id === messageId);
    if (!msg) return { reasoning: "", reportBody: "", isGenerating: true, reportTitle: "Generating report…", tools: [] };
    return extractFromMessage(msg);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [messageId, threadRuntime, tick]);

  const { reasoning, reportBody, isGenerating, reportTitle, tools } = useMemo(() => {
    // If we have a fetched async report, render it. Otherwise use whatever
    // the message transcript carried (existing behaviour for synchronous
    // reports and legacy memory-rehydrated reports).
    if (fetchedReport) {
      const body = (fetchedReport.sections || [])
        .filter((s) => s.status === "complete" && s.content)
        .map((s) => `## ${s.title}\n\n${s.content}`)
        .join("\n\n");
      // Pull per-section traces from the fetched report and flatten into
      // a single list rendered at the top of the panel. The report row's
      // `traces` field is the durable source of truth — it survives
      // Memory expiry, page reload, and re-opening from the sidebar.
      // We coerce the API trace shape (`tool_name`, `tool_trace`) to the
      // frontend's `ToolInfo` shape (`name`, `tool_trace`) so the existing
      // PanelToolBlock can render it without branching.
      const fetchedTraces: ToolInfo[] = [];
      for (const s of fetchedReport.sections || []) {
        for (const t of s.traces || []) {
          fetchedTraces.push(toToolInfo(t));
        }
      }
      return {
        reasoning: messageDerived.reasoning,
        reportBody: body,
        isGenerating: fetchedReport.status !== "complete" && fetchedReport.status !== "error",
        reportTitle: fetchedReport.title || messageDerived.reportTitle,
        // Prefer fetched traces (durable, complete) over message-derived
        // ones (ephemeral, from in-flight stream). If the row has no
        // traces yet (still generating), fall back to whatever the
        // streaming pipeline has produced so the user sees live progress.
        tools: fetchedTraces.length > 0 ? fetchedTraces : messageDerived.tools,
      };
    }
    return messageDerived;
  }, [fetchedReport, messageDerived]);

  // Poll only while generating to get live updates; stop once complete
  useEffect(() => {
    if (!isGenerating) return;
    const interval = setInterval(() => setTick((t) => t + 1), 500);
    return () => clearInterval(interval);
  }, [isGenerating]);

  // Animate thinking spinner
  useEffect(() => {
    if (isGenerating) spinnerRef.current?.startAnimation();
    else spinnerRef.current?.stopAnimation();
  }, [isGenerating]);

  // Auto-scroll thinking view during generation
  useEffect(() => {
    if (isGenerating && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [isGenerating, reasoning, tools.length]);

  const [downloadMenuOpen, setDownloadMenuOpen] = useState(false);
  const downloadMenuRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!downloadMenuOpen) return;
    const handler = (e: MouseEvent) => {
      if (downloadMenuRef.current && !downloadMenuRef.current.contains(e.target as Node)) {
        setDownloadMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [downloadMenuOpen]);

  const handleDownloadHtml = useCallback(() => {
    exportReportAsHtml({ contentEl: contentRef.current, markdown: reportBody, title: reportTitle });
    setDownloadMenuOpen(false);
  }, [reportBody, reportTitle]);

  const handleDownloadMarkdown = useCallback(() => {
    exportReportAsMarkdown({
      contentEl: contentRef.current,
      markdown: reportBody,
      title: reportTitle,
    });
    setDownloadMenuOpen(false);
  }, [reportBody, reportTitle]);

  const handleDownloadPdf = useCallback(() => {
    exportReportAsPdf({ contentEl: contentRef.current, markdown: reportBody, title: reportTitle });
    setDownloadMenuOpen(false);
  }, [reportBody, reportTitle]);

  const [copied, setCopied] = useState(false);
  const handleCopy = useCallback(() => {
    // Strip preamble before first heading
    let text = reportBody.trim();
    const headingIdx = text.search(/^(#{1,6}\s|---)/m);
    if (headingIdx > 0) text = text.slice(headingIdx);
    navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }, [reportBody]);

  const handleDownloadImage = useCallback(async () => {
    await exportImage({
      element: contentRef.current,
      filename: suggestImageFilename(
        reportTitle.replace(/[^a-zA-Z0-9_-]+/g, "-").toLowerCase() || "report",
      ),
    });
  }, [exportImage, reportTitle]);

  return (
    <div
      ref={panelRef}
      className="flex flex-col relative"
      style={{
        // Full-bleed reading mode: ignore the user-resized fixed width and let
        // flex stretch us across the row (chat is collapsed by the parent).
        // Regular mode keeps the fixed, resizable width beside the chat.
        ...(fullBleed
          ? { flex: "1 1 0%", minWidth: 0, width: "auto" }
          : { flex: "0 0 auto", width: panelWidth }),
        background: "var(--bg-secondary)",
        borderLeft: "1px solid var(--border-subtle)",
      }}
    >
      {/* Resize handle — hidden in full-bleed (dragging a flex-filled panel is
           meaningless). */}
      {!fullBleed && (
        <div
          className={`resize-handle absolute left-0 top-0 bottom-0 z-10${isDragging ? " dragging" : ""}`}
          style={{ width: 6 }}
          onMouseDown={handleMouseDown}
        />
      )}
      {/* Header */}
      <div
        className="flex items-center justify-between px-4 py-3"
        style={{ borderBottom: "1px solid var(--border-subtle)" }}
      >
        <div className="flex items-center gap-2 min-w-0">
          {isGenerating
            ? <Loader2 className="h-4 w-4 flex-shrink-0 animate-spin" style={{ color: "var(--accent-ai)" }} />
            : <NotebookText className="h-4 w-4 flex-shrink-0" style={{ color: "var(--accent-ai)" }} />
          }
          <span
            className="text-sm font-medium truncate"
            style={{ color: "var(--text-primary)" }}
          >
            {reportTitle}
          </span>
        </div>
        <div className="flex items-center gap-1">
          {!isGenerating && reportBody && (
            <>
              <button
                onClick={handleCopy}
                className="p-1.5 rounded-lg transition-colors"
                style={{ color: copied ? "var(--accent)" : "var(--text-muted)" }}
                aria-label="Copy report to clipboard"
              >
                {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
              </button>
              <div ref={downloadMenuRef} className="relative">
                <button
                  onClick={() => setDownloadMenuOpen((o) => !o)}
                  className="p-1.5 rounded-lg transition-colors"
                  style={{ color: "var(--text-muted)" }}
                  aria-label="Download report"
                  aria-haspopup="menu"
                  aria-expanded={downloadMenuOpen}
                  title="Download as PDF / HTML / Markdown"
                >
                  <Download className="h-3.5 w-3.5" />
                </button>
                {downloadMenuOpen && (
                  <div
                    role="menu"
                    className="absolute right-0 top-full mt-1 py-1 rounded-lg shadow-lg z-50 min-w-[180px]"
                    style={{
                      background: "var(--bg-surface)",
                      border: "1px solid var(--border-default)",
                    }}
                  >
                    <button
                      role="menuitem"
                      onClick={handleDownloadPdf}
                      className="w-full flex items-center gap-2 text-left px-3 py-1.5 text-xs hover:bg-[var(--bg-elevated)]"
                      style={{ color: "var(--text-secondary)" }}
                    >
                      <FileText className="h-3.5 w-3.5 opacity-60" aria-hidden="true" />
                      Download as PDF
                    </button>
                    <button
                      role="menuitem"
                      onClick={handleDownloadHtml}
                      className="w-full flex items-center gap-2 text-left px-3 py-1.5 text-xs hover:bg-[var(--bg-elevated)]"
                      style={{ color: "var(--text-secondary)" }}
                    >
                      <FileCode className="h-3.5 w-3.5 opacity-60" aria-hidden="true" />
                      Download as HTML
                    </button>
                    <button
                      role="menuitem"
                      onClick={handleDownloadMarkdown}
                      className="w-full flex items-center gap-2 text-left px-3 py-1.5 text-xs hover:bg-[var(--bg-elevated)]"
                      style={{ color: "var(--text-secondary)" }}
                    >
                      <FileType className="h-3.5 w-3.5 opacity-60" aria-hidden="true" />
                      Download as Markdown
                    </button>
                  </div>
                )}
              </div>
              <button
                onClick={handleDownloadImage}
                className="p-1.5 rounded-lg transition-colors"
                style={{ color: "var(--text-muted)" }}
                aria-label="Download report as PNG"
                title="Download as PNG"
              >
                <ImageIcon className="h-3.5 w-3.5" />
              </button>
            </>
          )}
          {onToggleFullBleed && (
            <button
              onClick={onToggleFullBleed}
              className="p-1.5 rounded-lg transition-colors"
              style={{ color: "var(--text-muted)" }}
              aria-label={fullBleed ? "Exit full-width reading mode" : "Expand report to full width"}
              title={fullBleed ? "Back to split view (show chat)" : "Full-width reading mode"}
            >
              {fullBleed ? <Minimize2 className="h-3.5 w-3.5" /> : <Maximize2 className="h-3.5 w-3.5" />}
            </button>
          )}
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg transition-colors"
            style={{ color: "var(--text-muted)" }}
            aria-label="Close report panel"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto" ref={scrollRef}>
        {isGenerating ? (
          /* Live thinking traces during generation */
          <div className="px-4 py-3">
            <div className="flex items-center gap-2 mb-3">
              <LoaderPinwheelIcon size={14} style={{ color: "var(--accent-ai)" }} ref={spinnerRef} />
              <span className="text-xs font-medium" style={{ color: "var(--text-muted)" }}>
                Thinking…
              </span>
            </div>
            {reasoning && (
              <pre
                className="text-xs leading-relaxed whitespace-pre-wrap"
                style={{ color: "var(--text-muted)" }}
              >
                {reasoning}
              </pre>
            )}
            {tools.map((tool, i) => (
              <PanelToolBlock key={`${tool.name}-${i}`} tool={tool} isStreaming={isGenerating} />
            ))}
            {!reasoning && tools.length === 0 && (
              <pre className="text-xs leading-relaxed whitespace-pre-wrap" style={{ color: "var(--text-muted)" }}>
                Starting analysis…
              </pre>
            )}
          </div>
        ) : (
          <>
            {/* Collapsed thinking summary */}
            {(reasoning || tools.length > 0) && <ThinkingSection reasoning={reasoning} tools={tools} />}
            {/* Full report rendered as HTML. In full-width reading mode, cap
                the text to a comfortable centered measure with slightly larger
                type — a full-viewport line length hurts readability. Split view
                keeps the original compact styling unchanged. */}
            <div
              ref={contentRef}
              className={`prose-finops ${fullBleed ? "mx-auto max-w-[820px] px-8 py-8 text-[15px] leading-7 reading-mode" : "px-5 py-4 text-sm leading-relaxed"}`}
              style={{ color: "var(--text-secondary)" }}
            >
              <MarkdownText text={reportBody} />
            </div>
          </>
        )}
      </div>
    </div>
  );
}

/* Collapsed thinking block shown after report completes */
function ThinkingSection({ reasoning, tools }: { reasoning: string; tools: ToolInfo[] }) {
  const [open, setOpen] = useState(false);
  // Different label depending on what we have. "Agent Trace" emphasises
  // the tool-call breakdown when traces are loaded from the persisted
  // report (the durable, post-completion state). "Thinking process"
  // remains for the in-flight streaming state when only reasoning is
  // available. Either way the underlying view is the same.
  const hasTraces = tools.length > 0;
  const label = hasTraces ? "Agent Trace" : "Thinking process";
  const callCount = tools.length;

  return (
    <div style={{ borderBottom: "1px solid var(--border-subtle)" }}>
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2 w-full px-4 py-2.5 text-left transition-colors"
        onMouseEnter={(e) => { e.currentTarget.style.background = "var(--bg-surface)"; }}
        onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; }}
      >
        {open
          ? <ChevronDown className="h-3 w-3" style={{ color: "var(--text-muted)" }} />
          : <ChevronRight className="h-3 w-3" style={{ color: "var(--text-muted)" }} />
        }
        {hasTraces ? (
          <Wrench className="h-3 w-3" style={{ color: "var(--accent)" }} />
        ) : (
          <LoaderPinwheelIcon size={12} style={{ color: "var(--accent-ai)" }} />
        )}
        <span className="text-xs" style={{ color: "var(--text-muted)" }}>
          {label}
        </span>
        {callCount > 0 && (
          <span className="text-[10px] opacity-60">
            ({callCount} call{callCount > 1 ? "s" : ""})
          </span>
        )}
      </button>
      {open && (
        <div className="px-4 pb-3" style={{ maxHeight: 400, overflowY: "auto" }}>
          {reasoning && (
            <pre
              className="text-xs leading-relaxed whitespace-pre-wrap"
              style={{ color: "var(--text-muted)" }}
            >
              {reasoning}
            </pre>
          )}
          {tools.map((tool, i) => (
            <PanelToolBlock key={`${tool.name}-${i}`} tool={tool} />
          ))}
        </div>
      )}
    </div>
  );
}
