"""Snapshot freshness, tag filtering, and opaque cursor helpers."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
from typing import Any

FRESH_HOURS = 8
USABLE_HOURS = 48
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
SCHEMA_VERSION = "cloudwatch-coverage-v1"


def freshness(
    collected_at: str | dt.datetime,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    collected = _parse_time(collected_at)
    current = now or dt.datetime.now(dt.timezone.utc)
    age_seconds = max(0, int((current - collected).total_seconds()))
    if age_seconds <= FRESH_HOURS * 3600:
        state = "fresh"
    elif age_seconds <= USABLE_HOURS * 3600:
        state = "stale"
    else:
        state = "expired"
    return {
        "state": state,
        "age_seconds": age_seconds,
        "refresh_required": state != "fresh",
        "servable": state != "expired",
    }


def matches_tags(
    resource_tags: dict[str, str],
    filters: dict[str, str | list[str]],
) -> bool:
    """AND tag keys and OR values for each key."""
    for key, raw_values in filters.items():
        values = raw_values if isinstance(raw_values, list) else [raw_values]
        if resource_tags.get(key) not in {str(value) for value in values}:
            return False
    return True


def query_hash(query: dict[str, Any]) -> str:
    canonical = json.dumps(query, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def encode_cursor(
    snapshot_id: str,
    query_digest: str,
    offset: int,
    secret: str,
) -> str:
    payload = json.dumps(
        {
            "snapshot_id": snapshot_id,
            "query_hash": query_digest,
            "offset": offset,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    return (
        base64.urlsafe_b64encode(payload + b"." + signature).decode("ascii").rstrip("=")
    )


def decode_cursor(
    cursor: str,
    snapshot_id: str,
    query_digest: str,
    secret: str,
) -> int:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload, signature = raw.rsplit(b".", 1)
        expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("cursor signature mismatch")
        decoded = json.loads(payload)
        if decoded["snapshot_id"] != snapshot_id:
            raise ValueError("cursor snapshot mismatch")
        if decoded["query_hash"] != query_digest:
            raise ValueError("cursor query mismatch")
        offset = int(decoded["offset"])
        if offset < 0:
            raise ValueError("cursor offset is negative")
        return offset
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid cursor") from exc


def bounded_page_size(value: Any) -> int:
    try:
        return max(1, min(int(value), MAX_PAGE_SIZE))
    except (TypeError, ValueError):
        return DEFAULT_PAGE_SIZE


def _parse_time(value: str | dt.datetime) -> dt.datetime:
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)
