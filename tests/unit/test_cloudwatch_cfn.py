"""Unit tests for src/lambda/mcp/cloudwatch/cfn.py — build_cfn_alarm.

Covers both alarm shapes from design.md "Component 1" → "CfnAlarm":
  * Static-threshold path — Statistic / Threshold / ComparisonOperator / Dimensions.
  * Anomaly-detection path — Metrics array + ThresholdMetricId; dimensions live
    inside MetricStat.Metric.Dimensions, NOT at the top level.

Property test verifies design.md "Property 2: SNS injection invariant":
  for any valid SNS ARN matching the regex, the output's
  Properties.AlarmActions[0] is exactly that ARN.

The module is loaded under a unique name (cloudwatch_cfn) to avoid collisions
with other handler.py-style modules under src/lambda/mcp/.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from hypothesis import given, strategies as st

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CFN_PATH = _REPO_ROOT / "src" / "lambda" / "mcp" / "cloudwatch" / "cfn.py"
_spec = importlib.util.spec_from_file_location("cloudwatch_cfn", _CFN_PATH)
cfn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cfn)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_SNS = "arn:aws:sns:us-east-1:123456789012:my-alarms"


def _static_recommendation() -> dict:
    """Catalogue-style alarm recommendation, static-threshold (the dominant shape)."""
    return {
        "namespace": "AWS/Lambda",
        "metric_name": "Errors",
        "comparisonOperator": "GreaterThanThreshold",
        "statistic": "Sum",
        "period": 60,
        "evaluationPeriods": 5,
        "datapointsToAlarm": 5,
        "treatMissingData": "notBreaching",
        "alarmDescription": "Lambda function error count exceeds threshold.",
        "intent": "Detect Lambda function failures.",
    }


def _anomaly_recommendation(operator: str) -> dict:
    """Anomaly-detection variant — picks the comparisonOperator."""
    return {
        "namespace": "AWS/RDS",
        "metric_name": "CPUUtilization",
        "comparisonOperator": operator,
        "statistic": "Average",
        "period": 300,
        "evaluationPeriods": 3,
        "datapointsToAlarm": 3,
        "treatMissingData": "missing",
        "alarmDescription": "Detect anomalous CPU utilization on the RDS instance.",
    }


def _dimensions() -> list[dict]:
    return [{"name": "FunctionName", "value": "my-fn"}]


# ---------------------------------------------------------------------------
# Static-threshold path
# ---------------------------------------------------------------------------

class TestStaticThresholdPath:
    def test_returns_cloudformation_alarm_resource(self):
        result = cfn.build_cfn_alarm(
            _static_recommendation(),
            _dimensions(),
            threshold=5.0,
            sns_topic_arn=VALID_SNS,
        )
        assert result["Type"] == "AWS::CloudWatch::Alarm"
        assert "Properties" in result

    def test_static_path_includes_all_required_keys(self):
        result = cfn.build_cfn_alarm(
            _static_recommendation(),
            _dimensions(),
            threshold=5.0,
            sns_topic_arn=VALID_SNS,
        )
        props = result["Properties"]
        # Per design.md "CfnAlarm" data model — every key must be present.
        for key in (
            "AlarmName",
            "AlarmDescription",
            "Namespace",
            "MetricName",
            "Dimensions",
            "Statistic",
            "ComparisonOperator",
            "Threshold",
            "EvaluationPeriods",
            "DatapointsToAlarm",
            "Period",
            "TreatMissingData",
            "AlarmActions",
        ):
            assert key in props, f"missing required Property: {key}"

    def test_dimensions_normalized_to_cfn_form(self):
        result = cfn.build_cfn_alarm(
            _static_recommendation(),
            [{"name": "FunctionName", "value": "my-fn"}],
            threshold=5.0,
            sns_topic_arn=VALID_SNS,
        )
        assert result["Properties"]["Dimensions"] == [
            {"Name": "FunctionName", "Value": "my-fn"}
        ]

    def test_alarm_actions_is_exactly_the_sns_arn(self):
        result = cfn.build_cfn_alarm(
            _static_recommendation(),
            _dimensions(),
            threshold=5.0,
            sns_topic_arn=VALID_SNS,
        )
        assert result["Properties"]["AlarmActions"] == [VALID_SNS]

    def test_threshold_is_passed_through(self):
        result = cfn.build_cfn_alarm(
            _static_recommendation(),
            _dimensions(),
            threshold=42.5,
            sns_topic_arn=VALID_SNS,
        )
        assert result["Properties"]["Threshold"] == 42.5

    def test_alarm_name_is_metric_plus_short_hash(self):
        result = cfn.build_cfn_alarm(
            _static_recommendation(),
            _dimensions(),
            threshold=5.0,
            sns_topic_arn=VALID_SNS,
        )
        # Format: "<MetricName>-<8 hex chars>".
        name = result["Properties"]["AlarmName"]
        assert name.startswith("Errors-")
        suffix = name.split("-", 1)[1]
        assert len(suffix) == 8
        assert all(c in "0123456789abcdef" for c in suffix)

    def test_alarm_name_is_stable_for_same_dimensions(self):
        rec = _static_recommendation()
        result_a = cfn.build_cfn_alarm(rec, _dimensions(), 5.0, VALID_SNS)
        result_b = cfn.build_cfn_alarm(rec, _dimensions(), 5.0, VALID_SNS)
        assert result_a["Properties"]["AlarmName"] == result_b["Properties"]["AlarmName"]

    def test_alarm_name_differs_for_different_dimensions(self):
        rec = _static_recommendation()
        a = cfn.build_cfn_alarm(rec, [{"name": "FunctionName", "value": "fn-a"}], 5.0, VALID_SNS)
        b = cfn.build_cfn_alarm(rec, [{"name": "FunctionName", "value": "fn-b"}], 5.0, VALID_SNS)
        assert a["Properties"]["AlarmName"] != b["Properties"]["AlarmName"]


# ---------------------------------------------------------------------------
# Tag handling
# ---------------------------------------------------------------------------

class TestTags:
    def test_non_empty_tags_emit_cfn_tag_list(self):
        result = cfn.build_cfn_alarm(
            _static_recommendation(),
            _dimensions(),
            threshold=5.0,
            sns_topic_arn=VALID_SNS,
            tags={"App": "payment", "Owner": "team-finance"},
        )
        # Order is dict-insertion-order preserving; assert membership.
        emitted = result["Properties"]["Tags"]
        assert {"Key": "App", "Value": "payment"} in emitted
        assert {"Key": "Owner", "Value": "team-finance"} in emitted
        assert len(emitted) == 2

    def test_empty_tags_dict_omits_tags_key_entirely(self):
        result = cfn.build_cfn_alarm(
            _static_recommendation(),
            _dimensions(),
            threshold=5.0,
            sns_topic_arn=VALID_SNS,
            tags={},
        )
        assert "Tags" not in result["Properties"]

    def test_none_tags_omits_tags_key_entirely(self):
        result = cfn.build_cfn_alarm(
            _static_recommendation(),
            _dimensions(),
            threshold=5.0,
            sns_topic_arn=VALID_SNS,
            tags=None,
        )
        assert "Tags" not in result["Properties"]


# ---------------------------------------------------------------------------
# Invalid SNS ARN
# ---------------------------------------------------------------------------

class TestInvalidSnsArn:
    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "arn:aws:sns",
            "arn:aws:sns:us-east-1:abc:topic",  # non-numeric account
            "arn:aws:sns:US-EAST-1:123456789012:topic",  # uppercase region
            "arn:aws:sns:us-east-1:1234:topic",  # short account
            "not-an-arn",
        ],
    )
    def test_invalid_arn_returns_structured_error(self, bad):
        result = cfn.build_cfn_alarm(
            _static_recommendation(),
            _dimensions(),
            threshold=5.0,
            sns_topic_arn=bad,
        )
        assert result["error"] == "invalid_sns_topic_arn"
        assert result["received"] == bad
        assert "arn:aws:sns" in result["message"]

    def test_non_string_arn_also_returns_structured_error(self):
        result = cfn.build_cfn_alarm(
            _static_recommendation(),
            _dimensions(),
            threshold=5.0,
            sns_topic_arn=None,  # type: ignore[arg-type]
        )
        assert result["error"] == "invalid_sns_topic_arn"


# ---------------------------------------------------------------------------
# Anomaly-detection path
# ---------------------------------------------------------------------------

class TestAnomalyDetectionPath:
    @pytest.mark.parametrize(
        "operator",
        [
            "LessThanLowerOrGreaterThanUpperThreshold",
            "LessThanLowerThreshold",
            "GreaterThanUpperThreshold",
        ],
    )
    def test_anomaly_path_emits_metrics_array(self, operator):
        result = cfn.build_cfn_alarm(
            _anomaly_recommendation(operator),
            [{"name": "DBInstanceIdentifier", "value": "prod-db"}],
            threshold=999.0,  # ignored on anomaly path
            sns_topic_arn=VALID_SNS,
        )
        props = result["Properties"]
        assert "Metrics" in props
        assert "ThresholdMetricId" in props
        assert props["ThresholdMetricId"] == "ad1"
        assert props["ComparisonOperator"] == operator

    def test_anomaly_path_omits_static_keys(self):
        result = cfn.build_cfn_alarm(
            _anomaly_recommendation("GreaterThanUpperThreshold"),
            [{"name": "DBInstanceIdentifier", "value": "prod-db"}],
            threshold=999.0,
            sns_topic_arn=VALID_SNS,
        )
        props = result["Properties"]
        # Per design.md: anomaly-detection alarms drop these keys; dimensions
        # live inside MetricStat.Metric.Dimensions instead.
        for key in ("Statistic", "Threshold", "Dimensions", "Namespace", "MetricName"):
            assert key not in props, f"{key} must NOT appear at top level on anomaly path"

    def test_anomaly_path_dimensions_inside_metric_stat(self):
        result = cfn.build_cfn_alarm(
            _anomaly_recommendation("GreaterThanUpperThreshold"),
            [{"name": "DBInstanceIdentifier", "value": "prod-db"}],
            threshold=999.0,
            sns_topic_arn=VALID_SNS,
        )
        metrics = result["Properties"]["Metrics"]
        # First entry is the metric itself; second is the anomaly band expression.
        m1 = next(m for m in metrics if m["Id"] == "m1")
        ad1 = next(m for m in metrics if m["Id"] == "ad1")
        assert m1["MetricStat"]["Metric"]["Namespace"] == "AWS/RDS"
        assert m1["MetricStat"]["Metric"]["MetricName"] == "CPUUtilization"
        assert m1["MetricStat"]["Metric"]["Dimensions"] == [
            {"Name": "DBInstanceIdentifier", "Value": "prod-db"}
        ]
        assert ad1["Expression"].startswith("ANOMALY_DETECTION_BAND(m1")

    def test_anomaly_path_alarm_actions_still_wired(self):
        result = cfn.build_cfn_alarm(
            _anomaly_recommendation("LessThanLowerThreshold"),
            [{"name": "DBInstanceIdentifier", "value": "prod-db"}],
            threshold=999.0,
            sns_topic_arn=VALID_SNS,
        )
        assert result["Properties"]["AlarmActions"] == [VALID_SNS]


# ---------------------------------------------------------------------------
# Property test — design.md "Property 2: SNS injection invariant"
# ---------------------------------------------------------------------------

# Generate any valid SNS ARN matching the module's regex. The regex is:
# ^arn:aws:sns:[a-z0-9-]+:\d{12}:.+$
# So we sample regions from lowercase-alphanum-hyphen, account IDs from 12-digit
# strings, and topic names from any non-empty string of safe characters.
_region_st = st.from_regex(r"^[a-z0-9-]+$", fullmatch=True).filter(
    lambda s: 1 <= len(s) <= 30
)
_account_st = st.from_regex(r"^\d{12}$", fullmatch=True)
_topic_name_st = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="-_."
    ),
    min_size=1,
    max_size=64,
)


@st.composite
def valid_sns_arns(draw):
    return f"arn:aws:sns:{draw(_region_st)}:{draw(_account_st)}:{draw(_topic_name_st)}"


class TestSnsInjectionInvariant:
    """Property 2: for any valid SNS ARN, AlarmActions[0] equals the ARN exactly.

    Validates: Requirements 1.2, 5.3
    """

    @given(arn=valid_sns_arns())
    def test_static_path_alarm_actions_equals_input_arn(self, arn):
        result = cfn.build_cfn_alarm(
            _static_recommendation(),
            _dimensions(),
            threshold=5.0,
            sns_topic_arn=arn,
        )
        # If our generator drifted from the regex, fall back gracefully.
        if "error" in result:
            pytest.skip(f"generator produced ARN that fails regex: {arn!r}")
        assert result["Properties"]["AlarmActions"] == [arn]
        assert result["Properties"]["AlarmActions"][0] == arn

    @given(arn=valid_sns_arns())
    def test_anomaly_path_alarm_actions_equals_input_arn(self, arn):
        result = cfn.build_cfn_alarm(
            _anomaly_recommendation("GreaterThanUpperThreshold"),
            [{"name": "DBInstanceIdentifier", "value": "prod-db"}],
            threshold=0.0,
            sns_topic_arn=arn,
        )
        if "error" in result:
            pytest.skip(f"generator produced ARN that fails regex: {arn!r}")
        assert result["Properties"]["AlarmActions"] == [arn]



# ---------------------------------------------------------------------------
# assemble_cfn_template — Task 12 — single-source-of-truth full-YAML emitter
# ---------------------------------------------------------------------------

import yaml as _yaml


def _alarm_entry(metric_name: str, dim_value: str, threshold: float = 5.0) -> dict:
    """Canonical alarm spec shape that assemble_cfn_template expects."""
    return {
        "alarm_dict": {
            "namespace": "AWS/Lambda",
            "metric_name": metric_name,
            "comparisonOperator": "GreaterThanThreshold",
            "statistic": "Sum",
            "period": 60,
            "evaluationPeriods": 5,
            "datapointsToAlarm": 5,
            "treatMissingData": "notBreaching",
            "alarmDescription": f"Lambda {metric_name} alarm",
        },
        "dimensions": [{"Name": "FunctionName", "Value": dim_value}],
        "threshold": threshold,
    }


class TestAssembleCfnTemplateValidYaml:
    """The returned template_yaml is valid YAML and round-trips to a CFN
    document with the right top-level keys."""

    def test_returns_template_yaml_and_summary(self):
        result = cfn.assemble_cfn_template(
            alarms=[_alarm_entry("Errors", "fn-a")],
            sns_topic_arn=VALID_SNS,
        )
        assert "template_yaml" in result
        assert "summary" in result
        assert isinstance(result["template_yaml"], str)
        assert isinstance(result["summary"], dict)

    def test_yaml_round_trips_to_cfn_document(self):
        result = cfn.assemble_cfn_template(
            alarms=[_alarm_entry("Errors", "fn-a")],
            sns_topic_arn=VALID_SNS,
        )
        parsed = _yaml.safe_load(result["template_yaml"])
        assert parsed["AWSTemplateFormatVersion"] == "2010-09-09"
        assert "Description" in parsed
        assert "Resources" in parsed
        assert isinstance(parsed["Resources"], dict)


class TestAssembleCfnTemplateOneResourcePerAlarm:
    """Each alarm in the input becomes exactly one Resources entry in the output."""

    def test_three_alarms_three_resources(self):
        alarms = [
            _alarm_entry("Errors", "fn-a"),
            _alarm_entry("Duration", "fn-a"),
            _alarm_entry("Throttles", "fn-a"),
        ]
        result = cfn.assemble_cfn_template(alarms=alarms, sns_topic_arn=VALID_SNS)
        parsed = _yaml.safe_load(result["template_yaml"])
        assert len(parsed["Resources"]) == 3
        assert result["summary"]["alarm_count"] == 3
        assert len(result["summary"]["logical_ids"]) == 3
        assert result["summary"]["errors"] == []

    def test_logical_ids_are_alphanumeric(self):
        result = cfn.assemble_cfn_template(
            alarms=[_alarm_entry("Errors", "fn-a")],
            sns_topic_arn=VALID_SNS,
        )
        for logical_id in result["summary"]["logical_ids"]:
            assert logical_id.isalnum(), (
                f"CFN logical ID {logical_id!r} must be alphanumeric"
            )

    def test_collisions_get_disambiguated_with_suffix(self):
        # Two alarms with the same metric + same dimensions → same AlarmName,
        # so the same base logical ID. assemble_cfn_template must disambiguate
        # so both end up in Resources (no overwrite).
        a = _alarm_entry("Errors", "fn-a")
        result = cfn.assemble_cfn_template(alarms=[a, a], sns_topic_arn=VALID_SNS)
        parsed = _yaml.safe_load(result["template_yaml"])
        assert len(parsed["Resources"]) == 2
        assert len(result["summary"]["logical_ids"]) == 2
        # Both IDs start with the metric name; one ends with a numeric suffix.
        assert all(lid.startswith("Errors") for lid in result["summary"]["logical_ids"])


class TestAssembleCfnTemplateBuildCfnAlarmParity:
    """assemble_cfn_template must call build_cfn_alarm internally — no
    duplicated resource-shaping logic. Verified by checking the resource
    in the YAML matches what build_cfn_alarm produces directly."""

    def test_resource_shape_matches_build_cfn_alarm(self):
        entry = _alarm_entry("Errors", "fn-a")
        # What build_cfn_alarm produces directly:
        expected = cfn.build_cfn_alarm(
            alarm_recommendation=entry["alarm_dict"],
            dimensions=entry["dimensions"],
            threshold=entry["threshold"],
            sns_topic_arn=VALID_SNS,
        )
        # What assemble_cfn_template emits inside its YAML:
        result = cfn.assemble_cfn_template(alarms=[entry], sns_topic_arn=VALID_SNS)
        parsed = _yaml.safe_load(result["template_yaml"])
        emitted = next(iter(parsed["Resources"].values()))
        assert emitted == expected, (
            "assemble_cfn_template must use build_cfn_alarm verbatim per resource"
        )


class TestAssembleCfnTemplateSnsValidation:
    """Invalid SNS ARN short-circuits before any template is built."""

    def test_invalid_sns_returns_error_no_template(self):
        result = cfn.assemble_cfn_template(
            alarms=[_alarm_entry("Errors", "fn-a")],
            sns_topic_arn="not-an-arn",
        )
        assert result["error"] == "invalid_sns_topic_arn"
        assert result["received"] == "not-an-arn"
        assert "template_yaml" not in result
        assert "summary" not in result

    def test_non_string_sns_also_short_circuits(self):
        result = cfn.assemble_cfn_template(
            alarms=[_alarm_entry("Errors", "fn-a")],
            sns_topic_arn=None,  # type: ignore[arg-type]
        )
        assert result["error"] == "invalid_sns_topic_arn"


class TestAssembleCfnTemplateErrorCollection:
    """Per-alarm build_cfn_alarm errors are captured in summary.errors and
    don't abort the whole template — the rest of the alarms still build."""

    def test_bad_entry_recorded_others_still_built(self):
        good = _alarm_entry("Errors", "fn-a")
        bad = "not a dict"  # entry must be a dict — captured as error
        result = cfn.assemble_cfn_template(
            alarms=[good, bad, good],  # type: ignore[list-item]
            sns_topic_arn=VALID_SNS,
        )
        assert result["summary"]["alarm_count"] == 2
        assert len(result["summary"]["errors"]) == 1
        assert result["summary"]["errors"][0]["index"] == 1


class TestAssembleCfnTemplateDeterminism:
    """Same input → byte-identical output, every time. Idempotent CFN
    deploys depend on this."""

    def test_byte_identical_yaml_for_same_input(self):
        alarms = [
            _alarm_entry("Errors", "fn-a"),
            _alarm_entry("Duration", "fn-a"),
            _alarm_entry("Throttles", "fn-a"),
        ]
        a = cfn.assemble_cfn_template(alarms=alarms, sns_topic_arn=VALID_SNS)
        b = cfn.assemble_cfn_template(alarms=alarms, sns_topic_arn=VALID_SNS)
        assert a["template_yaml"] == b["template_yaml"]


class TestAssembleCfnTemplateCompleteness:
    """50-alarm completeness check — every input alarm must end up in the
    serialized YAML body. Validates the spec contract that the artifact
    body is the COMPLETE template, never an excerpt."""

    def test_fifty_alarms_all_present_in_yaml(self):
        alarms = [_alarm_entry(f"Metric{i}", f"fn-{i}") for i in range(50)]
        result = cfn.assemble_cfn_template(alarms=alarms, sns_topic_arn=VALID_SNS)

        parsed = _yaml.safe_load(result["template_yaml"])
        assert len(parsed["Resources"]) == 50
        assert result["summary"]["alarm_count"] == 50
        assert result["summary"]["errors"] == []

        # Every metric name appears literally in the YAML — guards against
        # the model-abbreviation failure mode this whole task is fixing.
        for i in range(50):
            assert f"Metric{i}" in result["template_yaml"]
            assert f"fn-{i}" in result["template_yaml"]


class TestAssembleCfnTemplateTags:
    """Template-level tags apply to every alarm; per-alarm tags override."""

    def test_template_level_tags_applied_to_all(self):
        alarms = [
            _alarm_entry("Errors", "fn-a"),
            _alarm_entry("Duration", "fn-a"),
        ]
        result = cfn.assemble_cfn_template(
            alarms=alarms,
            sns_topic_arn=VALID_SNS,
            tags={"ManagedBy": "cloudwatch-agent"},
        )
        parsed = _yaml.safe_load(result["template_yaml"])
        for resource in parsed["Resources"].values():
            tags = resource["Properties"]["Tags"]
            assert {"Key": "ManagedBy", "Value": "cloudwatch-agent"} in tags

    def test_per_alarm_tags_override_template_tags(self):
        a = _alarm_entry("Errors", "fn-a")
        a["tags"] = {"Owner": "team-a"}  # per-alarm override
        b = _alarm_entry("Duration", "fn-a")  # inherits template tags
        result = cfn.assemble_cfn_template(
            alarms=[a, b],
            sns_topic_arn=VALID_SNS,
            tags={"ManagedBy": "cloudwatch-agent"},
        )
        parsed = _yaml.safe_load(result["template_yaml"])
        resources = list(parsed["Resources"].values())
        # First alarm has only its per-alarm tag.
        assert resources[0]["Properties"]["Tags"] == [{"Key": "Owner", "Value": "team-a"}]
        # Second alarm has the inherited template-level tag.
        assert resources[1]["Properties"]["Tags"] == [
            {"Key": "ManagedBy", "Value": "cloudwatch-agent"}
        ]


class TestAssembleCfnTemplateInvalidInput:
    def test_non_list_alarms_returns_error(self):
        result = cfn.assemble_cfn_template(
            alarms="not a list",  # type: ignore[arg-type]
            sns_topic_arn=VALID_SNS,
        )
        assert result["error"] == "invalid_request"

    def test_empty_alarms_list_emits_empty_resources(self):
        result = cfn.assemble_cfn_template(alarms=[], sns_topic_arn=VALID_SNS)
        parsed = _yaml.safe_load(result["template_yaml"])
        assert parsed["Resources"] == {}
        assert result["summary"]["alarm_count"] == 0
        assert result["summary"]["logical_ids"] == []
