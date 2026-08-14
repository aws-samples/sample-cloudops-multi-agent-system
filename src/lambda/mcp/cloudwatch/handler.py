"""
CloudWatch MCP Lambda — Gateway-facing handler.

Routing contract (shared with every MCP Lambda on the gateway):
    context.client_context.custom["bedrockAgentCoreToolName"] = "<target>___<tool_name>"
    event body = tool params (NOT wrapped)

Nine tools, all sync (boto3 calls are blocking; no asyncio):

  * get_metric_data               — cloudwatch:GetMetricData wrapper. Supports
                                    percentile stats (p50/p99/...) and metric-math
                                    `queries` arrays.
  * get_recommended_metric_alarms — vendored catalogue lookup; optional
                                    resource_arn → dimensions via the ARN parser.
                                    No AWS call.
  * analyze_alarm_coverage        — compare bounded alarm posture with catalogue
                                    recommendations for one scope or an inventory.
  * analyse_metric                — cloudwatch:GetMetricData over a 14-day window,
                                    fed into the pure numpy/pandas analysis.
  * get_active_alarms             — cloudwatch:DescribeAlarms filtered to ALARM.
  * get_alarm_posture             — bounded alarm configuration inventory.
  * get_alarm_history             — cloudwatch:DescribeAlarmHistory.
  * build_cfn_alarm               — pure dict assembler. No AWS call.
  * assemble_cfn_template         — pure typed-artifact assembler. No AWS call.

All AWS calls flow through shared.cross_account.get_aws_client(role_alias="CLOUDWATCH")
so cross-account access works when CROSS_ACCOUNT_ROLE_ARN_CLOUDWATCH is set and falls
back to the execution role when it isn't.

Imports
-------
The helper modules (recommendations, analysis, cfn, arn) live next to this file.
`make package` copies *.py + the data/ dir + shared/ into the zip root, so flat
sibling imports resolve at runtime (the Lambda runtime puts /var/task — the zip
root — on sys.path automatically).

Under pytest the cloudwatch dir is NOT on sys.path, so the flat imports raise
ImportError. We deliberately do NOT insert the cloudwatch dir onto sys.path —
doing so globally shadows other Lambdas' bare module names (e.g. another tool's
`handler.py`) for the rest of the pytest session. Instead we fall back to loading
the siblings by file path under cloudwatch-namespaced module names, leaving
sys.path untouched.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

try:
    # Runtime (Lambda zip root on sys.path): flat sibling imports resolve.
    import analysis  # noqa: E402
    import arn as arn_mod  # noqa: E402
    import cfn  # noqa: E402
    import recommendations  # noqa: E402
except ImportError:
    # Pytest: the cloudwatch dir isn't on sys.path. Load the siblings by path
    # under unique names so we don't pollute sys.path and shadow other Lambdas'
    # bare module names (e.g. network_resilience's `handler`).
    import importlib.util

    _THIS_DIR = os.path.dirname(os.path.abspath(__file__))

    def _load_sibling(name: str):
        spec = importlib.util.spec_from_file_location(
            f"cloudwatch_{name}", os.path.join(_THIS_DIR, f"{name}.py")
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    analysis = _load_sibling("analysis")
    arn_mod = _load_sibling("arn")
    cfn = _load_sibling("cfn")
    recommendations = _load_sibling("recommendations")

# `from shared.cross_account import get_aws_client` needs src/lambda/mcp on the
# path under pytest; that dir contains no bare handler.py so it shadows nothing.
# At runtime, shared/ is copied into the zip root and resolves directly.
from shared.cross_account import get_aws_client  # noqa: E402
from shared.cloudwatch_domain import coverage as coverage_domain  # noqa: E402
from shared.cloudwatch_domain import normalize as normalize_domain  # noqa: E402
from shared.cloudwatch_domain import snapshot as snapshot_domain  # noqa: E402
from shared.cloudwatch_domain.recommendations import (  # noqa: E402
    CATALOGUE_VERSION,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Default lookback for get_metric_data when start_time is omitted (Requirement 1.2).
_DEFAULT_METRIC_LOOKBACK = _dt.timedelta(hours=3)
# Default lookback for analyse_metric (Requirement 1.2).
_DEFAULT_ANALYSE_LOOKBACK_DAYS = 14
# Default lookback for get_alarm_history when start_time is omitted (Requirement 1.2).
_DEFAULT_ALARM_HISTORY_LOOKBACK = _dt.timedelta(hours=24)
_POSTURE_DEFAULT_MAX_ITEMS = 100
_POSTURE_HARD_MAX_ITEMS = 1000
_COVERAGE_RESOURCE_HARD_CAP = 1000
_SNAPSHOT_TABLE = os.environ.get("CLOUDWATCH_COVERAGE_TABLE_NAME", "")
_COORDINATOR_FUNCTION = os.environ.get("CLOUDWATCH_COVERAGE_COORDINATOR_NAME", "")
_CURSOR_SECRET = os.environ.get("CLOUDWATCH_COVERAGE_CURSOR_SECRET", _SNAPSHOT_TABLE)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _now_utc() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _parse_time(value: Any) -> _dt.datetime | None:
    """Parse an ISO-8601 string or epoch number into an aware datetime.

    Returns None for falsy input so callers can apply their own default.
    Accepts trailing 'Z'. Never raises on bad input — returns None.
    """
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return _dt.datetime.fromtimestamp(float(value), tz=_dt.timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = _dt.datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.timezone.utc)
        return parsed
    return None


def _cw_client(event: dict[str, Any]):
    """Return a cross-account-aware cloudwatch client.

    Honours an optional `region` in the event; otherwise boto3 resolves the
    Lambda's region.
    """
    region = event.get("region") or None
    return get_aws_client(
        service_name="cloudwatch",
        region_name=region,
        role_alias="CLOUDWATCH",
    )


# ---------------------------------------------------------------------------
# Tool: get_metric_data
# ---------------------------------------------------------------------------


def get_metric_data(event: dict[str, Any]) -> dict[str, Any]:
    """boto3 cloudwatch:GetMetricData wrapper.

    Two ways to specify what to fetch:
      1. `queries`: a list of raw MetricDataQuery dicts (passed through to
         boto3 verbatim). Use this for metric math and multi-metric pulls.
      2. namespace + metric_name + dimensions + stat + period: a single
         metric query is built for you. `stat` accepts standard statistics
         (Average/Sum/Maximum/Minimum/SampleCount) AND percentiles
         (p50/p90/p95/p99/p99.9/tm99/...) — boto3 takes them as-is.

    Time window: start_time/end_time as ISO-8601 strings or epoch seconds.
    Defaults to a 3-hour lookback ending now when start_time is omitted.
    """
    end_time = _parse_time(event.get("end_time")) or _now_utc()
    start_time = _parse_time(event.get("start_time")) or (
        end_time - _DEFAULT_METRIC_LOOKBACK
    )

    queries = event.get("queries")
    if queries:
        metric_data_queries = list(queries)
    else:
        namespace = event.get("namespace")
        metric_name = event.get("metric_name")
        if not namespace or not metric_name:
            return {
                "error": "invalid_request",
                "message": (
                    "Provide either a `queries` array or "
                    "`namespace` + `metric_name` (plus optional dimensions/stat/period)."
                ),
            }
        stat = event.get("stat") or event.get("statistic") or "Average"
        period = int(event.get("period", 300))
        dimensions = _to_cw_dimensions(event.get("dimensions"))
        metric_data_queries = [
            {
                "Id": event.get("query_id", "m1"),
                "MetricStat": {
                    "Metric": {
                        "Namespace": namespace,
                        "MetricName": metric_name,
                        "Dimensions": dimensions,
                    },
                    "Period": period,
                    "Stat": stat,
                },
                "ReturnData": True,
            }
        ]

    client = _cw_client(event)
    params: dict[str, Any] = {
        "MetricDataQueries": metric_data_queries,
        "StartTime": start_time,
        "EndTime": end_time,
    }
    scan_by = event.get("scan_by")
    if scan_by:
        params["ScanBy"] = scan_by

    resp = client.get_metric_data(**params)

    results = []
    for r in resp.get("MetricDataResults", []):
        results.append(
            {
                "id": r.get("Id"),
                "label": r.get("Label"),
                "timestamps": [t.isoformat() for t in r.get("Timestamps", [])],
                "values": list(r.get("Values", [])),
                "status_code": r.get("StatusCode"),
            }
        )
    return {
        "metric_data_results": results,
        "messages": resp.get("Messages", []),
        "window": {"start": start_time.isoformat(), "end": end_time.isoformat()},
    }


def _to_cw_dimensions(dimensions: Any) -> list[dict]:
    """Normalize dimensions to boto3's [{Name, Value}] form.

    Accepts the catalogue/ARN-parser {Name, Value} form, the lowercase
    {name, value} form, or a flat {key: value} dict.
    """
    if not dimensions:
        return []
    if isinstance(dimensions, dict):
        return [{"Name": k, "Value": v} for k, v in dimensions.items()]
    out: list[dict] = []
    for d in dimensions:
        if not isinstance(d, dict):
            continue
        name = d.get("Name", d.get("name"))
        value = d.get("Value", d.get("value"))
        if name is not None:
            out.append({"Name": name, "Value": value})
    return out


# ---------------------------------------------------------------------------
# Tool: get_metric_metadata
# ---------------------------------------------------------------------------


def get_metric_metadata(event: dict[str, Any]) -> dict[str, Any]:
    """Return one vendored metric catalogue entry without an AWS call."""
    namespace = event.get("namespace")
    metric_name = event.get("metric_name")
    if not namespace or not metric_name:
        return {
            "error": "invalid_request",
            "message": "namespace and metric_name are required.",
        }
    metadata = recommendations.get_metric_metadata_from_catalogue(
        namespace, metric_name
    )
    return {
        "namespace": namespace,
        "metric_name": metric_name,
        "metadata": metadata,
        "catalogue_version": CATALOGUE_VERSION,
        "note": None if metadata else "not_in_catalogue",
    }


# ---------------------------------------------------------------------------
# Tool: get_recommended_metric_alarms
# ---------------------------------------------------------------------------


def get_recommended_metric_alarms(event: dict[str, Any]) -> dict[str, Any]:
    """Vendored catalogue lookup for alarm recommendations — no AWS call.

    When `resource_arn` is supplied and dimensions are not, the ARN is parsed
    to a (namespace, dimensions) pair. An explicit `namespace` in the event
    still wins for the catalogue lookup; the parsed namespace is the fallback.
    """
    namespace = event.get("namespace")
    metric_name = event.get("metric_name")
    dimensions = _to_cw_dimensions(event.get("dimensions"))
    resource_arn = event.get("resource_arn")
    note: str | None = None

    if resource_arn:
        parsed_ns, parsed_dims, info = arn_mod.parse_arn_to_dimensions(resource_arn)
        if not dimensions:
            dimensions = parsed_dims
        if not namespace:
            namespace = parsed_ns
        if info.get("note"):
            note = info["note"]

    if not namespace or not metric_name:
        return {
            "error": "invalid_request",
            "message": (
                "Provide namespace + metric_name, or a resource_arn the parser "
                "recognises plus metric_name."
            ),
            "namespace": namespace,
            "metric_name": metric_name,
            "dimensions": dimensions,
            "note": note,
        }

    recs = recommendations.get_recommended_alarms_from_catalogue(namespace, metric_name)
    if not recs and note is None:
        note = "no_recommendations_in_catalogue"

    return {
        "namespace": namespace,
        "metric_name": metric_name,
        "recommendations": recs,
        "dimensions": dimensions,
        "note": note,
    }


# ---------------------------------------------------------------------------
# Tool: analyse_metric
# ---------------------------------------------------------------------------


def analyse_metric(event: dict[str, Any]) -> dict[str, Any]:
    """Fetch a metric over `lookback_days` and run the statistical analysis.

    boto3 returns Values/Timestamps most-recent-first; we reverse to
    chronological order before feeding the pure analysis function. The
    `metric` + `window` metadata blocks are merged onto the AnalyseMetricResult
    per design.md. An `insufficient_history` result is passed through as-is.
    """
    namespace = event.get("namespace")
    metric_name = event.get("metric_name")
    if not namespace or not metric_name:
        return {
            "error": "invalid_request",
            "message": "namespace and metric_name are required.",
        }

    dimensions = _to_cw_dimensions(event.get("dimensions"))
    statistic = event.get("statistic") or event.get("stat") or "Average"
    lookback_days = int(event.get("lookback_days", _DEFAULT_ANALYSE_LOOKBACK_DAYS))
    period = int(event.get("period", 300))

    end_time = _parse_time(event.get("end_time")) or _now_utc()
    start_time = _parse_time(event.get("start_time")) or (
        end_time - _dt.timedelta(days=lookback_days)
    )

    client = _cw_client(event)
    resp = client.get_metric_data(
        MetricDataQueries=[
            {
                "Id": "m1",
                "MetricStat": {
                    "Metric": {
                        "Namespace": namespace,
                        "MetricName": metric_name,
                        "Dimensions": dimensions,
                    },
                    "Period": period,
                    "Stat": statistic,
                },
                "ReturnData": True,
            }
        ],
        StartTime=start_time,
        EndTime=end_time,
        ScanBy="TimestampDescending",
    )

    results = resp.get("MetricDataResults", [])
    if not results:
        values: list[float] = []
        timestamps: list[_dt.datetime] = []
    else:
        first = results[0]
        # boto3 returns most-recent-first; reverse to chronological order.
        timestamps = list(reversed(first.get("Timestamps", [])))
        values = list(reversed(first.get("Values", [])))

    analysis_result = analysis.analyse_metric_data(values, timestamps)

    metric_block = {
        "namespace": namespace,
        "metric_name": metric_name,
        "dimensions": dimensions,
    }
    window_block = {
        "start": start_time.isoformat(),
        "end": end_time.isoformat(),
        "period_seconds": period,
        "datapoints": len(values),
    }

    if analysis_result.get("error"):
        # insufficient_history (or similar) — surface verbatim with context.
        analysis_result["metric"] = metric_block
        analysis_result["window"] = window_block
        return analysis_result

    return {
        "metric": metric_block,
        "window": window_block,
        **analysis_result,
    }


# ---------------------------------------------------------------------------
# Tool: get_active_alarms
# ---------------------------------------------------------------------------


def get_active_alarms(event: dict[str, Any]) -> dict[str, Any]:
    """cloudwatch:DescribeAlarms filtered to StateValue=ALARM.

    Returns trimmed summaries (not the full boto3 response) for both metric
    and composite alarms, bounded by `max_items` (default 50).
    """
    max_items = int(event.get("max_items", 50))
    client = _cw_client(event)

    metric_alarms: list[dict] = []
    composite_alarms: list[dict] = []
    paginator = client.get_paginator("describe_alarms")
    for page in paginator.paginate(
        StateValue="ALARM",
        AlarmTypes=["MetricAlarm", "CompositeAlarm"],
    ):
        for a in page.get("MetricAlarms", []):
            if len(metric_alarms) < max_items:
                metric_alarms.append(_summarize_metric_alarm(a))
        for a in page.get("CompositeAlarms", []):
            if len(composite_alarms) < max_items:
                composite_alarms.append(_summarize_composite_alarm(a))
        if len(metric_alarms) >= max_items and len(composite_alarms) >= max_items:
            break

    return {
        "metric_alarms": metric_alarms,
        "composite_alarms": composite_alarms,
        "metric_alarm_count": len(metric_alarms),
        "composite_alarm_count": len(composite_alarms),
        "max_items": max_items,
    }


def _summarize_metric_alarm(a: dict[str, Any]) -> dict[str, Any]:
    return {
        "alarm_name": a.get("AlarmName"),
        "alarm_arn": a.get("AlarmArn"),
        "namespace": a.get("Namespace"),
        "metric_name": a.get("MetricName"),
        "dimensions": [
            {"Name": d.get("Name"), "Value": d.get("Value")}
            for d in a.get("Dimensions", [])
        ],
        "statistic": a.get("Statistic") or a.get("ExtendedStatistic"),
        "comparison_operator": a.get("ComparisonOperator"),
        "threshold": a.get("Threshold"),
        "period": a.get("Period"),
        "evaluation_periods": a.get("EvaluationPeriods"),
        "datapoints_to_alarm": a.get("DatapointsToAlarm"),
        "state_value": a.get("StateValue"),
        "state_reason": a.get("StateReason"),
        "state_updated_timestamp": _iso_or_none(a.get("StateUpdatedTimestamp")),
    }


def _summarize_composite_alarm(a: dict[str, Any]) -> dict[str, Any]:
    return {
        "alarm_name": a.get("AlarmName"),
        "alarm_arn": a.get("AlarmArn"),
        "alarm_rule": a.get("AlarmRule"),
        "state_value": a.get("StateValue"),
        "state_reason": a.get("StateReason"),
        "state_updated_timestamp": _iso_or_none(a.get("StateUpdatedTimestamp")),
    }


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


# ---------------------------------------------------------------------------
# Tool: get_alarm_posture (stable configuration, optional snapshot fast path)
# ---------------------------------------------------------------------------


def _bounded_posture_max_items(value: Any) -> int:
    try:
        requested = int(value)
    except (TypeError, ValueError):
        requested = _POSTURE_DEFAULT_MAX_ITEMS
    return max(1, min(requested, _POSTURE_HARD_MAX_ITEMS))


def _snapshot_account_id() -> str:
    """Resolve the snapshot account, preferring the configured target role."""
    role_arn = os.environ.get("CROSS_ACCOUNT_ROLE_ARN_CLOUDWATCH", "")
    parts = role_arn.split(":")
    if len(parts) >= 5 and parts[4].isdigit() and len(parts[4]) == 12:
        return parts[4]
    try:
        return str(boto3.client("sts").get_caller_identity().get("Account", ""))
    except Exception as exc:  # noqa: BLE001 - live posture remains available
        logger.warning("Could not resolve snapshot account: %s", exc)
        return ""


def _posture_metric_alarm(alarm: dict[str, Any]) -> dict[str, Any]:
    return {
        "alarm_type": "metric",
        "alarm_name": alarm.get("AlarmName"),
        "alarm_arn": alarm.get("AlarmArn"),
        "alarm_description": alarm.get("AlarmDescription"),
        "namespace": alarm.get("Namespace"),
        "metric_name": alarm.get("MetricName"),
        "dimensions": [
            {"Name": dimension.get("Name"), "Value": dimension.get("Value")}
            for dimension in alarm.get("Dimensions", [])
        ],
        "statistic": alarm.get("Statistic") or alarm.get("ExtendedStatistic"),
        "comparison_operator": alarm.get("ComparisonOperator"),
        "threshold": alarm.get("Threshold"),
        "threshold_metric_id": alarm.get("ThresholdMetricId"),
        "metrics": alarm.get("Metrics", []),
        "period": alarm.get("Period"),
        "evaluation_periods": alarm.get("EvaluationPeriods"),
        "datapoints_to_alarm": alarm.get("DatapointsToAlarm"),
        "treat_missing_data": alarm.get("TreatMissingData"),
        "evaluate_low_sample_count_percentile": alarm.get(
            "EvaluateLowSampleCountPercentile"
        ),
        "actions_enabled": alarm.get("ActionsEnabled"),
        "alarm_actions": alarm.get("AlarmActions", []),
        "ok_actions": alarm.get("OKActions", []),
        "insufficient_data_actions": alarm.get("InsufficientDataActions", []),
    }


def _posture_composite_alarm(alarm: dict[str, Any]) -> dict[str, Any]:
    return {
        "alarm_type": "composite",
        "alarm_name": alarm.get("AlarmName"),
        "alarm_arn": alarm.get("AlarmArn"),
        "alarm_description": alarm.get("AlarmDescription"),
        "alarm_rule": alarm.get("AlarmRule"),
        "actions_enabled": alarm.get("ActionsEnabled"),
        "alarm_actions": alarm.get("AlarmActions", []),
        "ok_actions": alarm.get("OKActions", []),
        "insufficient_data_actions": alarm.get("InsufficientDataActions", []),
    }


def get_alarm_posture(event: dict[str, Any]) -> dict[str, Any]:
    """List bounded alarm configuration posture without incident state fields."""
    max_items = _bounded_posture_max_items(event.get("max_items"))
    client = _cw_client(event)
    alarms: list[dict[str, Any]] = []
    metric_count = 0
    composite_count = 0
    capped = False
    paginator = client.get_paginator("describe_alarms")
    for page in paginator.paginate(AlarmTypes=["MetricAlarm", "CompositeAlarm"]):
        for alarm in page.get("MetricAlarms", []):
            metric_count += 1
            if len(alarms) < max_items:
                alarms.append(_posture_metric_alarm(alarm))
            else:
                capped = True
        for alarm in page.get("CompositeAlarms", []):
            composite_count += 1
            if len(alarms) < max_items:
                alarms.append(_posture_composite_alarm(alarm))
            else:
                capped = True
        if capped:
            break

    region = event.get("region") or os.environ.get("AWS_REGION", "us-east-1")
    return {
        "account_id": _snapshot_account_id(),
        "region": region,
        "summary": {
            "metric_alarm_count": metric_count,
            "composite_alarm_count": composite_count,
            "returned_alarm_count": len(alarms),
            "alarm_cap": max_items,
            "truncated": capped,
            "truncation_note": (
                f"Alarm inventory capped at {max_items}; pass a higher max_items "
                f"up to {_POSTURE_HARD_MAX_ITEMS} for more configuration rows."
                if capped
                else None
            ),
        },
        "alarms": alarms,
        "data_source": "live_api",
    }


def _snapshot_lookup_alarm_posture(event: dict[str, Any]) -> dict[str, Any] | None:
    """Read the canonical local-region posture snapshot or return None for live."""
    if not _SNAPSHOT_TABLE or event.get("force_refresh"):
        return None
    region = event.get("region") or os.environ.get("AWS_REGION", "us-east-1")
    if event.get("region") and region != os.environ.get("AWS_REGION", "us-east-1"):
        return None
    account_id = _snapshot_account_id()
    if not account_id:
        return None
    pk = f"POSTURE#{account_id}#{region}"
    try:
        ddb = boto3.client("dynamodb")
        response = ddb.get_item(
            TableName=_SNAPSHOT_TABLE,
            Key={"pk": {"S": pk}, "sk": {"S": "SUMMARY"}},
        )
        item = response.get("Item")
        if not item:
            return None
        stored = json.loads(item["payload"]["S"])
        snapshot_at = item.get("snapshot_at", {}).get("S", "")
        if stored.get("error"):
            stored["data_as_of"] = snapshot_at
            stored["data_source"] = "scheduled_snapshot"
            stored["freshness_note"] = (
                "Served from the scheduled alarm posture snapshot. "
                "Pass force_refresh=true for a live request."
            )
            return stored

        max_items = _bounded_posture_max_items(event.get("max_items"))
        alarms: list[dict[str, Any]] = []
        last_key = None
        while len(alarms) < max_items:
            query: dict[str, Any] = {
                "TableName": _SNAPSHOT_TABLE,
                "KeyConditionExpression": "pk = :pk AND begins_with(sk, :prefix)",
                "FilterExpression": "snapshot_at = :snapshot_at",
                "ExpressionAttributeValues": {
                    ":pk": {"S": pk},
                    ":prefix": {"S": "ALARM#"},
                    ":snapshot_at": {"S": snapshot_at},
                },
            }
            if last_key:
                query["ExclusiveStartKey"] = last_key
            page = ddb.query(**query)
            for alarm_item in page.get("Items", []):
                alarms.append(json.loads(alarm_item["payload"]["S"]))
                if len(alarms) >= max_items:
                    break
            last_key = page.get("LastEvaluatedKey")
            if not last_key:
                break

        summary = dict(stored.get("summary") or {})
        summary["returned_alarm_count"] = len(alarms)
        if len(alarms) >= max_items and stored.get("stored_alarm_count", 0) > max_items:
            summary["truncated"] = True
        return {
            "account_id": account_id,
            "region": region,
            "summary": summary,
            "alarms": alarms,
            "data_as_of": snapshot_at,
            "data_source": "scheduled_snapshot",
            "freshness_note": (
                "Served from the scheduled alarm configuration snapshot. "
                "Pass force_refresh=true for a live configuration refresh."
            ),
        }
    except Exception as exc:  # noqa: BLE001 - cache failures must not block reads
        logger.warning("alarm posture snapshot lookup failed; using live API: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Tool: analyze_alarm_coverage
# ---------------------------------------------------------------------------
#
# Grades existing alarm configuration against AWS's recommended alarm
# catalogue. The legacy mode grades one namespace/resource. Inventory mode
# consumes the bounded output of find_resources_by_tag (or resource_arns),
# groups resources by region, and reads alarm posture once per region. This
# makes resources with no matching alarm rows visible without an unbounded AWS
# resource scan.
# ---------------------------------------------------------------------------

# Structural fields compared for drift on an already-implemented alarm. Threshold
# VALUE is excluded on purpose: the catalogue carries only threshold justification
# text (env-specific numbers come from analyse_metric), so a value comparison
# would be meaningless.
_COVERAGE_DRIFT_FIELDS: tuple[tuple[str, str], ...] = (
    ("comparison_operator", "comparisonOperator"),
    ("statistic", "statistic"),
    ("period", "period"),
    ("evaluation_periods", "evaluationPeriods"),
    ("datapoints_to_alarm", "datapointsToAlarm"),
)


def _coverage_existing_alarms(event: dict[str, Any]) -> dict[str, Any]:
    """Return metric alarms plus posture identity and completeness metadata."""
    posture = _snapshot_lookup_alarm_posture(event)
    if posture is None:
        posture = get_alarm_posture(event)
    alarms = posture.get("alarms") if isinstance(posture.get("alarms"), list) else []
    summary = posture.get("summary")
    if not isinstance(summary, dict):
        summary = {}
    return {
        "alarms": [a for a in alarms if a.get("alarm_type") == "metric"],
        "account_id": posture.get("account_id", ""),
        "region": posture.get(
            "region",
            event.get("region") or os.environ.get("AWS_REGION", "us-east-1"),
        ),
        "data_source": posture.get("data_source", "live_api"),
        "data_as_of": posture.get("data_as_of", ""),
        "freshness_note": posture.get("freshness_note"),
        "truncated": bool(summary.get("truncated")),
        "truncation_note": summary.get("truncation_note"),
        "returned_alarm_count": summary.get("returned_alarm_count", len(alarms)),
        "alarm_cap": summary.get("alarm_cap"),
    }


def _dimensions_match(alarm_dims: list[dict], scope_dims: list[dict]) -> bool:
    """True when every scope {Name, Value} pair is present on the alarm."""
    if not scope_dims:
        return True
    have = {(d.get("Name"), d.get("Value")) for d in alarm_dims}
    want = {(d.get("Name"), d.get("Value")) for d in scope_dims}
    return want.issubset(have)


def _coverage_drift(
    alarm: dict[str, Any], recommendation: dict[str, Any]
) -> list[dict]:
    """Structural differences between an existing alarm and its recommendation."""
    drift: list[dict] = []
    for alarm_key, rec_key in _COVERAGE_DRIFT_FIELDS:
        if rec_key not in recommendation:
            continue
        recommended = recommendation.get(rec_key)
        actual = alarm.get(alarm_key)
        if actual is not None and recommended is not None and actual != recommended:
            drift.append(
                {"field": alarm_key, "configured": actual, "recommended": recommended}
            )
    return drift


def _alarm_metric_names_for_scope(
    alarm: dict[str, Any], namespace: str, scope_dims: list[dict]
) -> set[str]:
    """Return matching top-level and metric-math metric names for one alarm."""
    metric_names: set[str] = set()
    if alarm.get("namespace") == namespace and _dimensions_match(
        alarm.get("dimensions", []), scope_dims
    ):
        metric_name = alarm.get("metric_name")
        if metric_name:
            metric_names.add(metric_name)

    for query in alarm.get("metrics") or []:
        if not isinstance(query, dict):
            continue
        metric_stat = query.get("MetricStat") or query.get("metricStat") or {}
        metric = metric_stat.get("Metric") or metric_stat.get("metric") or {}
        if not isinstance(metric, dict) or metric.get("Namespace") != namespace:
            continue
        dimensions = metric.get("Dimensions") or []
        if not _dimensions_match(dimensions, scope_dims):
            continue
        metric_name = metric.get("MetricName")
        if metric_name:
            metric_names.add(metric_name)
    return metric_names


def _grade_alarm_scope(
    namespace: str,
    scope_dims: list[dict],
    existing_alarms: list[dict],
    recommended: dict[str, list[dict]],
    *,
    compact_missing: bool = False,
) -> dict[str, Any]:
    """Grade one resource scope against alarms already loaded for its region."""
    matching_alarms: list[dict] = []
    existing_by_metric: dict[str, list[dict]] = {}
    for alarm in existing_alarms:
        metric_names = _alarm_metric_names_for_scope(alarm, namespace, scope_dims)
        if not metric_names:
            continue
        matching_alarms.append(alarm)
        for metric_name in metric_names:
            existing_by_metric.setdefault(metric_name, []).append(alarm)

    implemented: list[dict] = []
    missing: list[dict] = []
    drift: list[dict] = []
    for metric_name, recs in sorted(recommended.items()):
        primary_rec = recs[0]
        matches = existing_by_metric.get(metric_name, [])
        if matches:
            implemented.append(
                {
                    "metric_name": metric_name,
                    "alarm_names": [a.get("alarm_name") for a in matches],
                    "recommendation_intent": primary_rec.get("intent"),
                }
            )
            for alarm in matches:
                alarm_drift = _coverage_drift(alarm, primary_rec)
                if alarm_drift:
                    drift.append(
                        {
                            "metric_name": metric_name,
                            "alarm_name": alarm.get("alarm_name"),
                            "differences": alarm_drift,
                        }
                    )
        else:
            metadata = recommendations.get_metric_metadata_from_catalogue(
                namespace, metric_name
            )
            missing_item = {
                "metric_name": metric_name,
                "metric_description": (metadata or {}).get("description"),
            }
            if not compact_missing:
                missing_item["recommendation"] = primary_rec
            missing.append(missing_item)

    return {
        "matching_alarm_count": len(matching_alarms),
        "implemented": implemented,
        "missing": missing,
        "drift": drift,
        "summary": {
            "recommended_metric_count": len(recommended),
            "implemented_count": len(implemented),
            "missing_count": len(missing),
            "drift_count": len(drift),
        },
    }


def _resource_identity_from_arn(arn: str, fallback_region: str) -> tuple[str, str]:
    """Return (region, account_id), using the request region for global ARNs."""
    parts = arn.split(":", 5)
    if len(parts) < 6:
        return fallback_region, ""
    return parts[3] or fallback_region, parts[4]


def _normalise_coverage_inventory(
    event: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]] | dict[str, Any]:
    """Validate, bound, parse, and deduplicate inventory-mode inputs."""
    inventory = event.get("resource_inventory")
    resource_arns = event.get("resource_arns")
    if inventory is not None and resource_arns is not None:
        return {
            "error": "invalid_request",
            "message": "Provide resource_inventory or resource_arns, not both.",
        }

    if inventory is not None:
        if not isinstance(inventory, dict) or not isinstance(
            inventory.get("resources"), list
        ):
            return {
                "error": "invalid_request",
                "message": "resource_inventory must contain a resources array.",
            }
        raw_resources = inventory["resources"]
        inventory_truncated = bool(inventory.get("truncated"))
        inventory_note = inventory.get("note")
        source = "resource_inventory"
    else:
        if not isinstance(resource_arns, list):
            return {
                "error": "invalid_request",
                "message": "resource_arns must be an array of AWS resource ARNs.",
            }
        raw_resources = [{"arn": arn} for arn in resource_arns]
        inventory_truncated = False
        inventory_note = None
        source = "resource_arns"

    if len(raw_resources) > _COVERAGE_RESOURCE_HARD_CAP:
        return {
            "error": "inventory_too_large",
            "message": (
                f"Coverage accepts at most {_COVERAGE_RESOURCE_HARD_CAP} resources. "
                "Use narrower tag filters or split explicit ARNs into scoped batches."
            ),
        }

    fallback_region = event.get("region") or os.environ.get("AWS_REGION", "us-east-1")
    resources: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    seen_arns: set[str] = set()
    duplicate_count = 0
    for index, raw in enumerate(raw_resources):
        if not isinstance(raw, dict):
            unsupported.append(
                {"arn": "", "reason": "invalid_inventory_item", "input_index": index}
            )
            continue
        arn = raw.get("arn")
        if not isinstance(arn, str) or not arn:
            unsupported.append(
                {"arn": "", "reason": "missing_resource_arn", "input_index": index}
            )
            continue
        if arn in seen_arns:
            duplicate_count += 1
            continue
        seen_arns.add(arn)

        namespace, dimensions, parser_info = arn_mod.parse_arn_to_dimensions(arn)
        arn_region, arn_account_id = _resource_identity_from_arn(arn, fallback_region)
        region = raw.get("region") or arn_region
        account_id = raw.get("account_id") or arn_account_id
        if not namespace or not dimensions:
            unsupported.append(
                {
                    "arn": arn,
                    "region": region,
                    "account_id": account_id,
                    "reason": parser_info.get("note", "unknown_resource_type"),
                }
            )
            continue
        resources.append(
            {
                "arn": arn,
                "namespace": namespace,
                "dimensions": dimensions,
                "region": region,
                "account_id": account_id,
                "tags": raw.get("tags") if isinstance(raw.get("tags"), dict) else {},
                "parser_info": parser_info,
            }
        )

    metadata = {
        "source": source,
        "input_resource_count": len(raw_resources),
        "unique_supported_resource_count": len(resources),
        "unsupported_resource_count": len(unsupported),
        "duplicate_resource_count": duplicate_count,
        "truncated": inventory_truncated,
        "note": inventory_note,
        "complete": not inventory_truncated,
        "hard_cap": _COVERAGE_RESOURCE_HARD_CAP,
    }
    return resources, unsupported, metadata


def _coverage_catalogue_payload(
    namespace: str, recommended: dict[str, list[dict]]
) -> dict[str, dict[str, Any]]:
    """Return one recommendation copy per namespace/metric for compact batching."""
    payload: dict[str, dict[str, Any]] = {}
    for metric_name, recs in sorted(recommended.items()):
        metadata = recommendations.get_metric_metadata_from_catalogue(
            namespace, metric_name
        )
        payload[metric_name] = {
            "metric_description": (metadata or {}).get("description"),
            "recommendation": recs[0],
        }
    return payload


def _analyze_inventory_coverage(event: dict[str, Any]) -> dict[str, Any]:
    """Grade a bounded resource inventory with one posture load per region."""
    normalised = _normalise_coverage_inventory(event)
    if isinstance(normalised, dict):
        return normalised
    resources, unsupported, inventory_metadata = normalised

    posture_by_region: dict[str, dict[str, Any]] = {}
    for region in sorted({resource["region"] for resource in resources}):
        posture_event = {
            "region": region,
            "max_items": event.get("max_items", _POSTURE_HARD_MAX_ITEMS),
            "force_refresh": bool(event.get("force_refresh")),
        }
        posture_by_region[region] = _coverage_existing_alarms(posture_event)

    recommendations_by_namespace: dict[str, dict[str, list[dict]]] = {}
    catalogue_payload: dict[str, dict[str, dict[str, Any]]] = {}
    for namespace in sorted({resource["namespace"] for resource in resources}):
        recommended = recommendations.get_namespace_alarm_recommendations(namespace)
        recommendations_by_namespace[namespace] = recommended
        catalogue_payload[namespace] = _coverage_catalogue_payload(
            namespace, recommended
        )

    resource_results: list[dict[str, Any]] = []
    zero_alarm_resources: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {
        "covered": 0,
        "partial": 0,
        "no_matching_alarms": 0,
        "no_catalogue": 0,
        "inventory_incomplete": 0,
    }
    total_matching_alarms = 0
    verified_zero_alarm_count = 0

    for resource in resources:
        posture = posture_by_region[resource["region"]]
        recommended = recommendations_by_namespace[resource["namespace"]]
        grade = _grade_alarm_scope(
            resource["namespace"],
            resource["dimensions"],
            posture["alarms"],
            recommended,
            compact_missing=True,
        )
        total_matching_alarms += grade["matching_alarm_count"]

        completeness_notes: list[str] = []
        account_mismatch = bool(
            resource["account_id"]
            and posture["account_id"]
            and resource["account_id"] != posture["account_id"]
        )
        if posture["truncated"]:
            completeness_notes.append(
                posture["truncation_note"]
                or "Alarm posture is truncated; absent alarms are not definitive."
            )
        if account_mismatch:
            completeness_notes.append(
                "Resource account does not match the CloudWatch posture account."
            )

        if completeness_notes:
            status = "inventory_incomplete"
        elif not recommended:
            status = "no_catalogue"
        elif grade["matching_alarm_count"] == 0:
            status = "no_matching_alarms"
        elif grade["summary"]["missing_count"] == 0:
            status = "covered"
        else:
            status = "partial"
        status_counts[status] += 1

        result = {
            "arn": resource["arn"],
            "namespace": resource["namespace"],
            "dimensions": resource["dimensions"],
            "region": resource["region"],
            "account_id": resource["account_id"],
            "tags": resource["tags"],
            "status": status,
            "matching_alarm_count": grade["matching_alarm_count"],
            "implemented": grade["implemented"],
            "missing": grade["missing"],
            "drift": grade["drift"],
            "summary": grade["summary"],
            "alarm_inventory_complete": not completeness_notes,
            "completeness_notes": completeness_notes,
            "data_source": posture["data_source"],
            "data_as_of": posture["data_as_of"],
        }
        resource_results.append(result)
        if grade["matching_alarm_count"] == 0 and not completeness_notes:
            verified_zero_alarm_count += 1
            zero_alarm_resources.append(
                {
                    "arn": resource["arn"],
                    "namespace": resource["namespace"],
                    "region": resource["region"],
                    "account_id": resource["account_id"],
                    "catalogue_status": (
                        "available"
                        if recommended
                        else "no_recommendations_in_catalogue"
                    ),
                }
            )

    data_sources_by_region = {
        region: {
            "account_id": posture["account_id"],
            "data_source": posture["data_source"],
            "data_as_of": posture["data_as_of"],
            "freshness_note": posture["freshness_note"],
            "alarm_inventory_complete": not posture["truncated"],
            "truncated": posture["truncated"],
            "truncation_note": posture["truncation_note"],
            "returned_alarm_count": posture["returned_alarm_count"],
            "alarm_cap": posture["alarm_cap"],
        }
        for region, posture in sorted(posture_by_region.items())
    }
    alarm_inventories_complete = all(
        not posture["truncated"] for posture in posture_by_region.values()
    )

    return {
        "mode": "resource_inventory",
        "resource_inventory": inventory_metadata,
        "summary": {
            "input_resource_count": inventory_metadata["input_resource_count"],
            "supported_resource_count": len(resources),
            "unsupported_resource_count": len(unsupported),
            "duplicate_resource_count": inventory_metadata["duplicate_resource_count"],
            "covered_resource_count": status_counts["covered"],
            "partial_resource_count": status_counts["partial"],
            "zero_matching_alarm_resource_count": verified_zero_alarm_count,
            "no_catalogue_resource_count": status_counts["no_catalogue"],
            "inventory_incomplete_resource_count": status_counts[
                "inventory_incomplete"
            ],
            "matching_alarm_count": total_matching_alarms,
        },
        "resources": resource_results,
        "zero_matching_alarm_resources": zero_alarm_resources,
        "unsupported_resources": unsupported,
        "recommendations_by_namespace": catalogue_payload,
        "data_sources_by_region": data_sources_by_region,
        "coverage_complete": inventory_metadata["complete"]
        and alarm_inventories_complete
        and not unsupported,
        "coverage_notes": [
            note
            for note in (
                (
                    inventory_metadata.get("note")
                    if inventory_metadata["truncated"]
                    else None
                ),
                (
                    "One or more alarm inventories are truncated; resources in those "
                    "regions are inventory_incomplete and are not reported as zero-alarm."
                    if not alarm_inventories_complete
                    else None
                ),
                (
                    "Unsupported resource ARNs were excluded from alarm grading."
                    if unsupported
                    else None
                ),
                (
                    "Thresholds remain environment-specific; use analyse_metric before "
                    "creating any missing alarm."
                ),
            )
            if note
        ],
    }


def analyze_alarm_coverage(event: dict[str, Any]) -> dict[str, Any]:
    """Grade one scope or a bounded resource inventory against recommendations."""
    if (
        event.get("resource_inventory") is not None
        or event.get("resource_arns") is not None
    ):
        return _analyze_inventory_coverage(event)

    namespace = event.get("namespace")
    scope_dims = _to_cw_dimensions(event.get("dimensions"))
    resource_arn = event.get("resource_arn")
    if resource_arn:
        parsed_ns, parsed_dims, _info = arn_mod.parse_arn_to_dimensions(resource_arn)
        if not namespace:
            namespace = parsed_ns
        if not scope_dims:
            scope_dims = parsed_dims

    if not namespace:
        return {
            "error": "invalid_request",
            "message": (
                "Provide namespace/resource_arn for one-scope mode, or provide "
                "resource_inventory/resource_arns for bounded inventory mode."
            ),
        }

    recommended = recommendations.get_namespace_alarm_recommendations(namespace)
    if not recommended:
        return {
            "namespace": namespace,
            "note": "no_recommendations_in_catalogue",
            "implemented": [],
            "missing": [],
            "drift": [],
            "summary": {
                "recommended_metric_count": 0,
                "implemented_count": 0,
                "missing_count": 0,
            },
        }

    posture = _coverage_existing_alarms(event)
    grade = _grade_alarm_scope(namespace, scope_dims, posture["alarms"], recommended)
    return {
        "namespace": namespace,
        "scope_dimensions": scope_dims,
        "summary": grade["summary"],
        "implemented": grade["implemented"],
        "missing": grade["missing"],
        "drift": grade["drift"],
        "data_source": posture["data_source"],
        "data_as_of": posture["data_as_of"],
        "freshness_note": posture["freshness_note"],
        "alarm_inventory_complete": not posture["truncated"],
        "truncation_note": posture["truncation_note"],
        "coverage_note": (
            "Grades existing alarms against AWS recommended alarms for this "
            "namespace/resource. Thresholds for missing alarms are environment-specific "
            "— use analyse_metric to calibrate before creating them. If alarm_inventory_"
            "complete is false, absent alarms are not definitive."
        ),
    }


# ---------------------------------------------------------------------------
# Tool: get_alarm_history
# ---------------------------------------------------------------------------


def get_alarm_history(event: dict[str, Any]) -> dict[str, Any]:
    """cloudwatch:DescribeAlarmHistory wrapper.

    Defaults to a 24-hour lookback for state-update events when start_time is
    omitted.
    """
    alarm_name = event.get("alarm_name")
    if not alarm_name:
        return {"error": "invalid_request", "message": "alarm_name is required."}

    max_items = int(event.get("max_items", 50))
    end_time = _parse_time(event.get("end_time")) or _now_utc()
    start_time = _parse_time(event.get("start_time")) or (
        end_time - _DEFAULT_ALARM_HISTORY_LOOKBACK
    )

    params: dict[str, Any] = {
        "AlarmName": alarm_name,
        "StartDate": start_time,
        "EndDate": end_time,
        "MaxRecords": max_items,
        "ScanBy": "TimestampDescending",
    }
    history_item_type = event.get("history_item_type")
    if history_item_type:
        params["HistoryItemType"] = history_item_type

    client = _cw_client(event)
    resp = client.describe_alarm_history(**params)

    items = []
    for h in resp.get("AlarmHistoryItems", []):
        items.append(
            {
                "alarm_name": h.get("AlarmName"),
                "alarm_type": h.get("AlarmType"),
                "timestamp": _iso_or_none(h.get("Timestamp")),
                "history_item_type": h.get("HistoryItemType"),
                "history_summary": h.get("HistorySummary"),
            }
        )
    return {
        "alarm_name": alarm_name,
        "history_items": items,
        "count": len(items),
        "window": {"start": start_time.isoformat(), "end": end_time.isoformat()},
    }


# ---------------------------------------------------------------------------
# Tool: build_cfn_alarm
# ---------------------------------------------------------------------------


def build_cfn_alarm(event: dict[str, Any]) -> dict[str, Any]:
    """Thin pass-through to cfn.build_cfn_alarm — pure, no AWS call."""
    alarm_dict = event.get("alarm_dict") or {}
    dimensions = event.get("dimensions") or []
    threshold = event.get("threshold")
    sns_topic_arn = event.get("sns_topic_arn")
    tags = event.get("tags")
    return cfn.build_cfn_alarm(
        alarm_recommendation=alarm_dict,
        dimensions=dimensions,
        threshold=threshold,
        sns_topic_arn=sns_topic_arn,
        tags=tags,
    )


# ---------------------------------------------------------------------------
# Tool: assemble_cfn_template
# ---------------------------------------------------------------------------


def assemble_cfn_template(event: dict[str, Any]) -> dict[str, Any]:
    """Thin pass-through to cfn.assemble_cfn_template.

    The worker captures a successful structured result at the MCP boundary
    and delivers the template through the internal artifact pipeline. The
    model receives a compact acknowledgement rather than the YAML body.
    """
    alarms = event.get("alarms") or []
    sns_topic_arn = event.get("sns_topic_arn")
    tags = event.get("tags")
    return cfn.assemble_cfn_template(
        alarms=alarms,
        sns_topic_arn=sns_topic_arn,
        tags=tags,
    )


# ---------------------------------------------------------------------------
# Snapshot-backed inventory, coverage, deployment, and tuning tools
# ---------------------------------------------------------------------------


def _snapshot_table():
    return boto3.resource("dynamodb").Table(_SNAPSHOT_TABLE)


def _trigger_refresh() -> dict[str, Any]:
    if not _COORDINATOR_FUNCTION:
        return {"status": "not_configured"}
    try:
        boto3.client("lambda").invoke(
            FunctionName=_COORDINATOR_FUNCTION,
            InvocationType="Event",
            Payload=json.dumps({"force_refresh": True, "on_demand": True}).encode(),
        )
        return {"status": "requested"}
    except Exception as exc:  # noqa: BLE001 - reads remain available
        logger.warning("Could not start CloudWatch snapshot refresh: %s", exc)
        return {"status": "failed", "message": str(exc)}


def _current_snapshot(account_id: str) -> dict[str, Any] | None:
    if not _SNAPSHOT_TABLE:
        return None
    return (
        _snapshot_table()
        .get_item(Key={"pk": f"ACCOUNT#{account_id}", "sk": "CURRENT"})
        .get("Item")
    )


def _snapshot_context(event: dict[str, Any]) -> dict[str, Any]:
    account_id = _snapshot_account_id()
    requested_account = event.get("account_id") or account_id
    if requested_account != account_id:
        return {
            "error": "account_mismatch",
            "message": "Queries are limited to the configured target account.",
        }
    current = _current_snapshot(account_id)
    refresh = None
    if event.get("force_refresh") or current is None:
        refresh = _trigger_refresh()
    if current is None:
        return {
            "error": "snapshot_unavailable",
            "account_id": account_id,
            "refresh": refresh,
        }
    state = snapshot_domain.freshness(current["collected_at"])
    if current.get("catalogue_version") != CATALOGUE_VERSION:
        state = {
            **state,
            "state": "catalogue_mismatch",
            "servable": False,
            "refresh_required": True,
        }
    if state["refresh_required"] and refresh is None:
        refresh = _trigger_refresh()
    if not state["servable"]:
        return {
            "error": "snapshot_unusable",
            "account_id": account_id,
            "snapshot_id": current.get("snapshot_id"),
            "freshness": state,
            "refresh": refresh,
        }
    return {
        "account_id": account_id,
        "current": current,
        "freshness": state,
        "refresh": refresh,
    }


def _query_snapshot_region(
    account_id: str, snapshot_id: str, region: str, prefix: str
) -> list[dict[str, Any]]:
    table = _snapshot_table()
    pk = f"SNAPSHOT#{account_id}#{snapshot_id}#{region}"
    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("pk").eq(pk) & Key("sk").begins_with(prefix)
    }
    while True:
        page = table.query(**kwargs)
        items.extend(page.get("Items", []))
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            return items
        kwargs["ExclusiveStartKey"] = last_key


def _load_snapshot_rows(
    context: dict[str, Any], prefix: str, regions: list[str] | None = None
) -> list[dict[str, Any]]:
    current = context["current"]
    selected = regions or list(current["regions"])
    with ThreadPoolExecutor(max_workers=min(16, max(1, len(selected)))) as executor:
        pages = executor.map(
            lambda region: _query_snapshot_region(
                context["account_id"], current["snapshot_id"], region, prefix
            ),
            selected,
        )
        return [item for page in pages for item in page]


def _page_rows(
    rows: list[dict[str, Any]],
    event: dict[str, Any],
    snapshot_id: str,
    query: dict[str, Any],
) -> dict[str, Any]:
    digest = snapshot_domain.query_hash(query)
    offset = 0
    cursor = event.get("cursor")
    if cursor:
        try:
            offset = snapshot_domain.decode_cursor(
                cursor, snapshot_id, digest, _CURSOR_SECRET
            )
        except ValueError:
            return {"error": "invalid_cursor"}
    page_size = snapshot_domain.bounded_page_size(event.get("page_size"))
    page = rows[offset : offset + page_size]
    next_offset = offset + len(page)
    next_cursor = (
        snapshot_domain.encode_cursor(snapshot_id, digest, next_offset, _CURSOR_SECRET)
        if next_offset < len(rows)
        else None
    )
    return {
        "items": page,
        "returned": len(page),
        "total": len(rows),
        "next_cursor": next_cursor,
        "page_size": page_size,
    }


def query_alarm_inventory(event: dict[str, Any]) -> dict[str, Any]:
    context = _snapshot_context(event)
    if context.get("error"):
        return context
    regions = event.get("regions")
    if regions is not None and not isinstance(regions, list):
        return {"error": "invalid_request", "message": "regions must be an array"}
    rows = _load_snapshot_rows(context, "ALARM#", regions)
    alarm_type = event.get("alarm_type")
    namespace = event.get("namespace")
    names = set(event.get("alarm_names") or [])
    rows = [
        row
        for row in rows
        if (not alarm_type or row.get("alarm_type") == alarm_type)
        and (not namespace or row.get("namespace") == namespace)
        and (not names or row.get("alarm_name") in names)
    ]
    rows.sort(key=lambda row: (row.get("region", ""), row.get("alarm_name", "")))
    query = {
        "regions": regions,
        "alarm_type": alarm_type,
        "namespace": namespace,
        "alarm_names": sorted(names),
    }
    page = _page_rows(rows, event, context["current"]["snapshot_id"], query)
    if page.get("error"):
        return page
    return {
        "account_id": context["account_id"],
        "snapshot_id": context["current"]["snapshot_id"],
        "source": "scheduled_snapshot",
        "freshness": context["freshness"],
        "completeness": context["current"]["completeness"],
        "refresh": context["refresh"],
        "alarms": page.pop("items"),
        **page,
    }


def _live_alarms(region: str) -> list[dict[str, Any]]:
    client = get_aws_client("cloudwatch", region_name=region, role_alias="CLOUDWATCH")
    result = []
    for page in client.get_paginator("describe_alarms").paginate(
        AlarmTypes=["MetricAlarm", "CompositeAlarm"]
    ):
        result.extend(
            normalize_domain.normalize_alarm(alarm, region)
            for alarm in page.get("MetricAlarms", [])
        )
        result.extend(
            normalize_domain.normalize_alarm(alarm, region)
            for alarm in page.get("CompositeAlarms", [])
        )
    return result


def _live_resource_coverage(resource_arns: list[str]) -> dict[str, Any]:
    if len(resource_arns) > 100:
        return {
            "error": "resource_limit_exceeded",
            "message": "Explicit live fallback accepts at most 100 resource ARNs.",
        }
    profiles = [
        normalize_domain.resource_profile(
            arn, region=os.environ.get("AWS_REGION", "us-east-1")
        )
        for arn in dict.fromkeys(resource_arns)
    ]
    alarms_by_region = {
        region: _live_alarms(region)
        for region in sorted(
            {
                profile["metric_region"]
                for profile in profiles
                if profile["metric_region"]
            }
        )
    }
    resources = [
        coverage_domain.evaluate_resource(
            profile,
            alarms_by_region.get(profile["metric_region"], []),
            resource_inventory_complete=True,
            alarm_inventory_complete=True,
        )
        for profile in profiles
    ]
    for resource in resources:
        resource.pop("candidates", None)
    return {
        "mode": "resources",
        "source": "live_fallback",
        "freshness": {"state": "live", "age_seconds": 0},
        "completeness": {"complete": True},
        "resources": resources,
    }


def analyze_alarm_coverage_snapshot(event: dict[str, Any]) -> dict[str, Any]:
    mode = event.get("mode", "account")
    if mode not in {"account", "tags", "resources"}:
        return {
            "error": "invalid_request",
            "message": "mode must be account, tags, or resources",
        }
    resource_arns = event.get("resource_arns") or event.get("resources") or []
    if mode == "resources" and not isinstance(resource_arns, list):
        return {"error": "invalid_request", "message": "resources must be an array"}

    context = _snapshot_context(event)
    if context.get("error"):
        if mode == "resources" and resource_arns:
            return _live_resource_coverage(resource_arns)
        return context
    rows = _load_snapshot_rows(context, "RESOURCE#", event.get("regions"))
    tags = event.get("tags") or {}
    if mode == "tags":
        if not isinstance(tags, dict) or not tags:
            return {
                "error": "invalid_request",
                "message": "tags are required in tags mode",
            }
        rows = [
            row
            for row in rows
            if snapshot_domain.matches_tags(row.get("tags", {}), tags)
        ]
    if mode == "resources":
        if len(resource_arns) > 100:
            return {
                "error": "resource_limit_exceeded",
                "message": "At most 100 resource ARNs are accepted.",
            }
        wanted = set(resource_arns)
        rows = [row for row in rows if row.get("arn") in wanted]
    statuses = set(event.get("statuses") or [])
    if statuses:
        rows = [row for row in rows if row.get("coverage_status") in statuses]
    rows.sort(key=lambda row: (row.get("region", ""), row.get("arn", "")))
    query = {
        "mode": mode,
        "tags": tags,
        "resources": sorted(resource_arns),
        "regions": event.get("regions"),
        "statuses": sorted(statuses),
    }
    page = _page_rows(rows, event, context["current"]["snapshot_id"], query)
    if page.get("error"):
        return page
    summary: dict[str, int] = {}
    for row in rows:
        status = row.get("coverage_status", "unknown")
        summary[status] = summary.get(status, 0) + 1
    return {
        "mode": mode,
        "account_id": context["account_id"],
        "snapshot_id": context["current"]["snapshot_id"],
        "source": "scheduled_snapshot",
        "freshness": context["freshness"],
        "completeness": context["current"]["completeness"],
        "refresh": context["refresh"],
        "summary": {"resource_count": len(rows), "by_status": summary},
        "resources": page.pop("items"),
        **page,
    }


def get_alarm_snapshot_status(event: dict[str, Any]) -> dict[str, Any]:
    account_id = _snapshot_account_id()
    if event.get("account_id") not in (None, "", account_id):
        return {"error": "account_mismatch"}
    current = _current_snapshot(account_id)
    if not current:
        return {
            "account_id": account_id,
            "current": None,
            "refresh": _trigger_refresh() if event.get("force_refresh") else None,
        }
    refresh = (
        _snapshot_table()
        .get_item(Key={"pk": f"ACCOUNT#{account_id}", "sk": "REFRESH"})
        .get("Item")
    )
    refresh_run_id = (refresh or {}).get("run_id") or current["run_id"]
    run = (
        _snapshot_table()
        .get_item(Key={"pk": f"RUN#{refresh_run_id}", "sk": "META"})
        .get("Item")
    )
    return {
        "account_id": account_id,
        "current": current,
        "freshness": snapshot_domain.freshness(current["collected_at"]),
        "run": run,
        "refresh_run": refresh,
        "refresh_request": _trigger_refresh() if event.get("force_refresh") else None,
    }


def _selected_candidates(
    context: dict[str, Any], candidate_ids: list[str]
) -> list[dict[str, Any]]:
    table = _snapshot_table()
    found: dict[str, dict[str, Any]] = {}
    for region in context["current"]["regions"]:
        pk = (
            f"SNAPSHOT#{context['account_id']}#"
            f"{context['current']['snapshot_id']}#{region}"
        )
        for candidate_id in candidate_ids:
            item = table.get_item(
                Key={"pk": pk, "sk": f"CANDIDATE#{candidate_id}"}
            ).get("Item")
            if item:
                found[candidate_id] = item
    return [
        found[candidate_id] for candidate_id in candidate_ids if candidate_id in found
    ]


def _calibrate_candidates(
    candidates: list[dict[str, Any]], overrides: dict[str, Any]
) -> tuple[dict[str, float], list[dict[str, Any]]]:
    thresholds: dict[str, float] = {}
    blocked: list[dict[str, Any]] = []
    now = _now_utc()
    by_region: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        candidate_id = candidate["candidate_id"]
        if candidate_id in overrides:
            thresholds[candidate_id] = float(overrides[candidate_id])
        elif candidate["threshold_strategy"]["type"] == "fixed":
            thresholds[candidate_id] = float(candidate["threshold_strategy"]["value"])
        else:
            by_region.setdefault(candidate["region"], []).append(candidate)

    for region, regional in by_region.items():
        client = get_aws_client(
            "cloudwatch", region_name=region, role_alias="CLOUDWATCH"
        )
        queries = []
        by_query: dict[str, dict[str, Any]] = {}
        for index, candidate in enumerate(regional):
            query_id = f"m{index}"
            recommendation = candidate["recommendation"]
            queries.append(
                {
                    "Id": query_id,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": candidate["namespace"],
                            "MetricName": candidate["metric_name"],
                            "Dimensions": candidate["dimensions"],
                        },
                        "Period": int(recommendation.get("period", 300)),
                        "Stat": recommendation.get("statistic", "Average"),
                    },
                    "ReturnData": True,
                }
            )
            by_query[query_id] = candidate
        for offset in range(0, len(queries), 500):
            response = client.get_metric_data(
                MetricDataQueries=queries[offset : offset + 500],
                StartTime=now - _dt.timedelta(days=14),
                EndTime=now,
                ScanBy="TimestampAscending",
            )
            for result in response.get("MetricDataResults", []):
                candidate = by_query[result["Id"]]
                values = list(result.get("Values", []))
                timestamps = list(result.get("Timestamps", []))
                analysed = analysis.analyse_metric_data(values, timestamps)
                if analysed.get("error"):
                    blocked.append(
                        {
                            "candidate_id": candidate["candidate_id"],
                            "reason": "insufficient_history",
                            "datapoints": len(values),
                        }
                    )
                    continue
                operator = candidate["recommendation"].get("comparisonOperator", "")
                stats = analysed["stats"]
                thresholds[candidate["candidate_id"]] = float(
                    stats["p50"] if operator.startswith("Less") else stats["p99"]
                )
    return thresholds, blocked


def prepare_alarm_deployment(event: dict[str, Any]) -> dict[str, Any]:
    snapshot_id = event.get("snapshot_id")
    candidate_ids = event.get("candidate_ids") or []
    sns_topic_arn = event.get("sns_topic_arn")
    if not snapshot_id or not candidate_ids or not sns_topic_arn:
        return {
            "error": "invalid_request",
            "message": "snapshot_id, candidate_ids, and sns_topic_arn are required.",
        }
    if len(candidate_ids) > 100:
        return {"error": "candidate_limit_exceeded"}
    context = _snapshot_context(event)
    if context.get("error"):
        return context
    if context["current"]["snapshot_id"] != snapshot_id:
        return {
            "error": "snapshot_replaced",
            "current_snapshot_id": context["current"]["snapshot_id"],
        }
    candidates = _selected_candidates(context, list(dict.fromkeys(candidate_ids)))
    if len(candidates) != len(set(candidate_ids)):
        return {"error": "candidate_not_found"}

    live_by_region = {
        region: _live_alarms(region)
        for region in sorted({candidate["region"] for candidate in candidates})
    }
    deployable = []
    excluded = []
    for candidate in candidates:
        profile = normalize_domain.resource_profile(
            candidate["resource_arn"], region=candidate["region"]
        )
        evaluated = coverage_domain.evaluate_resource(
            profile,
            live_by_region[candidate["region"]],
            resource_inventory_complete=True,
            alarm_inventory_complete=True,
        )
        current = next(
            (
                item
                for item in evaluated["candidates"]
                if item["recommendation_id"] == candidate["recommendation_id"]
            ),
            None,
        )
        if current and current["status"] in {"implemented", "implemented_with_drift"}:
            excluded.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "reason": "already_implemented",
                    "matched_alarm_ids": current.get("matched_alarm_ids", []),
                }
            )
        else:
            deployable.append(candidate)

    thresholds, blocked = _calibrate_candidates(
        deployable, event.get("threshold_overrides") or {}
    )
    blocked_ids = {item["candidate_id"] for item in blocked}
    alarm_specs = []
    for candidate in deployable:
        if candidate["candidate_id"] in blocked_ids:
            continue
        alarm_specs.append(
            {
                "alarm_dict": candidate["recommendation"],
                "dimensions": candidate["dimensions"],
                "threshold": thresholds[candidate["candidate_id"]],
            }
        )
    artifact = cfn.assemble_cfn_template(
        alarms=alarm_specs,
        sns_topic_arn=sns_topic_arn,
        tags=event.get("tags"),
    )
    return {
        **artifact,
        "snapshot_id": snapshot_id,
        "selected_count": len(candidate_ids),
        "deployment_count": len(alarm_specs),
        "excluded": excluded,
        "blocked": blocked,
        "revalidated_at": _now_utc().isoformat(),
    }


def analyze_alarm_tuning(event: dict[str, Any]) -> dict[str, Any]:
    alarm_name = event.get("alarm_name")
    region = event.get("region") or os.environ.get("AWS_REGION", "us-east-1")
    if not alarm_name:
        return {"error": "invalid_request", "message": "alarm_name is required."}
    client = get_aws_client("cloudwatch", region_name=region, role_alias="CLOUDWATCH")
    response = client.describe_alarms(AlarmNames=[alarm_name])
    raw = (response.get("MetricAlarms") or [None])[0]
    if raw is None:
        return {"error": "alarm_not_found", "alarm_name": alarm_name}
    normalized = normalize_domain.normalize_alarm(raw, region)
    history = get_alarm_history({**event, "alarm_name": alarm_name, "region": region})
    baseline = None
    if normalized.get("namespace") and normalized.get("metric_name"):
        baseline = analyse_metric(
            {
                "namespace": normalized["namespace"],
                "metric_name": normalized["metric_name"],
                "dimensions": normalized["dimensions"],
                "statistic": normalized["statistic"],
                "period": normalized["period"] or 300,
                "lookback_days": event.get("lookback_days", 14),
                "region": region,
            }
        )
    return {
        "alarm": normalized,
        "history": history,
        "metric_baseline": baseline,
        "source": "live_api",
    }


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_TOOL_HANDLERS: dict[str, Any] = {
    "get_metric_data": get_metric_data,
    "get_metric_metadata": get_metric_metadata,
    "get_recommended_metric_alarms": get_recommended_metric_alarms,
    "analyse_metric": analyse_metric,
    "get_active_alarms": get_active_alarms,
    "get_alarm_posture": get_alarm_posture,
    "query_alarm_inventory": query_alarm_inventory,
    "analyze_alarm_coverage": analyze_alarm_coverage_snapshot,
    "get_alarm_snapshot_status": get_alarm_snapshot_status,
    "prepare_alarm_deployment": prepare_alarm_deployment,
    "analyze_alarm_tuning": analyze_alarm_tuning,
    "get_alarm_history": get_alarm_history,
    "build_cfn_alarm": build_cfn_alarm,
    "assemble_cfn_template": assemble_cfn_template,
}


def handler(event, context):
    """Gateway entrypoint — split the tool name and dispatch."""
    extended_tool_name = context.client_context.custom["bedrockAgentCoreToolName"]
    tool_name = extended_tool_name.split("___")[1]
    logger.info("cloudwatch invoke: tool=%s", tool_name)

    fn = _TOOL_HANDLERS.get(tool_name)
    if fn is None:
        return {
            "error": "unknown_tool",
            "available_tools": list(_TOOL_HANDLERS.keys()),
        }
    try:
        return fn(event or {})
    except Exception as exc:  # noqa: BLE001 — surface the error to the agent
        logger.exception("Unhandled error in %s", tool_name)
        return {"error": f"{tool_name} failed: {exc}"}
