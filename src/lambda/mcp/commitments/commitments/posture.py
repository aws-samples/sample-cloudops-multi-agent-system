"""Is it safe to buy more, and do the two AWS pipelines agree?

Two checks that sit around the sizing in `analyze`: the health of commitments
already held (buying on top of an under-used one compounds waste), and a
like-for-like comparison against Cost Optimization Hub.

Pure functions over collected data; nothing here calls AWS.
"""

from __future__ import annotations

from typing import Any

from .analyze import Finding, _f, _opt
from .api import RI_SERVICE_LABELS, SP_TYPE_LABELS, is_no_data
from .pools import COH_POOL_OPTIONS, option_totals

# Utilization below this on an existing commitment means money is already being
# wasted, and is a reason to hold off buying more.
UTILIZATION_WARN_PCT = 95.0

# Coverage above this means there is little on-demand left to convert; further
# purchase risks over-committing.
COVERAGE_SATURATED_PCT = 90.0

# ---------------------------------------------------------------------------
# Existing-commitment posture
# ---------------------------------------------------------------------------


def _record_failure(posture: dict[str, Any], source: str, result: dict[str, Any], none_note: str) -> None:
    """Classify a failed posture query as "nothing held" or "not measured".

    DataUnavailableException is how Cost Explorer says there is no commitment
    of that kind to measure — a real, safe answer. Anything else (AccessDenied,
    a window outside Cost Explorer's history) means the gate could not run, and
    a gate that could not run must not read as a gate that passed.
    """
    if is_no_data(result):
        posture["notes"].append(none_note)
    else:
        posture["unmeasured"].append(source)
        posture["notes"].append(f"{source} unavailable: {result['error']}")


def _sp_posture(posture: dict[str, Any], sp_coverage: dict, sp_utilization: dict) -> None:
    periods = sp_coverage.get("periods") or []
    if periods:
        latest = periods[-1].get("Coverage", {})
        pct = _opt(latest.get("CoveragePercentage"))
        posture["sp_coverage_pct"] = pct
        posture["sp_on_demand_cost"] = _f(latest.get("OnDemandCost"))
        if pct is not None and pct >= COVERAGE_SATURATED_PCT:
            posture["blockers"].append(
                f"Savings Plans coverage is already {pct:.1f}% — little "
                "on-demand spend left to convert. Verify headroom before buying."
            )
    elif sp_coverage.get("error"):
        _record_failure(posture, "Savings Plans coverage", sp_coverage, "No Savings Plans coverage data.")

    util = (sp_utilization.get("total") or {}).get("Utilization") or {}
    if util:
        pct = _opt(util.get("UtilizationPercentage"))
        held = _opt(util.get("TotalCommitment"))
        posture["sp_utilization_pct"] = pct
        posture["sp_unused_commitment"] = _f(util.get("UnusedCommitment"))
        # `is not None`, not truthiness: a measured 0% utilization is the worst
        # possible posture, not a missing one.
        if pct is not None and (held is None or held > 0) and pct < UTILIZATION_WARN_PCT:
            posture["blockers"].append(
                f"Existing Savings Plans are only {pct:.1f}% utilized "
                f"(${_f(util.get('UnusedCommitment')):,.2f} unused). Fix this "
                "before adding commitment."
            )
    elif sp_utilization.get("error"):
        _record_failure(
            posture,
            "Savings Plans utilization",
            sp_utilization,
            "No active Savings Plans — nothing to measure utilization against.",
        )


def _ri_posture(posture: dict[str, Any], service: str, coverage: dict, utilization: dict) -> None:
    label = RI_SERVICE_LABELS.get(service, service)
    entry: dict[str, Any] = {"service": service}

    hours = (coverage.get("total") or {}).get("CoverageHours") or {}
    if hours:
        entry["coverage_pct"] = _opt(hours.get("CoverageHoursPercentage"))
        entry["on_demand_hours"] = _f(hours.get("OnDemandHours"))
    elif coverage.get("error"):
        _record_failure(posture, f"{label} reservation coverage", coverage, f"No {label} reservation coverage data.")

    total = utilization.get("total") or {}
    if total:
        pct = _opt(total.get("UtilizationPercentage"))
        purchased = _opt(total.get("PurchasedHours"))
        entry["utilization_pct"] = pct
        entry["unused_hours"] = _f(total.get("UnusedHours"))
        entry["realized_savings"] = _f(total.get("RealizedSavings"))
        if pct is not None and (purchased is None or purchased > 0) and pct < UTILIZATION_WARN_PCT:
            posture["blockers"].append(
                f"Existing {label} reservations are only {pct:.1f}% utilized "
                f"({_f(total.get('UnusedHours')):,.0f} unused hours). Reconcile "
                f"before buying more {label} commitment."
            )
    elif utilization.get("error"):
        _record_failure(
            posture,
            f"{label} reservation utilization",
            utilization,
            f"No active {label} reservations — nothing to measure utilization against.",
        )

    if len(entry) > 1:
        posture["reservations"][label] = entry
        posture["ri_utilization_by_service"][service] = entry.get("utilization_pct")


def assess_existing_posture(
    sp_coverage: dict[str, Any],
    sp_utilization: dict[str, Any],
    ri_coverage: dict[str, dict[str, Any]],
    ri_utilization: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Summarize whether existing commitments are healthy enough to add more.

    `ri_coverage` / `ri_utilization` are keyed by Cost Explorer service name,
    one filtered query each — unfiltered, Cost Explorer reports EC2 only, and
    an RDS renewal would be judged on EC2's utilization.

    Buying on top of an under-utilized commitment compounds waste, so this
    produces explicit blockers the report surfaces before any recommendation.
    `unmeasured` lists every source that failed for a reason other than "no
    commitments held"; see `safe_to_buy_more`.
    """
    posture: dict[str, Any] = {
        "blockers": [],
        "notes": [],
        "unmeasured": [],
        "reservations": {},
        "ri_utilization_by_service": {},
    }
    _sp_posture(posture, sp_coverage, sp_utilization)
    for service in sorted(set(ri_coverage) | set(ri_utilization)):
        _ri_posture(posture, service, ri_coverage.get(service, {}), ri_utilization.get(service, {}))
    return posture


def safe_to_buy_more(posture: dict[str, Any]) -> bool | None:
    """True/False when the gate ran; None when it could not.

    A posture whose every query failed has no blockers, and returning True for
    it would tell the caller that existing commitments are healthy when nothing
    was measured.
    """
    if posture.get("blockers"):
        return False
    if posture.get("unmeasured"):
        return None
    return True


# ---------------------------------------------------------------------------
# Reconciliation against Cost Optimization Hub
# ---------------------------------------------------------------------------


def reconcile_with_coh(
    findings: list[Finding],
    coh: dict[str, Any],
    preferences: dict[str, Any] | None = None,
    *,
    ce_account_id: str = "",
    coh_account_id: str = "",
) -> dict[str, Any]:
    """Compare Cost Explorer against Cost Optimization Hub, like for like.

    `findings` is every analyzed permutation, not just the selected ones. The
    comparison is only meaningful when both sides describe the same purchase,
    so it is narrowed on three axes:

      * **Term and payment.** COH prices every commitment at one preferred
        (term, payment) — 3-year All Upfront by default. Only Cost Explorer
        findings at that same permutation are compared.
      * **Coverage.** COH has no Database Savings Plan resource type, so those
        findings are excluded from the Cost Explorer side.
      * **Overlap.** Alternatives for the same usage are totalled once per
        usage pool on both sides, at the AWS best case (COH publishes a best
        case too).

    A material gap that survives all three is worth investigating; one that
    would vanish under them is an artifact.
    """
    if coh.get("error"):
        return {"status": "unavailable", "reason": coh["error"]}

    prefs = preferences or {}
    term = prefs.get("term", "THREE_YEARS")
    payment = prefs.get("payment", "ALL_UPFRONT")
    no_coh_equivalent = SP_TYPE_LABELS["DATABASE_SP"]
    excluded = sorted({f.label for f in findings if f.label == no_coh_equivalent})
    comparable = [
        f
        for f in findings
        if f.term == term
        and f.payment == payment
        and f.label not in excluded
        and not f.do_not_buy
    ]
    notes: list[str] = []
    if prefs.get("assumed_default"):
        notes.append(
            "Cost Optimization Hub's commitment preference could not be read; "
            "its documented default (3-year, All Upfront) is assumed."
        )
    if excluded:
        notes.append(
            f"Excluded from the comparison: {', '.join(excluded)} — Cost "
            "Optimization Hub publishes no equivalent recommendation."
        )
    if coh_account_id and ce_account_id and coh_account_id != ce_account_id:
        notes.append(
            f"Cost Optimization Hub was read in account {coh_account_id} and "
            f"Cost Explorer in {ce_account_id}; the two are only comparable if "
            "COH's visibility covers the same accounts."
        )

    coh_recs = coh.get("recommendations", [])
    coh_options: dict[str, dict[str, float]] = {}
    by_type: dict[str, float] = {}
    for r in coh_recs:
        rtype = r["recommended_resource_type"] or r["current_resource_type"]
        amount = r["estimated_monthly_savings"]
        by_type[rtype] = round(by_type.get(rtype, 0.0) + amount, 2)
        pool, option = COH_POOL_OPTIONS.get(rtype, (rtype, rtype))
        options = coh_options.setdefault(pool, {})
        options[option] = options.get(option, 0.0) + amount
    coh_total = round(sum(max(o.values()) for o in coh_options.values() if o), 2)

    ce_options = option_totals(comparable, lambda f: f.api_monthly_savings)
    ce_total = round(sum(max(o.values()) for o in ce_options.values() if o), 2)

    basis = {"term": term, "payment": payment, "assumed_default": bool(prefs.get("assumed_default"))}
    base = {
        "ce_monthly_savings": ce_total,
        "coh_monthly_savings": coh_total,
        "coh_count": len(coh_recs),
        "coh_by_resource_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
        "basis": basis,
        "excluded": excluded,
        "notes": notes,
    }
    if not comparable and any(not f.do_not_buy for f in findings) and coh_total > 0:
        return {
            "status": "not-comparable",
            "reason": (
                f"Cost Optimization Hub prices commitments at {term} / {payment}, "
                "and this sweep has no finding at that term and payment option."
            ),
            "delta": None,
            "delta_pct": None,
            **base,
        }

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
    return {"status": status, "delta": delta, "delta_pct": round(delta_pct, 1), **base}
