"""
CloudWatch MCP Lambda — Gateway-facing handler.

Routing contract (shared with every MCP Lambda on the gateway):
    context.client_context.custom["bedrockAgentCoreToolName"] = "<target>___<tool_name>"
    event body = tool params (NOT wrapped)

Seven tools, all sync (boto3 calls are blocking; no asyncio):

  * get_metric_data              — cloudwatch:GetMetricData wrapper. Supports
                                   percentile stats (p50/p99/...) and metric-math
                                   `queries` arrays.
  * get_metric_metadata          — vendored catalogue lookup. No AWS call.
  * get_recommended_metric_alarms — vendored catalogue lookup; optional
                                   resource_arn → dimensions via the ARN parser.
                                   No AWS call.
  * analyse_metric               — cloudwatch:GetMetricData over a 14-day window,
                                   fed into the pure numpy/pandas analysis.
  * get_active_alarms            — cloudwatch:DescribeAlarms filtered to ALARM.
  * get_alarm_history            — cloudwatch:DescribeAlarmHistory.
  * build_cfn_alarm              — pure dict assembler. No AWS call.

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
import logging
from typing import Any

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
    import os

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

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Default lookback for get_metric_data when start_time is omitted (Requirement 1.2).
_DEFAULT_METRIC_LOOKBACK = _dt.timedelta(hours=3)
# Default lookback for analyse_metric (Requirement 1.2).
_DEFAULT_ANALYSE_LOOKBACK_DAYS = 14
# Default lookback for get_alarm_history when start_time is omitted (Requirement 1.2).
_DEFAULT_ALARM_HISTORY_LOOKBACK = _dt.timedelta(hours=24)


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
    """Vendored catalogue lookup — no AWS call."""
    namespace = event.get("namespace")
    metric_name = event.get("metric_name")
    if not namespace or not metric_name:
        return {
            "error": "invalid_request",
            "message": "namespace and metric_name are required.",
        }
    entry = recommendations.get_metric_metadata_from_catalogue(namespace, metric_name)
    if entry is None:
        return {
            "namespace": namespace,
            "metric_name": metric_name,
            "metadata": None,
            "note": "not_in_catalogue",
        }
    return {
        "namespace": namespace,
        "metric_name": metric_name,
        "metadata": entry,
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

    The agent calls this ONCE per request with all agreed alarms and places
    the returned `template_yaml` verbatim inside the artifact panel — see
    spec Requirement 7. The YAML is built deterministically here in Python
    (not by the model) so the artifact body always contains the complete
    template, never a structural excerpt.
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
# Dispatch
# ---------------------------------------------------------------------------

_TOOL_HANDLERS: dict[str, Any] = {
    "get_metric_data": get_metric_data,
    "get_metric_metadata": get_metric_metadata,
    "get_recommended_metric_alarms": get_recommended_metric_alarms,
    "analyse_metric": analyse_metric,
    "get_active_alarms": get_active_alarms,
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
