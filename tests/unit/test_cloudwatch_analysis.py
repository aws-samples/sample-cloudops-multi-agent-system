"""Unit tests for src/lambda/mcp/cloudwatch/analysis.py.

Covers the four canonical fixtures from the spec (constant, linear ramp,
sinusoid, noise) plus edge cases (insufficient datapoints, mean-zero
fallback, density on a sparse window) and one Hypothesis property test
for Property 6 — totality on sufficient data.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

# analysis.py lives under src/lambda/mcp/cloudwatch/ — load it directly
# so we don't need src/ on the default sys.path. The same trick used by
# tests/unit/test_tag_governance_tool.py.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ANALYSIS_PATH = _REPO_ROOT / "src" / "lambda" / "mcp" / "cloudwatch" / "analysis.py"
_spec = importlib.util.spec_from_file_location(
    "cloudwatch_analysis", _ANALYSIS_PATH
)
analysis = importlib.util.module_from_spec(_spec)
sys.modules["cloudwatch_analysis"] = analysis
_spec.loader.exec_module(analysis)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _timestamps(n: int, period_seconds: int = 60) -> list[datetime]:
    """Generate ``n`` timestamps spaced ``period_seconds`` apart."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [base + timedelta(seconds=i * period_seconds) for i in range(n)]


def _required_keys(result: dict) -> None:
    """Property 6: results on sufficient data have the full schema."""
    assert "stats" in result
    assert "trend" in result
    assert "seasonality_seconds" in result
    assert "noise_score" in result
    assert "data_quality" in result

    stats = result["stats"]
    for k in (
        "mean",
        "median",
        "p50",
        "p90",
        "p99",
        "stddev",
        "coefficient_of_variation",
        "min",
        "max",
    ):
        assert k in stats, f"missing stats key: {k}"

    dq = result["data_quality"]
    assert "publishing_period_seconds" in dq
    assert "density_pct" in dq


# ---------------------------------------------------------------------------
# Constant signal
# ---------------------------------------------------------------------------

class TestConstantSignal:
    def test_constant_signal_no_trend_no_variance_no_seasonality(self):
        values = [42.0] * 100
        result = analysis.analyse_metric_data(values, _timestamps(100))

        _required_keys(result)
        assert result["trend"] == "NONE"
        assert result["stats"]["stddev"] == pytest.approx(0.0)
        assert result["stats"]["mean"] == pytest.approx(42.0)
        assert result["stats"]["min"] == pytest.approx(42.0)
        assert result["stats"]["max"] == pytest.approx(42.0)
        assert result["seasonality_seconds"] is None
        # Coefficient of variation is 0 when stddev is 0.
        assert result["stats"]["coefficient_of_variation"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Linear ramp
# ---------------------------------------------------------------------------

class TestLinearRamp:
    def test_increasing_ramp(self):
        values = [float(i) for i in range(100)]
        result = analysis.analyse_metric_data(values, _timestamps(100))

        _required_keys(result)
        assert result["trend"] == "INCREASING"
        assert result["seasonality_seconds"] is None
        # Sanity-check the stats against the known ramp.
        assert result["stats"]["min"] == pytest.approx(0.0)
        assert result["stats"]["max"] == pytest.approx(99.0)
        assert result["stats"]["mean"] == pytest.approx(49.5)

    def test_decreasing_ramp(self):
        values = [float(99 - i) for i in range(100)]
        result = analysis.analyse_metric_data(values, _timestamps(100))

        assert result["trend"] == "DECREASING"
        assert result["seasonality_seconds"] is None


# ---------------------------------------------------------------------------
# Sinusoid (seasonality detection)
# ---------------------------------------------------------------------------

class TestSinusoidalSeasonality:
    def test_one_minute_step_with_60_step_period_yields_3600_second_period(self):
        # 600 samples × 60s spacing = 36,000 seconds covered.
        # Period of 60 samples × 60s = 3600s.
        n = 600
        period_steps = 60
        values = [math.sin(2 * math.pi * i / period_steps) for i in range(n)]
        result = analysis.analyse_metric_data(values, _timestamps(n, 60))

        _required_keys(result)
        assert result["seasonality_seconds"] is not None
        # Allow ±5% rounding error from FFT bin discretisation.
        assert (
            abs(result["seasonality_seconds"] - 3600) <= 0.05 * 3600
        ), f"expected ~3600s, got {result['seasonality_seconds']}"


# ---------------------------------------------------------------------------
# Pure noise
# ---------------------------------------------------------------------------

class TestPureNoise:
    def test_zero_mean_gaussian_noise(self):
        values = np.random.RandomState(42).randn(200).tolist()
        result = analysis.analyse_metric_data(values, _timestamps(200))

        _required_keys(result)
        # Pure noise has no real trend.
        assert result["trend"] == "NONE"
        # No clear seasonality should rise above the 3× median power floor.
        assert result["seasonality_seconds"] is None
        # Mean is ~0 so the noise_score falls back to stddev.
        assert result["noise_score"] > 0.5


# ---------------------------------------------------------------------------
# Insufficient data
# ---------------------------------------------------------------------------

class TestInsufficientHistory:
    def test_below_threshold_returns_structured_error(self):
        n = analysis.MIN_DATAPOINTS - 1
        values = [1.0] * n
        result = analysis.analyse_metric_data(values, _timestamps(n))

        assert result == {
            "error": "insufficient_history",
            "datapoints_found": n,
            "datapoints_required": analysis.MIN_DATAPOINTS,
        }

    def test_zero_datapoints(self):
        result = analysis.analyse_metric_data([], [])
        assert result["error"] == "insufficient_history"
        assert result["datapoints_found"] == 0


# ---------------------------------------------------------------------------
# Data quality
# ---------------------------------------------------------------------------

class TestDataQuality:
    def test_dense_one_minute_publishing(self):
        n = 100
        result = analysis.analyse_metric_data([1.0] * n, _timestamps(n, 60))
        assert result["data_quality"]["publishing_period_seconds"] == 60
        assert result["data_quality"]["density_pct"] == pytest.approx(100.0)

    def test_density_clamped_at_100(self):
        # Slight cadence jitter can push raw density above 100; ensure clamp.
        n = 100
        ts = _timestamps(n, 60)
        # Compress a few samples to simulate faster-than-nominal publishing.
        ts[10] = ts[9] + timedelta(seconds=30)
        result = analysis.analyse_metric_data([float(i) for i in range(n)], ts)
        assert result["data_quality"]["density_pct"] <= 100.0


# ---------------------------------------------------------------------------
# Noise score fallback
# ---------------------------------------------------------------------------

class TestNoiseScoreFallback:
    def test_mean_zero_falls_back_to_stddev(self):
        # Symmetric series around 0 — mean ≈ 0.
        values = [-1.0, 1.0] * 50
        result = analysis.analyse_metric_data(values, _timestamps(100))
        # Mean is 0 so noise_score == stddev (1.0 for ±1 series).
        assert result["stats"]["mean"] == pytest.approx(0.0)
        assert result["noise_score"] == pytest.approx(result["stats"]["stddev"])


# ---------------------------------------------------------------------------
# Property test (Hypothesis) — Property 6: analysis totality
# ---------------------------------------------------------------------------

class TestAnalysisTotality:
    """Validates: Requirements 1.2 (Property 6 — Analysis totality)."""

    def test_property_any_sufficient_input_has_full_schema(self):
        from hypothesis import given, settings, strategies as st

        @given(
            values=st.lists(
                st.floats(
                    min_value=-1e6,
                    max_value=1e6,
                    allow_nan=False,
                    allow_infinity=False,
                ),
                min_size=analysis.MIN_DATAPOINTS,
                max_size=200,
            ),
        )
        @settings(max_examples=50, deadline=None)
        def _check(values: list[float]):
            result = analysis.analyse_metric_data(
                values, _timestamps(len(values))
            )
            # Property 6: full schema, no error key.
            assert "error" not in result
            _required_keys(result)
            assert result["trend"] in ("INCREASING", "DECREASING", "NONE")
            seasonality = result["seasonality_seconds"]
            assert seasonality is None or isinstance(seasonality, int)

        _check()
