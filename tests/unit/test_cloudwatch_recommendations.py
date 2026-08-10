"""Unit + property tests for src/lambda/mcp/cloudwatch/recommendations.py.

Covers:

* Hits against representative ``(namespace, metric_name)`` pairs across
  three different shapes of catalogue entry: with embedded alarm
  recommendations (Lambda Errors, RDS CPUUtilization), and without
  (EKS scheduler_pending_pods).
* Catalogue misses returning ``None`` for metadata and ``[]`` for
  recommendations — exercising design.md Property 5 (Catalogue lookup
  totality): no exceptions, canonical empty result on miss.
* Hypothesis property test that fuzzes arbitrary ``(str, str)`` pairs
  and asserts both helpers stay total and return the documented types.
* Cold-start timing fixture: ``importlib.reload`` reloads the module
  from a fresh import (re-running the JSON parse + dict comprehension)
  and asserts the load completes under 500ms. Threshold is generous
  per the spec note (200ms is the target, 500ms is the flake-resistant
  bound on shared CI machines).

The ``recommendations`` module is loaded by file path (same pattern as
``tests/unit/test_cloudwatch_arn.py`` and
``tests/unit/test_cloudwatch_analysis.py``) since
``src/lambda/mcp/cloudwatch/`` is not on the default ``sys.path``.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import time
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RECS_PATH = (
    _REPO_ROOT / "src" / "lambda" / "mcp" / "cloudwatch" / "recommendations.py"
)


def _load_recommendations_module(module_name: str = "cloudwatch_recommendations"):
    """Load recommendations.py under ``module_name`` from its file path.

    Each call returns a freshly executed module object, so callers that
    want a true "cold start" measurement can simply call this again
    rather than relying on ``importlib.reload`` (which preserves
    module-level state).
    """
    spec = importlib.util.spec_from_file_location(module_name, _RECS_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# Module-level load for the bulk of the tests.
recommendations = _load_recommendations_module()


# ---------------------------------------------------------------------------
# Catalogue shape sanity checks
# ---------------------------------------------------------------------------


class TestCatalogueShape:
    def test_catalogue_has_1179_entries(self):
        # Vendored from awslabs.cloudwatch-mcp-server==0.1.4 — known size.
        assert len(recommendations.CATALOGUE) == 1179

    def test_catalogue_keys_are_namespace_metric_pairs(self):
        for key in recommendations.CATALOGUE:
            assert isinstance(key, tuple)
            assert len(key) == 2
            namespace, metric_name = key
            assert isinstance(namespace, str)
            assert isinstance(metric_name, str)
            assert namespace  # non-empty
            assert metric_name


# ---------------------------------------------------------------------------
# get_metric_metadata_from_catalogue
# ---------------------------------------------------------------------------


class TestGetMetricMetadata:
    def test_lambda_errors_returns_full_entry(self):
        entry = recommendations.get_metric_metadata_from_catalogue(
            "AWS/Lambda", "Errors"
        )
        assert entry is not None
        assert entry["metricId"]["namespace"] == "AWS/Lambda"
        assert entry["metricId"]["metricName"] == "Errors"
        # Lambda Errors carries an embedded alarm recommendation.
        assert "alarmRecommendations" in entry
        assert "description" in entry
        assert "recommendedStatistics" in entry
        assert "unitInfo" in entry

    def test_rds_cpu_utilization_returns_full_entry(self):
        entry = recommendations.get_metric_metadata_from_catalogue(
            "AWS/RDS", "CPUUtilization"
        )
        assert entry is not None
        assert entry["metricId"]["namespace"] == "AWS/RDS"
        assert entry["metricId"]["metricName"] == "CPUUtilization"
        assert "alarmRecommendations" in entry

    def test_eks_no_alarm_recommendations_entry(self):
        # scheduler_pending_pods has metadata but no alarmRecommendations
        # — verifies the helper returns the entry as-is, no synthesis.
        entry = recommendations.get_metric_metadata_from_catalogue(
            "AWS/EKS", "scheduler_pending_pods"
        )
        assert entry is not None
        assert entry["metricId"]["namespace"] == "AWS/EKS"
        assert entry["metricId"]["metricName"] == "scheduler_pending_pods"
        assert "alarmRecommendations" not in entry
        assert entry["recommendedStatistics"] == "Sum"

    def test_miss_returns_none(self):
        assert (
            recommendations.get_metric_metadata_from_catalogue(
                "AWS/NotAService", "DefinitelyNotAMetric"
            )
            is None
        )

    def test_miss_with_correct_namespace_wrong_metric(self):
        assert (
            recommendations.get_metric_metadata_from_catalogue(
                "AWS/Lambda", "ThisMetricDoesNotExist"
            )
            is None
        )


# ---------------------------------------------------------------------------
# get_recommended_alarms_from_catalogue
# ---------------------------------------------------------------------------


class TestGetRecommendedAlarms:
    def test_lambda_errors_returns_non_empty_list(self):
        recs = recommendations.get_recommended_alarms_from_catalogue(
            "AWS/Lambda", "Errors"
        )
        assert isinstance(recs, list)
        assert len(recs) >= 1
        rec = recs[0]
        # Sanity-check the recommendation shape matches design.md
        # "Recommended alarm (catalogue entry)".
        for required_key in (
            "comparisonOperator",
            "datapointsToAlarm",
            "evaluationPeriods",
            "period",
            "statistic",
            "threshold",
            "treatMissingData",
            "dimensions",
            "intent",
            "alarmDescription",
        ):
            assert required_key in rec, f"missing key: {required_key}"

    def test_rds_cpu_utilization_returns_non_empty_list(self):
        recs = recommendations.get_recommended_alarms_from_catalogue(
            "AWS/RDS", "CPUUtilization"
        )
        assert isinstance(recs, list)
        assert len(recs) >= 1

    def test_eks_metric_without_recommendations_returns_empty_list(self):
        # The catalogue has the entry but no alarmRecommendations field.
        recs = recommendations.get_recommended_alarms_from_catalogue(
            "AWS/EKS", "scheduler_pending_pods"
        )
        assert recs == []

    def test_miss_returns_empty_list(self):
        # Property 5: catalogue miss returns [], never None, never raises.
        recs = recommendations.get_recommended_alarms_from_catalogue(
            "AWS/NotAService", "DefinitelyNotAMetric"
        )
        assert recs == []


# ---------------------------------------------------------------------------
# Property 5 — Catalogue lookup totality
# ---------------------------------------------------------------------------


class TestCatalogueLookupTotality:
    """Validates: Requirements 1.2, 2.3 (design.md Property 5)."""

    @given(
        namespace=st.text(max_size=64),
        metric_name=st.text(max_size=64),
    )
    @settings(
        max_examples=300,
        suppress_health_check=[HealthCheck.too_slow],
    )
    def test_metadata_lookup_total_over_string_pairs(
        self, namespace: str, metric_name: str
    ):
        """For any (str, str) pair, the helper returns dict or None."""
        result = recommendations.get_metric_metadata_from_catalogue(
            namespace, metric_name
        )
        assert result is None or isinstance(result, dict)

    @given(
        namespace=st.text(max_size=64),
        metric_name=st.text(max_size=64),
    )
    @settings(
        max_examples=300,
        suppress_health_check=[HealthCheck.too_slow],
    )
    def test_recommendations_lookup_total_over_string_pairs(
        self, namespace: str, metric_name: str
    ):
        """For any (str, str) pair, the helper returns a list of dicts."""
        result = recommendations.get_recommended_alarms_from_catalogue(
            namespace, metric_name
        )
        assert isinstance(result, list)
        for item in result:
            assert isinstance(item, dict)


# ---------------------------------------------------------------------------
# Cold-start timing
# ---------------------------------------------------------------------------


class TestColdStartTiming:
    """Verify the catalogue parses fast enough for Lambda cold start.

    The spec target is under 200ms, but on shared CI machines under
    light load the JSON parse + dict comprehension can drift up. We
    use a generous 500ms threshold — meaningful enough to catch a real
    regression (e.g. an O(n²) lookup index, or a deserialiser swap)
    without flaking on noisy hardware.
    """

    def test_module_load_under_500ms(self):
        # Wipe any cached copy so the import does the full JSON parse.
        sys.modules.pop("cloudwatch_recommendations_coldstart", None)

        start = time.perf_counter()
        module = _load_recommendations_module(
            "cloudwatch_recommendations_coldstart"
        )
        elapsed_ms = (time.perf_counter() - start) * 1000

        # Sanity-check: the freshly-loaded module actually built the dict.
        assert len(module.CATALOGUE) == 1179
        assert (
            elapsed_ms < 500
        ), f"cold-start took {elapsed_ms:.1f}ms, expected <500ms"
