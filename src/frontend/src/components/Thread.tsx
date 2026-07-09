"use client";

import { useState, useCallback, useEffect, useRef, useSyncExternalStore } from "react";
import {
  ThreadPrimitive,
  ComposerPrimitive,
  MessagePrimitive,
  useThreadRuntime,
  useMessage,
} from "@assistant-ui/react";
import {
  ArrowUp,
  Sparkles,
  ChevronDown,
  ChevronRight,
  Wrench,
  Loader2,
  Copy,
  ThumbsUp,
  ThumbsDown,
  CornerDownRight,
  Check,
  NotebookText,
  Pencil,
  X,
  Square,
  CircleOff,
  BarChart3,
  TrendingUp,
  Receipt,
  CalendarSearch,
} from "lucide-react";
import { MarkdownText } from "@/components/MarkdownRenderer";
import { LoaderPinwheelIcon, type LoaderPinwheelIconHandle } from "@/components/ui/loader-pinwheel";
import { CircleCheckIcon, type CircleCheckIconHandle } from "@/components/ui/circle-check";
import { VisualizerCard } from "@/components/visualizer/VisualizerCard";
import { ReportCard } from "@/components/ReportCard";
import { useThreadBusyRemote } from "@/lib/thread-busy-context";
import { useEditingReport } from "@/lib/editing-report-context";
import { isReportModeMessage, subscribeReportModeMessages } from "@/lib/report-mode-messages";

/* ── Suggestion cards for empty state ──
 * Each card launches one of the 4 pre-loaded report templates with no
 * variable inputs (the agui_server falls back to the current month/year
 * if the user didn't specify any). Click → fires `generate-from-template`
 * which the Composer listens for and dispatches the report run.
 */

const SUGGESTIONS = [
  {
    icon: BarChart3,
    label: "FinOps Monthly Report",
    description: "Spend overview, savings opportunities, anomalies, and forecast for last month",
    templateId: "finops_monthly_report",
    templateName: "FinOps Monthly Report",
  },
  {
    icon: TrendingUp,
    label: "Org Tag Governance Review",
    description: "Org-wide tag compliance, drill-down by service and account, prioritized fixes",
    templateId: "org_tag_governance",
    templateName: "Org Tag Governance Review",
  },
  {
    icon: Receipt,
    label: "AWS Health Events Report",
    description: "Critical and high-risk events for last month with operator remediation hints",
    templateId: "health_events_report",
    templateName: "AWS Health Events Report",
  },
  {
    icon: CalendarSearch,
    label: "DX Resilience Review",
    description: "Direct Connect topology assessment with per-DXGW score and cost-to-target",
    templateId: "dx_resiliency_report",
    templateName: "Direct Connect Resilience Review",
  },
] as const;

export function Thread() {
  return (
    <ThreadPrimitive.Root className="flex flex-col h-full" style={{ background: "var(--bg-primary)" }}>
      <ThreadPrimitive.Viewport className="flex-1 overflow-y-auto px-4 pt-8 pb-4">
        <ThreadPrimitive.Empty>
          <EmptyState />
        </ThreadPrimitive.Empty>
        <ThreadPrimitive.Messages
          components={{ UserMessage, AssistantMessage }}
        />
      </ThreadPrimitive.Viewport>
      <Composer />
    </ThreadPrimitive.Root>
  );
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center h-full gap-6 max-w-2xl mx-auto px-4">
      <div className="flex flex-col items-center gap-3">
        <Sparkles className="h-8 w-8" style={{ color: "var(--accent-ai)" }} aria-hidden="true" />
        <h1 className="text-2xl font-medium" style={{ color: "var(--text-primary)", textWrap: "balance", textAlign: "center" }}>
          What do you want to know about your AWS cloud operations?
        </h1>
        <p className="text-sm" style={{ color: "var(--text-muted)", textAlign: "center" }}>
          CloudOps Agent — explore costs, governance, ops health, and security.
        </p>
      </div>
      <div className="grid grid-cols-2 gap-3 w-full max-w-lg">
        {SUGGESTIONS.map((s) => (
          <button
            key={s.templateId}
            type="button"
            onClick={() => {
              if (typeof window !== "undefined") {
                window.dispatchEvent(
                  new CustomEvent("generate-from-template", {
                    detail: {
                      template_id: s.templateId,
                      name: s.templateName,
                      variables: {},
                    },
                  })
                );
              }
            }}
            className="group flex flex-col gap-1.5 p-3.5 rounded-xl border text-left cursor-pointer transition-colors duration-150"
            style={{ borderColor: "var(--border-default)", background: "var(--bg-surface)" }}
          >
            <div className="flex items-center gap-2">
              <s.icon className="h-4 w-4" style={{ color: "var(--accent-ai)" }} aria-hidden={true} />
              <span className="text-sm font-medium text-[var(--text-secondary)] group-hover:text-[var(--text-primary)] transition-colors">{s.label}</span>
            </div>
            <span className="text-xs leading-relaxed text-[var(--text-muted)] group-hover:text-[var(--text-secondary)] transition-colors">{s.description}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

/* ── Composer with + menu and report mode chip ── */

function Composer() {
  const [reportMode, setReportMode] = useState(false);
  const [templatePickerOpen, setTemplatePickerOpen] = useState(false);
  const [templates, setTemplates] = useState<Array<{ template_id: string; name: string; user_id: string; sections?: Array<{ id: string; title: string; prompt: string }> }>>([]);
  const [templatesLoaded, setTemplatesLoaded] = useState(false);
  const [expandedTemplateId, setExpandedTemplateId] = useState<string | null>(null);
  const [varValues, setVarValues] = useState<Record<string, string>>({});
  const threadRuntime = useThreadRuntime();
  const [isStreamingLocally, setIsStreamingLocally] = useState(false);
  const isThreadBusyRemotely = useThreadBusyRemote();
  const { editing, setEditing } = useEditingReport();

  useEffect(() => {
    const update = () => setIsStreamingLocally(threadRuntime.getState().isRunning);
    update();
    return threadRuntime.subscribe(update);
  }, [threadRuntime]);

  // When the server is busy with a previous run on this thread but THIS
  // tab isn't the one streaming it (e.g. user switched threads, came back),
  // the composer has to be disabled so a new message doesn't corrupt the
  // in-flight run. Local streaming is handled by the existing stop/send split.
  const onlyRemoteBusy = !isStreamingLocally && isThreadBusyRemotely;

  useEffect(() => {
    window.__chatMode = reportMode ? "report" : null;
  }, [reportMode]);

  useEffect(() => {
    const handler = () => {
      setReportMode(false);
      // Also clear the window global DIRECTLY. setReportMode(false) is a no-op
      // when reportMode is already false — which is exactly the case for report
      // flows that set window.__chatMode from page.tsx (template generate,
      // adhoc report) without flipping this component's state. In that case the
      // reportMode effect below never re-runs, so the global would stay stuck at
      // "report" and the NEXT (normal) turn would wrongly render as a report.
      // Clearing here guarantees the reset regardless of React state.
      if (typeof window !== "undefined") window.__chatMode = null;
    };
    window.addEventListener("chat-stream-done", handler);
    return () => window.removeEventListener("chat-stream-done", handler);
  }, []);

  // Allow external code (e.g. template generate) to activate report mode
  useEffect(() => {
    const handler = () => setReportMode(true);
    window.addEventListener("activate-report-mode", handler);
    return () => window.removeEventListener("activate-report-mode", handler);
  }, []);

  // Load templates when picker opens
  useEffect(() => {
    if (!templatePickerOpen || templatesLoaded) return;
    (async () => {
      try {
        const { getToken, getActorId } = await import("@/lib/auth");
        const { listTemplates } = await import("@/lib/runtime-client");
        const actorId = getActorId();
        const token = await getToken();
        const result = await listTemplates(actorId, async () => token);
        // Only show templates with sections
        setTemplates(result.filter((t) => t.sections && t.sections.length > 0) as typeof templates);
        setTemplatesLoaded(true);
      } catch (e) {
        console.error("Failed to load templates:", e);
      }
    })();
  }, [templatePickerOpen, templatesLoaded]);

  /** Extract unique {placeholder} names from all section prompts. */
  const getTemplateVars = useCallback((t: typeof templates[number]): string[] => {
    const vars = new Set<string>();
    for (const s of t.sections ?? []) {
      for (const m of s.prompt.matchAll(/\{(\w+)\}/g)) vars.add(m[1]);
    }
    return Array.from(vars).sort();
  }, []);

  const handleSelectTemplate = useCallback((t: typeof templates[number]) => {
    const vars = getTemplateVars(t);
    if (vars.length === 0) {
      // No variables — generate immediately
      setTemplatePickerOpen(false);
      setExpandedTemplateId(null);
      if (typeof window !== "undefined") {
        window.dispatchEvent(new CustomEvent("generate-from-template", { detail: { template_id: t.template_id, name: t.name, variables: {} } }));
      }
    } else {
      // Has variables — expand to show inputs
      if (expandedTemplateId === t.template_id) {
        setExpandedTemplateId(null);
        return;
      }
      setExpandedTemplateId(t.template_id);
      // Pre-fill defaults
      const defaults: Record<string, string> = {};
      const now = new Date();
      const prevMonth = now.getMonth() === 0 ? 11 : now.getMonth() - 1;
      const prevYear = now.getMonth() === 0 ? now.getFullYear() - 1 : now.getFullYear();
      const monthNames = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
      for (const v of vars) {
        if (v === "month") defaults[v] = monthNames[prevMonth];
        else if (v === "year") defaults[v] = String(prevYear);
        else defaults[v] = "";
      }
      setVarValues(defaults);
    }
  }, [getTemplateVars, expandedTemplateId]);

  const handleGenerateWithVars = useCallback((t: typeof templates[number]) => {
    setTemplatePickerOpen(false);
    setExpandedTemplateId(null);
    const varDesc = Object.entries(varValues)
      .filter(([, v]) => v.trim())
      .map(([k, v]) => `${k}: ${v}`)
      .join(", ");
    if (typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent("generate-from-template", { detail: { template_id: t.template_id, name: t.name, variables: varValues, varDesc } }));
    }
  }, [varValues]);

  return (
    <ComposerPrimitive.Root className="mx-auto w-full max-w-2xl px-4 pb-4">
      <div
        className="composer-ring rounded-2xl px-4 py-3"
        style={{ background: "var(--bg-surface)", border: "1px solid var(--border-default)" }}
      >
        {/* Report mode chip */}
        {reportMode && !editing && (
          <div className="mode-chip flex items-center gap-1.5 mb-2">
            <div
              className="flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs"
              style={{ background: "var(--accent-surface)", border: "1px solid var(--accent-border)", color: "var(--accent-ai)" }}
            >
              <NotebookText className="h-3 w-3" />
              <span className="font-medium">Report mode</span>
              <button
                onClick={() => setReportMode(false)}
                className="ml-0.5 rounded-full p-0.5 transition-colors hover:bg-[var(--accent-hover)]"
                aria-label="Remove report mode"
              >
                <X className="h-3 w-3" />
              </button>
            </div>
          </div>
        )}

        {/* Editing-report chip. Shown when a ReportCard opened an edit
            session — the next send goes to the report-edit backend path
            and creates a new version. */}
        {editing && (
          <div className="mode-chip flex items-center gap-1.5 mb-2">
            <div
              className="flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs max-w-full"
              style={{ background: "var(--accent-surface)", border: "1px solid var(--accent-border)", color: "var(--accent-ai)" }}
            >
              <NotebookText className="h-3 w-3 shrink-0" />
              <span className="font-medium shrink-0">
                Editing{editing.version ? ` v${editing.version}` : ""}:
              </span>
              <span className="truncate max-w-[320px]" title={editing.title}>
                {editing.title}
              </span>
              <button
                onClick={() => setEditing(null)}
                className="ml-0.5 rounded-full p-0.5 transition-colors hover:bg-[var(--accent-hover)] shrink-0"
                aria-label="Cancel editing report"
              >
                <X className="h-3 w-3" />
              </button>
            </div>
          </div>
        )}

        {/* Input row */}
        <div className="flex items-center gap-2.5">
          {/* Template picker button */}
          <div className="relative">
            <button
              onClick={() => { setTemplatePickerOpen(!templatePickerOpen); setTemplatesLoaded(false); }}
              className="flex h-8 w-8 items-center justify-center rounded-lg shrink-0 transition-all duration-150 active:scale-95"
              style={{
                color: templatePickerOpen ? "var(--accent-ai)" : "var(--text-muted)",
                background: templatePickerOpen ? "var(--accent-surface)" : "transparent",
                border: templatePickerOpen ? "1px solid var(--accent-border-subtle)" : "1px solid var(--border-default)",
              }}
              aria-label="Generate report from template"
            >
              <NotebookText className="h-4 w-4" />
            </button>
            {templatePickerOpen && (
              <>
                <div className="fixed inset-0 z-10" onClick={() => setTemplatePickerOpen(false)} />
                <div className="absolute bottom-full left-0 mb-2 rounded-lg p-1 z-20 min-w-[240px] max-h-[280px] overflow-y-auto"
                  style={{ background: "var(--bg-elevated)", border: "1px solid var(--border-default)", boxShadow: "0 4px 16px var(--shadow-dropdown)" }}>
                  <div className="px-2.5 py-1.5 text-[10px] font-medium uppercase tracking-wider" style={{ color: "var(--text-muted)" }}>
                    Generate Report
                  </div>
                  {templates.length === 0 ? (
                    <div className="px-2.5 py-3 text-xs" style={{ color: "var(--text-muted)" }}>
                      {templatesLoaded ? "No templates available" : "Loading..."}
                    </div>
                  ) : (
                    templates.map((t) => {
                      const isExpanded = expandedTemplateId === t.template_id;
                      const vars = isExpanded ? getTemplateVars(t) : [];
                      return (
                        <div key={t.template_id}>
                          <button onClick={() => handleSelectTemplate(t)}
                            className="flex flex-col w-full px-2.5 py-2 text-left rounded-md transition-colors hover:bg-[var(--bg-surface)]"
                            style={isExpanded ? { background: "var(--bg-surface)" } : undefined}>
                            <span className="text-xs font-medium" style={{ color: "var(--text-primary)" }}>{t.name}</span>
                            <span className="text-[10px]" style={{ color: "var(--text-muted)" }}>
                              {t.sections?.length ?? 0} sections
                              {t.user_id === "system" ? " · pre-loaded" : ""}
                            </span>
                          </button>
                          {isExpanded && vars.length > 0 && (
                            <div className="px-2.5 pb-2 space-y-1.5">
                              {vars.map((v) => (
                                <div key={v} className="flex items-center gap-2">
                                  <label className="text-[10px] font-medium capitalize w-12 shrink-0" style={{ color: "var(--text-muted)" }}>{v}</label>
                                  <input value={varValues[v] ?? ""} onChange={(e) => setVarValues((prev) => ({ ...prev, [v]: e.target.value }))}
                                    className="flex-1 text-xs px-2 py-1 rounded"
                                    style={{ background: "var(--bg-primary)", border: "1px solid var(--border-default)", color: "var(--text-primary)" }}
                                    onKeyDown={(e) => { if (e.key === "Enter") handleGenerateWithVars(t); }} />
                                </div>
                              ))}
                              <button onClick={() => handleGenerateWithVars(t)}
                                className="w-full text-xs py-1 rounded-md font-medium mt-1"
                                style={{ background: "var(--accent)", color: "white" }}>
                                Generate
                              </button>
                            </div>
                          )}
                        </div>
                      );
                    })
                  )}
                </div>
              </>
            )}
          </div>

          {/* Report-mode toggle — opt into report-formatted responses without
              picking a template. When on, the next send goes to the report
              path and creates a fresh report from the user's prompt rather
              than a pre-built template. Hidden when an Edit session is
              already active (the Editing chip owns that state). */}
          {!reportMode && !editing && (
            <button
              onClick={() => setReportMode(true)}
              className="flex h-8 w-8 items-center justify-center rounded-lg shrink-0 transition-all duration-150 active:scale-95"
              style={{
                color: "var(--text-muted)",
                background: "transparent",
                border: "1px solid var(--border-default)",
              }}
              aria-label="Toggle report mode"
              title="Report mode — response will be formatted as a report"
            >
              <Pencil className="h-4 w-4" />
            </button>
          )}

          <ComposerPrimitive.Input
            placeholder={onlyRemoteBusy
              ? "This thread is still running on the backend..."
              : "Ask about your AWS cloud operations..."}
            className="flex-1 resize-none border-0 bg-transparent text-sm outline-none min-h-[20px] max-h-[120px] disabled:opacity-60"
            style={{ color: "var(--text-primary)" }}
            autoFocus={!onlyRemoteBusy}
            disabled={onlyRemoteBusy}
          />
          {isStreamingLocally ? (
            <button
              onClick={() => threadRuntime.cancelRun()}
              className="flex h-8 w-8 items-center justify-center rounded-lg shrink-0 transition-all duration-150 active:scale-95"
              style={{ background: "var(--bg-elevated)", border: "1px solid var(--border-default)" }}
              aria-label="Stop generating"
            >
              <Square className="h-3.5 w-3.5" style={{ color: "var(--text-primary)", fill: "var(--text-primary)" }} />
            </button>
          ) : onlyRemoteBusy ? (
            <button
              disabled
              className="flex h-8 w-8 items-center justify-center rounded-lg shrink-0 opacity-40 cursor-not-allowed"
              style={{ background: "var(--bg-elevated)", border: "1px solid var(--border-default)" }}
              aria-label="Backend still running — wait for the current run to finish"
              title="Backend still running — wait for the current run to finish"
            >
              <Loader2 className="h-3.5 w-3.5 animate-spin" style={{ color: "var(--text-muted)" }} aria-hidden="true" />
            </button>
          ) : (
            <ComposerPrimitive.Send
              className="flex h-8 w-8 items-center justify-center rounded-lg text-white shrink-0 transition-all duration-150 active:scale-95 disabled:opacity-20 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-[var(--border-active)]"
              style={{ background: "var(--accent-ai-dim)" }}
              aria-label="Send message"
            >
              <ArrowUp className="h-4 w-4" aria-hidden="true" />
            </ComposerPrimitive.Send>
          )}
        </div>
      </div>
      <div className="flex items-center justify-end mt-2 px-1">
        <span className="text-[10px]" style={{ color: "var(--text-muted)" }}>
          Responses may be inaccurate — always verify critical data
        </span>
      </div>
    </ComposerPrimitive.Root>
  );
}

/* ── User message — right-aligned bubble ── */

function UserMessage() {
  // Badge a report-mode prompt two ways:
  //  1. LIVE (immediately on send): the adapter records this message id in the
  //     report-mode store — covers templated, freeform, and edit turns.
  //  2. RELOAD: history carries a hidden <report-request/> text part.
  // Either source lights up the "Report mode" badge; the custom Text renderer
  // below hides the marker from the visible bubble.
  const messageId = useMessage((m) => m.id);
  const liveReport = useSyncExternalStore(
    subscribeReportModeMessages,
    () => isReportModeMessage(messageId),
    () => false,
  );
  const persistedReport = useMessage((m) =>
    m.content.some((p) => p.type === "text" && p.text.trim() === "<report-request/>")
  );
  const isReport = liveReport || persistedReport;
  return (
    <MessagePrimitive.Root className="animate-msg mx-auto max-w-2xl pt-6 pb-2 flex flex-col items-end gap-1">
      {isReport && (
        <div
          className="flex items-center gap-1 text-[11px] font-medium px-2 py-0.5 rounded-full"
          style={{ color: "var(--accent-ai)", background: "var(--accent-surface)" }}
          title="Sent in report mode"
        >
          <NotebookText className="h-3 w-3" />
          Report mode
        </div>
      )}
      <div
        className="rounded-2xl px-4 py-2.5 max-w-[80%] text-sm"
        style={{
          background: "var(--bg-elevated)",
          color: "var(--text-primary)",
        }}
      >
        <MessagePrimitive.Content
          components={{
            // Hide the internal report-request marker part from the visible bubble.
            Text: ({ text }) =>
              text.trim() === "<report-request/>" ? null : <>{text}</>,
          }}
        />
      </div>
    </MessagePrimitive.Root>
  );
}

/* ── Assistant message — clean flow with action bar ── */

function AssistantMessage() {
  const isStreaming = useMessage((m) => m.status?.type === "running");

  return (
    <MessagePrimitive.Root className="animate-msg mx-auto max-w-2xl py-2">
      <div className="prose-finops text-sm">
        <MessagePrimitive.Content
          components={{
            Text: ({ text }) => <AssistantTextContent text={text} />,
          }}
        />
        {isStreaming && <span className="streaming-cursor" />}
      </div>
      <ActionBar />
      <FollowUpSuggestions />
    </MessagePrimitive.Root>
  );
}

/* ── Action bar — copy, share, thumbs up/down ── */

function ActionBar() {
  const [copied, setCopied] = useState(false);
  const [vote, setVote] = useState<"up" | "down" | null>(null);
  const message = useMessage();

  const handleCopy = useCallback(() => {
    const INTERNAL_TAG_RE = /^<(think|tool|report-tool|report-body|suggestions|artifact|status|visualizer-state|report-pending)[\s>/]/;
    const textParts = message.content
      .filter((c): c is { type: "text"; text: string } => c.type === "text")
      .map((c) => c.text)
      .filter((t) => !INTERNAL_TAG_RE.test(t));
    // Join and strip preamble text before the first heading or rule
    let text = textParts.join("\n").trim();
    const headingIdx = text.search(/^(#{1,6}\s|---)/m);
    if (headingIdx > 0) text = text.slice(headingIdx);
    navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }, [message]);

  if (message.role !== "assistant") return null;

  // Don't show action bar while streaming (status indicator only)
  const hasRealContent = message.content.some(
    (c) => c.type === "text" && "text" in c && !(c as { text: string }).text.match(/^<(status|think\s)/)
  );
  if (!hasRealContent) return null;

  return (
    <div className="flex items-center gap-0.5 mt-3 pt-2" style={{ borderTop: "1px solid var(--border-subtle)" }}>
      <ActionButton
        onClick={handleCopy}
        label={copied ? "Copied" : "Copy"}
        icon={copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
        active={copied}
      />
      <div className="flex-1" />
      <ActionButton
        onClick={() => setVote(vote === "up" ? null : "up")}
        label="Helpful"
        icon={<ThumbsUp className="h-4 w-4" />}
        active={vote === "up"}
        activeColor="var(--accent)"
      />
      <ActionButton
        onClick={() => setVote(vote === "down" ? null : "down")}
        label="Not helpful"
        icon={<ThumbsDown className="h-4 w-4" />}
        active={vote === "down"}
        activeColor="var(--danger)"
      />
    </div>
  );
}

function ActionButton({ onClick, label, icon, active, activeColor }: { onClick: () => void; label: string; icon: React.ReactNode; active?: boolean; activeColor?: string }) {
  return (
    <button
      onClick={onClick}
      className="p-2 rounded-lg transition-all duration-150 active:scale-90 hover:bg-[var(--bg-elevated)]"
      style={{ color: active ? (activeColor || "var(--accent)") : "var(--text-muted)" }}
      aria-label={label}
      title={label}
    >
      {icon}
    </button>
  );
}

/* ── Follow-up suggestions — AI-generated from <suggestions> tag ── */

function FollowUpSuggestions() {
  const message = useMessage();
  const threadRuntime = useThreadRuntime();

  if (message.role !== "assistant") return null;

  // Only show on the last assistant message
  const messages = threadRuntime.getState().messages;
  const lastAssistantIdx = [...messages].reverse().findIndex((m) => m.role === "assistant");
  const lastAssistantId = lastAssistantIdx >= 0 ? messages[messages.length - 1 - lastAssistantIdx]?.id : null;
  if (message.id !== lastAssistantId) return null;

  // Extract suggestions from <suggestions> tag in message content
  const followUps: string[] = [];
  for (const part of message.content) {
    if (part.type === "text" && "text" in part) {
      const match = (part as { text: string }).text.match(/^<suggestions>([\s\S]*?)<\/suggestions>$/);
      if (match) {
        try {
          const parsed = JSON.parse(match[1]);
          if (Array.isArray(parsed)) followUps.push(...parsed);
        } catch { /* ignore parse errors */ }
      }
    }
  }

  if (followUps.length === 0) return null;

  return (
    <div className="mt-4">
      <h3 className="text-sm font-semibold mb-1" style={{ color: "var(--text-primary)" }}>
        Follow-ups
      </h3>
      {followUps.map((suggestion) => (
        <button
          key={suggestion}
          onClick={() => {
            threadRuntime.append({
              role: "user",
              content: [{ type: "text", text: suggestion }],
            });
          }}
          className="flex items-center gap-3 w-full py-3 text-sm text-left transition-colors duration-150 group"
          style={{
            color: "var(--text-secondary)",
            borderTop: "1px solid var(--border-subtle)",
          }}
        >
          <CornerDownRight
            className="h-4 w-4 shrink-0"
            style={{ color: "var(--text-muted)" }}
            aria-hidden="true"
          />
          <span className="group-hover:text-[var(--text-primary)] transition-colors">
            {suggestion}
          </span>
        </button>
      ))}
    </div>
  );
}

/* ── Thinking / reasoning block ── */

function ThinkingBlock({ text, isStreaming: isStreamingProp, seconds }: { text: string; isStreaming: boolean; seconds?: number }) {
  const [open, setOpen] = useState(false);
  const spinnerRef = useRef<LoaderPinwheelIconHandle>(null);
  const checkRef = useRef<CircleCheckIconHandle>(null);
  const threadRuntime = useThreadRuntime();
  const message = useMessage();
  const messageId = message.id;
  const [skipped, setSkipped] = useState(false);

  useEffect(() => {
    if (skipped) return; // already latched
    const check = () => {
      if (isStreamingProp && !threadRuntime.getState().isRunning) {
        setSkipped(true);
      }
    };
    check();
    return threadRuntime.subscribe(check);
  }, [threadRuntime, isStreamingProp, skipped]);

  const isStreaming = isStreamingProp && !skipped;

  useEffect(() => {
    if (isStreaming) {
      spinnerRef.current?.startAnimation();
    } else {
      spinnerRef.current?.stopAnimation();
      if (!skipped) checkRef.current?.startAnimation();
    }
  }, [isStreaming, skipped]);

  const label = isStreaming
    ? "Thinking…"
    : skipped
      ? "Answer skipped"
      : seconds
        ? `Thought for ${seconds} second${seconds !== 1 ? "s" : ""}`
        : "Thought process";
  return (
    <div className="mb-3 text-xs">
      <button
        onClick={() => {
          if (!isStreaming) {
            window.dispatchEvent(new CustomEvent("open-trace", { detail: { messageId } }));
          }
        }}
        className="flex items-center gap-1.5 py-1"
        style={{ color: "var(--text-muted)" }}
        aria-label={label}
      >
        {isStreaming ? (
          <LoaderPinwheelIcon ref={spinnerRef} size={14} style={{ color: "var(--accent-ai)" }} aria-hidden="true" />
        ) : skipped ? (
          <CircleOff className="h-3.5 w-3.5" style={{ color: "var(--text-muted)" }} aria-hidden="true" />
        ) : (
          <CircleCheckIcon ref={checkRef} size={14} style={{ color: "var(--accent-ai)" }} aria-hidden="true" />
        )}
        <span>{label}</span>
        {!isStreaming && <ChevronRight className="h-3 w-3" aria-hidden="true" />}
      </button>
    </div>
  );
}

/* ── Tool call block ── */

function ToolBlock({ name, input, output, isStreaming }: { name: string; input: Record<string, unknown>; output?: string; isStreaming: boolean }) {
  const message = useMessage();
  const messageId = message.id;
  return (
    <div
      className="rounded-lg mb-3 overflow-hidden text-xs"
      style={{ border: "1px solid var(--accent-border-subtle)", background: "var(--accent-bg-faint)" }}
    >
      <button
        onClick={() => {
          window.dispatchEvent(new CustomEvent("open-trace", { detail: { messageId } }));
        }}
        className="flex items-center gap-2 w-full px-3 py-2 text-left"
        style={{ color: "var(--text-muted)" }}
        aria-label={isStreaming ? "Agent working" : "View agent trace"}
      >
        {isStreaming ? (
          <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" style={{ color: "var(--accent)" }} aria-hidden="true" />
        ) : (
          <Wrench className="h-3.5 w-3.5 shrink-0" style={{ color: "var(--accent)" }} aria-hidden="true" />
        )}
        <span className="font-medium" style={{ color: "var(--accent)" }}>
          {isStreaming ? "Agent working…" : "Agent Trace"}
        </span>
        <span className="text-[10px] opacity-60">{name}</span>
        <ChevronRight className="h-3 w-3 ml-auto" aria-hidden="true" />
      </button>
    </div>
  );
}

/* ── Nested tool trace item — collapsible with input/output, same format as ToolBlock ── */
interface TraceEntry {
  tool_name: string;
  duration_s?: number;
  status?: string;
  input?: Record<string, unknown>;
  output?: string;
  tool_trace?: TraceEntry[];
}

function NestedToolTrace({ trace, depth }: { trace: TraceEntry; depth: number }) {
  const [open, setOpen] = useState(false);
  const statusIcon = trace.status === "success" ? "✓" : trace.status === "started" ? "⏳" : "✗";
  const statusColor = trace.status === "success" ? "var(--accent)" : trace.status === "started" ? "var(--text-muted)" : "#ef4444";
  const duration = trace.duration_s != null ? `${trace.duration_s}s` : "";
  const hasChildren = (trace.tool_trace && trace.tool_trace.length > 0) || false;
  const hasInput = trace.input && Object.keys(trace.input).length > 0;
  const hasOutput = !!trace.output;
  const hasContent = hasInput || hasOutput || hasChildren;

  return (
    <div
      className="rounded-md overflow-hidden text-xs"
      style={{
        border: "1px solid var(--accent-border-subtle)",
        background: "var(--accent-bg-faint)",
        opacity: depth > 0 ? 0.95 : 1,
        marginTop: "4px",
      }}
    >
      <button
        onClick={() => hasContent && setOpen(!open)}
        className="flex items-center gap-1.5 w-full px-2.5 py-1.5 text-left"
        style={{ color: "var(--text-muted)", cursor: hasContent ? "pointer" : "default" }}
        aria-expanded={open}
      >
        <span style={{ color: statusColor, fontSize: "10px" }}>{statusIcon}</span>
        <Wrench className="h-2.5 w-2.5 shrink-0" style={{ color: "var(--accent)", opacity: 0.6 }} aria-hidden="true" />
        <span className="font-medium" style={{ color: "var(--accent)" }}>{trace.tool_name}</span>
        {duration && <span className="opacity-60">({duration})</span>}
        {hasChildren && (
          <span className="text-[10px] opacity-50">
            ({trace.tool_trace!.length} sub-call{trace.tool_trace!.length > 1 ? "s" : ""})
          </span>
        )}
        {hasContent && (
          open
            ? <ChevronDown className="h-2.5 w-2.5 ml-auto" aria-hidden="true" />
            : <ChevronRight className="h-2.5 w-2.5 ml-auto" aria-hidden="true" />
        )}
      </button>
      {open && (
        <div className="px-2.5 pb-2 space-y-1.5">
          {hasInput && (
            <div>
              <div className="text-[10px] font-medium uppercase tracking-wider mb-0.5" style={{ color: "var(--text-muted)", opacity: 0.7 }}>Input</div>
              <pre className="overflow-x-auto text-[11px] leading-relaxed" style={{ color: "var(--text-muted)" }}>
                {JSON.stringify(trace.input, null, 2)}
              </pre>
            </div>
          )}
          {hasChildren && (
            <div>
              <div className="text-[10px] font-medium uppercase tracking-wider mb-0.5" style={{ color: "var(--text-muted)", opacity: 0.7 }}>Sub-agent calls</div>
              {trace.tool_trace!.map((nested, i) => (
                <NestedToolTrace key={i} trace={nested} depth={depth + 1} />
              ))}
            </div>
          )}
          {hasOutput && (
            <div>
              <div className="text-[10px] font-medium uppercase tracking-wider mb-0.5" style={{ color: "var(--text-muted)", opacity: 0.7 }}>Output</div>
              <div className="overflow-x-auto text-[11px] leading-relaxed" style={{ color: "var(--text-secondary)", maxHeight: "200px", overflowY: "auto" }}>
                <MarkdownText text={trace.output || ""} />
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ── Artifact card — inline report reference (generating or complete) ── */

function ArtifactCard({ meta }: { meta: { title: string; generating?: boolean; report_id?: string; version?: number } }) {
  const message = useMessage();
  const { setEditing } = useEditingReport();
  const isGenerating = meta.generating === true;
  const canEdit = !isGenerating && Boolean(meta.report_id);

  const handleEdit = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!meta.report_id) return;
    setEditing({ report_id: meta.report_id, title: meta.title, version: meta.version });
  };

  return (
    <button
      onClick={() => {
        window.dispatchEvent(
          new CustomEvent("open-artifact", {
            detail: { messageId: message.id, reportId: meta.report_id },
          })
        );
      }}
      className="artifact-card flex items-center gap-3.5 w-full rounded-xl p-4 my-3 text-left group active:scale-[0.98] transition-transform duration-150"
      style={{
        background: "var(--bg-surface)",
        border: "1px solid var(--border-default)",
      }}
    >
      <div
        className="h-10 w-10 rounded-xl flex items-center justify-center shrink-0"
        style={{ background: "var(--accent-surface)", border: "1px solid var(--accent-border-subtle)" }}
      >
        {isGenerating
          ? <Loader2 className="h-5 w-5 animate-spin" style={{ color: "var(--accent-ai)" }} />
          : <NotebookText className="h-5 w-5" style={{ color: "var(--accent-ai)" }} />
        }
      </div>
      <div className="flex-1 min-w-0">
        <div className="text-sm font-medium truncate flex items-center gap-1.5" style={{ color: "var(--text-primary)" }}>
          <span className="truncate">{meta.title}</span>
          {meta.version && meta.version > 1 && (
            <span
              className="text-[10px] px-1.5 py-0.5 rounded-full shrink-0"
              style={{ background: "var(--bg-elevated)", color: "var(--text-muted)", border: "1px solid var(--border-subtle)" }}
            >
              v{meta.version}
            </span>
          )}
        </div>
        <div className="text-xs mt-0.5 flex items-center gap-1.5" style={{ color: "var(--text-muted)" }}>
          {isGenerating ? (
            <>
              <span className="artifact-pulse inline-block h-1.5 w-1.5 rounded-full" style={{ background: "var(--accent-ai)" }} />
              Analyzing data and generating report…
            </>
          ) : (
            "View full report"
          )}
        </div>
      </div>
      {canEdit && (
        <span
          onClick={handleEdit}
          className="flex items-center gap-1 px-2 py-1 rounded-md text-[11px] transition-colors hover:bg-[var(--accent-hover)] shrink-0"
          style={{ color: "var(--accent-ai)", border: "1px solid var(--accent-border-subtle)" }}
          role="button"
          tabIndex={0}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.stopPropagation();
              handleEdit(e as unknown as React.MouseEvent);
            }
          }}
          aria-label="Edit this report"
        >
          <Pencil className="h-3 w-3" />
          Edit
        </span>
      )}
      {!isGenerating && (
        <ChevronRight
          className="h-4 w-4 shrink-0 opacity-30 group-hover:opacity-70 transition-opacity"
          style={{ color: "var(--text-muted)" }}
        />
      )}
    </button>
  );
}

/* ── Content router — parses structured tags ── */

function AssistantTextContent({ text }: { text: string }) {
  // Suggestions are rendered by FollowUpSuggestions, not inline
  if (text.match(/^<suggestions>[\s\S]*<\/suggestions>$/)) {
    return null;
  }

  // Report body and report tools are hidden inline (only shown in the artifact panel)
  if (text.match(/^<report-body>[\s\S]*<\/report-body>$/) || text.match(/^<report-tool>[\s\S]*<\/report-tool>$/)) {
    return null;
  }

  const thinkMatch = text.match(/^<think( streaming| seconds=(\d+))?>\n?([\s\S]*?)\n?<\/think>$/);
  if (thinkMatch) {
    return <ThinkingBlock text={thinkMatch[3]} isStreaming={thinkMatch[1]?.trim() === "streaming"} seconds={thinkMatch[2] ? parseInt(thinkMatch[2]) : undefined} />;
  }

  const toolMatch = text.match(/^<tool>([\s\S]*?)<\/tool>$/);
  if (toolMatch) {
    try {
      const parsed = JSON.parse(toolMatch[1]);
      return <ToolBlock name={parsed.name} input={parsed.input} output={parsed.output} isStreaming={!!parsed.isStreaming} />;
    } catch {
      return <MarkdownText text={text} />;
    }
  }

  // Artifact card — clickable inline report reference
  const artifactMatch = text.match(/^<artifact>([\s\S]*?)<\/artifact>$/);
  if (artifactMatch) {
    try {
      const meta = JSON.parse(artifactMatch[1]);
      return <ArtifactCard meta={meta} />;
    } catch {
      return null;
    }
  }

  // Visualizer card — clickable inline reference that opens the
  // VisualizerPanel. Emitted by MyRuntimeProvider when a discover_dx_topology
  // or assess_dx_resiliency tool result arrives.
  if (text.match(/^<visualizer-state>[\s\S]*<\/visualizer-state>$/)) {
    return <VisualizerCard payloadText={text} />;
  }

  // Async-report kickoff marker. The supervisor emits this immediately
  // after starting a background report worker; the card polls
  // /reports/{id}/status until the row reaches a terminal state, then
  // swaps to a clickable artifact that opens the report panel.
  const pendingMatch = text.match(/^<report-pending\s+([^/>]*)\/>$/);
  if (pendingMatch) {
    return <ReportCard markerText={pendingMatch[1].trim()} />;
  }

  return <MarkdownText text={text} />;
}
