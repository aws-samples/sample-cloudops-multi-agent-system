"""Tests for the risk-adjustment, selection, posture and reconciliation logic.

Fixtures are built from a real discount rather than hand-picked numbers. An
earlier version set the commitment equal to average on-demand spend (a 0%
discount) and picked upfront figures unrelated to the term — fixtures that
happily locked in two pricing errors instead of catching them:

* break-even computed as upfront ÷ NET savings, which fails every All-Upfront
  plan with a discount under 50%;
* a Savings Plan commitment (priced at SP rates) compared directly against an
  on-demand-dollar floor.

`sp_rec` therefore derives every field from (commitment, discount,
utilization) the way Cost Explorer does, and `ri_rec` derives upfront from the
term. Money and percentages are STRINGS, as Cost Explorer returns them.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from commitments.analyze import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    analyze_ri_recommendation,
    analyze_sp_recommendation,
    classify_volatility,
)
from commitments.pools import (
    ROLE_ALTERNATIVE,
    ROLE_DO_NOT_BUY,
    ROLE_RECOMMENDED,
    plan_purchases,
    recommended,
    select_best_findings,
)
from commitments.posture import (
    assess_existing_posture,
    reconcile_with_coh,
    safe_to_buy_more,
)
from commitments.report import render

HOURS = 730.0
TERM_MONTHS = {"ONE_YEAR": 12, "THREE_YEARS": 36}
EC2 = "Amazon Elastic Compute Cloud - Compute"
RDS = "Amazon Relational Database Service"


def sp_rec(
    floor_od: float | None,
    avg_od: float | None,
    *,
    hourly: float = 10.0,
    discount: float = 0.30,
    util: float = 100.0,
    term: str = "ONE_YEAR",
    payment: str = "NO_UPFRONT",
    sp_type: str = "COMPUTE_SP",
    with_sp_cost: bool = True,
    currency: str = "USD",
) -> dict:
    """An SP recommendation priced the way Cost Explorer prices one.

    A commitment of $c/hr at discount d and utilization u covers on-demand
    usage worth c·u/(1−d) an hour, and saves that minus c. Upfront follows the
    payment option: the full term's commitment for All Upfront, half for
    Partial. AWS's monthly savings figure is NET of the amortized upfront.
    """
    sp_cost = hourly * HOURS
    covered = sp_cost * (util / 100.0) / (1 - discount)
    savings = covered - sp_cost
    months = TERM_MONTHS[term]
    upfront = {"NO_UPFRONT": 0.0, "PARTIAL_UPFRONT": 0.5, "ALL_UPFRONT": 1.0}[payment] * sp_cost * months
    detail = {
        "HourlyCommitmentToPurchase": str(hourly),
        "EstimatedMonthlySavingsAmount": str(savings),
        "UpfrontCost": str(upfront),
        "EstimatedAverageUtilization": str(util),
        "CurrencyCode": currency,
        "CurrentMinimumHourlyOnDemandSpend": "" if floor_od is None else str(floor_od),
        "CurrentAverageHourlyOnDemandSpend": "" if avg_od is None else str(avg_od),
    }
    if with_sp_cost:
        detail["EstimatedSPCost"] = str(sp_cost)
        detail["EstimatedSavingsAmount"] = str(savings)
    return {
        "sp_type": sp_type,
        "term": term,
        "payment": payment,
        "lookback": "THIRTY_DAYS",
        "account_scope": "PAYER",
        "summary": {
            "HourlyCommitmentToPurchase": str(hourly),
            "EstimatedMonthlySavingsAmount": str(savings),
            "EstimatedSavingsPercentage": str(discount * 100),
            "CurrencyCode": currency,
        },
        "details": [detail],
    }


def ri_line(
    recommended_units: float,
    floor: float | str,
    average: float,
    *,
    monthly_savings: float,
    unit_monthly_cost: float = 100.0,
    payment: str = "NO_UPFRONT",
    term: str = "ONE_YEAR",
    break_even: str | None = None,
    instance_type: str = "db.r6g.large",
) -> dict:
    """One RI detail. Upfront and recurring derive from a per-unit monthly cost."""
    months = TERM_MONTHS[term]
    total = unit_monthly_cost * recommended_units
    share = {"NO_UPFRONT": 0.0, "PARTIAL_UPFRONT": 0.5, "ALL_UPFRONT": 1.0}[payment]
    detail = {
        "RecommendedNumberOfInstancesToPurchase": str(recommended_units),
        "MinimumNumberOfInstancesUsedPerHour": str(floor),
        "AverageNumberOfInstancesUsedPerHour": str(average),
        "EstimatedMonthlySavingsAmount": str(monthly_savings),
        "UpfrontCost": str(total * share * months),
        "RecurringStandardMonthlyCost": str(total * (1 - share)),
        "InstanceDetails": {
            "RDSInstanceDetails": {
                "InstanceType": instance_type,
                "DeploymentOption": "Multi-AZ",
                "Region": "us-east-1",
            }
        },
    }
    if break_even is not None:
        detail["EstimatedBreakEvenInMonths"] = break_even
    return detail


def ri_rec(details: list[dict], *, service: str = RDS, label: str = "RDS",
           term: str = "ONE_YEAR", payment: str = "NO_UPFRONT") -> dict:
    return {
        "service": service,
        "label": label,
        "term": term,
        "payment": payment,
        "summary": {
            "TotalEstimatedMonthlySavingsAmount": str(
                sum(float(d["EstimatedMonthlySavingsAmount"]) for d in details)
            ),
            "TotalEstimatedMonthlySavingsPercentage": "31.0",
        },
        "details": details,
    }


# --------------------------------------------------------------- volatility


@pytest.mark.unit
@pytest.mark.parametrize(
    "floor,average,expected",
    [
        (9.0, 10.0, "stable"),
        (8.0, 10.0, "stable"),
        (7.9, 10.0, "moderate"),
        (5.0, 10.0, "moderate"),
        (4.9, 10.0, "spiky"),
        (0.0, 10.0, "spiky"),
        (5.0, 0.0, "unknown"),
        (None, 10.0, "unknown"),
        (5.0, None, "unknown"),
    ],
)
def test_classify_volatility_bands(floor, average, expected):
    label, _ = classify_volatility(floor, average)
    assert label == expected


# ------------------------------------------- SP sizing in commitment dollars


@pytest.mark.unit
def test_stable_workload_keeps_api_recommendation():
    f = analyze_sp_recommendation(sp_rec(floor_od=13.0, avg_od=15.0))
    assert f.confidence == CONFIDENCE_HIGH
    assert f.safe_hourly_commitment == pytest.approx(10.0)
    assert f.safe_monthly_savings == pytest.approx(f.api_monthly_savings)


@pytest.mark.unit
def test_floor_is_converted_to_commitment_dollars_before_it_caps():
    """A $9/hr on-demand trough at 30% off absorbs only $6.30/hr of commitment."""
    f = analyze_sp_recommendation(sp_rec(floor_od=9.0, avg_od=15.0))
    [line] = f.line_items
    assert line.floor_on_demand == pytest.approx(9.0)
    assert line.floor == pytest.approx(6.3)
    assert line.average == pytest.approx(10.5)
    # 0.6 ratio → moderate → midpoint of 6.30 and 10.00.
    assert f.confidence == CONFIDENCE_MEDIUM
    assert f.safe_hourly_commitment == pytest.approx(8.15)
    assert f.safe_monthly_savings == pytest.approx(f.api_monthly_savings * 0.815)


@pytest.mark.unit
def test_a_commitment_below_the_on_demand_floor_can_still_strand_spend():
    """The reviewer's case: 3-year, 55% off, $4/hr floor, $3.60/hr commitment.

    In on-demand dollars $3.60 < $4 looks safe. In commitment dollars the
    trough absorbs $4 × 0.45 = $1.80/hr, so $1.80/hr — $1,314/mo — of the AWS
    commitment sits unused in the quietest hour.
    """
    f = analyze_sp_recommendation(
        sp_rec(floor_od=4.0, avg_od=8.0, hourly=3.6, discount=0.55, term="THREE_YEARS")
    )
    [line] = f.line_items
    assert line.floor == pytest.approx(1.8)
    assert f.aws_waste_exposure_monthly == pytest.approx(1.8 * HOURS)
    assert f.safe_hourly_commitment < 3.6


@pytest.mark.unit
def test_spiky_workload_clamps_to_the_commitment_dollar_floor():
    f = analyze_sp_recommendation(sp_rec(floor_od=4.0, avg_od=15.0))
    assert f.confidence == CONFIDENCE_LOW
    assert f.safe_hourly_commitment == pytest.approx(2.8)
    assert f.waste_exposure_monthly == pytest.approx(0.0)
    assert f.aws_waste_exposure_monthly == pytest.approx((10.0 - 2.8) * HOURS)


@pytest.mark.unit
def test_waste_is_reported_at_both_the_recommended_and_aws_size():
    f = analyze_sp_recommendation(sp_rec(floor_od=13.0, avg_od=15.0))
    # Stable keeps $10; the trough absorbs $9.10.
    assert f.waste_exposure_monthly == pytest.approx(0.9 * HOURS)
    assert f.aws_waste_exposure_monthly == pytest.approx(0.9 * HOURS)


@pytest.mark.unit
def test_safe_commitment_never_exceeds_api_figure():
    f = analyze_sp_recommendation(sp_rec(floor_od=50.0, avg_od=55.0))
    assert f.safe_hourly_commitment <= 10.0
    assert f.safe_monthly_savings <= f.api_monthly_savings


@pytest.mark.unit
def test_an_unreturned_floor_keeps_the_aws_figure_at_low_confidence():
    """Blank is unknown, not zero — and unknown is no basis for a trim."""
    f = analyze_sp_recommendation(sp_rec(floor_od=None, avg_od=None))
    assert f.safe_hourly_commitment == pytest.approx(10.0)
    assert f.confidence == CONFIDENCE_LOW
    assert f.volatility == "unknown"
    assert any("did not return the hourly usage floor" in n for n in f.rationale)


@pytest.mark.unit
def test_a_measured_zero_floor_is_a_real_trough():
    f = analyze_sp_recommendation(sp_rec(floor_od=0.0, avg_od=15.0))
    assert f.volatility == "spiky"
    assert f.safe_hourly_commitment == pytest.approx(0.0)


@pytest.mark.unit
def test_without_sp_cost_the_floor_is_not_misused_in_on_demand_dollars():
    f = analyze_sp_recommendation(sp_rec(floor_od=4.0, avg_od=15.0, with_sp_cost=False))
    assert f.safe_hourly_commitment == pytest.approx(10.0)
    assert f.confidence == CONFIDENCE_LOW
    assert any("could not be converted" in n for n in f.rationale)


@pytest.mark.unit
def test_average_fallback_divides_by_the_lookback_not_a_month():
    """CurrentOnDemandSpend covers the lookback: 7 days is 168 hours, not 730."""
    rec = sp_rec(floor_od=13.0, avg_od=None)
    rec["lookback"] = "SEVEN_DAYS"
    rec["summary"]["CurrentOnDemandSpend"] = str(15.0 * 168)
    [line] = analyze_sp_recommendation(rec).line_items
    assert line.average == pytest.approx(15.0 * 0.7)


@pytest.mark.unit
def test_zero_recommendation_is_dropped():
    rec = sp_rec(1.0, 1.0)
    rec["summary"]["HourlyCommitmentToPurchase"] = "0"
    rec["summary"]["EstimatedMonthlySavingsAmount"] = "0"
    assert analyze_sp_recommendation(rec) is None


@pytest.mark.unit
def test_errored_recommendation_is_dropped():
    assert analyze_sp_recommendation({"error": "boom", "error_code": "X"}) is None


@pytest.mark.unit
def test_sp_trim_note_uses_dollars_per_hour():
    f = analyze_sp_recommendation(sp_rec(floor_od=9.0, avg_od=15.0))
    trimmed = [n for n in f.rationale if "trimmed to" in n]
    assert "$8.1500/hr" in trimmed[0]


# ------------------------------------------------------ cash break-even (B1)


@pytest.mark.unit
def test_all_upfront_break_even_is_cash_not_net():
    """1-year All Upfront at 28% off, $1,000/mo on demand.

    Upfront $8,640, net savings $280/mo. Dividing by net gives 30.9 months and
    a false "Do not buy"; the cash recovered each month is the full $1,000 of
    avoided on-demand spend, so break-even is 8.6 months.
    """
    hourly = 720.0 / HOURS
    f = analyze_sp_recommendation(
        sp_rec(floor_od=1000 / HOURS, avg_od=1000 / HOURS, hourly=hourly,
               discount=0.28, payment="ALL_UPFRONT")
    )
    assert f.aws_upfront_cost == pytest.approx(8640.0)
    assert f.break_even_months == pytest.approx(8.64)
    assert not f.do_not_buy
    assert not any("Do not buy" in n for n in f.rationale)


@pytest.mark.unit
@pytest.mark.parametrize("discount", [0.10, 0.20, 0.28, 0.40, 0.49])
@pytest.mark.parametrize("term", ["ONE_YEAR", "THREE_YEARS"])
def test_no_all_upfront_plan_that_saves_money_is_rejected(discount, term):
    """Any positive discount pays back inside the term on a flat workload."""
    f = analyze_sp_recommendation(
        sp_rec(floor_od=15.0, avg_od=15.0, discount=discount, term=term, payment="ALL_UPFRONT")
    )
    assert not f.do_not_buy
    assert f.break_even_months == pytest.approx(TERM_MONTHS[term] * (1 - discount))


@pytest.mark.unit
def test_a_plan_that_loses_money_is_do_not_buy():
    rec = sp_rec(floor_od=15.0, avg_od=15.0, payment="ALL_UPFRONT")
    rec["details"][0]["EstimatedMonthlySavingsAmount"] = "-1000"
    rec["details"][0]["UpfrontCost"] = str(10.0 * HOURS * 12 * 2)
    f = analyze_sp_recommendation(rec)
    assert f.do_not_buy
    assert f.safe_monthly_savings == 0
    assert "Do not buy" in f.rationale[0]


@pytest.mark.unit
def test_upfront_is_quoted_at_the_achievable_size():
    f = analyze_sp_recommendation(sp_rec(floor_od=4.0, avg_od=15.0, payment="ALL_UPFRONT"))
    assert f.upfront_cost == pytest.approx(f.aws_upfront_cost * 0.28)


# ------------------------------------------------------------ RI sizing (S9)


@pytest.mark.unit
def test_ri_recommendation_rounds_to_whole_units():
    f = analyze_ri_recommendation(
        ri_rec([ri_line(10, 6, 10, monthly_savings=900.0, payment="ALL_UPFRONT")],
               payment="ALL_UPFRONT")
    )
    assert f.safe_hourly_commitment == 8.0


@pytest.mark.unit
def test_each_ri_line_is_sized_against_its_own_floor():
    """A flat line must not subsidize a spiky one, nor the reverse."""
    f = analyze_ri_recommendation(
        ri_rec([
            ri_line(10, 10, 10, monthly_savings=1000.0, instance_type="db.r6g.large"),
            ri_line(10, 1, 10, monthly_savings=1000.0, instance_type="db.r6g.4xlarge"),
        ])
    )
    by_type = {i.spec.split(" · ")[0]: i for i in f.line_items}
    assert by_type["db.r6g.large"].achievable == 10
    assert by_type["db.r6g.4xlarge"].achievable == 1
    assert f.safe_hourly_commitment == 11
    assert f.safe_monthly_savings == pytest.approx(1000.0 + 100.0)


@pytest.mark.unit
def test_ri_line_with_unreturned_floor_keeps_aws_figure():
    f = analyze_ri_recommendation(ri_rec([ri_line(10, "", 10, monthly_savings=500.0)]))
    [line] = f.line_items
    assert line.floor is None
    assert line.achievable == 10
    assert line.confidence == CONFIDENCE_LOW


@pytest.mark.unit
def test_all_upfront_ri_waste_is_priced_from_the_amortized_upfront():
    """Recurring is $0 on All Upfront; waste priced on recurring alone was $0."""
    f = analyze_ri_recommendation(
        ri_rec([ri_line(10, 5, 10, monthly_savings=500.0, payment="ALL_UPFRONT")],
               payment="ALL_UPFRONT")
    )
    # 5 idle reservations at $100/unit-month amortized.
    assert f.aws_waste_exposure_monthly == pytest.approx(500.0)


@pytest.mark.unit
def test_ri_break_even_is_weighted_by_upfront():
    small = ri_line(1, 1, 1, monthly_savings=50.0, payment="ALL_UPFRONT",
                    unit_monthly_cost=10.0, break_even="2", instance_type="db.t4g.small")
    large = ri_line(10, 10, 10, monthly_savings=900.0, payment="ALL_UPFRONT",
                    unit_monthly_cost=500.0, break_even="11", instance_type="db.r6g.4xlarge")
    f = analyze_ri_recommendation(ri_rec([small, large], payment="ALL_UPFRONT"))
    expected = (2 * 120.0 + 11 * 60000.0) / 60120.0
    assert f.break_even_months == pytest.approx(expected)


@pytest.mark.unit
def test_an_ri_line_that_cannot_pay_back_is_excluded_not_averaged():
    payback = ri_line(4, 4, 4, monthly_savings=400.0, payment="ALL_UPFRONT",
                      break_even="8", instance_type="db.r6g.large")
    loser = ri_line(4, 4, 4, monthly_savings=10.0, payment="ALL_UPFRONT",
                    break_even="40", instance_type="db.m4.large")
    f = analyze_ri_recommendation(ri_rec([payback, loser], payment="ALL_UPFRONT"))
    excluded = [i for i in f.line_items if i.do_not_buy]
    assert [i.spec.split(" · ")[0] for i in excluded] == ["db.m4.large"]
    assert excluded[0].achievable == 0
    assert not f.do_not_buy
    assert f.safe_monthly_savings == pytest.approx(400.0)


@pytest.mark.unit
def test_ri_where_every_line_fails_to_pay_back_is_do_not_buy():
    f = analyze_ri_recommendation(
        ri_rec([ri_line(4, 4, 4, monthly_savings=100.0, payment="ALL_UPFRONT", break_even="40")],
               service="Amazon Redshift", label="Redshift", payment="ALL_UPFRONT")
    )
    assert f.do_not_buy
    assert f.confidence == CONFIDENCE_LOW
    assert "Do not buy" in f.rationale[0]


@pytest.mark.unit
def test_ri_capacity_unit_fallback():
    f = analyze_ri_recommendation({
        "service": "Amazon DynamoDB Service", "label": "DynamoDB",
        "term": "ONE_YEAR", "payment": "NO_UPFRONT",
        "summary": {"TotalEstimatedMonthlySavingsAmount": "400.0"},
        "details": [{
            "RecommendedNumberOfCapacityUnitsToPurchase": "100",
            "MinimumNumberOfCapacityUnitsUsedPerHour": "95",
            "AverageNumberOfCapacityUnitsUsedPerHour": "100",
        }],
    })
    assert f.api_hourly_commitment == pytest.approx(100.0)
    assert f.confidence == CONFIDENCE_HIGH
    # A blank per-line savings figure is filled from the summary, not lost.
    assert f.safe_monthly_savings == pytest.approx(400.0)


@pytest.mark.unit
def test_ri_trim_note_quotes_the_rounded_unit_figure():
    f = analyze_ri_recommendation(ri_rec([ri_line(12, 9, 12, monthly_savings=900.0)]))
    assert f.safe_hourly_commitment == 10.0
    [trimmed] = [n for n in f.rationale if "trimmed to" in n]
    assert "10 unit(s)" in trimmed
    assert "$" not in trimmed


# ----------------------------------------------------- selection and pools


def _sp(sp_type: str, savings_scale: float = 1.0, **kw):
    rec = sp_rec(floor_od=15.0, avg_od=15.0, sp_type=sp_type, **kw)
    for block in (rec["summary"], rec["details"][0]):
        block["EstimatedMonthlySavingsAmount"] = str(
            float(block["EstimatedMonthlySavingsAmount"]) * savings_scale
        )
    return analyze_sp_recommendation(rec)


@pytest.mark.unit
def test_selection_keeps_one_permutation_per_family():
    weak = _sp("COMPUTE_SP", 0.5)
    strong = replace(_sp("COMPUTE_SP", 1.5), term="THREE_YEARS")
    best = select_best_findings([weak, strong])
    assert len(best) == 1
    assert best[0].term == "THREE_YEARS"


@pytest.mark.unit
def test_selection_breaks_ties_toward_shorter_term():
    one_year = _sp("COMPUTE_SP")
    three_year = replace(one_year, term="THREE_YEARS")
    assert select_best_findings([three_year, one_year])[0].term == "ONE_YEAR"


@pytest.mark.unit
def test_a_purchase_that_cannot_pay_back_never_wins_selection():
    viable = _sp("COMPUTE_SP", 0.2)
    loser = replace(_sp("COMPUTE_SP", 3.0), do_not_buy=True, payment="ALL_UPFRONT")
    [best] = select_best_findings([loser, viable])
    assert not best.do_not_buy


@pytest.mark.unit
def test_alternatives_for_the_same_ec2_usage_are_not_summed():
    compute = _sp("COMPUTE_SP", 1.0)
    instance = _sp("EC2_INSTANCE_SP", 1.2)
    ri = analyze_ri_recommendation(
        ri_rec([ri_line(10, 10, 10, monthly_savings=5000.0)], service=EC2, label="EC2")
    )
    planned, pools = plan_purchases(select_best_findings([compute, instance, ri]))
    roles = {f.label: f.role for f in planned}
    assert roles == {
        "EC2": ROLE_RECOMMENDED,
        "EC2 Instance Savings Plan": ROLE_ALTERNATIVE,
        "Compute Savings Plan": ROLE_ALTERNATIVE,
    }
    assert sum(f.safe_monthly_savings for f in recommended(planned)) == pytest.approx(5000.0)
    [compute_pool] = pools
    assert compute_pool["pool"] == "compute"
    assert set(compute_pool["alternatives"]) == {"EC2 Instance Savings Plan", "Compute Savings Plan"}


@pytest.mark.unit
def test_database_reservations_add_up_within_their_option():
    """RDS and ElastiCache RIs cover different usage; together they are one option."""
    rds = analyze_ri_recommendation(ri_rec([ri_line(4, 4, 4, monthly_savings=600.0)]))
    cache = analyze_ri_recommendation(
        ri_rec([ri_line(4, 4, 4, monthly_savings=500.0)], service="Amazon ElastiCache", label="ElastiCache")
    )
    db_sp = _sp("DATABASE_SP")  # ~$3,129/mo, larger than either RI alone
    planned, _ = plan_purchases(select_best_findings([rds, cache, db_sp]))
    roles = {f.label: f.role for f in planned}
    assert roles["Database Savings Plan"] == ROLE_RECOMMENDED
    assert roles["RDS"] == roles["ElastiCache"] == ROLE_ALTERNATIVE

    cheap_sp = _sp("DATABASE_SP", 0.2)  # ~$626/mo, less than RDS + ElastiCache
    planned, _ = plan_purchases(select_best_findings([rds, cache, cheap_sp]))
    roles = {f.label: f.role for f in planned}
    assert roles["RDS"] == roles["ElastiCache"] == ROLE_RECOMMENDED
    assert roles["Database Savings Plan"] == ROLE_ALTERNATIVE


@pytest.mark.unit
def test_independent_pools_both_count():
    compute = _sp("COMPUTE_SP")
    sagemaker = _sp("SAGEMAKER_SP")
    planned, _ = plan_purchases(select_best_findings([compute, sagemaker]))
    assert all(f.role == ROLE_RECOMMENDED for f in planned)


@pytest.mark.unit
def test_do_not_buy_findings_are_never_recommended():
    loser = replace(_sp("COMPUTE_SP"), do_not_buy=True)
    planned, pools = plan_purchases([loser])
    assert planned[0].role == ROLE_DO_NOT_BUY
    assert recommended(planned) == []
    assert pools == []


# ------------------------------------------------------------- posture gate


def _ri_util(pct: str, purchased: str = "720") -> dict:
    return {"total": {"UtilizationPercentage": pct, "PurchasedHours": purchased,
                      "UnusedHours": "50", "RealizedSavings": "300"}}


@pytest.mark.unit
def test_low_sp_utilization_is_a_blocker():
    posture = assess_existing_posture(
        sp_coverage={"periods": [{"Coverage": {"CoveragePercentage": "40.0", "OnDemandCost": "100"}}]},
        sp_utilization={"total": {"Utilization": {"UtilizationPercentage": "72.0", "UnusedCommitment": "500.0", "TotalCommitment": "2000"}}},
        ri_coverage={}, ri_utilization={},
    )
    assert any("72.0% utilized" in b for b in posture["blockers"])
    assert safe_to_buy_more(posture) is False


@pytest.mark.unit
def test_a_measured_zero_percent_utilization_is_a_blocker_not_missing():
    posture = assess_existing_posture(
        sp_coverage={}, sp_utilization={},
        ri_coverage={}, ri_utilization={RDS: _ri_util("0")},
    )
    assert any("RDS reservations are only 0.0% utilized" in b for b in posture["blockers"])


@pytest.mark.unit
def test_reservation_utilization_is_judged_per_service():
    """EC2 at 99% must not vouch for RDS at 60%."""
    posture = assess_existing_posture(
        sp_coverage={}, sp_utilization={},
        ri_coverage={}, ri_utilization={EC2: _ri_util("99"), RDS: _ri_util("60")},
    )
    assert posture["ri_utilization_by_service"] == {EC2: 99.0, RDS: 60.0}
    assert [b for b in posture["blockers"] if "RDS" in b]
    assert not [b for b in posture["blockers"] if "EC2" in b]


@pytest.mark.unit
def test_saturated_coverage_is_a_blocker():
    posture = assess_existing_posture(
        sp_coverage={"periods": [{"Coverage": {"CoveragePercentage": "97.0", "OnDemandCost": "10"}}]},
        sp_utilization={}, ri_coverage={}, ri_utilization={},
    )
    assert any("97.0%" in b for b in posture["blockers"])


@pytest.mark.unit
def test_healthy_posture_has_no_blockers_and_is_safe():
    posture = assess_existing_posture(
        sp_coverage={"periods": [{"Coverage": {"CoveragePercentage": "60.0", "OnDemandCost": "900"}}]},
        sp_utilization={"total": {"Utilization": {"UtilizationPercentage": "99.5", "UnusedCommitment": "1.0", "TotalCommitment": "100"}}},
        ri_coverage={EC2: {"total": {"CoverageHours": {"CoverageHoursPercentage": "55.0", "OnDemandHours": "100"}}}},
        ri_utilization={EC2: _ri_util("99.0")},
    )
    assert posture["blockers"] == []
    assert safe_to_buy_more(posture) is True


@pytest.mark.unit
def test_no_commitments_held_is_safe_not_unmeasured():
    """DataUnavailable means "nothing to measure" — a real, safe answer."""
    no_data = {"error_code": "DataUnavailableException", "error": "No data for this period."}
    posture = assess_existing_posture(no_data, no_data, {RDS: no_data}, {RDS: no_data})
    assert posture["unmeasured"] == []
    assert safe_to_buy_more(posture) is True
    assert all(n.strip() for n in posture["notes"])


@pytest.mark.unit
def test_a_failed_posture_query_is_not_a_passed_gate():
    denied = {"error_code": "AccessDeniedException", "error": "not authorized"}
    posture = assess_existing_posture(denied, denied, {RDS: denied}, {RDS: denied})
    assert posture["blockers"] == []
    assert len(posture["unmeasured"]) == 4
    assert safe_to_buy_more(posture) is None


# ---------------------------------------------------------- reconciliation


def _coh(*pairs: tuple[str, float]) -> dict:
    return {
        "recommendations": [
            {"estimated_monthly_savings": amount, "recommended_resource_type": rtype,
             "current_resource_type": ""}
            for rtype, amount in pairs
        ],
        "count": len(pairs),
    }


PREF_1Y_NU = {"term": "ONE_YEAR", "payment": "NO_UPFRONT"}


@pytest.mark.unit
@pytest.mark.parametrize(
    "scale,expected",
    [(1.0, "reconciled"), (0.95, "reconciled"), (0.8, "minor-variance"), (0.5, "material-variance")],
)
def test_reconciliation_bands(scale, expected):
    f = _sp("COMPUTE_SP")
    result = reconcile_with_coh([f], _coh(("ComputeSavingsPlans", f.api_monthly_savings * scale)), PREF_1Y_NU)
    assert result["status"] == expected


@pytest.mark.unit
def test_reconciliation_agree_zero():
    assert reconcile_with_coh([], _coh(), PREF_1Y_NU)["status"] == "agree-zero"


@pytest.mark.unit
def test_reconciliation_unavailable_when_coh_errors():
    assert reconcile_with_coh([], {"error": "not enrolled"})["status"] == "unavailable"


@pytest.mark.unit
def test_reconciliation_compares_at_cohs_own_term_and_payment():
    one_year = _sp("COMPUTE_SP")
    three_year = replace(_sp("COMPUTE_SP", 2.0), term="THREE_YEARS", payment="ALL_UPFRONT")
    result = reconcile_with_coh(
        [one_year, three_year],
        _coh(("ComputeSavingsPlans", three_year.api_monthly_savings)),
        {"term": "THREE_YEARS", "payment": "ALL_UPFRONT"},
    )
    assert result["status"] == "reconciled"
    assert result["ce_monthly_savings"] == pytest.approx(three_year.api_monthly_savings, abs=0.01)


@pytest.mark.unit
def test_reconciliation_without_a_matching_permutation_is_not_comparable():
    result = reconcile_with_coh(
        [_sp("COMPUTE_SP")], _coh(("ComputeSavingsPlans", 500.0)),
        {"term": "THREE_YEARS", "payment": "ALL_UPFRONT"},
    )
    assert result["status"] == "not-comparable"
    assert result["delta"] is None


@pytest.mark.unit
def test_reconciliation_counts_overlapping_options_once_per_side():
    compute = _sp("COMPUTE_SP")
    instance = _sp("EC2_INSTANCE_SP", 1.1)
    result = reconcile_with_coh(
        [compute, instance],
        _coh(("ComputeSavingsPlans", compute.api_monthly_savings),
             ("Ec2InstanceSavingsPlans", instance.api_monthly_savings)),
        PREF_1Y_NU,
    )
    assert result["ce_monthly_savings"] == pytest.approx(instance.api_monthly_savings, abs=0.01)
    assert result["coh_monthly_savings"] == pytest.approx(instance.api_monthly_savings, abs=0.01)
    assert result["status"] == "reconciled"


@pytest.mark.unit
def test_database_savings_plans_are_excluded_because_coh_has_none():
    db_sp = _sp("DATABASE_SP")
    result = reconcile_with_coh([db_sp], _coh(), PREF_1Y_NU)
    assert result["excluded"] == ["Database Savings Plan"]
    assert result["ce_monthly_savings"] == 0
    assert any("no equivalent" in n for n in result["notes"])


@pytest.mark.unit
def test_reconciliation_flags_different_accounts():
    result = reconcile_with_coh([], _coh(), PREF_1Y_NU, ce_account_id="111111111111",
                                coh_account_id="222222222222")
    assert any("222222222222" in n for n in result["notes"])


# ---------------------------------------------------------------- report


def _payload(findings, posture=None, recon=None, errors=None):
    planned, pools = plan_purchases(select_best_findings(findings))
    return {
        "meta": {
            "account_id": "111122223333",
            "profile": "test",
            "generated_at": "2026-08-07 00:00 UTC",
            "lookback": "THIRTY_DAYS",
            "account_scope": "PAYER",
        },
        "findings": planned,
        "pools": pools,
        "posture": posture or {"blockers": [], "notes": [], "unmeasured": []},
        "reconciliation": recon or {"status": "unavailable", "reason": "test"},
        "eligible_spend": {"periods": [{"start": "2026-07-01", "end": "2026-08-01", "total": 5000.0, "by_service": {EC2: 4000.0}}]},
        "errors": errors or [],
    }


@pytest.mark.integration
def test_report_renders_with_findings():
    md = render(_payload([analyze_sp_recommendation(sp_rec(9.0, 15.0))]))
    assert "# AWS Discounted Commitments Report" in md
    assert "Risk-adjusted achievable savings" in md
    assert "Compute Savings Plan" in md
    assert "below the AWS best case" in md


@pytest.mark.integration
def test_report_renders_the_genuine_empty_case_as_real():
    md = render(_payload([]))
    assert "No commitment opportunity found" in md
    assert "real result, not a failure" in md


@pytest.mark.integration
def test_an_empty_result_with_failed_queries_is_not_called_real():
    md = render(_payload([], errors=[{"query": "SP Compute ONE_YEAR NO_UPFRONT",
                                      "error_code": "AccessDeniedException", "error": "denied"}]))
    assert "real result" not in md
    assert "incomplete result" in md


@pytest.mark.integration
def test_report_marks_alternatives_and_totals_only_the_recommended():
    compute = _sp("COMPUTE_SP")
    instance = _sp("EC2_INSTANCE_SP", 1.2)
    md = render(_payload([compute, instance]))
    assert "Alternative — do not stack" in md
    assert "*instead of*, not *in addition to*" in md
    expected = f"**${instance.safe_monthly_savings:,.2f}**"
    assert expected in md


@pytest.mark.integration
def test_report_surfaces_blockers_before_recommendations():
    posture = {"blockers": ["Existing Savings Plans are only 70.0% utilized"], "notes": [], "unmeasured": []}
    md = render(_payload([_sp("COMPUTE_SP")], posture=posture))
    assert "Do not act on these numbers yet" in md
    assert md.index("Blockers") < md.index("## Recommended commitments")


@pytest.mark.integration
def test_report_says_when_posture_could_not_be_measured():
    posture = {"blockers": [], "notes": [], "unmeasured": ["Savings Plans utilization"]}
    md = render(_payload([_sp("COMPUTE_SP")], posture=posture))
    assert "could not be fully measured" in md


@pytest.mark.integration
def test_report_flags_material_variance():
    recon = {
        "status": "material-variance", "ce_monthly_savings": 1000.0,
        "coh_monthly_savings": 400.0, "delta": 600.0, "delta_pct": 60.0,
        "coh_count": 1, "coh_by_resource_type": {"ComputeSavingsPlans": 400.0},
        "basis": {"term": "THREE_YEARS", "payment": "ALL_UPFRONT"}, "notes": [],
    }
    md = render(_payload([_sp("COMPUTE_SP")], recon=recon))
    assert "MATERIAL VARIANCE" in md
    assert "3-year, All upfront" in md


@pytest.mark.integration
def test_report_zero_posture_does_not_imply_wasted_commitment():
    posture = {"blockers": [], "notes": [], "unmeasured": [], "sp_coverage_pct": 0.0,
               "reservations": {"EC2": {"coverage_pct": 0.0, "utilization_pct": 0.0}}}
    md = render(_payload([], posture=posture))
    assert "nothing to measure" in md
    assert "| Savings Plans coverage | 0.0% |" not in md


@pytest.mark.integration
def test_report_states_never_for_a_plan_that_cannot_pay_back():
    rec = sp_rec(floor_od=15.0, avg_od=15.0, payment="ALL_UPFRONT")
    rec["details"][0]["EstimatedMonthlySavingsAmount"] = "-5000"
    md = render(_payload([analyze_sp_recommendation(rec)]))
    assert "cannot pay back" in md
    assert "never" in md
    assert "**Do not buy**" in md


@pytest.mark.integration
def test_report_uses_the_returned_currency():
    md = render(_payload([_sp("COMPUTE_SP", currency="EUR")]))
    assert " EUR" in md


@pytest.mark.integration
def test_report_has_no_unseparated_headings():
    md = render(_payload([analyze_sp_recommendation(sp_rec(9.0, 15.0))]))
    lines = md.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("#") and i > 0:
            assert lines[i - 1] == "", f"heading {line!r} not preceded by blank line"
