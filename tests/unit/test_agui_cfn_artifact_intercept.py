"""Unit tests for the supervisor's <cfn-artifact> interceptor.

The interceptor lives in ``agents.shared.agui_server`` and runs inside the
chat-mode AG-UI ``event_generator``. It splits TEXT_MESSAGE_CONTENT deltas
into:

  * forwards — text outside the <cfn-artifact> block, sent to the client
    and tracked into Memory's enriched-save state.
  * completed_artifacts — (title, yaml) tuples for blocks that closed
    during the delta. The supervisor persists each to the reports table
    and replaces it in the stream with a <report-pending> marker.

Properties under test:
  1. No-tag streams pass through bit-for-bit unchanged (other agents
     unaffected).
  2. A complete <cfn-artifact> in a single delta yields the prefix forward,
     captures the YAML body, and reports the close.
  3. Tags split across deltas — open straddling, close straddling, body
     across many deltas — all behave identically to a single-delta input.
  4. Multiple artifacts in one stream are captured separately in order.
  5. Title attribute is parsed; missing title returns "".
  6. Unterminated artifact (no close tag) flushes a fallback string with a
     marker note so the user at least sees the partial output.
  7. Trailing partial-open prefix is held back across delta boundaries
     (e.g. a delta ending "<cfn-art" must NOT forward those chars yet).
  8. ``_persist_cfn_artifact_as_report`` writes a row whose user_id /
     section / status / fenced YAML shape matches what the frontend's
     ReportPanel expects to load via getReport(report_id).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from agents.shared.agui_server import (
    _CfnArtifactInterceptor,
    _ReportPendingStripper,
    _abbreviate_bulky_tool_traces,
    _handle_cfn_artifacts_from_tool_result,
    _new_cfn_artifact_report_id,
    _persist_cfn_artifact_as_report,
)
from agents.shared.registry import get_pending_cfn_artifacts

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _feed_all(deltas: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Feed every delta in order; concat all forwards, return all completed."""
    interceptor = _CfnArtifactInterceptor()
    all_forwards: list[str] = []
    all_completed: list[tuple[str, str]] = []
    for d in deltas:
        forwards, completed = interceptor.feed(d)
        all_forwards.extend(forwards)
        all_completed.extend(completed)
    return all_forwards, all_completed


# ---------------------------------------------------------------------------
# 1. Passthrough — no <cfn-artifact> tag at all
# ---------------------------------------------------------------------------


class TestPassthroughUnaffected:
    def test_plain_text_unchanged(self):
        forwards, completed = _feed_all(
            ["Hello, ", "world. ", "No tags here."]
        )
        assert "".join(forwards) == "Hello, world. No tags here."
        assert completed == []

    def test_markdown_with_other_tags_passes_through(self):
        # Other tags (think, tool, suggestions, artifact, report-body,
        # visualizer-state, report-pending) must NOT trigger the
        # interceptor — only <cfn-artifact> does.
        deltas = [
            "<think>reasoning here</think>",
            "<tool>{\"name\":\"x\"}</tool>",
            "<artifact>{\"title\":\"x\"}</artifact>",
            "<report-body>section content</report-body>",
        ]
        forwards, completed = _feed_all(deltas)
        assert "".join(forwards) == "".join(deltas)
        assert completed == []


# ---------------------------------------------------------------------------
# 2. Complete <cfn-artifact> in a single delta
# ---------------------------------------------------------------------------


class TestSingleDeltaArtifact:
    def test_full_artifact_in_one_delta(self):
        delta = (
            "Summary first.\n"
            "<cfn-artifact title=\"My alarms\">"
            "AWSTemplateFormatVersion: '2010-09-09'\nResources: {}\n"
            "</cfn-artifact>"
            "\nApply now."
        )
        forwards, completed = _feed_all([delta])

        assert "".join(forwards) == "Summary first.\n\nApply now."
        assert len(completed) == 1
        title, yaml_text = completed[0]
        assert title == "My alarms"
        assert yaml_text == (
            "AWSTemplateFormatVersion: '2010-09-09'\nResources: {}\n"
        )

    def test_yaml_never_appears_in_forwards(self):
        # Concrete invariant: the YAML body must be entirely absent
        # from anything yielded back to the client.
        delta = (
            "<cfn-artifact title=\"x\">"
            "secret-yaml-content-marker-12345"
            "</cfn-artifact>"
        )
        forwards, _ = _feed_all([delta])
        joined = "".join(forwards)
        assert "secret-yaml-content-marker-12345" not in joined

    def test_missing_title_attribute_returns_empty_string(self):
        delta = "<cfn-artifact>YAML body</cfn-artifact>"
        _, completed = _feed_all([delta])
        assert completed == [("", "YAML body")]


# ---------------------------------------------------------------------------
# 3. Tag split across delta boundaries
# ---------------------------------------------------------------------------


class TestTagSplitAcrossDeltas:
    def test_open_tag_split_across_two_deltas(self):
        forwards, completed = _feed_all(
            [
                "Hello.<cfn-art",            # partial open
                "ifact title=\"Split open\">YAML body</cfn-artifact>End.",
            ]
        )
        assert "".join(forwards) == "Hello.End."
        assert completed == [("Split open", "YAML body")]

    def test_close_tag_split_across_deltas(self):
        forwards, completed = _feed_all(
            [
                "<cfn-artifact title=\"Split close\">YAML body</cfn-art",
                "ifact>Tail.",
            ]
        )
        assert "".join(forwards) == "Tail."
        assert completed == [("Split close", "YAML body")]

    def test_body_streamed_across_many_deltas(self):
        # The model emits the YAML in many small token-sized deltas.
        # The interceptor must accumulate the full body without leakage.
        deltas = [
            "<cfn-artifact title=\"streaming\">",
            "AWSTemplate",
            "FormatVersion: ",
            "'2010-09-09'\n",
            "Resources:\n",
            "  Alarm1:\n",
            "    Type: AWS::CloudWatch::Alarm\n",
            "</cfn-artifact>",
        ]
        forwards, completed = _feed_all(deltas)
        assert "".join(forwards) == ""
        assert len(completed) == 1
        title, yaml_text = completed[0]
        assert title == "streaming"
        assert yaml_text == (
            "AWSTemplateFormatVersion: '2010-09-09'\n"
            "Resources:\n  Alarm1:\n    Type: AWS::CloudWatch::Alarm\n"
        )

    def test_partial_open_prefix_held_back_across_deltas(self):
        # A delta that ends with a partial open tag prefix must NOT
        # forward those characters yet — they might be the start of a
        # cfn-artifact open tag in the next delta.
        interceptor = _CfnArtifactInterceptor()
        forwards1, completed1 = interceptor.feed("Some text<cfn-art")
        # The "<cfn-art" prefix is held back as carry.
        assert "".join(forwards1) == "Some text"
        assert completed1 == []
        # Next delta resolves it as NOT actually a cfn-artifact tag.
        forwards2, completed2 = interceptor.feed("ifle of an article")
        assert "".join(forwards2) == "<cfn-artifle of an article"
        assert completed2 == []

    def test_open_tag_with_long_title_completes_on_close_bracket(self):
        # Regression: the user hit this in production. The model emitted
        # the open tag's literal `<cfn-artifact title="…"` in one delta,
        # then the closing `>` and the YAML body in subsequent deltas.
        # The original implementation forwarded the open tag as plain text
        # because the regex `<cfn-artifact\b[^>]*>` didn't match without
        # the `>` — the YAML then streamed through as raw text in chat.
        interceptor = _CfnArtifactInterceptor()
        forwards1, _ = interceptor.feed(
            "Summary text. <cfn-artifact title=\"CloudWatch Alarms — "
            "environment=dev (clare-cloudops) — 34 Alarms\""
        )
        # The delta has no `>` — the entire `<cfn-artifact …` literal must
        # be held back, but the prefix "Summary text. " can stream out.
        assert "".join(forwards1) == "Summary text. "

        forwards2, completed2 = interceptor.feed(">YAML body</cfn-artifact>")
        # Now the open tag completes; YAML body buffers; close tag fires.
        assert "".join(forwards2) == ""
        assert len(completed2) == 1
        title, yaml_text = completed2[0]
        assert title == (
            "CloudWatch Alarms — environment=dev (clare-cloudops) — 34 Alarms"
        )
        assert yaml_text == "YAML body"

    def test_open_tag_attributes_streamed_token_by_token(self):
        # Even harder: every char of the open tag arrives in its own delta.
        deltas = list("<cfn-artifact title=\"x\">YAML</cfn-artifact>")
        forwards, completed = _feed_all(deltas)
        # Nothing forwarded (the entire delta sequence is the artifact).
        assert "".join(forwards) == ""
        assert completed == [("x", "YAML")]


# ---------------------------------------------------------------------------
# 4. Multiple artifacts in one stream
# ---------------------------------------------------------------------------


class TestMultipleArtifacts:
    def test_two_back_to_back_artifacts(self):
        delta = (
            "First: "
            "<cfn-artifact title=\"a\">YAML A</cfn-artifact>"
            " then "
            "<cfn-artifact title=\"b\">YAML B</cfn-artifact>"
            " done."
        )
        forwards, completed = _feed_all([delta])
        assert "".join(forwards) == "First:  then  done."
        assert completed == [("a", "YAML A"), ("b", "YAML B")]


# ---------------------------------------------------------------------------
# 5. Unterminated artifact — flush() fallback
# ---------------------------------------------------------------------------


class TestUnterminatedArtifactFallback:
    def test_no_close_tag_flushes_partial_with_marker(self):
        interceptor = _CfnArtifactInterceptor()
        forwards, completed = interceptor.feed(
            "<cfn-artifact title=\"truncated\">partial-yaml-content"
        )
        assert "".join(forwards) == ""
        assert completed == []

        flushed = interceptor.flush()
        joined = "".join(flushed)
        assert "partial-yaml-content" not in joined
        assert "incomplete" in joined.lower()

    def test_flush_returns_carry_in_passthrough_state(self):
        # If the carry holds back a partial-open prefix at end-of-stream,
        # flush() must release it as plain text.
        interceptor = _CfnArtifactInterceptor()
        forwards, _ = interceptor.feed("Trailing<cfn-art")
        assert "".join(forwards) == "Trailing"
        flushed = interceptor.flush()
        assert "".join(flushed) == "<cfn-art"


# ---------------------------------------------------------------------------
# 6. Defensive carry cap
# ---------------------------------------------------------------------------


class TestCarryCap:
    def test_unbounded_partial_is_eventually_flushed(self):
        # If the model emits a malformed tag that the interceptor can't
        # resolve, the carry must not grow without bound.
        interceptor = _CfnArtifactInterceptor()
        # Feed >MAX_CARRY chars that look like the start of a tag.
        big = "<cfn-artif" + "a" * 600
        forwards, completed = interceptor.feed(big)
        assert completed == []
        # Once the carry exceeds MAX_CARRY, it gets flushed as text.
        # (This is the "defensive" path; the precise content split is an
        # implementation detail, but the total output must equal the input.)
        joined = "".join(forwards) + "".join(interceptor.flush())
        assert joined == big


# ---------------------------------------------------------------------------
# 7. _persist_cfn_artifact_as_report — DynamoDB row shape
# ---------------------------------------------------------------------------


class TestPersistCfnArtifactAsReport:
    def test_writes_complete_single_section_report(self):
        with patch("agents.shared.agui_server.reports.save_report") as mock_save:
            mock_save.return_value = True
            ok = _persist_cfn_artifact_as_report(
                report_id="rpt-cfn-abc123",
                title="Alarms for App=test",
                template_yaml="AWSTemplateFormatVersion: '2010-09-09'\n",
                actor_id="claretjy_at_test_com",
                report_table="my-report-table",
                region="us-east-1",
            )
        assert ok is True
        mock_save.assert_called_once()
        report_data = mock_save.call_args.args[0]
        # Frontend's getReport(report_id) requires this exact shape.
        assert report_data["report_id"] == "rpt-cfn-abc123"
        assert report_data["user_id"] == "claretjy_at_test_com"
        assert report_data["status"] == "complete"
        assert report_data["title"] == "Alarms for App=test"
        assert len(report_data["sections"]) == 1
        section = report_data["sections"][0]
        assert section["status"] == "complete"
        assert section["title"] == "CloudFormation Template"
        # YAML must be wrapped in a ```yaml fenced block so ReportPanel
        # renders it with syntax highlighting.
        assert section["content"].startswith("```yaml\n")
        assert section["content"].endswith("\n```")
        assert "AWSTemplateFormatVersion" in section["content"]

    def test_missing_title_falls_back_to_default(self):
        with patch("agents.shared.agui_server.reports.save_report") as mock_save:
            mock_save.return_value = True
            _persist_cfn_artifact_as_report(
                report_id="rpt-cfn-x",
                title="",
                template_yaml="x",
                actor_id="a",
                report_table="t",
                region="us-east-1",
            )
        report_data = mock_save.call_args.args[0]
        assert report_data["title"] == "CloudFormation alarm template"

    def test_no_table_returns_false(self):
        # Defensive: missing REPORT_TABLE_NAME must not raise — the caller
        # falls back to streaming the YAML through as plain text.
        ok = _persist_cfn_artifact_as_report(
            report_id="rpt-cfn-x",
            title="t",
            template_yaml="y",
            actor_id="a",
            report_table="",
            region="us-east-1",
        )
        assert ok is False

    def test_save_report_failure_returns_false(self):
        with patch("agents.shared.agui_server.reports.save_report") as mock_save:
            mock_save.return_value = False
            ok = _persist_cfn_artifact_as_report(
                report_id="rpt-cfn-x",
                title="t",
                template_yaml="y",
                actor_id="a",
                report_table="t",
                region="us-east-1",
            )
        assert ok is False


# ---------------------------------------------------------------------------
# 8. _new_cfn_artifact_report_id — stable prefix for future migration
# ---------------------------------------------------------------------------


class TestReportIdPrefix:
    def test_id_prefix_is_stable(self):
        rid = _new_cfn_artifact_report_id()
        assert rid.startswith("rpt-cfn-")
        # uuid4 hex[:12] = 12 hex chars, plus "rpt-cfn-" = 8, total 20.
        assert len(rid) == 8 + 12

    def test_ids_are_unique(self):
        ids = {_new_cfn_artifact_report_id() for _ in range(100)}
        assert len(ids) == 100



# ---------------------------------------------------------------------------
# 9. _abbreviate_bulky_tool_traces — keep CFN tool output out of Memory
# ---------------------------------------------------------------------------


class TestAbbreviateBulkyToolTraces:
    """`assemble_cfn_template` returns a 100k+ char template. The user-facing
    body already goes to the reports table (via <report-pending>), but a SECOND
    copy rides inside the collapsed `<tool>` trace that gets persisted to
    AgentCore Memory — overflowing the 100k-char per-event limit and silently
    dropping the whole assistant turn on reload (reproduced live).

    The CFN tool trace has NO functional consumer on reload (it's a display-only
    collapsed view; the body re-renders from the reports table). So we replace
    its `output` with a short placeholder, keyed by TOOL NAME — robust to the
    runtime envelope's deep nesting/escaping. DX-topology tools are exempt
    because the visualizer rebuilds the diagram from their saved trace output.
    """

    def test_cfn_tool_output_abbreviated_top_level(self):
        big = "AWSTemplateFormatVersion " + ("x" * 100_000)
        tool_data = {"name": "assemble_cfn_template", "input": {}, "output": big}
        out = _abbreviate_bulky_tool_traces(tool_data)
        assert out["output"] == "[tool output omitted from chat memory — see ReportPanel]"
        assert "AWSTemplateFormatVersion" not in json.dumps(out)
        assert len(json.dumps(out)) < 1_000

    def test_cfn_output_abbreviated_in_nested_trace(self):
        # The ACTUAL production shape: the YAML rides in a NESTED trace entry
        # (worker assemble_cfn_template) under an orchestrator <tool> segment.
        big = "x" * 120_000
        tool_data = {
            "name": "ops-excellence-agent",
            "input": {},
            "output": "short sanitized summary",
            "tool_trace": [
                {"tool_name": "assemble_cfn_template", "output": big, "status": "success"},
                {"tool_name": "get_metric_data", "output": "small", "status": "success"},
            ],
        }
        out = _abbreviate_bulky_tool_traces(tool_data)
        assert big not in json.dumps(out)
        # The CFN nested entry is abbreviated; the sibling non-bulky tool is kept.
        nested = out["tool_trace"]
        assert nested[0]["output"] == "[tool output omitted from chat memory — see ReportPanel]"
        assert nested[1]["output"] == "small"
        # Orchestrator's own (already-sanitized) output is untouched.
        assert out["output"] == "short sanitized summary"

    def test_gateway_prefixed_cfn_tool_is_abbreviated(self):
        # In the real trace the tool name is gateway-prefixed
        # ("cloudwatch___assemble_cfn_template"). The prefix must be stripped
        # before matching or the abbreviation silently no-ops (the live bug).
        big = "x" * 120_000
        tool_data = {
            "name": "ops-excellence-agent",
            "output": "summary",
            "tool_trace": [
                {"tool_name": "cloudwatch___assemble_cfn_template", "output": big},
            ],
        }
        out = _abbreviate_bulky_tool_traces(tool_data)
        assert big not in json.dumps(out)
        assert out["tool_trace"][0]["output"] == "[tool output omitted from chat memory — see ReportPanel]"

    def test_recommendation_fanout_trace_is_abbreviated(self):
        # The live >100k overflow: a many-resource CFN run fans out
        # get_recommended_metric_alarms ~26× (full AWS catalogue JSON each).
        # The CFN YAML was already dropped, but THIS nested fan-out was the
        # dominant term that pushed the saved turn past Memory's 100k limit.
        # Each reco/metric-read tool output must be abbreviated (gateway-
        # prefixed name, nested under the orchestrator <tool> segment).
        big = "x" * 6_000
        tool_data = {
            "name": "ops-excellence-agent",
            "output": "summary",
            "tool_trace": [
                {"tool_name": f"cloudwatch___get_recommended_metric_alarms",
                 "output": big, "status": "success"}
                for _ in range(26)
            ] + [
                {"tool_name": "cloudwatch___analyse_metric", "output": big},
                {"tool_name": "cloudwatch___get_metric_metadata", "output": big},
            ],
        }
        out = _abbreviate_bulky_tool_traces(tool_data)
        assert big not in json.dumps(out)
        for entry in out["tool_trace"]:
            assert entry["output"] == (
                "[tool output omitted from chat memory — see ReportPanel]"
            )

    def test_dx_topology_trace_is_preserved(self):
        # DX-topology tool output MUST survive — the visualizer rebuilds the
        # diagram from it on reload. Not in _NO_RELOAD_TRACE_TOOLS.
        topo = "x" * 80_000
        tool_data = {
            "name": "network-resiliency-agent",
            "input": {},
            "output": "summary",
            "tool_trace": [
                {"tool_name": "assess_dx_resiliency", "output": topo, "status": "success"},
            ],
        }
        out = _abbreviate_bulky_tool_traces(tool_data)
        assert out["tool_trace"][0]["output"] == topo

    def test_non_bulky_tool_untouched(self):
        tool_data = {"name": "get_cost_and_usage", "input": {}, "output": "x" * 50_000}
        out = _abbreviate_bulky_tool_traces(tool_data)
        assert out["output"] == "x" * 50_000

    def test_does_not_mutate_input(self):
        big = "x" * 100_000
        tool_data = {"name": "assemble_cfn_template", "output": big}
        _abbreviate_bulky_tool_traces(tool_data)
        # Original dict unchanged (shallow-copy semantics).
        assert tool_data["output"] == big

    def test_abbreviated_turn_stays_under_memory_limit(self):
        # End-to-end: after abbreviating the CFN trace, the whole turn stays
        # well under AgentCore's 100k-char Memory limit, so the report-pending
        # marker survives and the ReportCard renders on reload.
        from agents.shared.memory import build_enriched_text

        big = "x" * 120_000
        tool_data = _abbreviate_bulky_tool_traces({
            "name": "ops-excellence-agent",
            "input": {},
            "output": "sanitized",
            "tool_trace": [{"tool_name": "assemble_cfn_template", "output": big}],
        })
        segments = [
            {"type": "tool", "value": json.dumps(tool_data)},
            {"type": "text", "value": '<report-pending report_id="r" title="t"/>'},
        ]
        saved = build_enriched_text(segments)
        assert "<report-pending" in saved
        assert saved.count("<tool>") == saved.count("</tool>")
        assert len(saved) < 100_000


# ---------------------------------------------------------------------------
# 10. _handle_cfn_artifacts_from_tool_result — typed event-loop integration
# ---------------------------------------------------------------------------


class TestHandleCfnArtifactsFromToolResult:
    """The event-loop handler that persists extracted artifacts and emits
    <report-pending> events. Artifacts arrive via the module-level queue
    (populated by _delegate in registry.py), not from the event content."""

    def setup_method(self):
        """Clear the pending queue before each test."""
        import agents.shared.registry as reg
        reg._pending_cfn_artifacts.clear()

    def test_no_artifacts_returns_empty(self):
        from types import SimpleNamespace

        event = SimpleNamespace(type="TOOL_CALL_RESULT", content="{}")
        result = _handle_cfn_artifacts_from_tool_result(
            event, "actor", "table", "us-east-1"
        )
        assert result == []

    def test_artifacts_present_emits_report_pending(self):
        import agents.shared.registry as reg
        from types import SimpleNamespace

        reg._pending_cfn_artifacts.append(
            {
                "kind": "cloudformation-template",
                "title": "Test",
                "template_yaml": "AWSTemplateFormatVersion: '2010-09-09'",
                "summary": {"alarm_count": 1},
            }
        )
        event = SimpleNamespace(type="TOOL_CALL_RESULT", content="{}")

        with patch("agents.shared.agui_server.reports.save_report") as mock_save:
            mock_save.return_value = True
            events = _handle_cfn_artifacts_from_tool_result(
                event, "actor_id", "my-table", "us-east-1"
            )

        assert len(events) == 1
        delta = getattr(events[0], "delta", "")
        assert "<report-pending" in delta
        assert 'title="Test"' in delta
        # Queue is drained after consumption.
        assert get_pending_cfn_artifacts() == []

    def test_persistence_failure_returns_compact_retry_notice(self):
        import agents.shared.registry as reg
        from types import SimpleNamespace

        reg._pending_cfn_artifacts.append(
            {
                "kind": "cloudformation-template",
                "title": "X",
                "template_yaml": "Resources: {}",
                "summary": {},
            }
        )
        event = SimpleNamespace(type="TOOL_CALL_RESULT", content="{}")

        with patch("agents.shared.agui_server.reports.save_report") as mock_save:
            mock_save.return_value = False
            events = _handle_cfn_artifacts_from_tool_result(
                event, "actor_id", "my-table", "us-east-1"
            )

        assert len(events) == 1
        delta = getattr(events[0], "delta", "")
        assert "```yaml" not in delta
        assert "Resources: {}" not in delta
        assert "retry" in delta.lower()

    def test_dedup_set_skips_already_persisted_title(self):
        # When the title is already in the request-scoped persisted set
        # (e.g. the streaming interceptor persisted it first), the handler
        # must NOT persist a second row or emit a second marker.
        import agents.shared.registry as reg
        from types import SimpleNamespace

        reg._pending_cfn_artifacts.append(
            {
                "kind": "cloudformation-template",
                "title": "Dup",
                "template_yaml": "Resources: {}",
                "summary": {},
            }
        )
        event = SimpleNamespace(type="TOOL_CALL_RESULT", content="{}")
        seen = {"Dup"}

        with patch("agents.shared.agui_server.reports.save_report") as mock_save:
            mock_save.return_value = True
            events = _handle_cfn_artifacts_from_tool_result(
                event, "actor_id", "my-table", "us-east-1", seen
            )

        assert events == []
        mock_save.assert_not_called()

    def test_dedup_set_records_persisted_title(self):
        # On successful persist the handler records the title so a later
        # interceptor pass for the same artifact is skipped.
        import agents.shared.registry as reg
        from types import SimpleNamespace

        reg._pending_cfn_artifacts.append(
            {
                "kind": "cloudformation-template",
                "title": "Recorded",
                "template_yaml": "Resources: {}",
                "summary": {},
            }
        )
        event = SimpleNamespace(type="TOOL_CALL_RESULT", content="{}")
        seen: set[str] = set()

        with patch("agents.shared.agui_server.reports.save_report") as mock_save:
            mock_save.return_value = True
            _handle_cfn_artifacts_from_tool_result(
                event, "actor_id", "my-table", "us-east-1", seen
            )

        assert "Recorded" in seen


# ---------------------------------------------------------------------------
# 12. _ReportPendingStripper — drop model-typed <report-pending> markers
#
# Regression for the live tune-follow-up bug: the model mimicked a prior
# turn's platform-injected <report-pending> marker and typed its own with a
# fabricated id (rpt-cfn-tuned-a7b92c01), rendering a ReportCard that polled
# a non-existent DynamoDB row. The stripper removes any model-origin marker;
# the platform's own injected markers bypass this filter entirely.
# ---------------------------------------------------------------------------


def _strip_all(deltas: list[str]) -> str:
    """Feed deltas through a fresh stripper, append flush, return the text."""
    stripper = _ReportPendingStripper()
    out = [stripper.feed(d) for d in deltas]
    out.append(stripper.flush())
    return "".join(out)


class TestReportPendingStripper:
    def test_plain_text_unchanged(self):
        assert _strip_all(["Here are ", "your alarms."]) == "Here are your alarms."

    def test_self_closing_marker_removed(self):
        text = (
            'Tuned alarms below.'
            '<report-pending report_id="rpt-cfn-tuned-a7b92c01" '
            'title="CloudWatch Alarms Tuned"/>'
            ' Apply when ready.'
        )
        assert _strip_all([text]) == "Tuned alarms below. Apply when ready."

    def test_marker_at_start_removed(self):
        text = (
            '<report-pending report_id="rpt-cfn-tuned-a7b92c01" title="T"/>\n\n'
            "Here's the tuned configuration:"
        )
        assert _strip_all([text]) == "\n\nHere's the tuned configuration:"

    def test_marker_split_across_deltas(self):
        deltas = [
            "Done. <report-pending ",
            'report_id="rpt-cfn-tuned-a7b92c01" ',
            'title="T"',
            "/> Next steps.",
        ]
        assert _strip_all(deltas) == "Done.  Next steps."

    def test_literal_prefix_split_across_deltas(self):
        # The "<report-p" prefix straddles the boundary then completes.
        deltas = ["See report. <report-p", 'ending report_id="x"/> done']
        assert _strip_all(deltas) == "See report.  done"

    def test_partial_prefix_that_is_not_a_marker_is_released(self):
        # A trailing "<report" that turns out to be ordinary text must not be
        # swallowed — flush releases it.
        assert _strip_all(["text ends with <report"]) == "text ends with <report"

    def test_two_markers_removed(self):
        text = (
            '<report-pending report_id="a" title="T"/>'
            "middle"
            '<report-pending report_id="b" title="T"/>end'
        )
        assert _strip_all([text]) == "middleend"

    def test_unrelated_tags_pass_through(self):
        # <report-body> and <report-status> share a prefix with the literal
        # but are not <report-pending> — they must pass through untouched.
        text = "<report-body>x</report-body><report-status>ok</report-status>"
        assert _strip_all([text]) == text

    def test_unterminated_marker_is_dropped_on_flush(self):
        # A marker whose closing '>' never arrives is discarded (it was a
        # malformed marker, not content worth showing).
        assert _strip_all(['junk <report-pending report_id="x"']) == "junk "
