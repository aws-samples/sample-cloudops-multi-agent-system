"""What expires when, and what to do about it.

Pure functions over a normalized commitment inventory (see `inventory`);
nothing here calls AWS.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .api import HOURS_PER_MONTH
from .inventory import QUEUED_STATES
from .posture import UTILIZATION_WARN_PCT

# Expiry urgency bands, in days remaining. 30 days is roughly the shortest
# notice on which a renewal can clear finance approval, so it is the point at
# which a lapse becomes a scheduling problem rather than a planning one.
EXPIRY_URGENT_DAYS = 30
EXPIRY_SOON_DAYS = 60
EXPIRY_HORIZON_DAYS = 90

# Renewal verdicts. Deliberately less conservative than the initial-purchase
# bands: a lapsing commitment has zero switching cost, so expiry is the one
# free resize point in a commitment's life. Renewing at the same size is the
# risk; renewing smaller is not.
RENEW = "renew"
RENEW_SMALLER = "renew-smaller"
LET_LAPSE = "let-lapse"
REVIEW = "review"
# A matching purchase is already queued to start when this one ends. Buying
# again would double-commit the same usage for a full term.
RENEWAL_QUEUED = "renewal-queued"

VERDICTS = (RENEW, RENEW_SMALLER, LET_LAPSE, REVIEW, RENEWAL_QUEUED)

# Utilization below this means the commitment is more waste than saving, so
# re-buying it at the same size compounds a mistake rather than protecting a
# discount.
RENEW_LAPSE_PCT = 50.0


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


def _renewal_key(item: dict[str, Any]) -> tuple:
    """What a queued purchase has to share with an expiring one to renew it.

    The same family, service, region and specification: a queued
    db.r6g.large Single-AZ reservation does not renew a Multi-AZ one, and a
    queued Compute Savings Plan does not renew an EC2 Instance plan.
    """
    return (
        item.get("family"),
        item.get("service"),
        item.get("label"),
        item.get("region"),
        item.get("instance_type"),
        item.get("spec"),
    )


def _queued_rationale(item: dict[str, Any], queued: list[dict[str, Any]]) -> str:
    covered = sum(q.get("quantity", 0.0) for q in queued)
    ids = ", ".join(f"`{q.get('commitment_id') or '(no id)'}`" for q in queued)
    starts = min((q.get("start") or "" for q in queued), default="")
    text = (
        f"A queued purchase of the same specification ({ids}) starts "
        f"{starts or 'on a future date'}, so this renewal is already "
        "scheduled. Do not buy it again — a second purchase double-commits the "
        "same usage for a full term."
    )
    quantity = item.get("quantity", 0.0)
    if covered < quantity:
        text += (
            f" The queued purchase covers {covered:,.4g} of the {quantity:,.4g} "
            "expiring; size any top-up from fresh analysis, not by re-buying."
        )
    return text


def analyze_expiry(
    items: list[dict[str, Any]],
    as_of: date,
    horizon_days: int = EXPIRY_HORIZON_DAYS,
    sp_utilization_pct: float | None = None,
    ri_utilization_by_service: dict[str, float | None] | None = None,
) -> dict[str, Any]:
    """Sort a commitment inventory into what expires when, and what to do.

    `as_of` is a parameter rather than `date.today()` so this stays a pure
    function that a test can pin to a fixed day.

    Utilization arrives per family because Cost Explorer only reports it in
    aggregate — there is no per-commitment utilization API, so every Savings
    Plan shares the account's SP figure and every RDS reservation shares the
    RDS figure. `ri_utilization_by_service` is keyed by inventory family
    (`ec2`, `rds`, ...): one filtered query per service, because an
    unfiltered reservation-utilization query reports EC2 only.

    Queued purchases are matched against what is expiring before any verdict
    is final, so an already-scheduled renewal reads `renewal-queued` instead
    of `renew`.

    Anything already past its end date but still listed as active is reported
    separately: the commitment is gone and the spend it covered is already back
    at on-demand rates, which is a different (and more urgent) conversation
    than a renewal.
    """
    by_service = ri_utilization_by_service or {}
    queued = [i for i in items if str(i.get("state", "")).lower() in QUEUED_STATES]
    active = [i for i in items if str(i.get("state", "")).lower() not in QUEUED_STATES]
    queued_by_key: dict[tuple, list[dict[str, Any]]] = {}
    for q in queued:
        queued_by_key.setdefault(_renewal_key(q), []).append(dict(q))

    expiring: list[dict[str, Any]] = []
    expired: list[dict[str, Any]] = []
    undated: list[dict[str, Any]] = []

    for item in active:
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
            else by_service.get(item.get("service", ""))
        )
        action, rationale = _renewal_verdict(item, utilization)
        matches = queued_by_key.get(_renewal_key(item), [])
        if matches:
            action, rationale = RENEWAL_QUEUED, _queued_rationale(item, matches)
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
    actions = {verdict: 0 for verdict in VERDICTS}
    hourly_commitment = 0.0
    reserved_units = 0.0
    for entry in expiring:
        counts[entry["urgency"]] = counts.get(entry["urgency"], 0) + 1
        actions[entry["action"]] = actions.get(entry["action"], 0) + 1
        # A queued renewal keeps the discount, so it is not exposure.
        if entry["action"] == RENEWAL_QUEUED:
            continue
        if entry.get("family") == "savings-plan":
            hourly_commitment += entry.get("quantity", 0.0)
        else:
            reserved_units += entry.get("quantity", 0.0)

    return {
        "as_of": as_of.isoformat(),
        "horizon_days": horizon_days,
        "urgent_days": EXPIRY_URGENT_DAYS,
        "soon_days": EXPIRY_SOON_DAYS,
        "total_active": len(active),
        "expiring": expiring,
        "expired": expired,
        "undated": undated,
        "queued": queued,
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
