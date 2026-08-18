"""Unit tests for src/lambda/mcp/cloudwatch/handler.py.

Verifies:
  * Tool dispatch routing via the bedrockAgentCoreToolName "<target>___<tool>" split.
  * Unknown-tool error shape.
  * get_recommended_metric_alarms with resource_arn invokes the ARN parser
    and uses the parsed namespace + dimensions.
  * analyse_metric reverses boto3's most-recent-first arrays to chronological
    order and merges the metric + window metadata blocks.
  * build_cfn_alarm passes through to cfn.build_cfn_alarm (SNS injection).
  * get_active_alarms / get_alarm_history call boto3 through the mocked client.

The cloudwatch Lambda source dir isn't on sys.path by default, and we keep it
that way: handler.py resolves its sibling modules (recommendations/analysis/
cfn/arn) by file path under cloudwatch-namespaced names rather than via a
sys.path insert, so it never shadows other Lambdas' bare `handler.py`. We only
add src/lambda/mcp so `from shared.cross_account import get_aws_client`
resolves. The module is loaded under a unique name to avoid colliding with
other handler.py modules under src/lambda/mcp/.
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MCP_DIR = _REPO_ROOT / "src" / "lambda" / "mcp"
_CW_DIR = _MCP_DIR / "cloudwatch"
_HANDLER_PATH = _CW_DIR / "handler.py"

# `from shared.cross_account import get_aws_client` needs the mcp dir on path.
# Do NOT add _CW_DIR — that would put a bare `handler.py` on sys.path and
# shadow other Lambdas' `import handler` for the rest of the session.
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

_spec = importlib.util.spec_from_file_location("cloudwatch_handler", _HANDLER_PATH)
handler_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler_mod)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(tool_name: str) -> SimpleNamespace:
    """Build a fake Lambda context carrying the gateway tool name."""
    return SimpleNamespace(
        client_context=SimpleNamespace(
            custom={"bedrockAgentCoreToolName": f"cloudwatch___{tool_name}"}
        )
    )


@pytest.fixture
def mock_cw(monkeypatch):
    """Patch handler.get_aws_client to return a MagicMock cloudwatch client."""
    client = MagicMock()
    monkeypatch.setattr(handler_mod, "get_aws_client", lambda **kwargs: client)
    return client


# ---------------------------------------------------------------------------
# Dispatch routing
# ---------------------------------------------------------------------------


def test_unknown_tool_returns_error():
    result = handler_mod.handler({}, _ctx("does_not_exist"))
    assert result["error"] == "unknown_tool"
    assert "get_metric_data" in result["available_tools"]
    assert "assemble_cfn_template" in result["available_tools"]
    assert "analyze_alarm_coverage" in result["available_tools"]
    assert "get_metric_metadata" in result["available_tools"]
    assert len(result["available_tools"]) == 14


def test_dispatch_routes_to_named_tool(monkeypatch):
    sentinel = {"ok": True}
    monkeypatch.setitem(
        handler_mod._TOOL_HANDLERS, "get_metric_data", lambda e: sentinel
    )
    result = handler_mod.handler({"namespace": "AWS/Lambda"}, _ctx("get_metric_data"))
    assert result is sentinel


def test_dispatch_surfaces_exceptions_as_error(monkeypatch):
    def _boom(_event):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(handler_mod._TOOL_HANDLERS, "get_active_alarms", _boom)
    result = handler_mod.handler({}, _ctx("get_active_alarms"))
    assert result["error"] == "get_active_alarms failed: kaboom"


def test_packaged_cloudwatch_handler_imports_from_lambda_root(tmp_path):
    """The flattened Lambda zip must import shared domain modules from /var/task."""
    package_root = tmp_path / "task"
    package_root.mkdir()
    for source in _CW_DIR.glob("*.py"):
        (package_root / source.name).write_bytes(source.read_bytes())

    shared_source = _MCP_DIR / "shared"
    shared_target = package_root / "shared"
    shared_target.symlink_to(shared_source, target_is_directory=True)

    completed = subprocess.run(
        [sys.executable, "-c", "import handler"],
        cwd=package_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


# ---------------------------------------------------------------------------
# get_recommended_metric_alarms (catalogue, no AWS)
# ---------------------------------------------------------------------------


def test_get_recommended_metric_alarms_with_resource_arn_parses_dimensions():
    """resource_arn drives both namespace and dimensions via the ARN parser."""
    arn = "arn:aws:lambda:us-east-1:123456789012:function:my-fn"
    result = handler_mod.get_recommended_metric_alarms(
        {"resource_arn": arn, "metric_name": "Errors"}
    )
    assert result["namespace"] == "AWS/Lambda"
    assert result["dimensions"] == [{"Name": "FunctionName", "Value": "my-fn"}]
    assert isinstance(result["recommendations"], list)


def test_get_recommended_metric_alarms_catalogue_miss_note():
    result = handler_mod.get_recommended_metric_alarms(
        {"namespace": "AWS/Nonexistent", "metric_name": "Nope"}
    )
    assert result["recommendations"] == []
    assert result["note"] == "no_recommendations_in_catalogue"


# ---------------------------------------------------------------------------
# analyse_metric — reversal + metadata merge
# ---------------------------------------------------------------------------


def test_analyse_metric_reverses_timestamps_and_merges_metadata(mock_cw, monkeypatch):
    captured = {}

    def _fake_analyse(values, timestamps):
        captured["values"] = values
        captured["timestamps"] = timestamps
        return {"stats": {"mean": 1.0}, "trend": "NONE", "seasonality_seconds": None}

    monkeypatch.setattr(handler_mod.analysis, "analyse_metric_data", _fake_analyse)

    # boto3 returns most-recent-first: t2 before t1.
    t1 = _dt.datetime(2024, 1, 1, 0, 0, tzinfo=_dt.timezone.utc)
    t2 = _dt.datetime(2024, 1, 1, 0, 5, tzinfo=_dt.timezone.utc)
    mock_cw.get_metric_data.return_value = {
        "MetricDataResults": [
            {"Id": "m1", "Timestamps": [t2, t1], "Values": [20.0, 10.0]}
        ]
    }

    result = handler_mod.analyse_metric(
        {
            "namespace": "AWS/Lambda",
            "metric_name": "Duration",
            "dimensions": [{"Name": "FunctionName", "Value": "my-fn"}],
            "statistic": "Average",
        }
    )

    # Reversed to chronological order before analysis.
    assert captured["values"] == [10.0, 20.0]
    assert captured["timestamps"] == [t1, t2]
    # Metadata merged in.
    assert result["metric"]["namespace"] == "AWS/Lambda"
    assert result["window"]["datapoints"] == 2
    assert result["stats"]["mean"] == 1.0


def test_analyse_metric_passes_through_insufficient_history(mock_cw, monkeypatch):
    monkeypatch.setattr(
        handler_mod.analysis,
        "analyse_metric_data",
        lambda v, t: {"error": "insufficient_history", "datapoints_found": 2},
    )
    mock_cw.get_metric_data.return_value = {
        "MetricDataResults": [{"Id": "m1", "Timestamps": [], "Values": []}]
    }
    result = handler_mod.analyse_metric(
        {"namespace": "AWS/Lambda", "metric_name": "Duration"}
    )
    assert result["error"] == "insufficient_history"
    assert result["metric"]["metric_name"] == "Duration"
    assert "window" in result


# ---------------------------------------------------------------------------
# build_cfn_alarm — pass-through to cfn module
# ---------------------------------------------------------------------------


def test_build_cfn_alarm_passes_through_and_injects_sns():
    sns = "arn:aws:sns:us-east-1:123456789012:my-alarms"
    result = handler_mod.build_cfn_alarm(
        {
            "alarm_dict": {
                "namespace": "AWS/Lambda",
                "metric_name": "Errors",
                "comparisonOperator": "GreaterThanThreshold",
                "statistic": "Sum",
                "period": 60,
                "evaluationPeriods": 5,
                "datapointsToAlarm": 5,
                "treatMissingData": "notBreaching",
            },
            "dimensions": [{"Name": "FunctionName", "Value": "my-fn"}],
            "threshold": 5,
            "sns_topic_arn": sns,
        }
    )
    assert result["Type"] == "AWS::CloudWatch::Alarm"
    assert result["Properties"]["AlarmActions"] == [sns]


def test_build_cfn_alarm_invalid_sns():
    result = handler_mod.build_cfn_alarm(
        {"alarm_dict": {}, "dimensions": [], "threshold": 1, "sns_topic_arn": "bad"}
    )
    assert result["error"] == "invalid_sns_topic_arn"


# ---------------------------------------------------------------------------
# get_active_alarms / get_alarm_history — boto3 wrappers
# ---------------------------------------------------------------------------


def test_get_active_alarms_filters_and_summarizes(mock_cw):
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {
            "MetricAlarms": [
                {
                    "AlarmName": "a1",
                    "Namespace": "AWS/Lambda",
                    "MetricName": "Errors",
                    "Dimensions": [{"Name": "FunctionName", "Value": "fn"}],
                    "Statistic": "Sum",
                    "ComparisonOperator": "GreaterThanThreshold",
                    "Threshold": 5.0,
                    "StateValue": "ALARM",
                }
            ],
            "CompositeAlarms": [
                {"AlarmName": "c1", "AlarmRule": "ALARM(a1)", "StateValue": "ALARM"}
            ],
        }
    ]
    mock_cw.get_paginator.return_value = paginator

    result = handler_mod.get_active_alarms({"max_items": 10})

    mock_cw.get_paginator.assert_called_once_with("describe_alarms")
    _, kwargs = paginator.paginate.call_args
    assert kwargs["StateValue"] == "ALARM"
    assert result["metric_alarms"][0]["alarm_name"] == "a1"
    assert result["composite_alarms"][0]["alarm_name"] == "c1"


def test_get_alarm_history_requires_name(mock_cw):
    result = handler_mod.get_alarm_history({})
    assert result["error"] == "invalid_request"


def test_get_alarm_history_calls_boto3(mock_cw):
    mock_cw.describe_alarm_history.return_value = {
        "AlarmHistoryItems": [
            {
                "AlarmName": "a1",
                "Timestamp": _dt.datetime(2024, 1, 1, tzinfo=_dt.timezone.utc),
                "HistoryItemType": "StateUpdate",
                "HistorySummary": "Alarm updated from OK to ALARM",
            }
        ]
    }
    result = handler_mod.get_alarm_history({"alarm_name": "a1"})
    assert result["count"] == 1
    assert result["history_items"][0]["alarm_name"] == "a1"
    _, kwargs = mock_cw.describe_alarm_history.call_args
    assert kwargs["AlarmName"] == "a1"


# ---------------------------------------------------------------------------
# get_metric_data — single-metric build + percentile passthrough
# ---------------------------------------------------------------------------


def test_get_metric_data_builds_single_query_with_percentile(mock_cw):
    mock_cw.get_metric_data.return_value = {
        "MetricDataResults": [
            {"Id": "m1", "Label": "p99", "Timestamps": [], "Values": []}
        ]
    }
    handler_mod.get_metric_data(
        {
            "namespace": "AWS/Lambda",
            "metric_name": "Duration",
            "dimensions": [{"Name": "FunctionName", "Value": "fn"}],
            "stat": "p99",
        }
    )
    _, kwargs = mock_cw.get_metric_data.call_args
    mdq = kwargs["MetricDataQueries"][0]
    assert mdq["MetricStat"]["Stat"] == "p99"
    assert mdq["MetricStat"]["Metric"]["Namespace"] == "AWS/Lambda"


def test_get_metric_data_requires_query_or_metric(mock_cw):
    result = handler_mod.get_metric_data({})
    assert result["error"] == "invalid_request"


# ---------------------------------------------------------------------------
# assemble_cfn_template — dispatch + pass-through
# ---------------------------------------------------------------------------


def _alarm_entry() -> dict:
    return {
        "alarm_dict": {
            "namespace": "AWS/Lambda",
            "metric_name": "Errors",
            "comparisonOperator": "GreaterThanThreshold",
            "statistic": "Sum",
            "period": 60,
            "evaluationPeriods": 5,
            "datapointsToAlarm": 5,
            "treatMissingData": "notBreaching",
        },
        "dimensions": [{"Name": "FunctionName", "Value": "fn-a"}],
        "threshold": 5,
    }


def test_assemble_cfn_template_returns_template_yaml():
    sns = "arn:aws:sns:us-east-1:123456789012:my-alarms"
    result = handler_mod.assemble_cfn_template(
        {"alarms": [_alarm_entry()], "sns_topic_arn": sns}
    )
    assert "template_yaml" in result
    assert "summary" in result
    assert result["summary"]["alarm_count"] == 1
    # Verbatim YAML must contain the SNS ARN and the metric name.
    assert sns in result["template_yaml"]
    assert "Errors" in result["template_yaml"]


def test_assemble_cfn_template_dispatch_via_handler():
    sns = "arn:aws:sns:us-east-1:123456789012:my-alarms"
    result = handler_mod.handler(
        {"alarms": [_alarm_entry()], "sns_topic_arn": sns},
        _ctx("assemble_cfn_template"),
    )
    assert "template_yaml" in result


def test_assemble_cfn_template_invalid_sns():
    result = handler_mod.assemble_cfn_template(
        {"alarms": [_alarm_entry()], "sns_topic_arn": "bad"}
    )
    assert result["error"] == "invalid_sns_topic_arn"
    assert "template_yaml" not in result


# ---------------------------------------------------------------------------
# get_alarm_posture — scheduled snapshot with live fallback
# ---------------------------------------------------------------------------


def _snapshot_summary_item(
    payload: dict, snapshot_at: str = "2026-08-11T00:00:00+00:00"
):
    import json

    return {
        "payload": {"S": json.dumps(payload)},
        "snapshot_at": {"S": snapshot_at},
    }


def _snapshot_alarm_item(payload: dict):
    import json

    return {"payload": {"S": json.dumps(payload)}}


def test_get_alarm_posture_uses_unfiltered_describe_alarms(mock_cw, monkeypatch):
    monkeypatch.setattr(handler_mod, "_snapshot_account_id", lambda: "123456789012")
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {
            "MetricAlarms": [
                {
                    "AlarmName": "errors",
                    "AlarmArn": "arn:aws:cloudwatch:us-east-1:123456789012:alarm:errors",
                    "Namespace": "AWS/Lambda",
                    "MetricName": "Errors",
                    "StateValue": "ALARM",
                    "StateReason": "live incident detail",
                }
            ],
            "CompositeAlarms": [],
        }
    ]
    mock_cw.get_paginator.return_value = paginator

    result = handler_mod.get_alarm_posture({"max_items": 10})

    _, kwargs = paginator.paginate.call_args
    assert "StateValue" not in kwargs
    assert result["data_source"] == "live_api"
    assert result["summary"]["returned_alarm_count"] == 1
    assert "state_value" not in result["alarms"][0]
    assert "state_reason" not in result["alarms"][0]


def test_alarm_posture_remains_live_only(monkeypatch):
    live = MagicMock(return_value={"data_source": "live_api", "alarms": []})
    monkeypatch.setitem(handler_mod._TOOL_HANDLERS, "get_alarm_posture", live)

    result = handler_mod.handler({}, _ctx("get_alarm_posture"))

    assert result["data_source"] == "live_api"
    live.assert_called_once_with({})


def test_alarm_posture_force_refresh_and_other_region_bypass_snapshot(monkeypatch):
    monkeypatch.setattr(handler_mod, "_SNAPSHOT_TABLE", "snap-table")
    monkeypatch.setattr(handler_mod, "_snapshot_account_id", lambda: "123456789012")
    live = MagicMock(return_value={"data_source": "live_api"})
    monkeypatch.setitem(handler_mod._TOOL_HANDLERS, "get_alarm_posture", live)

    force = handler_mod.handler({"force_refresh": True}, _ctx("get_alarm_posture"))
    other_region = handler_mod.handler(
        {"region": "us-west-2"}, _ctx("get_alarm_posture")
    )

    assert force["data_source"] == "live_api"
    assert other_region["data_source"] == "live_api"
    assert live.call_count == 2


def test_alarm_posture_snapshot_miss_falls_back_to_live(monkeypatch):
    monkeypatch.setattr(handler_mod, "_SNAPSHOT_TABLE", "snap-table")
    monkeypatch.setattr(handler_mod, "_snapshot_account_id", lambda: "123456789012")
    ddb = MagicMock()
    ddb.get_item.return_value = {}
    monkeypatch.setattr(handler_mod.boto3, "client", MagicMock(return_value=ddb))
    live = MagicMock(return_value={"data_source": "live_api"})
    monkeypatch.setitem(handler_mod._TOOL_HANDLERS, "get_alarm_posture", live)

    result = handler_mod.handler({}, _ctx("get_alarm_posture"))

    assert result["data_source"] == "live_api"
    live.assert_called_once()


def test_snapshot_status_normalizes_legacy_markers_and_pending_regions(monkeypatch):
    account_id = "123456789012"
    current = {
        "run_id": "published-run",
        "snapshot_id": "published-run",
        "collected_at": "2026-08-17T00:00:00+00:00",
    }
    refresh = {"run_id": "refresh-run", "status": "started"}
    run = {
        "pk": "RUN#refresh-run",
        "sk": "META",
        "run_id": "refresh-run",
        "regions": ["eu-west-1", "us-east-1"],
        "expected_region_count": 2,
    }
    legacy_marker = {
        "pk": "RUN#refresh-run",
        "sk": "REGION#us-east-1",
        "region": "us-east-1",
        "status": "complete",
        "completeness": {
            "complete": False,
            "resource_inventory": False,
            "alarm_inventory": True,
            "source": "tagging_api",
        },
    }
    table = MagicMock()

    def get_item(*, Key):
        if Key["sk"] == "REFRESH":
            return {"Item": refresh}
        if Key["sk"] == "META":
            return {"Item": run}
        return {}

    table.get_item.side_effect = get_item
    table.query.return_value = {"Items": [legacy_marker]}
    monkeypatch.setattr(handler_mod, "_SNAPSHOT_TABLE", "coverage")
    monkeypatch.setattr(handler_mod, "_snapshot_account_id", lambda: account_id)
    monkeypatch.setattr(handler_mod, "_current_snapshot", lambda _: current)
    monkeypatch.setattr(handler_mod, "_snapshot_table", lambda: table)

    result = handler_mod.get_alarm_snapshot_status({})

    progress = result["run_progress"]
    assert progress["expected"] == 2
    assert progress["succeeded"] == 1
    assert progress["pending_or_retrying"] == 1
    assert progress["expected_region_count"] == 2
    assert progress["succeeded_region_count"] == 1
    assert progress["pending_or_retrying_region_count"] == 1
    assert progress["regions"]["eu-west-1"] == {
        "status": "pending_or_retrying",
        "collection_status": "pending_or_retrying",
    }
    completed = progress["regions"]["us-east-1"]
    assert completed["status"] == "succeeded"
    assert completed["resource_inventory_status"] == "partial"
    assert completed["alarm_inventory_status"] == "complete"
    assert completed["incomplete_reasons"] == [
        "tagging_api_fallback_may_omit_untagged_resources"
    ]


# ---------------------------------------------------------------------------
# analyze_alarm_coverage — grade existing alarms vs recommended catalogue
# ---------------------------------------------------------------------------


def _rec(
    period=60,
    comparison="GreaterThanThreshold",
    statistic="Average",
    evaluation_periods=5,
    datapoints=5,
):
    return {
        "comparisonOperator": comparison,
        "statistic": statistic,
        "period": period,
        "evaluationPeriods": evaluation_periods,
        "datapointsToAlarm": datapoints,
        "intent": "detect X",
        "alarmDescription": "desc",
        "threshold": {"justification": "set to a critical level"},
        "dimensions": [{"name": "FunctionName"}],
    }


def _existing_alarm(
    metric_name,
    dims=None,
    period=60,
    comparison="GreaterThanThreshold",
    statistic="Average",
    evaluation_periods=5,
    datapoints=5,
    name=None,
):
    return {
        "alarm_type": "metric",
        "alarm_name": name or f"{metric_name}-alarm",
        "namespace": "AWS/Lambda",
        "metric_name": metric_name,
        "dimensions": dims or [{"Name": "FunctionName", "Value": "my-fn"}],
        "comparison_operator": comparison,
        "statistic": statistic,
        "period": period,
        "evaluation_periods": evaluation_periods,
        "datapoints_to_alarm": datapoints,
    }


def _patch_coverage(
    monkeypatch, recommended, existing, data_source="live_api", data_as_of=""
):
    monkeypatch.setattr(
        handler_mod.recommendations,
        "get_namespace_alarm_recommendations",
        lambda ns: recommended,
    )
    monkeypatch.setattr(
        handler_mod,
        "_snapshot_lookup_alarm_posture",
        lambda event: {
            "alarms": existing,
            "data_source": data_source,
            "data_as_of": data_as_of,
        },
    )


def test_alarm_coverage_requires_namespace_or_arn():
    result = handler_mod.analyze_alarm_coverage({})
    assert result["error"] == "invalid_request"


def test_alarm_coverage_no_catalogue_recommendations(monkeypatch):
    monkeypatch.setattr(
        handler_mod.recommendations,
        "get_namespace_alarm_recommendations",
        lambda ns: {},
    )
    result = handler_mod.analyze_alarm_coverage({"namespace": "AWS/Nope"})
    assert result["note"] == "no_recommendations_in_catalogue"
    assert result["missing"] == []
    assert result["implemented"] == []


def test_alarm_coverage_classifies_implemented_and_missing(monkeypatch):
    recommended = {"Errors": [_rec()], "Throttles": [_rec()]}
    existing = [_existing_alarm("Errors")]
    _patch_coverage(monkeypatch, recommended, existing)

    result = handler_mod.analyze_alarm_coverage({"namespace": "AWS/Lambda"})

    assert {i["metric_name"] for i in result["implemented"]} == {"Errors"}
    assert {m["metric_name"] for m in result["missing"]} == {"Throttles"}
    assert result["summary"]["implemented_count"] == 1
    assert result["summary"]["missing_count"] == 1
    # The missing entry carries the recommendation for later CFN assembly.
    throttles = next(m for m in result["missing"] if m["metric_name"] == "Throttles")
    assert throttles["recommendation"]["comparisonOperator"] == "GreaterThanThreshold"


def test_alarm_coverage_reports_structural_drift(monkeypatch):
    recommended = {"Errors": [_rec(period=60)]}
    existing = [_existing_alarm("Errors", period=300)]  # period drift
    _patch_coverage(monkeypatch, recommended, existing)

    result = handler_mod.analyze_alarm_coverage({"namespace": "AWS/Lambda"})
    assert result["summary"]["implemented_count"] == 1
    assert result["summary"]["drift_count"] == 1
    fields = {d["field"] for d in result["drift"][0]["differences"]}
    assert "period" in fields


def test_alarm_coverage_resource_scope_filters_by_dimensions(monkeypatch):
    recommended = {"Errors": [_rec()]}
    # Existing alarm is for a DIFFERENT function than the scoped resource.
    existing = [
        _existing_alarm("Errors", dims=[{"Name": "FunctionName", "Value": "other-fn"}])
    ]
    _patch_coverage(monkeypatch, recommended, existing)

    result = handler_mod.analyze_alarm_coverage(
        {
            "namespace": "AWS/Lambda",
            "dimensions": [{"Name": "FunctionName", "Value": "my-fn"}],
        }
    )
    # No alarm matches the scoped resource → Errors is missing.
    assert result["summary"]["missing_count"] == 1
    assert result["summary"]["implemented_count"] == 0


def test_alarm_coverage_propagates_snapshot_freshness(monkeypatch):
    _patch_coverage(
        monkeypatch,
        {"Errors": [_rec()]},
        [_existing_alarm("Errors")],
        data_source="scheduled_snapshot",
        data_as_of="2026-08-13T00:00:00+00:00",
    )
    result = handler_mod.analyze_alarm_coverage({"namespace": "AWS/Lambda"})
    assert result["data_source"] == "scheduled_snapshot"
    assert result["data_as_of"] == "2026-08-13T00:00:00+00:00"


def test_alarm_coverage_falls_back_to_live_posture(monkeypatch):
    monkeypatch.setattr(
        handler_mod.recommendations,
        "get_namespace_alarm_recommendations",
        lambda ns: {"Errors": [_rec()]},
    )
    monkeypatch.setattr(
        handler_mod, "_snapshot_lookup_alarm_posture", lambda event: None
    )
    monkeypatch.setattr(
        handler_mod,
        "get_alarm_posture",
        lambda event: {
            "alarms": [_existing_alarm("Errors")],
            "data_source": "live_api",
        },
    )
    result = handler_mod.analyze_alarm_coverage({"namespace": "AWS/Lambda"})
    assert result["data_source"] == "live_api"
    assert result["summary"]["implemented_count"] == 1


def test_alarm_coverage_dispatch_via_handler(monkeypatch):
    monkeypatch.setitem(
        handler_mod._TOOL_HANDLERS,
        "analyze_alarm_coverage",
        lambda event: {
            "mode": event["mode"],
            "summary": {"implemented_count": 1},
        },
    )
    result = handler_mod.handler({"mode": "account"}, _ctx("analyze_alarm_coverage"))
    assert result["mode"] == "account"
    assert result["summary"]["implemented_count"] == 1


# ---------------------------------------------------------------------------
# get_metric_metadata remains part of the low-level public surface.
# ---------------------------------------------------------------------------


def test_get_metric_metadata_tool_is_available():
    assert "get_metric_metadata" in handler_mod._TOOL_HANDLERS
    result = handler_mod.get_metric_metadata(
        {"namespace": "AWS/Lambda", "metric_name": "Errors"}
    )
    assert result["metadata"]["metricId"]["metricName"] == "Errors"
    assert result["catalogue_version"]


# ---------------------------------------------------------------------------
# analyze_alarm_coverage — bounded resource-inventory mode
# ---------------------------------------------------------------------------


def _inventory_resource(arn, region="us-east-1", account_id="123456789012"):
    return {"arn": arn, "region": region, "account_id": account_id, "tags": {}}


def _inventory_posture(alarms, region="us-east-1", truncated=False):
    return {
        "alarms": alarms,
        "account_id": "123456789012",
        "region": region,
        "data_source": "scheduled_snapshot",
        "data_as_of": "2026-08-13T00:00:00+00:00",
        "freshness_note": "snapshot",
        "truncated": truncated,
        "truncation_note": "Alarm inventory capped." if truncated else None,
        "returned_alarm_count": len(alarms),
        "alarm_cap": 1000,
    }


def _patch_inventory_catalogue(monkeypatch, by_namespace):
    monkeypatch.setattr(
        handler_mod.recommendations,
        "get_namespace_alarm_recommendations",
        lambda namespace: by_namespace.get(namespace, {}),
    )


def test_inventory_coverage_reports_resource_with_zero_matching_alarms(monkeypatch):
    _patch_inventory_catalogue(monkeypatch, {"AWS/Lambda": {"Errors": [_rec()]}})
    monkeypatch.setattr(
        handler_mod,
        "_coverage_existing_alarms",
        lambda event: _inventory_posture(
            [
                _existing_alarm(
                    "Errors",
                    dims=[{"Name": "FunctionName", "Value": "covered-fn"}],
                )
            ],
            event["region"],
        ),
    )
    inventory = {
        "resources": [
            _inventory_resource(
                "arn:aws:lambda:us-east-1:123456789012:function:covered-fn"
            ),
            _inventory_resource(
                "arn:aws:lambda:us-east-1:123456789012:function:no-alarm-fn"
            ),
        ],
        "truncated": False,
        "note": None,
    }

    result = handler_mod.analyze_alarm_coverage({"resource_inventory": inventory})

    by_arn = {resource["arn"]: resource for resource in result["resources"]}
    assert by_arn[inventory["resources"][0]["arn"]]["status"] == "covered"
    assert by_arn[inventory["resources"][0]["arn"]]["matching_alarm_count"] == 1
    assert by_arn[inventory["resources"][1]["arn"]]["status"] == "no_matching_alarms"
    assert by_arn[inventory["resources"][1]["arn"]]["matching_alarm_count"] == 0
    assert [item["arn"] for item in result["zero_matching_alarm_resources"]] == [
        inventory["resources"][1]["arn"]
    ]


def test_inventory_coverage_counts_non_catalogue_alarm_without_calling_it_zero(
    monkeypatch,
):
    _patch_inventory_catalogue(monkeypatch, {"AWS/Lambda": {"Errors": [_rec()]}})
    monkeypatch.setattr(
        handler_mod,
        "_coverage_existing_alarms",
        lambda event: _inventory_posture(
            [_existing_alarm("Invocations")], event["region"]
        ),
    )
    arn = "arn:aws:lambda:us-east-1:123456789012:function:my-fn"

    result = handler_mod.analyze_alarm_coverage({"resource_arns": [arn]})

    resource = result["resources"][0]
    assert resource["matching_alarm_count"] == 1
    assert resource["status"] == "partial"
    assert resource["summary"]["missing_count"] == 1
    assert result["zero_matching_alarm_resources"] == []


def test_inventory_coverage_deduplicates_resource_arns(monkeypatch):
    _patch_inventory_catalogue(monkeypatch, {"AWS/Lambda": {"Errors": [_rec()]}})
    monkeypatch.setattr(
        handler_mod,
        "_coverage_existing_alarms",
        lambda event: _inventory_posture([], event["region"]),
    )
    arn = "arn:aws:lambda:us-east-1:123456789012:function:my-fn"

    result = handler_mod.analyze_alarm_coverage({"resource_arns": [arn, arn]})

    assert len(result["resources"]) == 1
    assert result["summary"]["duplicate_resource_count"] == 1


def test_inventory_coverage_supports_mixed_namespaces_and_loads_once_per_region(
    monkeypatch,
):
    _patch_inventory_catalogue(
        monkeypatch,
        {
            "AWS/Lambda": {"Errors": [_rec()]},
            "AWS/DynamoDB": {"ThrottledRequests": [_rec()]},
        },
    )
    calls = []

    def posture(event):
        calls.append(event["region"])
        return _inventory_posture([_existing_alarm("Errors")], event["region"])

    monkeypatch.setattr(handler_mod, "_coverage_existing_alarms", posture)
    resources = [
        "arn:aws:lambda:us-east-1:123456789012:function:my-fn",
        "arn:aws:dynamodb:us-east-1:123456789012:table/my-table",
    ]

    result = handler_mod.analyze_alarm_coverage({"resource_arns": resources})

    assert calls == ["us-east-1"]
    by_namespace = {resource["namespace"]: resource for resource in result["resources"]}
    assert by_namespace["AWS/Lambda"]["status"] == "covered"
    assert by_namespace["AWS/DynamoDB"]["status"] == "no_matching_alarms"


def test_inventory_coverage_classifies_unsupported_arn(monkeypatch):
    _patch_inventory_catalogue(monkeypatch, {})
    result = handler_mod.analyze_alarm_coverage(
        {"resource_arns": ["arn:aws:unknown:us-east-1:123456789012:item/x"]}
    )

    assert result["resources"] == []
    assert result["summary"]["unsupported_resource_count"] == 1
    assert result["unsupported_resources"][0]["reason"] == "unknown_resource_type"
    assert result["coverage_complete"] is False


def test_inventory_coverage_propagates_resource_inventory_truncation(monkeypatch):
    _patch_inventory_catalogue(monkeypatch, {"AWS/Lambda": {"Errors": [_rec()]}})
    monkeypatch.setattr(
        handler_mod,
        "_coverage_existing_alarms",
        lambda event: _inventory_posture([], event["region"]),
    )
    result = handler_mod.analyze_alarm_coverage(
        {
            "resource_inventory": {
                "resources": [
                    _inventory_resource(
                        "arn:aws:lambda:us-east-1:123456789012:function:my-fn"
                    )
                ],
                "truncated": True,
                "note": "Results capped at 1000.",
            }
        }
    )

    assert result["resource_inventory"]["truncated"] is True
    assert result["coverage_complete"] is False
    assert "Results capped at 1000." in result["coverage_notes"]


def test_inventory_coverage_never_reports_zero_when_alarm_posture_is_truncated(
    monkeypatch,
):
    _patch_inventory_catalogue(monkeypatch, {"AWS/Lambda": {"Errors": [_rec()]}})
    monkeypatch.setattr(
        handler_mod,
        "_coverage_existing_alarms",
        lambda event: _inventory_posture([], event["region"], truncated=True),
    )
    arn = "arn:aws:lambda:us-east-1:123456789012:function:my-fn"

    result = handler_mod.analyze_alarm_coverage({"resource_arns": [arn]})

    assert result["resources"][0]["status"] == "inventory_incomplete"
    assert result["resources"][0]["alarm_inventory_complete"] is False
    assert result["zero_matching_alarm_resources"] == []
    assert result["summary"]["inventory_incomplete_resource_count"] == 1


def test_inventory_coverage_loads_posture_once_for_each_distinct_region(monkeypatch):
    _patch_inventory_catalogue(monkeypatch, {"AWS/Lambda": {"Errors": [_rec()]}})
    calls = []

    def posture(event):
        calls.append(event["region"])
        return _inventory_posture([], event["region"])

    monkeypatch.setattr(handler_mod, "_coverage_existing_alarms", posture)
    result = handler_mod.analyze_alarm_coverage(
        {
            "resource_arns": [
                "arn:aws:lambda:us-east-1:123456789012:function:east-a",
                "arn:aws:lambda:us-east-1:123456789012:function:east-b",
                "arn:aws:lambda:us-west-2:123456789012:function:west-a",
            ]
        }
    )

    assert calls == ["us-east-1", "us-west-2"]
    assert len(result["resources"]) == 3


def test_inventory_coverage_rejects_more_than_hard_cap(monkeypatch):
    arns = [
        f"arn:aws:lambda:us-east-1:123456789012:function:fn-{index}"
        for index in range(handler_mod._COVERAGE_RESOURCE_HARD_CAP + 1)
    ]
    result = handler_mod.analyze_alarm_coverage({"resource_arns": arns})
    assert result["error"] == "inventory_too_large"


def test_coverage_posture_metadata_preserves_truncation_and_freshness(monkeypatch):
    monkeypatch.setattr(
        handler_mod,
        "_snapshot_lookup_alarm_posture",
        lambda event: {
            "account_id": "123456789012",
            "region": "us-east-1",
            "alarms": [_existing_alarm("Errors")],
            "summary": {
                "returned_alarm_count": 1,
                "alarm_cap": 1,
                "truncated": True,
                "truncation_note": "Alarm inventory capped at 1.",
            },
            "data_source": "scheduled_snapshot",
            "data_as_of": "2026-08-13T00:00:00+00:00",
            "freshness_note": "snapshot freshness",
        },
    )

    posture = handler_mod._coverage_existing_alarms({})

    assert posture["truncated"] is True
    assert posture["truncation_note"] == "Alarm inventory capped at 1."
    assert posture["freshness_note"] == "snapshot freshness"
    assert posture["account_id"] == "123456789012"


def test_inventory_coverage_recognises_metric_math_alarm_identity(monkeypatch):
    _patch_inventory_catalogue(monkeypatch, {"AWS/Lambda": {"Errors": [_rec()]}})
    metric_math_alarm = _existing_alarm("unused")
    metric_math_alarm.update(
        {
            "namespace": None,
            "metric_name": None,
            "dimensions": [],
            "metrics": [
                {
                    "Id": "errors",
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "AWS/Lambda",
                            "MetricName": "Errors",
                            "Dimensions": [{"Name": "FunctionName", "Value": "my-fn"}],
                        },
                        "Period": 60,
                        "Stat": "Sum",
                    },
                },
                {"Id": "expr", "Expression": "errors > 0"},
            ],
        }
    )
    monkeypatch.setattr(
        handler_mod,
        "_coverage_existing_alarms",
        lambda event: _inventory_posture([metric_math_alarm], event["region"]),
    )

    result = handler_mod.analyze_alarm_coverage(
        {"resource_arns": ["arn:aws:lambda:us-east-1:123456789012:function:my-fn"]}
    )

    resource = result["resources"][0]
    assert resource["matching_alarm_count"] == 1
    assert resource["status"] == "covered"
    assert result["zero_matching_alarm_resources"] == []


def test_inventory_coverage_reports_zero_alarm_even_without_catalogue(monkeypatch):
    _patch_inventory_catalogue(monkeypatch, {"AWS/DynamoDB": {}})
    monkeypatch.setattr(
        handler_mod,
        "_coverage_existing_alarms",
        lambda event: _inventory_posture([], event["region"]),
    )
    arn = "arn:aws:dynamodb:us-east-1:123456789012:table/my-table"

    result = handler_mod.analyze_alarm_coverage({"resource_arns": [arn]})

    assert result["resources"][0]["status"] == "no_catalogue"
    assert result["summary"]["zero_matching_alarm_resource_count"] == 1
    assert result["zero_matching_alarm_resources"][0]["arn"] == arn
    assert (
        result["zero_matching_alarm_resources"][0]["catalogue_status"]
        == "no_recommendations_in_catalogue"
    )


# ---------------------------------------------------------------------------
# prepare_alarm_deployment redesign
# ---------------------------------------------------------------------------


def _deployment_candidate(candidate_id="candidate-1"):
    return {
        "candidate_id": candidate_id,
        "recommendation_id": "rec-1",
        "resource_arn": "arn:aws:rds:us-east-1:123456789012:db:orders",
        "region": "us-east-1",
        "namespace": "AWS/RDS",
        "metric_name": "CPUUtilization",
        "dimensions": [{"Name": "DBInstanceIdentifier", "Value": "orders"}],
        "threshold_strategy": {"type": "fixed", "value": 90},
        "recommendation": {
            "recommendationId": "rec-1",
            "namespace": "AWS/RDS",
            "metricName": "CPUUtilization",
            "comparisonOperator": "GreaterThanThreshold",
            "statistic": "Average",
            "period": 60,
            "evaluationPeriods": 5,
            "datapointsToAlarm": 5,
            "treatMissingData": "missing",
        },
    }


def _patch_deployment_context(monkeypatch, candidates):
    monkeypatch.setattr(
        handler_mod,
        "_snapshot_context",
        lambda event: {
            "account_id": "123456789012",
            "current": {
                "snapshot_id": "run-1",
                "regions": ["us-east-1"],
            },
        },
    )
    monkeypatch.setattr(
        handler_mod, "_selected_candidates", lambda context, ids: candidates
    )
    monkeypatch.setattr(handler_mod, "_live_alarms", lambda region: [])


def test_prepare_deployment_excludes_already_implemented(monkeypatch):
    candidate = _deployment_candidate()
    _patch_deployment_context(monkeypatch, [candidate])
    monkeypatch.setattr(
        handler_mod.coverage_domain,
        "evaluate_resource",
        lambda *args, **kwargs: {
            "candidates": [
                {
                    "recommendation_id": "rec-1",
                    "status": "implemented",
                    "matched_alarm_ids": ["alarm-1"],
                }
            ]
        },
    )
    monkeypatch.setattr(
        handler_mod.cfn,
        "assemble_cfn_template",
        lambda **kwargs: {"template_yaml": "Resources: {}", "summary": {}},
    )

    result = handler_mod.prepare_alarm_deployment(
        {
            "snapshot_id": "run-1",
            "candidate_ids": ["candidate-1"],
            "sns_topic_arn": "arn:aws:sns:us-east-1:123456789012:ops",
        }
    )

    assert result["deployment_count"] == 0
    assert result["excluded"][0]["reason"] == "already_implemented"


def test_prepare_deployment_blocks_insufficient_history(monkeypatch):
    candidate = _deployment_candidate()
    candidate["threshold_strategy"] = {"type": "calibrate"}
    _patch_deployment_context(monkeypatch, [candidate])
    monkeypatch.setattr(
        handler_mod.coverage_domain,
        "evaluate_resource",
        lambda *args, **kwargs: {
            "candidates": [{"recommendation_id": "rec-1", "status": "missing"}]
        },
    )
    monkeypatch.setattr(
        handler_mod,
        "_calibrate_candidates",
        lambda candidates, overrides: (
            {},
            [{"candidate_id": "candidate-1", "reason": "insufficient_history"}],
        ),
    )
    monkeypatch.setattr(
        handler_mod.cfn,
        "assemble_cfn_template",
        lambda **kwargs: {"template_yaml": "Resources: {}", "summary": {}},
    )

    result = handler_mod.prepare_alarm_deployment(
        {
            "snapshot_id": "run-1",
            "candidate_ids": ["candidate-1"],
            "sns_topic_arn": "arn:aws:sns:us-east-1:123456789012:ops",
        }
    )

    assert result["deployment_count"] == 0
    assert result["blocked"][0]["reason"] == "insufficient_history"


def test_prepare_deployment_uses_override_and_emits_one_artifact(monkeypatch):
    candidate = _deployment_candidate()
    candidate["threshold_strategy"] = {"type": "calibrate"}
    _patch_deployment_context(monkeypatch, [candidate])
    monkeypatch.setattr(
        handler_mod.coverage_domain,
        "evaluate_resource",
        lambda *args, **kwargs: {
            "candidates": [{"recommendation_id": "rec-1", "status": "missing"}]
        },
    )
    assembler = MagicMock(
        return_value={"template_yaml": "Resources:\n  Alarm: {}", "summary": {}}
    )
    monkeypatch.setattr(handler_mod.cfn, "assemble_cfn_template", assembler)

    result = handler_mod.prepare_alarm_deployment(
        {
            "snapshot_id": "run-1",
            "candidate_ids": ["candidate-1"],
            "sns_topic_arn": "arn:aws:sns:us-east-1:123456789012:ops",
            "threshold_overrides": {"candidate-1": 82},
        }
    )

    assert result["deployment_count"] == 1
    assert assembler.call_count == 1
    assert assembler.call_args.kwargs["alarms"][0]["threshold"] == 82
