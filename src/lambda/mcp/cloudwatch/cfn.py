"""
CloudFormation alarm assemblers — pure functions, no AWS calls, no I/O beyond
the YAML serializer in `assemble_cfn_template`.

`build_cfn_alarm` produces ONE `AWS::CloudWatch::Alarm` resource dict. Two
output shapes:
  * Static-threshold (most alarms): Statistic + Threshold + ComparisonOperator + Dimensions.
  * Anomaly-detection: Metrics array + ThresholdMetricId (used when ComparisonOperator is
    one of the anomaly variants: LessThanLowerOrGreaterThanUpperThreshold,
    LessThanLowerThreshold, GreaterThanUpperThreshold). For these the dimensions live
    inside MetricStat.Metric.Dimensions, not at the top level.

`assemble_cfn_template` is the single source of the emitted YAML for the
cloudwatch-agent. It calls `build_cfn_alarm` once per entry in the input
`alarms` array (so there is exactly ONE resource-shaping code path), wraps
the resources into a complete `{AWSTemplateFormatVersion, Description, Resources}`
document, and serializes it to a deterministic YAML string. The agent never
serializes the template itself — it places `template_yaml` verbatim inside an
artifact panel, removing the previous failure mode where the model abbreviated
inline YAML into a structural excerpt.

Both functions validate the SNS topic ARN up front and return a structured
error dict on bad input rather than raising, so the Lambda handler can surface
the validation failure verbatim to the agent.

AlarmName is derived from "<MetricName>-<short-hash-of-dimensions>". The hash is
deterministic (sorted dimension keys, SHA-256 truncated to 8 chars), so the same
resource produces the same AlarmName across re-runs — keeping CFN deploys idempotent.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

import yaml

# arn:aws:sns:<region>:<12-digit-account>:<topic-name>
# Compiled once at module load — used on every build_cfn_alarm call.
SNS_ARN_RE = re.compile(r"^arn:aws:sns:[a-z0-9-]+:\d{12}:.+$")

# CloudWatch comparison operators that mean "anomaly detection alarm". Static-threshold
# alarms use one of GreaterThanThreshold / LessThanThreshold /
# GreaterThanOrEqualToThreshold / LessThanOrEqualToThreshold instead.
ANOMALY_OPERATORS = frozenset(
    {
        "LessThanLowerOrGreaterThanUpperThreshold",
        "LessThanLowerThreshold",
        "GreaterThanUpperThreshold",
    }
)


def _normalize_dimensions(dimensions: list[dict] | None) -> list[dict]:
    """Accept either the catalogue's lowercase {name, value} form or CFN's {Name, Value}
    form and always emit CFN form."""
    out: list[dict] = []
    for d in dimensions or []:
        out.append(
            {
                "Name": d.get("Name", d.get("name", "")),
                "Value": d.get("Value", d.get("value", "")),
            }
        )
    return out


def _short_hash(cfn_dimensions: list[dict]) -> str:
    """Stable 8-char hash of the dimension list — disambiguates AlarmName per resource."""
    canonical = ",".join(
        f"{d['Name']}={d['Value']}"
        for d in sorted(cfn_dimensions, key=lambda d: d["Name"])
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]


def _tag_list(tags: dict[str, str]) -> list[dict]:
    """Convert {key: value} dict to CFN [{Key: ..., Value: ...}, ...]."""
    return [{"Key": k, "Value": v} for k, v in tags.items()]


def build_cfn_alarm(
    alarm_recommendation: dict[str, Any],
    dimensions: list[dict],
    threshold: float,
    sns_topic_arn: str,
    tags: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Assemble a CloudFormation AWS::CloudWatch::Alarm resource dict.

    Args:
        alarm_recommendation: One entry from the catalogue's alarmRecommendations
            list, augmented by the caller with the metric identity. Required keys:
            comparisonOperator, statistic, period, evaluationPeriods,
            datapointsToAlarm, treatMissingData. Optional: namespace, metric_name
            (or namespace/metricName camelCase), alarmDescription, intent.
        dimensions: [{name, value} | {Name, Value}, ...]. Callers fill in values
            from the resource ARN before calling. For anomaly-detection alarms,
            these are placed inside MetricStat.Metric.Dimensions, not at the top
            level (the design.md "CfnAlarm" data model documents this).
        threshold: numeric threshold value. Ignored on the anomaly-detection path.
        sns_topic_arn: SNS topic ARN to wire into AlarmActions. Validated against
            ^arn:aws:sns:[a-z0-9-]+:\\d{12}:.+$ before any other work.
        tags: optional {key: value} dict. Tags key is OMITTED from Properties when
            tags is None or empty (avoids producing the noise of "Tags: []").

    Returns:
        {"Type": "AWS::CloudWatch::Alarm", "Properties": {...}} on success, or
        {"error": "invalid_sns_topic_arn", "received": ..., "message": ...} on
        invalid SNS ARN.
    """
    if not isinstance(sns_topic_arn, str) or not SNS_ARN_RE.match(sns_topic_arn):
        return {
            "error": "invalid_sns_topic_arn",
            "received": sns_topic_arn,
            "message": (
                "sns_topic_arn must match pattern "
                "arn:aws:sns:<region>:<12-digit-account>:<topic-name>"
            ),
        }

    # The catalogue uses camelCase (metricId.namespace, metricId.metricName) at the
    # top level of each entry, but build_cfn_alarm callers (the Lambda handler)
    # flatten that into the alarm_recommendation dict. Accept both shapes.
    namespace = (
        alarm_recommendation.get("namespace")
        or alarm_recommendation.get("Namespace")
        or ""
    )
    metric_name = (
        alarm_recommendation.get("metric_name")
        or alarm_recommendation.get("metricName")
        or alarm_recommendation.get("MetricName")
        or ""
    )
    comparison_operator = alarm_recommendation.get(
        "comparisonOperator", "GreaterThanThreshold"
    )
    statistic = alarm_recommendation.get("statistic", "Average")
    period = alarm_recommendation.get("period", 60)
    evaluation_periods = alarm_recommendation.get("evaluationPeriods", 5)
    datapoints_to_alarm = alarm_recommendation.get("datapointsToAlarm", 5)
    treat_missing_data = alarm_recommendation.get("treatMissingData", "missing")
    alarm_description = alarm_recommendation.get("alarmDescription", "")
    intent = alarm_recommendation.get("intent", "")

    cfn_dimensions = _normalize_dimensions(dimensions)
    dim_hash = _short_hash(cfn_dimensions)
    alarm_name = (
        f"{metric_name}-{dim_hash}" if metric_name else f"alarm-{dim_hash}"
    )
    # AlarmDescription falls back through alarmDescription → intent → namespace+metric
    # so the alarm always shows useful context in the CloudWatch console.
    description = alarm_description or intent or f"{namespace} {metric_name}".strip()

    if comparison_operator in ANOMALY_OPERATORS:
        # Anomaly-detection alarm — Metrics + ThresholdMetricId.
        # Statistic / Threshold / Dimensions are intentionally absent at the top
        # level; dimensions live inside MetricStat.Metric.Dimensions.
        # The expression "ANOMALY_DETECTION_BAND(m1, 2)" uses a 2-stddev band,
        # matching CloudWatch's default when none is specified.
        properties: dict[str, Any] = {
            "AlarmName": alarm_name,
            "AlarmDescription": description,
            "ComparisonOperator": comparison_operator,
            "EvaluationPeriods": evaluation_periods,
            "DatapointsToAlarm": datapoints_to_alarm,
            "TreatMissingData": treat_missing_data,
            "AlarmActions": [sns_topic_arn],
            "Metrics": [
                {
                    "Id": "m1",
                    "ReturnData": True,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": namespace,
                            "MetricName": metric_name,
                            "Dimensions": cfn_dimensions,
                        },
                        "Period": period,
                        "Stat": statistic,
                    },
                },
                {
                    "Id": "ad1",
                    "Expression": "ANOMALY_DETECTION_BAND(m1, 2)",
                    "Label": f"{metric_name} (expected band)" if metric_name else "Expected band",
                    "ReturnData": True,
                },
            ],
            "ThresholdMetricId": "ad1",
        }
    else:
        # Static-threshold alarm — standard shape.
        properties = {
            "AlarmName": alarm_name,
            "AlarmDescription": description,
            "Namespace": namespace,
            "MetricName": metric_name,
            "Dimensions": cfn_dimensions,
            "Statistic": statistic,
            "ComparisonOperator": comparison_operator,
            "Threshold": threshold,
            "EvaluationPeriods": evaluation_periods,
            "DatapointsToAlarm": datapoints_to_alarm,
            "Period": period,
            "TreatMissingData": treat_missing_data,
            "AlarmActions": [sns_topic_arn],
        }

    if tags:
        properties["Tags"] = _tag_list(tags)

    return {
        "Type": "AWS::CloudWatch::Alarm",
        "Properties": properties,
    }


# ---------------------------------------------------------------------------
# Logical-ID derivation for assemble_cfn_template
# ---------------------------------------------------------------------------

# CloudFormation logical IDs must match ^[A-Za-z0-9]+$ — no hyphens, dots,
# slashes, or other punctuation. AlarmName carries a hyphen ("Errors-3f2a9c1b"),
# so we strip every non-alphanumeric character to derive the logical ID.
_LOGICAL_ID_FORBIDDEN = re.compile(r"[^A-Za-z0-9]")


def _logical_id_from_alarm_name(alarm_name: str) -> str:
    """Derive a CFN-safe logical ID from an AlarmName.

    Strips every non-alphanumeric character. Empty input falls back to "Alarm".
    Used by assemble_cfn_template so each alarm has a unique key under
    Resources without colliding on the alphanumeric hash suffix from the
    AlarmName.
    """
    clean = _LOGICAL_ID_FORBIDDEN.sub("", alarm_name or "")
    return clean or "Alarm"


# ---------------------------------------------------------------------------
# YAML emitter — deterministic, key-order preserving
# ---------------------------------------------------------------------------

# We need three things from the YAML emitter:
#   1. Insertion-order key emission (so the doc reads top-down: Type then
#      Properties, AlarmName before everything else, etc.).
#   2. Block style — no flow-style {a: 1, b: 2}; CFN templates are read by
#      humans.
#   3. No line wrapping inside long ARN values.
#
# yaml.safe_dump with sort_keys=False, default_flow_style=False, and a huge
# `width` covers all three. We bypass yaml.SafeDumper's default tuple/dict
# alphabetization with sort_keys=False, and we use insertion-order dicts
# everywhere upstream (build_cfn_alarm builds Properties in the order CFN
# users expect to read).


class _CfnDumper(yaml.SafeDumper):
    """Subclass to keep emitter customizations isolated from the global
    SafeDumper (which other parts of the platform may rely on)."""


# Force block-style emission for both dicts and lists. The default SafeDumper
# uses flow-style for empty collections; we never emit those, but we set the
# style explicitly so an empty Tags list doesn't accidentally come out as `[]`.
_CfnDumper.add_representer(
    dict,
    lambda dumper, data: dumper.represent_mapping(
        "tag:yaml.org,2002:map", data, flow_style=False
    ),
)
_CfnDumper.add_representer(
    list,
    lambda dumper, data: dumper.represent_sequence(
        "tag:yaml.org,2002:seq", data, flow_style=False
    ),
)


def _serialize_template_yaml(template: dict[str, Any]) -> str:
    """Deterministic YAML serializer for the CFN document.

    sort_keys=False → preserve insertion order from build_cfn_alarm.
    width=10**9 → never wrap long ARNs / descriptions onto multiple lines.
    default_flow_style=False → block style throughout.
    """
    return yaml.dump(
        template,
        Dumper=_CfnDumper,
        sort_keys=False,
        default_flow_style=False,
        width=10**9,
        allow_unicode=True,
    )


# ---------------------------------------------------------------------------
# assemble_cfn_template
# ---------------------------------------------------------------------------


def assemble_cfn_template(
    alarms: list[dict[str, Any]],
    sns_topic_arn: str,
    tags: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Assemble a complete CloudFormation document of `AWS::CloudWatch::Alarm`
    resources from a list of alarm specs.

    This is the SINGLE source of the emitted YAML for the cloudwatch-agent.
    Per spec Requirement 7: the agent must place `template_yaml` verbatim
    inside the artifact panel — it must NOT re-serialize, summarize, or
    abbreviate. Building the YAML in Python (here) instead of letting the
    model serialize a code block in its reply is what makes that contract
    enforceable.

    Args:
        alarms: list of per-alarm specs. Each entry is one of:
          - {"alarm_dict": {...}, "dimensions": [...], "threshold": <num>,
             "tags": {...} | None}  — recommended canonical shape.
          - The flat dict you'd pass to build_cfn_alarm directly (the same
             alarm_dict, with `dimensions`/`threshold`/`tags` siblings).
          The first form is what the agent and tools.json schema use; the
          second is accepted as a convenience for direct callers.
        sns_topic_arn: required SNS topic ARN; validated once up front.
        tags: optional template-level tag map applied to every alarm. Per-alarm
            `tags` (when supplied in the alarm entry) override the template-level
            tags for that alarm.

    Returns:
        On success: {
            "template_yaml": <full CFN YAML as a single string>,
            "summary": {
                "alarm_count": <int>,
                "logical_ids": [<id>, ...],
                "errors": [{"index": int, "message": str}, ...],
            },
        }
        On invalid SNS ARN (validated up front): the same error dict shape
        build_cfn_alarm returns — {"error": "invalid_sns_topic_arn", ...} —
        with NO template built. Per-alarm build_cfn_alarm errors do NOT abort
        the template; they're captured in `summary.errors` and the offending
        alarm is omitted from `Resources`.
    """
    # SNS validation — single point of failure, no template built on failure.
    if not isinstance(sns_topic_arn, str) or not SNS_ARN_RE.match(sns_topic_arn):
        return {
            "error": "invalid_sns_topic_arn",
            "received": sns_topic_arn,
            "message": (
                "sns_topic_arn must match pattern "
                "arn:aws:sns:<region>:<12-digit-account>:<topic-name>"
            ),
        }

    if not isinstance(alarms, list):
        return {
            "error": "invalid_request",
            "message": "alarms must be a list of alarm specs.",
        }

    resources: dict[str, Any] = {}
    logical_ids: list[str] = []
    errors: list[dict[str, Any]] = []

    for index, entry in enumerate(alarms):
        if not isinstance(entry, dict):
            errors.append({"index": index, "message": "alarm entry must be a dict"})
            continue

        # Canonical shape: {"alarm_dict": {...}, "dimensions": [...],
        # "threshold": <num>, "tags": {...}}. Falls back to the flat shape
        # for direct callers — pull alarm_dict from the entry itself.
        if "alarm_dict" in entry:
            alarm_dict = entry.get("alarm_dict") or {}
            dimensions = entry.get("dimensions") or []
            threshold = entry.get("threshold")
            per_alarm_tags = entry.get("tags")
        else:
            alarm_dict = entry
            dimensions = entry.get("dimensions") or []
            threshold = entry.get("threshold")
            per_alarm_tags = entry.get("tags")

        # Per-alarm tags override the template-level tags for that alarm.
        # Otherwise inherit the template-level tags. Both `None` and `{}`
        # collapse to "no tags emitted" by build_cfn_alarm.
        effective_tags = per_alarm_tags if per_alarm_tags is not None else tags

        resource = build_cfn_alarm(
            alarm_recommendation=alarm_dict,
            dimensions=dimensions,
            threshold=threshold,
            sns_topic_arn=sns_topic_arn,
            tags=effective_tags,
        )

        if "error" in resource:
            errors.append(
                {
                    "index": index,
                    "message": resource.get("message")
                    or resource.get("error", "unknown"),
                    "error": resource.get("error"),
                }
            )
            continue

        alarm_name = resource["Properties"].get("AlarmName") or f"Alarm{index}"
        logical_id = _logical_id_from_alarm_name(alarm_name)
        # If two alarms collapse to the same logical ID after stripping
        # punctuation, append a numeric suffix to keep CFN happy.
        candidate = logical_id
        suffix = 2
        while candidate in resources:
            candidate = f"{logical_id}{suffix}"
            suffix += 1
        resources[candidate] = resource
        logical_ids.append(candidate)

    template = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": (
            "CloudWatch alarms generated by cloudwatch-agent. "
            "Review thresholds before deploying."
        ),
        "Resources": resources,
    }

    template_yaml = _serialize_template_yaml(template)

    return {
        "template_yaml": template_yaml,
        "summary": {
            "alarm_count": len(resources),
            "logical_ids": logical_ids,
            "errors": errors,
        },
    }
