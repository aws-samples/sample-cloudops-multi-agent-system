"use client";

import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import type { ThreadActivity } from "@/lib/runtime-client";

interface ThreadBusyCardProps {
    activity: ThreadActivity;
}

/**
 * Banner shown in place of (or above) the thread when the server-side
 * invocation for this thread is still in-flight but this tab is no
 * longer the one streaming it. Explains to the user that the backend
 * hasn't forgotten them and the composer is disabled on purpose.
 */
export function ThreadBusyCard({ activity }: ThreadBusyCardProps) {
    const [elapsed, setElapsed] = useState("");

    useEffect(() => {
        if (!activity.started_at) {
            setElapsed("");
            return;
        }
        const start = Date.parse(activity.started_at);
        if (Number.isNaN(start)) return;
        const tick = () => setElapsed(formatElapsed(Date.now() - start));
        tick();
        const id = setInterval(tick, 1000);
        return () => clearInterval(id);
    }, [activity.started_at]);

    const step = activity.current_step || "Working…";

    return (
        <div
            // shrink-0: as a flex child of <main> (now min-h-0/overflow-hidden)
            // the card must keep its natural height instead of being squeezed,
            // while the Thread below flexes into whatever remains.
            className="max-w-2xl mx-auto w-full px-4 pt-8 shrink-0"
            role="status"
            aria-live="polite"
        >
            <div
                className="flex items-start gap-3 rounded-lg p-4"
                style={{
                    background: "var(--bg-elevated)",
                    border: "1px solid var(--border-subtle)",
                }}
            >
                <Loader2
                    className="h-5 w-5 shrink-0 animate-spin"
                    style={{ color: "var(--accent)" }}
                    aria-hidden="true"
                />
                <div className="flex-1 min-w-0">
                    <div
                        className="text-sm font-medium"
                        style={{ color: "var(--text-primary)" }}
                    >
                        This thread is still running{elapsed ? ` · ${elapsed}` : ""}
                    </div>
                    <div
                        className="text-xs mt-1"
                        style={{ color: "var(--text-secondary)" }}
                    >
                        {step}
                    </div>
                    <div
                        className="text-[11px] mt-2"
                        style={{ color: "var(--text-muted)" }}
                    >
                        New messages are disabled to keep the current run from
                        being interrupted. The response will appear here when it
                        finishes.
                    </div>
                </div>
            </div>
        </div>
    );
}

function formatElapsed(ms: number): string {
    if (ms < 0) return "";
    const secs = Math.floor(ms / 1000);
    if (secs < 60) return `${secs}s`;
    const mins = Math.floor(secs / 60);
    const rem = secs % 60;
    return `${mins}m ${rem}s`;
}
