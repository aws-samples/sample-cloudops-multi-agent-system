/**
 * Tracks which user-message IDs were sent in report mode, so the UserMessage
 * bubble can show the "Report mode" badge IMMEDIATELY on send — not only after
 * a reload (reload is handled separately by the persisted <report-request/>
 * marker in message content).
 *
 * Why an external store: the user bubble is rendered by assistant-ui BEFORE the
 * model adapter runs, and we can't mutate an existing message's content parts.
 * The adapter (which knows report mode at send time) records the message id
 * here; UserMessage subscribes via useSyncExternalStore, so marking an id
 * triggers an immediate re-render of just that bubble.
 */
const reportModeIds = new Set<string>();
const listeners = new Set<() => void>();

export function markReportModeMessage(id: string | undefined | null): void {
  if (!id || reportModeIds.has(id)) return;
  reportModeIds.add(id);
  listeners.forEach((l) => l());
}

export function isReportModeMessage(id: string | undefined | null): boolean {
  return !!id && reportModeIds.has(id);
}

export function subscribeReportModeMessages(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
