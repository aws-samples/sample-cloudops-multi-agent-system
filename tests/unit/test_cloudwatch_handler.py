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
    assert len(result["available_tools"]) == 8


def test_dispatch_routes_to_named_tool(monkeypatch):
    sentinel = {"ok": True}
    monkeypatch.setitem(handler_mod._TOOL_HANDLERS, "get_metric_metadata", lambda e: sentinel)
    result = handler_mod.handler({"namespace": "AWS/Lambda"}, _ctx("get_metric_metadata"))
    assert result is sentinel


def test_dispatch_surfaces_exceptions_as_error(monkeypatch):
    def _boom(_event):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(handler_mod._TOOL_HANDLERS, "get_active_alarms", _boom)
    result = handler_mod.handler({}, _ctx("get_active_alarms"))
    assert result["error"] == "get_active_alarms failed: kaboom"


# ---------------------------------------------------------------------------
# get_metric_metadata / get_recommended_metric_alarms (catalogue, no AWS)
# ---------------------------------------------------------------------------


def test_get_metric_metadata_known_pair():
    result = handler_mod.get_metric_metadata(
        {"namespace": "AWS/Lambda", "metric_name": "Errors"}
    )
    assert result["namespace"] == "AWS/Lambda"
    assert result["metric_name"] == "Errors"
    assert result["metadata"] is not None


def test_get_metric_metadata_requires_inputs():
    result = handler_mod.get_metric_metadata({"namespace": "AWS/Lambda"})
    assert result["error"] == "invalid_request"


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
