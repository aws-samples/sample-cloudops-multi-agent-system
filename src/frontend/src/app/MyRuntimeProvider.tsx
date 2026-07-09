"use client";

import type { ChatModelAdapter } from "@assistant-ui/react";
import {
  useLocalRuntime,
  AssistantRuntimeProvider,
} from "@assistant-ui/react";
import type { ReactNode } from "react";
import { useEffect, useState } from "react";
import { v4 as uuidv4 } from "uuid";
import { isAuthenticated, isDevBypass, login, handleCallback, getToken, getActorId } from "@/lib/auth";
import { extractVisualizerState, extractVisualizerStateFromMemory } from "@/lib/visualizer-state";
import { EditingReportProvider } from "@/lib/editing-report-context";
import { markReportModeMessage } from "@/lib/report-mode-messages";

const MyModelAdapter: ChatModelAdapter = {
  async *run({ messages, abortSignal }) {
    const lastUserMessage = [...messages]
      .reverse()
      .find((m) => m.role === "user");
    const prompt =
      lastUserMessage?.content
        .filter((c): c is { type: "text"; text: string } => c.type === "text")
        .map((c) => c.text)
        .join(" ") ?? "";

    const sessionId =
      (typeof window !== "undefined" && window.__currentThreadExternalId) ||
      uuidv4();

    // Read mode — do NOT clear here. The Composer owns __chatMode lifecycle
    // (cleared on explicit user toggle-off or chat-stream-done). Clearing in
    // the adapter on read causes the flag to be lost on assistant-ui retries
    // or on multi-pass runs, which dropped freeform-report-mode state mid-turn.
    const mode = (typeof window !== "undefined" && window.__chatMode) || null;
    const isReportMode = mode === "report";

    // Read and clear template ID for report generation. Templates are
    // one-shot — the user picked this template for THIS submission only —
    // so consuming-on-read is correct here.
    const reportTemplateId = (typeof window !== "undefined" && window.__reportTemplateId) || null;
    if (typeof window !== "undefined") window.__reportTemplateId = null;

    // Fire the end-of-turn signal exactly once, no matter how this generator
    // exits (normal finish, HTTP error, stream read failure, backend error
    // event, or abort). Listeners use it to reset one-shot composer state —
    // report mode and edit target. If ANY exit path skips it, that state stays
    // stuck and the NEXT normal turn wrongly renders as a report. Idempotent so
    // it's safe to call from a finally AND leave existing explicit calls in.
    let streamDoneFired = false;
    const streamDone = () => {
      if (streamDoneFired) return;
      streamDoneFired = true;
      if (typeof window !== "undefined") window.dispatchEvent(new Event("chat-stream-done"));
    };

    // Show thinking indicator immediately. The "Generating report…" artifact
    // card is no longer pre-emitted — we wait for the backend's first signal
    // (<report-pending> for templated, <report-body>/artifact_meta for
    // freeform) so we don't render a placeholder that competes with the real
    // ReportCard or never terminates.
    yield { content: [{ type: "text" as const, text: "<think streaming>\n</think>" }] };

    const RUNTIME_ARN = process.env.NEXT_PUBLIC_RUNTIME_ARN || "";
    const AWS_REGION = process.env.NEXT_PUBLIC_AWS_REGION || "ap-southeast-1";
    const encodedArn = encodeURIComponent(RUNTIME_ARN);
    const runtimeUrl = `https://bedrock-agentcore.${AWS_REGION}.amazonaws.com/runtimes/${encodedArn}/invocations`;

    // Get auth token
    const token = await getToken();
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (token) {
      headers["Authorization"] = `Bearer ${token}`;
    }

    const actorId = getActorId();
    const threadId = sessionId;
    const runId = uuidv4();

    // Build AG-UI format request
    const aguiPayload = {
      threadId,
      runId,
      messages: [{ id: uuidv4(), role: "user", content: prompt }],
      state: {},
      tools: [],
      context: [],
      forwardedProps: {
        session_id: sessionId,
        actor_id: actorId,
        template_id: reportTemplateId || undefined,
        // Freeform report mode (composer toggle, no template). Backend
        // builds a synthetic single-section template from the user prompt
        // and routes through the same async path as templated reports —
        // so persistence (DDB row + <report-pending> marker), reload,
        // edit lineage, and get_report follow-ups all work uniformly.
        chat_mode: isReportMode && !reportTemplateId ? "report" : undefined,
        edit_report_id:
          (typeof window !== "undefined" && window.__editingReportId) || undefined,
      },
    };

    // If this turn is a report request (templated, freeform, OR an edit),
    // badge the user's bubble IMMEDIATELY — the persisted <report-request/>
    // marker only comes back on reload. Mark by the live message id so
    // UserMessage re-renders the "Report mode" badge right away.
    const isReportTurn =
      isReportMode ||
      !!reportTemplateId ||
      (typeof window !== "undefined" && !!window.__editingReportId);
    if (isReportTurn) markReportModeMessage(lastUserMessage?.id);

    const res = await fetch(runtimeUrl, {
      method: "POST",
      headers,
      body: JSON.stringify(aguiPayload),
      signal: abortSignal,
    });

    if (!res.ok) {
      const errText = await res.text();
      streamDone();
      yield { content: [{ type: "text" as const, text: `Error: ${errText}` }] };
      return;
    }

    // Signal sidebar to optimistically show this thread immediately
    if (typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent("chat-stream-start", {
        detail: { sessionId, prompt },
      }));
    }

    try {

    const reader = res.body!.getReader();
    const decoder = new TextDecoder();
    const segments: Segment[] = [];
    let buffer = "";
    let lastYieldTime = 0;
    const MIN_YIELD_INTERVAL = 50;

    // Track tool calls by ID for matching start/args/end/result
    const toolCalls: Record<string, { name: string; args: string; output?: string }> = {};

    const lastSegment = (kind: Segment["kind"]) =>
      segments.length > 0 && segments[segments.length - 1].kind === kind
        ? segments[segments.length - 1]
        : null;
    const pushOrAppend = (kind: Segment["kind"], value: string) => {
      const seg = lastSegment(kind);
      if (seg) seg.value += value;
      else segments.push({ kind, value, startMs: Date.now() });
    };

    while (true) {
      let done: boolean;
      let value: Uint8Array | undefined;
      try {
        ({ done, value } = await reader.read());
      } catch {
        return;
      }
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";

      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        try {
          const parsed = JSON.parse(line.slice(6));
          const eventType: string = parsed.type || parsed.event || "";

          // --- AG-UI events ---

          // Reasoning (thinking)
          if (eventType === "REASONING_MESSAGE_CONTENT" || eventType === "REASONING_CONTENT") {
            const delta = parsed.delta || "";
            if (isReportMode) {
              const existing = segments.find((s) => s.kind === "reasoning");
              if (existing) existing.value += delta;
              else segments.push({ kind: "reasoning", value: delta, startMs: Date.now() });
            } else {
              pushOrAppend("reasoning", delta);
            }
            const now = Date.now();
            if (now - lastYieldTime >= MIN_YIELD_INTERVAL) {
              lastYieldTime = now;
              yield { content: buildContent(segments, true, isReportMode) };
            }
          }
          // Text message content (streaming tokens)
          else if (eventType === "TEXT_MESSAGE_CONTENT") {
            const delta = parsed.delta || "";
            // Check if this is a suggestions tag injected before RUN_FINISHED
            if (delta.includes("<suggestions>")) {
              const sugMatch = delta.match(/<suggestions>([\s\S]*?)<\/suggestions>/);
              if (sugMatch) {
                segments.push({ kind: "suggestions", value: sugMatch[1], startMs: Date.now() });
                // Extract any text before/after the tag
                const before = delta.slice(0, delta.indexOf("<suggestions>")).trim();
                if (before) pushOrAppend(isReportMode ? "report_body" : "text", before);
              }
            } else if (delta.includes("<report-pending")) {
              // Async report kickoff marker emitted by the supervisor before
              // the background worker starts producing sections. Strip the
              // tag out of the transcript and push a dedicated segment so
              // Thread.tsx can render a polling ReportCard.
              const match = delta.match(/<report-pending\s+([^/>]*)\/>/);
              if (match) {
                segments.push({ kind: "report_pending", value: match[1].trim(), startMs: Date.now() });
              }
            } else {
              pushOrAppend(isReportMode ? "report_body" : "text", delta);
            }
            if (!isReportMode) {
              const now = Date.now();
              if (now - lastYieldTime >= MIN_YIELD_INTERVAL) {
                lastYieldTime = now;
                yield { content: buildContent(segments, true, isReportMode) };
              }
            }
          }
          // Tool call start
          else if (eventType === "TOOL_CALL_START") {
            const tcId = parsed.toolCallId || "";
            const tcName = parsed.toolCallName || "";
            toolCalls[tcId] = { name: tcName, args: "" };
            segments.push({ kind: "tool", value: JSON.stringify({ name: tcName, input: {} }), startMs: Date.now() });
            yield { content: buildContent(segments, true, isReportMode) };
          }
          // Tool call args (streaming)
          else if (eventType === "TOOL_CALL_ARGS") {
            const tcId = parsed.toolCallId || "";
            if (toolCalls[tcId]) {
              toolCalls[tcId].args += parsed.delta || "";
            }
          }
          // Tool call end
          else if (eventType === "TOOL_CALL_END") {
            const tcId = parsed.toolCallId || "";
            const tc = toolCalls[tcId];
            if (tc) {
              // Update the tool segment with parsed args
              let parsedInput = {};
              try { parsedInput = JSON.parse(tc.args); } catch { /* keep empty */ }
              const toolIdx = segments.findIndex((s) => {
                if (s.kind !== "tool") return false;
                try { const v = JSON.parse(s.value); return v.name === tc.name && !v.output; } catch { return false; }
              });
              if (toolIdx >= 0) {
                segments[toolIdx].value = JSON.stringify({ name: tc.name, input: parsedInput });
              }
            }
            yield { content: buildContent(segments, true, isReportMode) };
          }
          // Tool call result
          else if (eventType === "TOOL_CALL_RESULT") {
            const tcId = parsed.toolCallId || "";
            const tc = toolCalls[tcId];
            const resultContent = parsed.content || "";
            if (tc) {
              let parsedInput = {};
              try { parsedInput = JSON.parse(tc.args); } catch { /* keep empty */ }
              const toolIdx = segments.findIndex((s) => {
                if (s.kind !== "tool") return false;
                try { const v = JSON.parse(s.value); return v.name === tc.name && !v.output; } catch { return false; }
              });
              if (toolIdx >= 0) {
                segments[toolIdx].value = JSON.stringify({ name: tc.name, input: parsedInput, output: resultContent });
              }
              // Phase 5: if this is a network-resiliency tool, also emit a
              // visualizer_state segment so Thread renders a VisualizerCard.
              // The card opens the VisualizerPanel on click. Dedup: only
              // push a new segment when the synthesized state carries new
              // topology/assessment data — otherwise a later tool result
              // from the same stream would append a duplicate card.
              const vizState = extractVisualizerState(tc.name, resultContent);
              if (vizState) {
                const alreadyHave = segments.some((s) => {
                  if (s.kind !== "visualizer_state") return false;
                  try {
                    const existing = JSON.parse(s.value) as { topology?: unknown; assessment?: unknown };
                    const newBrings =
                      (vizState.topology && !existing.topology) ||
                      (vizState.assessment && !existing.assessment);
                    return !newBrings;
                  } catch {
                    return false;
                  }
                });
                if (!alreadyHave) {
                  segments.push({ kind: "visualizer_state", value: JSON.stringify(vizState), startMs: Date.now() });
                }
              }
            }
            yield { content: buildContent(segments, true, isReportMode) };
          }
          // Run error
          else if (eventType === "RUN_ERROR") {
            const errMsg = parsed.message || parsed.code || "Unknown error";
            yield { content: [{ type: "text" as const, text: `Error: ${errMsg}` }] };
            return;
          }
          // Run finished
          else if (eventType === "RUN_FINISHED") {
            // In report mode, create artifact metadata from accumulated text
            if (isReportMode) {
              const reportBody = segments.find(s => s.kind === "report_body");
              if (reportBody) {
                const titleMatch = reportBody.value.match(/^#\s+(.+)$/m);
                const title = titleMatch ? titleMatch[1].trim() : "CloudOps Report";
                segments.push({ kind: "artifact_meta", value: JSON.stringify({ title }), startMs: Date.now() });
              }
            }
            // Safety net: if the per-TOOL_CALL_RESULT synthesis missed the
            // payload (e.g. the supervisor wrapped the sub-agent response in
            // an envelope shape the walker didn't recognize), scan the saved
            // <tool> segments accumulated during the stream and try the same
            // memory-path extractor as the history loader. Guarantees live
            // and history paths yield the same VisualizerCard for the same
            // tool output.
            const hasVizSegment = segments.some((s) => s.kind === "visualizer_state");
            if (!hasVizSegment) {
              const toolSegmentsText = segments
                .filter((s) => s.kind === "tool")
                .map((s) => `<tool>${s.value}</tool>`)
                .join("\n");
              if (toolSegmentsText) {
                const fallbackState = extractVisualizerStateFromMemory(toolSegmentsText);
                if (fallbackState) {
                  segments.push({ kind: "visualizer_state", value: JSON.stringify(fallbackState), startMs: Date.now() });
                }
              }
            }
            yield { content: buildContent(segments, false, isReportMode) };
            streamDone();
            return;
          }

          // --- Legacy event fallbacks (for backward compat during transition) ---
          else if (eventType === "reasoning") {
            pushOrAppend("reasoning", parsed.data?.text || "");
            const now = Date.now();
            if (now - lastYieldTime >= MIN_YIELD_INTERVAL) {
              lastYieldTime = now;
              yield { content: buildContent(segments, true, isReportMode) };
            }
          }
          else if (eventType === "token") {
            pushOrAppend(isReportMode ? "report_body" : "text", parsed.data?.token || "");
            if (!isReportMode) {
              const now = Date.now();
              if (now - lastYieldTime >= MIN_YIELD_INTERVAL) {
                lastYieldTime = now;
                yield { content: buildContent(segments, true, isReportMode) };
              }
            }
          }
          else if (eventType === "tool_start") {
            const data = parsed.data || parsed;
            segments.push({ kind: "tool", value: JSON.stringify({ name: data.name, input: data.input ?? {} }), startMs: Date.now() });
            yield { content: buildContent(segments, true, isReportMode) };
          }
          else if (eventType === "tool_result") {
            const data = parsed.data || parsed;
            const toolIdx = segments.findIndex((s) => {
              if (s.kind !== "tool") return false;
              try { const v = JSON.parse(s.value); return v.name === data.name && !v.output; } catch { return false; }
            });
            if (toolIdx >= 0) {
              segments[toolIdx].value = JSON.stringify({ name: data.name, input: data.input ?? {}, output: data.output ?? "" });
            }
            yield { content: buildContent(segments, true, isReportMode) };
          }
          else if (eventType === "complete" || eventType === "suggestions") {
            const data = parsed.data || parsed;
            if (data.suggestions?.length) {
              segments.push({ kind: "suggestions", value: JSON.stringify(data.suggestions), startMs: Date.now() });
            }
            if (eventType === "complete" && data.response && !segments.some(s => s.kind === "text" || s.kind === "report_body")) {
              pushOrAppend(isReportMode ? "report_body" : "text", data.response);
            }
            if (eventType === "complete" && isReportMode && data.response) {
              const titleMatch = data.response.match(/^#\s+(.+)$/m);
              const title = titleMatch ? titleMatch[1].trim() : "CloudOps Report";
              segments.push({ kind: "artifact_meta", value: JSON.stringify({ title }), startMs: Date.now() });
            }
            yield { content: buildContent(segments, false, isReportMode) };
          }
          else if (eventType === "error") {
            yield { content: [{ type: "text" as const, text: `Error: ${parsed.data?.error || "Unknown error"}` }] };
            return;
          }
          // Non-streaming fallback (no event type)
          else if (!eventType) {
            const text = parsed.response || JSON.stringify(parsed);
            segments.push({ kind: "text", value: text, startMs: Date.now() });
            yield { content: buildContent(segments, false, isReportMode) };
            return;
          }
        } catch {
          // Skip malformed lines
        }
      }
    }

    // Flush remaining
    yield { content: buildContent(segments, true, isReportMode) };
    yield { content: buildContent(segments, false, isReportMode) };

    } finally {
      // Runs on every exit from the streaming block: normal finish, the
      // RUN_FINISHED/error `return`s, a `reader.read()` throw, or generator
      // abort (assistant-ui calls .return() on the iterator when the user
      // stops or navigates). Guarantees one-shot composer state is reset.
      streamDone();
    }
  },
};

type Segment = { kind: "reasoning" | "tool" | "text" | "suggestions" | "artifact_meta" | "report_body" | "visualizer_state" | "report_pending"; value: string; startMs: number };

function buildContent(
  segments: Segment[],
  streaming: boolean,
  isReportMode = false,
): Array<{ type: "text"; text: string }> {
  const parts: Array<{ type: "text"; text: string }> = [];

  if (segments.length === 0 && streaming) {
    parts.push({ type: "text", text: "<think streaming>\n</think>" });
    return parts;
  }

  const reasoningDone = !streaming;

  for (const seg of segments) {
    if (seg.kind === "reasoning") {
      const elapsed = Math.round((Date.now() - seg.startMs) / 1000);
      const tag = !reasoningDone ? "<think streaming>" : `<think seconds=${elapsed}>`;
      parts.push({ type: "text", text: `${tag}\n${seg.value}\n</think>` });
    } else if (seg.kind === "tool") {
      // Add streaming flag to tools without output
      let toolValue = seg.value;
      if (streaming) {
        try {
          const td = JSON.parse(toolValue);
          if (!td.output) { td.isStreaming = true; toolValue = JSON.stringify(td); }
        } catch { /* keep original */ }
      }
      parts.push({ type: "text", text: isReportMode ? `<report-tool>${toolValue}</report-tool>` : `<tool>${toolValue}</tool>` });
    } else if (seg.kind === "suggestions") {
      parts.push({ type: "text", text: `<suggestions>${seg.value}</suggestions>` });
    } else if (seg.kind === "artifact_meta") {
      parts.push({ type: "text", text: `<artifact>${seg.value}</artifact>` });
    } else if (seg.kind === "report_body") {
      parts.push({ type: "text", text: `<report-body>${seg.value}</report-body>` });
    } else if (seg.kind === "text") {
      parts.push({ type: "text", text: seg.value });
    } else if (seg.kind === "visualizer_state") {
      // Surfaced by TOOL_CALL_RESULT handler when the network-resiliency tools
      // return. Thread.tsx renders this as a VisualizerCard that opens the
      // visualizer panel on click.
      parts.push({ type: "text", text: `<visualizer-state>${seg.value}</visualizer-state>` });
    } else if (seg.kind === "report_pending") {
      // Kickoff marker for an async-running report. Thread.tsx renders
      // this as a polling ReportCard that swaps to the full artifact
      // once the backend flips the row's status to "complete".
      parts.push({ type: "text", text: `<report-pending ${seg.value}/>` });
    }
  }

  // Show the "Generating report…" placeholder ONLY while the freeform-report
  // path is still streaming and hasn't produced anything terminal yet:
  //   - artifact_meta: body finished; the real card is now in segments
  //   - report_pending: templated path; ReportCard owns the UI from here
  //   - report_body: freeform body actively streaming; user sees progress
  // Suppressing while streaming prevents bug 1 (stuck card) and bug 4
  // (placeholder + real card co-existing).
  const hasTerminalReportSeg = segments.some(
    (s) =>
      s.kind === "artifact_meta" ||
      s.kind === "report_pending" ||
      s.kind === "report_body"
  );
  if (isReportMode && streaming && !hasTerminalReportSeg) {
    parts.push({ type: "text", text: `<artifact>${JSON.stringify({ title: "Generating report…", generating: true })}</artifact>` });
  }

  return parts.length ? parts : [{ type: "text", text: "" }];
}

// ----- Phase 5: visualizer-state extraction ---------------------------------
//
// Live-stream extractor and the constants/helpers moved to
// `@/lib/visualizer-state` so the history loader in `page.tsx` can reuse the
// same logic. Without a shared implementation, a page reload would lose the
// VisualizerCard even though the saved `<tool>` output still has the data.

declare global {
  interface Window {
    __currentThreadExternalId?: string;
    __chatMode?: "report" | null;
    __reportTemplateId?: string | null;
    __editingReportId?: string | null;
  }
}

export function MyRuntimeProvider({ children }: { children: ReactNode }) {
  const runtime = useLocalRuntime(MyModelAdapter);
  const [authReady, setAuthReady] = useState(isDevBypass());

  useEffect(() => {
    if (isDevBypass()) {
      setAuthReady(true);
      return;
    }

    const params = new URLSearchParams(window.location.search);
    const code = params.get("code");
    if (code) {
      handleCallback(code).then(() => {
        window.history.replaceState({}, "", "/");
        setAuthReady(true);
      }).catch((err) => {
        console.error("Auth callback failed:", err);
        login();
      });
      return;
    }

    if (window.location.pathname.startsWith("/callback")) {
      window.history.replaceState({}, "", "/");
    }

    if (!isAuthenticated()) {
      login();
      return;
    }

    setAuthReady(true);
  }, []);

  if (!authReady) {
    return (
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100vh", color: "var(--text-muted, #888)" }}>
        Authenticating...
      </div>
    );
  }

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <EditingReportProvider>{children}</EditingReportProvider>
    </AssistantRuntimeProvider>
  );
}
