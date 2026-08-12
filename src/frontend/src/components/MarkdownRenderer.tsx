"use client";

import { useState, useMemo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  BarChart3,
  Table2,
} from "lucide-react";
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell,
  AreaChart, Area, CartesianGrid, PieChart, Pie, Legend,
} from "recharts";

/* ── Chart colors ── */
export const CHART_COLORS = [
  "#34d399", "#60a5fa", "#f59e0b", "#a78bfa", "#f472b6",
  "#fb923c", "#38bdf8", "#4ade80", "#e879f9", "#facc15",
];

export function parseTableData(node: React.ReactNode): { headers: string[]; rows: string[][] } | null {
  const headers: string[] = [];
  const rows: string[][] = [];
  const children = Array.isArray(node) ? node : [node];

  for (const child of children) {
    if (!child || typeof child !== "object" || !("props" in child)) continue;
    const tag = child.type;
    if (tag === "thead" || (typeof tag === "function" && child.props?.children)) {
      const headRows = Array.isArray(child.props.children) ? child.props.children : [child.props.children];
      for (const tr of headRows) {
        if (!tr?.props?.children) continue;
        const ths = Array.isArray(tr.props.children) ? tr.props.children : [tr.props.children];
        for (const th of ths) {
          headers.push(extractText(th));
        }
      }
    }
    if (tag === "tbody" || (typeof tag === "function" && child.props?.children)) {
      const bodyRows = Array.isArray(child.props.children) ? child.props.children : [child.props.children];
      for (const tr of bodyRows) {
        if (!tr?.props?.children) continue;
        const tds = Array.isArray(tr.props.children) ? tr.props.children : [tr.props.children];
        const row: string[] = [];
        for (const td of tds) row.push(extractText(td));
        if (row.length) rows.push(row);
      }
    }
  }
  return headers.length && rows.length ? { headers, rows } : null;
}

export function extractText(node: React.ReactNode): string {
  if (node == null) return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(extractText).join("");
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  if (typeof node === "object" && "props" in node) return extractText((node as any).props?.children);
  return "";
}

export function parseNumeric(s: string): number | null {
  const cleaned = s.replace(/[$,%\s]/g, "");
  const n = parseFloat(cleaned);
  return isNaN(n) ? null : n;
}

type ChartType = "bar" | "area" | "pie";

const TIME_PATTERNS = /^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|\d{4}[-/]\d{2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|q[1-4]|week|month|day|date|period|time)/i;

// Columns that are just row indices — not meaningful for charting
const INDEX_COL_RE = /^(#|no\.?|rank|row|id|index|s\.?no\.?)$/i;

// Columns that represent deltas/changes — not meaningful alongside absolute values
const DELTA_COL_RE = /(\bchange\b|\bdiff\b|\bdelta\b|[→→]|vs\.?\b)/i;

export function detectChartType(labels: string[], headers: string[], numKeys: string[]): ChartType {
  const labelHeader = headers[0]?.toLowerCase() ?? "";
  const isTimeSeries =
    TIME_PATTERNS.test(labelHeader) ||
    labels.filter((l) => TIME_PATTERNS.test(l.trim())).length > labels.length * 0.5;

  if (isTimeSeries && labels.length >= 3) return "area";
  if (numKeys.length === 1 && labels.length >= 2 && labels.length <= 8) return "pie";
  return "bar";
}

export function formatValue(v: number): string {
  if (Math.abs(v) >= 1e6) return `$${(v / 1e6).toFixed(1)}M`;
  if (Math.abs(v) >= 1e3) return `$${(v / 1e3).toFixed(1)}K`;
  return `$${v.toFixed(2)}`;
}

function truncateLabel(s: string, max = 14): string {
  return s.length > max ? s.slice(0, max - 1) + "…" : s;
}

const TOOLTIP_STYLE = {
  contentStyle: { background: "var(--bg-elevated)", border: "1px solid var(--border-default)", borderRadius: 8, fontSize: 11, color: "var(--text-primary)" },
  labelStyle: { color: "var(--text-primary)", fontWeight: 500 },
  itemStyle: { color: "var(--text-secondary)" },
};

const LEGEND_STYLE = {
  wrapperStyle: { fontSize: 10, color: "var(--text-muted)", paddingTop: 8 },
};

export function SmartTable({ children }: { children: React.ReactNode }) {
  const [showChart, setShowChart] = useState(false);

  const chartInfo = useMemo(() => {
    const parsed = parseTableData(children);
    if (!parsed || parsed.headers.length < 2) return null;

    // Need at least 3 data rows for a meaningful chart
    if (parsed.rows.length < 3) return null;

    // Find label column (first non-numeric column)
    let labelCol = 0;
    for (let c = 0; c < parsed.headers.length; c++) {
      if (!parsed.rows.every((row) => parseNumeric(row[c] ?? "") !== null)) {
        labelCol = c;
        break;
      }
    }

    // Skip tables with long descriptive text in any column
    const hasLongText = parsed.rows.some((r) =>
      r.some((cell) => (cell?.length ?? 0) > 80)
    );
    if (hasLongText) return null;

    // Find numeric columns, excluding index/rank and delta/change columns
    const numCols: number[] = [];
    for (let c = 0; c < parsed.headers.length; c++) {
      if (c === labelCol) continue;
      if (INDEX_COL_RE.test(parsed.headers[c])) continue;
      if (DELTA_COL_RE.test(parsed.headers[c])) continue;
      if (parseNumeric(parsed.rows[0]?.[c] ?? "") !== null) numCols.push(c);
    }
    if (!numCols.length) return null;

    // Require meaningful numeric density (≥30% of non-index columns)
    const nonIndexCols = parsed.headers.filter((h) => !INDEX_COL_RE.test(h)).length;
    if (numCols.length / nonIndexCols < 0.3) return null;

    const data = parsed.rows.map((row) => {
      const entry: Record<string, string | number> = { name: row[labelCol] ?? "" };
      for (const c of numCols) entry[parsed.headers[c]] = parseNumeric(row[c] ?? "") ?? 0;
      return entry;
    });
    const keys = numCols.map((c) => parsed.headers[c]);
    const labels = parsed.rows.map((r) => r[labelCol] ?? "");
    const chartType = detectChartType(labels, [parsed.headers[labelCol], ...keys], keys);

    // Sort bar charts descending by first numeric column
    if (chartType === "bar") {
      data.sort((a, b) => (b[keys[0]] as number) - (a[keys[0]] as number));
    }

    return { data, keys, chartType };
  }, [children]);

  return (
    <div className="my-3 relative" data-smart-table>
      {chartInfo && (
        <div className="flex justify-end mb-1.5">
          <button
            onClick={() => setShowChart(!showChart)}
            className="flex items-center gap-1 text-xs px-2 py-1 rounded-md"
            style={{ color: "var(--text-muted)", background: "var(--bg-elevated)" }}
            aria-label={showChart ? "Switch to table view" : "Switch to chart view"}
            data-chart-toggle
          >
            {showChart ? <Table2 className="h-3 w-3" aria-hidden="true" /> : <BarChart3 className="h-3 w-3" aria-hidden="true" />}
            <span data-toggle-label>{showChart ? "Table" : "Chart"}</span>
          </button>
        </div>
      )}
      {/* Chart view — always rendered when available so download can capture it */}
      {chartInfo && (
        <div data-view="chart" className="rounded-lg p-3" style={showChart ? { background: "var(--bg-surface)", border: "1px solid var(--border-default)" } : { position: "absolute", opacity: 0, pointerEvents: "none", zIndex: -1, width: "100%", background: "var(--bg-surface)", border: "1px solid var(--border-default)" }}>
          {chartInfo.chartType === "area" ? (
            /* Area chart for time series — shows magnitude + trend */
            <ResponsiveContainer width="100%" height={300} debounce={150}>
              <AreaChart data={chartInfo.data} margin={{ left: 10, right: 20, top: 5, bottom: chartInfo.data.length > 6 ? 40 : 5 }}>
                <defs>
                  {chartInfo.keys.map((key, i) => (
                    <linearGradient key={key} id={`grad-${i}`} x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor={CHART_COLORS[i % CHART_COLORS.length]} stopOpacity={0.3} />
                      <stop offset="95%" stopColor={CHART_COLORS[i % CHART_COLORS.length]} stopOpacity={0.02} />
                    </linearGradient>
                  ))}
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border-subtle)" />
                <XAxis
                  dataKey="name"
                  tick={{ fill: "var(--text-muted)", fontSize: 10 }}
                  angle={chartInfo.data.length > 6 ? -45 : 0}
                  textAnchor={chartInfo.data.length > 6 ? "end" : "middle"}
                  height={chartInfo.data.length > 6 ? 60 : 30}
                />
                <YAxis tick={{ fill: "var(--text-muted)", fontSize: 10 }} tickFormatter={(v) => formatValue(v)} />
                <Tooltip {...TOOLTIP_STYLE} formatter={(value) => formatValue(Number(value))} cursor={{ fill: "var(--chart-cursor)" }} />
                {chartInfo.keys.length > 1 && <Legend {...LEGEND_STYLE} />}
                {chartInfo.keys.map((key, i) => (
                  <Area
                    key={key}
                    type="monotone"
                    dataKey={key}
                    stroke={CHART_COLORS[i % CHART_COLORS.length]}
                    strokeWidth={2}
                    fill={`url(#grad-${i})`}
                    dot={{ fill: CHART_COLORS[i % CHART_COLORS.length], r: 3 }}
                    activeDot={{ r: 5 }}
                  />
                ))}
              </AreaChart>
            </ResponsiveContainer>
          ) : chartInfo.chartType === "pie" ? (
            <ResponsiveContainer width="100%" height={300} debounce={150}>
              <PieChart>
                <Pie
                  data={chartInfo.data}
                  dataKey={chartInfo.keys[0]}
                  nameKey="name"
                  cx="50%"
                  cy="50%"
                  outerRadius={100}
                  innerRadius={50}
                  paddingAngle={2}
                  label={({ name, percent }) => `${truncateLabel(String(name ?? ""), 18)} (${((percent ?? 0) * 100).toFixed(0)}%)`}
                  labelLine={{ stroke: "var(--text-muted)" }}
                  style={{ fontSize: 10 }}
                >
                  {chartInfo.data.map((_, i) => (
                    <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />
                  ))}
                </Pie>
                <Tooltip {...TOOLTIP_STYLE} formatter={(value, name) => [formatValue(Number(value)), name]} />
              </PieChart>
            </ResponsiveContainer>
          ) : (
            /* Default: horizontal bar chart, sorted descending */
            <ResponsiveContainer width="100%" height={Math.min(chartInfo.data.length * (chartInfo.keys.length > 1 ? 48 : 36) + 60, 500)} debounce={150}>
              <BarChart data={chartInfo.data} layout="vertical" margin={{ left: 10, right: 20, top: 5, bottom: 5 }}>
                <XAxis type="number" tick={{ fill: "var(--text-muted)", fontSize: 10 }} tickFormatter={(v) => formatValue(v)} />
                <YAxis type="category" dataKey="name" width={120} tick={{ fill: "var(--text-muted)", fontSize: 10 }} tickFormatter={(v) => truncateLabel(v)} />
                <Tooltip {...TOOLTIP_STYLE} formatter={(value) => formatValue(Number(value))} cursor={{ fill: "var(--chart-cursor)" }} />
                {chartInfo.keys.length > 1 && <Legend {...LEGEND_STYLE} />}
                {chartInfo.keys.map((key, i) => (
                  <Bar key={key} dataKey={key} fill={CHART_COLORS[i % CHART_COLORS.length]} radius={[0, 4, 4, 0]}>
                    {chartInfo.data.map((_, j) => (
                      <Cell key={j} fill={CHART_COLORS[(chartInfo.keys.length > 1 ? i : j) % CHART_COLORS.length]} />
                    ))}
                  </Bar>
                ))}
              </BarChart>
            </ResponsiveContainer>
          )}
        </div>
      )}
      {/* Table view */}
      <div data-view="table" className="overflow-x-auto rounded-lg" style={showChart ? { position: "absolute", opacity: 0, pointerEvents: "none", zIndex: -1, width: "100%" } : { border: "1px solid var(--border-default)" }}>
        <table className="min-w-full text-xs border-collapse">{children}</table>
      </div>
    </div>
  );
}

export function MarkdownText({ text }: { text: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        table: ({ children }) => <SmartTable>{children}</SmartTable>,
        code: ({ className, children }) => {
          const isBlock = className?.includes("language-");
          return isBlock ? (
            <pre className="overflow-x-auto rounded-lg p-3 my-2 text-xs" style={{ background: "var(--bg-primary)" }}>
              <code>{children}</code>
            </pre>
          ) : (
            <code>{children}</code>
          );
        },
      }}
    >
      {text}
    </ReactMarkdown>
  );
}
