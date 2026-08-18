"""
recommendations.py — module-level catalogue lookup helpers.

The vendored ``data/metric_metadata.json`` snapshot from
``awslabs.cloudwatch-mcp-server==0.1.4`` is loaded once at module
import (cold start) into a ``CATALOGUE`` dict keyed by
``(namespace, metric_name)`` for O(1) lookup. The 1,179-entry list
parses comfortably under the 200ms cold-start budget — pure JSON +
dict comprehension, no AWS calls.

Two pure-Python helpers expose the catalogue to the handler:

* ``get_metric_metadata_from_catalogue(namespace, metric_name)``
  returns the full catalogue entry (description,
  recommendedStatistics, unitInfo, optional alarmRecommendations) or
  ``None`` on miss. The handler's ``get_metric_metadata`` tool wraps
  this directly.

* ``get_recommended_alarms_from_catalogue(namespace, metric_name)``
  returns the entry's ``alarmRecommendations`` list or ``[]`` on miss
  (catalogue miss is a first-class result, not an error — see design.md
  "Property 5: Catalogue lookup totality"). The handler's
  ``get_recommended_metric_alarms`` tool wraps this and adds the
  ``namespace`` / ``metric_name`` / ``dimensions`` annotation.

Both helpers are total: they never raise on bad input, they return
either the canonical dict-or-None / list shape per the schema.
"""

from __future__ import annotations

import json
import os
import hashlib
from copy import deepcopy

CATALOGUE_VERSION = "awslabs-cloudwatch-mcp-0.1.4+coverage-v1"

# ---------------------------------------------------------------------------
# Cold-start catalogue load
# ---------------------------------------------------------------------------

# Resolve the JSON path relative to this module so the same code works
# whether the Lambda runs in /var/task (deployed) or under pytest (local
# repo). The data directory is always co-located with this file.
_CATALOGUE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "data",
    "metric_metadata.json",
)

with open(_CATALOGUE_PATH, encoding="utf-8") as _f:
    _RAW_ENTRIES = json.load(_f)

# Catalogue keyed by (namespace, metric_name) for O(1) lookup. The
# vendored JSON has unique (namespace, metric_name) pairs by construction
# — duplicates would silently collapse here, which is acceptable since the
# catalogue is the source of truth for the lookup.
CATALOGUE: dict[tuple[str, str], dict] = {
    (entry["metricId"]["namespace"], entry["metricId"]["metricName"]): entry
    for entry in _RAW_ENTRIES
}

# Free the raw list — we only need the dict from here on.
del _RAW_ENTRIES


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def get_metric_metadata_from_catalogue(namespace: str, metric_name: str) -> dict | None:
    """Return the catalogue entry for ``(namespace, metric_name)`` or None.

    The full entry includes (at minimum) ``metricId``, ``description``,
    ``recommendedStatistics``, and ``unitInfo``. Entries with embedded
    alarm recommendations also include ``alarmRecommendations``.

    Total over arbitrary input: never raises. A non-string namespace
    or metric_name simply misses the dict and returns None.
    """
    return CATALOGUE.get((namespace, metric_name))


def get_recommended_alarms_from_catalogue(
    namespace: str, metric_name: str
) -> list[dict]:
    """Return the entry's ``alarmRecommendations`` list or ``[]`` on miss.

    Catalogue miss returns ``[]`` rather than ``None`` so callers can
    iterate without a None-check. Many catalogue entries have no embedded
    alarm recommendations; those also return ``[]``.

    Validates: design.md Property 5 (Catalogue lookup totality) — the
    function is total: never raises, never returns None, always returns
    a list of dicts (possibly empty).
    """
    entry = CATALOGUE.get((namespace, metric_name))
    if entry is None:
        return []
    return [
        _with_identity(namespace, metric_name, index, recommendation)
        for index, recommendation in enumerate(entry.get("alarmRecommendations", []))
    ]


def get_namespace_alarm_recommendations(namespace: str) -> dict[str, list[dict]]:
    """Return ``{metric_name: alarmRecommendations}`` for every catalogue entry
    in *namespace* that carries at least one alarm recommendation.

    This is the enumeration backing ``analyze_alarm_coverage``: given a
    namespace (e.g. ``AWS/Lambda``) it yields the full recommended-alarm set
    the account's existing alarms are graded against. Entries without alarm
    recommendations are omitted so the caller only sees actionable metrics.

    Total over arbitrary input: a namespace with no catalogue entries simply
    returns an empty dict.
    """
    result: dict[str, list[dict]] = {}
    for (entry_namespace, metric_name), entry in CATALOGUE.items():
        if entry_namespace != namespace:
            continue
        recommendations = get_recommended_alarms_from_catalogue(
            entry_namespace, metric_name
        )
        if recommendations:
            result[metric_name] = recommendations
    return result


def _with_identity(
    namespace: str,
    metric_name: str,
    index: int,
    recommendation: dict,
) -> dict:
    """Return a defensive recommendation copy with a deterministic identity."""
    result = deepcopy(recommendation)
    signature = {
        "namespace": namespace,
        "metric_name": metric_name,
        "dimensions": sorted(
            dimension.get("name", dimension.get("Name", ""))
            for dimension in recommendation.get("dimensions", [])
        ),
        "comparison_operator": recommendation.get("comparisonOperator"),
        "statistic": recommendation.get("statistic"),
        "period": recommendation.get("period"),
        "intent": recommendation.get("intent"),
        "index": index,
    }
    digest = hashlib.sha256(
        json.dumps(signature, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    result["recommendationId"] = f"cwr-{digest}"
    result["catalogueVersion"] = CATALOGUE_VERSION
    result["namespace"] = namespace
    result["metricName"] = metric_name
    return result


def find_recommendation(recommendation_id: str) -> dict | None:
    """Find one recommendation by deterministic ID."""
    for namespace, metric_name in CATALOGUE:
        for recommendation in get_recommended_alarms_from_catalogue(
            namespace, metric_name
        ):
            if recommendation["recommendationId"] == recommendation_id:
                return recommendation
    return None
