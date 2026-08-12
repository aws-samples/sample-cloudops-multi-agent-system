"""Unit tests for agents.shared.visualizer_extract.

The DX topology the VisualizerCard renders from is buried in the nested
tool_trace of a delegate result, under a GATEWAY-PREFIXED tool name
(network-resilience___discover_dx_topology) whose output is a JSON string.
These tests pin the extraction against a real captured payload so the
persisted <visualizer-state> can't silently regress to empty again.
"""

from __future__ import annotations

import json
from pathlib import Path

from agents.shared.visualizer_extract import (
    _MAX_VIZ_STATE,
    _base_tool_name,
    extract_visualizer_state,
)

_FIXTURE = Path(__file__).parent / "fixtures_dx_tool_segment.json"


def _real_segment() -> dict:
    return json.loads(_FIXTURE.read_text())


class TestBaseToolName:
    def test_strips_gateway_prefix(self):
        assert _base_tool_name("network-resilience___discover_dx_topology") == "discover_dx_topology"

    def test_bare_name_passthrough(self):
        assert _base_tool_name("assess_dx_resiliency") == "assess_dx_resiliency"

    def test_non_string_safe(self):
        assert _base_tool_name(None) == ""


class TestExtractVisualizerState:
    def test_extracts_from_real_delegate_payload(self):
        viz = extract_visualizer_state([_real_segment()])
        assert viz is not None, "regression: topology not extracted from real payload"
        assert viz["toolName"] in ("assess_dx_resiliency", "discover_dx_topology")
        assert isinstance(viz.get("topology"), dict)
        # The captured scenario has real DX connections.
        assert len(viz["topology"].get("connections", [])) > 0

    def test_prefers_paired_assessment_shape(self):
        # The real payload has BOTH discover_dx_topology and assess_dx_resiliency;
        # the richer paired shape (topology + assessment) must win.
        viz = extract_visualizer_state([_real_segment()])
        assert viz["toolName"] == "assess_dx_resiliency"
        assert viz.get("assessment") is not None

    def test_compact_state_fits_memory_budget(self):
        viz = extract_visualizer_state([_real_segment()])
        compact = json.dumps(viz, separators=(",", ":"))
        assert len(compact) <= _MAX_VIZ_STATE

    def test_no_topology_returns_none(self):
        seg = {"type": "tool", "value": json.dumps(
            {"name": "cost-operations-agent", "output": "spend was $1,234", "tool_trace": []}
        )}
        assert extract_visualizer_state([seg]) is None

    def test_falls_back_to_topology_only_when_paired_too_big(self, monkeypatch):
        import agents.shared.visualizer_extract as vx
        monkeypatch.setattr(vx, "_MAX_VIZ_STATE", 100)  # force the paired shape over budget
        viz = extract_visualizer_state([_real_segment()])
        assert viz is not None
        assert viz["toolName"] == "discover_dx_topology"  # dropped the assessment
        assert viz.get("assessment") is None
