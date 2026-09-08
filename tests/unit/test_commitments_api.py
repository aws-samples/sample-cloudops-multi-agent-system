"""Tests for the AWS API wrapper layer, using stub clients (no network calls).

Focus is on the two things that actually bite: Cost Explorer's blank error
messages, and correct request shaping (notably that OfferingClass is only sent
for EC2, which is the one parameter the API rejects elsewhere).
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from commitments import api


def client_error(code: str, message: str = "") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": message}}, "OperationName"
    )


class StubCE:
    """Records calls and replays canned results or raises."""

    def __init__(self, result=None, error: ClientError | None = None):
        self.result = result or {}
        self.error = error
        self.calls: list[dict] = []

    def _respond(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result

    get_savings_plans_purchase_recommendation = _respond
    get_reservation_purchase_recommendation = _respond
    get_savings_plans_coverage = _respond
    get_savings_plans_utilization = _respond
    get_reservation_coverage = _respond
    get_reservation_utilization = _respond
    get_cost_and_usage = _respond


def clients(ce=None, coh=None) -> api.Clients:
    return api.Clients(
        ce=ce or StubCE(), coh=coh or StubCE(), account_id="111122223333", profile="t"
    )


# ------------------------------------------------------- error normalization


@pytest.mark.unit
def test_blank_data_unavailable_gets_readable_message():
    """Cost Explorer returns DataUnavailableException with an EMPTY message."""
    ce = StubCE(error=client_error("DataUnavailableException", ""))
    result = api.get_sp_utilization(clients(ce=ce), 30)
    assert result["error_code"] == "DataUnavailableException"
    assert "no active commitment" in result["error"].lower()
    assert result["error"].strip()


@pytest.mark.unit
def test_blank_unknown_error_still_produces_text():
    ce = StubCE(error=client_error("SomeOtherException", ""))
    result = api.get_sp_coverage(clients(ce=ce), 30)
    assert result["error"].strip()


@pytest.mark.unit
def test_real_error_message_is_preserved():
    ce = StubCE(error=client_error("ValidationException", "Invalid Service."))
    result = api.get_ri_recommendation(
        clients(ce=ce), "Amazon Redshift", "ONE_YEAR", "NO_UPFRONT", "THIRTY_DAYS", "PAYER"
    )
    assert result["error"] == "Invalid Service."


# ---------------------------------------------------------- request shaping


@pytest.mark.unit
def test_offering_class_sent_only_for_ec2():
    """RDS/Redshift reject ServiceSpecification; EC2 requires it for STANDARD."""
    ce = StubCE(result={"Recommendations": [], "Metadata": {}})
    c = clients(ce=ce)

    api.get_ri_recommendation(
        c, "Amazon Elastic Compute Cloud - Compute", "ONE_YEAR", "NO_UPFRONT", "THIRTY_DAYS", "PAYER"
    )
    assert "ServiceSpecification" in ce.calls[0]

    api.get_ri_recommendation(
        c, "Amazon Relational Database Service", "ONE_YEAR", "NO_UPFRONT", "THIRTY_DAYS", "PAYER"
    )
    assert "ServiceSpecification" not in ce.calls[1]


@pytest.mark.unit
def test_sp_request_sends_all_required_params():
    """All four are required by the API; omitting any is a ValidationException."""
    ce = StubCE(result={"SavingsPlansPurchaseRecommendation": {}, "Metadata": {}})
    api.get_sp_recommendation(
        clients(ce=ce), "COMPUTE_SP", "ONE_YEAR", "NO_UPFRONT", "THIRTY_DAYS", "PAYER"
    )
    sent = ce.calls[0]
    for required in (
        "SavingsPlansType",
        "TermInYears",
        "PaymentOption",
        "LookbackPeriodInDays",
    ):
        assert required in sent


@pytest.mark.unit
def test_time_period_end_is_today_and_start_is_days_back():
    from datetime import date, timedelta

    ce = StubCE(result={"SavingsPlansCoverages": []})
    api.get_sp_coverage(clients(ce=ce), 30)
    tp = ce.calls[0]["TimePeriod"]
    assert tp["End"] == date.today().isoformat()
    assert tp["Start"] == (date.today() - timedelta(days=30)).isoformat()


# --------------------------------------------------------- response mapping


@pytest.mark.unit
def test_sp_recommendation_unwraps_nested_payload():
    ce = StubCE(
        result={
            "SavingsPlansPurchaseRecommendation": {
                "SavingsPlansPurchaseRecommendationSummary": {"HourlyCommitmentToPurchase": "5.0"},
                "SavingsPlansPurchaseRecommendationDetails": [{"UpfrontCost": "0"}],
            },
            "Metadata": {"RecommendationId": "abc", "GenerationTimestamp": "2026-08-07"},
        }
    )
    result = api.get_sp_recommendation(
        clients(ce=ce), "COMPUTE_SP", "ONE_YEAR", "NO_UPFRONT", "THIRTY_DAYS", "PAYER"
    )
    assert result["summary"]["HourlyCommitmentToPurchase"] == "5.0"
    assert len(result["details"]) == 1
    assert result["recommendation_id"] == "abc"


@pytest.mark.unit
def test_ri_recommendation_reads_first_recommendation_entry():
    ce = StubCE(
        result={
            "Recommendations": [
                {
                    "RecommendationSummary": {"TotalEstimatedMonthlySavingsAmount": "100"},
                    "RecommendationDetails": [{"UpfrontCost": "0"}, {"UpfrontCost": "0"}],
                }
            ],
            "Metadata": {},
        }
    )
    result = api.get_ri_recommendation(
        clients(ce=ce), "Amazon ElastiCache", "ONE_YEAR", "NO_UPFRONT", "THIRTY_DAYS", "PAYER"
    )
    assert result["summary"]["TotalEstimatedMonthlySavingsAmount"] == "100"
    assert len(result["details"]) == 2
    assert result["label"] == "ElastiCache"


@pytest.mark.unit
def test_ri_recommendation_handles_empty_recommendations():
    ce = StubCE(result={"Recommendations": [], "Metadata": {}})
    result = api.get_ri_recommendation(
        clients(ce=ce), "Amazon Redshift", "ONE_YEAR", "NO_UPFRONT", "THIRTY_DAYS", "PAYER"
    )
    assert result["summary"] == {}
    assert result["details"] == []
    assert "error" not in result


@pytest.mark.unit
def test_eligible_spend_aggregates_service_groups():
    ce = StubCE(
        result={
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-07-01", "End": "2026-08-01"},
                    "Groups": [
                        {"Keys": ["EC2"], "Metrics": {"UnblendedCost": {"Amount": "100.5"}}},
                        {"Keys": ["RDS"], "Metrics": {"UnblendedCost": {"Amount": "50.25"}}},
                    ],
                }
            ]
        }
    )
    result = api.get_eligible_spend(clients(ce=ce), 60)
    period = result["periods"][0]
    assert period["total"] == pytest.approx(150.75)
    assert period["by_service"]["EC2"] == pytest.approx(100.5)


# ------------------------------------------------- Cost Optimization Hub


class StubCOH:
    def __init__(self, items=None, enrollment=None, error=None):
        self.items = items or []
        self.enrollment = enrollment
        self.error = error
        self.paginate_kwargs = None

    def list_enrollment_statuses(self, **kwargs):
        if self.error:
            raise self.error
        return self.enrollment or {}

    def get_paginator(self, name):
        stub = self

        class Paginator:
            def paginate(self, **kwargs):
                stub.paginate_kwargs = kwargs
                if stub.error:
                    raise stub.error
                return [{"items": stub.items}]

        return Paginator()


@pytest.mark.unit
def test_coh_enrollment_active():
    coh = StubCOH(
        enrollment={"items": [{"status": "Active", "accountId": "1"}], "includeMemberAccounts": True}
    )
    result = api.get_coh_enrollment(clients(coh=coh))
    assert result["enrolled"] is True


@pytest.mark.unit
def test_coh_enrollment_empty_means_not_enrolled():
    result = api.get_coh_enrollment(clients(coh=StubCOH(enrollment={"items": []})))
    assert result["enrolled"] is False
    assert result["status"] == "NOT_ENROLLED"


@pytest.mark.unit
def test_coh_enrollment_access_denied_is_not_fatal():
    coh = StubCOH(error=client_error("AccessDeniedException", "no perms"))
    result = api.get_coh_enrollment(clients(coh=coh))
    assert result["enrolled"] is False
    assert "error" in result


@pytest.mark.unit
def test_coh_filters_to_commitment_purchases_only():
    """Rightsizing/idle findings must not dilute a commitment report."""
    coh = StubCOH(items=[])
    api.get_coh_commitment_recommendations(clients(coh=coh))
    flt = coh.paginate_kwargs["filter"]
    assert set(flt["actionTypes"]) == {
        "PurchaseSavingsPlans",
        "PurchaseReservedInstances",
    }
    assert "Ec2Instance" not in flt["resourceTypes"]
    assert "ComputeSavingsPlans" in flt["resourceTypes"]


@pytest.mark.unit
def test_coh_recommendations_normalize_null_savings():
    """COH returns None for savings on some records; arithmetic must not break."""
    coh = StubCOH(
        items=[
            {
                "recommendationId": "r1",
                "estimatedMonthlySavings": None,
                "estimatedSavingsPercentage": None,
                "recommendedResourceType": "ComputeSavingsPlans",
            }
        ]
    )
    result = api.get_coh_commitment_recommendations(clients(coh=coh))
    rec = result["recommendations"][0]
    assert rec["estimated_monthly_savings"] == 0
    assert sum(r["estimated_monthly_savings"] for r in result["recommendations"]) == 0


# ------------------------------------------------------------- constants


@pytest.mark.unit
def test_ri_service_list_matches_api_supported_values():
    """Guards against someone adding a guessed service name.

    This list was read back from the ValidationException the API raises on an
    unknown Service value. Changing it requires re-probing, not guessing.
    """
    assert api.RI_SERVICES == (
        "Amazon Elastic Compute Cloud - Compute",
        "Amazon Relational Database Service",
        "Amazon Redshift",
        "Amazon ElastiCache",
        "Amazon Elasticsearch Service",
        "Amazon OpenSearch Service",
        "Amazon MemoryDB Service",
        "Amazon DynamoDB Service",
    )
    assert set(api.RI_SERVICE_LABELS) == set(api.RI_SERVICES)


@pytest.mark.unit
def test_sp_types_cover_all_four_plan_families():
    assert set(api.SP_TYPES) == {
        "COMPUTE_SP",
        "EC2_INSTANCE_SP",
        "SAGEMAKER_SP",
        "DATABASE_SP",
    }
    assert set(api.SP_TYPE_LABELS) == set(api.SP_TYPES)
