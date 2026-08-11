"use client";

import { useEffect, useState } from "react";
import { ChevronRight, Loader2, NotebookText, AlertCircle, Pencil } from "lucide-react";
import { useMessage } from "@assistant-ui/react";
import { getReportStatus } from "@/lib/runtime-client";
import { getToken } from "@/lib/auth";
import { useEditingReport } from "@/lib/editing-report-context";

/**
 * Polled card rendered inline when the backend returns a
 * ``<report-pending report_id="..." title="...">`` marker.
 *
 * Reports are generated asynchronously by a background thread on the
 * supervisor runtime (see ``agents/shared/agui_server.py::_run_report_async``).
 * The runtime response returns immediately with this marker; actual
 * section work writes to DynamoDB as it progresses. This card polls
 * ``GET /reports/{id}/status`` every 3s until the report is
 * ``complete`` or ``error``, then swaps to a fully clickable artifact
 * card that opens the existing artifact panel.
 */
const POLL_INTERVAL_MS = 3000;
const TERMINAL = new Set(["complete", "error"]);

interface ReportStatus {
    status?: string;
    title?: string;
    current_section?: number;
    total_sections?: number;
    error?: string;
}

function parseMarkerAttrs(raw: string): Record<string, string> {
    // ``raw`` is the body between "<report-pending " and "/>" — a flat
    // sequence of key="value" pairs. No nesting or multiline expected
    // since the backend writes a single-line marker.
    const out: Record<string, string> = {};
    for (const m of raw.matchAll(/(\w+)="([^"]*)"/g)) {
        out[m[1]] = m[2];
    }
    return out;
}

export function ReportCard({ markerText }: { markerText: string }) {
    const attrs = parseMarkerAttrs(markerText);
    const reportId = attrs.report_id || "";
    const initialTitle = attrs.title || "Report";
    const versionNum = attrs.version ? parseInt(attrs.version, 10) || undefined : undefined;
    const message = useMessage();
    const { setEditing } = useEditingReport();

    const [status, setStatus] = useState<ReportStatus>({ status: "pending", title: initialTitle });

    useEffect(() => {
        if (!reportId) return;
        let cancelled = false;
        let timer: ReturnType<typeof setTimeout> | null = null;

        const tick = async () => {
            if (cancelled) return;
            try {
                const resp = await getReportStatus(reportId, "", getToken);
                if (cancelled) return;
                // Only re-set state when something actually CHANGED. This poll
                // ran every 3s and always set a fresh object, so a completed
                // report re-rendered this card indefinitely — and because the
                // card sits above later messages in the thread, that reflow
                // reset the scroll position while a follow-up answer was
                // streaming in.
                setStatus((prev) =>
                    prev.status === (resp as ReportStatus).status &&
                    prev.title === (resp as ReportStatus).title &&
                    prev.current_section === (resp as ReportStatus).current_section &&
                    prev.total_sections === (resp as ReportStatus).total_sections &&
                    prev.error === (resp as ReportStatus).error
                        ? prev
                        : (resp as ReportStatus),
                );
                if (!TERMINAL.has((resp as ReportStatus).status || "")) {
                    timer = setTimeout(tick, POLL_INTERVAL_MS);
                }
            } catch {
                // Swallow — the row may not exist yet on the first few polls
                // because the background thread writes the pending shell
                // before beginning work, but either way we don't want to
                // spam the console. Try again until terminal.
                if (!cancelled) timer = setTimeout(tick, POLL_INTERVAL_MS);
            }
        };
        void tick();

        return () => {
            cancelled = true;
            if (timer) clearTimeout(timer);
        };
    }, [reportId]);

    const currentStatus = status.status || "pending";
    const isTerminal = TERMINAL.has(currentStatus);
    const isError = currentStatus === "error";
    const title = status.title || initialTitle;

    const sectionHint = (() => {
        const cur = status.current_section;
        const tot = status.total_sections;
        if (typeof cur === "number" && typeof tot === "number" && tot > 0) {
            return `${cur} of ${tot} sections`;
        }
        return "";
    })();

    const handleOpen = () => {
        if (!isTerminal || isError) return;
        window.dispatchEvent(
            new CustomEvent("open-artifact", { detail: { messageId: message.id, reportId } })
        );
    };

    const handleEdit = (e: React.MouseEvent) => {
        e.stopPropagation();
        if (!isTerminal || isError || !reportId) return;
        setEditing({ report_id: reportId, title, version: versionNum });
    };

    return (
        <button
            onClick={handleOpen}
            disabled={!isTerminal || isError}
            className="artifact-card flex items-center gap-3.5 w-full rounded-xl p-4 my-3 text-left group active:scale-[0.98] transition-transform duration-150 disabled:cursor-default disabled:active:scale-100"
            style={{
                background: "var(--bg-surface)",
                border: "1px solid var(--border-default)",
            }}
        >
            <div
                className="h-10 w-10 rounded-xl flex items-center justify-center shrink-0"
                style={{
                    background: isError
                        ? "var(--bg-elevated)"
                        : "var(--accent-surface)",
                    border: isError
                        ? "1px solid var(--border-default)"
                        : "1px solid var(--accent-border-subtle)",
                }}
            >
                {isError ? (
                    <AlertCircle className="h-5 w-5" style={{ color: "var(--text-secondary)" }} />
                ) : isTerminal ? (
                    <NotebookText className="h-5 w-5" style={{ color: "var(--accent-ai)" }} />
                ) : (
                    <Loader2
                        className="h-5 w-5 animate-spin"
                        style={{ color: "var(--accent-ai)" }}
                    />
                )}
            </div>
            <div className="flex-1 min-w-0">
                <div className="text-sm font-medium truncate flex items-center gap-1.5" style={{ color: "var(--text-primary)" }}>
                    <span className="truncate">{title}</span>
                    {versionNum && versionNum > 1 && (
                        <span
                            className="text-[10px] px-1.5 py-0.5 rounded-full shrink-0"
                            style={{ background: "var(--bg-elevated)", color: "var(--text-muted)", border: "1px solid var(--border-subtle)" }}
                        >
                            v{versionNum}
                        </span>
                    )}
                </div>
                <div
                    className="text-xs mt-0.5 flex items-center gap-1.5"
                    style={{ color: "var(--text-muted)" }}
                >
                    {isError ? (
                        <span>Report generation failed{status.error ? ` — ${status.error}` : "."}</span>
                    ) : isTerminal ? (
                        <span>View full report</span>
                    ) : (
                        <>
                            <span
                                className="artifact-pulse inline-block h-1.5 w-1.5 rounded-full"
                                style={{ background: "var(--accent-ai)" }}
                            />
                            {currentStatus === "pending"
                                ? "Queued…"
                                : sectionHint
                                ? `Generating — ${sectionHint}`
                                : "Generating…"}
                        </>
                    )}
                </div>
            </div>
            {isTerminal && !isError && (
                <div className="flex items-center gap-1 shrink-0">
                    <span
                        onClick={handleEdit}
                        className="flex items-center gap-1 px-2 py-1 rounded-md text-[11px] transition-colors hover:bg-[var(--accent-hover)]"
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
                    <ChevronRight
                        className="h-4 w-4 opacity-30 group-hover:opacity-70 transition-opacity"
                        style={{ color: "var(--text-muted)" }}
                    />
                </div>
            )}
        </button>
    );
}
