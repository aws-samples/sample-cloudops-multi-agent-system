from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
MCP_ROOT = ROOT / "src" / "lambda" / "mcp"
COLLECTOR_ROOT = ROOT / "src" / "lambda" / "collectors" / "cloudwatch"
sys.path.insert(0, str(MCP_ROOT))
sys.path.insert(0, str(COLLECTOR_ROOT))

spec = importlib.util.spec_from_file_location(
    "cloudwatch_collection_storage", COLLECTOR_ROOT / "storage.py"
)
storage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(storage)

worker_spec = importlib.util.spec_from_file_location(
    "cloudwatch_collection_worker", COLLECTOR_ROOT / "worker.py"
)
worker = importlib.util.module_from_spec(worker_spec)
worker_spec.loader.exec_module(worker)


def test_batch_write_retries_unprocessed_items():
    table = MagicMock()
    table.name = "coverage"
    table.meta.client.batch_write_item.side_effect = [
        {
            "UnprocessedItems": {
                "coverage": [{"PutRequest": {"Item": {"pk": "a", "sk": "b"}}}]
            }
        },
        {"UnprocessedItems": {}},
    ]

    storage.batch_write_items(
        table,
        [{"pk": "a", "sk": "b"}],
        sleep=lambda _: None,
    )

    assert table.meta.client.batch_write_item.call_count == 2


def test_batch_write_handles_ten_thousand_rows_in_bounded_batches():
    table = MagicMock()
    table.name = "coverage"
    table.meta.client.batch_write_item.return_value = {"UnprocessedItems": {}}

    storage.batch_write_items(
        table,
        [{"pk": "run", "sk": f"resource-{index}"} for index in range(10_000)],
        sleep=lambda _: None,
    )

    assert table.meta.client.batch_write_item.call_count == 400
    assert all(
        len(call.kwargs["RequestItems"]["coverage"]) <= 25
        for call in table.meta.client.batch_write_item.call_args_list
    )


def test_batch_write_converts_nested_floats_to_decimal():
    table = MagicMock()
    table.name = "coverage"
    table.meta.client.batch_write_item.return_value = {"UnprocessedItems": {}}

    storage.batch_write_items(
        table,
        [
            {
                "pk": "run",
                "sk": "candidate",
                "threshold": 1.25,
                "strategy": {"baseline": [0.1, 2]},
            }
        ],
    )

    item = table.meta.client.batch_write_item.call_args.kwargs["RequestItems"][
        "coverage"
    ][0]["PutRequest"]["Item"]
    assert item["threshold"] == Decimal("1.25")
    assert item["strategy"]["baseline"] == [Decimal("0.1"), 2]


def test_duplicate_region_completion_is_idempotent():
    table = MagicMock()
    failure = type("ConditionalCheckFailedException", (Exception,), {})
    table.meta.client.exceptions.ConditionalCheckFailedException = failure
    table.put_item.side_effect = [None, failure()]

    assert storage.complete_region(
        table, "run", "us-east-1", {}, "now", {"complete": True}
    )
    assert not storage.complete_region(
        table, "run", "us-east-1", {}, "now", {"complete": True}
    )


def test_failed_or_partial_run_cannot_publish_current():
    table = MagicMock()
    meta = {
        "pk": "RUN#run",
        "run_id": "run",
        "created_at": "2026-08-14T00:00:00+00:00",
        "regions": ["us-east-1", "eu-west-1"],
        "catalogue_version": "v",
        "schema_version": "s",
    }
    assert not storage.publish_current(
        table,
        "123456789012",
        meta,
        [
            {
                "status": "complete",
                "region": "us-east-1",
                "completeness": {"complete": True},
            }
        ],
        "now",
        "later",
    )
    table.put_item.assert_not_called()


def test_all_regions_publish_one_atomic_current_pointer():
    table = MagicMock()
    meta = {
        "pk": "RUN#run",
        "run_id": "run",
        "created_at": "2026-08-14T00:00:00+00:00",
        "regions": ["us-east-1", "eu-west-1"],
        "catalogue_version": "v",
        "schema_version": "s",
    }
    rows = [
        {
            "status": "complete",
            "region": region,
            "completeness": {"complete": True},
        }
        for region in meta["regions"]
    ]
    assert storage.publish_current(table, "123456789012", meta, rows, "now", "later")
    current = table.put_item.call_args.kwargs["Item"]
    assert current["pk"] == "ACCOUNT#123456789012"
    assert current["sk"] == "CURRENT"
    assert current["run_id"] == "run"


@pytest.mark.parametrize(
    ("inventory_source", "inventory_complete", "resource_arn", "expected"),
    [
        (
            "resource_explorer",
            True,
            "arn:aws:lambda:us-east-1:123456789012:function:fn",
            {
                "complete": True,
                "resource_inventory": True,
                "alarm_inventory": True,
                "source": "resource_explorer",
                "collection_status": "succeeded",
                "resource_inventory_status": "complete",
                "alarm_inventory_status": "complete",
                "incomplete_reasons": [],
            },
        ),
        (
            "tagging_api",
            False,
            "arn:aws:unknown:us-east-1:123456789012:item/example",
            {
                "complete": False,
                "resource_inventory": False,
                "alarm_inventory": True,
                "source": "tagging_api",
                "collection_status": "succeeded",
                "resource_inventory_status": "partial",
                "alarm_inventory_status": "complete",
                "incomplete_reasons": [
                    "tagging_api_fallback_may_omit_untagged_resources"
                ],
            },
        ),
    ],
)
def test_successful_region_marker_separates_collection_from_inventory(
    monkeypatch,
    inventory_source,
    inventory_complete,
    resource_arn,
    expected,
):
    table = MagicMock()
    monkeypatch.setattr(worker, "_table", lambda: table)
    monkeypatch.setattr(worker.storage, "get_item", lambda *args: None)
    monkeypatch.setattr(
        worker,
        "_inventory_for_region",
        lambda message: [{"ResourceARN": resource_arn, "Tags": []}],
    )
    monkeypatch.setattr(worker, "_enrich_tags", lambda *args: {})
    monkeypatch.setattr(worker, "_alarms", lambda region: [])
    batch_write_items = MagicMock()
    monkeypatch.setattr(worker.storage, "batch_write_items", batch_write_items)
    complete_region = MagicMock(return_value=True)
    monkeypatch.setattr(worker.storage, "complete_region", complete_region)
    monkeypatch.setattr(worker, "_send_jobs", lambda jobs: None)
    monkeypatch.setattr(worker, "_emit", lambda *args: None)

    worker.region_job(
        {
            "run_id": "run",
            "account_id": "123456789012",
            "region": "us-east-1",
            "inventory_source": inventory_source,
            "resource_inventory_complete": inventory_complete,
        }
    )

    assert complete_region.call_args.args[5] == expected
    if inventory_source == "tagging_api":
        resource_rows = [
            row
            for row in batch_write_items.call_args.args[1]
            if row["entity"] == "resource"
        ]
        assert resource_rows[0]["coverage_status"] == "unsupported_resource"
