"""Tests for the shared pipeline: parameter resolution, sweep, JSON envelope.

`commitments.collect` is what the tool handlers drive: they parse the event and
build clients, and everything after that happens here. The envelope key names
are asserted explicitly because they are the contract with whatever renders the
recommendations — renaming one silently breaks the report and the frontend.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from commitments import api, collect
from commitments.analyze import analyze_sp_recommendation

from tests.unit.test_commitments_analyze import sp_rec


# ------------------------------------------------------------- test doubles


class RecordingCE:
    """Counts calls per operation so the sweep's shape can be asserted."""

    def __init__(self):
        self.sp_calls: list[dict] = []
        self.ri_calls: list[dict] = []

    def get_savings_plans_purchase_recommendation(self, **kw):
        self.sp_calls.append(kw)
        return {"SavingsPlansPurchaseRecommendation": {}, "Metadata": {}}

    def get_reservation_purchase_recommendation(self, **kw):
        self.ri_calls.append(kw)
        return {"Recommendations": [], "Metadata": {}}

    def get_savings_plans_coverage(self, **kw):
        return {"SavingsPlansCoverages": []}

    def get_savings_plans_utilization(self, **kw):
        return {"Total": {}, "SavingsPlansUtilizationsByTime": []}

    def get_reservation_coverage(self, **kw):
        return {"Total": {}, "CoveragesByTime": []}

    def get_reservation_utilization(self, **kw):
        return {"Total": {}, "UtilizationsByTime": []}

    def get_cost_and_usage(self, **kw):
        return {"ResultsByTime": []}


class NotEnrolledCOH:
    def list_enrollment_statuses(self, **kw):
        return {"items": []}

    def get_preferences(self, **kw):
        return {}


def recording_clients(ce):
    return api.Clients(
        ce=ce, coh=NotEnrolledCOH(), account_id="111122223333", profile=None
    )


# ------------------------------------------------------ RI service resolution


@pytest.mark.unit
def test_all_resolves_to_every_verified_service():
    assert collect.resolve_ri_services(["all"]) == list(api.RI_SERVICES)


@pytest.mark.unit
def test_short_labels_resolve_to_full_api_names():
    """Users type 'RDS', the API demands the long name."""
    assert collect.resolve_ri_services(["rds", "EC2"]) == [
        "Amazon Relational Database Service",
        "Amazon Elastic Compute Cloud - Compute",
    ]


@pytest.mark.unit
def test_full_api_names_pass_through():
    assert collect.resolve_ri_services(["Amazon Redshift"]) == ["Amazon Redshift"]


@pytest.mark.unit
def test_unknown_service_raises_rather_than_querying():
    """A typo must fail loudly, not silently drop a service from the sweep."""
    with pytest.raises(ValueError) as exc:
        collect.resolve_ri_services(["Aurora"])
    assert "Aurora" in str(exc.value)


@pytest.mark.unit
def test_parenthetical_label_matches_on_base_word():
    """'Elasticsearch (legacy)' must resolve from plain 'Elasticsearch'."""
    expected = ["Amazon Elasticsearch Service"]
    assert collect.resolve_ri_services(["Elasticsearch"]) == expected
    assert collect.resolve_ri_services(["elasticsearch"]) == expected
    assert collect.resolve_ri_services(["Elasticsearch (legacy)"]) == expected


@pytest.mark.unit
def test_every_label_is_reachable_by_its_base_word():
    """Guards against a label nobody can type in a comma-separated flag."""
    for service, label in api.RI_SERVICE_LABELS.items():
        base = label.split("(")[0].strip()
        assert collect.resolve_ri_services([base]) == [service], f"{label} unreachable"


@pytest.mark.unit
def test_duplicate_tokens_are_dropped():
    """Cost Explorer bills per request; 'EC2, ec2' must not pay twice."""
    assert collect.resolve_ri_services(["EC2", "ec2"]) == [
        "Amazon Elastic Compute Cloud - Compute"
    ]
    assert collect.resolve_sp_types(["compute_sp", "COMPUTE_SP"]) == ["COMPUTE_SP"]


@pytest.mark.unit
def test_blank_entries_are_ignored():
    assert collect.resolve_ri_services(["rds", "", " "]) == [
        "Amazon Relational Database Service"
    ]


# ------------------------------------------------------- SP type resolution


@pytest.mark.unit
def test_sp_types_all_and_case_insensitive():
    assert collect.resolve_sp_types(["all"]) == list(api.SP_TYPES)
    assert collect.resolve_sp_types(["compute_sp"]) == ["COMPUTE_SP"]


@pytest.mark.unit
def test_unknown_sp_type_raises():
    with pytest.raises(ValueError) as exc:
        collect.resolve_sp_types(["GRAVITON_SP"])
    assert "GRAVITON_SP" in str(exc.value)


@pytest.mark.unit
def test_validate_choices_names_the_offender():
    with pytest.raises(ValueError) as exc:
        collect.validate_choices(["TWO_YEARS"], collect.TERMS, "term")
    assert "TWO_YEARS" in str(exc.value)


# ------------------------------------------------------------- collect_all()


@pytest.mark.unit
def test_rejects_invalid_term_before_calling_aws():
    ce = RecordingCE()
    with pytest.raises(ValueError):
        collect.collect_all(recording_clients(ce), terms=["TWO_YEARS"])
    assert ce.sp_calls == []


@pytest.mark.unit
def test_rejects_invalid_payment_before_calling_aws():
    ce = RecordingCE()
    with pytest.raises(ValueError):
        collect.collect_all(recording_clients(ce), payments=["MONTHLY"])
    assert ce.sp_calls == []


@pytest.mark.unit
def test_rejects_invalid_family_before_calling_aws():
    ce = RecordingCE()
    with pytest.raises(ValueError):
        collect.collect_all(recording_clients(ce), families=["spot"])
    assert ce.sp_calls == []


@pytest.mark.unit
def test_sweeps_every_permutation():
    """4 SP types and 8 RI services, each across every term x payment."""
    ce = RecordingCE()
    collect.collect_all(
        recording_clients(ce),
        terms=["ONE_YEAR", "THREE_YEARS"],
        payments=["NO_UPFRONT", "ALL_UPFRONT"],
    )

    assert len(ce.sp_calls) == len(api.SP_TYPES) * 2 * 2
    assert len(ce.ri_calls) == len(api.RI_SERVICES) * 2 * 2
    assert {c["SavingsPlansType"] for c in ce.sp_calls} == set(api.SP_TYPES)
    assert {c["Service"] for c in ce.ri_calls} == set(api.RI_SERVICES)


@pytest.mark.unit
def test_honors_family_filter():
    ce = RecordingCE()
    collect.collect_all(
        recording_clients(ce), families=["sp"], terms=["ONE_YEAR"],
        payments=["NO_UPFRONT"],
    )
    assert ce.sp_calls
    assert ce.ri_calls == []


@pytest.mark.unit
def test_honors_sp_type_filter():
    ce = RecordingCE()
    collect.collect_all(
        recording_clients(ce), families=["sp"], sp_types=["COMPUTE_SP"],
        terms=["ONE_YEAR"], payments=["NO_UPFRONT"],
    )
    assert {c["SavingsPlansType"] for c in ce.sp_calls} == {"COMPUTE_SP"}


@pytest.mark.unit
def test_marks_coh_unavailable_when_not_enrolled():
    """Reconciliation must degrade to a stated caveat, not a crash."""
    payload = collect.collect_all(
        recording_clients(RecordingCE()), families=["sp"], terms=["ONE_YEAR"],
        payments=["NO_UPFRONT"],
    )
    coh = payload["raw"]["coh"]
    assert "error" in coh
    assert "not available" in coh["error"]


@pytest.mark.unit
def test_records_per_query_errors_without_aborting():
    """One failing permutation must not lose the other 15."""

    class PartlyBrokenCE(RecordingCE):
        def get_savings_plans_purchase_recommendation(self, **kw):
            if kw["SavingsPlansType"] == "SAGEMAKER_SP":
                raise RuntimeError("throttled")
            return super().get_savings_plans_purchase_recommendation(**kw)

    payload = collect.collect_all(
        recording_clients(PartlyBrokenCE()), families=["sp"], terms=["ONE_YEAR"],
        payments=["NO_UPFRONT"],
    )
    assert len(payload["errors"]) == 1
    # Labelled for humans, not with the raw enum — same wording both hosts emit.
    assert "SageMaker" in payload["errors"][0]["query"]
    # The other three SP types still produced results.
    assert len(payload["raw"]["sp_recs"]) == len(api.SP_TYPES) - 1


@pytest.mark.unit
def test_payload_carries_every_render_key():
    """`report.render` indexes these directly; a missing key is a KeyError."""
    payload = collect.collect_all(
        recording_clients(RecordingCE()), families=["sp"], terms=["ONE_YEAR"],
        payments=["NO_UPFRONT"], profile="prod",
    )
    for key in (
        "meta", "findings", "posture", "reconciliation", "eligible_spend", "errors"
    ):
        assert key in payload, f"render key {key} missing"
    assert payload["meta"]["account_id"] == "111122223333"
    assert payload["meta"]["profile"] == "prod"


@pytest.mark.unit
def test_posture_is_collected_even_when_no_family_is_swept():
    """Coverage/utilization health is useful on its own."""
    payload = collect.collect_all(
        recording_clients(RecordingCE()), families=[], terms=["ONE_YEAR"],
        payments=["NO_UPFRONT"],
    )
    assert payload["findings"] == []
    assert "posture" in payload


# ------------------------------------------------------------ JSON envelope


@pytest.mark.unit
def test_envelope_matches_consumer_key_names():
    """These keys are the contract with every host consuming the analysis."""
    f = analyze_sp_recommendation(sp_rec(6.0, 10.0))
    env = collect.envelope([f], {"status": "reconciled"})

    assert set(env) == {
        "recommendations",
        "count",
        "total_estimated_monthly_savings",
        "aws_best_case_monthly_savings",
        "total_upfront_cost",
        "reconciliation",
    }
    assert env["count"] == 1
    rec = env["recommendations"][0]
    for key in (
        "estimated_monthly_savings",
        "estimated_savings_percentage",
        "implementation_effort",
        "commitment_unit",
        "role",
        "usage_pool",
        "line_items",
        "line_items_total",
    ):
        assert key in rec, f"consumer key {key} missing"


@pytest.mark.unit
def test_envelope_omits_reconciliation_when_not_supplied():
    """A sizing-only caller has nothing to reconcile; the key must not appear empty."""
    f = analyze_sp_recommendation(sp_rec(6.0, 10.0))
    assert "reconciliation" not in collect.envelope([f])


@pytest.mark.unit
def test_envelope_reports_adjusted_and_best_case_separately():
    """Collapsing these two into one number is the error this skill exists to fix."""
    # Spiky: a $2/hr on-demand trough absorbs $1.40/hr of the $10/hr commitment.
    f = analyze_sp_recommendation(sp_rec(2.0, 10.0))
    env = collect.envelope([f], {})
    assert env["total_estimated_monthly_savings"] == pytest.approx(
        f.api_monthly_savings * 0.14
    )
    assert env["aws_best_case_monthly_savings"] == pytest.approx(f.api_monthly_savings)
    assert (
        env["total_estimated_monthly_savings"] < env["aws_best_case_monthly_savings"]
    )


@pytest.mark.unit
def test_envelope_is_serializable():
    f = analyze_sp_recommendation(sp_rec(6.0, 10.0))
    env = collect.envelope([f], {"status": "reconciled", "delta": 0.0})
    reloaded = json.loads(json.dumps(env, default=str))
    assert reloaded["recommendations"][0]["confidence"] == "Medium"


@pytest.mark.unit
def test_envelope_empty_findings_totals_zero():
    env = collect.envelope([], {"status": "agree-zero"})
    assert env["count"] == 0
    assert env["total_estimated_monthly_savings"] == 0
    assert env["recommendations"] == []


@pytest.mark.unit
def test_envelope_break_even_is_null_not_zero():
    """A no-upfront plan has no break-even; 0 would read as 'pays back instantly'."""
    f = analyze_sp_recommendation(sp_rec(9.0, 10.0))
    env = collect.envelope([f], {})
    assert env["recommendations"][0]["break_even_months"] is None


@pytest.mark.unit
def test_savings_plan_commitment_unit_is_hourly_dollars():
    """RI 'commitment' is a unit count; SP is $/hr. Mislabeling misreads by 1000x."""
    f = analyze_sp_recommendation(sp_rec(6.0, 10.0))
    assert collect.serialize_finding(f)["commitment_unit"] == "USD/hour"


# ------------------------------------------------- clients stay the caller's


@pytest.mark.unit
def test_module_builds_no_clients_and_touches_no_files():
    """The pipeline must stay drivable by a caller that supplies its own clients.

    `collect` runs in Lambda, where a boto3 profile has no meaning and the only
    writable path is /tmp. Keeping session construction, argument parsing and
    file IO out of this module is what leaves the handler as the single place
    credentials are resolved.
    """
    source = (
        Path(__file__).resolve().parents[2]
        / "src" / "lambda" / "mcp" / "commitments" / "commitments" / "collect.py"
    ).read_text()
    assert "import boto3" not in source
    assert "import argparse" not in source
    assert "open(" not in source


@pytest.mark.unit
def test_envelope_totals_one_option_per_usage_pool():
    """Compute SP and EC2 Instance SP discount the same EC2 usage."""
    from commitments.pools import plan_purchases, select_best_findings

    compute = analyze_sp_recommendation(sp_rec(9.0, 10.0, sp_type="COMPUTE_SP"))
    instance = analyze_sp_recommendation(sp_rec(9.0, 10.0, sp_type="EC2_INSTANCE_SP", discount=0.4))
    planned, pools = plan_purchases(select_best_findings([compute, instance]))
    env = collect.envelope(planned, pools=pools)
    assert env["count"] == 2
    assert env["total_estimated_monthly_savings"] == pytest.approx(instance.safe_monthly_savings, abs=0.01)
    assert [r["role"] for r in env["recommendations"]] == ["recommended", "alternative"]
    assert env["usage_pools"][0]["recommended_option"] == "EC2 Instance Savings Plan"


@pytest.mark.unit
def test_line_items_are_capped_and_can_be_dropped():
    from dataclasses import replace

    f = analyze_sp_recommendation(sp_rec(9.0, 10.0))
    many = replace(f, line_items=f.line_items * (collect.LINE_ITEM_LIMIT + 7))
    rec = collect.serialize_finding(many)
    assert len(rec["line_items"]) == collect.LINE_ITEM_LIMIT
    assert rec["line_items_omitted"] == 7
    assert rec["line_items_total"] == collect.LINE_ITEM_LIMIT + 7
    assert "line_items" not in collect.serialize_finding(many, include_lines=False)


# --------------------------------------------------- failure and time budget


@pytest.mark.unit
def test_a_run_where_every_query_failed_is_an_error_not_an_empty_report():
    from botocore.exceptions import ClientError

    class DeniedCE(RecordingCE):
        def get_savings_plans_purchase_recommendation(self, **kw):
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "nope"}}, "Get")

    payload = collect.collect_all(
        recording_clients(DeniedCE()), families=["sp"], terms=["ONE_YEAR"],
        payments=["NO_UPFRONT"],
    )
    assert "findings" not in payload
    fatal = payload["fatal"]
    assert "not a 'no opportunity' result" in fatal["error"]
    assert "Mode B" in fatal["hint"]
    assert len(payload["errors"]) == len(api.SP_TYPES)


@pytest.mark.unit
def test_diagnose_failure_picks_the_hint_for_the_error_code():
    hint = collect.diagnose_failure(
        [{"query": "q", "error_code": "DataUnavailableException", "error": "x"}]
    )["hint"]
    assert "usage history" in hint


@pytest.mark.unit
def test_unfinished_queries_are_abandoned_at_the_deadline():
    import time

    def slow():
        time.sleep(2)
        return {"ok": True}

    started = time.monotonic()
    results, errors = collect.run_jobs(
        [("fast", lambda: {"ok": True}), ("slow", slow)],
        deadline=time.monotonic() + 0.2,
    )
    assert time.monotonic() - started < 1.5
    assert results == [{"ok": True}]
    assert errors[0]["query"] == "slow"
    assert errors[0]["error_code"] == collect.TIME_BUDGET_ERROR


# ---------------------------------------------------------- paging and posture


@pytest.mark.unit
def test_every_recommendation_page_is_read_and_billed():
    """Details past page one carry real upfront and floors; dropping them
    understates the cost of the purchase."""

    class PagedCE(RecordingCE):
        def get_savings_plans_purchase_recommendation(self, **kw):
            self.sp_calls.append(kw)
            page = 0 if "NextPageToken" not in kw else 1
            detail = {"HourlyCommitmentToPurchase": "1.0", "UpfrontCost": "100"}
            return {
                "SavingsPlansPurchaseRecommendation": {
                    "SavingsPlansPurchaseRecommendationSummary": {"TotalRecommendationCount": "3"},
                    "SavingsPlansPurchaseRecommendationDetails": [detail] * (2 if page == 0 else 1),
                },
                **({"NextPageToken": "p2"} if page == 0 else {}),
            }

    ce = PagedCE()
    rec = api.get_sp_recommendation(
        recording_clients(ce), "COMPUTE_SP", "ONE_YEAR", "NO_UPFRONT", "THIRTY_DAYS", "LINKED"
    )
    assert len(rec["details"]) == 3
    assert rec["pages"] == 2
    assert "detail_count_warning" not in rec
    assert ce.sp_calls[1]["NextPageToken"] == "p2"


@pytest.mark.unit
def test_a_short_detail_list_is_flagged():
    class ShortCE(RecordingCE):
        def get_savings_plans_purchase_recommendation(self, **kw):
            return {"SavingsPlansPurchaseRecommendation": {
                "SavingsPlansPurchaseRecommendationSummary": {"TotalRecommendationCount": "5"},
                "SavingsPlansPurchaseRecommendationDetails": [{}],
            }}

    rec = api.get_sp_recommendation(
        recording_clients(ShortCE()), "COMPUTE_SP", "ONE_YEAR", "NO_UPFRONT", "THIRTY_DAYS", "LINKED"
    )
    assert "5 recommendation detail(s) but 1" in rec["detail_count_warning"]


@pytest.mark.unit
def test_reservation_posture_is_queried_per_service_with_a_filter():
    """Unfiltered, Cost Explorer reports EC2 and calls it everything."""

    class FilterCE(RecordingCE):
        def __init__(self):
            super().__init__()
            self.filters = []

        def get_reservation_utilization(self, **kw):
            self.filters.append(kw.get("Filter"))
            return super().get_reservation_utilization(**kw)

    ce = FilterCE()
    collect.collect_posture(recording_clients(ce), 30, 60, ["Amazon Relational Database Service"])
    assert ce.filters == [
        {"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Relational Database Service"]}}
    ]


@pytest.mark.unit
def test_posture_filter_uses_the_coverage_api_service_names():
    """The coverage/utilization SERVICE filter rejects two recommendation-API names.

    Cost Explorer's ValidationException lists the filter's supported values;
    MemoryDB is "Amazon MemoryDB" there (no "Service" suffix) and DynamoDB is
    not a valid value at all. Pinned here because stub clients accept any
    string, which is how the mismatch first shipped.
    """

    class FilterCE(RecordingCE):
        def __init__(self):
            super().__init__()
            self.filters = []

        def get_reservation_utilization(self, **kw):
            self.filters.append(kw["Filter"]["Dimensions"]["Values"][0])
            return super().get_reservation_utilization(**kw)

    ce = FilterCE()
    collected = collect.collect_posture(recording_clients(ce), 30, 60, list(api.RI_SERVICES))
    supported = {
        "Amazon Elastic Compute Cloud - Compute", "Amazon Relational Database Service",
        "Amazon ElastiCache", "Amazon Redshift", "Amazon Elasticsearch Service",
        "Amazon OpenSearch Service", "Amazon MemoryDB",
    }
    assert set(ce.filters) <= supported
    assert "Amazon MemoryDB" in ce.filters
    assert collected["ri_unmeasurable"] == ["Amazon DynamoDB Service"]


@pytest.mark.unit
def test_unfilterable_dynamodb_is_a_blind_spot_not_a_failed_gate():
    snapshot = collect.posture_snapshot(recording_clients(RecordingCE()), 30, 60, list(api.RI_SERVICES))
    assert not any("DynamoDB" in s for s in snapshot["unmeasured"])
    assert any("DynamoDB reservation coverage and utilization are not exposed" in n
               for n in snapshot["notes"])
    assert snapshot["safe_to_buy_more"] is not None


@pytest.mark.unit
def test_posture_snapshot_is_not_safe_when_nothing_was_measured():
    from botocore.exceptions import ClientError

    class DeniedPostureCE(RecordingCE):
        def _deny(self, **kw):
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "Get")

        get_savings_plans_coverage = _deny
        get_savings_plans_utilization = _deny
        get_reservation_coverage = _deny
        get_reservation_utilization = _deny

    snapshot = collect.posture_snapshot(recording_clients(DeniedPostureCE()), 30, 60, ["Amazon Redshift"])
    assert snapshot["blockers"] == []
    assert snapshot["safe_to_buy_more"] is None
    assert snapshot["unmeasured"]
    assert snapshot["queries_run"] == 5
