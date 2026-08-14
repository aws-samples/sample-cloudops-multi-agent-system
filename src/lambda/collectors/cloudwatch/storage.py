"""DynamoDB persistence for immutable CloudWatch coverage runs."""

from __future__ import annotations

import math
import time
from decimal import Decimal
from typing import Any, Callable

from boto3.dynamodb.conditions import Attr, Key


def _normalize_numbers(value: Any) -> Any:
    """Convert nested floats to DynamoDB-compatible exact decimals."""
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("DynamoDB rows cannot contain non-finite numbers")
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: _normalize_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_numbers(item) for item in value]
    if isinstance(value, tuple):
        return [_normalize_numbers(item) for item in value]
    if isinstance(value, set):
        return {_normalize_numbers(item) for item in value}
    return value


def put_run_if_absent(
    table,
    run_id: str,
    account_id: str,
    regions: list[str],
    created_at: str,
    catalogue_version: str,
    schema_version: str,
) -> bool:
    try:
        table.put_item(
            Item={
                "pk": f"RUN#{run_id}",
                "sk": "META",
                "run_id": run_id,
                "account_id": account_id,
                "status": "collecting",
                "regions": regions,
                "expected_region_count": len(regions),
                "created_at": created_at,
                "catalogue_version": catalogue_version,
                "schema_version": schema_version,
            },
            ConditionExpression=Attr("pk").not_exists(),
        )
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def get_item(table, pk: str, sk: str) -> dict[str, Any] | None:
    return table.get_item(Key={"pk": pk, "sk": sk}).get("Item")


def query_prefix(table, pk: str, prefix: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("pk").eq(pk) & Key("sk").begins_with(prefix)
    }
    while True:
        page = table.query(**kwargs)
        items.extend(page.get("Items", []))
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            return items
        kwargs["ExclusiveStartKey"] = last_key


def batch_write_items(
    table,
    items: list[dict[str, Any]],
    *,
    sleep: Callable[[float], None] = time.sleep,
    max_attempts: int = 8,
) -> None:
    """Write in groups of 25 and retry every unprocessed item."""
    client = table.meta.client
    table_name = table.name
    for offset in range(0, len(items), 25):
        pending = [
            {"PutRequest": {"Item": _normalize_numbers(item)}}
            for item in items[offset : offset + 25]
        ]
        for attempt in range(max_attempts):
            response = client.batch_write_item(RequestItems={table_name: pending})
            pending = response.get("UnprocessedItems", {}).get(table_name, [])
            if not pending:
                break
            sleep(min(2**attempt / 10, 5))
        if pending:
            raise RuntimeError(
                f"DynamoDB left {len(pending)} unprocessed items after {max_attempts} attempts"
            )


def complete_region(
    table,
    run_id: str,
    region: str,
    counters: dict[str, int],
    complete_at: str,
    completeness: dict[str, Any],
) -> bool:
    """Create the completion marker once; duplicate SQS deliveries are no-ops."""
    try:
        table.put_item(
            Item={
                "pk": f"RUN#{run_id}",
                "sk": f"REGION#{region}",
                "status": "complete",
                "region": region,
                "completed_at": complete_at,
                "counters": counters,
                "completeness": completeness,
            },
            ConditionExpression=Attr("pk").not_exists(),
        )
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def publish_current(
    table,
    account_id: str,
    run_meta: dict[str, Any],
    region_rows: list[dict[str, Any]],
    collected_at: str,
    expires_at: str,
) -> bool:
    expected = set(run_meta["regions"])
    complete = {row["region"] for row in region_rows if row.get("status") == "complete"}
    if complete != expected:
        return False
    completeness = {
        "complete": all(
            row.get("completeness", {}).get("complete", False) for row in region_rows
        ),
        "regions": {row["region"]: row.get("completeness", {}) for row in region_rows},
    }
    try:
        table.put_item(
            Item={
                "pk": f"ACCOUNT#{account_id}",
                "sk": "CURRENT",
                "run_id": run_meta["run_id"],
                "snapshot_id": run_meta["run_id"],
                "regions": sorted(expected),
                "collected_at": collected_at,
                "run_created_at": run_meta["created_at"],
                "expires_at": expires_at,
                "catalogue_version": run_meta["catalogue_version"],
                "schema_version": run_meta["schema_version"],
                "completeness": completeness,
            },
            ConditionExpression=(
                Attr("run_created_at").not_exists()
                | Attr("run_created_at").lt(run_meta["created_at"])
            ),
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False
    table.update_item(
        Key={"pk": run_meta["pk"], "sk": "META"},
        UpdateExpression="SET #status = :status, completed_at = :completed",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":status": "published",
            ":completed": collected_at,
        },
    )
    return True
