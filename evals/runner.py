"""CLI entry point: ``python -m evals.runner --mode fixture|live``.

Every non-``--dry-run`` invocation calls paid services (Bedrock models and
AgentCore Evaluations; live mode also AgentCore Runtime and CloudWatch Logs
Insights). Live mode additionally requires ``EVAL_LIVE=1``.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from evals import telemetry
from evals.agentcore import STATUS_TELEMETRY_MISSING, AgentCoreEvaluator, EvalRecord
from evals.results import ResultWriter, ScenarioOutcome, provenance
from evals.scenarios import Scenario, Suite, default_suite_path, load_suite

logger = logging.getLogger("evals")

# Delegation is a tool call in this system (each child agent is a Strands
# tool), so tool-level and trajectory evaluators measure routing too.
FALLBACK_EVALUATORS = (
    "Builtin.GoalSuccessRate",
    "Builtin.TrajectoryAnyOrderMatch",
    "Builtin.ToolSelectionAccuracy",
    "Builtin.Helpfulness",
    "Builtin.Correctness",
    "Builtin.Faithfulness",
)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m evals.runner", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", choices=("fixture", "live"), required=True)
    p.add_argument("--suite", type=Path, help="Suite YAML (default: evals/suites/<mode>.yaml)")
    p.add_argument("--scenario", action="append", default=[], help="Only run these scenario ids (repeatable)")
    p.add_argument("--evaluator", action="append", default=[], help="Override evaluator ids (repeatable)")
    p.add_argument("--model", help="Fixture mode: override the supervisor model id from hierarchy.json")
    p.add_argument("--region", default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1")
    p.add_argument("--out-dir", type=Path, help="Results root (default: evals/results)")
    p.add_argument("--max-wait", type=int, default=600, help="Live mode: seconds to wait for span ingestion")
    p.add_argument("--dry-run", action="store_true", help="Validate the suite and print the plan; no AWS calls")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def _select(suite: Suite, ids: list[str]) -> list[Scenario]:
    if not ids:
        return list(suite.scenarios)
    known = {s.id for s in suite.scenarios}
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise SystemExit(f"Unknown scenario id(s): {unknown}")
    return [s for s in suite.scenarios if s.id in ids]


def _evaluators_for(scenario: Scenario, suite: Suite, override: list[str]) -> list[str]:
    return list(override or scenario.evaluators or suite.default_evaluators or FALLBACK_EVALUATORS)


def _reference(scenario: Scenario) -> dict:
    return {
        "assertions": list(scenario.assertions),
        "expected_trajectory": list(scenario.expected_route),
        "expected_response": scenario.expected_response,
    }


def _evaluate_session(
    ev: AgentCoreEvaluator,
    scenario: Scenario,
    evaluator_ids: list[str],
    spans: list[dict],
    summary: telemetry.SpanSummary,
    coverage: telemetry.CoverageReport,
    session_id: str,
    execution_error: str | None,
) -> list[dict]:
    if execution_error or not coverage.evaluable:
        reason = execution_error or "; ".join(coverage.problems)
        status = STATUS_TELEMETRY_MISSING if not execution_error else "SKIPPED_EXECUTION_ERROR"
        return [EvalRecord(e, None, status, error_message=reason).as_dict() for e in evaluator_ids]
    out: list[dict] = []
    tool_span_ids = [t.span_id for t in summary.tool_calls if t.span_id]
    for evaluator_id in evaluator_ids:
        logger.info("  %s", evaluator_id)
        for rec in ev.evaluate(
            evaluator_id,
            spans,
            session_id=session_id,
            trace_ids=summary.trace_ids,
            tool_span_ids=tool_span_ids,
            reference=_reference(scenario),
        ):
            out.append(rec.as_dict())
    return out


def _run_fixture(scenarios, suite, args, ev, writer, child_names) -> None:
    from evals.fixture_harness import run_fixture_scenario

    capture = telemetry.InMemoryCapture(service_name="supervisor")
    for sc in scenarios:
        logger.info("[fixture] %s", sc.id)
        run = run_fixture_scenario(sc, capture, model_id=args.model, region=args.region)
        summary = telemetry.summarize(run.spans)
        coverage = telemetry.check_coverage(summary)
        checks = [telemetry.route_check(summary, sc.expected_route, child_names)]
        evaluations = _evaluate_session(ev, sc, _evaluators_for(sc, suite, args.evaluator), run.spans,
                                        summary, coverage, run.session_id, run.error)
        writer.add(ScenarioOutcome(
            scenario_id=sc.id, mode="fixture", domain=sc.domain, kind=sc.kind,
            session_id=run.session_id, execution_error=run.error,
            telemetry=coverage.as_dict(), span_summary=summary.as_dict(), checks=checks,
            evaluations=evaluations, expected_outcome=sc.expected_outcome,
            response_excerpt=run.response_text[:300],
            extra={"model_id": run.model_id, "system_prompt_sha256": run.system_prompt_sha256,
                   "delegations": run.delegations, "negative_control": sc.system_prompt_override is not None},
        ))


def _run_live(scenarios, suite, args, ev, writer, child_names) -> None:
    from evals import live_harness

    dep = live_harness.discover_deployment()
    token = live_harness.cognito_token(dep)
    for sc in scenarios:
        logger.info("[live] %s", sc.id)
        run = live_harness.invoke(dep, token, sc)
        spans: list[dict] = []
        if not run.error:
            logger.info("  waiting for span ingestion (up to %ss)…", args.max_wait)
            spans = live_harness.collect_session_spans(dep, run, max_wait=args.max_wait)
        summary = telemetry.summarize(spans)
        raw_services = live_harness.child_services_in_traces(dep, summary.trace_ids, run)
        child_seen = live_harness.normalize_child_services(raw_services, child_names)
        coverage = telemetry.check_coverage(summary, child_agents=child_names, child_services_seen=child_seen)
        checks = [telemetry.route_check(summary, sc.expected_route, child_names)]
        evaluations = _evaluate_session(ev, sc, _evaluators_for(sc, suite, args.evaluator), spans,
                                        summary, coverage, run.session_id, run.error)
        writer.add(ScenarioOutcome(
            scenario_id=sc.id, mode="live", domain=sc.domain, kind=sc.kind,
            session_id=run.session_id, execution_error=run.error,
            telemetry=coverage.as_dict(), span_summary=summary.as_dict(), checks=checks,
            evaluations=evaluations, expected_outcome=sc.expected_outcome,
            response_excerpt=run.response_text[:300],
            extra={"child_services_in_trace": child_seen, "sse_tool_calls": run.sse_tool_calls,
                   "duration_s": (run.finished_at - run.started_at).total_seconds()},
        ))


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("botocore", "urllib3", "httpx", "strands", "opentelemetry"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    suite = load_suite(args.suite or default_suite_path(args.mode))
    if suite.mode != args.mode:
        raise SystemExit(f"Suite {suite.path} is a '{suite.mode}' suite, not '{args.mode}'")
    scenarios = _select(suite, args.scenario)
    all_evaluators = list(dict.fromkeys(e for s in scenarios for e in _evaluators_for(s, suite, args.evaluator)))

    print(f"Suite {suite.path.name}: {len(scenarios)} scenario(s), evaluators: {', '.join(all_evaluators)}")
    if args.dry_run:
        for s in scenarios:
            print(f"  - {s.id} [{s.domain}/{s.kind}] route={list(s.expected_route) or '(none)'}")
        return 0
    if args.mode == "live" and os.environ.get("EVAL_LIVE") != "1":
        raise SystemExit("Live mode invokes the deployed stack and incurs charges; set EVAL_LIVE=1 to proceed.")

    from evals.fixture_harness import child_agents, load_hierarchy

    child_names = child_agents(load_hierarchy())
    ev = AgentCoreEvaluator(region=args.region)
    infos = [ev.describe(e) for e in all_evaluators]
    evaluator_config = [
        {"evaluator_id": i.evaluator_id, "level": i.level, "available": i.available,
         "name": i.name, "provider": i.provider, "error": i.error}
        for i in infos
    ]
    for i in infos:
        print(f"  {i.evaluator_id}: {i.level if i.available else 'UNAVAILABLE ' + i.error}")
    if not any(i.available for i in infos):
        print("No evaluator is available in this region/account; aborting before invoking agents.", file=sys.stderr)
        return 2

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{args.mode}"
    prov = provenance(mode=args.mode, region=args.region, suite_path=suite.path,
                      suite_sha=suite.sha256, model_override=args.model)
    writer = ResultWriter(run_id, prov, evaluator_config, out_dir=args.out_dir)
    try:
        if args.mode == "fixture":
            _run_fixture(scenarios, suite, args, ev, writer, child_names)
        else:
            _run_live(scenarios, suite, args, ev, writer, child_names)
    finally:
        summary_path = writer.close()
    print(f"\nResults: {writer.dir / 'results.jsonl'}\nSummary: {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
