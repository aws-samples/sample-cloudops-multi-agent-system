"""
analysis.py — pure statistical analysis of CloudWatch metric time series.

Exposes ``analyse_metric_data(values, timestamps)`` which returns the
``AnalyseMetricResult`` shape from design.md (without the ``metric`` /
``window`` blocks — the handler annotates those):

    {
        "stats": {mean, median, p50, p90, p99, stddev,
                  coefficient_of_variation, min, max},
        "trend": "INCREASING" | "DECREASING" | "NONE",
        "seasonality_seconds": int | None,
        "noise_score": float,
        "data_quality": {publishing_period_seconds, density_pct},
    }

Pure module: no AWS calls, no I/O, no module-level state beyond
constants. The handler.py boto3 fetch (Task 6) feeds (values, timestamps)
into this function.

Implementation notes
--------------------

* Trend uses ``numpy.polyfit`` linear regression on (index, value).
  We compare the regression's "predicted total drift" (``|slope| * n``)
  to a 5% relative-range dead-band (``0.05 * (max - min)``). A small
  relative drift returns NONE so pure noise stays NONE while real
  ramps fire INCREASING / DECREASING. The 5% threshold is small
  enough that genuine trends still fire and large enough to absorb
  the regression-slope variance of typical CloudWatch noise.

* Seasonality uses ``numpy.fft.fft`` on the **linearly detrended**
  series — without detrending, a pure ramp puts all its mean-removed
  energy into the lowest non-DC bin and gets misclassified as a
  long-period cycle. Once detrended:
    1. If detrending consumed nearly all variance (``detrended_var /
       original_var < 0.01``), the series is effectively linear and
       has no seasonality.
    2. We look at positive frequencies only — DC (index 0) and
       Nyquist (index ``n//2`` when ``n`` is even) are excluded.
    3. The dominant peak must hold at least 10% of the total
       positive-frequency energy. A clean sinusoid puts ~all its
       positive-frequency energy in one bin (ratio ≈ 1.0); white
       noise spreads it roughly evenly across ``~n/2`` bins, so the
       max bin is around ``log(n/2)/(n/2)`` of the total — well
       below 10% for typical ``n``.
  Period is ``1.0 / freqs[idx]``, rounded to integer seconds.

* Noise score is ``stddev / |mean|``. Many CW metrics (e.g.
  AWS/Lambda Errors) have mean ≈ 0; in that case we fall back to
  ``stddev`` alone so the value is still comparable across windows.

* Density_pct is derived from the median publishing period — robust
  against an occasional missing datapoint. Clamped at 100% because
  raw computation can briefly exceed 100 if a metric was published
  faster than its nominal cadence.

* MIN_DATAPOINTS = 50 — below this we return ``insufficient_history``
  with the count and required threshold so the handler can surface
  the structured error directly.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np


# Minimum datapoints required for meaningful analysis. Below this we
# return an ``insufficient_history`` error and the handler surfaces it.
MIN_DATAPOINTS = 50

# Dead-band multiplier on the regression's predicted total drift before
# a trend is called INCREASING / DECREASING. Tuned on the unit fixtures:
#   * pure noise (seed 42, n=200) stays NONE
#   * linear ramp 0..99 fires INCREASING
_TREND_DEADBAND_FRAC = 0.05

# Seasonality threshold: the dominant non-DC, non-Nyquist frequency's
# power must hold at least this fraction of the total positive-frequency
# energy after linear detrending for us to call it "real seasonality".
# Tuned on the unit fixtures:
#   * pure sinusoid → ratio is essentially 1.0 (single-bin concentration)
#   * pure noise (n=200) → ratio is typically <0.05 across all bins
#   * linear ramp → caught earlier by the detrending-residual check
_SEASONALITY_PEAK_FRACTION = 0.10

# After linearly detrending the series, the residual must retain at
# least this fraction of the original variance for seasonality
# detection to even run. A pure ramp shrinks to ~0 residual variance;
# real seasonal series keep most of theirs.
_DETREND_RESIDUAL_VAR_FRAC = 0.01

# Mean-zero epsilon for the noise-score / CoV fallback.
_MEAN_ZERO_EPSILON = 1e-9


def analyse_metric_data(
    values: list[float], timestamps: list[datetime]
) -> dict[str, Any]:
    """Compute summary stats, trend, seasonality, noise, density.

    Parameters
    ----------
    values
        Numeric metric samples. Length must match ``timestamps``.
    timestamps
        ``datetime`` instances corresponding to each value, in
        ascending order. Used to derive the publishing period and
        data density.

    Returns
    -------
    dict
        Either the ``AnalyseMetricResult`` schema (less the ``metric``
        / ``window`` blocks the handler adds) or
        ``{"error": "insufficient_history", "datapoints_found": n,
           "datapoints_required": MIN_DATAPOINTS}`` when there are
        fewer than ``MIN_DATAPOINTS`` samples.
    """
    n = len(values)
    if n < MIN_DATAPOINTS or len(timestamps) < MIN_DATAPOINTS:
        return {
            "error": "insufficient_history",
            "datapoints_found": n,
            "datapoints_required": MIN_DATAPOINTS,
        }

    arr = np.asarray(values, dtype=float)

    # --- Stats -----------------------------------------------------------
    mean = float(np.mean(arr))
    median_v = float(np.median(arr))
    stddev = float(np.std(arr))
    p50 = float(np.percentile(arr, 50))
    p90 = float(np.percentile(arr, 90))
    p99 = float(np.percentile(arr, 99))
    min_v = float(np.min(arr))
    max_v = float(np.max(arr))

    if abs(mean) > _MEAN_ZERO_EPSILON:
        coefficient_of_variation = stddev / abs(mean)
    else:
        coefficient_of_variation = 0.0

    # --- Publishing period + density ------------------------------------
    deltas = np.diff(np.asarray([t.timestamp() for t in timestamps]))
    positive_deltas = deltas[deltas > 0]
    if positive_deltas.size > 0:
        publishing_period_seconds = int(round(float(np.median(positive_deltas))))
    else:
        publishing_period_seconds = 0

    if publishing_period_seconds > 0:
        window_seconds = (timestamps[-1] - timestamps[0]).total_seconds()
        expected_count = window_seconds / publishing_period_seconds + 1
        if expected_count > 0:
            density_pct = min(100.0, 100.0 * n / expected_count)
        else:
            density_pct = 100.0
    else:
        density_pct = 100.0

    # --- Trend -----------------------------------------------------------
    range_v = max_v - min_v
    if range_v == 0:
        # Degenerate: all values identical. polyfit would emit RankWarning.
        trend = "NONE"
    else:
        indices = np.arange(n, dtype=float)
        slope, _intercept = np.polyfit(indices, arr, 1)
        predicted_drift = abs(slope * n)
        deadband = _TREND_DEADBAND_FRAC * range_v
        if predicted_drift <= deadband:
            trend = "NONE"
        elif slope > 0:
            trend = "INCREASING"
        else:
            trend = "DECREASING"

    # --- Seasonality (FFT) ----------------------------------------------
    seasonality_seconds: int | None = None
    if publishing_period_seconds > 0 and range_v > 0:
        seasonality_seconds = _detect_seasonality(arr, publishing_period_seconds)

    # --- Noise score -----------------------------------------------------
    if abs(mean) > _MEAN_ZERO_EPSILON:
        noise_score = stddev / abs(mean)
    else:
        # mean ~= 0: fall back to stddev so the value stays comparable
        # across windows where the metric is normally zero (e.g. Errors).
        noise_score = stddev

    return {
        "stats": {
            "mean": mean,
            "median": median_v,
            "p50": p50,
            "p90": p90,
            "p99": p99,
            "stddev": stddev,
            "coefficient_of_variation": coefficient_of_variation,
            "min": min_v,
            "max": max_v,
        },
        "trend": trend,
        "seasonality_seconds": seasonality_seconds,
        "noise_score": noise_score,
        "data_quality": {
            "publishing_period_seconds": publishing_period_seconds,
            "density_pct": density_pct,
        },
    }


def _detect_seasonality(arr: np.ndarray, period_seconds: int) -> int | None:
    """FFT dominant-frequency detection on a linearly detrended series.

    Returns the period (in seconds) of the strongest non-DC,
    non-Nyquist frequency component when:
      1. Linear detrending leaves at least ``_DETREND_RESIDUAL_VAR_FRAC``
         of the original variance (otherwise the signal is essentially
         a ramp and has no seasonality).
      2. The dominant peak holds at least
         ``_SEASONALITY_PEAK_FRACTION`` of the total positive-frequency
         energy (otherwise it's noise picking a winner).
    Returns ``None`` when either check fails.
    """
    n = arr.size
    if n < 4:
        return None

    original_var = float(np.var(arr))
    if original_var <= 0:
        return None

    # Linear detrend: subtract a least-squares fit line so a pure ramp
    # collapses to ~zero residual and never reaches the FFT.
    indices = np.arange(n, dtype=float)
    slope, intercept = np.polyfit(indices, arr, 1)
    detrended = arr - (slope * indices + intercept)
    detrended_var = float(np.var(detrended))
    if detrended_var / original_var < _DETREND_RESIDUAL_VAR_FRAC:
        # Series is essentially linear — no seasonal component.
        return None

    spectrum = np.fft.fft(detrended)
    freqs = np.fft.fftfreq(n, d=float(period_seconds))
    power = np.abs(spectrum) ** 2

    # Positive-frequency band: indices 1 .. n//2 - 1 inclusive.
    # Skip 0 (DC) and n//2 (Nyquist when n is even).
    nyquist_idx = n // 2
    positive_power = power[1:nyquist_idx]
    positive_freqs = freqs[1:nyquist_idx]
    if positive_power.size == 0:
        return None

    total_positive_power = float(positive_power.sum())
    if total_positive_power <= 0:
        return None

    peak_idx = int(np.argmax(positive_power))
    peak_power = float(positive_power[peak_idx])
    peak_freq = float(positive_freqs[peak_idx])
    if peak_freq <= 0:
        return None

    if peak_power / total_positive_power < _SEASONALITY_PEAK_FRACTION:
        return None

    period = 1.0 / peak_freq
    return int(round(period))
