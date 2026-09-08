"""Parallel collection and envelope shaping — shared by every entrypoint.

This module exists so that a caller only has to supply credentials. Everything
between "here are my clients" and "here is the finished payload" lives here:
the permutation sweep, the thread pools, the per-query error tolerance, and the
two output shapes (markdown payload, JSON envelope).

Any host can drive the analysis with four lines::

    from commitments import api, collect, report
    clients = api.build_clients(profile=None)   # or build api.Clients yourself
    payload = collect.collect_all(clients)
    print(report.render(payload))

Nothing here imports boto3, argparse, or reads the filesystem. Every AWS call
goes through the `api.Clients` record handed in, so a host that builds its
clients some other way (assumed role, injected stub, cross-account session)
gets the same pipeline without touching this file.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, datetime, timezone
from typing import Any

from .analyze import (
    EXPIRY_HORIZON_DAYS,
    Finding,
    LineItem,
    analyze_expiry,
    analyze_ri_recommendation,
    analyze_sp_recommendation,
    assess_existing_posture,
    reconcile_with_coh,
    select_best_findings,
)
from .api import (
    INVENTORY_BLIND_SPOTS,
    INVENTORY_KEYS,
    RI_SERVICE_LABELS,
    RI_SERVICES,
    SP_TYPE_LABELS,
    SP_TYPES,
    Clients,
    get_coh_commitment_recommendations,
    get_coh_enrollment,
    get_eligible_spend,
    get_reservation_inventory,
    get_ri_coverage,
    get_ri_recommendation,
    get_ri_utilization,
    get_savings_plan_inventory,
    get_sp_coverage,
    get_sp_recommendation,
    get_sp_utilization,
)

# Cost Explorer throttles aggressively on the recommendation APIs; 6 keeps a
# full permutation sweep inside the rate limit while still finishing in
# reasonable wall-clock time (and inside a 300s Lambda timeout).
MAX_WORKERS = 6

TERMS = ("ONE_YEAR", "THREE_YEARS")
PAYMENTS = ("NO_UPFRONT", "PARTIAL_UPFRONT", "ALL_UPFRONT")
LOOKBACKS = ("SEVEN_DAYS", "THIRTY_DAYS", "SIXTY_DAYS")
ACCOUNT_SCOPES = ("PAYER", "LINKED")
FAMILIES = ("sp", "ri")

# Defaults chosen to keep a first run cheap: Cost Explorer bills $0.01 per
# recommendation request, so evaluating both terms against the two payment
# extremes (rather than all three) halves the sweep without losing the
# no-upfront/all-upfront spread that drives the break-even discussion.
DEFAULT_TERMS = ("ONE_YEAR", "THREE_YEARS")
DEFAULT_PAYMENTS = ("NO_UPFRONT", "ALL_UPFRONT")
DEFAULT_LOOKBACK = "THIRTY_DAYS"
DEFAULT_ACCOUNT_SCOPE = "PAYER"
DEFAULT_POSTURE_DAYS = 30
DEFAULT_SPEND_DAYS = 60
DEFAULT_EXPIRY_HORIZON_DAYS = EXPIRY_HORIZON_DAYS

# Region names reach the AWS SDK as an endpoint component, so they are checked
# against the shape AWS actually uses rather than passed through. Caller input
# that is not region-shaped is a caller error, not something to send onward.
REGION_PATTERN = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")


# ---------------------------------------------------------------------------
# Parameter resolution
# ---------------------------------------------------------------------------


def resolve_ri_services(tokens: list[str]) -> list[str]:
    """Resolve RI service tokens to Cost Explorer service names.

    Accepts the full API name or the short label, case-insensitively. Labels
    carrying a parenthetical qualifier ("Elasticsearch (legacy)") also match on
    their base word alone, since nobody types the parentheses. Duplicates are
    dropped: Cost Explorer bills per recommendation request, so asking for
    "EC2, ec2" must not pay twice for the same answer.

    Raises `ValueError` on an unknown token — callers translate that into
    whatever their host expects (a CLI `SystemExit`, a tool error envelope).
    """
    if len(tokens) == 1 and tokens[0].strip().lower() == "all":
        return list(RI_SERVICES)

    by_label: dict[str, str] = {}
    for service, label in RI_SERVICE_LABELS.items():
        by_label[label.lower()] = service
        by_label.setdefault(label.split("(")[0].strip().lower(), service)
    by_name = {s.lower(): s for s in RI_SERVICES}

    resolved: list[str] = []
    for raw in tokens:
        key = raw.strip().lower()
        if not key:
            continue
        match = by_name.get(key) or by_label.get(key)
        if match is None:
            raise ValueError(
                f"Unknown RI service {raw.strip()!r}. Valid short labels: "
                + ", ".join(sorted(RI_SERVICE_LABELS.values()))
            )
        if match not in resolved:
            resolved.append(match)
    return resolved


def resolve_sp_types(tokens: list[str]) -> list[str]:
    """Resolve Savings Plan type tokens, case-insensitively. `ValueError` on miss.

    Duplicates are dropped for the same reason as `resolve_ri_services`.
    """
    if len(tokens) == 1 and tokens[0].strip().lower() == "all":
        return list(SP_TYPES)

    upper = [t.strip().upper() for t in tokens if t.strip()]
    unknown = [t for t in upper if t not in SP_TYPES]
    if unknown:
        raise ValueError(
            f"Unknown savings plan type {unknown[0]!r}. Choose from "
            + ", ".join(SP_TYPES)
        )
    return list(dict.fromkeys(upper))


def validate_choices(values: list[str], allowed: tuple[str, ...], label: str) -> list[str]:
    """Return *values* unchanged, or raise `ValueError` naming the first bad one."""
    for value in values:
        if value not in allowed:
            raise ValueError(
                f"Invalid {label} {value!r}. Choose from {', '.join(allowed)}."
            )
    return values


# ---------------------------------------------------------------------------
# Parallel collection
# ---------------------------------------------------------------------------


def run_jobs(jobs: list[tuple[str, Callable[[], dict]]]) -> tuple[list[dict], list[dict]]:
    """Run labelled callables in parallel, tolerating per-query failure.

    One throttled or unauthorized permutation must not lose the other 40, so
    failures are collected as warnings and the report is reported as a lower
    bound rather than aborted.
    """
    # Imported lazily so that importing this module costs nothing on hosts that
    # only want the pure helpers (serialize_finding, envelope).
    from concurrent.futures import ThreadPoolExecutor

    results: list[dict] = []
    errors: list[dict] = []
    if not jobs:
        return results, errors

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [(label, pool.submit(fn)) for label, fn in jobs]
        for label, future in futures:
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - one bad query must not abort
                errors.append({"query": label, "error": str(exc)})
                continue
            if result.get("error"):
                errors.append(
                    {
                        "query": label,
                        "error_code": result.get("error_code", ""),
                        "error": result["error"],
                    }
                )
                continue
            results.append(result)
    return results, errors


def sweep_savings_plans(
    clients: Clients,
    sp_types: list[str],
    terms: list[str],
    payments: list[str],
    lookback: str,
    account_scope: str,
) -> tuple[list[dict], list[dict]]:
    """Query every (type × term × payment) SP permutation. Returns (recs, errors)."""
    jobs = [
        (
            f"SP {SP_TYPE_LABELS.get(sp_type, sp_type)} {term} {payment}",
            lambda s=sp_type, t=term, p=payment: get_sp_recommendation(
                clients, s, t, p, lookback, account_scope
            ),
        )
        for sp_type in sp_types
        for term in terms
        for payment in payments
    ]
    return run_jobs(jobs)


def sweep_reservations(
    clients: Clients,
    services: list[str],
    terms: list[str],
    payments: list[str],
    lookback: str,
    account_scope: str,
) -> tuple[list[dict], list[dict]]:
    """Query every (service × term × payment) RI permutation. Returns (recs, errors)."""
    jobs = [
        (
            f"RI {RI_SERVICE_LABELS.get(service, service)} {term} {payment}",
            lambda s=service, t=term, p=payment: get_ri_recommendation(
                clients, s, t, p, lookback, account_scope
            ),
        )
        for service in services
        for term in terms
        for payment in payments
    ]
    return run_jobs(jobs)


def collect_posture(
    clients: Clients,
    posture_days: int = DEFAULT_POSTURE_DAYS,
    spend_days: int = DEFAULT_SPEND_DAYS,
) -> dict:
    """Fetch existing coverage/utilization, COH enrollment, and eligible spend.

    Every key is always present; a failed query lands as `{"error": ...}` in its
    own slot so the caller can report partial posture instead of nothing.
    """
    from concurrent.futures import ThreadPoolExecutor

    jobs: dict[str, Callable[[], dict]] = {
        "sp_coverage": lambda: get_sp_coverage(clients, posture_days),
        "sp_utilization": lambda: get_sp_utilization(clients, posture_days),
        "ri_coverage": lambda: get_ri_coverage(clients, posture_days),
        "ri_utilization": lambda: get_ri_utilization(clients, posture_days),
        "coh_enrollment": lambda: get_coh_enrollment(clients),
        "eligible_spend": lambda: get_eligible_spend(clients, spend_days),
    }
    collected: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        submitted = {key: pool.submit(fn) for key, fn in jobs.items()}
        for key, future in submitted.items():
            try:
                collected[key] = future.result()
            except Exception as exc:  # noqa: BLE001
                collected[key] = {"error": str(exc)}
    return collected


def resolve_regions(tokens: list[str] | None, default_region: str = "") -> list[str]:
    """Validate and de-duplicate a caller-supplied region list.

    Reservations are regional, so a sweep needs an explicit list; Savings Plans
    are account-level and are fetched once regardless. Defaults to the single
    region the host itself runs in, because widening the sweep multiplies API
    calls and a caller who wants org-wide coverage should say so.
    """
    values = [t.strip().lower() for t in (tokens or []) if t and t.strip()]
    if not values and default_region:
        values = [default_region.strip().lower()]
    if not values:
        raise ValueError("At least one AWS region is required for expiry inventory.")
    bad = [v for v in values if not REGION_PATTERN.match(v)]
    if bad:
        raise ValueError(
            f"Not a valid AWS region name: {', '.join(bad)}. "
            "Expected e.g. us-east-1, ap-northeast-1."
        )
    return list(dict.fromkeys(values))


def collect_expiry(
    clients: Clients,
    regions: list[str],
    services: list[str] | None = None,
    horizon_days: int = DEFAULT_EXPIRY_HORIZON_DAYS,
    *,
    sp_utilization_pct: float | None = None,
    ri_utilization_pct: float | None = None,
    as_of: date | None = None,
) -> tuple[dict[str, Any], list[dict]]:
    """Inventory every commitment that carries a date, and judge its renewal.

    One job per (family, region) plus one un-multiplied Savings Plans job — the
    SP API is account-level, so sweeping it per region would return the same
    plans N times and quietly inflate every total.
    """
    families = [s.strip().lower() for s in (services or list(INVENTORY_KEYS)) if s.strip()]
    validate_choices(families, INVENTORY_KEYS, "reservation family")

    jobs: list[tuple[str, Callable[[], dict]]] = [
        (
            f"{family} reservations ({region})",
            lambda f=family, r=region: get_reservation_inventory(clients, f, r),
        )
        for region in regions
        for family in families
    ]
    jobs.append(("savings plan inventory", lambda: get_savings_plan_inventory(clients)))

    results, errors = run_jobs(jobs)
    items = [item for result in results for item in result.get("items", [])]
    expiry = analyze_expiry(
        items,
        as_of or datetime.now(timezone.utc).date(),
        horizon_days,
        sp_utilization_pct=sp_utilization_pct,
        ri_utilization_pct=ri_utilization_pct,
    )
    expiry["regions"] = list(regions)
    expiry["blind_spots"] = list(INVENTORY_BLIND_SPOTS)
    return expiry, errors


def fetch_coh(clients: Clients, coh_enrollment: dict) -> dict:
    """Fetch COH recommendations, or an error dict explaining why we cannot.

    Calling `ListRecommendations` while unenrolled returns an unhelpful access
    error, so enrollment is checked first and the reason is passed through to
    the report instead.
    """
    if coh_enrollment.get("enrolled"):
        return get_coh_commitment_recommendations(clients)
    reason = coh_enrollment.get("error") or coh_enrollment.get("status", "not enrolled")
    return {"error": f"Cost Optimization Hub not available: {reason}"}


# ---------------------------------------------------------------------------
# Output shaping
# ---------------------------------------------------------------------------


def findings_from(sp_recs: list[dict], ri_recs: list[dict]) -> list[Finding]:
    """Turn raw recommendation responses into risk-adjusted findings."""
    findings = [
        f for f in (analyze_sp_recommendation(r) for r in sp_recs) if f is not None
    ]
    findings += [
        f for f in (analyze_ri_recommendation(r) for r in ri_recs) if f is not None
    ]
    return findings


def serialize_line_item(item: LineItem) -> dict:
    """Flatten one purchasable line of a recommendation.

    A consumer that only reads the finding-level total cannot act on it: a
    reservation applies to usage matching its exact specification, so `spec` and
    `region` are what a purchase is actually placed against.
    """
    return {
        "spec": item.spec,
        "region": item.region,
        "commitment_unit": item.unit,
        "aws_recommended_commitment": round(item.recommended, 4),
        "achievable_commitment": round(item.achievable, 4),
        "minimum_observed_units": round(item.floor, 4),
        "average_observed_units": round(item.average, 4),
        "estimated_monthly_savings": round(item.monthly_savings, 2),
        "upfront_cost": round(item.upfront_cost, 2),
        "monthly_on_demand_cost": round(item.monthly_on_demand, 2),
        "estimated_utilization_percentage": (
            round(item.utilization_pct, 2) if item.utilization_pct is not None else None
        ),
        "size_flexible": item.size_flex_eligible,
        "current_generation": item.current_generation,
        "account_id": item.account_id,
    }


def serialize_finding(f: Finding) -> dict:
    """Flatten a Finding into a flat recommendation dict.

    Key names mirror what an AWS Cost Optimization Hub style recommendation
    list looks like, so a host that already renders those needs no translation
    layer.
    """
    return {
        "commitment_family": f.family,
        "commitment_type": f.label,
        "term": f.term,
        "payment_option": f.payment,
        "aws_recommended_commitment": round(f.api_hourly_commitment, 4),
        "achievable_commitment": round(f.safe_hourly_commitment, 4),
        "commitment_unit": "USD/hour" if f.family == "savings-plan" else "units",
        "estimated_monthly_savings": round(f.safe_monthly_savings, 2),
        "aws_best_case_monthly_savings": round(f.api_monthly_savings, 2),
        "estimated_savings_percentage": round(f.savings_percentage, 2),
        "upfront_cost": round(f.upfront_cost, 2),
        "break_even_months": (
            round(f.break_even_months, 1) if f.break_even_months else None
        ),
        "waste_exposure_monthly": round(f.waste_exposure_monthly, 2),
        "confidence": f.confidence,
        "spend_profile": f.volatility,
        "implementation_effort": "Medium",
        "rationale": f.rationale,
        "line_items": [serialize_line_item(i) for i in f.line_items],
    }


def envelope(findings: list[Finding], reconciliation: dict | None = None) -> dict:
    """Build the JSON response shape: recommendations plus roll-up totals."""
    payload = {
        "recommendations": [serialize_finding(f) for f in findings],
        "count": len(findings),
        "total_estimated_monthly_savings": round(
            sum(f.safe_monthly_savings for f in findings), 2
        ),
        "aws_best_case_monthly_savings": round(
            sum(f.api_monthly_savings for f in findings), 2
        ),
    }
    if reconciliation is not None:
        payload["reconciliation"] = reconciliation
    return payload


def build_meta(
    clients: Clients,
    lookback: str,
    account_scope: str,
    profile: str | None = None,
) -> dict:
    """Provenance block for the rendered report."""
    return {
        "account_id": clients.account_id,
        "profile": profile,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "lookback": lookback,
        "account_scope": account_scope,
    }


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------


def collect_all(
    clients: Clients,
    *,
    families: tuple[str, ...] | list[str] = FAMILIES,
    sp_types: list[str] | None = None,
    ri_services: list[str] | None = None,
    terms: list[str] | None = None,
    payments: list[str] | None = None,
    lookback: str = DEFAULT_LOOKBACK,
    account_scope: str = DEFAULT_ACCOUNT_SCOPE,
    posture_days: int = DEFAULT_POSTURE_DAYS,
    spend_days: int = DEFAULT_SPEND_DAYS,
    regions: list[str] | None = None,
    inventory_services: list[str] | None = None,
    expiry_horizon_days: int = DEFAULT_EXPIRY_HORIZON_DAYS,
    profile: str | None = None,
) -> dict[str, Any]:
    """Run the whole pipeline and return the payload `report.render` expects.

    Sweeps recommendations, fetches existing posture, reconciles against Cost
    Optimization Hub, and keeps the best finding per (family, term, payment).
    Every parameter is validated here, so a host can pass user input straight
    through and catch `ValueError`.

    The returned dict carries `meta`, `findings`, `posture`, `reconciliation`,
    `eligible_spend`, `expiry`, `errors`, `sweep_errors` — plus `raw` for a
    caller that wants the unanalyzed responses.

    `regions` is opt-in: expiry inventory needs regional Describe* permissions
    beyond the Cost Explorer set, so passing nothing leaves `expiry` as None
    and makes no extra calls rather than failing a caller that only granted
    `ce:Get*`.
    """
    families = [f.strip().lower() for f in families if f.strip()]
    validate_choices(families, FAMILIES, "family")
    terms = validate_choices(list(terms or DEFAULT_TERMS), TERMS, "term")
    payments = validate_choices(
        list(payments or DEFAULT_PAYMENTS), PAYMENTS, "payment option"
    )
    validate_choices([lookback], LOOKBACKS, "lookback")
    validate_choices([account_scope], ACCOUNT_SCOPES, "account scope")
    if regions:
        regions = resolve_regions(regions)

    sp_recs: list[dict] = []
    ri_recs: list[dict] = []
    errors: list[dict] = []

    if "sp" in families:
        recs, errs = sweep_savings_plans(
            clients,
            resolve_sp_types(sp_types or ["all"]),
            terms,
            payments,
            lookback,
            account_scope,
        )
        sp_recs, errors = recs, errors + errs

    if "ri" in families:
        recs, errs = sweep_reservations(
            clients,
            resolve_ri_services(ri_services or ["all"]),
            terms,
            payments,
            lookback,
            account_scope,
        )
        ri_recs, errors = recs, errors + errs

    posture_data = collect_posture(clients, posture_days, spend_days)
    coh = fetch_coh(clients, posture_data["coh_enrollment"])

    best = select_best_findings(findings_from(sp_recs, ri_recs))
    posture = assess_existing_posture(
        posture_data["sp_coverage"],
        posture_data["sp_utilization"],
        posture_data["ri_coverage"],
        posture_data["ri_utilization"],
    )
    reconciliation = reconcile_with_coh(best, coh)

    # Reuses the utilization already measured above rather than re-querying:
    # Cost Explorer has no per-commitment utilization API, so the account-level
    # figure is the only one there is, and it is what drives the renewal call.
    # Kept separate from `errors` because a caller counting queries is counting
    # BILLABLE ones — Cost Explorer charges $0.01 per recommendation request,
    # while the inventory Describe* calls are free. Folding expiry failures into
    # that count would overstate the bill.
    sweep_errors = list(errors)

    expiry = None
    if regions:
        expiry, expiry_errors = collect_expiry(
            clients,
            regions,
            inventory_services,
            expiry_horizon_days,
            sp_utilization_pct=posture.get("sp_utilization_pct"),
            ri_utilization_pct=posture.get("ri_utilization_pct"),
        )
        errors = errors + expiry_errors

    return {
        "meta": build_meta(clients, lookback, account_scope, profile),
        "findings": best,
        "posture": posture,
        "reconciliation": reconciliation,
        "eligible_spend": posture_data["eligible_spend"],
        "expiry": expiry,
        "errors": errors,
        "sweep_errors": sweep_errors,
        "raw": {"sp_recs": sp_recs, "ri_recs": ri_recs, "coh": coh, **posture_data},
    }
