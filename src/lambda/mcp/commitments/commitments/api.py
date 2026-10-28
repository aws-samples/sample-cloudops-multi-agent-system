"""Read-only AWS API wrappers for commitment (RI/SP) analysis.

Every call in this module is a Get*/List*/Describe* operation. Nothing here
mutates state, purchases a commitment, or starts a billable analysis.

Both Cost Explorer and Cost Optimization Hub are us-east-1-only APIs regardless
of where your resources live, so the clients are pinned there.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# Cost Explorer and Cost Optimization Hub are global services fronted only by
# us-east-1. Calling them in another region fails to resolve an endpoint.
CE_REGION = "us-east-1"
COH_REGION = "us-east-1"

# Cost Explorer signals throttling with LimitExceededException, which botocore's
# legacy retry mode does not retry. Without this, a throttled permutation turns
# into a warning and selection quietly picks a worse term or payment option.
RETRY_CONFIG = Config(retries={"mode": "adaptive", "max_attempts": 10})

# Cost Explorer bills every API request — recommendation, coverage, utilization
# and cost-and-usage alike, per page — at this rate.
CE_PRICE_PER_REQUEST = 0.01

# Page size for the recommendation APIs; both cap at 100 detail rows per page.
RECOMMENDATION_PAGE_SIZE = 100

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

# The coverage/utilization SERVICE filter accepts a DIFFERENT set of names than
# the purchase-recommendation `Service` parameter above, read back the same way
# (ValidationException "Supported values for Service Filter are ..."). MemoryDB
# drops the "Service" suffix, and DynamoDB reserved capacity has no filter
# value at all, so its coverage and utilization cannot be measured.
RI_POSTURE_FILTER_VALUES = {
    "Amazon Elastic Compute Cloud - Compute": "Amazon Elastic Compute Cloud - Compute",
    "Amazon Relational Database Service": "Amazon Relational Database Service",
    "Amazon Redshift": "Amazon Redshift",
    "Amazon ElastiCache": "Amazon ElastiCache",
    "Amazon Elasticsearch Service": "Amazon Elasticsearch Service",
    "Amazon OpenSearch Service": "Amazon OpenSearch Service",
    "Amazon MemoryDB Service": "Amazon MemoryDB",
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

# Hours in each lookback window, for turning a lookback-period total into an
# hourly rate. Dividing by HOURS_PER_MONTH instead is only right for 30 days and
# is 4.3x off for SEVEN_DAYS.
LOOKBACK_HOURS = {
    "SEVEN_DAYS": 7 * 24.0,
    "THIRTY_DAYS": 30 * 24.0,
    "SIXTY_DAYS": 60 * 24.0,
}

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
class Clients:
    """Immutable bundle of the read-only clients a collection run needs.

    `make_client` is the escape hatch for the regional inventory calls: unlike
    Cost Explorer there is no single client that can answer them, so the host
    supplies a factory instead of a fixed client. It is optional and defaults
    to None, which makes expiry collection degrade to a reported warning rather
    than an exception on hosts that do not grant the extra Describe*
    permissions.

    `coh_account_id` is the account the Cost Optimization Hub client speaks
    for. It differs from `account_id` when COH is reached through its own role
    (a delegated-admin account), and reconciliation has to say so: two
    pipelines read in different accounts are not like-for-like.
    """

    ce: Any
    coh: Any
    account_id: str
    profile: str | None
    make_client: Callable[[str, str], Any] | None = None
    coh_account_id: str = ""


class CountingClient:
    """Wrap a client and count every request sent through it.

    Cost Explorer bills every request, each page included, so the only honest
    `queries_run` is a count taken at the client rather than one reconstructed
    from how many permutations were planned. Thread-safe because the sweep fans
    out across a pool.
    """

    _PASSTHROUGH = frozenset({"meta", "exceptions", "get_paginator", "can_paginate"})

    def __init__(self, client: Any):
        self._client = client
        self._lock = threading.Lock()
        self._count = 0

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._client, name)
        if name.startswith("_") or name in self._PASSTHROUGH or not callable(attr):
            return attr

        def counted(*args: Any, **kwargs: Any) -> Any:
            with self._lock:
                self._count += 1
            return attr(*args, **kwargs)

        return counted


def build_clients(profile: str | None = None) -> Clients:
    session = (
        boto3.Session(profile_name=profile) if profile else boto3.Session()
    )
    sts = session.client("sts", region_name=CE_REGION, config=RETRY_CONFIG)
    account_id = sts.get_caller_identity()["Account"]
    return Clients(
        ce=session.client("ce", region_name=CE_REGION, config=RETRY_CONFIG),
        coh=session.client(
            "cost-optimization-hub", region_name=COH_REGION, config=RETRY_CONFIG
        ),
        account_id=account_id,
        profile=profile,
        make_client=lambda service, region: session.client(
            service, region_name=region, config=RETRY_CONFIG
        ),
        coh_account_id=account_id,
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


# Errors that mean "there is nothing to measure here", not "the query broke".
# Utilization of a commitment type the account does not hold is
# DataUnavailableException; reporting that as a failed posture source would
# make every account without reservations look unmeasurable.
NO_DATA_ERROR_CODES = frozenset({"DataUnavailableException"})


def is_no_data(result: dict[str, Any]) -> bool:
    return result.get("error_code") in NO_DATA_ERROR_CODES


def _ce_pages(
    call: Callable[..., dict], token_key: str = "NextPageToken", **params: Any
) -> list[dict]:
    """Follow a Cost Explorer page token to the end, returning every page.

    None of these reads auto-paginate in boto3. Reading only page one silently
    truncates — for the recommendation APIs that means an organization with
    more than 100 accounts under LINKED scope loses accounts from upfront cost,
    break-even and every floor. `token_key` exists because the API is not
    consistent: `GetSavingsPlansCoverage` alone names its token `NextToken`.
    """
    pages: list[dict] = []
    token: str | None = None
    while True:
        resp = call(**params, **({token_key: token} if token else {}))
        pages.append(resp)
        token = resp.get(token_key)
        if not token:
            return pages


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
    """Fetch one Savings Plans purchase recommendation permutation, all pages.

    A failure on any page fails the whole permutation: a partial detail list
    understates upfront cost, which is the figure the break-even guard reads,
    so half a recommendation is worse than none.
    """
    try:
        pages = _ce_pages(
            clients.ce.get_savings_plans_purchase_recommendation,
            SavingsPlansType=sp_type,
            TermInYears=term,
            PaymentOption=payment,
            LookbackPeriodInDays=lookback,
            AccountScope=account_scope,
            PageSize=RECOMMENDATION_PAGE_SIZE,
        )
    except ClientError as exc:
        return {"sp_type": sp_type, "term": term, "payment": payment, **_error(exc)}

    first = pages[0].get("SavingsPlansPurchaseRecommendation", {})
    summary = first.get("SavingsPlansPurchaseRecommendationSummary", {})
    details = [
        d
        for page in pages
        for d in page.get("SavingsPlansPurchaseRecommendation", {}).get(
            "SavingsPlansPurchaseRecommendationDetails", []
        )
    ]
    meta = pages[0].get("Metadata", {})
    result = {
        "sp_type": sp_type,
        "term": term,
        "payment": payment,
        "lookback": lookback,
        "account_scope": account_scope,
        "summary": summary,
        "details": details,
        "pages": len(pages),
        "generated_at": meta.get("GenerationTimestamp", ""),
        "recommendation_id": meta.get("RecommendationId", ""),
    }
    expected = summary.get("TotalRecommendationCount")
    if expected not in (None, "") and str(expected).isdigit():
        if int(expected) != len(details):
            result["detail_count_warning"] = (
                f"AWS reports {int(expected)} recommendation detail(s) but "
                f"{len(details)} were returned; figures built from the details "
                "may be incomplete."
            )
    return result


def get_ri_recommendation(
    clients: Clients,
    service: str,
    term: str,
    payment: str,
    lookback: str,
    account_scope: str,
    offering_class: str = "STANDARD",
) -> dict[str, Any]:
    """Fetch one Reserved Instance purchase recommendation permutation, all pages.

    ServiceSpecification/OfferingClass is EC2-only; sending it for RDS or
    Redshift is rejected, so it is applied conditionally.
    """
    params: dict[str, Any] = {
        "Service": service,
        "TermInYears": term,
        "PaymentOption": payment,
        "LookbackPeriodInDays": lookback,
        "AccountScope": account_scope,
        "PageSize": RECOMMENDATION_PAGE_SIZE,
    }
    if service == "Amazon Elastic Compute Cloud - Compute":
        params["ServiceSpecification"] = {
            "EC2Specification": {"OfferingClass": offering_class}
        }

    try:
        pages = _ce_pages(clients.ce.get_reservation_purchase_recommendation, **params)
    except ClientError as exc:
        return {"service": service, "term": term, "payment": payment, **_error(exc)}

    # One Recommendations entry per (term, payment, scope); its details are what
    # pages, so the summary comes from the first page and the details from all.
    recs = [r for page in pages for r in page.get("Recommendations", [])]
    summary = recs[0].get("RecommendationSummary", {}) if recs else {}
    details = [d for r in recs for d in r.get("RecommendationDetails", [])]
    meta = pages[0].get("Metadata", {})
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
        "pages": len(pages),
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
        pages = _ce_pages(
            clients.ce.get_savings_plans_coverage,
            token_key="NextToken",
            TimePeriod=_time_period(days),
            Granularity="MONTHLY",
        )
    except ClientError as exc:
        return _error(exc)
    return {"periods": [p for page in pages for p in page.get("SavingsPlansCoverages", [])]}


def get_sp_utilization(clients: Clients, days: int) -> dict[str, Any]:
    """How much of what you already committed to is actually being used.

    Not paginated by the API: `GetSavingsPlansUtilization` takes no token.
    """
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


def _service_filter(service: str) -> dict[str, Any]:
    """Coverage/utilization filter for a recommendation-API service name."""
    return {
        "Dimensions": {
            "Key": "SERVICE",
            "Values": [RI_POSTURE_FILTER_VALUES[service]],
        }
    }


def get_ri_coverage(clients: Clients, days: int, service: str) -> dict[str, Any]:
    """Reservation coverage for ONE service.

    The SERVICE filter is mandatory in practice: omitted, Cost Explorer
    defaults it to EC2, and every RDS or ElastiCache figure would silently be
    the EC2 one.
    """
    try:
        pages = _ce_pages(
            clients.ce.get_reservation_coverage,
            TimePeriod=_time_period(days),
            Granularity="MONTHLY",
            Filter=_service_filter(service),
        )
    except ClientError as exc:
        return {"service": service, **_error(exc)}
    return {
        "service": service,
        "total": pages[0].get("Total", {}),
        "periods": [p for page in pages for p in page.get("CoveragesByTime", [])],
    }


def get_ri_utilization(clients: Clients, days: int, service: str) -> dict[str, Any]:
    """Reservation utilization for ONE service — see `get_ri_coverage`."""
    try:
        pages = _ce_pages(
            clients.ce.get_reservation_utilization,
            TimePeriod=_time_period(days),
            Granularity="MONTHLY",
            Filter=_service_filter(service),
        )
    except ClientError as exc:
        return {"service": service, **_error(exc)}
    return {
        "service": service,
        "total": pages[0].get("Total", {}),
        "periods": [p for page in pages for p in page.get("UtilizationsByTime", [])],
    }


# --------------------------------------------------------------------------
# Cost Optimization Hub — independent second opinion for reconciliation
# --------------------------------------------------------------------------


def get_coh_enrollment(clients: Clients) -> dict[str, Any]:
    """Enrollment status of the account the COH client speaks for.

    `includeOrganizationInfo=True` lists member accounts too, in no guaranteed
    order, so the caller's own row is found by account id rather than read from
    position 0 — which could be any member.
    """
    own_account = clients.coh_account_id or clients.account_id
    try:
        resp = clients.coh.list_enrollment_statuses(includeOrganizationInfo=True)
    except ClientError as exc:
        return {"enrolled": False, **_error(exc)}
    items = resp.get("items", [])
    own = next((i for i in items if i.get("accountId") == own_account), None)
    if own is None and items and not own_account:
        own = items[0]
    if own is None:
        return {
            "enrolled": False,
            "status": "NOT_ENROLLED",
            "account_id": own_account,
            "error": "Cost Optimization Hub is not enabled for this account.",
        }
    status = own.get("status", "Inactive")
    return {
        "enrolled": status == "Active",
        "status": status,
        "account_id": own.get("accountId", ""),
        "include_member_accounts": resp.get("includeMemberAccounts", False),
    }


# COH writes its commitment preference as OneYear/AllUpfront; Cost Explorer as
# ONE_YEAR/ALL_UPFRONT. Reconciling across the two needs the mapping.
COH_TERMS = {"OneYear": "ONE_YEAR", "ThreeYears": "THREE_YEARS"}
COH_PAYMENTS = {
    "NoUpfront": "NO_UPFRONT",
    "PartialUpfront": "PARTIAL_UPFRONT",
    "AllUpfront": "ALL_UPFRONT",
}

# COH's documented default when no preference has been set.
COH_DEFAULT_COMMITMENT = {"term": "THREE_YEARS", "payment": "ALL_UPFRONT"}


def get_coh_preferences(clients: Clients) -> dict[str, Any]:
    """The term/payment COH prices its commitment recommendations at.

    COH sizes every commitment at ONE preferred (term, payment) — 3-year All
    Upfront unless changed — so comparing its total against a Cost Explorer
    finding at a different term is comparing two different purchases.
    """
    try:
        resp = clients.coh.get_preferences()
    except ClientError as exc:
        return {**COH_DEFAULT_COMMITMENT, "assumed_default": True, **_error(exc)}
    pref = resp.get("preferredCommitment") or {}
    term = COH_TERMS.get(pref.get("term", ""))
    payment = COH_PAYMENTS.get(pref.get("paymentOption", ""))
    return {
        "term": term or COH_DEFAULT_COMMITMENT["term"],
        "payment": payment or COH_DEFAULT_COMMITMENT["payment"],
        "assumed_default": not (term and payment),
        "savings_estimation_mode": resp.get("savingsEstimationMode", ""),
    }


def get_coh_commitment_recommendations(clients: Clients) -> dict[str, Any]:
    """List only COH recommendations that are commitment purchases.

    Filtered to the commitment resource types so rightsizing/idle findings —
    which the platform's cost-optimization-hub tool already surfaces — do not
    dilute this report.

    `includeAllRecommendations=False` keeps COH's single preferred option per
    resource. With it on, every alternative for the same usage is returned and
    summing them counts that usage once per alternative.
    """
    try:
        paginator = clients.coh.get_paginator("list_recommendations")
        pages = paginator.paginate(
            filter={
                "actionTypes": ["PurchaseSavingsPlans", "PurchaseReservedInstances"],
                "resourceTypes": list(COH_COMMITMENT_RESOURCE_TYPES),
            },
            includeAllRecommendations=False,
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
    "$0 savings" result can be distinguished from "no eligible spend". Grouping
    by SERVICE paginates once an account has enough services, so every page is
    read and its groups merged per period.
    """
    try:
        pages = _ce_pages(
            clients.ce.get_cost_and_usage,
            TimePeriod=_time_period(days),
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
    except ClientError as exc:
        return _error(exc)

    by_period: dict[tuple[str, str], dict[str, float]] = {}
    currency = ""
    for page in pages:
        for result in page.get("ResultsByTime", []):
            key = (result["TimePeriod"]["Start"], result["TimePeriod"]["End"])
            groups = by_period.setdefault(key, {})
            for g in result.get("Groups", []):
                metric = g["Metrics"]["UnblendedCost"]
                currency = currency or metric.get("Unit", "")
                groups[g["Keys"][0]] = groups.get(g["Keys"][0], 0.0) + float(
                    metric["Amount"]
                )
    periods = [
        {
            "start": start,
            "end": end,
            "total": round(sum(groups.values()), 2),
            "by_service": groups,
        }
        for (start, end), groups in sorted(by_period.items())
    ]
    return {"periods": periods, "currency": currency or "USD"}
