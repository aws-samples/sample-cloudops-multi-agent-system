from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

MCP_ROOT = Path(__file__).resolve().parents[2] / "src" / "lambda" / "mcp"
sys.path.insert(0, str(MCP_ROOT))

from shared.cloudwatch_domain import coverage, normalize, recommendations, snapshot


def _alarm(arn: str, metric: str, dimensions: list[dict], **changes):
    raw = {
        "AlarmArn": arn,
        "AlarmName": arn.rsplit(":", 1)[-1],
        "Namespace": "AWS/RDS",
        "MetricName": metric,
        "Dimensions": dimensions,
        "Statistic": "Average",
        "ComparisonOperator": "GreaterThanThreshold",
        "Period": 60,
        "EvaluationPeriods": 5,
        "DatapointsToAlarm": 5,
        "TreatMissingData": "missing",
    }
    raw.update(changes)
    return normalize.normalize_alarm(raw, "us-east-1")


def test_catalogue_preserves_multiple_recommendations_with_stable_ids():
    first = recommendations.get_recommended_alarms_from_catalogue(
        "AWS/ApiGateway", "Count"
    )
    second = recommendations.get_recommended_alarms_from_catalogue(
        "AWS/ApiGateway", "Count"
    )

    assert len(first) > 1
    assert [item["recommendationId"] for item in first] == [
        item["recommendationId"] for item in second
    ]
    assert len({item["recommendationId"] for item in first}) == len(first)
    assert all(item["catalogueVersion"] for item in first)


def test_exact_dimensions_keep_rds_instance_and_cluster_separate():
    instance = normalize.resource_profile(
        "arn:aws:rds:us-east-1:123456789012:db:orders"
    )
    wrong_cluster_alarm = _alarm(
        "arn:aws:cloudwatch:us-east-1:123456789012:alarm:cluster-cpu",
        "CPUUtilization",
        [{"Name": "DBClusterIdentifier", "Value": "orders"}],
    )

    result = coverage.evaluate_resource(
        instance,
        [wrong_cluster_alarm],
        resource_inventory_complete=True,
        alarm_inventory_complete=True,
    )
    cpu = [
        candidate
        for candidate in result["candidates"]
        if candidate["metric_name"] == "CPUUtilization"
        and not candidate.get("unresolved_dimensions")
    ]
    assert cpu
    assert all(candidate["status"] == "missing" for candidate in cpu)


def test_unresolved_dimensions_are_not_reported_missing():
    profile = normalize.resource_profile(
        "arn:aws:elasticache:us-east-1:123456789012:cluster:cache-a"
    )
    result = coverage.evaluate_resource(
        profile, [], resource_inventory_complete=True, alarm_inventory_complete=True
    )
    cpu = [
        candidate
        for candidate in result["candidates"]
        if candidate["metric_name"] == "CPUUtilization"
    ]
    assert cpu[0]["status"] == "unresolved_dimensions"
    assert cpu[0]["unresolved_dimensions"] == ["CacheNodeId"]


def test_missing_is_non_authoritative_when_alarm_inventory_is_incomplete():
    profile = normalize.resource_profile("arn:aws:rds:us-east-1:123456789012:db:orders")
    result = coverage.evaluate_resource(
        profile, [], resource_inventory_complete=True, alarm_inventory_complete=False
    )
    assert result["coverage_status"] == "inventory_incomplete"
    assert "alarm_inventory_incomplete" in result["completeness_reasons"]
    assert not any(
        candidate["status"] == "missing" for candidate in result["candidates"]
    )


def test_drift_is_reported_separately_from_implementation():
    profile = normalize.resource_profile("arn:aws:rds:us-east-1:123456789012:db:orders")
    alarm = _alarm(
        "arn:aws:cloudwatch:us-east-1:123456789012:alarm:cpu",
        "CPUUtilization",
        [{"Name": "DBInstanceIdentifier", "Value": "orders"}],
        Period=300,
    )
    result = coverage.evaluate_resource(
        profile,
        [alarm],
        resource_inventory_complete=True,
        alarm_inventory_complete=True,
    )
    cpu = [
        candidate
        for candidate in result["candidates"]
        if candidate["metric_name"] == "CPUUtilization"
        and not candidate.get("unresolved_dimensions")
    ]
    assert cpu[0]["status"] in {"implemented", "implemented_with_drift"}
    if cpu[0]["recommendation"]["period"] != 300:
        assert cpu[0]["status"] == "implemented_with_drift"


def test_metric_math_matching_uses_complete_canonical_signature():
    queries = [
        {
            "Id": "rate",
            "Expression": "errors / invocations * 100",
            "ReturnData": True,
        },
        {
            "Id": "errors",
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/Lambda",
                    "MetricName": "Errors",
                    "Dimensions": [{"Name": "FunctionName", "Value": "orders"}],
                },
                "Period": 60,
                "Stat": "Sum",
            },
            "ReturnData": False,
        },
    ]
    alarm = normalize.normalize_alarm(
        {
            "AlarmArn": "arn:aws:cloudwatch:us-east-1:123456789012:alarm:error-rate",
            "AlarmName": "error-rate",
            "Metrics": list(reversed(queries)),
        },
        "us-east-1",
    )
    recommendation = {"metrics": queries}

    assert coverage._matches(alarm, recommendation, "", [])
    changed = {**recommendation, "metrics": [{**queries[0], "Expression": "errors"}]}
    assert not coverage._matches(alarm, changed, "", [])


@pytest.mark.parametrize(
    "arn,region",
    [
        ("arn:aws:cloudfront::123456789012:distribution/ABC", "us-east-1"),
        ("arn:aws:route53:::healthcheck/abc", "us-east-1"),
    ],
)
def test_global_services_use_real_metric_region(arn, region):
    assert normalize.resource_profile(arn)["metric_region"] == region


def test_tag_filter_semantics_and_cursor_validation():
    assert snapshot.matches_tags(
        {"Env": "prod", "Team": "payments"},
        {"Env": ["prod", "stage"], "Team": "payments"},
    )
    assert not snapshot.matches_tags(
        {"Env": "prod", "Team": "search"},
        {"Env": ["prod", "stage"], "Team": "payments"},
    )
    digest = snapshot.query_hash({"tags": {"Env": "prod"}})
    cursor = snapshot.encode_cursor("run-1", digest, 50, "secret")
    assert snapshot.decode_cursor(cursor, "run-1", digest, "secret") == 50
    with pytest.raises(ValueError, match="invalid cursor"):
        snapshot.decode_cursor(cursor, "run-2", digest, "secret")


def test_snapshot_fresh_stale_and_expired():
    now = dt.datetime(2026, 8, 14, tzinfo=dt.timezone.utc)
    assert snapshot.freshness(now - dt.timedelta(hours=7), now)["state"] == "fresh"
    assert snapshot.freshness(now - dt.timedelta(hours=9), now)["state"] == "stale"
    assert snapshot.freshness(now - dt.timedelta(hours=49), now)["state"] == "expired"
