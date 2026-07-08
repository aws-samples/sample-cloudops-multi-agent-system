"use client";

import { useState, useCallback, useEffect, useRef } from "react";
import { v4 as uuidv4 } from "uuid";
import { useAssistantRuntime } from "@assistant-ui/react";
import type { ThreadMessageLike } from "@assistant-ui/react";
import { PanelLeftClose, PanelLeftOpen, Sun, Moon, NotebookText, LogOut, User } from "lucide-react";
import { getToken, getActorId, getUserEmail, signOut, isDevBypass } from "@/lib/auth";
import { listTemplates, createTemplate, updateTemplate, deleteTemplate } from "@/lib/runtime-client";
import type { Template } from "@/lib/runtime-client";
import { Thread } from "@/components/Thread";
import { ThreadSidebar } from "@/components/ThreadSidebar";
import { ReportPanel } from "@/components/ReportPanel";
import { TracePanel } from "@/components/TracePanel";
import { VisualizerPanel } from "@/components/visualizer/VisualizerPanel";
import { ReportTemplateEditor } from "@/components/ReportTemplateEditor";
import { ReportTemplateList } from "@/components/ReportTemplateList";
import { ThreadBusyCard } from "@/components/ThreadBusyCard";
import { TourMenuButton } from "@/lib/tours/TourMenuButton";
import { useTheme } from "@/lib/theme";
import type { TopologyData, CombinedAssessment } from "@/lib/topology";
import { extractVisualizerStateFromMemory } from "@/lib/visualizer-state";
import { useThreadActivity } from "@/lib/thread-activity";
import { ThreadBusyProvider } from "@/lib/thread-busy-context";

function ChatSkeleton() {
  return (
    <div className="flex-1 flex flex-col px-4 pt-8 pb-4 max-w-2xl mx-auto w-full">
      <div className="flex justify-end mb-6">
        <div className="skeleton h-10 rounded-2xl" style={{ width: "35%" }} />
      </div>
      <div className="space-y-3">
        <div className="skeleton h-4 rounded" style={{ width: "90%" }} />
        <div className="skeleton h-4 rounded" style={{ width: "75%" }} />
        <div className="skeleton h-4 rounded" style={{ width: "82%" }} />
        <div className="skeleton h-4 rounded" style={{ width: "60%" }} />
        <div className="mt-6" />
        <div className="skeleton h-32 rounded-lg" style={{ width: "100%" }} />
        <div className="mt-4" />
        <div className="skeleton h-4 rounded" style={{ width: "88%" }} />
        <div className="skeleton h-4 rounded" style={{ width: "70%" }} />
        <div className="skeleton h-4 rounded" style={{ width: "45%" }} />
      </div>
    </div>
  );
}

export default function Home() {
  const [threadId, setThreadId] = useState<string>(() => uuidv4());
  const [activeArtifactMessageId, setActiveArtifactMessageId] = useState<string | null>(null);
  // Optional report_id carried by the open-artifact event when the source
  // is a ReportCard for an async-generated report. ReportPanel falls back
  // to loading the report from the REST API when the message content has
  // only a <report-pending> marker (no <report-body> yet).
  const [activeArtifactReportId, setActiveArtifactReportId] = useState<string | null>(null);
  const [activeTraceMessageId, setActiveTraceMessageId] = useState<string | null>(null);
  const [activeVisualizerMessageId, setActiveVisualizerMessageId] = useState<string | null>(null);
  const [messagesLoading, setMessagesLoading] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  // When the visualizer is open, the user can collapse the main chat for a
  // full-bleed topology view. Resets to false when the visualizer closes so
  // the next chat turn is visible again. The sidebar stays force-collapsed
  // while the visualizer is open — toggling is the chat column only.
  const [chatCollapsed, setChatCollapsed] = useState(false);
  const [editorOpen, setEditorOpen] = useState(false);
  const [editingTemplate, setEditingTemplate] = useState<Template | null>(null);
  const [templateListOpen, setTemplateListOpen] = useState(false);
  const pendingPromptRef = useRef<string | null>(null);

  // Sync sidebar state from localStorage after hydration
  useEffect(() => {
    const collapsed = localStorage.getItem("sidebar-collapsed") === "true";
    if (collapsed) setSidebarOpen(false);
  }, []);
  const { theme, toggle: toggleTheme } = useTheme();
  const runtime = useAssistantRuntime();
  const threadActivity = useThreadActivity(threadId);
  const threadBusyRemote = threadActivity.status === "running";
  // Bumped each time a remote run completes (the activity poll transitions
  // running → idle). The history-load effect includes this in its dep array
  // so it re-hydrates from AgentCore Memory once the supervisor has finished
  // writing. Without this the user sees an empty chat after the busy card
  // vanishes — the stream landed in memory AFTER the initial history load,
  // and nothing forces a second load until the user navigates away and back.
  const [remoteRunCompletionTick, setRemoteRunCompletionTick] = useState(0);
  const prevThreadBusyRef = useRef(false);
  const prevLoadedThreadRef = useRef<string>("");
  useEffect(() => {
    if (prevThreadBusyRef.current && !threadBusyRemote) {
      setRemoteRunCompletionTick((t) => t + 1);
    }
    prevThreadBusyRef.current = threadBusyRemote;
  }, [threadBusyRemote]);

  // Build trace data from the current thread's last assistant message
  const [traceData, setTraceData] = useState<{ thinking?: string; tools: Array<{ name: string; input: Record<string, unknown>; output?: string; tool_trace?: Array<{ tool_name: string; duration_s?: number; status?: string; input?: Record<string, unknown>; output?: string; tool_trace?: Array<{ tool_name: string; duration_s?: number; status?: string; input?: Record<string, unknown>; output?: string }> }>; isStreaming?: boolean }>; isStreaming: boolean }>({ tools: [], isStreaming: false });

  useEffect(() => {
    const update = () => {
      const state = runtime.thread.getState();
      const messages = state.messages;

      // Find the specific message by ID, or fall back to last assistant message
      let targetMessage = activeTraceMessageId
        ? messages.find(m => m.id === activeTraceMessageId)
        : undefined;
      if (!targetMessage) {
        targetMessage = [...messages].reverse().find(m => m.role === "assistant");
      }
      if (!targetMessage || targetMessage.role !== "assistant") {
        setTraceData({ tools: [], isStreaming: state.isRunning });
        return;
      }

      let thinking = "";
      const tools: typeof traceData.tools = [];
      for (const part of targetMessage.content) {
        if (part.type !== "text" || !("text" in part)) continue;
        const text = (part as { text: string }).text;
        const thinkMatch = text.match(/^<think[^>]*>([\s\S]*?)<\/think>$/);
        if (thinkMatch) { thinking += thinkMatch[1]; continue; }
        const toolMatch = text.match(/^<tool>([\s\S]*?)<\/tool>$/);
        if (toolMatch) {
          try { tools.push(JSON.parse(toolMatch[1])); } catch { /* ignore */ }
        }
      }
      setTraceData({ thinking: thinking || undefined, tools, isStreaming: state.isRunning });
    };
    update();
    return runtime.thread.subscribe(update);
  }, [runtime, activeTraceMessageId]);

  // Extract topology/assessment from the active visualizer message's
  // <visualizer-state> tag. Kept separate from the trace extractor since it
  // runs on a different cadence (only when the message is active).
  const [visualizerData, setVisualizerData] = useState<{
    topology: TopologyData | null;
    assessment: CombinedAssessment | null;
  }>({ topology: null, assessment: null });

  useEffect(() => {
    if (!activeVisualizerMessageId) {
      setVisualizerData({ topology: null, assessment: null });
      return;
    }
    const update = () => {
      const state = runtime.thread.getState();
      const msg = state.messages.find((m) => m.id === activeVisualizerMessageId);
      if (!msg) return;
      let topology: TopologyData | null = null;
      let assessment: CombinedAssessment | null = null;
      for (const part of msg.content) {
        if (part.type !== "text" || !("text" in part)) continue;
        const text = (part as { text: string }).text;
        const m = text.match(/^<visualizer-state>([\s\S]*)<\/visualizer-state>$/);
        if (!m) continue;
        try {
          const parsed = JSON.parse(m[1]);
          if (parsed?.topology) topology = parsed.topology as TopologyData;
          if (parsed?.assessment) assessment = parsed.assessment as CombinedAssessment;
        } catch {
          /* ignore malformed */
        }
      }
      setVisualizerData({ topology, assessment });
    };
    update();
    return runtime.thread.subscribe(update);
  }, [runtime, activeVisualizerMessageId]);

  // Listen for artifact open events from ArtifactCard in chat
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      if (detail?.messageId) {
        setActiveArtifactMessageId(detail.messageId);
        setActiveArtifactReportId(detail.reportId || null);
        // Three-way mutual exclusion — only one right-panel mode at a time.
        setActiveTraceMessageId(null);
        setActiveVisualizerMessageId(null);
        // Open reports in split view by default; the user opts into full-width
        // via the panel's expand toggle. Reset any lingering collapse from a
        // prior full-width report/visualizer session.
        setChatCollapsed(false);
      }
    };
    window.addEventListener("open-artifact", handler);
    return () => window.removeEventListener("open-artifact", handler);
  }, []);

  // Listen for trace panel open requests
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      if (detail?.messageId) {
        setActiveTraceMessageId(detail.messageId);
        setActiveArtifactMessageId(null);
        setActiveVisualizerMessageId(null);
        // Trace panel has no full-width mode — make sure a prior report/viz
        // full-width session doesn't leave the chat collapsed behind it.
        setChatCollapsed(false);
      }
    };
    window.addEventListener("open-trace", handler);
    return () => window.removeEventListener("open-trace", handler);
  }, []);

  // Listen for visualizer panel open requests (from VisualizerCard in chat).
  // Also collapse the left sidebar — the visualizer needs horizontal room.
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      if (detail?.messageId) {
        setActiveVisualizerMessageId(detail.messageId);
        setActiveArtifactMessageId(null);
        setActiveTraceMessageId(null);
        setSidebarOpen(false);
      }
    };
    window.addEventListener("open-visualizer", handler);
    return () => window.removeEventListener("open-visualizer", handler);
  }, []);

  // Listen for template generation requests from the Composer template picker.
  // Always a fresh thread — the picker has no topology context in memory,
  // and starting clean avoids biasing the supervisor with unrelated prior
  // conversation. The prompt routes to the backend's template-generation
  // path (`template_id` populated in forwardedProps).
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      if (!detail?.template_id) return;
      // Set report mode and template ID
      if (typeof window !== "undefined") {
        window.__chatMode = "report";
        window.__reportTemplateId = detail.template_id;
        window.dispatchEvent(new Event("activate-report-mode"));
      }
      const varDesc = detail.varDesc || "";
      const prompt = varDesc
        ? `Generate the "${detail.name}" report for ${varDesc}`
        : `Generate the "${detail.name}" report`;
      pendingPromptRef.current = prompt;
      // Close any panels from the previous thread — their message IDs
      // wouldn't resolve in the new thread anyway.
      setActiveArtifactMessageId(null);
      setActiveTraceMessageId(null);
      setActiveVisualizerMessageId(null);
      setThreadId(uuidv4());
    };
    window.addEventListener("generate-from-template", handler);
    return () => window.removeEventListener("generate-from-template", handler);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runtime]);

  // Listen for adhoc-report requests from panels that want to turn what the
  // user is currently viewing into a chat-based report (e.g. the visualizer
  // toolbar's "Generate report" button). Contract:
  //   • Stays in the current thread — memory already carries the context the
  //     user is looking at, so the supervisor can reference it without
  //     re-fetching.
  //   • No `template_id`, no `edit_report_id` — backend routes this as a
  //     regular chat turn.
  //   • `__chatMode = "report"` flips MyRuntimeProvider into report mode so
  //     the response streams into ReportPanel instead of inline chat.
  //   • One turn, one artifact, one click — the composer send fires directly,
  //     no pendingPromptRef. Future panels (a dashboard report button, a
  //     pricing chart report, etc.) reuse this exact path.
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      const prompt: string = detail?.prompt || "";
      if (!prompt) return;
      if (typeof window !== "undefined") {
        window.__chatMode = "report";
        // Explicitly clear any lingering template id so the backend doesn't
        // route to the template-section machinery.
        window.__reportTemplateId = null;
        window.dispatchEvent(new Event("activate-report-mode"));
      }
      setActiveArtifactMessageId(null);
      setActiveTraceMessageId(null);
      setActiveVisualizerMessageId(null);
      // Tiny timeout so Thread's activate-report-mode listener flips its
      // badge before the composer send races ahead.
      setTimeout(() => {
        if (typeof window !== "undefined") {
          window.__chatMode = "report";
        }
        runtime.thread.composer.setText(prompt);
        runtime.thread.composer.send();
      }, 150);
    };
    window.addEventListener("generate-adhoc-report", handler);
    return () => window.removeEventListener("generate-adhoc-report", handler);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runtime]);

  const toggleSidebar = useCallback(() => {
    setSidebarOpen((prev) => {
      const next = !prev;
      localStorage.setItem("sidebar-collapsed", next ? "false" : "true");
      return next;
    });
  }, []);

  // Load messages when thread changes, or when a remote run completes on
  // the current thread (handles the "navigated away mid-run, came back,
  // waited, got stuck" flow — memory only has the assistant message AFTER
  // the run persists it, so we reload once the activity poll flips idle).
  useEffect(() => {
    if (typeof window !== "undefined") window.__currentThreadExternalId = threadId;

    // Cancel any in-progress stream before switching threads
    runtime.thread.cancelRun();

    let cancelled = false;
    // Skeleton only fires on the thread-change path (the tick-triggered
    // reload after a remote run completes is a same-thread refresh — the
    // body is already rendered, flashing a skeleton would be jarring).
    const isTickReload = prevLoadedThreadRef.current === threadId;
    prevLoadedThreadRef.current = threadId;
    if (!isTickReload) setMessagesLoading(true);
    (async () => {
      try {
        const { getSessionHistory } = await import("@/lib/runtime-client");
        const token = await getToken();
        const actorId = getActorId();

        const msgs = await getSessionHistory(threadId, actorId, () => Promise.resolve(token));
        if (cancelled) return;

        if (msgs.length === 0) {
          runtime.thread.reset();
          // If there's a pending prompt (e.g. from report template generate), send it
          const pending = pendingPromptRef.current;
          if (pending) {
            pendingPromptRef.current = null;
            // Activate report mode right before sending — must be set after reset
            // so the Composer's useEffect doesn't overwrite it
            if (typeof window !== "undefined" && window.__reportTemplateId) {
              window.__chatMode = "report";
              window.dispatchEvent(new Event("activate-report-mode"));
            }
            setTimeout(() => {
              // Re-set chatMode in case Composer's useEffect cleared it
              if (typeof window !== "undefined" && window.__reportTemplateId) {
                window.__chatMode = "report";
              }
              runtime.thread.composer.setText(pending);
              runtime.thread.composer.send();
            }, 150);
          }
          return;
        }

        const initial = msgs.map((m) => {
          let mainContent = m.content;
          let suggestionsPart: { type: "text"; text: string } | null = null;
          let artifactPart: { type: "text"; text: string } | null = null;
          // Synthesize a <visualizer-state> part from any saved <tool> tags
          // that contain DX topology/assessment data. The tag only ever exists
          // in the live stream's synthetic segment — memory stores the raw
          // tool output — so without reconstructing here, VisualizerCard
          // vanishes on history reload. Same pattern the artifact/report
          // branches use to rehydrate from memory.
          const vizState =
            m.role === "assistant"
              ? extractVisualizerStateFromMemory(mainContent)
              : null;
          const vizStatePart = vizState
            ? {
                type: "text" as const,
                text: `<visualizer-state>${JSON.stringify(vizState)}</visualizer-state>`,
              }
            : null;
          if (m.role === "assistant") {
            // Check if content already has <report-body> (template report from Memory)
            const hasReportBody = mainContent.includes("<report-body>");

            if (hasReportBody) {
              // Memory save format: <tool>...</tool>\n<report-body>...</report-body>\n<artifact>...</artifact>
              // Tool tags are OUTSIDE report-body. Convert them to <report-tool> for ReportPanel.
              const parts: { type: "text"; text: string }[] = [];

              // 1. Extract <tool> tags from OUTSIDE <report-body> (trace data)
              const outerToolRe = /<tool>([\s\S]*?)<\/tool>/g;
              let outerToolMatch;
              while ((outerToolMatch = outerToolRe.exec(mainContent)) !== null) {
                // Only include if it's outside <report-body>
                const pos = outerToolMatch.index;
                const rbStart = mainContent.indexOf("<report-body>");
                const rbEnd = mainContent.indexOf("</report-body>");
                if (rbStart === -1 || pos < rbStart || pos > rbEnd) {
                  parts.push({ type: "text" as const, text: `<report-tool>${outerToolMatch[1]}</report-tool>` });
                }
              }

              // 2. Extract <report-body> content
              const rbMatch = mainContent.match(/<report-body>([\s\S]*?)<\/report-body>/);
              const rbInner = rbMatch ? rbMatch[1] : "";

              // Split inner content into any embedded tool tags and text segments
              const innerToolRe = /<tool>([\s\S]*?)<\/tool>/g;
              let lastIdx = 0;
              let innerToolMatch;
              const innerSegments: { type: "text"; text: string }[] = [];
              while ((innerToolMatch = innerToolRe.exec(rbInner)) !== null) {
                const before = rbInner.slice(lastIdx, innerToolMatch.index).trim();
                if (before) innerSegments.push({ type: "text" as const, text: `<report-body>${before}</report-body>` });
                innerSegments.push({ type: "text" as const, text: `<report-tool>${innerToolMatch[1]}</report-tool>` });
                lastIdx = innerToolMatch.index + innerToolMatch[0].length;
              }
              const remaining = rbInner.slice(lastIdx).trim();
              if (remaining) innerSegments.push({ type: "text" as const, text: `<report-body>${remaining}</report-body>` });

              if (innerSegments.length > 0) {
                parts.push(...innerSegments);
              } else if (rbInner.trim()) {
                parts.push({ type: "text" as const, text: `<report-body>${rbInner}</report-body>` });
              }

              // 3. Extract <artifact>...</artifact>
              const artMatch2 = mainContent.match(/<artifact>[\s\S]*?<\/artifact>/);
              if (artMatch2) parts.push({ type: "text" as const, text: artMatch2[0] });

              // 4. Extract <suggestions>...</suggestions>
              const sugMatch2 = mainContent.match(/<suggestions>[\s\S]*?<\/suggestions>/);
              if (sugMatch2) parts.push({ type: "text" as const, text: sugMatch2[0] });

              if (parts.length > 0) {
                if (vizStatePart) parts.push(vizStatePart);
                return { role: m.role as "user" | "assistant", content: parts };
              }
            }

            // Non-report content: extract <artifact> and wrap in report-body
            const artMatch = mainContent.match(/\n?(<artifact>[\s\S]*?<\/artifact>)/);
            if (artMatch) {
              mainContent = mainContent.replace(artMatch[0], "");
              artifactPart = { type: "text" as const, text: artMatch[1] };
              // Extract <suggestions> before wrapping
              const sugMatch = mainContent.match(/\n?(<suggestions>[\s\S]*?<\/suggestions>)/);
              if (sugMatch) {
                mainContent = mainContent.replace(sugMatch[0], "");
                suggestionsPart = { type: "text" as const, text: sugMatch[1] };
              }
              // Wrap in report-body, splitting <tool> tags into separate <report-tool> parts
              // so ReportPanel can parse them individually
              const toolRe2 = /<tool>[\s\S]*?<\/tool>/g;
              let lastIdx2 = 0;
              let toolMatch2;
              const reportParts: { type: "text"; text: string }[] = [];
              while ((toolMatch2 = toolRe2.exec(mainContent)) !== null) {
                const before = mainContent.slice(lastIdx2, toolMatch2.index).trim();
                if (before) reportParts.push({ type: "text" as const, text: `<report-body>${before}</report-body>` });
                const converted = toolMatch2[0].replace("<tool>", "<report-tool>").replace("</tool>", "</report-tool>");
                reportParts.push({ type: "text" as const, text: converted });
                lastIdx2 = toolMatch2.index + toolMatch2[0].length;
              }
              const remaining2 = mainContent.slice(lastIdx2).trim();
              if (remaining2) reportParts.push({ type: "text" as const, text: `<report-body>${remaining2}</report-body>` });
              if (reportParts.length === 0) {
                reportParts.push({ type: "text" as const, text: `<report-body>${mainContent}</report-body>` });
              }
              reportParts.push(...(artifactPart ? [artifactPart] : []));
              reportParts.push(...(suggestionsPart ? [suggestionsPart] : []));
              if (vizStatePart) reportParts.push(vizStatePart);
              return { role: m.role as "user" | "assistant", content: reportParts };
            }
            // Extract <suggestions> for non-report messages
            const sugMatch = mainContent.match(/\n?(<suggestions>[\s\S]*?<\/suggestions>)/);
            if (sugMatch) {
              mainContent = mainContent.replace(sugMatch[0], "");
              suggestionsPart = { type: "text" as const, text: sugMatch[1] };
            }
          }

          // Render tool invocations as <tool> text tags (same as streaming) to avoid
          // assistant-ui MessageRepository parent-child issues with tool-call parts
          let toolParts: { type: "text"; text: string }[] = [];
          if (m.role === "assistant" && m.tool_invocations?.length) {
            toolParts = m.tool_invocations.map((t) => ({
              type: "text" as const,
              text: `<tool>${JSON.stringify({ name: t.tool_name, input: t.parameters, output: t.result || "" })}</tool>`,
            }));
          }

          // Also extract <tool> tags embedded in the content string (from enriched Memory save)
          // Split into ordered segments preserving interleaving of text and tool calls
          if (m.role === "assistant" && !toolParts.length) {
            const toolTagRegex = /<tool>[\s\S]*?<\/tool>/g;
            if (toolTagRegex.test(mainContent)) {
              toolTagRegex.lastIndex = 0; // reset after test
              const segments: { type: "text"; text: string }[] = [];
              let lastIdx = 0;
              let match;
              while ((match = toolTagRegex.exec(mainContent)) !== null) {
                // Text before this tool tag
                const before = mainContent.slice(lastIdx, match.index).trim();
                if (before) segments.push({ type: "text" as const, text: before });
                // The tool tag itself
                segments.push({ type: "text" as const, text: match[0] });
                lastIdx = match.index + match[0].length;
              }
              // Remaining text after last tool tag
              const after = mainContent.slice(lastIdx).trim();
              if (after) segments.push({ type: "text" as const, text: after });
              // Use segments as the ordered content — skip the toolParts/mainContent split
              const extraParts = [
                ...(artifactPart ? [artifactPart] : []),
                ...(suggestionsPart ? [suggestionsPart] : []),
                ...(vizStatePart ? [vizStatePart] : []),
              ];
              const contentParts = [...segments, ...extraParts];
              return { role: m.role as "user" | "assistant", content: contentParts };
            }
          }

          // User turn sent in report mode → carry a hidden marker part so the
          // UserMessage renderer can badge the bubble (survives reload). The
          // renderer filters this part out of the visible text.
          if (m.role === "user" && m.is_report) {
            return {
              role: "user" as const,
              content: [
                { type: "text" as const, text: mainContent },
                { type: "text" as const, text: "<report-request/>" },
              ],
            };
          }

          const extraParts = [
            ...(artifactPart ? [artifactPart] : []),
            ...(suggestionsPart ? [suggestionsPart] : []),
            ...(vizStatePart ? [vizStatePart] : []),
          ];

          const contentParts = toolParts.length || extraParts.length
            ? [
              ...toolParts,
              { type: "text" as const, text: mainContent },
              ...extraParts,
            ]
            : mainContent;

          return { role: m.role as "user" | "assistant", content: contentParts };
        }) satisfies ThreadMessageLike[];

        runtime.thread.reset(initial);

        // Auto-open artifact panel if the last message is a report (has <report-body> or <artifact>)
        const lastMsg = msgs[msgs.length - 1];
        if (lastMsg?.role === "assistant" && (lastMsg.content.includes("<report-body>") || lastMsg.content.includes("<artifact>"))) {
          // Get the message ID from the runtime after reset
          setTimeout(() => {
            const messages = runtime.thread.getState().messages;
            const lastAssistant = messages.filter(m => m.role === "assistant").pop();
            if (lastAssistant) {
              setActiveArtifactMessageId(lastAssistant.id);
            }
          }, 100);
        }
      } catch {
        if (!cancelled) runtime.thread.reset();
      } finally {
        if (!cancelled) setMessagesLoading(false);
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadId, remoteRunCompletionTick]);

  // Thread-scoped panels (artifact, trace, visualizer) reference messages by
  // ID. The IDs don't survive thread switches — so dropping them here prevents
  // a stale panel from lingering over an unrelated conversation.
  const closeAllPanels = useCallback(() => {
    setActiveArtifactMessageId(null);
    setActiveTraceMessageId(null);
    setActiveVisualizerMessageId(null);
  }, []);

  const handleNewThread = useCallback(() => {
    closeAllPanels();
    setThreadId(uuidv4());
  }, [closeAllPanels]);

  const handleSelectThread = useCallback((id: string) => {
    closeAllPanels();
    setThreadId(id);
  }, [closeAllPanels]);

  return (
    <div className="flex h-dvh" style={{ background: "var(--bg-primary)" }}>
      {/* Sidebar */}
      <div
        className="sidebar-container flex-shrink-0 relative"
        style={{ width: sidebarOpen ? 240 : 0 }}
      >
        <div
          className="sidebar-inner flex flex-col h-full"
          style={{ background: "var(--bg-secondary)", borderRight: "1px solid var(--border-subtle)", width: 240 }}
        >
          <div className="px-4 py-4 flex items-center gap-2">
            <div className="h-2 w-2 rounded-full shrink-0" style={{ background: "var(--accent)" }} />
            <span className="text-xs font-medium tracking-widest uppercase whitespace-nowrap" style={{ color: "var(--text-muted)" }}>
              CloudOps
            </span>
          </div>
          <ThreadSidebar
            currentThreadId={threadId}
            onSelectThread={handleSelectThread}
            onNewThread={handleNewThread}
          />
          <div className="px-4 py-3" style={{ borderTop: "1px solid var(--border-subtle)" }}>
            {/* User info */}
            <div className="flex items-center gap-2 px-3 py-2 mb-2 rounded-lg" style={{ background: "var(--bg-surface)", border: "1px solid var(--border-default)" }}>
              <User className="h-3.5 w-3.5 shrink-0" style={{ color: "var(--text-muted)" }} aria-hidden="true" />
              <span className="text-xs truncate" style={{ color: "var(--text-secondary)" }} title={getUserEmail() || "Not signed in"}>
                {getUserEmail() || "Not signed in"}
              </span>
            </div>
            <button
              onClick={() => setTemplateListOpen(true)}
              className="flex items-center gap-2 w-full rounded-lg px-3 py-2 mb-2 text-xs font-medium transition-colors hover:bg-[var(--bg-elevated)]"
              style={{ color: "var(--text-secondary)", background: "var(--bg-surface)", border: "1px solid var(--border-default)" }}
            >
              <NotebookText className="h-3.5 w-3.5" aria-hidden="true" />
              Report Templates
            </button>
            <TourMenuButton />
            <button
              onClick={toggleTheme}
              className="theme-toggle flex items-center w-full rounded-lg p-1 mb-2 cursor-pointer"
              style={{ background: "var(--bg-surface)", border: "1px solid var(--border-default)" }}
              aria-label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
            >
              <span
                className="flex items-center justify-center rounded-md py-1.5 flex-1 gap-1.5 text-xs font-medium transition-all duration-200"
                style={{
                  background: theme === "light" ? "var(--bg-elevated)" : "transparent",
                  color: theme === "light" ? "var(--text-primary)" : "var(--text-muted)",
                  boxShadow: theme === "light" ? "0 1px 3px var(--composer-shadow)" : "none",
                }}
              >
                <Sun className="h-3.5 w-3.5" aria-hidden="true" />
                Light
              </span>
              <span
                className="flex items-center justify-center rounded-md py-1.5 flex-1 gap-1.5 text-xs font-medium transition-all duration-200"
                style={{
                  background: theme === "dark" ? "var(--bg-elevated)" : "transparent",
                  color: theme === "dark" ? "var(--text-primary)" : "var(--text-muted)",
                  boxShadow: theme === "dark" ? "0 1px 3px var(--composer-shadow)" : "none",
                }}
              >
                <Moon className="h-3.5 w-3.5" aria-hidden="true" />
                Dark
              </span>
            </button>
            {/* Sign out button */}
            {!isDevBypass() && (
              <button
                onClick={() => signOut()}
                className="flex items-center gap-2 w-full rounded-lg px-3 py-2 text-xs font-medium transition-colors hover:bg-[var(--bg-elevated)]"
                style={{ color: "var(--text-muted)", background: "var(--bg-surface)", border: "1px solid var(--border-default)" }}
              >
                <LogOut className="h-3.5 w-3.5" aria-hidden="true" />
                Sign Out
              </button>
            )}
          </div>
        </div>
      </div>

      {/* Sidebar toggle — hidden while the visualizer is open, since the
           visualizer has its own chat-collapse affordance and three expanded
           columns on a single screen is a visually cramped read. */}
      {!activeVisualizerMessageId && (
        <button
          onClick={toggleSidebar}
          className="sidebar-toggle absolute z-10"
          style={{
            left: sidebarOpen ? 228 : 8,
            top: 16,
            color: "var(--text-muted)",
            padding: 6,
            borderRadius: 8,
            background: sidebarOpen ? "transparent" : "var(--bg-surface)",
            border: sidebarOpen ? "none" : "1px solid var(--border-subtle)",
          }}
          aria-label={sidebarOpen ? "Collapse sidebar" : "Expand sidebar"}
        >
          {sidebarOpen
            ? <PanelLeftClose className="h-4 w-4" />
            : <PanelLeftOpen className="h-4 w-4" />
          }
        </button>
      )}

      {/* Main chat. Collapses to width 0 when the user hits the chat-toggle
           button while the visualizer is open. Normal chat flow keeps its
           natural flex-1 width because `chatCollapsed` resets on viz close. */}
      <main
        className="flex flex-col min-w-0"
        style={{
          flex: chatCollapsed ? "0 0 0px" : "1 1 0%",
          width: chatCollapsed ? 0 : undefined,
          overflow: chatCollapsed ? "hidden" : undefined,
        }}
      >
        {threadBusyRemote && <ThreadBusyCard activity={threadActivity} />}
        <ThreadBusyProvider busy={threadBusyRemote}>
          {messagesLoading ? <ChatSkeleton /> : <Thread />}
        </ThreadBusyProvider>
      </main>

      {/* Chat-collapse toggle — only while the visualizer is open. Sits at
           the left edge of the visualizer so it reads as "pull the divider
           in/out". Matches the sidebar-toggle visual pattern. */}
      {activeVisualizerMessageId && (
        <button
          onClick={() => setChatCollapsed((v) => !v)}
          className="absolute z-10"
          style={{
            left: chatCollapsed ? 8 : undefined,
            right: chatCollapsed ? undefined : "calc(var(--visualizer-panel-width, 900px) - 12px)",
            top: 16,
            color: "var(--text-muted)",
            padding: 6,
            borderRadius: 8,
            background: "var(--bg-surface)",
            border: "1px solid var(--border-subtle)",
          }}
          aria-label={chatCollapsed ? "Show chat" : "Hide chat"}
          title={chatCollapsed ? "Show chat" : "Hide chat for full-screen topology"}
        >
          {chatCollapsed
            ? <PanelLeftOpen className="h-4 w-4" />
            : <PanelLeftClose className="h-4 w-4" />
          }
        </button>
      )}

      {/* Artifact panel */}
      {activeArtifactMessageId && (
        <ReportPanel
          messageId={activeArtifactMessageId}
          reportId={activeArtifactReportId || undefined}
          fullBleed={chatCollapsed}
          onToggleFullBleed={() => setChatCollapsed((v) => !v)}
          onClose={() => {
            setActiveArtifactMessageId(null);
            setActiveArtifactReportId(null);
            // Restore the chat when the report closes so we never leave the
            // user on a collapsed-chat layout with no panel.
            setChatCollapsed(false);
          }}
        />
      )}

      {/* Trace panel */}
      {activeTraceMessageId && (
        <TracePanel
          trace={traceData}
          onClose={() => setActiveTraceMessageId(null)}
        />
      )}

      {/* Visualizer panel */}
      {activeVisualizerMessageId && (
        <VisualizerPanel
          messageId={activeVisualizerMessageId}
          topology={visualizerData.topology}
          assessment={visualizerData.assessment}
          fullBleed={chatCollapsed}
          onClose={() => {
            setActiveVisualizerMessageId(null);
            // Restore the main chat when the visualizer closes so the user
            // doesn't land on a mostly-empty layout. Sidebar stays wherever
            // the user last left it (normal toggle still works here).
            setChatCollapsed(false);
          }}
        />
      )}

      {/* Report template editor modal */}
      {editorOpen && (
        <ReportTemplateEditor
          template={editingTemplate}
          onSave={async (data) => {
            const token = await getToken();
            const actorId = getActorId();
            const getTokenFn = async () => token;
            if (editingTemplate) {
              await updateTemplate(
                editingTemplate.user_id,
                editingTemplate.template_id,
                { name: data.name, description: data.description, prompt: "", sections: data.sections, dependencies: data.dependencies },
                getTokenFn,
              );
            } else {
              await createTemplate(
                actorId,
                { name: data.name, description: data.description, prompt: "", sections: data.sections, dependencies: data.dependencies },
                getTokenFn,
              );
            }
            setEditorOpen(false);
            setEditingTemplate(null);
            // Re-open list to show the new/updated template
            setTemplateListOpen(true);
          }}
          onClose={() => { setEditorOpen(false); setEditingTemplate(null); }}
        />
      )}

      {/* Report template list modal */}
      {templateListOpen && !editorOpen && (
        <ReportTemplateList
          onClose={() => setTemplateListOpen(false)}
          onEdit={(t) => { setEditingTemplate(t); setEditorOpen(true); }}
          onNew={() => { setEditingTemplate(null); setEditorOpen(true); }}
          onGenerate={(template, variables) => {
            // Close the list
            setTemplateListOpen(false);
            // Set report mode and template ID on window globals
            if (typeof window !== "undefined") {
              window.__chatMode = "report";
              window.__reportTemplateId = template.template_id;
              // Activate report mode in the Composer component
              window.dispatchEvent(new Event("activate-report-mode"));
            }
            // Build a prompt describing what to generate
            const varDesc = Object.entries(variables)
              .filter(([, v]) => v.trim())
              .map(([k, v]) => `${k}: ${v}`)
              .join(", ");
            const prompt = varDesc
              ? `Generate the "${template.name}" report for ${varDesc}`
              : `Generate the "${template.name}" report`;
            // Queue the prompt and start a new thread — the thread-change effect will send it after reset
            pendingPromptRef.current = prompt;
            setThreadId(uuidv4());
          }}
        />
      )}
    </div>
  );
}
