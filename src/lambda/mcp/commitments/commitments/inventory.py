"""Commitment inventory: which Reserved Instances and Savings Plans exist, and
when each one ends.

The only calls in the package that are not Cost Explorer — per-service,
regional `Describe*` reads, all free and all read-only. Each family names the
same concepts differently, so the differences live in `RESERVATION_INVENTORY`
as data rather than in six near-identical functions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from botocore.exceptions import ClientError

from .api import (
    CE_REGION,
    DAYS_PER_MONTH,
    Clients,
    _attribute_display,
    _error,
    _spec_label,
)

@dataclass(frozen=True)
class InventorySpec:
    """How to list one reservation family and where its fields live.

    Every reservation API names the same six concepts differently and none of
    them agrees with Savings Plans, so the differences are data rather than
    seven near-identical functions. Field names here were read from the
    botocore service models, not guessed — an `id_field` typo silently yields
    commitments with no identifier.

    Only EC2 returns an explicit end date; everywhere else it must be derived
    from `start_field` plus `Duration` (seconds).

    `attribute_fields` names the extras that decide *what a renewal has to
    match*: an RDS reservation covers one deployment option and one engine, and
    an EC2 one is pinned to an Availability Zone when its scope is zonal. Renew
    against the wrong value and the discount silently does not apply, so these
    travel with the instance type rather than being dropped.

    `ce_service` is the Cost Explorer SERVICE value for the family. Reservation
    utilization and coverage default to EC2 when no SERVICE filter is sent, so
    every non-EC2 renewal verdict needs its own filtered query.
    """

    key: str
    label: str
    service: str
    method: str
    response_key: str
    id_field: str
    count_field: str
    type_field: str
    start_field: str
    ce_service: str
    end_field: str | None = None
    arn_field: str | None = None
    payment_field: str = "OfferingType"
    attribute_fields: tuple[tuple[str, str], ...] = ()


# Regional APIs, unlike the us-east-1-pinned Cost Explorer calls above: a
# reservation is only visible in the region that holds it.
RESERVATION_INVENTORY = (
    InventorySpec(
        key="ec2",
        label="EC2",
        service="ec2",
        method="describe_reserved_instances",
        response_key="ReservedInstances",
        ce_service="Amazon Elastic Compute Cloud - Compute",
        id_field="ReservedInstancesId",
        count_field="InstanceCount",
        type_field="InstanceType",
        start_field="Start",
        end_field="End",
        attribute_fields=(
            ("Scope", "scope"),
            ("AvailabilityZone", "AZ"),
            ("ProductDescription", "platform"),
            ("OfferingClass", "class"),
            ("InstanceTenancy", "tenancy"),
        ),
    ),
    InventorySpec(
        key="rds",
        label="RDS",
        service="rds",
        method="describe_reserved_db_instances",
        response_key="ReservedDBInstances",
        ce_service="Amazon Relational Database Service",
        id_field="ReservedDBInstanceId",
        count_field="DBInstanceCount",
        type_field="DBInstanceClass",
        start_field="StartTime",
        arn_field="ReservedDBInstanceArn",
        # MultiAZ is a bool on the wire; ProductDescription carries the engine
        # ("aurora-postgresql", "postgresql"). Both are part of what an RDS or
        # Aurora reservation matches against, so neither can be dropped.
        attribute_fields=(("MultiAZ", "deployment"), ("ProductDescription", "engine")),
    ),
    InventorySpec(
        key="elasticache",
        label="ElastiCache",
        service="elasticache",
        method="describe_reserved_cache_nodes",
        response_key="ReservedCacheNodes",
        ce_service="Amazon ElastiCache",
        id_field="ReservedCacheNodeId",
        count_field="CacheNodeCount",
        type_field="CacheNodeType",
        start_field="StartTime",
        arn_field="ReservationARN",
        attribute_fields=(("ProductDescription", "engine"),),
    ),
    InventorySpec(
        key="redshift",
        label="Redshift",
        service="redshift",
        method="describe_reserved_nodes",
        response_key="ReservedNodes",
        ce_service="Amazon Redshift",
        id_field="ReservedNodeId",
        count_field="NodeCount",
        type_field="NodeType",
        start_field="StartTime",
        attribute_fields=(("ReservedNodeOfferingType", "offering"),),
    ),
    InventorySpec(
        key="opensearch",
        label="OpenSearch",
        service="opensearch",
        method="describe_reserved_instances",
        response_key="ReservedInstances",
        ce_service="Amazon OpenSearch Service",
        id_field="ReservedInstanceId",
        count_field="InstanceCount",
        type_field="InstanceType",
        start_field="StartTime",
        payment_field="PaymentOption",
    ),
    InventorySpec(
        key="memorydb",
        label="MemoryDB",
        service="memorydb",
        method="describe_reserved_nodes",
        response_key="ReservedNodes",
        ce_service="Amazon MemoryDB Service",
        id_field="ReservationId",
        count_field="NodeCount",
        type_field="NodeType",
        start_field="StartTime",
        arn_field="ARN",
    ),
)

INVENTORY_SPECS = {spec.key: spec for spec in RESERVATION_INVENTORY}
INVENTORY_KEYS = tuple(INVENTORY_SPECS)

# Fields whose raw value does not read as an attribute on its own. `MultiAZ` is
# the important one: printing "MultiAZ: False" invites a reader to skim past the
# single most expensive detail of an RDS or Aurora reservation, whereas
# "Single-AZ" states it.
INVENTORY_ATTRIBUTE_VALUES: dict[str, dict[Any, str]] = {
    "MultiAZ": {True: "Multi-AZ", False: "Single-AZ"},
}

# DynamoDB reserved capacity is deliberately absent: there is no
# describe-reserved-capacity API on any SDK, so its expiry cannot be read.
# Cost Explorer can still *size* a DynamoDB reservation (RI_SERVICES above),
# it just cannot tell you when an existing one lapses.
#
# Member-account purchases are the second gap. The Describe* APIs list only the
# calling account's own commitments, while PAYER-scope coverage and utilization
# are organization-wide — so an RI a member account bought is counted in the
# utilization figure but never appears in the inventory.
INVENTORY_BLIND_SPOTS = (
    "DynamoDB reserved capacity (no describe API exists)",
    "commitments purchased by other accounts in the organization (the inventory "
    "lists only the calling account's own purchases, although PAYER-scope "
    "coverage and utilization include them)",
)

# States that mean "this commitment is still costing or saving money". A
# retired/expired row is history, not something to renew. Redshift spells the
# pending state `pending-payment`; every other family uses `payment-pending`.
ACTIVE_RESERVATION_STATES = (
    "active",
    "payment-pending",
    "pending-payment",
    "pending",
    "retired-pending",
)

# A queued purchase is how a renewal gets scheduled ahead of an expiry. It is
# not yet saving anything, but leaving it out of the inventory would let an
# already-renewed commitment draw a "renew" verdict — and a second,
# non-cancellable purchase.
QUEUED_STATES = ("queued",)
ACTIVE_SP_STATES = ("active", "payment-pending")
INVENTORY_SP_STATES = ACTIVE_SP_STATES + QUEUED_STATES


# ---------------------------------------------------------------------------
# Commitment inventory — the only calls here that are not Cost Explorer
# ---------------------------------------------------------------------------


def _as_datetime(value: Any) -> datetime | None:
    """Coerce whatever an SDK or a CLI JSON dump handed us into UTC datetime.

    boto3 returns real datetimes; `aws ... --output json` returns ISO strings;
    a hand-built stub may return a plain date. All three have to work, and an
    unparseable value must return None rather than raise, because one odd row
    must not lose the rest of the inventory.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso_day(moment: datetime | None) -> str:
    return moment.date().isoformat() if moment else ""


def _term_months(start: datetime | None, end: datetime | None) -> int | None:
    """Label the term from the elapsed span, in months."""
    if not start or not end:
        return None
    months = round((end - start).days / DAYS_PER_MONTH)
    return months or None


def _reservation_attributes(spec: InventorySpec, row: dict) -> dict[str, str]:
    """Read the match-critical extras for one reservation family.

    A bool has to be tested against None rather than truthiness: `MultiAZ:
    False` is the meaningful value "Single-AZ", and dropping it because it is
    falsy would leave the reader assuming Multi-AZ.
    """
    attributes: dict[str, str] = {}
    for field, label in spec.attribute_fields:
        value = row.get(field)
        if value is None or value == "":
            continue
        mapped = INVENTORY_ATTRIBUTE_VALUES.get(field, {}).get(value)
        attributes[label] = mapped or _attribute_display(label, value)
    return {k: v for k, v in attributes.items() if v}


def _normalize_reservation(spec: InventorySpec, row: dict, region: str) -> dict:
    """Flatten one reservation row into the shape every family shares."""
    start = _as_datetime(row.get(spec.start_field))
    end = _as_datetime(row.get(spec.end_field)) if spec.end_field else None
    if end is None:
        duration = row.get("Duration")
        if start and duration:
            end = start + timedelta(seconds=int(duration))
    instance_type = str(row.get(spec.type_field) or "")
    attributes = _reservation_attributes(spec, row)
    return {
        "family": "reservation",
        "service": spec.key,
        "label": spec.label,
        "commitment_id": str(row.get(spec.id_field) or ""),
        "arn": str(row.get(spec.arn_field) or "") if spec.arn_field else "",
        "instance_type": instance_type,
        "attributes": attributes,
        # What a renewal has to match, in one string. Region is already a column
        # of its own in the report, so it is left out here.
        "spec": _spec_label(instance_type, attributes),
        "quantity": float(row.get(spec.count_field) or 0),
        "unit": "units",
        "region": region,
        "state": str(row.get("State") or ""),
        "payment_option": str(row.get(spec.payment_field) or ""),
        "start": _iso_day(start),
        "end": _iso_day(end),
        "term_months": _term_months(start, end),
    }


def get_reservation_inventory(
    clients: Clients, service: str, region: str
) -> dict[str, Any]:
    """List one reservation family in one region, normalized.

    Returns `{"service", "region", "items"}` on success, or an `_error()` dict
    carrying the same two keys so the caller can name the failed query.
    """
    spec = INVENTORY_SPECS.get(service)
    if spec is None:
        raise ValueError(
            f"Unknown reservation family {service!r}. "
            f"Expected one of: {', '.join(INVENTORY_KEYS)}"
        )
    if clients.make_client is None:
        return {
            "service": service,
            "region": region,
            "error": "No client factory configured — reservation inventory "
            "needs regional Describe* access this host did not grant.",
        }
    try:
        client = clients.make_client(spec.service, region)
        rows = _inventory_rows(client, spec)
    except ClientError as exc:
        return {"service": service, "region": region, **_error(exc)}

    wanted = ACTIVE_RESERVATION_STATES + QUEUED_STATES
    items = [
        _normalize_reservation(spec, row, region)
        for row in rows
        if str(row.get("State", "")).lower() in wanted
    ]
    return {"service": service, "region": region, "items": items}


def _inventory_rows(client: Any, spec: InventorySpec) -> list[dict]:
    """Every row a reservation Describe* returns, across all pages.

    RDS, ElastiCache and Redshift page on `Marker` and MemoryDB on `NextToken`,
    all at up to 100 rows — and retired rows count toward that 100, so an old
    account's live, expiring reservations can sit on page two. Where botocore
    ships a paginator it is used; OpenSearch has none, so its `NextToken` is
    followed by hand; EC2 returns everything in one response.
    """
    method = getattr(client, spec.method)
    if client.can_paginate(spec.method):
        pages = client.get_paginator(spec.method).paginate()
        return [row for page in pages for row in page.get(spec.response_key, [])]

    rows: list[dict] = []
    token: str | None = None
    while True:
        resp = method(**({"NextToken": token} if token else {}))
        rows.extend(resp.get(spec.response_key, []))
        token = resp.get("NextToken")
        if not token:
            return rows


def get_savings_plan_inventory(
    clients: Clients, region: str = CE_REGION
) -> dict[str, Any]:
    """List active and queued Savings Plans — account-level, so called once.

    Queued plans are included so a renewal that is already scheduled is seen;
    `analyze_expiry` matches them against what is expiring.

    Savings Plans are the one family that returns an explicit `end`, and the
    only one whose commitment is denominated in dollars per hour rather than a
    unit count. `region` selects the API endpoint, not a filter on the plans.
    """
    if clients.make_client is None:
        return {
            "service": "savingsplans",
            "region": region,
            "error": "No client factory configured — Savings Plan inventory "
            "needs savingsplans:DescribeSavingsPlans, which this host did not "
            "grant.",
        }
    items: list[dict] = []
    try:
        client = clients.make_client("savingsplans", region)
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"states": list(INVENTORY_SP_STATES)}
            if token:
                kwargs["nextToken"] = token
            resp = client.describe_savings_plans(**kwargs)
            for row in resp.get("savingsPlans", []):
                start = _as_datetime(row.get("start"))
                end = _as_datetime(row.get("end"))
                # Only an EC2 Instance Savings Plan is pinned to a family and a
                # region; a Compute plan commits to dollars and nothing else, so
                # an empty spec here is correct rather than missing data.
                instance_family = str(row.get("ec2InstanceFamily") or "")
                items.append(
                    {
                        "family": "savings-plan",
                        "service": "savingsplans",
                        "label": f"{row.get('savingsPlanType') or 'Savings'} "
                        "Savings Plan",
                        "commitment_id": str(row.get("savingsPlanId") or ""),
                        "arn": str(row.get("savingsPlanArn") or ""),
                        "instance_type": instance_family,
                        "attributes": {},
                        "spec": instance_family,
                        "quantity": float(row.get("commitment") or 0),
                        "unit": "USD/hour",
                        "region": str(row.get("region") or "") or "global",
                        "state": str(row.get("state") or ""),
                        "payment_option": str(row.get("paymentOption") or ""),
                        "start": _iso_day(start),
                        "end": _iso_day(end),
                        "term_months": _term_months(start, end),
                    }
                )
            token = resp.get("nextToken")
            if not token:
                break
    except ClientError as exc:
        return {"service": "savingsplans", "region": region, **_error(exc)}
    return {"service": "savingsplans", "region": region, "items": items}
