/**
 * Hook for per-thread activity polling.
 *
 * Surfaces the server-side "is the backend still working on this thread"
 * state so the UI can show a "still running" card + disable the composer
 * when you navigate back to a thread whose invocation began in another
 * tab. Polls every 2s while ``status === "running"``; stops once the
 * thread goes idle or errors.
 */

import { useEffect, useRef, useState } from "react";
import { getThreadActivity, type ThreadActivity } from "./runtime-client";
import { getToken } from "./auth";

const POLL_INTERVAL_MS = 2000;
const IDLE: ThreadActivity = { status: "idle" };

export function useThreadActivity(threadId: string | null): ThreadActivity {
    const [activity, setActivity] = useState<ThreadActivity>(IDLE);
    // Identifies the thread each poll belongs to. A single shared boolean ref
    // was not enough: the new effect reset it to `false` while the PREVIOUS
    // thread's in-flight fetch was still pending, so that stale response then
    // wrote its own thread's state — two threads' pollers fighting over one
    // state, which showed up as a busy card whose elapsed time and
    // "Running N sections" flickered between conversations.
    const activeThreadRef = useRef<string | null>(null);

    useEffect(() => {
        activeThreadRef.current = threadId;
        // Always clear on thread change. Without this the previous thread's
        // `running` state stayed rendered until the new thread's first fetch
        // resolved — which is why opening a New Chat while a report was
        // generating elsewhere showed "This thread is still running" (with a
        // disabled composer) on an empty conversation.
        setActivity(IDLE);
        if (!threadId) return;

        let timer: ReturnType<typeof setTimeout> | null = null;
        const isStale = () => activeThreadRef.current !== threadId;

        const tick = async () => {
            if (isStale()) return;
            try {
                const next = await getThreadActivity(threadId, getToken);
                if (isStale()) return;
                setActivity(next);
                if (next.status === "running") {
                    timer = setTimeout(tick, POLL_INTERVAL_MS);
                }
            } catch {
                if (isStale()) return;
                // On fetch error, stop polling but don't clobber existing state.
                setActivity((prev) => (prev.status === "running" ? { status: "idle" } : prev));
            }
        };

        void tick();

        return () => {
            if (timer) clearTimeout(timer);
        };
    }, [threadId]);

    return activity;
}
