"""Scheduled and on-demand CloudWatch coverage run coordinator."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os

import boto3

from shared.cloudwatch_domain.recommendations import CATALOGUE_VERSION
from shared.cloudwatch_domain.snapshot import SCHEMA_VERSION
from shared.cross_account import get_aws_client

import storage

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

TABLE_NAME = os.environ.get("CLOUDWATCH_COVERAGE_TABLE_NAME", "")
QUEUE_URL = os.environ.get("CLOUDWATCH_COVERAGE_QUEUE_URL", "")
METRIC_NAMESPACE = os.environ.get(
    "CLOUDWATCH_COVERAGE_METRIC_NAMESPACE", "CloudOps/CloudWatchCoverage"
)


def _table():
    return boto3.resource("dynamodb").Table(TABLE_NAME)


def _queue():
    return boto3.client("sqs")


def _publish_snapshot_age(account_id: str, now: dt.datetime) -> None:
    current = storage.get_item(_table(), f"ACCOUNT#{account_id}", "CURRENT")
    if not current:
        return
    collected_at = dt.datetime.fromisoformat(current["collected_at"])
    age_seconds = max(0, (now - collected_at).total_seconds())
    boto3.client("cloudwatch").put_metric_data(
        Namespace=METRIC_NAMESPACE,
        MetricData=[
            {
                "MetricName": "SnapshotAge",
                "Value": age_seconds,
                "Unit": "Seconds",
            }
        ],
    )


def _target_account_id() -> str:
    role_arn = os.environ.get("CROSS_ACCOUNT_ROLE_ARN_CLOUDWATCH", "")
    parts = role_arn.split(":")
    if len(parts) > 4 and len(parts[4]) == 12 and parts[4].isdigit():
        return parts[4]
    return str(
        get_aws_client("sts", role_alias="CLOUDWATCH").get_caller_identity()["Account"]
    )


def _enabled_regions() -> list[str]:
    client = get_aws_client(
        "ec2",
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        role_alias="CLOUDWATCH",
    )
    regions = client.describe_regions(AllRegions=True).get("Regions", [])
    return sorted(
        region["RegionName"]
        for region in regions
        if region.get("OptInStatus") in {"opt-in-not-required", "opted-in"}
    )


def _run_id(account_id: str, now: dt.datetime, on_demand: bool) -> str:
    if on_demand:
        slot = now.strftime("%Y%m%dT%H%M")
    else:
        hour = (now.hour // 6) * 6
        slot = now.strftime("%Y%m%d") + f"T{hour:02d}"
    digest = hashlib.sha256(
        f"{account_id}:{slot}:{CATALOGUE_VERSION}".encode("utf-8")
    ).hexdigest()[:16]
    return f"{slot}-{digest}"


def handler(event, context):
    if not TABLE_NAME or not QUEUE_URL:
        raise RuntimeError(
            "CLOUDWATCH_COVERAGE_TABLE_NAME and CLOUDWATCH_COVERAGE_QUEUE_URL are required"
        )
    now = dt.datetime.now(dt.timezone.utc)
    account_id = _target_account_id()
    _publish_snapshot_age(account_id, now)
    regions = _enabled_regions()
    on_demand = bool(
        (event or {}).get("force_refresh") or (event or {}).get("on_demand")
    )
    run_id = _run_id(account_id, now, on_demand)
    created = storage.put_run_if_absent(
        _table(),
        run_id,
        account_id,
        regions,
        now.isoformat(),
        CATALOGUE_VERSION,
        SCHEMA_VERSION,
    )
    if created:
        _queue().send_message(
            QueueUrl=QUEUE_URL,
            MessageBody=json.dumps(
                {
                    "job": "inventory",
                    "run_id": run_id,
                    "account_id": account_id,
                    "regions": regions,
                }
            ),
        )
    _table().put_item(
        Item={
            "pk": f"ACCOUNT#{account_id}",
            "sk": "REFRESH",
            "run_id": run_id,
            "status": "started" if created else "reused",
            "requested_at": now.isoformat(),
        }
    )
    return {
        "status": "started" if created else "reused",
        "run_id": run_id,
        "account_id": account_id,
        "regions": regions,
    }
