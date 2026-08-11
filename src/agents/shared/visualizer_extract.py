"""Server-side extraction of visualizer (DX topology) state for persistence.

The frontend renders the VisualizerCard from a compact ``<visualizer-state>``
tag. On the LIVE stream it synthesizes that tag itself from the tool result;
on HISTORY RELOAD it used to re-extract it from the raw ``<tool>`` output saved
to memory. That raw output is the full DX topology, JSON-string-escaped up to
three times (leaf -> orchestrator -> supervisor), which can triple a 49KB
topology past AgentCore Memory's 100KB per-event limit — so it got trimmed
away and the card vanished on reload.

Fix: extract the topology ONCE at save time and persist the compact,
de-escaped state as its own ``<visualizer-state>`` tag (~32KB for the largest
fixture, well under the limit). This is a faithful Python port of
``src/frontend/src/lib/visualizer-state.ts`` — keep the two in sync.
"""

from __future__ import annotations

import json
from typing import Any

_VIZ_MCP_TOOL_NAMES = {"discover_dx_topology", "assess_dx_resiliency"}

# Keep the persisted <visualizer-state> comfortably under memory.py's 96k event
# budget (it shares the event with prose + suggestions). If the paired
# topology+assessment shape exceeds this, fall back to topology-only.
_MAX_VIZ_STATE = 80_000


def _peel(raw: Any, depth: int = 3) -> Any:
    """Decode up to `depth` JSON-string layers; stop at first non-string."""
    cur = raw
    for _ in range(depth):
        if not isinstance(cur, str):
            break
        try:
            cur = json.loads(cur)
        except (ValueError, TypeError):
            break
    return cur


def _from_mcp_output(tool_name: str, result_content: Any) -> dict | None:
    """Pull {topology, assessment, toolName} out of one MCP tool's output."""
    if tool_name not in _VIZ_MCP_TOOL_NAMES:
        return None
    parsed = _peel(result_content)
    if not isinstance(parsed, dict):
        return None

    # Agent wrapper: {response: "...", tool_trace: [...]}. Peel `response`.
    if isinstance(parsed.get("response"), str):
        inner = _peel(parsed["response"])
        if isinstance(inner, dict):
            parsed = inner

    # MCP handler shape: {status: "success", data: {...}} — unwrap `data`.
    d = parsed["data"] if isinstance(parsed.get("data"), dict) else parsed

    if tool_name == "discover_dx_topology":
        if any(isinstance(d.get(k), list) for k in ("connections", "virtualInterfaces", "dxGateways")):
            return {"topology": d, "toolName": tool_name}
        return None

    if tool_name == "assess_dx_resiliency":
        if isinstance(d.get("topology"), dict) and isinstance(d.get("assessment"), dict):
            return {"topology": d["topology"], "assessment": d["assessment"], "toolName": tool_name}
        if "perDxGateway" in d or "resiliency" in d:
            return {"assessment": d, "toolName": tool_name}
        return None

    return None


def _base_tool_name(name: Any) -> str:
    """Strip the AgentCore Gateway ``server___tool`` prefix.

    The nested trace names the MCP tool as e.g.
    ``network-resilience___discover_dx_topology`` — the viz tool name is the
    part AFTER the ``___`` separator. Bare names pass through unchanged.
    """
    if not isinstance(name, str):
        return ""
    return name.split("___", 1)[1] if "___" in name else name


def _walk(node: Any) -> dict | None:
    """Recurse any object/array looking for a viz-bearing MCP tool result.

    Mirrors the frontend's loose walker: the topology is nested under a
    delegate's tool_trace as a gateway-prefixed tool_name whose ``output`` is a
    JSON string ``{status, data:{...topology...}}``, so search every value and
    peel string blobs.
    """
    if isinstance(node, dict):
        # A tool_trace entry names its MCP tool via `tool_name` (or `name`),
        # gateway-prefixed as `server___tool`.
        base = _base_tool_name(node.get("tool_name") or node.get("name"))
        if base in _VIZ_MCP_TOOL_NAMES:
            got = _from_mcp_output(base, node.get("output", node))
            if got:
                return got
        for v in node.values():
            got = _walk(_peel(v) if isinstance(v, str) else v)
            if got:
                return got
    elif isinstance(node, list):
        for v in node:
            got = _walk(_peel(v) if isinstance(v, str) else v)
            if got:
                return got
    return None


def _collect(node: Any, out: list[dict]) -> None:
    """Collect ALL viz-bearing results under *node* (not just the first)."""
    if isinstance(node, dict):
        base = _base_tool_name(node.get("tool_name") or node.get("name"))
        if base in _VIZ_MCP_TOOL_NAMES:
            got = _from_mcp_output(base, node.get("output", node))
            if got:
                out.append(got)
        for v in node.values():
            _collect(_peel(v) if isinstance(v, str) else v, out)
    elif isinstance(node, list):
        for v in node:
            _collect(_peel(v) if isinstance(v, str) else v, out)


def extract_visualizer_state(tool_segments: list[dict]) -> dict | None:
    """Return the compact visualizer state from a turn's ``tool`` segments.

    `tool_segments` are the ``{"type": "tool", "value": <json str>}`` entries
    collected during streaming. Returns ``{topology?, assessment?, toolName}``
    or None if the turn produced no DX topology.

    Prefers ``assess_dx_resiliency`` (carries topology AND assessment, so the
    card renders resiliency scores/ghost nodes) over a bare
    ``discover_dx_topology`` when a turn produced both — matching the frontend's
    preference for the richer paired shape.
    """
    found: list[dict] = []
    for seg in tool_segments:
        if seg.get("type") != "tool":
            continue
        try:
            tool_obj = json.loads(seg["value"])
        except (ValueError, TypeError, KeyError):
            continue
        _collect(tool_obj, found)
    if not found:
        return None
    # Prefer a result that carries an assessment (richer render). But if the
    # paired shape would blow the Memory event budget, fall back to a
    # topology-only result (card still renders, just without resiliency
    # scores) — better a plain card than none.
    paired = next(
        (r for r in found if r.get("assessment") is not None and r.get("topology") is not None),
        None,
    )
    topo_only = next((r for r in found if r.get("topology") is not None), None)
    if paired is not None:
        if len(json.dumps(paired, separators=(",", ":"))) <= _MAX_VIZ_STATE:
            return paired
        if topo_only is not None:
            return topo_only
    return topo_only or found[0]
