"""Parallel collection and envelope shaping — shared by every entrypoint.

This module exists so that a caller only has to supply credentials. Everything
between "here are my clients" and "here is the finished payload" lives here:
the permutation sweep, the thread pools, the per-query error tolerance, the
time budget, and the output shapes (markdown payload, JSON envelope).

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
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Any

from .analyze import (
    Finding,
    analyze_ri_recommendation,
    analyze_sp_recommendation,
)
from .api import (
    CE_PRICE_PER_REQUEST,
    RI_SERVICE_LABELS,
    RI_SERVICES,
    SP_TYPE_LABELS,
    SP_TYPES,
    Clients,
    CountingClient,
    get_coh_commitment_recommendations,
    get_coh_enrollment,
    get_coh_preferences,
    get_eligible_spend,
    get_ri_coverage,
    get_ri_recommendation,
    get_ri_utilization,
    get_sp_coverage,
    get_sp_recommendation,
    get_sp_utilization,
)
from .expiry import EXPIRY_HORIZON_DAYS, analyze_expiry
from .inventory import (
    INVENTORY_BLIND_SPOTS,
    INVENTORY_KEYS,
    INVENTORY_SPECS,
    get_reservation_inventory,
    get_savings_plan_inventory,
)
from .pools import plan_purchases, select_best_findings
from .posture import assess_existing_posture, reconcile_with_coh, safe_to_buy_more
from .shape import (
    LINE_ITEM_LIMIT,
    TIME_BUDGET_ERROR,
    diagnose_failure,
    envelope,
    no_data_only,
    serialize_finding,
    serialize_line_item,
)

# Re-exported: hosts drive the whole pipeline through this module.
__all__ = [
    "LINE_ITEM_LIMIT",
    "TIME_BUDGET_ERROR",
    "diagnose_failure",
    "envelope",
    "no_data_only",
    "serialize_finding",
    "serialize_line_item",
]

# Cost Explorer throttles aggressively on the recommendation APIs; 6 keeps a
# full permutation sweep inside the rate limit while still finishing in
# reasonable wall-clock time.
MAX_WORKERS = 6

TERMS = ("ONE_YEAR", "THREE_YEARS")
PAYMENTS = ("NO_UPFRONT", "PARTIAL_UPFRONT", "ALL_UPFRONT")
LOOKBACKS = ("SEVEN_DAYS", "THIRTY_DAYS", "SIXTY_DAYS")
ACCOUNT_SCOPES = ("PAYER", "LINKED")
FAMILIES = ("sp", "ri")

# Defaults chosen to keep a first run cheap: Cost Explorer bills $0.01 per
# request, so evaluating both terms against the two payment extremes (rather
# than all three) cuts the sweep by a third without losing the
# no-upfront/all-upfront spread that drives the break-even discussion.
DEFAULT_TERMS = ("ONE_YEAR", "THREE_YEARS")
DEFAULT_PAYMENTS = ("NO_UPFRONT", "ALL_UPFRONT")
DEFAULT_LOOKBACK = "THIRTY_DAYS"
DEFAULT_ACCOUNT_SCOPE = "PAYER"
DEFAULT_POSTURE_DAYS = 30
DEFAULT_SPEND_DAYS = 60
DEFAULT_EXPIRY_HORIZON_DAYS = EXPIRY_HORIZON_DAYS

# Wall-clock budget for the recommendation sweep. The Lambda, the gateway's MCP
# read and the supervisor's delegation to the leaf agent all share one 300 s
# ceiling, and the agents still need time to read the result and answer. A
# permutation still running at the deadline is abandoned and reported as a
# collection warning, so a slow or throttled account returns a partial,
# clearly-labelled report instead of a timeout.
DEFAULT_TIME_BUDGET_SECONDS = 150.0

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
    dropped: Cost Explorer bills per request, so asking for "EC2, ec2" must not
    pay twice for the same answer.

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


def resolve_regions(tokens: list[str] | None, default_region: str = "") -> list[str]:
    """Validate and de-duplicate a caller-supplied region list.

    Reservations are regional, so a sweep needs an explicit list; Savings Plans
    are account-level and are fetched once regardless. `default_region` is used
    only when the caller passes one — the full analysis passes none, so
    omitting `regions` there really does skip the inventory.
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


# ---------------------------------------------------------------------------
# Parallel collection
# ---------------------------------------------------------------------------


def run_jobs(
    jobs: list[tuple[str, Callable[[], dict]]],
    deadline: float | None = None,
) -> tuple[list[dict], list[dict]]:
    """Run labelled callables in parallel, tolerating per-query failure.

    One throttled or unauthorized permutation must not lose the other 40, so
    failures are collected as warnings and the report is reported as a lower
    bound rather than aborted.

    `deadline` is a `time.monotonic()` value. Jobs unfinished by then are
    reported as `TimeBudgetExceeded` and left behind rather than waited for —
    the pool is shut down without waiting, so the caller returns on time.
    """
    # Imported lazily so that importing this module costs nothing on hosts that
    # only want the pure helpers (serialize_finding, envelope).
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FutureTimeout

    results: list[dict] = []
    errors: list[dict] = []
    if not jobs:
        return results, errors

    pool = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    try:
        futures = [(label, pool.submit(fn)) for label, fn in jobs]
        for label, future in futures:
            timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
            try:
                result = future.result(timeout=timeout)
            except FutureTimeout:
                future.cancel()
                errors.append(
                    {
                        "query": label,
                        "error_code": TIME_BUDGET_ERROR,
                        "error": "Not finished within the analysis time budget; "
                        "excluded from the totals.",
                    }
                )
                continue
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
    finally:
        pool.shutdown(wait=deadline is None, cancel_futures=True)
    return results, errors


def sweep_savings_plans(
    clients: Clients,
    sp_types: list[str],
    terms: list[str],
    payments: list[str],
    lookback: str,
    account_scope: str,
    deadline: float | None = None,
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
    return run_jobs(jobs, deadline)


def sweep_reservations(
    clients: Clients,
    services: list[str],
    terms: list[str],
    payments: list[str],
    lookback: str,
    account_scope: str,
    deadline: float | None = None,
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
    return run_jobs(jobs, deadline)


def _gather(jobs: dict[str, Callable[[], dict]]) -> dict[str, dict]:
    """Run keyed callables in parallel; a raised exception lands as an error."""
    from concurrent.futures import ThreadPoolExecutor

    collected: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        submitted = {key: pool.submit(fn) for key, fn in jobs.items()}
        for key, future in submitted.items():
            try:
                collected[key] = future.result()
            except Exception as exc:  # noqa: BLE001
                collected[key] = {"error": str(exc)}
    return collected


def collect_posture(
    clients: Clients,
    posture_days: int = DEFAULT_POSTURE_DAYS,
    spend_days: int = DEFAULT_SPEND_DAYS,
    ri_services: list[str] | None = None,
    *,
    include_sp: bool = True,
    include_coh: bool = True,
) -> dict:
    """Fetch existing coverage/utilization, COH state, and eligible spend.

    Reservation coverage and utilization are queried once per service in
    `ri_services`: Cost Explorer defaults an unfiltered query to EC2, so one
    call cannot speak for RDS. Each is a billed request, so callers pass only
    the services that matter to them.

    Every key is always present; a failed query lands as `{"error": ...}` in its
    own slot so the caller can report partial posture instead of nothing.
    """
    services = list(dict.fromkeys(ri_services or []))
    jobs: dict[str, Callable[[], dict]] = {
        "eligible_spend": lambda: get_eligible_spend(clients, spend_days),
    }
    if include_sp:
        jobs["sp_coverage"] = lambda: get_sp_coverage(clients, posture_days)
        jobs["sp_utilization"] = lambda: get_sp_utilization(clients, posture_days)
    if include_coh:
        jobs["coh_enrollment"] = lambda: get_coh_enrollment(clients)
        jobs["coh_preferences"] = lambda: get_coh_preferences(clients)
    for service in services:
        jobs[f"ri_coverage:{service}"] = lambda s=service: get_ri_coverage(clients, posture_days, s)
        jobs[f"ri_utilization:{service}"] = lambda s=service: get_ri_utilization(clients, posture_days, s)

    collected = _gather(jobs)
    return {
        "sp_coverage": collected.get("sp_coverage", {}),
        "sp_utilization": collected.get("sp_utilization", {}),
        "ri_coverage": {s: collected[f"ri_coverage:{s}"] for s in services},
        "ri_utilization": {s: collected[f"ri_utilization:{s}"] for s in services},
        "coh_enrollment": collected.get("coh_enrollment", {}),
        "coh_preferences": collected.get("coh_preferences", {}),
        "eligible_spend": collected["eligible_spend"],
    }


def posture_from(collected: dict) -> dict:
    return assess_existing_posture(
        collected["sp_coverage"],
        collected["sp_utilization"],
        collected["ri_coverage"],
        collected["ri_utilization"],
    )


def collect_inventory(
    clients: Clients,
    regions: list[str],
    families: list[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Inventory every commitment that carries a date. Returns (items, errors).

    One job per (family, region) plus one un-multiplied Savings Plans job — the
    SP API is account-level, so sweeping it per region would return the same
    plans N times and quietly inflate every total.
    """
    families = [s.strip().lower() for s in (families or list(INVENTORY_KEYS)) if s.strip()]
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
    return [item for result in results for item in result.get("items", [])], errors


def inventory_ce_services(items: list[dict]) -> list[str]:
    """Cost Explorer services whose reservations appear in an inventory."""
    keys = {i.get("service") for i in items if i.get("family") == "reservation"}
    return [INVENTORY_SPECS[k].ce_service for k in INVENTORY_KEYS if k in keys]


def expiry_from(
    items: list[dict],
    regions: list[str],
    posture: dict,
    horizon_days: int,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Judge renewals against per-service utilization from `posture`."""
    by_ce_service = posture.get("ri_utilization_by_service", {})
    by_family = {
        key: by_ce_service.get(spec.ce_service)
        for key, spec in INVENTORY_SPECS.items()
        if spec.ce_service in by_ce_service
    }
    expiry = analyze_expiry(
        items,
        as_of or datetime.now(timezone.utc).date(),
        horizon_days,
        sp_utilization_pct=posture.get("sp_utilization_pct"),
        ri_utilization_by_service=by_family,
    )
    expiry["regions"] = list(regions)
    expiry["blind_spots"] = list(INVENTORY_BLIND_SPOTS)
    return expiry


def collect_expiry(
    clients: Clients,
    regions: list[str],
    services: list[str] | None = None,
    horizon_days: int = DEFAULT_EXPIRY_HORIZON_DAYS,
    *,
    posture_days: int = DEFAULT_POSTURE_DAYS,
    as_of: date | None = None,
) -> tuple[dict[str, Any], dict, list[dict]]:
    """Inventory, then measure utilization for only what was found.

    Returns (expiry, posture, errors). Utilization is queried per service for
    the reservation families the inventory actually returned — an account
    with no ElastiCache reservations pays nothing to learn ElastiCache's
    utilization.
    """
    items, errors = collect_inventory(clients, regions, services)
    has_sp = any(i.get("family") == "savings-plan" for i in items)
    collected = collect_posture(
        clients,
        posture_days,
        DEFAULT_SPEND_DAYS,
        inventory_ce_services(items),
        include_sp=has_sp,
        include_coh=False,
    )
    posture = posture_from(collected)
    return expiry_from(items, regions, posture, horizon_days, as_of), posture, errors


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


def counting(clients: Clients) -> tuple[Clients, CountingClient]:
    """Route Cost Explorer through a request counter. Returns (clients, counter)."""
    counter = CountingClient(clients.ce)
    return replace(clients, ce=counter), counter


def cost_of(counter: CountingClient) -> dict[str, Any]:
    return {
        "queries_run": counter.count,
        "estimated_api_cost_usd": round(counter.count * CE_PRICE_PER_REQUEST, 2),
    }


# ---------------------------------------------------------------------------
# Entrypoints
# ---------------------------------------------------------------------------


def size_commitments(
    clients: Clients,
    family: str,
    choices: list[str],
    terms: list[str],
    payments: list[str],
    lookback: str,
    account_scope: str,
    *,
    time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS,
) -> dict[str, Any]:
    """Size one family on its own (`sp` → types, `ri` → services).

    No posture gate and no reconciliation — the caller asked for sizing only.
    Alternatives are still marked within the family, so two SP types covering
    the same compute spend are not summed.
    """
    clients, counter = counting(clients)
    deadline = time.monotonic() + time_budget_seconds
    if family == "sp":
        recs, errors = sweep_savings_plans(
            clients, choices, terms, payments, lookback, account_scope, deadline
        )
        findings = findings_from(recs, [])
    else:
        recs, errors = sweep_reservations(
            clients, choices, terms, payments, lookback, account_scope, deadline
        )
        findings = findings_from([], recs)
    if not recs and errors:
        return {**diagnose_failure(errors), "collection_warnings": errors, **cost_of(counter)}
    planned, pools = plan_purchases(select_best_findings(findings))
    return {
        **envelope(planned, pools=pools),
        "collection_warnings": errors,
        **cost_of(counter),
    }


def posture_snapshot(
    clients: Clients,
    posture_days: int = DEFAULT_POSTURE_DAYS,
    spend_days: int = DEFAULT_SPEND_DAYS,
    ri_services: list[str] | None = None,
) -> dict[str, Any]:
    """Health of existing commitments, without sizing anything new."""
    clients, counter = counting(clients)
    collected = collect_posture(
        clients, posture_days, spend_days, ri_services or list(RI_SERVICES)
    )
    posture = posture_from(collected)
    spend = collected["eligible_spend"]
    periods = spend.get("periods", [])
    top = (
        dict(sorted(periods[-1]["by_service"].items(), key=lambda kv: -kv[1])[:10])
        if periods
        else {}
    )
    return {
        "posture": posture,
        "blockers": posture["blockers"],
        "safe_to_buy_more": safe_to_buy_more(posture),
        "unmeasured": posture["unmeasured"],
        "cost_optimization_hub": collected["coh_enrollment"],
        "spend_periods": [
            {"start": p["start"], "end": p["end"], "total": p["total"]} for p in periods
        ],
        "spend_error": spend.get("error"),
        "top_services_latest_period": top,
        "notes": posture.get("notes", []),
        **cost_of(counter),
    }


def expiry_snapshot(
    clients: Clients,
    regions: list[str],
    services: list[str] | None = None,
    horizon_days: int = DEFAULT_EXPIRY_HORIZON_DAYS,
    posture_days: int = DEFAULT_POSTURE_DAYS,
) -> dict[str, Any]:
    """What expires, and the per-service utilization each verdict rests on."""
    clients, counter = counting(clients)
    expiry, posture, errors = collect_expiry(
        clients, regions, services, horizon_days, posture_days=posture_days
    )
    return {
        **expiry,
        "renewal_actions": expiry["actions"],
        "utilization_unmeasured": posture["unmeasured"],
        "collection_warnings": errors,
        **cost_of(counter),
    }


def _ri_services_of(findings: list[Finding]) -> list[str]:
    by_label = {label: service for service, label in RI_SERVICE_LABELS.items()}
    return [
        by_label[f.label]
        for f in findings
        if f.family == "reserved-instance" and not f.do_not_buy and f.label in by_label
    ]


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
    time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS,
) -> dict[str, Any]:
    """Run the whole pipeline and return the payload `report.render` expects.

    Sweeps recommendations, inventories existing commitments (when `regions`
    is given), measures posture for the services that matter, reconciles
    against Cost Optimization Hub, and plans one purchase per usage pool.
    Every parameter is validated here, so a host can pass user input straight
    through and catch `ValueError`.

    The returned dict carries `meta`, `findings` (planned, with roles),
    `pools`, `posture`, `reconciliation`, `eligible_spend`, `expiry`,
    `errors`, `fatal` (set when every recommendation query failed — render
    nothing, report the error), `queries_run`, plus `raw`.

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

    clients, counter = counting(clients)
    deadline = time.monotonic() + time_budget_seconds
    sp_recs: list[dict] = []
    ri_recs: list[dict] = []
    errors: list[dict] = []

    if "sp" in families:
        sp_recs, errs = sweep_savings_plans(
            clients, resolve_sp_types(sp_types or ["all"]), terms, payments,
            lookback, account_scope, deadline,
        )
        errors += errs
    if "ri" in families:
        ri_recs, errs = sweep_reservations(
            clients, resolve_ri_services(ri_services or ["all"]), terms, payments,
            lookback, account_scope, deadline,
        )
        errors += errs
    sweep_errors = list(errors)

    if not (sp_recs or ri_recs) and sweep_errors:
        return {
            "meta": build_meta(clients, lookback, account_scope, profile),
            "fatal": diagnose_failure(sweep_errors),
            "errors": sweep_errors,
            "sweep_errors": sweep_errors,
            **cost_of(counter),
        }

    all_findings = findings_from(sp_recs, ri_recs)
    planned, pools = plan_purchases(select_best_findings(all_findings))

    items: list[dict] = []
    if regions:
        items, inventory_errors = collect_inventory(clients, regions, inventory_services)
        errors += inventory_errors

    posture_services = list(
        dict.fromkeys(_ri_services_of(planned) + inventory_ce_services(items))
    )
    collected = collect_posture(clients, posture_days, spend_days, posture_services)
    posture = posture_from(collected)
    coh = fetch_coh(clients, collected["coh_enrollment"])
    reconciliation = reconcile_with_coh(
        all_findings,
        coh,
        collected["coh_preferences"],
        ce_account_id=clients.account_id,
        coh_account_id=clients.coh_account_id or clients.account_id,
    )
    expiry = (
        expiry_from(items, regions, posture, expiry_horizon_days) if regions else None
    )
    return {
        "meta": build_meta(clients, lookback, account_scope, profile),
        "findings": planned,
        "pools": pools,
        "posture": posture,
        "safe_to_buy_more": safe_to_buy_more(posture),
        "reconciliation": reconciliation,
        "eligible_spend": collected["eligible_spend"],
        "expiry": expiry,
        "errors": errors,
        "sweep_errors": sweep_errors,
        **cost_of(counter),
        "raw": {"sp_recs": sp_recs, "ri_recs": ri_recs, "coh": coh, **collected},
    }
