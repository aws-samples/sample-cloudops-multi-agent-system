"""Canonical CloudWatch resource and alarm shapes."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .arn import parse_arn_to_dimensions

GLOBAL_METRIC_REGIONS = {
    "AWS/CloudFront": "us-east-1",
    "AWS/Route53": "us-east-1",
}


def canonical_dimensions(dimensions: Any) -> list[dict[str, str]]:
    """Normalize and sort CloudWatch dimensions."""
    if isinstance(dimensions, dict):
        dimensions = [
            {"Name": name, "Value": value} for name, value in dimensions.items()
        ]
    result = []
    for dimension in dimensions or []:
        if not isinstance(dimension, dict):
            continue
        name = dimension.get("Name", dimension.get("name"))
        value = dimension.get("Value", dimension.get("value"))
        if name is not None and value is not None:
            result.append({"Name": str(name), "Value": str(value)})
    return sorted(result, key=lambda item: (item["Name"], item["Value"]))


def stable_hash(value: Any, length: int = 24) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:length]


def resource_profile(
    resource_arn: str,
    tags: dict[str, str] | None = None,
    region: str | None = None,
) -> dict[str, Any]:
    """Build the shared resource profile used by collection and query paths."""
    namespace, dimensions, info = parse_arn_to_dimensions(resource_arn)
    parts = resource_arn.split(":", 5)
    arn_region = parts[3] if len(parts) == 6 else ""
    account_id = parts[4] if len(parts) == 6 else ""
    metric_region = GLOBAL_METRIC_REGIONS.get(namespace or "", region or arn_region)
    return {
        "resource_id": stable_hash(resource_arn),
        "arn": resource_arn,
        "account_id": account_id,
        "region": region or arn_region or metric_region,
        "metric_region": metric_region,
        "namespace": namespace,
        "resource_type": _resource_type(resource_arn),
        "dimensions": canonical_dimensions(dimensions),
        "tags": {str(k): str(v) for k, v in (tags or {}).items()},
        "resolution_note": info.get("note"),
        "supported": bool(namespace),
    }


def _resource_type(resource_arn: str) -> str:
    parts = resource_arn.split(":", 5)
    if len(parts) != 6:
        return "unknown"
    resource = parts[5].lstrip("/")
    resource_type = resource.split("/", 1)[0].split(":", 1)[0]
    return f"{parts[2]}:{resource_type or 'resource'}"


def normalize_alarm(alarm: dict[str, Any], region: str) -> dict[str, Any]:
    """Normalize simple, metric-math, and composite alarm configuration."""
    alarm_arn = alarm.get("AlarmArn", alarm.get("alarm_arn", ""))
    alarm_name = alarm.get("AlarmName", alarm.get("alarm_name", ""))
    metrics = alarm.get("Metrics", alarm.get("metrics", [])) or []
    normalized_metrics = normalize_metric_queries(metrics)
    namespace = alarm.get("Namespace", alarm.get("namespace"))
    metric_name = alarm.get("MetricName", alarm.get("metric_name"))
    dimensions = canonical_dimensions(
        alarm.get("Dimensions", alarm.get("dimensions", []))
    )
    metric_signature = {
        "namespace": namespace,
        "metric_name": metric_name,
        "dimensions": dimensions,
    }
    math_signature = stable_hash(normalized_metrics, 64) if normalized_metrics else None
    return {
        "alarm_id": stable_hash(alarm_arn or f"{region}:{alarm_name}"),
        "alarm_arn": alarm_arn,
        "alarm_name": alarm_name,
        "alarm_type": (
            "composite"
            if alarm.get("AlarmRule", alarm.get("alarm_rule")) is not None
            else "metric"
        ),
        "region": region,
        "namespace": namespace,
        "metric_name": metric_name,
        "dimensions": dimensions,
        "metrics": normalized_metrics,
        "metric_signature": stable_hash(metric_signature, 64),
        "math_signature": math_signature,
        "statistic": alarm.get("Statistic")
        or alarm.get("ExtendedStatistic")
        or alarm.get("statistic"),
        "comparison_operator": alarm.get(
            "ComparisonOperator", alarm.get("comparison_operator")
        ),
        "threshold": alarm.get("Threshold", alarm.get("threshold")),
        "threshold_metric_id": alarm.get(
            "ThresholdMetricId", alarm.get("threshold_metric_id")
        ),
        "period": alarm.get("Period", alarm.get("period")),
        "evaluation_periods": alarm.get(
            "EvaluationPeriods", alarm.get("evaluation_periods")
        ),
        "datapoints_to_alarm": alarm.get(
            "DatapointsToAlarm", alarm.get("datapoints_to_alarm")
        ),
        "treat_missing_data": alarm.get(
            "TreatMissingData", alarm.get("treat_missing_data")
        ),
        "actions_enabled": alarm.get("ActionsEnabled", alarm.get("actions_enabled")),
        "alarm_actions": alarm.get("AlarmActions", alarm.get("alarm_actions", [])),
        "alarm_rule": alarm.get("AlarmRule", alarm.get("alarm_rule")),
    }


def _normalize_query(query: dict[str, Any]) -> dict[str, Any]:
    metric_stat = query.get("MetricStat", query.get("metric_stat", {})) or {}
    metric = metric_stat.get("Metric", metric_stat.get("metric", {})) or {}
    return {
        "id": query.get("Id", query.get("id")),
        "expression": query.get("Expression", query.get("expression")),
        "return_data": query.get("ReturnData", query.get("return_data")),
        "metric": (
            {
                "namespace": metric.get("Namespace", metric.get("namespace")),
                "metric_name": metric.get("MetricName", metric.get("metric_name")),
                "dimensions": canonical_dimensions(
                    metric.get("Dimensions", metric.get("dimensions", []))
                ),
                "period": metric_stat.get("Period", metric_stat.get("period")),
                "stat": metric_stat.get("Stat", metric_stat.get("stat")),
            }
            if metric
            else None
        ),
    }


def normalize_metric_queries(queries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Canonicalize a complete metric-math query set."""
    normalized = [_normalize_query(query) for query in queries or []]
    return sorted(normalized, key=lambda query: query.get("id") or "")
