"""Render the commitment analysis as a markdown report.

Report order is deliberate: posture blockers come before recommendations, so a
reader cannot skim the savings number without first seeing that existing
commitments are under-utilized.
"""

from __future__ import annotations

from typing import Any

from .analyze import TERM_MONTHS, Finding, LineItem

TERM_LABELS = {"ONE_YEAR": "1-year", "THREE_YEARS": "3-year"}
PAYMENT_LABELS = {
    "NO_UPFRONT": "No upfront",
    "PARTIAL_UPFRONT": "Partial upfront",
    "ALL_UPFRONT": "All upfront",
}
LOOKBACK_LABELS = {
    "SEVEN_DAYS": "7 days",
    "THIRTY_DAYS": "30 days",
    "SIXTY_DAYS": "60 days",
}
CONFIDENCE_MARKS = {"High": "High", "Medium": "Medium", "Low": "Low"}

RECONCILE_VERDICTS = {
    "reconciled": (
        "Reconciled — Cost Explorer and Cost Optimization Hub agree within 10%. "
        "Figures below are defensible against the console."
    ),
    "minor-variance": (
        "Minor variance — the two AWS pipelines differ by 10-30%. Usually a "
        "lookback-window difference; state the CE figure and note the spread."
    ),
    "material-variance": (
        "MATERIAL VARIANCE — the two AWS pipelines differ by more than 30%. Do "
        "not quote a single number until the cause is identified (commonly a "
        "different account scope, or COH data lagging a recent usage change)."
    ),
    "agree-zero": (
        "Both pipelines report no commitment opportunity. Consistent, and the "
        "eligible-spend section below shows why."
    ),
    "unavailable": (
        "Not reconciled — Cost Optimization Hub could not be queried, so these "
        "figures rest on Cost Explorer alone."
    ),
}


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _pct(value: float) -> str:
    return f"{value:.1f}%"


def _commitment_str(f: Finding, value: float) -> str:
    if f.family == "savings-plan":
        return f"${value:,.4f}/hr"
    return f"{value:,.0f} unit(s)"


def _size_str(item: dict[str, Any]) -> str:
    """Format a commitment's size in its own unit.

    Savings Plans commit in dollars per hour and reservations in unit counts.
    Printing an hourly-dollar commitment as a bare number misreads it by
    roughly 1000x, so the unit is never dropped.
    """
    quantity = item.get("quantity", 0.0)
    if item.get("unit") == "USD/hour":
        return f"${quantity:,.4f}/hr"
    return f"{quantity:,.0f} unit(s)"


ACTION_LABELS = {
    "renew": "**renew**",
    "renew-smaller": "**renew smaller**",
    "let-lapse": "let lapse",
    "review": "review",
}


def _line_quantity(value: float, unit: str) -> str:
    return f"${value:,.4f}/hr" if unit == "USD/hour" else f"{value:,.0f}"


def _line_spec(item: LineItem) -> str:
    """The spec cell, with the two flags that change what you should buy.

    Size flexibility means the recommended size is not binding — the discount
    follows any size in the family. A previous-generation instance is the
    opposite kind of signal: committing to one for three years locks the account
    out of the cheaper current generation for the whole term.
    """
    flags = []
    if item.size_flex_eligible:
        flags.append("size-flexible")
    if not item.current_generation:
        flags.append("**previous generation**")
    return item.spec + (f" ({', '.join(flags)})" if flags else "")


def _line_items_table(add: Any, f: Finding) -> None:
    """Render what to actually buy, one row per purchasable specification.

    The family total above is the ranking figure; a reservation applies only to
    usage matching its exact instance type, deployment option and Availability
    Zone, so without this table the recommendation cannot be acted on.
    """
    items = [i for i in f.line_items if i.recommended > 0]
    if not items:
        return
    add("Line items — what to buy:")
    add("")
    add(
        "| Buy | Region | AWS units | Floor | Achievable | Utilization "
        "| Savings/mo |"
    )
    add("|---|---|---:|---:|---:|---:|---:|")
    for item in items:
        util = (
            _pct(item.utilization_pct)
            if item.utilization_pct is not None
            else "unknown"
        )
        add(
            f"| {_line_spec(item)} | {item.region or '—'} "
            f"| {_line_quantity(item.recommended, item.unit)} "
            f"| {_line_quantity(item.floor, item.unit)} "
            f"| {_line_quantity(item.achievable, item.unit)} "
            f"| {util} | {_money(item.monthly_savings)} |"
        )
    add("")
    if f.family == "reserved-instance":
        allocated = sum(i.achievable for i in items)
        # Each line rounds down to a whole reservation, so the rounding losses
        # accumulate. Saying which line absorbs the remainder is what keeps the
        # table addable to the headline figure.
        if allocated < f.safe_hourly_commitment:
            shortfall = f.safe_hourly_commitment - allocated
            add(
                f"Rounding each line down to whole reservations leaves "
                f"{shortfall:,.0f} unit(s) unallocated against the "
                f"{f.safe_hourly_commitment:,.0f} achievable total — add them to "
                "the line with the highest floor."
            )
            add("")
        add(
            "*Floor* is the count that line never dropped below during the "
            "lookback, so it is the part of the recommendation that carries no "
            "unused-commitment risk."
        )
        add("")


def _expiry_section(add: Any, expiry: dict[str, Any]) -> None:
    """Render the expiry/renewal section.

    Deliberately placed between existing-commitment health and the new-purchase
    recommendations: a commitment lapsing in three weeks is a decision with a
    deadline, and it belongs ahead of an optional purchase.
    """
    add("## Commitment expiry and renewal")
    add("")
    regions = ", ".join(expiry.get("regions", [])) or "(none)"
    add(
        f"Inventory taken {expiry['as_of']} over a {expiry['horizon_days']}-day "
        f"horizon. Reservation regions swept: {regions}. Savings Plans are "
        "account-level and are listed once regardless of region."
    )
    add("")

    expiring = expiry.get("expiring") or []
    expired = expiry.get("expired") or []
    counts = expiry.get("counts", {})

    if not expiring:
        add(
            f"**No commitment expires within {expiry['horizon_days']} days.** "
            f"{expiry.get('total_active', 0)} active commitment(s) were "
            "inventoried."
        )
        add("")
    else:
        add(
            f"**{len(expiring)} commitment(s) expire within "
            f"{expiry['horizon_days']} days** — {counts.get('urgent', 0)} within "
            f"30 days, {counts.get('soon', 0)} within 60, "
            f"{counts.get('upcoming', 0)} within 90."
        )
        add("")
        monthly = expiry.get("monthly_committed_spend_expiring", 0.0)
        units = expiry.get("reserved_units_expiring", 0.0)
        exposure = []
        if monthly:
            exposure.append(
                f"{_money(monthly)}/mo of committed Savings Plan spend "
                f"({_money(expiry.get('hourly_commitment_expiring', 0.0))}/hr) "
                "reverts to on-demand rates if not renewed"
            )
        if units:
            exposure.append(
                f"{units:,.0f} reserved unit(s) lose their discount. The dollar "
                "value of that is not stated because it needs per-instance "
                "pricing this analysis does not query"
            )
        if exposure:
            add("Exposure: " + "; ".join(exposure) + ".")
            add("")

        add(
            "| Ends | Days | Commitment | Spec | Size | Region | Utilization "
            "| Action |"
        )
        add("|---|---:|---|---|---:|---|---:|---|")
        for item in expiring:
            util = item.get("utilization_pct")
            util_str = _pct(util) if util is not None else "unknown"
            ident = item.get("commitment_id") or "(no id)"
            add(
                f"| {item.get('end', '')} | {item['days_remaining']} "
                f"| {item.get('label', '')} `{ident}` "
                f"| {item.get('spec') or '—'} "
                f"| {_size_str(item)} | {item.get('region', '')} "
                f"| {util_str} "
                f"| {ACTION_LABELS.get(item['action'], item['action'])} |"
            )
        add("")
        add(
            "> Utilization is the account-level figure from Cost Explorer, not "
            "per-commitment — there is no API that reports utilization for an "
            "individual Savings Plan or reservation. Treat it as the portfolio "
            "signal it is, and confirm a specific commitment in the console "
            "before acting."
        )
        add("")
        add(
            "> *Spec* is what a renewal has to match. A reservation bought "
            "against a different instance class, deployment option (Single-AZ "
            "vs Multi-AZ) or engine does not cover the same usage, so a renewal "
            "that changes any of these is a new purchase and needs fresh "
            "sizing. An empty spec on a Compute Savings Plan is correct — it "
            "commits to dollars, not to a family."
        )
        add("")
        add("### Why")
        add("")
        for item in expiring:
            ident = item.get("commitment_id") or item.get("label", "commitment")
            add(f"- `{ident}` — {item['rationale']}")
        add("")

    if expired:
        add("### Already ended but still listed as active")
        add("")
        for item in expired:
            ident = item.get("commitment_id") or item.get("label", "commitment")
            spec = item.get("spec")
            described = f"{item.get('label', '')} {spec}".strip() if spec else item.get(
                "label", ""
            )
            add(f"- `{ident}` ({described}) — {item['rationale']}")
        add("")

    undated = expiry.get("undated") or []
    if undated:
        add(
            f"{len(undated)} commitment(s) returned no usable end date and could "
            "not be assessed: "
            + ", ".join(
                f"`{i.get('commitment_id') or i.get('label', '?')}`" for i in undated
            )
            + "."
        )
        add("")

    blind_spots = expiry.get("blind_spots") or []
    if blind_spots:
        add("Not covered by this inventory: " + "; ".join(blind_spots) + ".")
        add("")


def render(data: dict[str, Any]) -> str:
    """Build the full markdown report from a collected+analyzed payload."""
    meta = data["meta"]
    findings: list[Finding] = data["findings"]
    posture = data["posture"]
    recon = data["reconciliation"]
    spend = data["eligible_spend"]
    errors = data.get("errors", [])

    out: list[str] = []
    add = out.append

    # ---------------------------------------------------------------- header
    add("# AWS Discounted Commitments Report")
    add("")
    add(f"**Account:** {meta['account_id']}")
    if meta.get("profile"):
        add(f"**Profile:** `{meta['profile']}`")
    add(f"**Generated:** {meta['generated_at']}")
    add(
        f"**Lookback:** {LOOKBACK_LABELS.get(meta['lookback'], meta['lookback'])}"
        f" &nbsp;|&nbsp; **Account scope:** {meta['account_scope']}"
    )
    add(
        "**Source APIs:** Cost Explorer (`GetSavingsPlansPurchaseRecommendation`, "
        "`GetReservationPurchaseRecommendation`, coverage + utilization), "
        "Cost Optimization Hub (`ListRecommendations`)"
    )
    add("")
    add("All data is read-only. This report does not purchase anything.")
    add("")

    # -------------------------------------------------------- bottom line
    total_api = sum(f.api_monthly_savings for f in findings)
    total_safe = sum(f.safe_monthly_savings for f in findings)
    high_conf = [f for f in findings if f.confidence == "High"]
    total_high = sum(f.safe_monthly_savings for f in high_conf)

    add("## Bottom line")
    add("")
    # An expiry inside 30 days outranks a purchase recommendation: it has a
    # deadline attached, and missing it silently raises the bill.
    urgent = (data.get("expiry") or {}).get("counts", {}).get("urgent", 0)
    if urgent:
        add(
            f"**{urgent} existing commitment(s) expire within 30 days.** That "
            "deadline comes before any new purchase — see *Commitment expiry "
            "and renewal*."
        )
        add("")
    if not findings:
        add(
            "**No commitment opportunity found.** Neither Cost Explorer nor Cost "
            "Optimization Hub recommends a Savings Plan or Reserved Instance "
            "purchase for this account at the requested term and payment options."
        )
        add("")
        add("")
        add(
            "This is a real result, not a failure — see *Eligible spend* below "
            "for whether the account simply has no commitment-addressable usage."
        )
        add("")
    else:
        add(
            f"| Measure | Monthly | Annual |\n"
            f"|---|---:|---:|\n"
            f"| AWS best-case savings (as the console shows) | {_money(total_api)} "
            f"| {_money(total_api * 12)} |\n"
            f"| **Risk-adjusted achievable savings** | **{_money(total_safe)}** "
            f"| **{_money(total_safe * 12)}** |\n"
            f"| High-confidence subset only | {_money(total_high)} "
            f"| {_money(total_high * 12)} |"
        )
        add("")
        if total_api > 0:
            haircut = (1 - total_safe / total_api) * 100
            add(
                f"The risk-adjusted figure is {haircut:.0f}% below the AWS "
                "best case. That gap is unused-commitment risk on variable "
                "workloads — see *Method* for how it is derived."
            )
        add("")
        if posture["blockers"]:
            add(
                "**Do not act on these numbers yet.** "
                f"{len(posture['blockers'])} blocker(s) on existing commitments "
                "must be resolved first — see the next section."
            )
            add("")

    # ------------------------------------------------------ reconciliation
    add("## Reconciliation against AWS native tools")
    add("")
    add(RECONCILE_VERDICTS.get(recon["status"], recon["status"]))
    add("")
    if recon["status"] != "unavailable":
        add("| Pipeline | Recommended monthly savings |")
        add("|---|---:|")
        add(f"| Cost Explorer purchase recommendations | {_money(recon['ce_monthly_savings'])} |")
        add(f"| Cost Optimization Hub ({recon['coh_count']} commitment recs) | {_money(recon['coh_monthly_savings'])} |")
        add(f"| Delta | {_money(recon['delta'])} ({recon['delta_pct']}%) |")
        add("")
        if recon.get("coh_by_resource_type"):
            add("Cost Optimization Hub breakdown by commitment type:")
            add("")
            add("| Resource type | Monthly savings |")
            add("|---|---:|")
            for rtype, amount in recon["coh_by_resource_type"].items():
                add(f"| {rtype} | {_money(amount)} |")
            add("")
    else:
        add(f"Reason: {recon.get('reason', 'unknown')}")
        add("")

    # ------------------------------------------------- existing commitments
    add("## Existing commitment health")
    add("")
    if posture["blockers"]:
        add("### Blockers")
        add("")
        for b in posture["blockers"]:
            add(f"- **{b}**")
        add("")

    rows = [
        ("Savings Plans coverage", posture.get("sp_coverage_pct"), "%"),
        ("Savings Plans utilization", posture.get("sp_utilization_pct"), "%"),
        ("Unused SP commitment", posture.get("sp_unused_commitment"), "$"),
        ("Reservation coverage", posture.get("ri_coverage_pct"), "%"),
        ("Reservation utilization", posture.get("ri_utilization_pct"), "%"),
        ("Unused reservation hours", posture.get("ri_unused_hours"), "h"),
        ("Realized RI savings", posture.get("ri_realized_savings"), "$"),
    ]
    # A table of all-zeros reads as "we measured 0% utilization", which implies
    # a broken commitment. Absent commitments produce zeros across the board, so
    # that case gets prose instead of a misleading table.
    present = [(label, val, unit) for label, val, unit in rows if val is not None]
    all_zero = bool(present) and all(val == 0 for _, val, _ in present)
    if all_zero:
        add(
            "No active Savings Plans or Reserved Instances detected — every "
            "coverage and utilization metric reads zero because there is nothing "
            "to measure, not because an existing commitment is being wasted. "
            "Any recommendation below is therefore a net-new purchase with no "
            "inherited utilization risk."
        )
        add("")
    elif present:
        add("| Metric | Value |")
        add("|---|---:|")
        for label, val, unit in present:
            if unit == "%":
                shown = _pct(val)
            elif unit == "$":
                shown = _money(val)
            else:
                shown = f"{val:,.0f}"
            add(f"| {label} | {shown} |")
        add("")
    else:
        # Neither table nor zero-prose applies: every posture query failed. The
        # per-query reasons print as notes just below.
        add(
            "Existing commitment posture could not be measured — every coverage "
            "and utilization query failed. Treat the recommendations below as "
            "unvalidated against current commitments."
        )
        add("")

    for note in posture.get("notes", []):
        add(f"> {note}")
    if posture.get("notes"):
        add("")

    # ------------------------------------------------------ expiry/renewal
    # Read with .get so a payload produced before expiry collection existed
    # still renders — the section is simply omitted.
    if data.get("expiry"):
        _expiry_section(add, data["expiry"])

    # ---------------------------------------------------- recommendations
    add("## Recommended commitments")
    add("")
    if not findings:
        add("None. See *Bottom line* above.")
        add("")
    else:
        add(
            "| # | Commitment | Term | Payment | AWS commitment | "
            "Achievable commitment | Achievable savings/mo | Discount | "
            "Confidence |"
        )
        add("|---|---|---|---|---:|---:|---:|---:|---|")
        for i, f in enumerate(findings, 1):
            add(
                f"| {i} | {f.label} | {TERM_LABELS.get(f.term, f.term)} "
                f"| {PAYMENT_LABELS.get(f.payment, f.payment)} "
                f"| {_commitment_str(f, f.api_hourly_commitment)} "
                f"| {_commitment_str(f, f.safe_hourly_commitment)} "
                f"| {_money(f.safe_monthly_savings)} "
                f"| {_pct(f.savings_percentage)} | {f.confidence} |"
            )
        add("")

        add("### Detail")
        add("")
        for i, f in enumerate(findings, 1):
            add(
                f"#### {i}. {f.label} — {TERM_LABELS.get(f.term, f.term)}, "
                f"{PAYMENT_LABELS.get(f.payment, f.payment)}"
            )
            add("")
            add(f"- **Confidence:** {f.confidence} (spend profile: {f.volatility})")
            add(
                f"- **Commitment:** AWS recommends "
                f"{_commitment_str(f, f.api_hourly_commitment)}; this report "
                f"recommends {_commitment_str(f, f.safe_hourly_commitment)}"
            )
            add(
                f"- **Savings:** {_money(f.safe_monthly_savings)}/mo achievable "
                f"({_money(f.api_monthly_savings)}/mo at the AWS best case)"
            )
            if f.upfront_cost > 0:
                add(f"- **Upfront cost:** {_money(f.upfront_cost)}")
            if f.break_even_months:
                term_months = TERM_MONTHS.get(f.term)
                # Break-even past the end of the term means the upfront payment
                # never returns — say so on the same line as the number.
                if term_months and f.break_even_months > term_months:
                    add(
                        f"- **Break-even:** {f.break_even_months:.1f} months "
                        f"— **longer than the {term_months}-month term, so this "
                        "purchase cannot pay back**"
                    )
                else:
                    add(f"- **Break-even:** {f.break_even_months:.1f} months")
            if f.waste_exposure_monthly > 0:
                add(
                    f"- **Waste exposure at the AWS figure:** up to "
                    f"{_money(f.waste_exposure_monthly)}/mo unused if usage "
                    "falls to its observed floor"
                )
            if f.detail_count:
                add(f"- **Line items analyzed:** {f.detail_count}")
            add("")
            _line_items_table(add, f)
            if f.rationale:
                add("Why:")
                add("")
                for note in f.rationale:
                    add(f"- {note}")
                add("")

    # ------------------------------------------------------ eligible spend
    add("## Eligible spend")
    add("")
    periods = spend.get("periods") or []
    if spend.get("error"):
        add(f"Could not retrieve spend: {spend['error']}")
    elif periods:
        add("| Period | Total unblended spend |")
        add("|---|---:|")
        for p in periods:
            add(f"| {p['start']} → {p['end']} | {_money(p['total'])} |")
        add("")
        latest = periods[-1]
        top = sorted(
            latest["by_service"].items(), key=lambda kv: -kv[1]
        )[:10]
        add(f"Top services, {latest['start']} → {latest['end']}:")
        add("")
        add("| Service | Spend |")
        add("|---|---:|")
        for svc, amount in top:
            add(f"| {svc} | {_money(amount)} |")
        add("")
        add(
            "Commitments only apply to compute and database *instance* usage. "
            "Serverless, data transfer, storage, support, and most managed-API "
            "spend (for example Bedrock inference) cannot be committed against, "
            "so a large bill with little instance usage will correctly yield no "
            "recommendation."
        )
    else:
        add("No spend data returned for the requested period.")
    add("")

    # ------------------------------------------------------------- method
    add("## Method")
    add("")
    add(
        "1. **Purchase recommendations** are pulled from Cost Explorer for every "
        "requested (term, payment) permutation across all four Savings Plan "
        "types and all eight RI-eligible services. The strongest permutation "
        "per family is reported; the rest are collected but suppressed to keep "
        "the decision legible."
    )
    add(
        "2. **Risk adjustment.** The AWS recommendation maximizes savings by "
        "assuming the lookback window repeats. This report also reads the "
        "*minimum* and *average* hourly on-demand spend AWS returns alongside "
        "it, and classifies the workload:"
    )
    add("")
    add(
        "   - trough ≥ 80% of average → **stable**, take the AWS figure as-is "
        "(High confidence)\n"
        "   - trough ≥ 50% of average → **moderate**, commit to the midpoint of "
        "floor and AWS figure (Medium confidence)\n"
        "   - trough < 50% of average → **spiky**, clamp the commitment to the "
        "measured floor (Low confidence)"
    )
    add("")
    add(
        "   Savings scale linearly with commitment size at a fixed discount "
        "rate, so a trimmed commitment carries proportionally trimmed savings."
    )
    add(
        "3. **Reconciliation.** Cost Optimization Hub runs an independent "
        "recommendation pipeline over the same billing data. Its commitment "
        "recommendations are summed and compared to the Cost Explorer total; a "
        "gap over 30% is flagged rather than averaged away."
    )
    add(
        "4. **Posture gate.** Coverage and utilization of *existing* "
        "commitments are checked first. Utilization below 95% is reported as a "
        "blocker, because buying on top of an under-used commitment compounds "
        "waste instead of saving money."
    )
    add("")

    # ------------------------------------------------------------- errors
    if errors:
        add("## Collection warnings")
        add("")
        add(
            "These queries failed and are excluded from the totals above. The "
            "report is therefore a lower bound on the opportunity."
        )
        add("")
        add("| Query | Error | Message |")
        add("|---|---|---|")
        for e in errors:
            msg = e.get("error", "").replace("|", "\\|")[:160]
            add(f"| {e['query']} | {e.get('error_code', '')} | {msg} |")
        add("")

    add("---")
    add("")
    add(
        "*Commitments are non-cancellable financial obligations. Verify the "
        "figures in the AWS console before purchasing, and confirm the "
        "underlying workload is not scheduled for migration, "
        "re-architecture, or decommissioning within the commitment term.*"
    )
    add("")

    return "\n".join(out)
