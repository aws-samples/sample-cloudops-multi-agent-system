"""Span capture and trace-coverage checks.

Works on ADOT-format documents (the shape AgentCore Evaluations consumes),
whether they came from the in-memory exporter in fixture mode or from
CloudWatch Logs in live mode. Coverage problems are reported as telemetry
status, never folded into quality scores.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

TELEMETRY_COMPLETE = "COMPLETE"
TELEMETRY_INCOMPLETE = "INCOMPLETE"
TELEMETRY_MISSING = "MISSING"


# ---------------------------------------------------------------------------
# In-memory capture (fixture mode)
# ---------------------------------------------------------------------------


class InMemoryCapture:
    """Global OTel tracer provider that keeps finished spans in memory.

    Must be installed before any Strands ``Agent`` is constructed, because
    Strands resolves its tracer from the global provider.
    """

    def __init__(self, service_name: str = "supervisor") -> None:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        self.exporter = InMemorySpanExporter()
        provider = TracerProvider(
            resource=Resource.create(
                {"service.name": service_name, "deployment.environment": "eval-fixture"}
            )
        )
        provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        trace.set_tracer_provider(provider)
        actual = trace.get_tracer_provider()
        if actual is not provider:
            raise RuntimeError(
                "A global OpenTelemetry tracer provider was already installed "
                "(is opentelemetry-instrument wrapping this process?). Fixture "
                "mode needs its own in-memory provider."
            )
        self.provider = provider

    def drain_adot(self) -> list[dict[str, Any]]:
        """Return captured spans converted to ADOT documents, then clear."""
        from bedrock_agentcore.evaluation import convert_strands_to_adot

        self.provider.force_flush()
        raw = list(self.exporter.get_finished_spans())
        self.exporter.clear()
        return convert_strands_to_adot(raw)


# ---------------------------------------------------------------------------
# Span analysis (both modes)
# ---------------------------------------------------------------------------


def is_span_document(doc: dict[str, Any]) -> bool:
    return isinstance(doc, dict) and "spanId" in doc and "startTimeUnixNano" in doc


def _attrs(doc: dict[str, Any]) -> dict[str, Any]:
    attrs = doc.get("attributes")
    return attrs if isinstance(attrs, dict) else {}


def _service(doc: dict[str, Any]) -> str:
    resource = doc.get("resource") or {}
    attrs = resource.get("attributes") if isinstance(resource, dict) else None
    if isinstance(attrs, dict):
        return str(attrs.get("service.name", ""))
    return ""


@dataclass(frozen=True)
class ToolCall:
    name: str
    span_id: str
    trace_id: str
    service: str
    error: bool


@dataclass
class SpanSummary:
    span_count: int = 0
    content_record_count: int = 0
    trace_ids: list[str] = field(default_factory=list)
    session_ids: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    agent_span_count: int = 0
    model_span_count: int = 0
    tool_calls: list[ToolCall] = field(default_factory=list)

    @property
    def tool_names(self) -> list[str]:
        return [t.name for t in self.tool_calls]

    def as_dict(self) -> dict[str, Any]:
        return {
            "span_count": self.span_count,
            "content_record_count": self.content_record_count,
            "trace_ids": self.trace_ids,
            "session_ids": self.session_ids,
            "services": self.services,
            "agent_span_count": self.agent_span_count,
            "model_span_count": self.model_span_count,
            "tool_calls": [
                {"name": t.name, "service": t.service, "error": t.error, "span_id": t.span_id}
                for t in self.tool_calls
            ],
        }


def summarize(docs: Iterable[dict[str, Any]]) -> SpanSummary:
    summary = SpanSummary()
    traces: dict[str, None] = {}
    sessions: dict[str, None] = {}
    services: dict[str, None] = {}
    spans = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        if is_span_document(doc):
            spans.append(doc)
        elif "body" in doc:
            summary.content_record_count += 1

    spans.sort(key=lambda d: d.get("startTimeUnixNano") or 0)
    for doc in spans:
        summary.span_count += 1
        attrs = _attrs(doc)
        if doc.get("traceId"):
            traces[doc["traceId"]] = None
        sid = attrs.get("session.id")
        if sid:
            sessions[str(sid)] = None
        svc = _service(doc)
        if svc:
            services[svc] = None
        op = attrs.get("gen_ai.operation.name")
        if op == "invoke_agent":
            summary.agent_span_count += 1
        elif op == "chat":
            summary.model_span_count += 1
        elif op == "execute_tool":
            status = doc.get("status") or {}
            summary.tool_calls.append(
                ToolCall(
                    name=str(attrs.get("gen_ai.tool.name", doc.get("name", ""))),
                    span_id=str(doc.get("spanId", "")),
                    trace_id=str(doc.get("traceId", "")),
                    service=svc,
                    error=str(status.get("code", "")).upper() == "ERROR",
                )
            )
    summary.trace_ids = list(traces)
    summary.session_ids = list(sessions)
    summary.services = list(services)
    return summary


@dataclass(frozen=True)
class CoverageReport:
    """Telemetry verdict for one session.

    ``blocking`` problems mean the session cannot be meaningfully evaluated
    (no agent span, no model span, no content). Non-blocking problems — e.g.
    a child runtime's spans not found in the same trace — still allow the
    supervisor session to be scored, but the result is flagged INCOMPLETE so
    it is never mistaken for full multi-agent coverage.
    """

    status: str
    problems: tuple[str, ...] = ()
    blocking: bool = False

    @property
    def evaluable(self) -> bool:
        return self.status != TELEMETRY_MISSING and not self.blocking

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "problems": list(self.problems), "blocking": self.blocking}


def check_coverage(
    summary: SpanSummary,
    *,
    child_agents: Iterable[str] = (),
    child_services_seen: Iterable[str] | None = None,
) -> CoverageReport:
    """Decide whether the captured telemetry is complete enough to evaluate.

    Args:
        summary: Spans for the session being evaluated.
        child_agents: Names of all child-agent tools the agent could call.
        child_services_seen: Live mode only. ``service.name`` values found
            for the same trace(s) in child runtimes' telemetry. When given,
            every child agent the supervisor actually delegated to must
            appear here, otherwise the multi-agent trace is incomplete.
    """
    if summary.span_count == 0:
        return CoverageReport(TELEMETRY_MISSING, ("no spans found for session",), blocking=True)

    problems: list[str] = []
    if summary.agent_span_count == 0:
        problems.append("no invoke_agent span (AgentCore Evaluations skips such sessions)")
    if summary.model_span_count == 0:
        problems.append("no model (chat) span")
    if summary.content_record_count == 0:
        problems.append("no conversation/tool content records (prompts and responses not captured)")
    if len(summary.session_ids) > 1:
        problems.append(f"spans span multiple session ids: {len(summary.session_ids)}")
    blocking = bool(problems)

    if child_services_seen is not None:
        seen = set(child_services_seen)
        children = set(child_agents)
        delegated = [t.name for t in summary.tool_calls if t.name in children]
        for name in dict.fromkeys(delegated):
            if name not in seen:
                problems.append(f"delegated to '{name}' but no spans from that runtime in the same trace")

    if problems:
        return CoverageReport(TELEMETRY_INCOMPLETE, tuple(problems), blocking=blocking)
    return CoverageReport(TELEMETRY_COMPLETE)


def route_check(summary: SpanSummary, expected_route: Iterable[str], child_agents: Iterable[str]) -> dict[str, Any]:
    """Deterministic delegation check: did the agent call the expected children?

    Order-insensitive set comparison over child-agent tool calls only; other
    tools (e.g. ``get_report``) are ignored. This is a free, repeatable signal
    that complements the LLM-judged trajectory/tool-selection scores.
    """
    children = set(child_agents)
    actual = [n for n in summary.tool_names if n in children]
    expected = list(expected_route)
    missing = [a for a in expected if a not in actual]
    unexpected = sorted({a for a in actual if a not in expected})
    return {
        "check": "route",
        "expected": expected,
        "actual": actual,
        "missing": missing,
        "unexpected": unexpected,
        "passed": not missing and not unexpected,
    }
