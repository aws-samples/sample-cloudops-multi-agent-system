"""Tests for the risk-adjustment logic.

Fixtures mirror real Cost Explorer response shapes, including its habit of
returning money and percentages as STRINGS.
"""

from __future__ import annotations

import pytest

from commitments.analyze import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    analyze_ri_recommendation,
    analyze_sp_recommendation,
    assess_existing_posture,
    classify_volatility,
    reconcile_with_coh,
    select_best_findings,
)
from commitments.report import render


def sp_rec(minimum: str, average: str, hourly: str = "10.0", monthly: str = "1000.0"):
    """Build an SP recommendation with a given hourly spend envelope."""
    return {
        "sp_type": "COMPUTE_SP",
        "term": "ONE_YEAR",
        "payment": "NO_UPFRONT",
        "lookback": "THIRTY_DAYS",
        "account_scope": "PAYER",
        "summary": {
            "HourlyCommitmentToPurchase": hourly,
            "EstimatedMonthlySavingsAmount": monthly,
            "EstimatedSavingsPercentage": "20.5",
            "CurrentOnDemandSpend": "8000.0",
        },
        "details": [
            {
                "CurrentMinimumHourlyOnDemandSpend": minimum,
                "CurrentAverageHourlyOnDemandSpend": average,
                "CurrentMaximumHourlyOnDemandSpend": "20.0",
                "UpfrontCost": "0.0",
                "EstimatedAverageUtilization": "97.5",
            }
        ],
        "generated_at": "2026-08-07T00:00:00Z",
        "recommendation_id": "rec-1",
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
    ],
)
def test_classify_volatility_bands(floor, average, expected):
    label, _ = classify_volatility(floor, average)
    assert label == expected


# ------------------------------------------------------- SP risk adjustment


@pytest.mark.unit
def test_stable_workload_keeps_api_recommendation():
    f = analyze_sp_recommendation(sp_rec(minimum="9.0", average="10.0"))
    assert f.confidence == CONFIDENCE_HIGH
    assert f.safe_hourly_commitment == pytest.approx(10.0)
    assert f.safe_monthly_savings == pytest.approx(1000.0)


@pytest.mark.unit
def test_moderate_workload_takes_midpoint():
    # floor 6.0 / avg 10.0 = 0.6 ratio -> moderate. Midpoint of 6 and 10 = 8.
    f = analyze_sp_recommendation(sp_rec(minimum="6.0", average="10.0"))
    assert f.confidence == CONFIDENCE_MEDIUM
    assert f.safe_hourly_commitment == pytest.approx(8.0)
    # Savings scale with commitment: 8/10 of $1000.
    assert f.safe_monthly_savings == pytest.approx(800.0)


@pytest.mark.unit
def test_spiky_workload_clamps_to_floor():
    f = analyze_sp_recommendation(sp_rec(minimum="2.0", average="10.0"))
    assert f.confidence == CONFIDENCE_LOW
    assert f.safe_hourly_commitment == pytest.approx(2.0)
    assert f.safe_monthly_savings == pytest.approx(200.0)


@pytest.mark.unit
def test_safe_commitment_never_exceeds_api_figure():
    """A floor above the API recommendation must not inflate the commitment."""
    f = analyze_sp_recommendation(
        sp_rec(minimum="50.0", average="55.0", hourly="10.0")
    )
    assert f.safe_hourly_commitment <= 10.0
    assert f.safe_monthly_savings <= 1000.0


@pytest.mark.unit
def test_waste_exposure_measured_against_floor():
    f = analyze_sp_recommendation(sp_rec(minimum="2.0", average="10.0"))
    # (10.0 api - 2.0 floor) * 730 hours
    assert f.waste_exposure_monthly == pytest.approx(8.0 * 730.0)


@pytest.mark.unit
def test_upfront_cost_produces_break_even():
    rec = sp_rec(minimum="9.0", average="10.0")
    rec["details"][0]["UpfrontCost"] = "6000.0"
    f = analyze_sp_recommendation(rec)
    assert f.break_even_months == pytest.approx(6.0)


@pytest.mark.unit
def test_empty_string_numerics_do_not_raise():
    """Cost Explorer returns "" for absent numbers; float("") would crash."""
    rec = sp_rec(minimum="", average="", hourly="10.0", monthly="500.0")
    f = analyze_sp_recommendation(rec)
    assert f is not None
    assert f.confidence == CONFIDENCE_LOW


@pytest.mark.unit
def test_zero_recommendation_is_dropped():
    assert analyze_sp_recommendation(sp_rec("0", "0", hourly="0.0", monthly="0.0")) is None


@pytest.mark.unit
def test_errored_recommendation_is_dropped():
    assert analyze_sp_recommendation({"error": "boom", "error_code": "X"}) is None


# ------------------------------------------------------- RI risk adjustment


@pytest.mark.unit
def test_ri_recommendation_rounds_to_whole_units():
    rec = {
        "service": "Amazon Relational Database Service",
        "label": "RDS",
        "term": "ONE_YEAR",
        "payment": "ALL_UPFRONT",
        "summary": {
            "TotalEstimatedMonthlySavingsAmount": "900.0",
            "TotalEstimatedMonthlySavingsPercentage": "31.0",
        },
        "details": [
            {
                "RecommendedNumberOfInstancesToPurchase": "10",
                "MinimumNumberOfInstancesUsedPerHour": "6",
                "AverageNumberOfInstancesUsedPerHour": "10",
                "UpfrontCost": "12000.0",
                "RecurringStandardMonthlyCost": "0.0",
                "EstimatedBreakEvenInMonths": "8.5",
                "AverageUtilization": "92.0",
            }
        ],
    }
    f = analyze_ri_recommendation(rec)
    # ratio 0.6 -> moderate -> midpoint 8.0, already whole.
    assert f.safe_hourly_commitment == 8.0
    assert f.safe_hourly_commitment == int(f.safe_hourly_commitment)
    assert f.break_even_months == pytest.approx(8.5)


@pytest.mark.unit
def test_ri_capacity_unit_fallback():
    """DynamoDB reports capacity units, not instance counts."""
    rec = {
        "service": "Amazon DynamoDB Service",
        "label": "DynamoDB",
        "term": "ONE_YEAR",
        "payment": "NO_UPFRONT",
        "summary": {"TotalEstimatedMonthlySavingsAmount": "400.0"},
        "details": [
            {
                "RecommendedNumberOfCapacityUnitsToPurchase": "100",
                "MinimumNumberOfCapacityUnitsUsedPerHour": "95",
                "AverageNumberOfCapacityUnitsUsedPerHour": "100",
            }
        ],
    }
    f = analyze_ri_recommendation(rec)
    assert f.api_hourly_commitment == pytest.approx(100.0)
    assert f.confidence == CONFIDENCE_HIGH


@pytest.mark.unit
def test_ri_rounds_down_and_note_quotes_the_rounded_figure():
    """The prose must not contradict the number the table shows."""
    rec = {
        "service": "Amazon Relational Database Service",
        "label": "RDS",
        "term": "ONE_YEAR",
        "payment": "PARTIAL_UPFRONT",
        "summary": {"TotalEstimatedMonthlySavingsAmount": "900.0"},
        "details": [
            {
                # floor 9 / avg 12 = 0.75 -> moderate -> midpoint 10.5 -> 10
                "RecommendedNumberOfInstancesToPurchase": "12",
                "MinimumNumberOfInstancesUsedPerHour": "9",
                "AverageNumberOfInstancesUsedPerHour": "12",
            }
        ],
    }
    f = analyze_ri_recommendation(rec)
    assert f.safe_hourly_commitment == 10.0
    trimmed = [n for n in f.rationale if "trimmed to" in n]
    assert trimmed, "expected a trim note"
    # Reservations are counted, not priced per hour — and 10.5 was rounded away.
    assert "10 unit(s)" in trimmed[0]
    assert "$" not in trimmed[0]
    assert "10.5" not in trimmed[0]


@pytest.mark.unit
def test_sp_trim_note_uses_dollars_per_hour():
    f = analyze_sp_recommendation(sp_rec(minimum="6.0", average="10.0"))
    trimmed = [n for n in f.rationale if "trimmed to" in n]
    assert "$8.0000/hr" in trimmed[0]


# -------------------------------------------------- break-even vs term guard


@pytest.mark.unit
def test_break_even_beyond_term_is_flagged_and_downgraded():
    """A 1-year commitment that pays back in 15 years is a loss, not a saving."""
    rec = sp_rec(minimum="2.0", average="10.0", hourly="10.0", monthly="1000.0")
    rec["term"] = "ONE_YEAR"
    rec["details"][0]["UpfrontCost"] = "38000.0"
    f = analyze_sp_recommendation(rec)
    assert f.break_even_months > 12
    assert f.confidence == CONFIDENCE_LOW
    assert any("Do not buy" in n for n in f.rationale)
    # The warning must lead, not trail the supporting detail.
    assert "Do not buy" in f.rationale[0]


@pytest.mark.unit
def test_break_even_inside_term_is_not_flagged():
    rec = sp_rec(minimum="9.0", average="10.0", monthly="1000.0")
    rec["details"][0]["UpfrontCost"] = "6000.0"
    f = analyze_sp_recommendation(rec)
    assert f.break_even_months == pytest.approx(6.0)
    assert f.confidence == CONFIDENCE_HIGH
    assert not any("Do not buy" in n for n in f.rationale)


@pytest.mark.unit
def test_three_year_term_allows_longer_break_even():
    """24 months is fatal on a 1-year term and fine on a 3-year one."""
    rec = sp_rec(minimum="9.0", average="10.0", monthly="1000.0")
    rec["details"][0]["UpfrontCost"] = "24000.0"

    rec["term"] = "THREE_YEARS"
    assert not any(
        "Do not buy" in n for n in analyze_sp_recommendation(rec).rationale
    )

    rec["term"] = "ONE_YEAR"
    assert any("Do not buy" in n for n in analyze_sp_recommendation(rec).rationale)


@pytest.mark.unit
def test_ri_break_even_beyond_term_is_flagged():
    rec = {
        "service": "Amazon Redshift",
        "label": "Redshift",
        "term": "ONE_YEAR",
        "payment": "ALL_UPFRONT",
        "summary": {"TotalEstimatedMonthlySavingsAmount": "100.0"},
        "details": [
            {
                "RecommendedNumberOfInstancesToPurchase": "4",
                "MinimumNumberOfInstancesUsedPerHour": "4",
                "AverageNumberOfInstancesUsedPerHour": "4",
                "UpfrontCost": "50000.0",
                "EstimatedBreakEvenInMonths": "40.0",
            }
        ],
    }
    f = analyze_ri_recommendation(rec)
    assert f.confidence == CONFIDENCE_LOW
    assert any("Do not buy" in n for n in f.rationale)


# ------------------------------------------------------------- posture gate


@pytest.mark.unit
def test_low_sp_utilization_is_a_blocker():
    posture = assess_existing_posture(
        sp_coverage={"periods": [{"Coverage": {"CoveragePercentage": "40.0", "OnDemandCost": "100"}}]},
        sp_utilization={"total": {"Utilization": {"UtilizationPercentage": "72.0", "UnusedCommitment": "500.0"}}},
        ri_coverage={},
        ri_utilization={},
    )
    assert any("72.0% utilized" in b for b in posture["blockers"])


@pytest.mark.unit
def test_saturated_coverage_is_a_blocker():
    posture = assess_existing_posture(
        sp_coverage={"periods": [{"Coverage": {"CoveragePercentage": "97.0", "OnDemandCost": "10"}}]},
        sp_utilization={},
        ri_coverage={},
        ri_utilization={},
    )
    assert any("97.0%" in b for b in posture["blockers"])


@pytest.mark.unit
def test_healthy_posture_has_no_blockers():
    posture = assess_existing_posture(
        sp_coverage={"periods": [{"Coverage": {"CoveragePercentage": "60.0", "OnDemandCost": "900"}}]},
        sp_utilization={"total": {"Utilization": {"UtilizationPercentage": "99.5", "UnusedCommitment": "1.0"}}},
        ri_coverage={"total": {"CoverageHours": {"CoverageHoursPercentage": "55.0", "OnDemandHours": "100"}}},
        ri_utilization={"total": {"UtilizationPercentage": "99.0", "UnusedHours": "2", "RealizedSavings": "300"}},
    )
    assert posture["blockers"] == []


@pytest.mark.unit
def test_empty_datauavailable_message_is_explained():
    """A blank DataUnavailableException must not surface as an empty note."""
    posture = assess_existing_posture(
        sp_coverage={},
        sp_utilization={"error": "No data for this period — no active commitment."},
        ri_coverage={},
        ri_utilization={},
    )
    assert posture["notes"]
    assert all(n.strip() for n in posture["notes"])


# ---------------------------------------------------------- reconciliation


@pytest.mark.unit
@pytest.mark.parametrize(
    "ce,coh,expected",
    [
        (1000.0, 1000.0, "reconciled"),
        (1000.0, 950.0, "reconciled"),
        (1000.0, 800.0, "minor-variance"),
        (1000.0, 500.0, "material-variance"),
        (0.0, 0.0, "agree-zero"),
    ],
)
def test_reconciliation_bands(ce, coh, expected):
    f = analyze_sp_recommendation(sp_rec("9.0", "10.0", hourly="10.0", monthly=str(ce)))
    findings = [f] if f else []
    result = reconcile_with_coh(
        findings,
        {"recommendations": [{"estimated_monthly_savings": coh, "recommended_resource_type": "ComputeSavingsPlans", "current_resource_type": ""}] if coh else [], "count": 1 if coh else 0},
    )
    assert result["status"] == expected


@pytest.mark.unit
def test_reconciliation_unavailable_when_coh_errors():
    result = reconcile_with_coh([], {"error": "not enrolled"})
    assert result["status"] == "unavailable"


@pytest.mark.unit
def test_reconciliation_compares_unadjusted_figures():
    """COH publishes a best case, so the like-for-like axis is the API figure."""
    f = analyze_sp_recommendation(sp_rec("2.0", "10.0", monthly="1000.0"))
    assert f.safe_monthly_savings < f.api_monthly_savings
    result = reconcile_with_coh(
        [f],
        {"recommendations": [{"estimated_monthly_savings": 1000.0, "recommended_resource_type": "ComputeSavingsPlans", "current_resource_type": ""}], "count": 1},
    )
    assert result["status"] == "reconciled"
    assert result["ce_monthly_savings"] == pytest.approx(1000.0)


# ------------------------------------------------------------- selection


@pytest.mark.unit
def test_selection_keeps_one_permutation_per_family():
    weak = analyze_sp_recommendation(sp_rec("9.0", "10.0", monthly="500.0"))
    strong = analyze_sp_recommendation(sp_rec("9.0", "10.0", monthly="1500.0"))
    strong.term = "THREE_YEARS"
    best = select_best_findings([weak, strong])
    assert len(best) == 1
    assert best[0].safe_monthly_savings == pytest.approx(1500.0)


@pytest.mark.unit
def test_selection_breaks_ties_toward_shorter_term():
    one_year = analyze_sp_recommendation(sp_rec("9.0", "10.0", monthly="1000.0"))
    three_year = analyze_sp_recommendation(sp_rec("9.0", "10.0", monthly="1000.0"))
    three_year.term = "THREE_YEARS"
    best = select_best_findings([three_year, one_year])
    assert best[0].term == "ONE_YEAR"


# ---------------------------------------------------------------- report


def _payload(findings, posture=None, recon=None):
    return {
        "meta": {
            "account_id": "111122223333",
            "profile": "test",
            "generated_at": "2026-08-07 00:00 UTC",
            "lookback": "THIRTY_DAYS",
            "account_scope": "PAYER",
        },
        "findings": findings,
        "posture": posture or {"blockers": [], "notes": []},
        "reconciliation": recon or {"status": "unavailable", "reason": "test"},
        "eligible_spend": {"periods": [{"start": "2026-07-01", "end": "2026-08-01", "total": 5000.0, "by_service": {"Amazon Elastic Compute Cloud - Compute": 4000.0}}]},
        "errors": [],
    }


@pytest.mark.integration
def test_report_renders_with_findings():
    f = analyze_sp_recommendation(sp_rec("6.0", "10.0"))
    md = render(_payload([f]))
    assert "# AWS Discounted Commitments Report" in md
    assert "Risk-adjusted achievable savings" in md
    assert "Compute Savings Plan" in md
    # The haircut must be stated, not hidden.
    assert "below the AWS best case" in md


@pytest.mark.integration
def test_report_renders_empty_case():
    md = render(_payload([]))
    assert "No commitment opportunity found" in md
    assert "real result, not a failure" in md


@pytest.mark.integration
def test_report_surfaces_blockers_before_recommendations():
    f = analyze_sp_recommendation(sp_rec("9.0", "10.0"))
    posture = {"blockers": ["Existing Savings Plans are only 70.0% utilized"], "notes": []}
    md = render(_payload([f], posture=posture))
    assert "Do not act on these numbers yet" in md
    assert md.index("Blockers") < md.index("## Recommended commitments")


@pytest.mark.integration
def test_report_flags_material_variance():
    f = analyze_sp_recommendation(sp_rec("9.0", "10.0"))
    recon = {
        "status": "material-variance",
        "ce_monthly_savings": 1000.0,
        "coh_monthly_savings": 400.0,
        "delta": 600.0,
        "delta_pct": 60.0,
        "coh_count": 1,
        "coh_by_resource_type": {"ComputeSavingsPlans": 400.0},
    }
    md = render(_payload([f], recon=recon))
    assert "MATERIAL VARIANCE" in md


@pytest.mark.integration
def test_report_zero_posture_does_not_imply_wasted_commitment():
    """All-zero metrics mean no commitments exist, not 0% utilization."""
    posture = {
        "blockers": [],
        "notes": [],
        "sp_coverage_pct": 0.0,
        "ri_coverage_pct": 0.0,
        "ri_utilization_pct": 0.0,
    }
    md = render(_payload([], posture=posture))
    assert "nothing to measure" in md
    assert "| Savings Plans coverage | 0.0% |" not in md


@pytest.mark.integration
def test_report_marks_break_even_past_the_term():
    rec = sp_rec(minimum="2.0", average="10.0", monthly="1000.0")
    rec["details"][0]["UpfrontCost"] = "38000.0"
    md = render(_payload([analyze_sp_recommendation(rec)]))
    assert "cannot pay back" in md
    assert "longer than the 12-month term" in md


@pytest.mark.integration
def test_report_has_no_blank_table_rows():
    """Markdown tables break if a heading is not preceded by a blank line."""
    f = analyze_sp_recommendation(sp_rec("6.0", "10.0"))
    md = render(_payload([f]))
    lines = md.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("#") and i > 0:
            assert lines[i - 1] == "", f"heading {line!r} not preceded by blank line"
