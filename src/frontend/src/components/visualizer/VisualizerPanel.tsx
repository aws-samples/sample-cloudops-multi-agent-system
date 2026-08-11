"use client";

/**
 * VisualizerPanel — third right-side panel mode (alongside ReportPanel
 * and TracePanel). Hosts the DX topology visualizer.
 *
 * Size: resizable, default 900px, wider than ReportPanel (network diagrams
 * need room). Width persisted to ``localStorage.visualizer-panel-width``.
 *
 * Lifecycle:
 *   - Mounted by page.tsx when ``activeVisualizerMessageId`` is set.
 *   - Opening the panel should also collapse the left sidebar (see page.tsx
 *     ``open-visualizer`` listener) — this file doesn't manage that.
 *   - onClose resets activeVisualizerMessageId to null on the parent.
 *
 * Data source: the parent reads the message's ``<visualizer-state>`` tag,
 * extracts the payload, and passes ``topology`` + ``assessment`` as props.
 * Keeps VisualizerPanel purely presentational; store updates happen at the
 * page level so state survives panel remounts.
 */

import { useState, useCallback, useEffect } from "react";
import dynamic from "next/dynamic";
import { X, Network } from "lucide-react";
import "@xyflow/react/dist/style.css";
import type { TopologyData, CombinedAssessment } from "@/lib/topology";
import { useTopologyStore } from "@/lib/topology/store";
import { useTheme } from "@/lib/theme";
import { useTopologyGraph } from "./useTopologyGraph";
import { useReassess } from "./useReassess";
import { useLiveStatus } from "./useLiveStatus";
import { useUtilization } from "./useUtilization";
import { VisualizerToolbar } from "./VisualizerToolbar";
import { ResiliencyScoreCard } from "./ResiliencyScoreCard";
import { useFocusTrap } from "@/lib/a11y/use-focus-trap";
import { TourAutoStarter } from "@/lib/tours/TourAutoStarter";
// Side-effect import: registers the visualizer tour with the central registry.
import "./visualizer-tour";

// React Flow touches `window` on import; defer to client-only render to keep
// the Next.js SSR build clean.
const FlowCanvas = dynamic(() => import("./FlowCanvas").then((m) => m.FlowCanvas), {
  ssr: false,
});

interface VisualizerPanelProps {
  messageId: string;
  topology: TopologyData | null;
  assessment: CombinedAssessment | null;
  onClose: () => void;
  // When the page has collapsed the main chat column for a full-bleed
  // topology view, the panel ignores its user-resized width and expands to
  // fill whatever space the flex row gives it. Hides the resize handle too
  // since drag-to-resize makes no sense against a viewport-filling panel.
  fullBleed?: boolean;
}

const MIN_WIDTH = 720;
const DEFAULT_WIDTH = 960;
const MAX_WIDTH_RATIO = 0.75;
const STORAGE_KEY = "visualizer-panel-width";

export function VisualizerPanel({
  messageId,
  topology,
  assessment,
  onClose,
  fullBleed = false,
}: VisualizerPanelProps) {
  const setTheme = useTopologyStore((s) => s.setTheme);
  const { theme } = useTheme();

  // Mirror the app-wide theme into the visualizer store so node components
  // can read it through their own selector without threading a prop chain.
  useEffect(() => {
    setTheme(theme);
  }, [theme, setTheme]);

  // Rebuild the layout whenever the topology/assessment props change or the
  // user toggles an expandable group — replaces the Vite app's useTopology hook.
  useTopologyGraph(topology, assessment);
  // Target-tier flips fire POST /reassess and rewrite store.assessment in place.
  useReassess();
  // Polling loop while the `Live Status` overlay is enabled.
  useLiveStatus();
  // On-demand peak-utilization fetch when the user enables the overlay.
  useUtilization();

  const [panelWidth, setPanelWidth] = useState(() => {
    if (typeof window === "undefined") return DEFAULT_WIDTH;
    const saved = localStorage.getItem(STORAGE_KEY);
    if (!saved) return DEFAULT_WIDTH;
    const n = Number(saved);
    if (!Number.isFinite(n)) return DEFAULT_WIDTH;
    return Math.max(MIN_WIDTH, Math.min(n, window.innerWidth * MAX_WIDTH_RATIO));
  });
  const [isDragging, setIsDragging] = useState(false);
  const panelRef = useFocusTrap<HTMLDivElement>(true, onClose);

  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    setIsDragging(true);
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";

    // Direct DOM writes during the drag; React state committed once on mouseup.
    // Per-mousemove setState re-rendered the panel's heavy children (ReactFlow
    // canvas here; Recharts in ReportPanel) ~60x/s — oscillating the handle
    // stacked their ResizeObserver/setState cascades until React threw
    // "Maximum update depth exceeded". See ReportPanel.handleMouseDown.
    let liveWidth = 0;
    const onMouseMove = (ev: MouseEvent) => {
      liveWidth = Math.max(
        MIN_WIDTH,
        Math.min(window.innerWidth - ev.clientX, window.innerWidth * MAX_WIDTH_RATIO),
      );
      if (panelRef.current) {
        panelRef.current.style.width = `${liveWidth}px`;
        // Keep the chat-collapse button anchored during the drag (it reads
        // this var); the panelWidth effect re-publishes it after commit.
        document.documentElement.style.setProperty("--visualizer-panel-width", `${liveWidth}px`);
      }
    };
    const onMouseUp = () => {
      document.removeEventListener("mousemove", onMouseMove);
      document.removeEventListener("mouseup", onMouseUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      setIsDragging(false);
      if (liveWidth) {
        localStorage.setItem(STORAGE_KEY, String(liveWidth));
        setPanelWidth(liveWidth);
      }
    };
    document.addEventListener("mousemove", onMouseMove);
    document.addEventListener("mouseup", onMouseUp);
  }, [panelRef]);

  // Escape-to-close is now handled by `useFocusTrap` above — no separate
  // window-level listener needed. Keeping the comment so a future reader
  // doesn't reintroduce the handler and fight the trap.

  // Expose the current panel width to the page-level chat-collapse toggle so
  // that button anchors to the visualizer's left edge regardless of the
  // user-resized width. Document-level CSS custom prop keeps this loosely
  // coupled — no prop drilling through page.tsx. When the panel goes
  // full-bleed, the chat-collapse button anchors to the left edge instead
  // so this var is irrelevant; we still publish it so CSS fallback math
  // stays valid.
  useEffect(() => {
    if (typeof document === "undefined") return;
    document.documentElement.style.setProperty(
      "--visualizer-panel-width",
      `${panelWidth}px`,
    );
    return () => {
      document.documentElement.style.removeProperty("--visualizer-panel-width");
    };
  }, [panelWidth]);

  return (
    <aside
      ref={panelRef}
      className="flex flex-col h-dvh flex-shrink-0 relative"
      style={{
        // Full-bleed mode: ignore the user-resized fixed width and let flex
        // stretch us across whatever the row gives us. Regular mode keeps
        // the fixed width so the chat column isn't squeezed.
        ...(fullBleed
          ? { flex: "1 1 0%", minWidth: 0, width: "auto" }
          : { width: panelWidth, flex: "0 0 auto" }),
        background: "var(--bg-secondary)",
        borderLeft: "1px solid var(--border-subtle)",
      }}
      aria-label="Network topology visualizer"
      data-message-id={messageId}
    >
      {/* Resize handle — hidden in full-bleed mode; dragging against a
           flex-filled panel is meaningless and the handle would overlap the
           chat-collapse toggle. */}
      {!fullBleed && (
        <div
          className={`resize-handle absolute left-0 top-0 bottom-0 z-10${isDragging ? " dragging" : ""}`}
          style={{ width: 6, cursor: "col-resize" }}
          onMouseDown={handleMouseDown}
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize visualizer panel"
        />
      )}

      {/* Header */}
      <header
        className="flex items-center justify-between px-4 py-3 flex-shrink-0"
        style={{ borderBottom: "1px solid var(--border-subtle)" }}
      >
        <div className="flex items-center gap-2 min-w-0">
          <Network
            className="h-4 w-4 flex-shrink-0"
            style={{ color: "var(--accent-ai, var(--accent))" }}
            aria-hidden="true"
          />
          <h2
            className="text-sm font-medium truncate"
            style={{ color: "var(--text-primary)" }}
          >
            Network topology
          </h2>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close visualizer panel"
          className="p-1.5 rounded-md transition-colors hover:bg-[var(--bg-elevated)]"
          style={{ color: "var(--text-muted)" }}
        >
          <X className="h-4 w-4" />
        </button>
      </header>

      {/* Interactive controls */}
      {topology && <VisualizerToolbar />}

      {/* First-visit visualizer tour auto-starts once the toolbar mounts. */}
      {topology && <TourAutoStarter tourId="visualizer" waitFor='[data-tour="overlays"]' />}

      {/* Body */}
      <div className="flex-1 min-h-0 overflow-hidden relative">
        {topology ? (
          <>
            <FlowCanvas />
            {/* Scorecard floats over the canvas in the bottom-left corner — */}
            {/* same position as the source SPA. Inside the same relative */}
            {/* wrapper so it inherits the panel's clipping instead of the */}
            {/* viewport's. */}
            <div
              data-tour="scorecard"
              className="absolute left-3 bottom-3 max-w-[440px] pointer-events-auto z-10"
            >
              <ResiliencyScoreCard />
            </div>
          </>
        ) : (
          <div
            className="flex items-center justify-center h-full text-sm"
            style={{ color: "var(--text-muted)" }}
          >
            No topology data available for this message.
          </div>
        )}
      </div>
    </aside>
  );
}
