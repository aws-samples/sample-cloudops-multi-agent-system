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


def get_metric_metadata_from_catalogue(
    namespace: str, metric_name: str
) -> dict | None:
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
    return entry.get("alarmRecommendations", [])
