"""
Tag Governance Collector — scheduled snapshot of org tag-compliance posture.

Triggered by an EventBridge SCHEDULE (default every 6 hours), not by events:
unlike AWS Health, nothing emits "resource became non-compliant" — compliance
is a derived scan, so the collector sweeps on a timer.

Architecture:
    EventBridge schedule → This Lambda → invoke tag-governance TOOL Lambda
                                       → DynamoDB (tag-compliance snapshot table)

Design decision — invoke the tool, don't reimplement it:
    The tag-governance MCP tool already owns the policy resolution, Resource
    Explorer sweep, and per-resource classification (~700 lines, carefully
    tuned: match-all query semantics, system-managed filtering, dedup,
    violation buckets). Duplicating that here would drift. Instead this
    collector calls the SAME Lambda with the same event shapes users trigger,
    and stores the responses. The tool then serves user requests from the
    snapshot (millisecond DynamoDB read) instead of a 3–60s live sweep.

What gets snapshotted (the "canonical" shapes — no caller filters):
    check_tag_compliance            {}                       (full sweep, in-Python mode)
    get_org_tag_compliance_summary  {"group_by": "TARGET_ID"} (only if a Tag Policy is attached)
    find_untagged_resources         {}
    list_tag_keys_in_use            {}
    get_required_tags               {}

Requests WITH caller filters (specific resource_types, regions, a
required_tags override…) always go live in the tool — a snapshot computed
for the canonical query cannot answer a filtered one honestly.

Item layout (single table, TTL'd):
    pk = "CACHE"    sk = <operation>   payload=<full JSON response>, snapshot_at, ttl
    pk = "META"     sk = "LAST_RUN"    run summary for observability
"""

import base64
import json
import logging
import os
import time
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

TABLE_NAME = os.environ.get("TAG_SNAPSHOT_TABLE_NAME", "")
TOOL_FUNCTION_NAME = os.environ.get("TAG_TOOL_FUNCTION_NAME", "")
SNAPSHOT_TTL_HOURS = int(os.environ.get("SNAPSHOT_TTL_HOURS", "48"))
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Operations swept every run, with their canonical (unfiltered) event shapes.
# get_org_tag_compliance_summary is attempted but tolerated to fail — it
# requires an attached AWS Tag Policy + management-account access, and the
# error response is itself worth caching (the tool would return the same
# error live, just slower).
#
# force_refresh on EVERY payload is load-bearing, not belt-and-braces: the
# tool serves canonical queries from this collector's own snapshot table, so
# without it the second scheduled run would read the cache and re-store the
# same bytes forever — a snapshot that never refreshes. force_refresh pins
# the collector to the live path; it is stripped from what gets stored only
# in the sense that responses never echo request fields.
CANONICAL_OPS: list[tuple[str, dict]] = [
    ("get_required_tags", {"force_refresh": True}),
    ("check_tag_compliance", {"max_resources": 1000, "force_refresh": True}),
    ("find_untagged_resources", {"max_resources": 1000, "force_refresh": True}),
    ("list_tag_keys_in_use", {"force_refresh": True}),
    ("get_org_tag_compliance_summary", {"group_by": "TARGET_ID", "force_refresh": True}),
]

# DynamoDB hard item limit is 400KB. check_tag_compliance against a large
# estate can carry a big non_compliant_resources detail list; trim the detail
# list (never the counts — those are pre-computed over the full set) until the
# item fits. The tool re-truncates to the caller's max_resources anyway.
_MAX_ITEM_BYTES = 350_000
_TRIMMABLE_LIST_KEYS = ("non_compliant_resources", "untagged_resources", "resources")


def _lambda_client():
    return boto3.client("lambda", region_name=AWS_REGION)


def _ddb():
    return boto3.client("dynamodb", region_name=AWS_REGION)


def _invoke_tool(operation: str, payload: dict) -> dict:
    """Invoke the tag-governance tool Lambda exactly as the gateway would.

    The tool handler routes on
    ``context.client_context.custom["bedrockAgentCoreToolName"]`` — Lambda's
    ``ClientContext`` parameter delivers that verbatim, so no tool-side
    changes are needed for direct invocation.
    """
    ctx = base64.b64encode(
        json.dumps(
            {"custom": {"bedrockAgentCoreToolName": f"tag-governance___{operation}"}}
        ).encode()
    ).decode()
    resp = _lambda_client().invoke(
        FunctionName=TOOL_FUNCTION_NAME,
        InvocationType="RequestResponse",
        ClientContext=ctx,
        Payload=json.dumps(payload).encode(),
    )
    body = resp["Payload"].read().decode()
    if resp.get("FunctionError"):
        raise RuntimeError(f"{operation} invoke failed: {body[:300]}")
    return json.loads(body)


def _shrink_to_fit(response: dict) -> tuple[dict, bool]:
    """Trim detail lists until the serialized response fits the DDB item cap.

    Counts/breakdowns are computed by the tool over the FULL result set before
    truncation, so trimming details here loses granularity, never correctness.
    """
    trimmed = False
    body = json.dumps(response, default=str)
    while len(body.encode()) > _MAX_ITEM_BYTES:
        for key in _TRIMMABLE_LIST_KEYS:
            lst = response.get(key)
            if isinstance(lst, list) and lst:
                del lst[len(lst) // 2 :]  # halve from the tail
                trimmed = True
                break
        else:
            # Nothing left to trim — store an explicit failure marker instead
            # of a silently-absent snapshot.
            return (
                {"error": "snapshot too large for DynamoDB item", "operation": "?"},
                True,
            )
        body = json.dumps(response, default=str)
    if trimmed:
        response["snapshot_note"] = (
            "Detail list trimmed to fit storage; counts and breakdowns reflect "
            "the full scan. Use force_refresh=true for the complete detail list."
        )
    return response, trimmed


def _put_cache_item(operation: str, response: dict, snapshot_at: str) -> None:
    ttl = int(time.time()) + SNAPSHOT_TTL_HOURS * 3600
    _ddb().put_item(
        TableName=TABLE_NAME,
        Item={
            "pk": {"S": "CACHE"},
            "sk": {"S": operation},
            "payload": {"S": json.dumps(response, default=str)},
            "snapshot_at": {"S": snapshot_at},
            "ttl": {"N": str(ttl)},
        },
    )


def handler(event, context):
    """Sweep every canonical operation and persist the responses."""
    if not TABLE_NAME or not TOOL_FUNCTION_NAME:
        raise RuntimeError(
            "TAG_SNAPSHOT_TABLE_NAME and TAG_TOOL_FUNCTION_NAME must be set"
        )

    snapshot_at = datetime.now(timezone.utc).isoformat()
    results: dict[str, str] = {}

    for operation, payload in CANONICAL_OPS:
        started = time.monotonic()
        try:
            response = _invoke_tool(operation, payload)
            response, trimmed = _shrink_to_fit(response)
            _put_cache_item(operation, response, snapshot_at)
            elapsed = time.monotonic() - started
            status = "ok+trimmed" if trimmed else "ok"
            # A tool-level error payload (e.g. no Tag Policy attached) is still
            # a valid snapshot — the tool would return it live too. Record it
            # distinctly so the run summary shows what's error-cached.
            if isinstance(response, dict) and response.get("error"):
                status = "error-cached"
            results[operation] = f"{status} ({elapsed:.1f}s)"
            logger.info("snapshot %s: %s", operation, results[operation])
        except Exception as exc:
            results[operation] = f"FAILED: {exc}"
            logger.error("snapshot %s failed: %s", operation, exc)

    # Run summary for observability / debugging staleness questions.
    _ddb().put_item(
        TableName=TABLE_NAME,
        Item={
            "pk": {"S": "META"},
            "sk": {"S": "LAST_RUN"},
            "snapshot_at": {"S": snapshot_at},
            "results": {"S": json.dumps(results)},
            "ttl": {"N": str(int(time.time()) + SNAPSHOT_TTL_HOURS * 3600)},
        },
    )

    failed = [op for op, r in results.items() if r.startswith("FAILED")]
    logger.info("=== tag snapshot done: %d ops, %d failed ===", len(results), len(failed))
    return {"snapshot_at": snapshot_at, "results": results}
