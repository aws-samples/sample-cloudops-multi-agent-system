"""Read-only AWS API wrappers for commitment (RI/SP) analysis.

Every call in this module is a Get*/List*/Describe* operation. Nothing here
mutates state, purchases a commitment, or starts a billable analysis.

Both Cost Explorer and Cost Optimization Hub are us-east-1-only APIs regardless
of where your resources live, so the clients are pinned there.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

# Cost Explorer and Cost Optimization Hub are global services fronted only by
# us-east-1. Calling them in another region fails to resolve an endpoint.
CE_REGION = "us-east-1"
COH_REGION = "us-east-1"

# Authoritative list, read back from the ValidationException the API itself
# raises on an unknown Service. Do not extend this by guessing service names —
# re-probe with an invalid value and copy the "Supported value(s)" list.
RI_SERVICES = (
    "Amazon Elastic Compute Cloud - Compute",
    "Amazon Relational Database Service",
    "Amazon Redshift",
    "Amazon ElastiCache",
    "Amazon Elasticsearch Service",
    "Amazon OpenSearch Service",
    "Amazon MemoryDB Service",
    "Amazon DynamoDB Service",
)

# Short labels for report headings, keyed by the API's Service string.
RI_SERVICE_LABELS = {
    "Amazon Elastic Compute Cloud - Compute": "EC2",
    "Amazon Relational Database Service": "RDS",
    "Amazon Redshift": "Redshift",
    "Amazon ElastiCache": "ElastiCache",
    "Amazon Elasticsearch Service": "Elasticsearch (legacy)",
    "Amazon OpenSearch Service": "OpenSearch",
    "Amazon MemoryDB Service": "MemoryDB",
    "Amazon DynamoDB Service": "DynamoDB",
}

SP_TYPES = ("COMPUTE_SP", "EC2_INSTANCE_SP", "SAGEMAKER_SP", "DATABASE_SP")

SP_TYPE_LABELS = {
    "COMPUTE_SP": "Compute Savings Plan",
    "EC2_INSTANCE_SP": "EC2 Instance Savings Plan",
    "SAGEMAKER_SP": "SageMaker Savings Plan",
    "DATABASE_SP": "Database Savings Plan",
}

# Cost Optimization Hub resource types that represent a commitment purchase,
# used to reconcile CE recommendations against COH's independent pipeline.
COH_COMMITMENT_RESOURCE_TYPES = (
    "ComputeSavingsPlans",
    "Ec2InstanceSavingsPlans",
    "SageMakerSavingsPlans",
    "Ec2ReservedInstances",
    "RdsReservedInstances",
    "OpenSearchReservedInstances",
    "RedshiftReservedInstances",
    "ElastiCacheReservedInstances",
    "DynamoDbReservedCapacity",
    "MemoryDbReservedInstances",
)

HOURS_PER_MONTH = 730.0

# Rough month length, used only to label a term as 12 or 36 months from the
# elapsed start->end span. 365/12 rounds both real terms correctly.
DAYS_PER_MONTH = 30.4375


@dataclass(frozen=True)
class SpecShape:
    """Where one service hides the purchasable spec inside a CE line item.

    `GetReservationPurchaseRecommendation` returns a count and a savings figure
    per line item, but the thing you actually buy — the instance class, the
    Availability Zone, whether RDS is Multi-AZ — is buried in a
    service-specific sub-structure under `InstanceDetails`. A recommendation
    without it is not purchasable: "buy 4 RDS reservations" does not say
    `db.r6g.xlarge Multi-AZ Aurora PostgreSQL`, and buying the wrong
    combination yields a reservation that matches nothing.

    `attribute_fields` are ordered for reading, most decision-relevant first.
    Field names come from the botocore CE model, including the two services
    that break the pattern: OpenSearch/Elasticsearch splits its type across
    `InstanceClass` + `InstanceSize`, and DynamoDB has no instance at all.
    """

    key: str
    container: str
    size_fields: tuple[str, ...]
    attribute_fields: tuple[tuple[str, str], ...] = ()
    family_field: str | None = "Family"
    region_field: str = "Region"


RECOMMENDATION_SPECS = (
    SpecShape(
        key="EC2InstanceDetails",
        container="InstanceDetails",
        size_fields=("InstanceType",),
        attribute_fields=(
            ("AvailabilityZone", "AZ"),
            ("Platform", "platform"),
            ("Tenancy", "tenancy"),
        ),
    ),
    SpecShape(
        key="RDSInstanceDetails",
        container="InstanceDetails",
        size_fields=("InstanceType",),
        attribute_fields=(
            ("DeploymentOption", "deployment"),
            ("DatabaseEngine", "engine"),
            ("DatabaseEdition", "edition"),
            ("LicenseModel", "license"),
            ("DeploymentModel", "deployment model"),
        ),
    ),
    SpecShape(
        key="ElastiCacheInstanceDetails",
        container="InstanceDetails",
        size_fields=("NodeType",),
        attribute_fields=(("ProductDescription", "engine"),),
    ),
    SpecShape(
        key="RedshiftInstanceDetails",
        container="InstanceDetails",
        size_fields=("NodeType",),
    ),
    SpecShape(
        key="MemoryDBInstanceDetails",
        container="InstanceDetails",
        size_fields=("NodeType",),
    ),
    SpecShape(
        key="ESInstanceDetails",
        container="InstanceDetails",
        size_fields=("InstanceClass", "InstanceSize"),
        family_field=None,
    ),
    SpecShape(
        key="DynamoDBCapacityDetails",
        container="ReservedCapacityDetails",
        size_fields=(),
        attribute_fields=(("CapacityUnits", "capacity units"),),
        family_field=None,
    ),
)


RECOMMENDATION_SPEC_KEYS = tuple(s.key for s in RECOMMENDATION_SPECS)


def _attribute_display(label: str, value: Any) -> str:
    """Render one attribute for a compact spec string.

    A bare number tells a reader nothing, so numeric values carry their label
    ("1000 capacity units"); named values already read as themselves
    ("Multi-AZ", "Linux/UNIX") and are left alone. A missing value is dropped
    rather than stringified — `str(None)` is the literal "None", which would
    read as a real specification in a report.
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    return f"{text} {label}" if text.replace(".", "", 1).isdigit() else text


def _spec_label(size: str, attributes: dict[str, str], region: str = "") -> str:
    """Join a spec into one line: what to buy, then where.

    Region goes last because it qualifies everything before it, and the whole
    string has to survive being read inside a markdown table cell.
    """
    parts = [size, *attributes.values(), region]
    return " · ".join(p for p in (str(x).strip() for x in parts) if p)


def describe_recommendation_spec(detail: dict[str, Any]) -> dict[str, Any]:
    """Extract the purchasable specification from one CE recommendation detail.

    Returns `{}` when no known sub-structure is present, so a caller can degrade
    to the family-level figure instead of raising — a new AWS service appearing
    under `InstanceDetails` should cost the report one column, not the run.
    """
    for spec in RECOMMENDATION_SPECS:
        raw = (detail.get(spec.container) or {}).get(spec.key)
        if not raw:
            continue
        size = ".".join(
            str(raw.get(f) or "").strip() for f in spec.size_fields
        ).strip(".")
        attributes = {
            label: _attribute_display(label, raw.get(field))
            for field, label in spec.attribute_fields
            if str(raw.get(field) or "").strip()
        }
        region = str(raw.get(spec.region_field) or "")
        return {
            "spec_key": spec.key,
            "instance_type": size,
            "family": str(raw.get(spec.family_field) or "") if spec.family_field else "",
            "region": region,
            "attributes": {k: v for k, v in attributes.items() if v},
            # Size flexibility decides whether the recommended size is binding:
            # a size-flexible reservation can be bought at another size in the
            # same family and still apply, and AWS reports it per line item.
            "size_flex_eligible": bool(raw.get("SizeFlexEligible")),
            "current_generation": bool(raw.get("CurrentGeneration")),
            "label": _spec_label(size, attributes, region),
        }
    return {}


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
INVENTORY_BLIND_SPOTS = ("DynamoDB reserved capacity (no describe API exists)",)

# States that mean "this commitment is still costing or saving money". A
# retired/expired row is history, not something to renew.
ACTIVE_RESERVATION_STATES = ("active", "payment-pending", "pending", "retired-pending")
ACTIVE_SP_STATES = ("active", "payment-pending")


@dataclass(frozen=True)
class Clients:
    """Immutable bundle of the read-only clients a collection run needs.

    `make_client` is the escape hatch for the regional inventory calls: unlike
    Cost Explorer there is no single client that can answer them, so the host
    supplies a factory instead of a fixed client. It is optional and defaults
    to None, which makes expiry collection degrade to a reported warning rather
    than an exception on hosts that do not grant the extra Describe*
    permissions.
    """

    ce: Any
    coh: Any
    account_id: str
    profile: str | None
    make_client: Callable[[str, str], Any] | None = None


def build_clients(profile: str | None = None) -> Clients:
    session = (
        boto3.Session(profile_name=profile) if profile else boto3.Session()
    )
    sts = session.client("sts", region_name=CE_REGION)
    return Clients(
        ce=session.client("ce", region_name=CE_REGION),
        coh=session.client("cost-optimization-hub", region_name=COH_REGION),
        account_id=sts.get_caller_identity()["Account"],
        profile=profile,
        make_client=lambda service, region: session.client(
            service, region_name=region
        ),
    )


def _error(exc: ClientError) -> dict[str, str]:
    """Normalize a ClientError into a reportable dict.

    Cost Explorer raises DataUnavailableException with an EMPTY message when an
    account has no commitments of the requested kind. Substituting a readable
    explanation here keeps that case from surfacing as a blank error in the
    report.
    """
    err = exc.response.get("Error", {})
    code = err.get("Code", "Unknown")
    message = err.get("Message") or ""
    if not message:
        if code == "DataUnavailableException":
            message = (
                "No data for this period — usually means no active commitment "
                "of this type, or the account is too new to have billing data."
            )
        else:
            message = "(API returned no error message)"
    return {"error_code": code, "error": message}


# --------------------------------------------------------------------------
# Purchase recommendations
# --------------------------------------------------------------------------


def get_sp_recommendation(
    clients: Clients,
    sp_type: str,
    term: str,
    payment: str,
    lookback: str,
    account_scope: str,
) -> dict[str, Any]:
    """Fetch one Savings Plans purchase recommendation permutation."""
    try:
        resp = clients.ce.get_savings_plans_purchase_recommendation(
            SavingsPlansType=sp_type,
            TermInYears=term,
            PaymentOption=payment,
            LookbackPeriodInDays=lookback,
            AccountScope=account_scope,
            PageSize=100,
        )
    except ClientError as exc:
        return {"sp_type": sp_type, "term": term, "payment": payment, **_error(exc)}

    rec = resp.get("SavingsPlansPurchaseRecommendation", {})
    meta = resp.get("Metadata", {})
    return {
        "sp_type": sp_type,
        "term": term,
        "payment": payment,
        "lookback": lookback,
        "account_scope": account_scope,
        "summary": rec.get("SavingsPlansPurchaseRecommendationSummary", {}),
        "details": rec.get("SavingsPlansPurchaseRecommendationDetails", []),
        "generated_at": meta.get("GenerationTimestamp", ""),
        "recommendation_id": meta.get("RecommendationId", ""),
    }


def get_ri_recommendation(
    clients: Clients,
    service: str,
    term: str,
    payment: str,
    lookback: str,
    account_scope: str,
    offering_class: str = "STANDARD",
) -> dict[str, Any]:
    """Fetch one Reserved Instance purchase recommendation permutation.

    ServiceSpecification/OfferingClass is EC2-only; sending it for RDS or
    Redshift is rejected, so it is applied conditionally.
    """
    params: dict[str, Any] = {
        "Service": service,
        "TermInYears": term,
        "PaymentOption": payment,
        "LookbackPeriodInDays": lookback,
        "AccountScope": account_scope,
        "PageSize": 100,
    }
    if service == "Amazon Elastic Compute Cloud - Compute":
        params["ServiceSpecification"] = {
            "EC2Specification": {"OfferingClass": offering_class}
        }

    try:
        resp = clients.ce.get_reservation_purchase_recommendation(**params)
    except ClientError as exc:
        return {"service": service, "term": term, "payment": payment, **_error(exc)}

    recs = resp.get("Recommendations", [])
    meta = resp.get("Metadata", {})
    # One Recommendations entry per (term, payment, scope); details hold the
    # per-instance-family line items.
    summary = recs[0].get("RecommendationSummary", {}) if recs else {}
    details = recs[0].get("RecommendationDetails", []) if recs else []
    return {
        "service": service,
        "label": RI_SERVICE_LABELS.get(service, service),
        "term": term,
        "payment": payment,
        "lookback": lookback,
        "account_scope": account_scope,
        "offering_class": offering_class if "EC2" in service else None,
        "summary": summary,
        "details": details,
        "generated_at": meta.get("GenerationTimestamp", ""),
        "recommendation_id": meta.get("RecommendationId", ""),
    }


# --------------------------------------------------------------------------
# Existing-commitment posture: coverage and utilization
# --------------------------------------------------------------------------


def _time_period(days: int) -> dict[str, str]:
    end = date.today()
    return {"Start": (end - timedelta(days=days)).isoformat(), "End": end.isoformat()}


def get_sp_coverage(clients: Clients, days: int) -> dict[str, Any]:
    """Share of SP-eligible spend already covered by a Savings Plan."""
    try:
        resp = clients.ce.get_savings_plans_coverage(
            TimePeriod=_time_period(days), Granularity="MONTHLY"
        )
    except ClientError as exc:
        return _error(exc)
    return {"periods": resp.get("SavingsPlansCoverages", [])}


def get_sp_utilization(clients: Clients, days: int) -> dict[str, Any]:
    """How much of what you already committed to is actually being used."""
    try:
        resp = clients.ce.get_savings_plans_utilization(
            TimePeriod=_time_period(days), Granularity="MONTHLY"
        )
    except ClientError as exc:
        return _error(exc)
    return {
        "total": resp.get("Total", {}),
        "periods": resp.get("SavingsPlansUtilizationsByTime", []),
    }


def get_ri_coverage(clients: Clients, days: int) -> dict[str, Any]:
    try:
        resp = clients.ce.get_reservation_coverage(
            TimePeriod=_time_period(days), Granularity="MONTHLY"
        )
    except ClientError as exc:
        return _error(exc)
    return {"total": resp.get("Total", {}), "periods": resp.get("CoveragesByTime", [])}


def get_ri_utilization(clients: Clients, days: int) -> dict[str, Any]:
    try:
        resp = clients.ce.get_reservation_utilization(
            TimePeriod=_time_period(days), Granularity="MONTHLY"
        )
    except ClientError as exc:
        return _error(exc)
    return {
        "total": resp.get("Total", {}),
        "periods": resp.get("UtilizationsByTime", []),
    }


# --------------------------------------------------------------------------
# Cost Optimization Hub — independent second opinion for reconciliation
# --------------------------------------------------------------------------


def get_coh_enrollment(clients: Clients) -> dict[str, Any]:
    try:
        resp = clients.coh.list_enrollment_statuses(includeOrganizationInfo=True)
    except ClientError as exc:
        return {"enrolled": False, **_error(exc)}
    items = resp.get("items", [])
    if not items:
        return {
            "enrolled": False,
            "status": "NOT_ENROLLED",
            "error": "Cost Optimization Hub is not enabled for this account.",
        }
    status = items[0].get("status", "Inactive")
    return {
        "enrolled": status == "Active",
        "status": status,
        "account_id": items[0].get("accountId", ""),
        "include_member_accounts": resp.get("includeMemberAccounts", False),
    }


def get_coh_commitment_recommendations(clients: Clients) -> dict[str, Any]:
    """List only COH recommendations that are commitment purchases.

    Filtered to the commitment resource types so rightsizing/idle findings —
    which the platform's cost-optimization-hub tool already surfaces — do not
    dilute this report.
    """
    try:
        paginator = clients.coh.get_paginator("list_recommendations")
        pages = paginator.paginate(
            filter={
                "actionTypes": ["PurchaseSavingsPlans", "PurchaseReservedInstances"],
                "resourceTypes": list(COH_COMMITMENT_RESOURCE_TYPES),
            },
            includeAllRecommendations=True,
        )
        items = [item for page in pages for item in page.get("items", [])]
    except ClientError as exc:
        return _error(exc)

    return {
        "recommendations": [
            {
                "recommendation_id": i.get("recommendationId", ""),
                "account_id": i.get("accountId", ""),
                "region": i.get("region", ""),
                "current_resource_type": i.get("currentResourceType", ""),
                "recommended_resource_type": i.get("recommendedResourceType", ""),
                "action_type": i.get("actionType", ""),
                "estimated_monthly_savings": i.get("estimatedMonthlySavings", 0) or 0,
                "estimated_savings_percentage": i.get("estimatedSavingsPercentage", 0)
                or 0,
                "implementation_effort": i.get("implementationEffort", ""),
            }
            for i in items
        ],
        "count": len(items),
    }


def get_eligible_spend(clients: Clients, days: int) -> dict[str, Any]:
    """Monthly unblended spend by service, for sizing the opportunity.

    Used to state what share of the bill is even commitment-addressable, so a
    "$0 savings" result can be distinguished from "no eligible spend".
    """
    try:
        resp = clients.ce.get_cost_and_usage(
            TimePeriod=_time_period(days),
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
    except ClientError as exc:
        return _error(exc)

    periods = []
    for result in resp.get("ResultsByTime", []):
        groups = {
            g["Keys"][0]: float(g["Metrics"]["UnblendedCost"]["Amount"])
            for g in result.get("Groups", [])
        }
        periods.append(
            {
                "start": result["TimePeriod"]["Start"],
                "end": result["TimePeriod"]["End"],
                "total": round(sum(groups.values()), 2),
                "by_service": groups,
            }
        )
    return {"periods": periods}


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
        resp = getattr(client, spec.method)()
    except ClientError as exc:
        return {"service": service, "region": region, **_error(exc)}

    items = [
        _normalize_reservation(spec, row, region)
        for row in resp.get(spec.response_key, [])
        if str(row.get("State", "")).lower() in ACTIVE_RESERVATION_STATES
    ]
    return {"service": service, "region": region, "items": items}


def get_savings_plan_inventory(
    clients: Clients, region: str = CE_REGION
) -> dict[str, Any]:
    """List active Savings Plans — account-level, so called once, not per region.

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
            kwargs: dict[str, Any] = {"states": list(ACTIVE_SP_STATES)}
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
