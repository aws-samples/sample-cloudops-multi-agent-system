"""Pure alarm recommendation coverage matching."""

from __future__ import annotations

from typing import Any

from .normalize import canonical_dimensions, normalize_metric_queries, stable_hash
from .recommendations import get_namespace_alarm_recommendations

COVERAGE_STATUSES = frozenset(
    {
        "implemented",
        "implemented_with_drift",
        "missing",
        "unresolved_dimensions",
        "unsupported_resource",
        "inventory_incomplete",
    }
)

_DRIFT_FIELDS = {
    "comparison_operator": "comparisonOperator",
    "statistic": "statistic",
    "period": "period",
    "evaluation_periods": "evaluationPeriods",
    "datapoints_to_alarm": "datapointsToAlarm",
    "treat_missing_data": "treatMissingData",
}


def evaluate_resource(
    resource: dict[str, Any],
    alarms: list[dict[str, Any]],
    *,
    resource_inventory_complete: bool,
    alarm_inventory_complete: bool,
) -> dict[str, Any]:
    """Evaluate every recommendation variant for one resource."""
    if not resource.get("supported") or not resource.get("namespace"):
        return _resource_result(
            resource,
            "unsupported_resource",
            [],
            ["The resource ARN cannot be mapped to a CloudWatch metric profile."],
        )

    recommendations = get_namespace_alarm_recommendations(resource["namespace"])
    candidates: list[dict[str, Any]] = []
    statuses: list[str] = []
    reasons: list[str] = []
    for metric_name, variants in sorted(recommendations.items()):
        for recommendation in variants:
            resolved, missing_dimensions = _resolve_dimensions(
                recommendation.get("dimensions", []), resource.get("dimensions", [])
            )
            candidate = {
                "candidate_id": stable_hash(
                    {
                        "resource": resource["arn"],
                        "recommendation": recommendation["recommendationId"],
                    }
                ),
                "recommendation_id": recommendation["recommendationId"],
                "catalogue_version": recommendation["catalogueVersion"],
                "namespace": resource["namespace"],
                "metric_name": metric_name,
                "dimensions": resolved,
                "recommendation": recommendation,
                "threshold_strategy": _threshold_strategy(recommendation),
            }
            if missing_dimensions:
                candidate.update(
                    {
                        "status": "unresolved_dimensions",
                        "unresolved_dimensions": missing_dimensions,
                    }
                )
                statuses.append("unresolved_dimensions")
                candidates.append(candidate)
                continue

            matches = [
                alarm
                for alarm in alarms
                if _matches(alarm, recommendation, metric_name, resolved)
            ]
            if matches:
                drift = [
                    difference
                    for alarm in matches
                    for difference in _drift(alarm, recommendation)
                ]
                candidate.update(
                    {
                        "status": (
                            "implemented_with_drift" if drift else "implemented"
                        ),
                        "matched_alarm_ids": [
                            alarm.get("alarm_id") for alarm in matches
                        ],
                        "drift": drift,
                    }
                )
            elif not resource_inventory_complete or not alarm_inventory_complete:
                candidate["status"] = "inventory_incomplete"
            else:
                candidate["status"] = "missing"
            statuses.append(candidate["status"])
            candidates.append(candidate)

    if not resource_inventory_complete:
        reasons.append("resource_inventory_incomplete")
    if not alarm_inventory_complete:
        reasons.append("alarm_inventory_incomplete")
    status = _aggregate_status(statuses)
    return _resource_result(resource, status, candidates, reasons)


def _resource_result(
    resource: dict[str, Any],
    status: str,
    candidates: list[dict[str, Any]],
    reasons: list[str],
) -> dict[str, Any]:
    counts = {name: 0 for name in COVERAGE_STATUSES}
    for candidate in candidates:
        counts[candidate["status"]] += 1
    if not candidates:
        counts[status] += 1
    return {
        "resource_id": resource.get("resource_id"),
        "arn": resource.get("arn"),
        "resource_type": resource.get("resource_type"),
        "region": resource.get("metric_region") or resource.get("region"),
        "tags": resource.get("tags", {}),
        "coverage_status": status,
        "coverage_counts": counts,
        "candidate_ids": [
            candidate["candidate_id"]
            for candidate in candidates
            if candidate["status"] == "missing"
        ],
        "candidates": candidates,
        "completeness_reasons": reasons,
    }


def _resolve_dimensions(
    required: list[dict[str, Any]], resource_dimensions: list[dict[str, Any]]
) -> tuple[list[dict[str, str]], list[str]]:
    available = {
        item["Name"]: item["Value"]
        for item in canonical_dimensions(resource_dimensions)
    }
    names = [
        item.get("Name", item.get("name"))
        for item in required
        if item.get("Name", item.get("name"))
    ]
    missing = sorted(name for name in names if name not in available)
    return (
        canonical_dimensions(
            [
                {"Name": name, "Value": available[name]}
                for name in names
                if name in available
            ]
        ),
        missing,
    )


def _matches(
    alarm: dict[str, Any],
    recommendation: dict[str, Any],
    metric_name: str,
    dimensions: list[dict[str, str]],
) -> bool:
    if alarm.get("alarm_type") != "metric":
        return False
    recommendation_metrics = recommendation.get("metrics")
    if recommendation_metrics:
        expected = stable_hash(normalize_metric_queries(recommendation_metrics), 64)
        return alarm.get("math_signature") == expected
    return (
        alarm.get("namespace") == recommendation.get("namespace")
        and alarm.get("metric_name") == metric_name
        and canonical_dimensions(alarm.get("dimensions", [])) == dimensions
    )


def _drift(
    alarm: dict[str, Any], recommendation: dict[str, Any]
) -> list[dict[str, Any]]:
    result = []
    for alarm_field, recommendation_field in _DRIFT_FIELDS.items():
        expected = recommendation.get(recommendation_field)
        actual = alarm.get(alarm_field)
        if expected is not None and actual is not None and expected != actual:
            result.append(
                {
                    "alarm_id": alarm.get("alarm_id"),
                    "field": alarm_field,
                    "configured": actual,
                    "recommended": expected,
                }
            )
    return result


def _threshold_strategy(recommendation: dict[str, Any]) -> dict[str, Any]:
    threshold = recommendation.get("threshold") or {}
    if isinstance(threshold, (int, float)):
        return {"type": "fixed", "value": threshold}
    if threshold.get("staticValue") is not None:
        return {"type": "fixed", "value": threshold["staticValue"]}
    return {
        "type": "calibrate",
        "justification": threshold.get("justification", ""),
    }


def _aggregate_status(statuses: list[str]) -> str:
    if not statuses:
        return "implemented"
    for status in (
        "inventory_incomplete",
        "missing",
        "unresolved_dimensions",
        "implemented_with_drift",
    ):
        if status in statuses:
            return status
    return "implemented"
