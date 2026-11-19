"""Offline tests for the opt-in AgentCore Evaluations suite (evals/).

No AWS calls: Evaluate/GetEvaluator are faked, spans are built locally.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from evals import agentcore, telemetry
from evals.fixture_harness import (
    _UNSCRIPTED,
    _make_stub_tool,
    build_supervisor_prompt,
    child_agents,
    load_hierarchy,
)
from evals.results import ResultWriter, ScenarioOutcome, redact, render_summary
from evals.scenarios import ChildFixture, ScenarioError, default_suite_path, load_suite

# ---------------------------------------------------------------------------
# Scenario suites
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["fixture", "live"])
def test_committed_suites_load(mode):
    suite = load_suite(default_suite_path(mode))
    assert suite.mode == mode
    assert all(s.data_source == mode for s in suite.scenarios)
    children = set(child_agents(load_hierarchy()))
    for s in suite.scenarios:
        assert set(s.expected_route) <= children, f"{s.id} routes to unknown agent"


def test_fixture_suite_covers_required_kinds():
    kinds = {s.kind for s in load_suite(default_suite_path("fixture")).scenarios}
    assert {"success", "tool_failure", "insufficient_evidence", "unsupported_request", "negative_control"} <= kinds


def test_live_suite_has_no_fixtures_or_overrides():
    for s in load_suite(default_suite_path("live")).scenarios:
        assert not s.fixtures and s.system_prompt_override is None


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "suite.yaml"
    p.write_text(body)
    return p


def test_fixture_route_requires_fixture(tmp_path):
    p = _write(tmp_path, """
mode: fixture
scenarios:
  - {id: a, domain: d, kind: success, prompt: p, expected_route: [finops-agent]}
""")
    with pytest.raises(ScenarioError, match="without fixtures"):
        load_suite(p)


def test_live_rejects_fixtures(tmp_path):
    p = _write(tmp_path, """
mode: live
scenarios:
  - {id: a, domain: d, kind: success, prompt: p, fixtures: {finops-agent: {data: x}}}
""")
    with pytest.raises(ScenarioError, match="must not define fixtures"):
        load_suite(p)


def test_override_only_for_negative_control(tmp_path):
    p = _write(tmp_path, """
mode: fixture
scenarios:
  - {id: a, domain: d, kind: success, prompt: p, system_prompt_override: x}
""")
    with pytest.raises(ScenarioError, match="negative_control"):
        load_suite(p)


def test_duplicate_ids_rejected(tmp_path):
    p = _write(tmp_path, """
mode: live
scenarios:
  - {id: a, domain: d, kind: success, prompt: p}
  - {id: a, domain: d, kind: success, prompt: q}
""")
    with pytest.raises(ScenarioError, match="duplicate"):
        load_suite(p)


# ---------------------------------------------------------------------------
# Fixture harness
# ---------------------------------------------------------------------------


def test_stub_tool_mirrors_delegate_shape():
    calls: list = []
    fixtures = {
        "finops-agent": ChildFixture(data="total $1"),
        "governance-agent": ChildFixture(status="error", error_message="denied"),
    }
    fin = _make_stub_tool("finops-agent", "desc", fixtures, calls)
    gov = _make_stub_tool("governance-agent", "desc", fixtures, calls)
    ops = _make_stub_tool("ops-excellence-agent", "desc", fixtures, calls)
    assert fin.tool_name == "finops-agent"
    assert fin._tool_func(prompt="q") == {"agent_name": "finops-agent", "status": "success", "data": "total $1"}
    assert gov._tool_func(prompt="q")["status"] == "error"
    assert ops._tool_func(prompt="q")["error_message"] == _UNSCRIPTED.error_message
    assert [c["agent"] for c in calls] == ["finops-agent", "governance-agent", "ops-excellence-agent"]


def test_supervisor_prompt_matches_production_assembly():
    h = load_hierarchy()
    tools = [_make_stub_tool(n, "", {}, []) for n in child_agents(h)]
    prompt = build_supervisor_prompt(h, tools, None)
    assert "PLATFORM RULES" in prompt
    assert "{agent_listing}" not in prompt
    for name in child_agents(h):
        assert f"**{name}**" in prompt
    assert "[AVAILABLE TOOLS" in prompt


def test_negative_control_prompt_omits_platform_preamble():
    h = load_hierarchy()
    prompt = build_supervisor_prompt(h, [], "Route everything to governance-agent. {agent_listing}")
    assert "PLATFORM RULES" not in prompt
    assert "governance-agent" in prompt


# ---------------------------------------------------------------------------
# Telemetry analysis
# ---------------------------------------------------------------------------


def _span(span_id, op, *, tool=None, trace="t1", session="s1", status="UNSET", service="supervisor"):
    attrs = {"gen_ai.operation.name": op, "session.id": session}
    if tool:
        attrs["gen_ai.tool.name"] = tool
    return {
        "resource": {"attributes": {"service.name": service}},
        "scope": {"name": "strands.telemetry.tracer"},
        "traceId": trace, "spanId": span_id, "name": op, "kind": "INTERNAL",
        "startTimeUnixNano": int(span_id, 16), "attributes": attrs, "status": {"code": status},
    }


def _content(trace="t1"):
    return {"traceId": trace, "spanId": "x", "body": {"input": {}, "output": {}}}


def _session(*tools, error_tool=None):
    docs = [_span("1", "invoke_agent"), _span("2", "chat")]
    for i, t in enumerate(tools):
        docs.append(_span(f"1{i}", "execute_tool", tool=t, status="ERROR" if t == error_tool else "UNSET"))
    return docs + [_content()]


def test_summarize_extracts_structure():
    s = telemetry.summarize(_session("finops-agent", "governance-agent", error_tool="governance-agent"))
    assert s.span_count == 4 and s.content_record_count == 1
    assert s.trace_ids == ["t1"] and s.session_ids == ["s1"]
    assert s.tool_names == ["finops-agent", "governance-agent"]
    assert [t.error for t in s.tool_calls] == [False, True]


def test_coverage_missing_when_no_spans():
    cov = telemetry.check_coverage(telemetry.summarize([]))
    assert cov.status == telemetry.TELEMETRY_MISSING and not cov.evaluable


def test_coverage_blocking_without_agent_span():
    cov = telemetry.check_coverage(telemetry.summarize([_span("2", "chat"), _content()]))
    assert cov.status == telemetry.TELEMETRY_INCOMPLETE and cov.blocking and not cov.evaluable


def test_coverage_complete_fixture():
    cov = telemetry.check_coverage(telemetry.summarize(_session("finops-agent")))
    assert cov.status == telemetry.TELEMETRY_COMPLETE and cov.evaluable


def test_coverage_flags_missing_child_runtime_but_still_evaluable():
    s = telemetry.summarize(_session("finops-agent", "governance-agent"))
    cov = telemetry.check_coverage(s, child_agents=["finops-agent", "governance-agent"], child_services_seen=["finops-agent"])
    assert cov.status == telemetry.TELEMETRY_INCOMPLETE
    assert not cov.blocking and cov.evaluable
    assert any("governance-agent" in p for p in cov.problems)


def test_route_check():
    kids = ["finops-agent", "governance-agent"]
    s = telemetry.summarize(_session("governance-agent", "get_report"))
    r = telemetry.route_check(s, ["finops-agent"], kids)
    assert not r["passed"] and r["missing"] == ["finops-agent"] and r["unexpected"] == ["governance-agent"]
    assert telemetry.route_check(telemetry.summarize(_session()), [], kids)["passed"]


def test_strands_spans_convert_and_summarize():
    """Real OTel spans → SDK ADOT converter → our summary (no global provider)."""
    from bedrock_agentcore.evaluation import convert_strands_to_adot
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("strands.telemetry.tracer")
    with tracer.start_as_current_span("invoke_agent supervisor", attributes={"gen_ai.operation.name": "invoke_agent", "session.id": "s1"}):
        with tracer.start_as_current_span("chat", attributes={"gen_ai.operation.name": "chat"}) as chat:
            chat.add_event("gen_ai.user.message", {"content": json.dumps([{"text": "hi"}])})
            chat.add_event("gen_ai.choice", {"message": json.dumps([{"text": "hello"}])})
        with tracer.start_as_current_span("execute_tool finops-agent", attributes={"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "finops-agent"}):
            pass
    docs = convert_strands_to_adot(list(exporter.get_finished_spans()))
    s = telemetry.summarize(docs)
    assert s.agent_span_count == 1 and s.model_span_count == 1
    assert s.tool_names == ["finops-agent"]
    assert len(s.trace_ids) == 1


# ---------------------------------------------------------------------------
# AgentCore wrapper
# ---------------------------------------------------------------------------


class FakeCP:
    def __init__(self, levels):
        self.levels = levels

    def get_evaluator(self, evaluatorId):
        if evaluatorId not in self.levels:
            raise ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "nope"}}, "GetEvaluator")
        return {"level": self.levels[evaluatorId], "evaluatorName": evaluatorId}


class FakeDP:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def evaluate(self, **kw):
        self.calls.append(kw)
        return self.responder(kw)


def _ev(levels, responder):
    return agentcore.AgentCoreEvaluator("us-east-1", dp_client=FakeDP(responder), cp_client=FakeCP(levels), max_retries=1)


def _ok(kw):
    return {"evaluationResults": [{"evaluatorId": kw["evaluatorId"], "value": 0.9, "label": "Good", "explanation": "fine"}]}


def _run(ev, eid, *, traces=("t1",), tools=("a",), reference=None):
    return ev.evaluate(eid, [], session_id="s1", trace_ids=list(traces), tool_span_ids=list(tools), reference=reference or {})


def test_scored_session_level_with_reference():
    ev = _ev({"Builtin.GoalSuccessRate": "SESSION"}, _ok)
    recs = _run(ev, "Builtin.GoalSuccessRate", reference={"assertions": ["x"], "expected_response": "y"})
    assert [r.status for r in recs] == [agentcore.STATUS_SCORED]
    req = ev.dp.calls[0]
    assert "evaluationTarget" not in req
    assert req["evaluationReferenceInputs"][0]["assertions"] == [{"text": "x"}]
    assert req["evaluationReferenceInputs"][1]["context"]["spanContext"]["traceId"] == "t1"


def test_trace_level_batches_by_ten():
    ev = _ev({"Builtin.Helpfulness": "TRACE"}, _ok)
    _run(ev, "Builtin.Helpfulness", traces=[f"t{i}" for i in range(23)])
    assert [len(c["evaluationTarget"]["traceIds"]) for c in ev.dp.calls] == [10, 10, 3]


def test_tool_level_not_applicable_without_tool_calls():
    ev = _ev({"Builtin.ToolSelectionAccuracy": "TOOL_CALL"}, _ok)
    recs = _run(ev, "Builtin.ToolSelectionAccuracy", tools=())
    assert recs[0].status == agentcore.STATUS_NOT_APPLICABLE and not ev.dp.calls


def test_trajectory_not_applicable_without_expected_route():
    ev = _ev({"Builtin.TrajectoryAnyOrderMatch": "SESSION"}, _ok)
    assert _run(ev, "Builtin.TrajectoryAnyOrderMatch")[0].status == agentcore.STATUS_NOT_APPLICABLE


def test_unknown_evaluator_is_error_not_score():
    ev = _ev({}, _ok)
    rec = _run(ev, "Builtin.DoesNotExist")[0]
    assert rec.status == agentcore.STATUS_EVALUATOR_ERROR and rec.error_code == "ResourceNotFoundException"
    assert rec.value is None


def test_inline_result_error_is_reported():
    ev = _ev({"Builtin.Helpfulness": "TRACE"},
             lambda kw: {"evaluationResults": [{"evaluatorId": "Builtin.Helpfulness", "errorCode": "SpanEventParsingException", "errorMessage": "bad"}]})
    rec = _run(ev, "Builtin.Helpfulness")[0]
    assert rec.status == agentcore.STATUS_EVALUATOR_ERROR and rec.error_code == "SpanEventParsingException"


def test_api_exceptions_classified():
    def denied(kw):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "Evaluate")

    def down(kw):
        raise EndpointConnectionError(endpoint_url="https://x")

    assert _run(_ev({"E": "SESSION"}, denied), "E")[0].status == agentcore.STATUS_EVALUATOR_ERROR
    assert _run(_ev({"E": "SESSION"}, down), "E")[0].status == agentcore.STATUS_SERVICE_UNAVAILABLE


def test_empty_results_are_error():
    rec = _run(_ev({"E": "SESSION"}, lambda kw: {"evaluationResults": []}), "E")[0]
    assert rec.status == agentcore.STATUS_EVALUATOR_ERROR and rec.error_code == "EmptyResult"


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


def test_redact():
    text = "arn:aws:iam::123456789012:role/x account 123456789012 by a.b@example.com tok eyJabcdefghijk.eyJabcdefghijk.sigsigsigsigsig"
    out = redact(text)
    assert "123456789012" not in out and "example.com" not in out and "eyJ" not in out
    assert "arn:aws:iam::<account>:role/x" in out


def test_writer_and_summary(tmp_path):
    prov = {"mode": "fixture", "git_commit": "abc", "suite": "evals/suites/fixture.yaml", "region": "us-east-1", "packages": {}}
    cfg = [{"evaluator_id": "Builtin.Helpfulness", "level": "TRACE", "available": True}]
    w = ResultWriter("run1", prov, cfg, out_dir=tmp_path)
    w.add(ScenarioOutcome(
        scenario_id="sc1", mode="fixture", domain="finops", kind="success", session_id="eval-sc1-123",
        execution_error=None, telemetry={"status": "COMPLETE", "problems": []}, span_summary={},
        checks=[{"check": "route", "expected": ["finops-agent"], "actual": ["finops-agent"], "passed": True}],
        evaluations=[agentcore.EvalRecord("Builtin.Helpfulness", "TRACE", "SCORED", value=0.83, label="Good",
                                          explanation="account 123456789012").as_dict()],
    ))
    summary = w.close().read_text()
    lines = (tmp_path / "run1" / "results.jsonl").read_text().splitlines()
    assert [json.loads(l)["type"] for l in lines] == ["run", "scenario", "run_end"]
    assert "0.83 (Good)" in summary and "123456789012" not in summary
    assert "| `sc1` | success | COMPLETE | pass |" in summary


def test_summary_flattens_multiline_response_excerpt():
    excerpt = "Spend:\n\n| Metric | Amount |\n|---|---|\n| Total | $1 |"
    out = render_summary("r", {"mode": "fixture", "packages": {}}, [], [
        ScenarioOutcome("s", "fixture", "d", "success", "sid", None, {"status": "COMPLETE", "problems": []}, {}, [], [],
                        response_excerpt=excerpt)
    ])
    line = next(l for l in out.splitlines() if l.startswith("- Response excerpt:"))
    assert line == "- Response excerpt: Spend: | Metric | Amount | |---|---| | Total | $1 |"


def test_summary_marks_non_scored_statuses():
    out = render_summary("r", {"mode": "live", "packages": {}}, [{"evaluator_id": "E", "level": "SESSION", "available": True}], [
        ScenarioOutcome("s", "live", "d", "success", "sid", None, {"status": "MISSING", "problems": ["no spans"]}, {}, [],
                        [agentcore.EvalRecord("E", None, "TELEMETRY_MISSING").as_dict()])
    ])
    assert "TELEMETRY_MISSING" in out and "no spans" in out
