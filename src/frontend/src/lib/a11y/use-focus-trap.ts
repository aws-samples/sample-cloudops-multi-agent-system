"use client";

/**
 * Generic focus-trap hook for modal/slideout panels.
 *
 * Promoted from the network-resilience visualizer's original
 * `useFocusTrap` — same behavior, moved to `@/lib/a11y/` so every panel
 * (TracePanel, ReportPanel, VisualizerPanel, ReportTemplateEditor…) can
 * share one implementation and keep keyboard semantics consistent across
 * the app.
 *
 * Usage:
 *   const panelRef = useFocusTrap(open, onClose);
 *   <aside ref={panelRef}> ... </aside>
 *
 * Behavior:
 *   - First focusable descendant receives focus when `active` becomes true.
 *   - Tab cycles within the container; Shift+Tab wraps backwards.
 *   - Escape invokes `onEscape` (panels typically wire this to onClose).
 *   - When `active` flips back to false, focus returns to the element that
 *     had focus before the trap engaged.
 */

import { useEffect, useRef } from "react";

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function useFocusTrap<T extends HTMLElement = HTMLDivElement>(
  active: boolean,
  onEscape?: () => void,
) {
  const ref = useRef<T>(null);
  // Keep the latest onEscape in a ref so the trap effect does NOT depend on its
  // identity. Callers pass an inline arrow (e.g. `onClose={() => {...}}`), which
  // is a new function on every parent render — with it in the dep array the trap
  // tore down and re-engaged on each render, re-running `first?.focus()` and the
  // `previouslyFocused?.focus()` cleanup. With the ReportPanel open (it ticks
  // every 500ms while streaming) that stole focus into the panel over and over,
  // scrolling it into view and pulling the user out of the chat mid-answer.
  const onEscapeRef = useRef(onEscape);
  useEffect(() => {
    onEscapeRef.current = onEscape;
  }, [onEscape]);

  useEffect(() => {
    if (!active || !ref.current) return;

    const container = ref.current;
    const previouslyFocused = document.activeElement as HTMLElement | null;

    const focusables = () => Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE));
    const first = focusables()[0];
    first?.focus();

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onEscapeRef.current?.();
        return;
      }
      if (e.key !== "Tab") return;

      const els = focusables();
      if (els.length === 0) return;
      const firstEl = els[0];
      const lastEl = els[els.length - 1];

      if (e.shiftKey && document.activeElement === firstEl) {
        e.preventDefault();
        lastEl.focus();
      } else if (!e.shiftKey && document.activeElement === lastEl) {
        e.preventDefault();
        firstEl.focus();
      }
    };

    container.addEventListener("keydown", handleKeyDown);
    return () => {
      container.removeEventListener("keydown", handleKeyDown);
      previouslyFocused?.focus();
    };
  }, [active]);

  return ref;
}
