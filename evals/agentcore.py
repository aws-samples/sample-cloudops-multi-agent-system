"""Thin, failure-explicit wrapper around the AgentCore Evaluations API.

``bedrock_agentcore.evaluation.EvaluationClient.run`` is convenient but hides
the cases this suite must report distinctly: it returns ``[]`` when no spans
are found, silently treats an evaluator whose level lookup fails as
SESSION-level, and raises on the first API error. Here every evaluator call
produces explicit records with one of the statuses below.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
    UnknownServiceError,
)

logger = logging.getLogger(__name__)

STATUS_SCORED = "SCORED"
STATUS_EVALUATOR_ERROR = "EVALUATOR_ERROR"
STATUS_SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
STATUS_TELEMETRY_MISSING = "TELEMETRY_MISSING"
STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"

MAX_TARGET_IDS_PER_REQUEST = 10  # Evaluate returns at most 10 results per call

# Evaluators that need an expected trajectory and are meaningless without one.
TRAJECTORY_EVALUATORS = {
    "Builtin.TrajectoryExactOrderMatch",
    "Builtin.TrajectoryInOrderMatch",
    "Builtin.TrajectoryAnyOrderMatch",
}

_UNAVAILABLE_CODES = {"InternalServerException", "ServiceUnavailableException", "UnknownOperationException"}
_RETRYABLE_CODES = {"ThrottlingException", "InternalServerException"}


@dataclass(frozen=True)
class EvaluatorInfo:
    evaluator_id: str
    level: str | None
    available: bool
    name: str = ""
    provider: str = ""
    error: str = ""


@dataclass
class EvalRecord:
    evaluator_id: str
    level: str | None
    status: str
    value: float | None = None
    label: str | None = None
    explanation: str | None = None
    dimension: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    token_usage: dict[str, Any] | None = None
    ignored_reference_fields: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _client_error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", "ClientError"))


def classify_exception(exc: BaseException) -> tuple[str, str, str]:
    """Map an exception to (status, error_code, message)."""
    if isinstance(exc, ClientError):
        code = _client_error_code(exc)
        msg = str(exc.response.get("Error", {}).get("Message", exc))
        status = STATUS_SERVICE_UNAVAILABLE if code in _UNAVAILABLE_CODES else STATUS_EVALUATOR_ERROR
        return status, code, msg
    if isinstance(exc, (EndpointConnectionError, UnknownServiceError)):
        return STATUS_SERVICE_UNAVAILABLE, type(exc).__name__, str(exc)
    if isinstance(exc, NoCredentialsError):
        return STATUS_EVALUATOR_ERROR, "NoCredentials", str(exc)
    return STATUS_EVALUATOR_ERROR, type(exc).__name__, str(exc)


class AgentCoreEvaluator:
    """Calls ``Evaluate`` once per evaluator and target batch."""

    def __init__(self, region: str, dp_client: Any = None, cp_client: Any = None, max_retries: int = 3):
        import boto3
        from botocore.config import Config

        cfg = Config(retries={"max_attempts": 1}, read_timeout=300)
        self.region = region
        self.dp = dp_client or boto3.client("bedrock-agentcore", region_name=region, config=cfg)
        self.cp = cp_client or boto3.client("bedrock-agentcore-control", region_name=region, config=cfg)
        self.max_retries = max_retries
        self._info: dict[str, EvaluatorInfo] = {}

    # -- evaluator discovery ------------------------------------------------

    def describe(self, evaluator_id: str) -> EvaluatorInfo:
        if evaluator_id in self._info:
            return self._info[evaluator_id]
        try:
            resp = self.cp.get_evaluator(evaluatorId=evaluator_id)
            info = EvaluatorInfo(
                evaluator_id=evaluator_id,
                level=resp.get("level"),
                available=True,
                name=str(resp.get("evaluatorName", "")),
                provider=str(resp.get("provider", resp.get("evaluatorType", ""))),
            )
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            _, code, msg = classify_exception(exc)
            info = EvaluatorInfo(evaluator_id, None, False, error=f"{code}: {msg}")
        self._info[evaluator_id] = info
        return info

    # -- evaluation -----------------------------------------------------------

    def evaluate(
        self,
        evaluator_id: str,
        spans: list[dict[str, Any]],
        *,
        session_id: str,
        trace_ids: list[str],
        tool_span_ids: list[str],
        reference: dict[str, Any] | None = None,
    ) -> list[EvalRecord]:
        info = self.describe(evaluator_id)
        if not info.available:
            status = (
                STATUS_SERVICE_UNAVAILABLE
                if info.error.split(":", 1)[0] in _UNAVAILABLE_CODES | {"EndpointConnectionError", "UnknownServiceError"}
                else STATUS_EVALUATOR_ERROR
            )
            code, _, msg = info.error.partition(": ")
            return [EvalRecord(evaluator_id, None, status, error_code=code, error_message=msg)]

        level = info.level or ""
        reference = reference or {}
        if evaluator_id in TRAJECTORY_EVALUATORS and not reference.get("expected_trajectory"):
            return [EvalRecord(evaluator_id, level, STATUS_NOT_APPLICABLE,
                               error_message="scenario defines no expected trajectory")]

        base: dict[str, Any] = {"evaluationInput": {"sessionSpans": spans}}
        refs = build_reference_inputs(session_id, reference, trace_ids)
        if refs:
            base["evaluationReferenceInputs"] = refs

        if level == "SESSION":
            requests = [base]
        elif level == "TRACE":
            requests = [
                {**base, "evaluationTarget": {"traceIds": trace_ids[i : i + MAX_TARGET_IDS_PER_REQUEST]}}
                for i in range(0, len(trace_ids), MAX_TARGET_IDS_PER_REQUEST)
            ]
        elif level == "TOOL_CALL":
            requests = [
                {**base, "evaluationTarget": {"spanIds": tool_span_ids[i : i + MAX_TARGET_IDS_PER_REQUEST]}}
                for i in range(0, len(tool_span_ids), MAX_TARGET_IDS_PER_REQUEST)
            ]
        else:
            return [EvalRecord(evaluator_id, level, STATUS_EVALUATOR_ERROR,
                               error_code="UnknownLevel", error_message=f"unsupported level {level!r}")]

        if not requests:
            what = "tool calls" if level == "TOOL_CALL" else "traces"
            return [EvalRecord(evaluator_id, level, STATUS_NOT_APPLICABLE,
                               error_message=f"no {what} to evaluate in this session")]

        records: list[EvalRecord] = []
        for request in requests:
            records.extend(self._call(evaluator_id, level, request))
        return records

    def _call(self, evaluator_id: str, level: str, request: dict[str, Any]) -> list[EvalRecord]:
        delay = 2.0
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.dp.evaluate(evaluatorId=evaluator_id, **request)
                break
            except ClientError as exc:
                if _client_error_code(exc) in _RETRYABLE_CODES and attempt < self.max_retries:
                    logger.warning("%s: %s, retrying in %.0fs", evaluator_id, _client_error_code(exc), delay)
                    time.sleep(delay)
                    delay *= 2
                    continue
                status, code, msg = classify_exception(exc)
                return [EvalRecord(evaluator_id, level, status, error_code=code, error_message=msg)]
            except Exception as exc:  # noqa: BLE001
                status, code, msg = classify_exception(exc)
                return [EvalRecord(evaluator_id, level, status, error_code=code, error_message=msg)]

        results = resp.get("evaluationResults") or []
        if not results:
            return [EvalRecord(evaluator_id, level, STATUS_EVALUATOR_ERROR,
                               error_code="EmptyResult", error_message="Evaluate returned no results")]
        return [parse_result(evaluator_id, level, r) for r in results]


def parse_result(evaluator_id: str, level: str | None, r: dict[str, Any]) -> EvalRecord:
    ctx = (r.get("context") or {}).get("spanContext") or {}
    rec = EvalRecord(
        evaluator_id=r.get("evaluatorId", evaluator_id),
        level=level,
        status=STATUS_SCORED,
        value=r.get("value"),
        label=r.get("label"),
        explanation=r.get("explanation"),
        dimension=r.get("evaluationDimensionName"),
        trace_id=ctx.get("traceId"),
        span_id=ctx.get("spanId"),
        token_usage=r.get("tokenUsage"),
        ignored_reference_fields=list(r.get("ignoredReferenceInputFields") or []),
    )
    if r.get("errorCode") or r.get("errorMessage"):
        rec.status = STATUS_EVALUATOR_ERROR
        rec.error_code = r.get("errorCode")
        rec.error_message = r.get("errorMessage")
    elif rec.value is None and rec.label is None:
        rec.status = STATUS_EVALUATOR_ERROR
        rec.error_code = "NoScore"
        rec.error_message = "result carried neither value nor label"
    return rec


def build_reference_inputs(session_id: str, reference: dict[str, Any], trace_ids: list[str]) -> list[dict[str, Any]]:
    """Ground truth in the ``evaluationReferenceInputs`` shape.

    Session-scoped: assertions and expected trajectory. Trace-scoped: an
    expected response, attached to the last trace (single-turn scenarios).
    """
    out: list[dict[str, Any]] = []
    session_ref: dict[str, Any] = {"context": {"spanContext": {"sessionId": session_id}}}
    if reference.get("assertions"):
        session_ref["assertions"] = [{"text": a} for a in reference["assertions"]]
    if reference.get("expected_trajectory"):
        session_ref["expectedTrajectory"] = {"toolNames": list(reference["expected_trajectory"])}
    if len(session_ref) > 1:
        out.append(session_ref)
    if reference.get("expected_response") and trace_ids:
        out.append({
            "context": {"spanContext": {"sessionId": session_id, "traceId": trace_ids[-1]}},
            "expectedResponse": {"text": reference["expected_response"]},
        })
    return out
