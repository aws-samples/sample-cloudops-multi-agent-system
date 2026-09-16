"""Unit tests for the commitments MCP tool — the event-adapter layer.

The analysis itself (`commitments/api.py`, `analyze.py`, `collect.py`,
`report.py`) is covered by test_commitments_api.py, test_commitments_analyze.py
and test_commitments_collect.py. What this module covers is everything the
handler adds on top:
  * Dispatcher — unknown tool returns error + tool list; known tool routes
  * _get_clients — builds api.Clients from shared.cross_account (never
    api.build_clients, which would need a boto3 profile that has no meaning
    in Lambda), with the COH role alias, and caches across invocations
  * Parameter validation — every ParamError path surfaces as {"error": ...}
  * _resolve_ri_services / _resolve_sp_types — label + case tolerance
  * collect.run_jobs — a throttled permutation is a warning, not a lost sweep
  * collect.serialize_finding — commitment_unit differs by family
  * handle_generate_commitment_analysis — end-to-end through the REAL
    api/analyze/report modules against a stub Cost Explorer client, asserting
    report_markdown is rendered and posture blockers are surfaced
  * handle_get_commitment_posture / size_* — envelope shape
  * Handler discipline — the sweep and the shared constants must not be
    re-implemented here, where none of the above tests would see them

The handler imports `commitments.*` and `shared.cross_account` at module scope,
so both must resolve before exec_module; conftest.py binds each. Note that the
handler is loaded from its file rather than imported by name, because the tool
directory also contains a `handler.py` and a sys.path entry for it would shadow
the top-level `handler` name every other Lambda test module imports (it
silently broke all 182 network-resilience tests when tried).
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOL_DIR = _REPO_ROOT / "src" / "lambda" / "mcp" / "commitments"

_HANDLER_PATH = _TOOL_DIR / "handler.py"
_spec = importlib.util.spec_from_file_location("commitments_handler", _HANDLER_PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)
sys.modules["commitments_handler"] = handler


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_client_cache():
    """Clear the module-scope client cache between tests.

    _get_clients memoizes for warm-container reuse; leaving a previous test's
    stub in place would make later tests pass for the wrong reason.
    """
    handler._clients = None
    yield
    handler._clients = None


def _make_context(tool_name: str) -> SimpleNamespace:
    """Build the AgentCore Gateway context shape the dispatcher reads."""
    return SimpleNamespace(
        client_context=SimpleNamespace(
            custom={"bedrockAgentCoreToolName": f"commitments___{tool_name}"}
        )
    )


def _stub_clients(monkeypatch, ce=None, coh=None, account_id="123456789012"):
    """Install a stub api.Clients so no test can construct a real boto3 client."""
    clients = handler.api.Clients(
        ce=ce if ce is not None else MagicMock(),
        coh=coh if coh is not None else MagicMock(),
        account_id=account_id,
        profile=None,
    )
    monkeypatch.setattr(handler, "_get_clients", lambda: clients)
    return clients


def _sp_response(hourly="10.0", monthly_savings="1000.0", minimum="6.0",
                 average="10.0", upfront="0.0"):
    """A Cost Explorer SP purchase recommendation, with CE's string numerics.

    The default floor/average ($6 trough vs $10 average = 0.6) lands in the
    "moderate" volatility band on purpose, so the risk adjustment is exercised:
    a flat 0.8+ workload would take the AWS figure unchanged and the tests
    would not distinguish the adjusted path from a passthrough.
    """
    return {
        "SavingsPlansPurchaseRecommendation": {
            "SavingsPlansPurchaseRecommendationSummary": {
                "HourlyCommitmentToPurchase": hourly,
                "EstimatedMonthlySavingsAmount": monthly_savings,
                "EstimatedSavingsPercentage": "22.5",
                "CurrentOnDemandSpend": "9000.0",
            },
            "SavingsPlansPurchaseRecommendationDetails": [
                {
                    "CurrentMinimumHourlyOnDemandSpend": minimum,
                    "CurrentAverageHourlyOnDemandSpend": average,
                    "UpfrontCost": upfront,
                    "EstimatedAverageUtilization": "95.0",
                }
            ],
        },
        "Metadata": {"GenerationTimestamp": "2026-09-01T00:00:00Z",
                     "RecommendationId": "sp-rec-1"},
    }


def _ce_stub(**overrides):
    """Cost Explorer stub covering every call the collection path makes."""
    ce = MagicMock()
    ce.get_savings_plans_purchase_recommendation.return_value = _sp_response()
    ce.get_reservation_purchase_recommendation.return_value = {
        "Recommendations": [], "Metadata": {},
    }
    ce.get_savings_plans_coverage.return_value = {
        "SavingsPlansCoverages": [
            {"Coverage": {"CoveragePercentage": "40.0", "OnDemandCost": "5000.0"}}
        ]
    }
    # 62% utilization is below the warn threshold → must produce a blocker.
    ce.get_savings_plans_utilization.return_value = {
        "Total": {"Utilization": {"UtilizationPercentage": "62.0",
                                  "UnusedCommitment": "1234.56"}}
    }
    ce.get_reservation_coverage.return_value = {
        "Total": {"CoverageHours": {"CoverageHoursPercentage": "30.0",
                                    "OnDemandHours": "700.0"}},
        "CoveragesByTime": [],
    }
    ce.get_reservation_utilization.return_value = {
        "Total": {"UtilizationPercentage": "99.0", "UnusedHours": "1.0"},
        "UtilizationsByTime": [],
    }
    ce.get_cost_and_usage.return_value = {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2026-08-01", "End": "2026-09-01"},
                "Groups": [
                    {"Keys": ["Amazon Elastic Compute Cloud - Compute"],
                     "Metrics": {"UnblendedCost": {"Amount": "9000.0"}}},
                    {"Keys": ["Amazon Simple Storage Service"],
                     "Metrics": {"UnblendedCost": {"Amount": "1500.0"}}},
                ],
            }
        ]
    }
    for name, value in overrides.items():
        getattr(ce, name).return_value = value
    return ce


def _coh_stub(enrolled=True, recommendations=()):
    coh = MagicMock()
    coh.list_enrollment_statuses.return_value = {
        "items": [{"status": "Active" if enrolled else "Inactive",
                   "accountId": "123456789012"}],
        "includeMemberAccounts": True,
    }
    paginator = MagicMock()
    paginator.paginate.return_value = [{"items": list(recommendations)}]
    coh.get_paginator.return_value = paginator
    return coh


# ---------------------------------------------------------------------------
# Handler discipline — keep logic where the analysis tests can see it
# ---------------------------------------------------------------------------


class TestHandlerDiscipline:
    """The handler must stay an event adapter over `commitments.collect`.

    Its job is to turn a JSON event into validated parameters and to build AWS
    clients. Analysis logic that migrates up into it leaves the coverage in
    test_commitments_{api,analyze,collect}.py behind, which is how the
    `commitment_unit` drift below got in.
    """

    def test_handler_never_calls_the_profile_based_client_builder(self):
        """api.build_clients opens a boto3 Session with a named profile, which
        does not exist in Lambda. The handler must build Clients itself."""
        assert "build_clients(" not in _HANDLER_PATH.read_text(encoding="utf-8")

    def test_handler_drives_the_shared_pipeline_instead_of_its_own(self):
        """The sweep must not be re-implemented alongside the shared pipeline.

        It was, once: two thread-pool fan-outs with the same shape drifted on
        `commitment_unit` before anyone noticed, because nothing compared them.
        Anything the handler orchestrates itself is orchestration that
        test_commitments_collect.py cannot reach.
        """
        source = _HANDLER_PATH.read_text(encoding="utf-8")
        assert "ThreadPoolExecutor" not in source
        for private in ("_run_jobs", "_sweep_savings_plans", "_sweep_reservations",
                        "_collect_posture", "_findings_from", "_serialize_finding",
                        "_envelope(", "_meta("):
            assert private not in source, (
                f"{private} belongs in commitments/collect.py, where the skill "
                "tests and the drift guard can see it"
            )

    def test_handler_does_not_restate_the_shared_defaults(self):
        """A local copy of TERMS/DEFAULT_* could accept what the pipeline rejects."""
        assert handler.TERMS is handler.collect.TERMS
        assert handler.PAYMENTS is handler.collect.PAYMENTS
        assert handler.LOOKBACKS is handler.collect.LOOKBACKS
        assert handler.ACCOUNT_SCOPES is handler.collect.ACCOUNT_SCOPES
        assert handler.FAMILIES is handler.collect.FAMILIES
        assert handler.DEFAULT_TERMS is handler.collect.DEFAULT_TERMS
        assert handler.DEFAULT_PAYMENTS is handler.collect.DEFAULT_PAYMENTS
        assert handler.DEFAULT_LOOKBACK is handler.collect.DEFAULT_LOOKBACK
        assert handler.DEFAULT_POSTURE_DAYS is handler.collect.DEFAULT_POSTURE_DAYS
        assert handler.DEFAULT_SPEND_DAYS is handler.collect.DEFAULT_SPEND_DAYS


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class TestDispatcher:
    def test_unknown_tool_returns_error_with_tool_list(self):
        result = handler.handler({}, _make_context("nonexistent_tool"))
        assert result["error"].startswith("Unknown tool: nonexistent_tool")
        assert result["available_tools"] == [
            "generate_commitment_analysis",
            "size_savings_plans",
            "size_reservations",
            "get_commitment_posture",
            "get_commitment_expiry",
        ]

    def test_known_tool_routes_correctly(self, monkeypatch):
        called = {}

        def fake(event):
            called["event"] = event
            return {"ok": True}

        monkeypatch.setattr(handler, "handle_get_commitment_posture", fake)
        result = handler.handler({"posture_days": 7}, _make_context("get_commitment_posture"))
        assert called["event"] == {"posture_days": 7}
        assert result == {"ok": True}

    def test_routing_strips_target_prefix(self, monkeypatch):
        """bedrockAgentCoreToolName is `target___tool` — dispatcher splits on ___."""
        monkeypatch.setattr(
            handler, "handle_size_savings_plans", lambda event: {"routed": True}
        )
        ctx = SimpleNamespace(
            client_context=SimpleNamespace(
                custom={"bedrockAgentCoreToolName": "cop-rt-commitments___size_savings_plans"}
            )
        )
        assert handler.handler({}, ctx) == {"routed": True}


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------


class TestGetClients:
    def test_builds_clients_from_cross_account_with_coh_role_alias(self, monkeypatch):
        calls = []

        def fake_get_aws_client(service, region_name=None, role_alias=None, **kw):
            calls.append((service, region_name, role_alias))
            client = MagicMock()
            if service == "sts":
                client.get_caller_identity.return_value = {"Account": "999888777666"}
            return client

        monkeypatch.setattr(handler, "get_aws_client", fake_get_aws_client)

        clients = handler._get_clients()
        assert clients.account_id == "999888777666"
        assert clients.profile is None
        # COH must go through its own role alias, matching cost-optimization-hub.
        assert ("cost-optimization-hub", handler.api.COH_REGION, "COH") in calls
        assert ("ce", handler.api.CE_REGION, None) in calls

    def test_clients_are_cached_across_invocations(self, monkeypatch):
        monkeypatch.setattr(
            handler, "get_aws_client", lambda *a, **k: MagicMock()
        )
        monkeypatch.setattr(handler, "_account_id", lambda: "111122223333")
        first = handler._get_clients()
        second = handler._get_clients()
        assert first is second

    def test_account_id_failure_is_not_fatal(self, monkeypatch):
        def boom(service, **kw):
            raise RuntimeError("STS unavailable")

        monkeypatch.setattr(handler, "get_aws_client", boom)
        assert handler._account_id() == "unknown"


# ---------------------------------------------------------------------------
# Parameter validation — every value arrives as caller-controlled JSON
# ---------------------------------------------------------------------------


class TestParameterParsing:
    def test_as_list_accepts_comma_separated_string(self):
        assert handler._as_list("ONE_YEAR, THREE_YEARS", ()) == ["ONE_YEAR", "THREE_YEARS"]

    def test_as_list_accepts_json_array(self):
        assert handler._as_list(["ONE_YEAR"], ()) == ["ONE_YEAR"]

    def test_as_list_empty_falls_back_to_default(self):
        assert handler._as_list(None, ("A", "B")) == ["A", "B"]
        assert handler._as_list("", ("A",)) == ["A"]

    def test_as_list_rejects_wrong_type(self):
        with pytest.raises(handler.ParamError):
            handler._as_list({"term": "ONE_YEAR"}, ())

    def test_validate_all_rejects_unknown_value(self):
        with pytest.raises(handler.ParamError) as exc:
            handler._validate_all(["FIVE_YEARS"], handler.TERMS, "term")
        assert "FIVE_YEARS" in str(exc.value)
        assert "ONE_YEAR" in str(exc.value)

    def test_validate_one_defaults_when_absent(self):
        assert handler._validate_one(
            None, handler.LOOKBACKS, "lookback", handler.DEFAULT_LOOKBACK
        ) == "THIRTY_DAYS"

    def test_validate_one_rejects_unknown_value(self):
        with pytest.raises(handler.ParamError):
            handler._validate_one("NINETY_DAYS", handler.LOOKBACKS, "lookback", "THIRTY_DAYS")

    @pytest.mark.parametrize("bad", ["abc", 0, -5])
    def test_positive_int_rejects_non_positive_and_non_numeric(self, bad):
        with pytest.raises(handler.ParamError):
            handler._positive_int(bad, "posture_days", 30)

    def test_positive_int_default_and_coercion(self):
        assert handler._positive_int(None, "posture_days", 30) == 30
        assert handler._positive_int("14", "posture_days", 30) == 14

    def test_resolve_ri_services_all(self):
        assert handler._resolve_ri_services(None) == list(handler.api.RI_SERVICES)
        assert handler._resolve_ri_services("all") == list(handler.api.RI_SERVICES)

    def test_resolve_ri_services_accepts_short_labels_case_insensitively(self):
        assert handler._resolve_ri_services("ec2, RDS") == [
            "Amazon Elastic Compute Cloud - Compute",
            "Amazon Relational Database Service",
        ]

    def test_resolve_ri_services_accepts_full_api_name(self):
        assert handler._resolve_ri_services(["Amazon Redshift"]) == ["Amazon Redshift"]

    def test_resolve_ri_services_strips_parenthetical_label(self):
        """"Elasticsearch (legacy)" must also match on the bare word."""
        assert handler._resolve_ri_services("Elasticsearch") == [
            "Amazon Elasticsearch Service"
        ]

    def test_resolve_ri_services_deduplicates(self):
        assert handler._resolve_ri_services("EC2, ec2") == [
            "Amazon Elastic Compute Cloud - Compute"
        ]

    def test_resolve_ri_services_rejects_unknown(self):
        with pytest.raises(handler.ParamError) as exc:
            handler._resolve_ri_services("Fargate")
        assert "Fargate" in str(exc.value)

    def test_resolve_sp_types_uppercases(self):
        assert handler._resolve_sp_types("compute_sp") == ["COMPUTE_SP"]

    def test_resolve_sp_types_rejects_unknown(self):
        with pytest.raises(handler.ParamError):
            handler._resolve_sp_types("LAMBDA_SP")

    def test_common_params_defaults_omit_partial_upfront(self):
        params = handler._common_params({})
        assert params["terms"] == ["ONE_YEAR", "THREE_YEARS"]
        assert params["payments"] == ["NO_UPFRONT", "ALL_UPFRONT"]
        assert params["lookback"] == "THIRTY_DAYS"
        assert params["account_scope"] == "PAYER"


class TestParamErrorsSurfaceAsToolErrors:
    """A ParamError must come back as {"error": ...}, never as a 500."""

    @pytest.mark.parametrize("tool,event", [
        ("handle_generate_commitment_analysis", {"terms": ["FIVE_YEARS"]}),
        ("handle_generate_commitment_analysis", {"families": ["gpu"]}),
        ("handle_generate_commitment_analysis", {"posture_days": -1}),
        ("handle_size_savings_plans", {"savings_plan_types": "LAMBDA_SP"}),
        ("handle_size_savings_plans", {"account_scope": "MEMBER"}),
        ("handle_size_reservations", {"ri_services": "Fargate"}),
        ("handle_get_commitment_posture", {"spend_days": "many"}),
    ])
    def test_invalid_parameter_returns_error(self, tool, event):
        result = getattr(handler, tool)(event)
        assert "error" in result
        assert "report_markdown" not in result


# ---------------------------------------------------------------------------
# Parallel collection — one bad permutation must not lose the sweep
# ---------------------------------------------------------------------------


class TestRunJobs:
    """Exercised through the vendored module the handler actually calls.

    The skill's own suite covers this logic too; these stay because they are the
    only check that the copy shipped in the zip behaves, and because a sweep
    that silently loses permutations produces a plausible-looking report.
    """

    def test_empty_job_list(self):
        assert handler.collect.run_jobs([]) == ([], [])

    def test_raised_exception_becomes_a_warning(self):
        def boom():
            raise RuntimeError("ThrottlingException")

        results, errors = handler.collect.run_jobs(
            [("ok", lambda: {"v": 1}), ("bad", boom)]
        )
        assert results == [{"v": 1}]
        assert errors == [{"query": "bad", "error": "ThrottlingException"}]

    def test_api_level_error_dict_becomes_a_warning(self):
        """api.get_* returns {"error": ...} for ClientError instead of raising."""
        results, errors = handler.collect.run_jobs([
            ("ok", lambda: {"v": 1}),
            ("denied", lambda: {"error": "AccessDeniedException", "error_code": "AccessDenied"}),
        ])
        assert results == [{"v": 1}]
        assert errors[0]["query"] == "denied"
        assert errors[0]["error_code"] == "AccessDenied"

    def test_sweep_builds_one_job_per_permutation(self, monkeypatch):
        seen = []

        def fake_sp(clients, sp_type, term, payment, lookback, scope):
            seen.append((sp_type, term, payment))
            return {"sp_type": sp_type, "term": term, "payment": payment}

        monkeypatch.setattr(handler.collect, "get_sp_recommendation", fake_sp)
        results, errors = handler.collect.sweep_savings_plans(
            MagicMock(), ["COMPUTE_SP", "EC2_INSTANCE_SP"],
            ["ONE_YEAR", "THREE_YEARS"], ["NO_UPFRONT"], "THIRTY_DAYS", "PAYER",
        )
        assert len(results) == 4  # 2 types x 2 terms x 1 payment
        assert errors == []
        assert len(set(seen)) == 4  # late-binding closure bug would collapse these


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _finding(**kw):
    base = dict(
        family="savings-plan", label="Compute Savings Plan", term="ONE_YEAR",
        payment="NO_UPFRONT", api_hourly_commitment=10.0,
        safe_hourly_commitment=8.0, api_monthly_savings=1000.0,
        safe_monthly_savings=800.0, savings_percentage=22.5, upfront_cost=0.0,
        confidence="High", volatility="stable", rationale=["because"],
        break_even_months=None, waste_exposure_monthly=1460.0,
    )
    base.update(kw)
    return handler.collect.Finding(**base)


class TestSerializeFinding:
    def test_savings_plan_unit_is_dollars_per_hour(self):
        out = handler.collect.serialize_finding(_finding())
        assert out["commitment_unit"] == "USD/hour"
        assert out["achievable_commitment"] == 8.0
        assert out["aws_recommended_commitment"] == 10.0
        assert out["estimated_monthly_savings"] == 800.0
        assert out["aws_best_case_monthly_savings"] == 1000.0
        assert out["rationale"] == ["because"]

    def test_reservation_unit_is_instance_units(self):
        out = handler.collect.serialize_finding(
            _finding(family="reservation", label="EC2")
        )
        assert out["commitment_unit"] == "units"

    def test_break_even_none_stays_none(self):
        f = handler.collect.serialize_finding(_finding())
        assert f["break_even_months"] is None
        rounded = handler.collect.serialize_finding(_finding(break_even_months=13.27))
        assert rounded["break_even_months"] == 13.3

    def test_envelope_totals_both_axes(self):
        env = handler.collect.envelope(
            [_finding(), _finding(label="EC2 Instance Savings Plan")]
        )
        assert env["count"] == 2
        assert env["total_estimated_monthly_savings"] == 1600.0
        assert env["aws_best_case_monthly_savings"] == 2000.0


# ---------------------------------------------------------------------------
# One-shot aggregator — real api/analyze/report against a stub CE client
# ---------------------------------------------------------------------------


class TestGenerateCommitmentAnalysis:
    def test_returns_rendered_report_and_structured_envelope(self, monkeypatch):
        _stub_clients(monkeypatch, ce=_ce_stub(), coh=_coh_stub())

        result = handler.handle_generate_commitment_analysis(
            {"families": ["sp"], "savings_plan_types": "COMPUTE_SP",
             "terms": ["ONE_YEAR"], "payment_options": ["NO_UPFRONT"]}
        )

        assert "error" not in result
        md = result["report_markdown"]
        assert md and isinstance(md, str)
        assert "Compute Savings Plan" in md
        assert result["data_source"] == "live"
        assert result["account_id"] == "123456789012"
        assert result["lookback"] == "THIRTY_DAYS"
        assert result["account_scope"] == "PAYER"
        assert result["count"] == 1

        rec = result["recommendations"][0]
        # Risk adjustment: the $8/hr floor caps the AWS-recommended $10/hr.
        assert rec["achievable_commitment"] < rec["aws_recommended_commitment"]
        assert rec["estimated_monthly_savings"] < rec["aws_best_case_monthly_savings"]

    def test_underutilized_existing_commitment_becomes_a_blocker(self, monkeypatch):
        """62% SP utilization must be surfaced before any purchase advice."""
        _stub_clients(monkeypatch, ce=_ce_stub(), coh=_coh_stub())
        result = handler.handle_generate_commitment_analysis({"families": ["sp"]})
        assert result["blockers"], "under-utilized SPs must block"
        assert any("utilized" in b for b in result["blockers"])
        assert result["existing_commitment_posture"]["sp_utilization_pct"] == 62.0

    def test_coh_is_skipped_when_not_enrolled(self, monkeypatch):
        """Querying COH unenrolled only yields an access error, so gate on it."""
        coh = _coh_stub(enrolled=False)
        _stub_clients(monkeypatch, ce=_ce_stub(), coh=coh)
        result = handler.handle_generate_commitment_analysis({"families": ["sp"]})
        assert result["reconciliation"]["status"] == "unavailable"
        coh.get_paginator.assert_not_called()

    def test_reconciles_against_coh_when_enrolled(self, monkeypatch):
        coh = _coh_stub(recommendations=[{
            "recommendationId": "r-1", "accountId": "123456789012",
            "region": "us-east-1", "currentResourceType": "",
            "recommendedResourceType": "ComputeSavingsPlans",
            "actionType": "PurchaseSavingsPlans",
            "estimatedMonthlySavings": 1000.0,
            "estimatedSavingsPercentage": 22.0,
            "implementationEffort": "VeryLow",
        }])
        _stub_clients(monkeypatch, ce=_ce_stub(), coh=coh)
        result = handler.handle_generate_commitment_analysis(
            {"families": ["sp"], "savings_plan_types": "COMPUTE_SP",
             "terms": ["ONE_YEAR"], "payment_options": ["NO_UPFRONT"]}
        )
        recon = result["reconciliation"]
        # CE best case ($1000) matches COH exactly.
        assert recon["status"] == "reconciled"
        assert recon["coh_monthly_savings"] == 1000.0
        assert recon["ce_monthly_savings"] == 1000.0

    def test_no_opportunity_is_a_real_result_not_an_error(self, monkeypatch):
        ce = _ce_stub(get_savings_plans_purchase_recommendation=_sp_response(
            hourly="0.0", monthly_savings="0.0", minimum="0.0", average="0.0",
        ))
        _stub_clients(monkeypatch, ce=ce, coh=_coh_stub())
        result = handler.handle_generate_commitment_analysis({"families": ["sp"]})
        assert "error" not in result
        assert result["count"] == 0
        assert result["recommendations"] == []
        assert result["report_markdown"]

    def test_failed_permutation_is_reported_as_a_warning(self, monkeypatch):
        ce = _ce_stub()
        ce.get_savings_plans_purchase_recommendation.side_effect = RuntimeError(
            "ThrottlingException"
        )
        _stub_clients(monkeypatch, ce=ce, coh=_coh_stub())
        result = handler.handle_generate_commitment_analysis(
            {"families": ["sp"], "savings_plan_types": "COMPUTE_SP",
             "terms": ["ONE_YEAR"], "payment_options": ["NO_UPFRONT"]}
        )
        assert "error" not in result
        assert result["collection_warnings"]
        assert result["queries_run"] == 1

    def test_families_filter_skips_the_other_sweep(self, monkeypatch):
        ce = _ce_stub()
        _stub_clients(monkeypatch, ce=ce, coh=_coh_stub())
        handler.handle_generate_commitment_analysis({"families": ["sp"]})
        ce.get_reservation_purchase_recommendation.assert_not_called()

    def test_unexpected_failure_returns_access_denied_hint(self, monkeypatch):
        def boom():
            raise RuntimeError("AccessDeniedException: ce:GetSavingsPlansPurchaseRecommendation")

        monkeypatch.setattr(handler, "_get_clients", boom)
        result = handler.handle_generate_commitment_analysis({})
        assert "AccessDenied" in result["error"]
        assert "ce:GetSavingsPlansPurchaseRecommendation" in result["hint"]

    def test_data_unavailable_gets_its_own_hint(self, monkeypatch):
        def boom():
            raise RuntimeError("DataUnavailableException")

        monkeypatch.setattr(handler, "_get_clients", boom)
        result = handler.handle_generate_commitment_analysis({})
        assert "shorter lookback" in result["hint"]


# ---------------------------------------------------------------------------
# Narrow sizing tools
# ---------------------------------------------------------------------------


class TestSizingTools:
    def test_size_savings_plans_envelope(self, monkeypatch):
        _stub_clients(monkeypatch, ce=_ce_stub(), coh=_coh_stub())
        result = handler.handle_size_savings_plans(
            {"savings_plan_types": "COMPUTE_SP", "terms": ["ONE_YEAR"],
             "payment_options": ["NO_UPFRONT"]}
        )
        assert result["savings_plan_types"] == ["COMPUTE_SP"]
        assert result["count"] == 1
        assert result["data_source"] == "live"
        # The narrow tool has no posture gate, so it must say so.
        assert "get_commitment_posture" in result["note"]

    def test_size_reservations_reports_short_service_labels(self, monkeypatch):
        _stub_clients(monkeypatch, ce=_ce_stub(), coh=_coh_stub())
        result = handler.handle_size_reservations(
            {"ri_services": "EC2, RDS", "terms": ["ONE_YEAR"],
             "payment_options": ["NO_UPFRONT"]}
        )
        assert result["services"] == ["EC2", "RDS"]
        assert result["count"] == 0  # stub returns no RI recommendations

    def test_size_reservations_does_not_query_savings_plans(self, monkeypatch):
        ce = _ce_stub()
        _stub_clients(monkeypatch, ce=ce, coh=_coh_stub())
        handler.handle_size_reservations({"ri_services": "EC2"})
        ce.get_savings_plans_purchase_recommendation.assert_not_called()


class TestGetCommitmentPosture:
    def test_returns_posture_blockers_and_top_services(self, monkeypatch):
        _stub_clients(monkeypatch, ce=_ce_stub(), coh=_coh_stub())
        result = handler.handle_get_commitment_posture({"posture_days": 30})

        assert result["window_days"] == 30
        assert result["safe_to_buy_more"] is False  # 62% utilization blocks
        assert result["blockers"]
        assert result["cost_optimization_hub"]["enrolled"] is True
        assert result["spend_periods"][0]["total"] == 10500.0
        top = result["top_services_latest_period"]
        assert list(top)[0] == "Amazon Elastic Compute Cloud - Compute"
        # Commitments do not apply to S3 — the note must say why a big bill can
        # still yield no recommendation.
        assert "cannot be committed against" in result["spend_note"]

    def test_healthy_posture_allows_buying(self, monkeypatch):
        ce = _ce_stub(get_savings_plans_utilization={
            "Total": {"Utilization": {"UtilizationPercentage": "99.5",
                                      "UnusedCommitment": "1.00"}}
        })
        _stub_clients(monkeypatch, ce=ce, coh=_coh_stub())
        result = handler.handle_get_commitment_posture({})
        assert result["blockers"] == []
        assert result["safe_to_buy_more"] is True

    def test_no_spend_data_yields_empty_top_services(self, monkeypatch):
        ce = _ce_stub(get_cost_and_usage={"ResultsByTime": []})
        _stub_clients(monkeypatch, ce=ce, coh=_coh_stub())
        result = handler.handle_get_commitment_posture({})
        assert result["spend_periods"] == []
        assert result["top_services_latest_period"] == {}


# ---------------------------------------------------------------------------
# Deployment wiring — tools.json / hierarchy.json / report template
# ---------------------------------------------------------------------------


class TestDeploymentWiring:
    """The tool is only reachable if all four registration points agree."""

    @staticmethod
    def _json(path: Path):
        import json

        return json.loads(path.read_text(encoding="utf-8"))

    def test_tools_json_declares_every_dispatched_tool(self):
        cfg = self._json(_REPO_ROOT / "src" / "lambda" / "mcp" / "tools.json")
        assert "commitments" in cfg
        entry = cfg["commitments"]
        declared = {t["name"] for t in entry["tools"]}
        # Read the dispatcher out of the source rather than restating it: a tool
        # the handler answers but tools.json omits is undeployable, and one
        # tools.json advertises but the handler drops is a broken promise to the
        # agent. Hardcoding the list here only catches the first the day someone
        # remembers to edit it.
        dispatched = set(
            re.findall(
                r'^\s+"(\w+)": handle_\w+,',
                _HANDLER_PATH.read_text(encoding="utf-8"),
                re.MULTILINE,
            )
        )
        assert dispatched, "dispatcher table not found in handler.py"
        assert declared == dispatched
        assert entry["handler"] == "handler.handler"

    def test_tools_json_grants_the_documented_iam_actions(self):
        cfg = self._json(_REPO_ROOT / "src" / "lambda" / "mcp" / "tools.json")
        actions = set(cfg["commitments"]["iam_actions"])
        # The purchase-recommendation APIs are the whole point of the tool and
        # exist nowhere else in the deployment.
        assert "ce:GetSavingsPlansPurchaseRecommendation" in actions
        assert "ce:GetReservationPurchaseRecommendation" in actions
        # Every action named in the handler docstring must actually be granted.
        documented = {
            line.strip("- ").strip()
            for line in _HANDLER_PATH.read_text(encoding="utf-8").splitlines()
            if line.startswith("- ") and (":Get" in line or ":List" in line)
        }
        assert documented <= actions

    def test_agent_is_wired_to_the_tool(self):
        hierarchy = self._json(_REPO_ROOT / "src" / "agents" / "hierarchy.json")
        agent = hierarchy["cost-operations-agent"]
        assert "commitments" in agent["tools"]
        assert "generate_commitment_analysis" in agent["prompt"]

    def test_report_template_is_bundled_for_both_consumers(self):
        """The agent loads templates from src/agents/shared; the frontend gets
        its list from the core-api Lambda's own bundled copy. Both must have it."""
        agent_copy = (_REPO_ROOT / "src" / "agents" / "shared" /
                      "report_templates" / "discounted_commitments.json")
        api_copy = (_REPO_ROOT / "src" / "lambda" / "frontend" / "core-api" /
                    "report_templates" / "discounted_commitments.json")
        assert agent_copy.read_bytes() == api_copy.read_bytes()
        template = self._json(agent_copy)
        assert len(template["sections"]) == 1
        prompt = template["sections"][0]["prompt"]
        assert "generate_commitment_analysis" in prompt
        assert "report_markdown" in prompt
