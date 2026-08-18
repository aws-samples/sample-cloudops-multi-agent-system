"""SQS worker for inventory, regional analysis, and atomic publication."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from typing import Any, Iterable

import boto3

from shared.cloudwatch_domain.coverage import evaluate_resource
from shared.cloudwatch_domain.normalize import (
    normalize_alarm,
    resource_profile,
)
from shared.cross_account import get_aws_client

import storage

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

TABLE_NAME = os.environ.get("CLOUDWATCH_COVERAGE_TABLE_NAME", "")
QUEUE_URL = os.environ.get("CLOUDWATCH_COVERAGE_QUEUE_URL", "")
METRIC_NAMESPACE = os.environ.get(
    "CLOUDWATCH_COVERAGE_METRIC_NAMESPACE", "CloudOps/CloudWatchCoverage"
)
RESOURCE_EXPLORER_REGION = os.environ.get(
    "RESOURCE_EXPLORER_AGGREGATOR_REGION", os.environ.get("AWS_REGION", "us-east-1")
)
TAGGING_API_FALLBACK_REASON = "tagging_api_fallback_may_omit_untagged_resources"


def _table():
    return boto3.resource("dynamodb").Table(TABLE_NAME)


def _sqs():
    return boto3.client("sqs")


def _emit(name: str, value: float = 1, unit: str = "Count") -> None:
    boto3.client("cloudwatch").put_metric_data(
        Namespace=METRIC_NAMESPACE,
        MetricData=[{"MetricName": name, "Value": value, "Unit": unit}],
    )


def _send_jobs(jobs: Iterable[dict[str, Any]]) -> None:
    entries = list(jobs)
    for offset in range(0, len(entries), 10):
        _sqs().send_message_batch(
            QueueUrl=QUEUE_URL,
            Entries=[
                {
                    "Id": str(index),
                    "MessageBody": json.dumps(job),
                }
                for index, job in enumerate(entries[offset : offset + 10])
            ],
        )


def _resource_explorer_inventory() -> tuple[list[dict[str, Any]], bool]:
    client = get_aws_client(
        "resource-explorer-2",
        region_name=RESOURCE_EXPLORER_REGION,
        role_alias="CLOUDWATCH",
    )
    view_arn = client.get_default_view().get("ViewArn")
    if not view_arn:
        raise RuntimeError("Resource Explorer has no default aggregator view")
    resources: list[dict[str, Any]] = []
    paginator = client.get_paginator("list_resources")
    for page in paginator.paginate(ViewArn=view_arn):
        resources.extend(page.get("Resources", []))
    return resources, True


def inventory_job(message: dict[str, Any]) -> dict[str, Any]:
    run_id = message["run_id"]
    inventory_complete = True
    inventory_source = "resource_explorer"
    try:
        raw_resources, inventory_complete = _resource_explorer_inventory()
    except Exception as exc:
        logger.warning(
            "Resource Explorer unavailable, using regional Tagging API: %s", exc
        )
        raw_resources = []
        inventory_complete = False
        inventory_source = "tagging_api"

    rows: list[dict[str, Any]] = []
    if raw_resources:
        ttl = int(dt.datetime.now(dt.timezone.utc).timestamp()) + 7 * 86400
        for raw in raw_resources:
            arn = raw.get("Arn")
            if not arn:
                continue
            profile = resource_profile(arn)
            region = (
                profile.get("metric_region") or profile.get("region") or "us-east-1"
            )
            rows.append(
                {
                    "pk": f"RUN#{run_id}",
                    "sk": f"INVENTORY#{region}#{profile['resource_id']}",
                    "arn": arn,
                    "region": region,
                    "ttl": ttl,
                }
            )
        storage.batch_write_items(_table(), rows)

    _send_jobs(
        {
            "job": "region",
            "run_id": run_id,
            "account_id": message["account_id"],
            "region": region,
            "inventory_source": inventory_source,
            "resource_inventory_complete": inventory_complete,
        }
        for region in message["regions"]
    )
    _emit("ResourcesDiscovered", len(rows))
    return {"job": "inventory", "resources": len(rows), "source": inventory_source}


def _tagging_inventory(region: str) -> list[dict[str, Any]]:
    client = get_aws_client(
        "resourcegroupstaggingapi", region_name=region, role_alias="CLOUDWATCH"
    )
    resources: list[dict[str, Any]] = []
    paginator = client.get_paginator("get_resources")
    for page in paginator.paginate():
        resources.extend(page.get("ResourceTagMappingList", []))
    return resources


def _inventory_for_region(message: dict[str, Any]) -> list[dict[str, Any]]:
    region = message["region"]
    if message["inventory_source"] == "tagging_api":
        return _tagging_inventory(region)
    rows = storage.query_prefix(
        _table(), f"RUN#{message['run_id']}", f"INVENTORY#{region}#"
    )
    return [{"ResourceARN": row["arn"], "Tags": []} for row in rows]


def _enrich_tags(
    region: str, inventory: list[dict[str, Any]]
) -> dict[str, dict[str, str]]:
    result = {
        item["ResourceARN"]: {
            tag["Key"]: tag.get("Value", "") for tag in item.get("Tags", [])
        }
        for item in inventory
    }
    missing = [arn for arn, tags in result.items() if not tags]
    if not missing:
        return result
    client = get_aws_client(
        "resourcegroupstaggingapi", region_name=region, role_alias="CLOUDWATCH"
    )
    for offset in range(0, len(missing), 100):
        page = client.get_resources(ResourceARNList=missing[offset : offset + 100])
        for item in page.get("ResourceTagMappingList", []):
            result[item["ResourceARN"]] = {
                tag["Key"]: tag.get("Value", "") for tag in item.get("Tags", [])
            }
    return result


def _alarms(region: str) -> list[dict[str, Any]]:
    client = get_aws_client("cloudwatch", region_name=region, role_alias="CLOUDWATCH")
    alarms: list[dict[str, Any]] = []
    paginator = client.get_paginator("describe_alarms")
    for page in paginator.paginate(AlarmTypes=["MetricAlarm", "CompositeAlarm"]):
        alarms.extend(
            normalize_alarm(alarm, region) for alarm in page.get("MetricAlarms", [])
        )
        alarms.extend(
            normalize_alarm(alarm, region) for alarm in page.get("CompositeAlarms", [])
        )
    return alarms


def region_job(message: dict[str, Any]) -> dict[str, Any]:
    table = _table()
    run_id = message["run_id"]
    region = message["region"]
    if storage.get_item(table, f"RUN#{run_id}", f"REGION#{region}"):
        _send_jobs(
            [
                {
                    "job": "finalize",
                    "run_id": run_id,
                    "account_id": message["account_id"],
                }
            ]
        )
        return {"job": "region", "region": region, "status": "duplicate"}

    inventory = _inventory_for_region(message)
    tags = _enrich_tags(region, inventory)
    alarms = _alarms(region)
    profiles = [
        resource_profile(item["ResourceARN"], tags.get(item["ResourceARN"]), region)
        for item in inventory
    ]
    results = [
        evaluate_resource(
            profile,
            alarms,
            resource_inventory_complete=message["resource_inventory_complete"],
            alarm_inventory_complete=True,
        )
        for profile in profiles
    ]
    pk = f"SNAPSHOT#{message['account_id']}#{run_id}#{region}"
    ttl = int(dt.datetime.now(dt.timezone.utc).timestamp()) + 7 * 86400
    rows: list[dict[str, Any]] = []
    for alarm in alarms:
        rows.append(
            {
                "pk": pk,
                "sk": f"ALARM#{alarm['alarm_id']}",
                "entity": "alarm",
                **alarm,
                "ttl": ttl,
            }
        )
    candidate_count = 0
    for result in results:
        candidates = result.pop("candidates")
        rows.append(
            {
                "pk": pk,
                "sk": f"RESOURCE#{result['resource_id']}",
                "entity": "resource",
                **result,
                "ttl": ttl,
            }
        )
        for candidate in candidates:
            candidate_count += 1
            rows.append(
                {
                    "pk": pk,
                    "sk": f"CANDIDATE#{candidate['candidate_id']}",
                    "entity": "candidate",
                    "resource_id": result["resource_id"],
                    "resource_arn": result["arn"],
                    "region": region,
                    **candidate,
                    "ttl": ttl,
                }
            )
    storage.batch_write_items(table, rows)
    counters = {
        "resources": len(results),
        "alarms": len(alarms),
        "candidates": candidate_count,
    }
    completeness = {
        "complete": bool(message["resource_inventory_complete"]),
        "resource_inventory": bool(message["resource_inventory_complete"]),
        "alarm_inventory": True,
        "source": message["inventory_source"],
        "collection_status": "succeeded",
        "resource_inventory_status": (
            "complete" if message["resource_inventory_complete"] else "partial"
        ),
        "alarm_inventory_status": "complete",
        "incomplete_reasons": (
            []
            if message["resource_inventory_complete"]
            else [TAGGING_API_FALLBACK_REASON]
        ),
    }
    storage.complete_region(
        table,
        run_id,
        region,
        counters,
        dt.datetime.now(dt.timezone.utc).isoformat(),
        completeness,
    )
    _send_jobs(
        [
            {
                "job": "finalize",
                "run_id": run_id,
                "account_id": message["account_id"],
            }
        ]
    )
    _emit("RegionsCompleted")
    _emit("Resources", len(results))
    _emit("Alarms", len(alarms))
    _emit("Candidates", candidate_count)
    return {"job": "region", "region": region, **counters}


def finalize_job(message: dict[str, Any]) -> dict[str, Any]:
    table = _table()
    run_meta = storage.get_item(table, f"RUN#{message['run_id']}", "META")
    if not run_meta:
        raise RuntimeError("run metadata is missing")
    region_rows = storage.query_prefix(table, run_meta["pk"], "REGION#")
    now = dt.datetime.now(dt.timezone.utc)
    published = storage.publish_current(
        table,
        message["account_id"],
        run_meta,
        region_rows,
        now.isoformat(),
        (now + dt.timedelta(hours=48)).isoformat(),
    )
    if published:
        created = dt.datetime.fromisoformat(run_meta["created_at"])
        _emit("RunDuration", (now - created).total_seconds(), "Seconds")
        _emit("SnapshotPublished")
        _emit("SnapshotAge", 0, "Seconds")
        table.update_item(
            Key={"pk": f"ACCOUNT#{message['account_id']}", "sk": "REFRESH"},
            UpdateExpression="SET #status = :status, completed_at = :completed",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":status": "published",
                ":completed": now.isoformat(),
            },
        )
    return {"job": "finalize", "published": published}


_JOBS = {
    "inventory": inventory_job,
    "region": region_job,
    "finalize": finalize_job,
}


def handler(event, context):
    if not TABLE_NAME or not QUEUE_URL:
        raise RuntimeError(
            "CLOUDWATCH_COVERAGE_TABLE_NAME and CLOUDWATCH_COVERAGE_QUEUE_URL are required"
        )
    results = []
    for record in event.get("Records", []):
        message = json.loads(record["body"])
        job = _JOBS.get(message.get("job"))
        if not job:
            raise ValueError(f"unknown job: {message.get('job')}")
        try:
            results.append(job(message))
        except Exception:
            _emit("WorkerErrors")
            if message.get("job") == "region":
                _emit("RegionsFailed")
            logger.exception("CloudWatch coverage worker failed")
            raise
    return {"results": results}
