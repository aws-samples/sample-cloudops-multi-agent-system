"""Render the commitment analysis as a markdown report.

Report order is deliberate: posture blockers come before recommendations, so a
reader cannot skim the savings number without first seeing that existing
commitments are under-utilized.
"""

from __future__ import annotations

import math
from typing import Any

from .analyze import TERM_MONTHS, UNIT_SP, Finding, LineItem
from .shape import LINE_ITEM_LIMIT, no_data_only
from .expiry import EXPIRY_SOON_DAYS, EXPIRY_URGENT_DAYS
from .pools import POOL_LABELS, ROLE_ALTERNATIVE, ROLE_DO_NOT_BUY, ROLE_RECOMMENDED, recommended

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
ROLE_LABELS = {
    ROLE_RECOMMENDED: "**Recommended**",
    ROLE_ALTERNATIVE: "Alternative — do not stack",
    ROLE_DO_NOT_BUY: "**Do not buy**",
}

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
    "not-comparable": (
        "Not compared — Cost Optimization Hub prices commitments at a term and "
        "payment option this sweep did not include."
    ),
    "unavailable": (
        "Not reconciled — Cost Optimization Hub could not be queried, so these "
        "figures rest on Cost Explorer alone."
    ),
}

ACTION_LABELS = {
    "renew": "**renew**",
    "renew-smaller": "**renew smaller**",
    "let-lapse": "let lapse",
    "review": "review",
    "renewal-queued": "already queued",
}


def _money(value: float, currency: str = "USD") -> str:
    if currency in ("", "USD"):
        return f"${value:,.2f}"
    return f"{value:,.2f} {currency}"


def _pct(value: float) -> str:
    return f"{value:.1f}%"


def _months(value: float | None) -> str:
    if value is None:
        return "—"
    return "never" if not math.isfinite(value) else f"{value:.1f} months"


def _hourly(value: float, currency: str = "USD") -> str:
    if currency in ("", "USD"):
        return f"${value:,.4f}/hr"
    return f"{value:,.4f} {currency}/hr"


def _commitment_str(f: Finding, value: float) -> str:
    if f.family == "savings-plan":
        return _hourly(value, f.currency)
    return f"{value:,.0f} unit(s)"


def _size_str(item: dict[str, Any]) -> str:
    """Format a commitment's size in its own unit.

    Savings Plans commit in dollars per hour and reservations in unit counts.
    Printing an hourly-dollar commitment as a bare number misreads it by
    roughly 1000x, so the unit is never dropped.
    """
    quantity = item.get("quantity", 0.0)
    if item.get("unit") == UNIT_SP:
        return _hourly(quantity)
    return f"{quantity:,.0f} unit(s)"


def _line_quantity(value: float | None, unit: str, currency: str) -> str:
    if value is None:
        return "unknown"
    return _hourly(value, currency) if unit == UNIT_SP else f"{value:,.0f}"


def _line_spec(item: LineItem) -> str:
    """The spec cell, with the flags that change what you should buy.

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
    if item.do_not_buy:
        flags.append("**cannot pay back — excluded**")
    if item.account_id:
        flags.append(f"account {item.account_id}")
    return item.spec + (f" ({', '.join(flags)})" if flags else "")


def _line_items_table(add: Any, f: Finding) -> None:
    """Render what to actually buy, one row per purchasable line.

    The family total above is the ranking figure; a reservation applies only to
    usage matching its exact instance type, deployment option and Availability
    Zone, so without this table the recommendation cannot be acted on. Capped
    at LINE_ITEM_LIMIT rows, largest achievable savings first.
    """
    items = [i for i in f.line_items if i.recommended > 0]
    if not items:
        return
    shown = items[:LINE_ITEM_LIMIT]
    add("Line items — what to buy:")
    add("")
    floor_label = "Floor (SP $/hr)" if f.family == "savings-plan" else "Floor"
    add(
        f"| Buy | Region | AWS size | {floor_label} | Achievable | Confidence "
        "| Achievable savings/mo | AWS best case/mo |"
    )
    add("|---|---|---:|---:|---:|---|---:|---:|")
    for item in shown:
        add(
            f"| {_line_spec(item)} | {item.region or '—'} "
            f"| {_line_quantity(item.recommended, item.unit, f.currency)} "
            f"| {_line_quantity(item.floor, item.unit, f.currency)} "
            f"| {_line_quantity(item.achievable, item.unit, f.currency)} "
            f"| {item.confidence} "
            f"| {_money(item.monthly_savings, f.currency)} "
            f"| {_money(item.aws_monthly_savings, f.currency)} |"
        )
    add("")
    hidden = items[LINE_ITEM_LIMIT:]
    if hidden:
        add(
            f"{len(hidden)} smaller line item(s) not shown, together "
            f"{_money(sum(i.monthly_savings for i in hidden), f.currency)}/mo "
            "achievable. They are included in the totals above."
        )
        add("")
    add(
        "*Floor* is the level that line never dropped below during the "
        "lookback — for a Savings Plan, the quietest hour's on-demand spend "
        "converted to commitment dollars. *Achievable* is sized against that "
        "line's own floor."
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
    horizon = expiry["horizon_days"]
    urgent_days = expiry.get("urgent_days", EXPIRY_URGENT_DAYS)
    soon_days = expiry.get("soon_days", EXPIRY_SOON_DAYS)
    add(
        f"Inventory taken {expiry['as_of']} over a {horizon}-day horizon. "
        f"Reservation regions swept: {regions}. Savings Plans are account-level "
        "and are listed once regardless of region."
    )
    add("")

    expiring = expiry.get("expiring") or []
    expired = expiry.get("expired") or []
    counts = expiry.get("counts", {})

    if not expiring:
        add(
            f"**No commitment expires within {horizon} days.** "
            f"{expiry.get('total_active', 0)} active commitment(s) were "
            "inventoried."
        )
        add("")
    else:
        add(
            f"**{len(expiring)} commitment(s) expire within {horizon} days** — "
            f"{counts.get('urgent', 0)} within {urgent_days} days, "
            f"{counts.get('soon', 0)} within {soon_days}, "
            f"{counts.get('upcoming', 0)} within {horizon}."
        )
        add("")
        monthly = expiry.get("monthly_committed_spend_expiring", 0.0)
        units = expiry.get("reserved_units_expiring", 0.0)
        exposure = []
        if monthly:
            exposure.append(
                f"{_money(monthly)}/mo of committed Savings Plan spend "
                f"({_hourly(expiry.get('hourly_commitment_expiring', 0.0))}) "
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
            "> Utilization is the service-level figure from Cost Explorer — all "
            "of the account's RDS reservations share one RDS figure, all Savings "
            "Plans one SP figure. No API reports utilization for an individual "
            "commitment, so confirm a specific one in the console before acting."
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

    queued = expiry.get("queued") or []
    if queued:
        add(
            f"{len(queued)} queued purchase(s) found (scheduled, not yet active): "
            + ", ".join(
                f"`{q.get('commitment_id') or '(no id)'}` {q.get('label', '')} "
                f"{q.get('spec') or ''} from {q.get('start') or '?'}".rstrip()
                for q in queued
            )
            + "."
        )
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


def _bottom_line(add: Any, data: dict[str, Any], findings: list[Finding], currency: str) -> None:
    posture = data["posture"]
    errors = data.get("errors", [])
    chosen = recommended(findings)
    add("## Bottom line")
    add("")
    # An expiry inside the urgent band outranks a purchase recommendation: it
    # has a deadline attached, and missing it silently raises the bill.
    expiry = data.get("expiry") or {}
    urgent = expiry.get("counts", {}).get("urgent", 0)
    if urgent:
        add(
            f"**{urgent} existing commitment(s) expire within "
            f"{expiry.get('urgent_days', EXPIRY_URGENT_DAYS)} days.** That "
            "deadline comes before any new purchase — see *Commitment expiry "
            "and renewal*."
        )
        add("")
    if not chosen:
        complete = not errors or no_data_only(errors)
        if complete:
            add(
                "**No commitment opportunity found.** Cost Explorer recommends no "
                "Savings Plan or Reserved Instance purchase that pays back, for "
                "this account at the requested term and payment options."
            )
            add("")
            add(
                "This is a real result, not a failure — see *Eligible spend* "
                "below for whether the account simply has no "
                "commitment-addressable usage."
            )
        else:
            add(
                f"**No opportunity was found among the queries that succeeded, "
                f"but {len(errors)} quer{'y' if len(errors) == 1 else 'ies'} "
                "failed** — see *Collection warnings*. This is an incomplete "
                "result, not evidence that there is nothing to buy."
            )
        add("")
        return

    total_api = sum(f.api_monthly_savings for f in chosen)
    total_safe = sum(f.safe_monthly_savings for f in chosen)
    total_high = sum(f.safe_monthly_savings for f in chosen if f.confidence == "High")
    add(
        f"| Measure | Monthly | Annual |\n"
        f"|---|---:|---:|\n"
        f"| AWS best-case savings (as the console shows) | {_money(total_api, currency)} "
        f"| {_money(total_api * 12, currency)} |\n"
        f"| **Risk-adjusted achievable savings** | **{_money(total_safe, currency)}** "
        f"| **{_money(total_safe * 12, currency)}** |\n"
        f"| High-confidence subset only | {_money(total_high, currency)} "
        f"| {_money(total_high * 12, currency)} |"
    )
    add("")
    add(
        f"Totals count one purchase per usage pool ({len(chosen)} recommended "
        "commitment(s)). Cost Explorer sizes Compute SPs, EC2 Instance SPs and "
        "EC2 RIs — and Database SPs and database RIs — independently against "
        "the same usage, so the alternatives listed below are *instead of*, not "
        "*in addition to*, the recommended rows."
    )
    add("")
    if total_api > 0:
        haircut = (1 - total_safe / total_api) * 100
        add(
            f"The risk-adjusted figure is {haircut:.0f}% below the AWS best case. "
            "That gap is unused-commitment risk on variable workloads — see "
            "*Method* for how it is derived."
        )
        add("")
    if posture["blockers"]:
        add(
            "**Do not act on these numbers yet.** "
            f"{len(posture['blockers'])} blocker(s) on existing commitments "
            "must be resolved first — see the next section."
        )
        add("")
    elif posture.get("unmeasured"):
        add(
            "**Existing commitment health could not be fully measured** "
            f"({', '.join(posture['unmeasured'])}). Treat these figures as "
            "unvalidated against current commitments."
        )
        add("")


def _reconciliation(add: Any, recon: dict[str, Any], currency: str) -> None:
    add("## Reconciliation against AWS native tools")
    add("")
    add(RECONCILE_VERDICTS.get(recon["status"], recon["status"]))
    add("")
    if recon["status"] == "unavailable":
        add(f"Reason: {recon.get('reason', 'unknown')}")
        add("")
        return
    basis = recon.get("basis") or {}
    if basis:
        add(
            f"Compared at {TERM_LABELS.get(basis.get('term'), basis.get('term'))}, "
            f"{PAYMENT_LABELS.get(basis.get('payment'), basis.get('payment'))} — "
            "the commitment preference Cost Optimization Hub prices at — at the "
            "AWS best case, one option per usage pool on both sides."
        )
        add("")
    if recon["status"] == "not-comparable":
        add(f"Reason: {recon.get('reason', '')}")
        add("")
    add("| Pipeline | Recommended monthly savings |")
    add("|---|---:|")
    add(f"| Cost Explorer purchase recommendations | {_money(recon['ce_monthly_savings'], currency)} |")
    add(f"| Cost Optimization Hub ({recon['coh_count']} commitment recs) | {_money(recon['coh_monthly_savings'], currency)} |")
    if recon.get("delta") is not None:
        add(f"| Delta | {_money(recon['delta'], currency)} ({recon['delta_pct']}%) |")
    add("")
    for note in recon.get("notes", []):
        add(f"> {note}")
    if recon.get("notes"):
        add("")
    if recon.get("coh_by_resource_type"):
        add("Cost Optimization Hub breakdown by commitment type:")
        add("")
        add("| Resource type | Monthly savings |")
        add("|---|---:|")
        for rtype, amount in recon["coh_by_resource_type"].items():
            add(f"| {rtype} | {_money(amount, currency)} |")
        add("")


def _posture(add: Any, posture: dict[str, Any]) -> None:
    add("## Existing commitment health")
    add("")
    if posture["blockers"]:
        add("### Blockers")
        add("")
        for b in posture["blockers"]:
            add(f"- **{b}**")
        add("")

    rows: list[tuple[str, Any, str]] = [
        ("Savings Plans coverage", posture.get("sp_coverage_pct"), "%"),
        ("Savings Plans utilization", posture.get("sp_utilization_pct"), "%"),
        ("Unused SP commitment", posture.get("sp_unused_commitment"), "$"),
    ]
    for label, entry in sorted((posture.get("reservations") or {}).items()):
        rows += [
            (f"{label} reservation coverage", entry.get("coverage_pct"), "%"),
            (f"{label} reservation utilization", entry.get("utilization_pct"), "%"),
            (f"{label} unused reservation hours", entry.get("unused_hours"), "h"),
            (f"{label} realized reservation savings", entry.get("realized_savings"), "$"),
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
            shown = _pct(val) if unit == "%" else _money(val) if unit == "$" else f"{val:,.0f}"
            add(f"| {label} | {shown} |")
        add("")
        add(
            "Reservation figures are queried per service — Cost Explorer reports "
            "EC2 only unless a service is named."
        )
        add("")
    elif posture.get("unmeasured"):
        add(
            "Existing commitment posture could not be measured — the coverage "
            "and utilization queries failed. Treat the recommendations below as "
            "unvalidated against current commitments."
        )
        add("")

    for note in posture.get("notes", []):
        add(f"> {note}")
    if posture.get("notes"):
        add("")


def _finding_detail(add: Any, i: int, f: Finding) -> None:
    add(
        f"#### {i}. {f.label} — {TERM_LABELS.get(f.term, f.term)}, "
        f"{PAYMENT_LABELS.get(f.payment, f.payment)} "
        f"({ROLE_LABELS.get(f.role, f.role).replace('**', '')})"
    )
    add("")
    if f.role == ROLE_ALTERNATIVE:
        add(
            f"- **Alternative** for the {POOL_LABELS.get(f.pool, f.pool)} usage "
            "pool. It discounts the same usage as the recommended row; buy one "
            "or the other, never both."
        )
    add(f"- **Confidence:** {f.confidence} (spend profile: {f.volatility})")
    add(
        f"- **Commitment:** AWS recommends "
        f"{_commitment_str(f, f.api_hourly_commitment)}; this report "
        f"recommends {_commitment_str(f, f.safe_hourly_commitment)}"
    )
    add(
        f"- **Savings:** {_money(f.safe_monthly_savings, f.currency)}/mo achievable "
        f"({_money(f.api_monthly_savings, f.currency)}/mo at the AWS best case)"
    )
    if f.aws_upfront_cost > 0:
        add(
            f"- **Upfront cost:** {_money(f.upfront_cost, f.currency)} at the "
            f"achievable size ({_money(f.aws_upfront_cost, f.currency)} at the AWS size)"
        )
    if f.break_even_months is not None:
        term_months = TERM_MONTHS.get(f.term)
        # Break-even past the end of the term means the upfront payment never
        # returns — say so on the same line as the number.
        if f.do_not_buy and term_months:
            add(
                f"- **Break-even:** {_months(f.break_even_months)} — **longer "
                f"than the {term_months}-month term, so this purchase cannot "
                "pay back**"
            )
        else:
            add(f"- **Cash break-even:** {_months(f.break_even_months)}")
    if f.waste_exposure_monthly > 0:
        add(
            f"- **Waste exposure at the recommended size:** up to "
            f"{_money(f.waste_exposure_monthly, f.currency)}/mo unused if usage "
            "falls to its observed floor"
        )
    if f.aws_waste_exposure_monthly > f.waste_exposure_monthly:
        add(
            f"- **Waste exposure at the AWS size:** up to "
            f"{_money(f.aws_waste_exposure_monthly, f.currency)}/mo"
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


def _recommendations(add: Any, findings: list[Finding]) -> None:
    add("## Recommended commitments")
    add("")
    if not findings:
        add("None. See *Bottom line* above.")
        add("")
        return
    add(
        "| # | Commitment | Role | Term | Payment | AWS commitment | "
        "Achievable commitment | Achievable savings/mo | Discount | Confidence |"
    )
    add("|---|---|---|---|---|---:|---:|---:|---:|---|")
    for i, f in enumerate(findings, 1):
        add(
            f"| {i} | {f.label} | {ROLE_LABELS.get(f.role, f.role)} "
            f"| {TERM_LABELS.get(f.term, f.term)} "
            f"| {PAYMENT_LABELS.get(f.payment, f.payment)} "
            f"| {_commitment_str(f, f.api_hourly_commitment)} "
            f"| {_commitment_str(f, f.safe_hourly_commitment)} "
            f"| {_money(f.safe_monthly_savings, f.currency)} "
            f"| {_pct(f.savings_percentage)} | {f.confidence} |"
        )
    add("")
    add("### Detail")
    add("")
    for i, f in enumerate(findings, 1):
        _finding_detail(add, i, f)


def _eligible_spend(add: Any, spend: dict[str, Any]) -> None:
    add("## Eligible spend")
    add("")
    periods = spend.get("periods") or []
    currency = spend.get("currency", "USD")
    if spend.get("error"):
        add(f"Could not retrieve spend: {spend['error']}")
    elif periods:
        add("| Period | Total unblended spend |")
        add("|---|---:|")
        for p in periods:
            add(f"| {p['start']} → {p['end']} | {_money(p['total'], currency)} |")
        add("")
        latest = periods[-1]
        top = sorted(latest["by_service"].items(), key=lambda kv: -kv[1])[:10]
        add(f"Top services, {latest['start']} → {latest['end']}:")
        add("")
        add("| Service | Spend |")
        add("|---|---:|")
        for svc, amount in top:
            add(f"| {svc} | {_money(amount, currency)} |")
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


METHOD = (
    "1. **Purchase recommendations** are pulled from Cost Explorer for every "
    "requested (term, payment) permutation, every page of detail rows included. "
    "The strongest permutation per commitment type is kept.\n"
    "2. **Risk adjustment, line by line.** Each purchasable line — one "
    "specification, or one account under LINKED scope — is sized against its "
    "own *minimum* and *average* hourly usage:\n\n"
    "   - trough ≥ 80% of average → **stable**, take the AWS figure as-is "
    "(High confidence)\n"
    "   - trough ≥ 50% of average → **moderate**, commit to the midpoint of "
    "floor and AWS figure (Medium confidence)\n"
    "   - trough < 50% of average → **spiky**, clamp the commitment to the "
    "measured floor (Low confidence)\n"
    "   - floor not returned → keep the AWS figure, unvalidated (Low confidence)\n\n"
    "   A Savings Plan floor is converted from on-demand dollars to commitment "
    "dollars first, at that recommendation's own effective rate: $1/hr of "
    "on-demand spend at a 40% discount absorbs only $0.60/hr of commitment. "
    "Savings scale linearly with commitment size at a fixed rate.\n"
    "3. **Cash break-even** is upfront ÷ (monthly net savings + upfront ÷ term "
    "months) — AWS's savings figure is already net of the amortized upfront. A "
    "line that cannot pay back within its term is excluded.\n"
    "4. **One purchase per usage pool.** Compute SPs, EC2 Instance SPs and EC2 "
    "RIs discount the same EC2 usage; Database SPs and RDS/ElastiCache/DynamoDB "
    "RIs the same database usage. Within each pool the option with the most "
    "achievable savings is recommended and the rest are alternatives — totals "
    "never add them together.\n"
    "5. **Reconciliation.** Cost Optimization Hub's commitment recommendations "
    "are compared at its own preferred term and payment, one option per pool, "
    "excluding types it does not publish; a gap over 30% is flagged rather than "
    "averaged away.\n"
    "6. **Posture gate.** Coverage and utilization of *existing* commitments "
    "are checked per service first. Utilization below 95% is a blocker, because "
    "buying on top of an under-used commitment compounds waste."
)


def render(data: dict[str, Any]) -> str:
    """Build the full markdown report from a collected+analyzed payload."""
    meta = data["meta"]
    findings: list[Finding] = data["findings"]
    errors = data.get("errors", [])
    currency = next((f.currency for f in findings), "USD")

    out: list[str] = []
    add = out.append

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
    if data.get("queries_run") is not None:
        add(
            f"**Cost Explorer requests:** {data['queries_run']} "
            f"(~{_money(data.get('estimated_api_cost_usd', 0.0))} at $0.01 each)"
        )
    add("")
    add("All data is read-only. This report does not purchase anything.")
    add("")

    _bottom_line(add, data, findings, currency)
    _reconciliation(add, data["reconciliation"], currency)
    _posture(add, data["posture"])
    if data.get("expiry"):
        _expiry_section(add, data["expiry"])
    _recommendations(add, findings)
    _eligible_spend(add, data["eligible_spend"])

    add("## Method")
    add("")
    add(METHOD)
    add("")

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
