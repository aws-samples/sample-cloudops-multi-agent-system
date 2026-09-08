"""Turn raw recommendation data into a measured, achievable commitment plan.

The AWS recommendation APIs return a best case: the commitment level that
maximizes savings assuming the lookback window repeats. This module adds the
parts a purchase decision actually needs —

  * whether spend is stable enough for the recommendation to hold (volatility)
  * a floor-based commitment the workload sustains even on its quietest hour
  * break-even and waste exposure if usage drops
  * reconciliation against Cost Optimization Hub's independent pipeline

Everything is a pure function over collected data; nothing calls AWS.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .api import HOURS_PER_MONTH, SP_TYPE_LABELS, describe_recommendation_spec

# Volatility bands for the ratio of trough to average hourly on-demand spend.
# A workload whose quietest hour is close to its average is safe to commit near
# the API recommendation; a spiky one is not.
STABLE_FLOOR_RATIO = 0.80
MODERATE_FLOOR_RATIO = 0.50

# Utilization below this on an existing commitment means money is already being
# wasted, and is a reason to hold off buying more.
UTILIZATION_WARN_PCT = 95.0

# Coverage above this means there is little on-demand left to convert; further
# purchase risks over-committing.
COVERAGE_SATURATED_PCT = 90.0

CONFIDENCE_HIGH = "High"
CONFIDENCE_MEDIUM = "Medium"
CONFIDENCE_LOW = "Low"

# Commitment terms in months, for checking whether an upfront payment can even
# pay back before the commitment expires.
TERM_MONTHS = {"ONE_YEAR": 12, "THREE_YEARS": 36}

# Expiry urgency bands, in days remaining. 30 days is roughly the shortest
# notice on which a renewal can clear finance approval, so it is the point at
# which a lapse becomes a scheduling problem rather than a planning one.
EXPIRY_URGENT_DAYS = 30
EXPIRY_SOON_DAYS = 60
EXPIRY_HORIZON_DAYS = 90

# Renewal verdicts. Deliberately less conservative than the initial-purchase
# bands above: a lapsing commitment has zero switching cost, so expiry is the
# one free resize point in a commitment's life. Renewing at the same size is
# the risk; renewing smaller is not.
RENEW = "renew"
RENEW_SMALLER = "renew-smaller"
LET_LAPSE = "let-lapse"
REVIEW = "review"

# Utilization below this means the commitment is more waste than saving, so
# re-buying it at the same size compounds a mistake rather than protecting a
# discount.
RENEW_LAPSE_PCT = 50.0


@dataclass
class LineItem:
    """One purchasable line of a recommendation: what to buy, and how much.

    A Finding aggregates every line item AWS returned for a service so the
    savings can be ranked against other services, but the aggregate is not
    purchasable — "4 RDS reservations" is not an order. Each line item carries
    its own specification (`db.r6g.xlarge · Multi-AZ · Aurora PostgreSQL`) and
    its own measured floor, which is what someone takes to the console.
    """

    spec: str
    unit: str
    recommended: float
    floor: float
    average: float
    achievable: float
    monthly_savings: float
    upfront_cost: float
    utilization_pct: float | None = None
    monthly_on_demand: float = 0.0
    size_flex_eligible: bool = False
    current_generation: bool = True
    region: str = ""
    account_id: str = ""


@dataclass
class Finding:
    """One actionable commitment opportunity, risk-adjusted."""

    family: str
    label: str
    term: str
    payment: str
    api_hourly_commitment: float
    safe_hourly_commitment: float
    api_monthly_savings: float
    safe_monthly_savings: float
    savings_percentage: float
    upfront_cost: float
    confidence: str
    volatility: str
    rationale: list[str] = field(default_factory=list)
    break_even_months: float | None = None
    waste_exposure_monthly: float = 0.0
    source: str = "cost-explorer"
    detail_count: int = 0
    line_items: list[LineItem] = field(default_factory=list)


def _f(value: Any, default: float = 0.0) -> float:
    """Coerce an API numeric-string to float.

    Cost Explorer returns money and percentages as STRINGS, and returns "" for
    absent values — float("") raises, so every read goes through here.
    """
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def classify_volatility(floor: float, average: float) -> tuple[str, float]:
    """Rate spend stability from the trough-to-average ratio.

    Returns (label, ratio). A ratio near 1.0 means a flat workload.
    """
    if average <= 0:
        return "unknown", 0.0
    ratio = floor / average
    if ratio >= STABLE_FLOOR_RATIO:
        return "stable", ratio
    if ratio >= MODERATE_FLOOR_RATIO:
        return "moderate", ratio
    return "spiky", ratio


def _safe_commitment(
    api_hourly: float,
    floor_hourly: float,
    avg_hourly: float,
    unit: str = "$/hr",
) -> tuple[float, str, str, list[str]]:
    """Derive a commitment level the workload sustains, plus confidence.

    The API optimizes for total savings and will happily recommend committing
    above the trough, which produces unused-commitment waste in quiet hours.
    For spiky workloads this clamps the commitment to the measured floor.

    `unit` selects both the wording and the granularity: Savings Plans commit in
    dollars per hour, reservations in whole instance or capacity units. Rounding
    happens here rather than in the caller so the figure quoted in the notes is
    the same one the report tabulates.
    """
    volatility, ratio = classify_volatility(floor_hourly, avg_hourly)
    notes: list[str] = []
    whole_units = unit != "$/hr"

    def quantize(value: float) -> float:
        # Reservations are sold in whole units; round down so the commitment
        # never lands above the level that was judged safe.
        return float(int(value)) if whole_units else value

    def fmt(value: float) -> str:
        return f"{value:,.0f} unit(s)" if whole_units else f"${value:,.4f}/hr"

    if api_hourly <= 0:
        return 0.0, CONFIDENCE_LOW, volatility, ["API recommended no commitment."]

    if volatility == "stable":
        safe = quantize(api_hourly)
        confidence = CONFIDENCE_HIGH
        notes.append(
            f"Trough hour is {ratio:.0%} of average — flat workload, "
            "API recommendation is safe to take as-is."
        )
    elif volatility == "moderate":
        # Commit to the floor plus half the gap to the API figure: captures
        # most of the savings while staying clear of the trough.
        safe = quantize(
            min(api_hourly, floor_hourly + (api_hourly - floor_hourly) * 0.5)
        )
        confidence = CONFIDENCE_MEDIUM
        notes.append(
            f"Trough hour is {ratio:.0%} of average — moderately variable. "
            f"Commitment trimmed to {fmt(safe)} (midpoint of floor and API "
            "recommendation) to limit unused-commitment risk."
        )
    else:
        # Never commit above what the quietest hour consumes.
        safe = quantize(min(api_hourly, floor_hourly))
        confidence = CONFIDENCE_LOW
        notes.append(
            f"Trough hour is only {ratio:.0%} of average — spiky workload. "
            f"Commitment clamped to the measured floor ({fmt(safe)}); "
            "committing to the API figure would strand spend in quiet hours."
        )

    if volatility == "unknown":
        notes.append(
            "No hourly on-demand spend data returned, so the floor could not be "
            "measured — treat the API figure as unvalidated."
        )
        confidence = CONFIDENCE_LOW

    return max(safe, 0.0), confidence, volatility, notes


def _check_break_even_against_term(
    break_even: float | None, term: str, notes: list[str], confidence: str
) -> str:
    """Flag an upfront purchase that cannot pay back within its own term.

    A commitment is non-cancellable and expires at the end of the term, so a
    break-even beyond the term means the purchase loses money outright — the
    single most expensive mistake this report exists to prevent. Returns the
    confidence to use, downgraded to Low when the purchase cannot pay back.
    """
    term_months = TERM_MONTHS.get(term)
    if break_even is None or term_months is None or break_even <= term_months:
        return confidence
    notes.insert(
        0,
        f"**Do not buy.** Break-even is {break_even:.1f} months but the term "
        f"ends at {term_months}. At the achievable commitment level this "
        "purchase never pays back — take the no-upfront option, a shorter "
        "term, or nothing.",
    )
    return CONFIDENCE_LOW


SPEC_UNAVAILABLE = "(specification not returned)"


def _ri_line_items(details: list[dict[str, Any]], scale: float) -> list[LineItem]:
    """Break an RI recommendation into the individual purchases it implies.

    `scale` is the family-level risk adjustment, applied per line and rounded
    **down** because reservations are sold whole. Rounding down can leave the
    line items summing slightly below the family total; the report says so
    rather than quietly padding a line.
    """
    items: list[LineItem] = []
    for d in details:
        # The instance fields come back empty for capacity-unit services
        # (DynamoDB), which report the same three measures under different
        # names. `or` picks up the fallback because a genuine 0 needs it too.
        recommended = _f(d.get("RecommendedNumberOfInstancesToPurchase")) or _f(
            d.get("RecommendedNumberOfCapacityUnitsToPurchase")
        )
        floor = _f(d.get("MinimumNumberOfInstancesUsedPerHour")) or _f(
            d.get("MinimumNumberOfCapacityUnitsUsedPerHour")
        )
        average = _f(d.get("AverageNumberOfInstancesUsedPerHour")) or _f(
            d.get("AverageNumberOfCapacityUnitsUsedPerHour")
        )
        spec = describe_recommendation_spec(d)
        utilization = _f(d.get("AverageUtilization"), -1.0)
        items.append(
            LineItem(
                spec=spec.get("label") or SPEC_UNAVAILABLE,
                unit="units",
                recommended=recommended,
                floor=floor,
                average=average,
                achievable=float(int(recommended * scale)),
                monthly_savings=_f(d.get("EstimatedMonthlySavingsAmount")),
                upfront_cost=_f(d.get("UpfrontCost")),
                utilization_pct=utilization if utilization >= 0 else None,
                monthly_on_demand=_f(d.get("EstimatedMonthlyOnDemandCost")),
                size_flex_eligible=bool(spec.get("size_flex_eligible")),
                current_generation=bool(spec.get("current_generation", True)),
                region=spec.get("region", ""),
                account_id=str(d.get("AccountId") or ""),
            )
        )
    return sorted(items, key=lambda i: -i.monthly_savings)


def _sp_line_items(details: list[dict[str, Any]], scale: float) -> list[LineItem]:
    """Break a Savings Plan recommendation into its per-family commitments.

    Only an EC2 Instance Savings Plan is scoped to a family and region, so for a
    Compute plan these fields come back empty — which is the plan being flexible
    by design, not data going missing.
    """
    items: list[LineItem] = []
    for d in details:
        sp = d.get("SavingsPlansDetails") or {}
        region = str(sp.get("Region") or "")
        family = str(sp.get("InstanceFamily") or "")
        hourly = _f(d.get("HourlyCommitmentToPurchase"))
        utilization = _f(d.get("EstimatedAverageUtilization"), -1.0)
        items.append(
            LineItem(
                spec=family or "any instance family",
                unit="USD/hour",
                recommended=hourly,
                floor=_f(d.get("CurrentMinimumHourlyOnDemandSpend")),
                average=_f(d.get("CurrentAverageHourlyOnDemandSpend")),
                achievable=hourly * scale,
                monthly_savings=_f(d.get("EstimatedMonthlySavingsAmount")),
                upfront_cost=_f(d.get("UpfrontCost")),
                utilization_pct=utilization if utilization >= 0 else None,
                monthly_on_demand=_f(d.get("EstimatedOnDemandCost")),
                region=region,
                account_id=str(d.get("AccountId") or ""),
            )
        )
    return sorted(items, key=lambda i: -i.monthly_savings)


def analyze_sp_recommendation(rec: dict[str, Any]) -> Finding | None:
    """Convert one SP recommendation permutation into a risk-adjusted Finding."""
    if rec.get("error"):
        return None
    summary = rec.get("summary") or {}
    details = rec.get("details") or []

    api_hourly = _f(summary.get("HourlyCommitmentToPurchase"))
    api_monthly = _f(summary.get("EstimatedMonthlySavingsAmount"))
    if api_hourly <= 0 and api_monthly <= 0:
        return None

    # Aggregate the per-detail hourly spend envelope. The summary omits it, so
    # the floor has to come from the details.
    floor = sum(_f(d.get("CurrentMinimumHourlyOnDemandSpend")) for d in details)
    average = sum(_f(d.get("CurrentAverageHourlyOnDemandSpend")) for d in details)
    if average <= 0:
        average = _f(summary.get("CurrentOnDemandSpend")) / HOURS_PER_MONTH

    safe_hourly, confidence, volatility, notes = _safe_commitment(
        api_hourly, floor, average
    )

    # Savings scale with the commitment, since the discount rate is fixed per
    # plan. Scaling down the commitment scales down the savings proportionally.
    scale = (safe_hourly / api_hourly) if api_hourly > 0 else 0.0
    safe_monthly = api_monthly * scale

    # Waste exposure: what an unused commitment costs per month if usage falls
    # to the trough while committed at the recommended level.
    waste = max(0.0, (api_hourly - floor)) * HOURS_PER_MONTH if floor > 0 else 0.0

    upfront = sum(_f(d.get("UpfrontCost")) for d in details)
    if upfront > 0 and safe_monthly > 0:
        break_even = upfront / safe_monthly
        notes.append(
            f"${upfront:,.2f} upfront pays back in {break_even:.1f} months at the "
            "adjusted savings rate."
        )
    else:
        break_even = None

    confidence = _check_break_even_against_term(
        break_even, rec["term"], notes, confidence
    )

    est_util = [_f(d.get("EstimatedAverageUtilization")) for d in details]
    est_util = [u for u in est_util if u > 0]
    if est_util:
        mean_util = sum(est_util) / len(est_util)
        notes.append(
            f"AWS projects {mean_util:.1f}% average utilization on the "
            "recommended commitment."
        )

    return Finding(
        family="savings-plan",
        label=SP_TYPE_LABELS.get(rec["sp_type"], rec["sp_type"]),
        term=rec["term"],
        payment=rec["payment"],
        api_hourly_commitment=api_hourly,
        safe_hourly_commitment=safe_hourly,
        api_monthly_savings=api_monthly,
        safe_monthly_savings=safe_monthly,
        savings_percentage=_f(summary.get("EstimatedSavingsPercentage")),
        upfront_cost=upfront,
        confidence=confidence,
        volatility=volatility,
        rationale=notes,
        break_even_months=break_even,
        waste_exposure_monthly=waste,
        detail_count=len(details),
        line_items=_sp_line_items(details, scale),
    )


def analyze_ri_recommendation(rec: dict[str, Any]) -> Finding | None:
    """Convert one RI recommendation permutation into a risk-adjusted Finding.

    RIs commit to instance counts rather than dollars per hour, so the floor is
    measured in instances: MinimumNumberOfInstancesUsedPerHour is the count the
    workload never drops below.
    """
    if rec.get("error"):
        return None
    summary = rec.get("summary") or {}
    details = rec.get("details") or []

    api_monthly = _f(summary.get("TotalEstimatedMonthlySavingsAmount"))
    if api_monthly <= 0 and not details:
        return None

    recommended_units = sum(
        _f(d.get("RecommendedNumberOfInstancesToPurchase")) for d in details
    )
    floor_units = sum(
        _f(d.get("MinimumNumberOfInstancesUsedPerHour")) for d in details
    )
    avg_units = sum(
        _f(d.get("AverageNumberOfInstancesUsedPerHour")) for d in details
    )

    # DynamoDB and other capacity-unit services report capacity units instead
    # of instance counts.
    if recommended_units <= 0:
        recommended_units = sum(
            _f(d.get("RecommendedNumberOfCapacityUnitsToPurchase")) for d in details
        )
        floor_units = sum(
            _f(d.get("MinimumNumberOfCapacityUnitsUsedPerHour")) for d in details
        )
        avg_units = sum(
            _f(d.get("AverageNumberOfCapacityUnitsUsedPerHour")) for d in details
        )

    safe_units, confidence, volatility, notes = _safe_commitment(
        recommended_units, floor_units, avg_units, unit="units"
    )

    scale = (safe_units / recommended_units) if recommended_units > 0 else 0.0
    safe_monthly = api_monthly * scale

    upfront = sum(_f(d.get("UpfrontCost")) for d in details)
    monthly_recurring = sum(
        _f(d.get("RecurringStandardMonthlyCost")) for d in details
    )

    # AWS computes break-even per detail; average it rather than recomputing,
    # so the figure ties out to the console.
    be = [_f(d.get("EstimatedBreakEvenInMonths")) for d in details]
    be = [b for b in be if b > 0]
    break_even = (sum(be) / len(be)) if be else None

    line_items = _ri_line_items(details, scale)
    if recommended_units > 0:
        notes.insert(
            0,
            f"API recommends {recommended_units:.0f} unit(s); workload floor is "
            f"{floor_units:.0f}, so {safe_units:.0f} is defensible.",
        )
    # A single-line recommendation needs no allocation guidance; a multi-line one
    # does, because the aggregate above spans several instance specifications and
    # a reservation can only be bought against one of them.
    if len(line_items) > 1:
        notes.append(
            f"This total spans {len(line_items)} distinct instance "
            "specifications — see the line items for what to buy against each. "
            "Reservations only apply to usage matching their exact "
            "specification, so the aggregate is a budget, not an order."
        )
    confidence = _check_break_even_against_term(
        break_even, rec["term"], notes, confidence
    )
    waste = max(0.0, recommended_units - floor_units)
    waste_cost = (
        (monthly_recurring / recommended_units * waste)
        if recommended_units > 0
        else 0.0
    )

    util = [_f(d.get("AverageUtilization")) for d in details]
    util = [u for u in util if u > 0]
    if util:
        notes.append(
            f"Observed average utilization across recommended families: "
            f"{sum(util) / len(util):.1f}%."
        )

    return Finding(
        family="reserved-instance",
        label=rec.get("label", rec.get("service", "")),
        term=rec["term"],
        payment=rec["payment"],
        api_hourly_commitment=recommended_units,
        safe_hourly_commitment=safe_units,
        api_monthly_savings=api_monthly,
        safe_monthly_savings=safe_monthly,
        savings_percentage=_f(summary.get("TotalEstimatedMonthlySavingsPercentage")),
        upfront_cost=upfront,
        confidence=confidence,
        volatility=volatility,
        rationale=notes,
        break_even_months=break_even,
        waste_exposure_monthly=waste_cost,
        detail_count=len(details),
        line_items=line_items,
    )


def assess_existing_posture(
    sp_coverage: dict[str, Any],
    sp_utilization: dict[str, Any],
    ri_coverage: dict[str, Any],
    ri_utilization: dict[str, Any],
) -> dict[str, Any]:
    """Summarize whether existing commitments are healthy enough to add more.

    Buying on top of an under-utilized commitment compounds waste, so this
    produces explicit blockers the report surfaces before any recommendation.
    """
    posture: dict[str, Any] = {"blockers": [], "notes": []}

    # --- Savings Plans coverage -------------------------------------------
    periods = sp_coverage.get("periods") or []
    if periods:
        latest = periods[-1].get("Coverage", {})
        pct = _f(latest.get("CoveragePercentage"))
        posture["sp_coverage_pct"] = pct
        posture["sp_on_demand_cost"] = _f(latest.get("OnDemandCost"))
        if pct >= COVERAGE_SATURATED_PCT:
            posture["blockers"].append(
                f"Savings Plans coverage is already {pct:.1f}% — little "
                "on-demand spend left to convert. Verify headroom before buying."
            )
    elif sp_coverage.get("error"):
        posture["notes"].append(f"SP coverage unavailable: {sp_coverage['error']}")

    # --- Savings Plans utilization ----------------------------------------
    sp_total = sp_utilization.get("total") or {}
    if sp_total:
        util = sp_total.get("Utilization", {})
        pct = _f(util.get("UtilizationPercentage"))
        posture["sp_utilization_pct"] = pct
        posture["sp_unused_commitment"] = _f(util.get("UnusedCommitment"))
        if pct and pct < UTILIZATION_WARN_PCT:
            posture["blockers"].append(
                f"Existing Savings Plans are only {pct:.1f}% utilized "
                f"(${_f(util.get('UnusedCommitment')):,.2f} unused). Fix this "
                "before adding commitment."
            )
    elif sp_utilization.get("error"):
        posture["notes"].append(
            f"SP utilization unavailable: {sp_utilization['error']}"
        )

    # --- Reservations ------------------------------------------------------
    ri_total = ri_coverage.get("total") or {}
    if ri_total:
        hours = ri_total.get("CoverageHours", {})
        pct = _f(hours.get("CoverageHoursPercentage"))
        posture["ri_coverage_pct"] = pct
        posture["ri_on_demand_hours"] = _f(hours.get("OnDemandHours"))
    elif ri_coverage.get("error"):
        posture["notes"].append(f"RI coverage unavailable: {ri_coverage['error']}")

    ri_util_total = ri_utilization.get("total") or {}
    if ri_util_total:
        pct = _f(ri_util_total.get("UtilizationPercentage"))
        posture["ri_utilization_pct"] = pct
        posture["ri_unused_hours"] = _f(ri_util_total.get("UnusedHours"))
        posture["ri_realized_savings"] = _f(ri_util_total.get("RealizedSavings"))
        if pct and pct < UTILIZATION_WARN_PCT:
            posture["blockers"].append(
                f"Existing reservations are only {pct:.1f}% utilized "
                f"({_f(ri_util_total.get('UnusedHours')):,.0f} unused hours). "
                "Reconcile before buying more."
            )
    elif ri_utilization.get("error"):
        posture["notes"].append(
            f"RI utilization unavailable: {ri_utilization['error']}"
        )

    return posture


def reconcile_with_coh(
    findings: list[Finding], coh: dict[str, Any]
) -> dict[str, Any]:
    """Compare Cost Explorer totals against Cost Optimization Hub's.

    The two run independent pipelines over the same billing data, so a material
    gap means one of them is looking at a different lookback or scope. Surfacing
    the delta is what makes the report defensible rather than just plausible.
    """
    if coh.get("error"):
        return {"status": "unavailable", "reason": coh["error"]}

    coh_recs = coh.get("recommendations", [])
    coh_total = round(sum(r["estimated_monthly_savings"] for r in coh_recs), 2)
    # Compare against the API's own figures, not the risk-adjusted ones — COH
    # publishes an unadjusted best case too, so that is the like-for-like axis.
    ce_total = round(sum(f.api_monthly_savings for f in findings), 2)

    delta = round(ce_total - coh_total, 2)
    larger = max(abs(ce_total), abs(coh_total))
    delta_pct = (abs(delta) / larger * 100) if larger > 0 else 0.0

    if coh_total == 0 and ce_total == 0:
        status = "agree-zero"
    elif delta_pct <= 10:
        status = "reconciled"
    elif delta_pct <= 30:
        status = "minor-variance"
    else:
        status = "material-variance"

    by_type: dict[str, float] = {}
    for r in coh_recs:
        key = r["recommended_resource_type"] or r["current_resource_type"]
        by_type[key] = round(
            by_type.get(key, 0.0) + r["estimated_monthly_savings"], 2
        )

    return {
        "status": status,
        "ce_monthly_savings": ce_total,
        "coh_monthly_savings": coh_total,
        "delta": delta,
        "delta_pct": round(delta_pct, 1),
        "coh_count": len(coh_recs),
        "coh_by_resource_type": dict(
            sorted(by_type.items(), key=lambda kv: -kv[1])
        ),
    }


def select_best_findings(findings: list[Finding]) -> list[Finding]:
    """Pick the strongest permutation per commitment family.

    Every (term, payment) combination is fetched, but a report that lists all of
    them buries the decision. Rank by risk-adjusted savings, breaking ties
    toward the shorter term and less upfront cash — the lower-risk purchase.
    """
    TERM_RANK = {"ONE_YEAR": 0, "THREE_YEARS": 1}
    PAYMENT_RANK = {"NO_UPFRONT": 0, "PARTIAL_UPFRONT": 1, "ALL_UPFRONT": 2}

    best: dict[str, Finding] = {}
    for f in findings:
        key = f"{f.family}:{f.label}"
        current = best.get(key)
        if current is None:
            best[key] = f
            continue
        if (
            -f.safe_monthly_savings,
            TERM_RANK.get(f.term, 9),
            PAYMENT_RANK.get(f.payment, 9),
        ) < (
            -current.safe_monthly_savings,
            TERM_RANK.get(current.term, 9),
            PAYMENT_RANK.get(current.payment, 9),
        ):
            best[key] = f

    return sorted(best.values(), key=lambda f: -f.safe_monthly_savings)


# ---------------------------------------------------------------------------
# Expiry and renewal
# ---------------------------------------------------------------------------


def _urgency(days_remaining: int) -> str:
    if days_remaining <= EXPIRY_URGENT_DAYS:
        return "urgent"
    if days_remaining <= EXPIRY_SOON_DAYS:
        return "soon"
    return "upcoming"


def _renewal_verdict(
    item: dict[str, Any], utilization_pct: float | None
) -> tuple[str, str]:
    """Decide what to do with one lapsing commitment, and say why.

    Utilization is the whole basis of the call: a commitment running at 99% is
    load-bearing and lapsing it raises the bill, while one running at 30% is
    already waste that renewal would lock in for another term. When
    utilization could not be measured the honest answer is "review", not a
    guess — the figure is what makes this decision defensible.
    """
    label = item.get("label") or item.get("service") or "commitment"
    if utilization_pct is None:
        return (
            REVIEW,
            f"Utilization for {label} could not be measured, so renewal size "
            "cannot be justified from data. Check the console before the term "
            "ends.",
        )
    if utilization_pct >= UTILIZATION_WARN_PCT:
        return (
            RENEW,
            f"{label} is running at {utilization_pct:.1f}% utilization — fully "
            "consumed. Letting it lapse moves this usage back to on-demand "
            "rates.",
        )
    if utilization_pct >= RENEW_LAPSE_PCT:
        return (
            RENEW_SMALLER,
            f"{label} is at {utilization_pct:.1f}% utilization, so part of the "
            "commitment is unused. Expiry is a zero-cost resize point: re-buy "
            "at roughly the utilized share, not the current size.",
        )
    return (
        LET_LAPSE,
        f"{label} is only at {utilization_pct:.1f}% utilization — more waste "
        "than saving. Let it lapse and re-buy only what fresh sizing supports.",
    )


def analyze_expiry(
    items: list[dict[str, Any]],
    as_of: date,
    horizon_days: int = EXPIRY_HORIZON_DAYS,
    sp_utilization_pct: float | None = None,
    ri_utilization_pct: float | None = None,
) -> dict[str, Any]:
    """Sort a commitment inventory into what expires when, and what to do.

    `as_of` is a parameter rather than `date.today()` so this stays a pure
    function that a test can pin to a fixed day.

    Utilization arrives per family from `assess_existing_posture` because Cost
    Explorer only reports it in aggregate — there is no per-commitment
    utilization API, so every Savings Plan in the account shares the account's
    SP utilization figure. That is a real limitation of the data and is stated
    in the report rather than papered over.

    Anything already past its end date but still listed as active is reported
    separately: the commitment is gone and the spend it covered is already back
    at on-demand rates, which is a different (and more urgent) conversation
    than a renewal.
    """
    expiring: list[dict[str, Any]] = []
    expired: list[dict[str, Any]] = []
    undated: list[dict[str, Any]] = []

    for item in items:
        end_raw = item.get("end") or ""
        try:
            end = date.fromisoformat(end_raw)
        except ValueError:
            undated.append(dict(item))
            continue

        days_remaining = (end - as_of).days
        utilization = (
            sp_utilization_pct
            if item.get("family") == "savings-plan"
            else ri_utilization_pct
        )
        action, rationale = _renewal_verdict(item, utilization)
        entry = {
            **item,
            "days_remaining": days_remaining,
            "utilization_pct": utilization,
            "action": action,
            "rationale": rationale,
        }
        if days_remaining < 0:
            entry["urgency"] = "expired"
            entry["rationale"] = (
                f"Already ended {abs(days_remaining)} days ago on {end_raw} but "
                "still listed as active. Confirm whether the covered usage is "
                "now billing at on-demand rates."
            )
            expired.append(entry)
        elif days_remaining <= horizon_days:
            entry["urgency"] = _urgency(days_remaining)
            expiring.append(entry)

    expiring.sort(key=lambda e: (e["days_remaining"], e.get("label", "")))
    expired.sort(key=lambda e: e["days_remaining"])

    counts = {band: 0 for band in ("urgent", "soon", "upcoming")}
    actions = {verdict: 0 for verdict in (RENEW, RENEW_SMALLER, LET_LAPSE, REVIEW)}
    hourly_commitment = 0.0
    reserved_units = 0.0
    for entry in expiring:
        counts[entry["urgency"]] = counts.get(entry["urgency"], 0) + 1
        actions[entry["action"]] = actions.get(entry["action"], 0) + 1
        if entry.get("family") == "savings-plan":
            hourly_commitment += entry.get("quantity", 0.0)
        else:
            reserved_units += entry.get("quantity", 0.0)

    return {
        "as_of": as_of.isoformat(),
        "horizon_days": horizon_days,
        "total_active": len(items),
        "expiring": expiring,
        "expired": expired,
        "undated": undated,
        "counts": counts,
        "actions": actions,
        # Savings Plan commitment is USD/hour, so it converts to money. RI
        # quantities are unit counts and deliberately are NOT converted —
        # turning units into dollars needs pricing data this module does not
        # have, and inventing a rate would be a fabricated figure.
        "hourly_commitment_expiring": round(hourly_commitment, 4),
        "monthly_committed_spend_expiring": round(
            hourly_commitment * HOURS_PER_MONTH, 2
        ),
        "reserved_units_expiring": round(reserved_units, 2),
    }
