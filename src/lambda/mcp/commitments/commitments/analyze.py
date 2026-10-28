"""Turn raw recommendation data into a measured, achievable commitment plan.

The AWS recommendation APIs return a best case: the commitment level that
maximizes savings assuming the lookback window repeats. This module adds the
parts a purchase decision actually needs —

  * whether spend is stable enough for the recommendation to hold (volatility)
  * a floor-based commitment the workload sustains even on its quietest hour
  * cash break-even and waste exposure if usage drops

`pools` decides which findings are alternatives for the same usage, and
`posture` checks existing commitments and reconciles against Cost Optimization
Hub.

Sizing is done per line item — one purchasable specification, or one account
under LINKED scope — because each line is a separate purchase with its own
floor. A family total is the sum of its lines, never a ratio applied across
them.

Everything is a pure function over collected data; nothing calls AWS.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .api import (
    HOURS_PER_MONTH,
    LOOKBACK_HOURS,
    RI_SERVICE_LABELS,
    SP_TYPE_LABELS,
    describe_recommendation_spec,
)
from .pools import RI_POOLS, ROLE_RECOMMENDED, SP_POOLS, ri_option

# Volatility bands for the ratio of trough to average hourly usage. A workload
# whose quietest hour is close to its average is safe to commit near the API
# recommendation; a spiky one is not.
STABLE_FLOOR_RATIO = 0.80
MODERATE_FLOOR_RATIO = 0.50

CONFIDENCE_HIGH = "High"
CONFIDENCE_MEDIUM = "Medium"
CONFIDENCE_LOW = "Low"
_CONFIDENCE_SCORE = {CONFIDENCE_HIGH: 2, CONFIDENCE_MEDIUM: 1, CONFIDENCE_LOW: 0}

# Commitment terms in months, for checking whether an upfront payment can even
# pay back before the commitment expires.
TERM_MONTHS = {"ONE_YEAR": 12, "THREE_YEARS": 36}

UNIT_SP = "USD/hour"
UNIT_RI = "units"

SPEC_UNAVAILABLE = "(specification not returned)"

@dataclass(frozen=True)
class LineItem:
    """One purchasable line of a recommendation: what to buy, and how much.

    A Finding aggregates every line item AWS returned for a service so the
    savings can be ranked against other services, but the aggregate is not
    purchasable — "4 RDS reservations" is not an order. Each line item carries
    its own specification (`db.r6g.xlarge · Multi-AZ · Aurora PostgreSQL`), its
    own measured floor and its own achievable size, which is what someone takes
    to the console.

    `floor` and `average` are in the line's commitment unit: reservation units,
    or — for a Savings Plan — Savings Plan dollars per hour, already converted
    from the on-demand dollars AWS reports (`floor_on_demand`). `None` means
    AWS did not return the figure, which is not the same as a floor of zero.
    """

    spec: str
    unit: str
    recommended: float
    floor: float | None
    average: float | None
    achievable: float
    aws_monthly_savings: float
    monthly_savings: float
    aws_upfront_cost: float
    upfront_cost: float
    confidence: str
    volatility: str
    break_even_months: float | None = None
    do_not_buy: bool = False
    waste_exposure_monthly: float = 0.0
    aws_waste_exposure_monthly: float = 0.0
    utilization_pct: float | None = None
    monthly_on_demand: float = 0.0
    floor_on_demand: float | None = None
    size_flex_eligible: bool = False
    current_generation: bool = True
    region: str = ""
    account_id: str = ""


@dataclass(frozen=True)
class Finding:
    """One commitment opportunity, risk-adjusted.

    `api_*` fields are the AWS best case. `safe_*`, `upfront_cost` and
    `waste_exposure_monthly` are at the achievable size this report
    recommends; `aws_upfront_cost` and `aws_waste_exposure_monthly` are the
    same measures at the AWS size, kept so the two can be compared.
    """

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
    aws_upfront_cost: float = 0.0
    aws_waste_exposure_monthly: float = 0.0
    do_not_buy: bool = False
    pool: str = ""
    option: str = ""
    role: str = ROLE_RECOMMENDED
    currency: str = "USD"


# ---------------------------------------------------------------------------
# Numeric helpers
# ---------------------------------------------------------------------------


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


def _opt(value: Any) -> float | None:
    """Like `_f`, but keeps "not returned" distinct from a measured zero.

    A blank trough is an unknown trough. Reading it as 0 would classify the
    workload as spiky and clamp the commitment to nothing — a confident answer
    built on missing data.
    """
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _currency(summary: dict[str, Any], details: list[dict[str, Any]]) -> str:
    code = summary.get("CurrencyCode") or next(
        (d.get("CurrencyCode") for d in details if d.get("CurrencyCode")), ""
    )
    return str(code or "USD")


def classify_volatility(floor: float | None, average: float | None) -> tuple[str, float]:
    """Rate spend stability from the trough-to-average ratio.

    Returns (label, ratio). A ratio near 1.0 means a flat workload. An
    unmeasured floor or average is "unknown", not spiky.
    """
    if floor is None or average is None or average <= 0:
        return "unknown", 0.0
    ratio = floor / average
    if ratio >= STABLE_FLOOR_RATIO:
        return "stable", ratio
    if ratio >= MODERATE_FLOOR_RATIO:
        return "moderate", ratio
    return "spiky", ratio


def _safe_commitment(
    api: float,
    floor: float | None,
    average: float | None,
    unit: str = UNIT_SP,
) -> tuple[float, str, str, list[str]]:
    """Derive a commitment level the workload sustains, plus confidence.

    The API optimizes for total savings and will happily recommend committing
    above the trough, which produces unused-commitment waste in quiet hours.
    For spiky workloads this clamps the commitment to the measured floor.

    `floor` and `average` must already be in the commitment's own unit —
    reservation units, or Savings Plan dollars (see `_sp_absorption_ratio`).

    When the floor was not measured the AWS figure is kept, at Low confidence:
    there is no evidence to trim it with, and inventing a trim is no better
    than inventing a floor.
    """
    volatility, ratio = classify_volatility(floor, average)
    whole_units = unit != UNIT_SP

    def quantize(value: float) -> float:
        # Reservations are sold in whole units; round down so the commitment
        # never lands above the level that was judged safe.
        return float(int(value)) if whole_units else value

    def fmt(value: float) -> str:
        return f"{value:,.0f} unit(s)" if whole_units else f"${value:,.4f}/hr"

    if api <= 0:
        return 0.0, CONFIDENCE_LOW, volatility, ["API recommended no commitment."]

    if volatility == "unknown":
        return (
            quantize(api),
            CONFIDENCE_LOW,
            volatility,
            [
                "AWS did not return the hourly usage floor, so it could not be "
                "measured — the AWS figure is kept but unvalidated."
            ],
        )

    if volatility == "stable":
        safe = quantize(api)
        return safe, CONFIDENCE_HIGH, volatility, [
            f"Trough hour is {ratio:.0%} of average — flat workload, "
            "API recommendation is safe to take as-is."
        ]

    if volatility == "moderate":
        # Commit to the floor plus half the gap to the API figure: captures
        # most of the savings while staying clear of the trough.
        safe = quantize(min(api, floor + (api - floor) * 0.5))
        return safe, CONFIDENCE_MEDIUM, volatility, [
            f"Trough hour is {ratio:.0%} of average — moderately variable. "
            f"Commitment trimmed to {fmt(safe)} (midpoint of floor and API "
            "recommendation) to limit unused-commitment risk."
        ]

    # Never commit above what the quietest hour consumes.
    safe = quantize(min(api, floor))
    return max(safe, 0.0), CONFIDENCE_LOW, volatility, [
        f"Trough hour is only {ratio:.0%} of average — spiky workload. "
        f"Commitment clamped to the measured floor ({fmt(safe)}); "
        "committing to the API figure would strand spend in quiet hours."
    ]


def _cash_break_even(upfront: float, net_monthly: float, term_months: int) -> float | None:
    """Months until an upfront payment is recovered in cash.

    `net_monthly` is AWS's `EstimatedMonthlySavingsAmount`, which is savings
    NET of the amortized upfront. The cash actually saved each month is that
    plus the amortized upfront (the recurring fee is lower by exactly that
    much), so dividing the upfront by the net figure alone overstates
    break-even by a factor of 1/d and fails every All-Upfront plan with a
    discount below 50%.

        cash_monthly = net_monthly + upfront / term_months
        break_even   = upfront / cash_monthly

    Returns None with no upfront (nothing to recover) and `inf` when the
    purchase never pays back.
    """
    if upfront <= 0:
        return None
    cash_monthly = net_monthly + upfront / term_months
    if cash_monthly <= 0:
        return math.inf
    return upfront / cash_monthly


def _blend_confidence(lines: list[LineItem]) -> str:
    """Family confidence, weighted by how much AWS savings each line carries.

    The weakest line would let one small spiky line drag a large stable
    portfolio to Low; a plain majority would hide a large spiky one. Weighting
    by savings keeps the label about the money.
    """
    viable = [line for line in lines if not line.do_not_buy and line.aws_monthly_savings > 0]
    if not viable:
        return CONFIDENCE_LOW
    weight = sum(line.aws_monthly_savings for line in viable)
    score = sum(_CONFIDENCE_SCORE[line.confidence] * line.aws_monthly_savings for line in viable) / weight
    if score >= 1.5:
        return CONFIDENCE_HIGH
    if score >= 0.5:
        return CONFIDENCE_MEDIUM
    return CONFIDENCE_LOW


def _weighted_break_even(lines: list[LineItem]) -> float | None:
    """Upfront-weighted break-even across the lines actually bought.

    An unweighted mean lets a $50 line with a 2-month break-even pull a
    $50,000 line's 11 months down to 6.5. Lines excluded as do-not-buy are not
    part of the purchase, so they must not pull the figure up either; the
    weight is each line's upfront at the achievable size for the same reason.
    """
    weighted = [
        (line.break_even_months, line.upfront_cost)
        for line in lines
        if not line.do_not_buy
        and line.break_even_months is not None
        and math.isfinite(line.break_even_months)
        and line.upfront_cost > 0
    ]
    total = sum(w for _, w in weighted)
    if total <= 0:
        return None
    return sum(be * w for be, w in weighted) / total


def _allocate(values: list[float | None], total: float, weights: list[float]) -> list[float]:
    """Fill per-line figures AWS left blank by allocating the summary total.

    Used only when every line is blank (`None`) and the summary is not — so a
    summary figure is never lost, never double-counted on top of real line
    data, and a genuinely negative line (a purchase that loses money) is never
    mistaken for a missing one.
    """
    if any(v is not None for v in values) or total <= 0:
        return [v or 0.0 for v in values]
    weight = sum(weights)
    if weight <= 0:
        return [total / len(values)] * len(values) if values else []
    return [total * w / weight for w in weights]


# ---------------------------------------------------------------------------
# Line sizing
# ---------------------------------------------------------------------------


def _size_line(
    *,
    spec: str,
    unit: str,
    recommended: float,
    floor: float | None,
    average: float | None,
    aws_savings: float,
    aws_upfront: float,
    term: str,
    unit_monthly_cost: float,
    aws_break_even: float | None = None,
    **extra: Any,
) -> tuple[LineItem, list[str]]:
    """Size one purchasable line and price its risk.

    `unit_monthly_cost` is what one unit of commitment costs per month — 730
    for a Savings Plan dollar-per-hour, (amortized upfront + recurring) per
    reservation — so waste is priced in the same unit the floor is measured in.

    `aws_break_even` is AWS's own per-line figure (RIs). When absent or zero
    the cash break-even is computed instead (always, for Savings Plans). Either way it is independent of
    the line's size, and a line that cannot pay back within its term is zeroed
    rather than recommended.
    """
    achievable, confidence, volatility, notes = _safe_commitment(
        recommended, floor, average, unit
    )
    term_months = TERM_MONTHS.get(term, 12)
    if aws_break_even is not None and aws_break_even > 0 and aws_upfront > 0:
        break_even = aws_break_even
    else:
        break_even = _cash_break_even(aws_upfront, aws_savings, term_months)
    do_not_buy = break_even is not None and break_even > term_months
    if do_not_buy:
        achievable, confidence = 0.0, CONFIDENCE_LOW

    scale = achievable / recommended if recommended > 0 else 0.0
    waste = aws_waste = 0.0
    if floor is not None:
        waste = max(0.0, achievable - floor) * unit_monthly_cost
        aws_waste = max(0.0, recommended - floor) * unit_monthly_cost

    line = LineItem(
        spec=spec,
        unit=unit,
        recommended=recommended,
        floor=floor,
        average=average,
        achievable=achievable,
        aws_monthly_savings=aws_savings,
        monthly_savings=aws_savings * scale,
        aws_upfront_cost=aws_upfront,
        upfront_cost=aws_upfront * scale,
        confidence=confidence,
        volatility=volatility,
        break_even_months=break_even,
        do_not_buy=do_not_buy,
        waste_exposure_monthly=waste,
        aws_waste_exposure_monthly=aws_waste,
        **extra,
    )
    return line, notes


def _line_notes(lines: list[LineItem], notes_by_line: list[list[str]], unit: str) -> list[str]:
    """Explain the sizing in as few bullets as the line count allows.

    One line gets its own trim/clamp sentence. Many lines get a tally, because
    a hundred near-identical sentences bury the decision.
    """
    if len(lines) == 1:
        return list(notes_by_line[0])
    bands: dict[str, int] = {}
    for line in lines:
        bands[line.volatility] = bands.get(line.volatility, 0) + 1
    order = ("stable", "moderate", "spiky", "unknown")
    tally = ", ".join(f"{bands[b]} {b}" for b in order if bands.get(b))
    recommended = sum(line.recommended for line in lines)
    achievable = sum(line.achievable for line in lines)
    fmt = (lambda v: f"${v:,.4f}/hr") if unit == UNIT_SP else (lambda v: f"{v:,.0f} unit(s)")
    return [
        f"Sized line by line against each line's own floor ({tally}): AWS "
        f"recommends {fmt(recommended)} in total and the per-line floors "
        f"support {fmt(achievable)}."
    ]


def _do_not_buy_note(lines: list[LineItem], term: str) -> list[str]:
    rejected = [line for line in lines if line.do_not_buy]
    if not rejected:
        return []
    term_months = TERM_MONTHS.get(term, 12)
    if len(rejected) == len(lines):
        worst = max(rejected, key=lambda line: line.break_even_months or 0)
        months = worst.break_even_months
        shown = "never" if months is None or not math.isfinite(months) else f"in {months:.1f} months"
        return [
            f"**Do not buy.** The upfront payment pays back {shown}, but the "
            f"term ends at {term_months} months — this purchase loses money. "
            "Take the no-upfront option, a shorter term, or nothing."
        ]
    return [
        f"{len(rejected)} line item(s) cannot pay back within the "
        f"{term_months}-month term and are excluded from the achievable "
        "figures (achievable 0)."
    ]


# ---------------------------------------------------------------------------
# Savings Plans
# ---------------------------------------------------------------------------


def _sp_absorption_ratio(detail: dict[str, Any]) -> float | None:
    """Savings Plan dollars absorbed per on-demand dollar, for one detail.

    `HourlyCommitmentToPurchase` is priced at Savings Plan rates while
    `CurrentMinimumHourlyOnDemandSpend` is in on-demand dollars. A commitment of
    $c/hr is only fully used once on-demand usage reaches c / (1 − d), so the
    floor has to be converted before it can cap the commitment:

        r = EstimatedSPCost × (util / 100) ÷ (EstimatedSPCost + EstimatedSavingsAmount)

    which is (1 − effective discount): the denominator is the on-demand cost of
    the usage the plan covers, and the numerator the commitment actually used.
    """
    sp_cost = _opt(detail.get("EstimatedSPCost"))
    savings = _opt(detail.get("EstimatedSavingsAmount"))
    util = _opt(detail.get("EstimatedAverageUtilization"))
    if sp_cost is None or savings is None or util is None:
        return None
    covered = sp_cost + savings
    if sp_cost <= 0 or covered <= 0 or util <= 0:
        return None
    ratio = sp_cost * (util / 100.0) / covered
    return ratio if 0 < ratio <= 1 else None


def _sp_lines(rec: dict[str, Any]) -> tuple[list[LineItem], list[list[str]], float | None]:
    """Size every Savings Plan detail. Returns (lines, notes, blended ratio)."""
    summary = rec.get("summary") or {}
    details = rec.get("details") or []
    term = rec["term"]
    if not details:
        details = [
            {
                "HourlyCommitmentToPurchase": summary.get("HourlyCommitmentToPurchase"),
                "EstimatedMonthlySavingsAmount": summary.get("EstimatedMonthlySavingsAmount"),
            }
        ]

    ratios = [_sp_absorption_ratio(d) for d in details]
    weights = [_f(d.get("EstimatedSPCost")) for d in details]
    known = [(r, w) for r, w in zip(ratios, weights) if r is not None]
    fallback = (
        sum(r * w for r, w in known) / sum(w for _, w in known)
        if known and sum(w for _, w in known) > 0
        else (known[0][0] if known else None)
    )

    commitments = _allocate(
        [_opt(d.get("HourlyCommitmentToPurchase")) for d in details],
        _f(summary.get("HourlyCommitmentToPurchase")),
        [1.0] * len(details),
    )
    savings = _allocate(
        [_opt(d.get("EstimatedMonthlySavingsAmount")) for d in details],
        _f(summary.get("EstimatedMonthlySavingsAmount")),
        commitments,
    )
    lookback_hours = LOOKBACK_HOURS.get(rec.get("lookback", ""), HOURS_PER_MONTH)

    lines: list[LineItem] = []
    notes: list[list[str]] = []
    for d, ratio, hourly, saving in zip(details, ratios, commitments, savings):
        ratio = ratio if ratio is not None else fallback
        floor_od = _opt(d.get("CurrentMinimumHourlyOnDemandSpend"))
        avg_od = _opt(d.get("CurrentAverageHourlyOnDemandSpend"))
        if avg_od is None and len(details) == 1:
            # The summary's on-demand total covers the whole lookback, so it
            # converts to an hourly rate over the lookback's own hours.
            total = _opt(summary.get("CurrentOnDemandSpend"))
            avg_od = total / lookback_hours if total else None
        convert = (lambda v: v * ratio if v is not None and ratio is not None else None)
        sp = d.get("SavingsPlansDetails") or {}
        utilization = _opt(d.get("EstimatedAverageUtilization"))
        line, line_notes = _size_line(
            spec=str(sp.get("InstanceFamily") or "") or "any instance family",
            unit=UNIT_SP,
            recommended=hourly,
            floor=convert(floor_od),
            average=convert(avg_od),
            aws_savings=saving,
            aws_upfront=_f(d.get("UpfrontCost")),
            term=term,
            unit_monthly_cost=HOURS_PER_MONTH,
            utilization_pct=utilization,
            monthly_on_demand=_f(d.get("EstimatedOnDemandCost")),
            floor_on_demand=floor_od,
            region=str(sp.get("Region") or ""),
            account_id=str(d.get("AccountId") or ""),
        )
        lines.append(line)
        notes.append(line_notes)
    return lines, notes, fallback


def analyze_sp_recommendation(rec: dict[str, Any]) -> Finding | None:
    """Convert one SP recommendation permutation into a risk-adjusted Finding."""
    if rec.get("error"):
        return None
    summary = rec.get("summary") or {}
    api_hourly = _f(summary.get("HourlyCommitmentToPurchase"))
    api_monthly = _f(summary.get("EstimatedMonthlySavingsAmount"))
    if api_hourly <= 0 and api_monthly <= 0:
        return None

    lines, notes_by_line, ratio = _sp_lines(rec)
    term = rec["term"]
    notes = _do_not_buy_note(lines, term)
    notes += _line_notes(lines, notes_by_line, UNIT_SP)

    floor_od = [line.floor_on_demand for line in lines]
    if ratio is not None and all(v is not None for v in floor_od):
        floor_sp = sum(line.floor for line in lines if line.floor is not None)
        notes.append(
            f"The quietest hour's ${sum(floor_od):,.4f}/hr of on-demand spend "
            f"absorbs only ${floor_sp:,.4f}/hr of Savings Plan commitment at "
            f"this plan's effective rate (~{1 - ratio:.0%} discount). The floor "
            "is compared in commitment dollars — comparing on-demand dollars "
            "against a Savings Plan commitment overstates what the trough "
            "can use."
        )
    elif ratio is None and any(v is not None for v in floor_od):
        notes.append(
            "AWS returned no Savings Plan cost for this recommendation, so the "
            "on-demand floor could not be converted to commitment dollars and "
            "was not used to size it."
        )

    upfront = sum(line.upfront_cost for line in lines)
    break_even = _weighted_break_even(lines)
    if upfront > 0 and break_even is not None:
        notes.append(
            f"${upfront:,.2f} upfront at the achievable size is recovered in "
            f"cash after {break_even:.1f} months."
        )
    utils = [line.utilization_pct for line in lines if line.utilization_pct]
    if utils:
        notes.append(
            f"AWS projects {sum(utils) / len(utils):.1f}% average utilization "
            "on its own recommended commitment."
        )
    if rec.get("detail_count_warning"):
        notes.append(rec["detail_count_warning"])

    floors = [line.floor for line in lines]
    averages = [line.average for line in lines]
    volatility, _ = classify_volatility(
        sum(floors) if all(v is not None for v in floors) else None,
        sum(averages) if all(v is not None for v in averages) else None,
    )
    do_not_buy = all(line.do_not_buy for line in lines)
    return Finding(
        family="savings-plan",
        label=SP_TYPE_LABELS.get(rec["sp_type"], rec["sp_type"]),
        term=term,
        payment=rec["payment"],
        api_hourly_commitment=api_hourly,
        safe_hourly_commitment=sum(line.achievable for line in lines),
        api_monthly_savings=api_monthly,
        safe_monthly_savings=sum(line.monthly_savings for line in lines),
        savings_percentage=_f(summary.get("EstimatedSavingsPercentage")),
        upfront_cost=upfront,
        aws_upfront_cost=sum(line.aws_upfront_cost for line in lines),
        confidence=_blend_confidence(lines),
        volatility=volatility,
        rationale=notes,
        break_even_months=break_even,
        waste_exposure_monthly=sum(line.waste_exposure_monthly for line in lines),
        aws_waste_exposure_monthly=sum(line.aws_waste_exposure_monthly for line in lines),
        detail_count=len(rec.get("details") or []),
        line_items=sorted(lines, key=lambda line: -line.monthly_savings),
        do_not_buy=do_not_buy,
        pool=SP_POOLS.get(rec["sp_type"], rec["sp_type"]),
        option=SP_TYPE_LABELS.get(rec["sp_type"], rec["sp_type"]),
        currency=_currency(summary, rec.get("details") or []),
    )


# ---------------------------------------------------------------------------
# Reserved Instances
# ---------------------------------------------------------------------------


def _ri_measure(d: dict[str, Any], instances: str, capacity: str) -> float | None:
    """Read an instance-count field, falling back to its capacity-unit twin.

    DynamoDB reports the same three measures in capacity units. `None` only
    when neither was returned, so a genuine 0 instance count stays 0.
    """
    value = _opt(d.get(instances))
    if value is None or (value == 0 and _opt(d.get(capacity))):
        alt = _opt(d.get(capacity))
        return alt if alt is not None else value
    return value


def analyze_ri_recommendation(rec: dict[str, Any]) -> Finding | None:
    """Convert one RI recommendation permutation into a risk-adjusted Finding.

    RIs commit to instance counts rather than dollars per hour, so each line's
    floor is measured in instances: `MinimumNumberOfInstancesUsedPerHour` is
    the count that line never drops below. Each line is sized against its own
    floor — a family-wide ratio would let a flat m5.large fleet subsidize a
    spiky r6g.4xlarge one.
    """
    if rec.get("error"):
        return None
    summary = rec.get("summary") or {}
    details = rec.get("details") or []
    api_monthly = _f(summary.get("TotalEstimatedMonthlySavingsAmount"))
    if api_monthly <= 0 and not details:
        return None

    term = rec["term"]
    term_months = TERM_MONTHS.get(term, 12)
    recommended = [
        _ri_measure(
            d, "RecommendedNumberOfInstancesToPurchase", "RecommendedNumberOfCapacityUnitsToPurchase"
        )
        or 0.0
        for d in details
    ]
    savings = _allocate(
        [_opt(d.get("EstimatedMonthlySavingsAmount")) for d in details],
        api_monthly,
        recommended,
    )

    lines: list[LineItem] = []
    notes_by_line: list[list[str]] = []
    for d, units, saving in zip(details, recommended, savings):
        upfront = _f(d.get("UpfrontCost"))
        recurring = _f(d.get("RecurringStandardMonthlyCost"))
        # Amortized upfront plus recurring, per unit: what one idle reservation
        # costs a month. Recurring alone is $0 for every All-Upfront RI, which
        # would price All-Upfront waste at nothing.
        unit_cost = (upfront / term_months + recurring) / units if units > 0 else 0.0
        spec = describe_recommendation_spec(d)
        line, line_notes = _size_line(
            spec=spec.get("label") or SPEC_UNAVAILABLE,
            unit=UNIT_RI,
            recommended=units,
            floor=_ri_measure(
                d, "MinimumNumberOfInstancesUsedPerHour", "MinimumNumberOfCapacityUnitsUsedPerHour"
            ),
            average=_ri_measure(
                d, "AverageNumberOfInstancesUsedPerHour", "AverageNumberOfCapacityUnitsUsedPerHour"
            ),
            aws_savings=saving,
            aws_upfront=upfront,
            term=term,
            unit_monthly_cost=unit_cost,
            aws_break_even=_f(d.get("EstimatedBreakEvenInMonths")),
            utilization_pct=_opt(d.get("AverageUtilization")),
            monthly_on_demand=_f(d.get("EstimatedMonthlyOnDemandCost")),
            size_flex_eligible=bool(spec.get("size_flex_eligible")),
            current_generation=bool(spec.get("current_generation", True)),
            region=spec.get("region", ""),
            account_id=str(d.get("AccountId") or ""),
        )
        lines.append(line)
        notes_by_line.append(line_notes)

    notes = _do_not_buy_note(lines, term)
    if lines:
        notes += _line_notes(lines, notes_by_line, UNIT_RI)
    real_lines = [line for line in lines if line.recommended > 0]
    if len(real_lines) > 1:
        notes.append(
            f"This total spans {len(real_lines)} distinct instance "
            "specifications — see the line items for what to buy against each. "
            "Reservations only apply to usage matching their exact "
            "specification, so the aggregate is a budget, not an order."
        )
    utils = [line.utilization_pct for line in lines if line.utilization_pct]
    if utils:
        notes.append(
            f"AWS projects {sum(utils) / len(utils):.1f}% average utilization "
            "across its recommended line items, at its own sizing."
        )

    # Family volatility is descriptive only (confidence comes from the lines).
    # Normalized units make different sizes in a family addable; raw instance
    # counts do not — 4 × large and 4 × 4xlarge are not "8 of something".
    norm_floor = [_opt(d.get("MinimumNormalizedUnitsUsedPerHour")) for d in details]
    norm_avg = [_opt(d.get("AverageNormalizedUnitsUsedPerHour")) for d in details]
    if details and all(v is not None for v in norm_floor + norm_avg):
        volatility, _ = classify_volatility(sum(norm_floor), sum(norm_avg))
    else:
        floors = [line.floor for line in lines]
        averages = [line.average for line in lines]
        volatility, _ = classify_volatility(
            sum(floors) if lines and all(v is not None for v in floors) else None,
            sum(averages) if lines and all(v is not None for v in averages) else None,
        )

    service = rec.get("service", "")
    return Finding(
        family="reserved-instance",
        label=rec.get("label") or RI_SERVICE_LABELS.get(service, service),
        term=term,
        payment=rec["payment"],
        api_hourly_commitment=sum(recommended),
        safe_hourly_commitment=sum(line.achievable for line in lines),
        api_monthly_savings=api_monthly if api_monthly > 0 else sum(savings),
        safe_monthly_savings=sum(line.monthly_savings for line in lines),
        savings_percentage=_f(summary.get("TotalEstimatedMonthlySavingsPercentage")),
        upfront_cost=sum(line.upfront_cost for line in lines),
        aws_upfront_cost=sum(line.aws_upfront_cost for line in lines),
        confidence=_blend_confidence(lines),
        volatility=volatility,
        rationale=notes,
        break_even_months=_weighted_break_even(lines),
        waste_exposure_monthly=sum(line.waste_exposure_monthly for line in lines),
        aws_waste_exposure_monthly=sum(line.aws_waste_exposure_monthly for line in lines),
        detail_count=len(details),
        line_items=sorted(lines, key=lambda line: -line.monthly_savings),
        do_not_buy=bool(lines) and all(line.do_not_buy for line in lines),
        pool=RI_POOLS.get(service, service),
        option=ri_option(service),
        currency=_currency(summary, details),
    )
