"""Output shaping: the JSON envelope, line-item capping, failure diagnosis.

Pure functions over analyzed findings and collected errors. `collect`
re-exports everything here, so hosts keep importing from one place.
"""

from __future__ import annotations

from typing import Any

from .analyze import UNIT_SP, Finding, LineItem
from .api import is_no_data
from .pools import recommended

# Line items per finding carried in a payload. A LINKED-scope sweep of a large
# organization returns one line per member account; uncapped, a dozen findings
# produced ~800 KB of JSON and a markdown report too long to relay verbatim.
LINE_ITEM_LIMIT = 25

TIME_BUDGET_ERROR = "TimeBudgetExceeded"


# ---------------------------------------------------------------------------
# Failure diagnosis
# ---------------------------------------------------------------------------

_HINTS = {
    "AccessDenied": (
        "The tool role is missing a Cost Explorer permission, or — in a "
        "cross-account deployment — the payer-account role's trust policy does "
        "not name this tool's role. See docs/agents/cost-operations.md, Mode B, "
        "for the full action list and trust policy."
    ),
    "ExpiredToken": (
        "The assumed-role credentials expired. Retry; if it recurs, the "
        "cross-account role's maximum session duration is shorter than the "
        "analysis."
    ),
    "DataUnavailable": (
        "Cost Explorer has no usage history for the requested lookback or "
        "posture window — the account may be new, or Cost Explorer was only "
        "recently enabled. Try a shorter window."
    ),
    "OptInRequired": "Cost Explorer is not enabled for this account.",
    "Validation": (
        "Cost Explorer rejected a parameter — commonly a posture window "
        "reaching further back than Cost Explorer retains."
    ),
    TIME_BUDGET_ERROR: (
        "No permutation finished within the time budget — Cost Explorer is "
        "throttling or slow. Narrow the sweep (ri_services, savings_plan_types, "
        "terms) and retry."
    ),
}


def diagnose_failure(errors: list[dict]) -> dict[str, Any]:
    """Explain a run in which every recommendation query failed.

    Such a run has no findings, and rendering it as a report would print "no
    commitment opportunity found" — a confident answer to a question that was
    never asked of AWS. It is returned as an error instead, with the most
    common cause named.
    """
    codes = [e.get("error_code") or "" for e in errors]
    hint = next(
        (text for marker, text in _HINTS.items() if any(marker in c for c in codes)),
        "Every recommendation query failed; see collection_warnings for each "
        "reason.",
    )
    first = next((e for e in errors if e.get("error")), {})
    return {
        "error": (
            f"All {len(errors)} Cost Explorer recommendation queries failed, so "
            "no analysis was produced. This is not a 'no opportunity' result. "
            f"First failure: {first.get('error_code', '')} {first.get('error', '')}".strip()
        ),
        "hint": hint,
    }


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


def _round(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None else None


def _break_even(value: float | None) -> float | str | None:
    if value is None:
        return None
    return round(value, 1) if value != float("inf") else "never"


def serialize_line_item(item: LineItem) -> dict:
    """Flatten one purchasable line of a recommendation.

    A consumer that only reads the finding-level total cannot act on it: a
    reservation applies to usage matching its exact specification, so `spec` and
    `region` are what a purchase is actually placed against. Savings and upfront
    are at the achievable size; the `aws_*` keys are AWS's own sizing.
    """
    return {
        "spec": item.spec,
        "region": item.region,
        "commitment_unit": item.unit,
        "aws_recommended_commitment": round(item.recommended, 4),
        "achievable_commitment": round(item.achievable, 4),
        "minimum_observed": _round(item.floor, 4),
        "average_observed": _round(item.average, 4),
        "minimum_hourly_on_demand_spend": _round(item.floor_on_demand, 4),
        "estimated_monthly_savings": round(item.monthly_savings, 2),
        "aws_best_case_monthly_savings": round(item.aws_monthly_savings, 2),
        "upfront_cost": round(item.upfront_cost, 2),
        "aws_upfront_cost": round(item.aws_upfront_cost, 2),
        "break_even_months": _break_even(item.break_even_months),
        "do_not_buy": item.do_not_buy,
        "confidence": item.confidence,
        "spend_profile": item.volatility,
        "monthly_on_demand_cost": round(item.monthly_on_demand, 2),
        "aws_estimated_utilization_percentage": _round(item.utilization_pct, 2),
        "size_flexible": item.size_flex_eligible,
        "current_generation": item.current_generation,
        "account_id": item.account_id,
    }


def serialize_finding(
    f: Finding, *, include_lines: bool = True, line_limit: int = LINE_ITEM_LIMIT
) -> dict:
    """Flatten a Finding into a flat recommendation dict.

    Key names mirror what an AWS Cost Optimization Hub style recommendation
    list looks like, so a host that already renders those needs no translation
    layer. Line items are capped at `line_limit`, highest achievable savings
    first, with the full count stated.
    """
    payload = {
        "commitment_family": f.family,
        "commitment_type": f.label,
        "role": f.role,
        "usage_pool": f.pool,
        "term": f.term,
        "payment_option": f.payment,
        "currency": f.currency,
        "aws_recommended_commitment": round(f.api_hourly_commitment, 4),
        "achievable_commitment": round(f.safe_hourly_commitment, 4),
        "commitment_unit": UNIT_SP if f.family == "savings-plan" else "units",
        "estimated_monthly_savings": round(f.safe_monthly_savings, 2),
        "aws_best_case_monthly_savings": round(f.api_monthly_savings, 2),
        "estimated_savings_percentage": round(f.savings_percentage, 2),
        "upfront_cost": round(f.upfront_cost, 2),
        "aws_upfront_cost": round(f.aws_upfront_cost, 2),
        "break_even_months": _break_even(f.break_even_months),
        "waste_exposure_monthly": round(f.waste_exposure_monthly, 2),
        "aws_waste_exposure_monthly": round(f.aws_waste_exposure_monthly, 2),
        "do_not_buy": f.do_not_buy,
        "confidence": f.confidence,
        "spend_profile": f.volatility,
        "implementation_effort": "Medium",
        "rationale": f.rationale,
        "line_items_total": len(f.line_items),
    }
    if include_lines:
        payload["line_items"] = [serialize_line_item(i) for i in f.line_items[:line_limit]]
        payload["line_items_omitted"] = max(0, len(f.line_items) - line_limit)
    return payload


def envelope(
    findings: list[Finding],
    reconciliation: dict | None = None,
    *,
    pools: list[dict] | None = None,
    include_lines: bool = True,
) -> dict:
    """Build the JSON response shape: recommendations plus roll-up totals.

    Totals add only `recommended` findings — one option per usage pool — so
    three ways to discount the same EC2 spend are never summed.
    """
    chosen = recommended(findings)
    payload = {
        "recommendations": [serialize_finding(f, include_lines=include_lines) for f in findings],
        "count": len(findings),
        "total_estimated_monthly_savings": round(
            sum(f.safe_monthly_savings for f in chosen), 2
        ),
        "aws_best_case_monthly_savings": round(
            sum(f.api_monthly_savings for f in chosen), 2
        ),
        "total_upfront_cost": round(sum(f.upfront_cost for f in chosen), 2),
    }
    if pools is not None:
        payload["usage_pools"] = pools
    if reconciliation is not None:
        payload["reconciliation"] = reconciliation
    return payload



def no_data_only(errors: list[dict]) -> bool:
    """True when every warning is Cost Explorer saying "nothing to report"."""
    return bool(errors) and all(is_no_data(e) for e in errors)
