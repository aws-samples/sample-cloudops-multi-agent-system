"""
AWS Discounted Commitments MCP Tool — Lambda Implementation for AgentCore Gateway

Sizes Savings Plan and Reserved Instance purchases the workload can actually
sustain, rather than the best case the AWS recommendation APIs return.

Tools (4):
- generate_commitment_analysis: ONE-SHOT aggregator — sweeps every requested
  (term, payment) permutation across Savings Plan types and RI-eligible
  services in parallel, measures existing commitment posture, reconciles
  against Cost Optimization Hub, and returns a fully rendered markdown report
  plus the structured envelope. Preferred fast path for reports.
- size_savings_plans: Savings Plans purchase recommendations only, risk-adjusted.
- size_reservations: Reserved Instance purchase recommendations only, risk-adjusted.
- get_commitment_posture: Coverage/utilization of EXISTING commitments, the
  blockers they imply, COH enrollment, and commitment-addressable spend.

Design note:
  All of the deterministic work — the permutation sweep, volatility
  classification, floor-based commitment sizing, break-even math, COH
  reconciliation, and markdown rendering — runs in Python here. The agent makes
  ONE tool call and gets a finished report, so the LLM spends its cycles on
  judgment (which purchase to make, in what order) rather than orchestrating
  dozens of sequential Cost Explorer calls. Same optimization as the
  lambda-runtime tool's generate_upgrade_analysis.

  This file owns only the two things that are specific to being a gateway tool:
  turning a caller-supplied JSON event into validated parameters, and building
  AWS clients from the platform's cross-account helper. Everything between
  those — the sweep, the thread pools, the analysis, the envelope — comes from
  `commitments.collect`. Logic added here rather than there is logic that
  tests/unit/test_commitments_{api,analyze,collect}.py do not cover, and
  `TestHandlerDiscipline` fails the build over it.

  The `commitments/` subpackage carries no platform imports, so it stays
  testable on its own, and it never opens a boto3 Session: the one
  profile-aware function it defines, `api.build_clients`, is unused here.
  `_get_clients` below builds the same `api.Clients` record from
  `shared.cross_account` instead.

Read-only: every AWS call is a Get*/List* operation. Nothing is purchased and
no billable analysis is started.

Required IAM Permissions:
- ce:GetSavingsPlansPurchaseRecommendation
- ce:GetReservationPurchaseRecommendation
- ce:GetSavingsPlansCoverage
- ce:GetSavingsPlansUtilization
- ce:GetReservationCoverage
- ce:GetReservationUtilization
- ce:GetCostAndUsage
- cost-optimization-hub:ListEnrollmentStatuses
- cost-optimization-hub:ListRecommendations
- sts:GetCallerIdentity
"""

import json
import os

from commitments import api, collect, report
from commitments.analyze import assess_existing_posture, select_best_findings
from shared.cross_account import get_aws_client

# Aliased, not restated. A copy of these values here would let the gateway tool
# accept a term the shared pipeline rejects (or default to a different sweep
# than the skill), and nothing would catch it.
TERMS = collect.TERMS
PAYMENTS = collect.PAYMENTS
LOOKBACKS = collect.LOOKBACKS
ACCOUNT_SCOPES = collect.ACCOUNT_SCOPES
FAMILIES = collect.FAMILIES
INVENTORY_KEYS = api.INVENTORY_KEYS

DEFAULT_TERMS = collect.DEFAULT_TERMS
DEFAULT_PAYMENTS = collect.DEFAULT_PAYMENTS
DEFAULT_LOOKBACK = collect.DEFAULT_LOOKBACK
DEFAULT_ACCOUNT_SCOPE = collect.DEFAULT_ACCOUNT_SCOPE
DEFAULT_POSTURE_DAYS = collect.DEFAULT_POSTURE_DAYS
DEFAULT_SPEND_DAYS = collect.DEFAULT_SPEND_DAYS
DEFAULT_EXPIRY_HORIZON_DAYS = collect.DEFAULT_EXPIRY_HORIZON_DAYS


def _default_region() -> str:
    """The region a reservation sweep defaults to.

    Reservations are regional and Lambda always sets AWS_REGION, so the tool's
    own region is the one region we can assume is interesting. Widening the
    sweep multiplies Describe* calls, so it stays opt-in via the `regions`
    parameter.
    """
    return os.environ.get("AWS_REGION") or os.environ.get(
        "AWS_DEFAULT_REGION", api.CE_REGION
    )


def handler(event, context):
    print(f"Event: {json.dumps(event)}")
    extended_tool_name = context.client_context.custom["bedrockAgentCoreToolName"]
    tool_name = extended_tool_name.split("___")[1]
    print(f"Tool name: {tool_name}")

    handlers = {
        "generate_commitment_analysis": handle_generate_commitment_analysis,
        "size_savings_plans": handle_size_savings_plans,
        "size_reservations": handle_size_reservations,
        "get_commitment_posture": handle_get_commitment_posture,
        "get_commitment_expiry": handle_get_commitment_expiry,
    }
    fn = handlers.get(tool_name)
    if fn:
        response = fn(event)
        print(f"Response: {json.dumps(response, default=str)}")
        return response
    return {
        "error": f"Unknown tool: {tool_name}",
        "available_tools": list(handlers.keys()),
    }


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

_clients = None


def _account_id() -> str:
    """Resolve the account the analysis speaks for.

    A failure here must not abort the report — the recommendations are still
    valid, they just carry an unknown account label.
    """
    try:
        sts = get_aws_client("sts", region_name=api.CE_REGION)
        return sts.get_caller_identity()["Account"]
    except Exception as exc:  # noqa: BLE001 - label only, never fatal
        print(f"Could not resolve account id: {exc}")
        return "unknown"


def _get_clients() -> api.Clients:
    """Build the `api.Clients` record the analysis package expects.

    The skill's `api.build_clients` is bypassed on purpose: it constructs a
    profile-based `boto3.Session`, which has no meaning in Lambda. Cross-account
    role assumption is the platform's concern, so it comes from
    `shared.cross_account` — the COH client uses the same `COH` role alias as
    the cost-optimization-hub tool, and Cost Explorer uses the default role.
    Cached at module scope so a warm container makes no repeat STS calls.
    """
    global _clients
    if _clients is None:
        _clients = api.Clients(
            ce=get_aws_client("ce", region_name=api.CE_REGION),
            coh=get_aws_client(
                "cost-optimization-hub",
                region_name=api.COH_REGION,
                role_alias="COH",
            ),
            account_id=_account_id(),
            profile=None,
            # Reservation and Savings Plan inventory are regional Describe*
            # calls with no single fixed client, so the factory routes each
            # through the same cross-account role Cost Explorer uses. Commitments
            # live in the payer/linked account being analyzed, not in the ops
            # account running this Lambda.
            make_client=lambda service, region: get_aws_client(
                service, region_name=region
            ),
        )
    return _clients


# ---------------------------------------------------------------------------
# Parameter parsing — the gateway passes tool params straight through as JSON,
# so every value arrives as caller-controlled data and is validated here.
# ---------------------------------------------------------------------------


class ParamError(ValueError):
    """A tool parameter the caller must fix. Surfaced as {"error": ...}."""


def _as_list(value, default: tuple[str, ...]) -> list[str]:
    """Accept either a JSON array or a comma-separated string."""
    if value is None or value == "":
        return list(default)
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    raise ParamError(f"Expected a list or comma-separated string, got {type(value).__name__}.")


def _validate_all(values: list[str], allowed: tuple[str, ...], label: str) -> list[str]:
    bad = [v for v in values if v not in allowed]
    if bad:
        raise ParamError(
            f"Invalid {label}: {', '.join(bad)}. Choose from {', '.join(allowed)}."
        )
    if not values:
        raise ParamError(f"No {label} requested. Choose from {', '.join(allowed)}.")
    return values


def _validate_one(value, allowed: tuple[str, ...], label: str, default: str) -> str:
    resolved = str(value).strip() if value else default
    if resolved not in allowed:
        raise ParamError(
            f"Invalid {label}: {resolved}. Choose from {', '.join(allowed)}."
        )
    return resolved


def _positive_int(value, label: str, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        days = int(value)
    except (TypeError, ValueError):
        raise ParamError(f"{label} must be a whole number of days.") from None
    if days < 1:
        raise ParamError(f"{label} must be at least 1 day.")
    return days


def _resolve_ri_services(spec) -> list[str]:
    """Adapt the event's RI-service parameter to the shared resolver.

    The event may carry a JSON array or a comma-separated string; the resolver
    takes a list and raises `ValueError`. Restating its label/case matching here
    is what caused this tool to accept names the skill rejected.
    """
    values = _as_list(spec, ("all",))
    try:
        return collect.resolve_ri_services(values)
    except ValueError as exc:
        raise ParamError(str(exc)) from None


def _resolve_sp_types(spec) -> list[str]:
    """Adapt the event's savings-plan-type parameter to the shared resolver."""
    values = _as_list(spec, ("all",))
    try:
        return collect.resolve_sp_types(values)
    except ValueError as exc:
        raise ParamError(str(exc)) from None


def _resolve_regions(spec) -> list[str]:
    """Adapt the event's region parameter to the shared resolver.

    Region strings become part of an SDK endpoint, so they are validated by the
    shared resolver rather than passed through — the same reason the enums
    above are aliased instead of restated.
    """
    values = _as_list(spec, ())
    try:
        return collect.resolve_regions(values, _default_region())
    except ValueError as exc:
        raise ParamError(str(exc)) from None


def _resolve_inventory_services(spec) -> list[str]:
    """Parse which reservation families to inventory."""
    values = [v.lower() for v in _as_list(spec, INVENTORY_KEYS)]
    return _validate_all(values, INVENTORY_KEYS, "reservation family")


def _common_params(event: dict) -> dict:
    """Parse the term/payment/lookback/scope parameters every sizing tool takes."""
    return {
        "terms": _validate_all(
            _as_list(event.get("terms"), DEFAULT_TERMS), TERMS, "term"
        ),
        "payments": _validate_all(
            _as_list(event.get("payment_options"), DEFAULT_PAYMENTS),
            PAYMENTS,
            "payment option",
        ),
        "lookback": _validate_one(
            event.get("lookback"), LOOKBACKS, "lookback", DEFAULT_LOOKBACK
        ),
        "account_scope": _validate_one(
            event.get("account_scope"),
            ACCOUNT_SCOPES,
            "account scope",
            DEFAULT_ACCOUNT_SCOPE,
        ),
    }


# ---------------------------------------------------------------------------
# Tool: generate_commitment_analysis
# ---------------------------------------------------------------------------


def handle_generate_commitment_analysis(event):
    """One-shot: sweep, size, gate on posture, reconcile, and render."""
    try:
        params = _common_params(event)
        families = [
            f.lower() for f in _as_list(event.get("families"), FAMILIES)
        ]
        _validate_all(families, FAMILIES, "commitment family")
        sp_types = _resolve_sp_types(event.get("savings_plan_types"))
        ri_services = _resolve_ri_services(event.get("ri_services"))
        posture_days = _positive_int(
            event.get("posture_days"), "posture_days", DEFAULT_POSTURE_DAYS
        )
        regions = _resolve_regions(event.get("regions"))
        expiry_horizon_days = _positive_int(
            event.get("expiry_horizon_days"),
            "expiry_horizon_days",
            DEFAULT_EXPIRY_HORIZON_DAYS,
        )
    except ParamError as exc:
        return {"error": str(exc)}

    try:
        clients = _get_clients()
        payload = collect.collect_all(
            clients,
            families=families,
            sp_types=sp_types,
            ri_services=ri_services,
            terms=params["terms"],
            payments=params["payments"],
            lookback=params["lookback"],
            account_scope=params["account_scope"],
            posture_days=posture_days,
            spend_days=DEFAULT_SPEND_DAYS,
            regions=regions,
            expiry_horizon_days=expiry_horizon_days,
        )

        posture = payload["posture"]
        errors = payload["errors"]
        raw = payload["raw"]
        return {
            "report_markdown": report.render(payload),
            "account_id": clients.account_id,
            "generated_at": payload["meta"]["generated_at"],
            "lookback": params["lookback"],
            "account_scope": params["account_scope"],
            **collect.envelope(payload["findings"]),
            "reconciliation": payload["reconciliation"],
            "existing_commitment_posture": posture,
            "expiry": payload["expiry"],
            "blockers": posture["blockers"],
            # Billable Cost Explorer requests only. The expiry Describe* calls
            # are free and are excluded on purpose — this number is what the
            # caller is charged $0.01 apiece for.
            "queries_run": (
                len(raw["sp_recs"])
                + len(raw["ri_recs"])
                + len(payload["sweep_errors"])
            ),
            "collection_warnings": errors,
            "data_source": "live",
        }
    except ValueError as exc:
        # collect_all re-validates every parameter; anything it rejects that got
        # past _common_params is still the caller's to fix, not a 500.
        return {"error": str(exc)}
    except Exception as e:
        return _aws_error(e)


# ---------------------------------------------------------------------------
# Tool: size_savings_plans / size_reservations
# ---------------------------------------------------------------------------


def handle_size_savings_plans(event):
    """Savings Plans purchase recommendations, risk-adjusted, no posture gate."""
    try:
        params = _common_params(event)
        sp_types = _resolve_sp_types(event.get("savings_plan_types"))
    except ParamError as exc:
        return {"error": str(exc)}

    try:
        clients = _get_clients()
        recs, errors = collect.sweep_savings_plans(
            clients,
            sp_types,
            params["terms"],
            params["payments"],
            params["lookback"],
            params["account_scope"],
        )
        best = select_best_findings(collect.findings_from(recs, []))
        return {
            "account_id": clients.account_id,
            "lookback": params["lookback"],
            "account_scope": params["account_scope"],
            "savings_plan_types": sp_types,
            **collect.envelope(best),
            "collection_warnings": errors,
            "note": (
                "Achievable figures are risk-adjusted against the measured "
                "hourly spend floor. Check get_commitment_posture before "
                "acting — buying on top of an under-utilized commitment "
                "compounds waste."
            ),
            "data_source": "live",
        }
    except Exception as e:
        return _aws_error(e)


def handle_size_reservations(event):
    """Reserved Instance purchase recommendations, risk-adjusted."""
    try:
        params = _common_params(event)
        ri_services = _resolve_ri_services(event.get("ri_services"))
    except ParamError as exc:
        return {"error": str(exc)}

    try:
        clients = _get_clients()
        recs, errors = collect.sweep_reservations(
            clients,
            ri_services,
            params["terms"],
            params["payments"],
            params["lookback"],
            params["account_scope"],
        )
        best = select_best_findings(collect.findings_from([], recs))
        return {
            "account_id": clients.account_id,
            "lookback": params["lookback"],
            "account_scope": params["account_scope"],
            "services": [api.RI_SERVICE_LABELS.get(s, s) for s in ri_services],
            **collect.envelope(best),
            "collection_warnings": errors,
            "note": (
                "Reservation commitments are quoted in whole instance or "
                "capacity units, rounded down to the level the workload "
                "sustains on its quietest hour."
            ),
            "data_source": "live",
        }
    except Exception as e:
        return _aws_error(e)


# ---------------------------------------------------------------------------
# Tool: get_commitment_posture
# ---------------------------------------------------------------------------


def handle_get_commitment_posture(event):
    """Health of EXISTING commitments, plus what share of spend is committable."""
    try:
        posture_days = _positive_int(
            event.get("posture_days"), "posture_days", DEFAULT_POSTURE_DAYS
        )
        spend_days = _positive_int(
            event.get("spend_days"), "spend_days", DEFAULT_SPEND_DAYS
        )
    except ParamError as exc:
        return {"error": str(exc)}

    try:
        clients = _get_clients()
        collected = collect.collect_posture(clients, posture_days, spend_days)
        posture = assess_existing_posture(
            collected["sp_coverage"],
            collected["sp_utilization"],
            collected["ri_coverage"],
            collected["ri_utilization"],
        )
        spend = collected["eligible_spend"]
        periods = [
            {"start": p["start"], "end": p["end"], "total": p["total"]}
            for p in spend.get("periods", [])
        ]
        latest = spend.get("periods", [])
        top_services = (
            dict(
                sorted(latest[-1]["by_service"].items(), key=lambda kv: -kv[1])[:10]
            )
            if latest
            else {}
        )
        return {
            "account_id": clients.account_id,
            "window_days": posture_days,
            "posture": posture,
            "blockers": posture["blockers"],
            "safe_to_buy_more": not posture["blockers"],
            "cost_optimization_hub": collected["coh_enrollment"],
            "spend_periods": periods,
            "top_services_latest_period": top_services,
            "spend_note": (
                "Commitments only apply to compute and database instance "
                "usage. Serverless, storage, data transfer, and managed-API "
                "spend cannot be committed against, so a large bill with "
                "little instance usage correctly yields no recommendation."
            ),
            "notes": posture.get("notes", []),
            "data_source": "live",
        }
    except Exception as e:
        return _aws_error(e)


# ---------------------------------------------------------------------------
# Tool: get_commitment_expiry
# ---------------------------------------------------------------------------


def handle_get_commitment_expiry(event):
    """When existing commitments lapse, and what to renew.

    The one tool here that is not Cost Explorer: expiry dates only exist on the
    per-service Describe* APIs, and those are regional. Utilization for the
    renewal call still comes from Cost Explorer, since it is the only source
    for it.
    """
    try:
        regions = _resolve_regions(event.get("regions"))
        services = _resolve_inventory_services(event.get("services"))
        horizon_days = _positive_int(
            event.get("horizon_days"), "horizon_days", DEFAULT_EXPIRY_HORIZON_DAYS
        )
        posture_days = _positive_int(
            event.get("posture_days"), "posture_days", DEFAULT_POSTURE_DAYS
        )
    except ParamError as exc:
        return {"error": str(exc)}

    try:
        clients = _get_clients()
        collected = collect.collect_posture(clients, posture_days, DEFAULT_SPEND_DAYS)
        posture = assess_existing_posture(
            collected["sp_coverage"],
            collected["sp_utilization"],
            collected["ri_coverage"],
            collected["ri_utilization"],
        )
        expiry, errors = collect.collect_expiry(
            clients,
            regions,
            services,
            horizon_days,
            sp_utilization_pct=posture.get("sp_utilization_pct"),
            ri_utilization_pct=posture.get("ri_utilization_pct"),
        )
        return {
            "account_id": clients.account_id,
            **expiry,
            "renewal_actions": expiry["actions"],
            "collection_warnings": errors,
            "note": (
                "Utilization is account-level (Cost Explorer publishes no "
                "per-commitment figure), so a renewal verdict is a portfolio "
                "signal, not a per-commitment measurement. Read-only: this "
                "reports expiry, it never renews or purchases."
            ),
            "data_source": "live",
        }
    except ValueError as exc:
        return {"error": str(exc)}
    except Exception as e:
        return _aws_error(e)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def _aws_error(exc: Exception) -> dict:
    """Normalize an unexpected failure, calling out the actionable cases."""
    message = str(exc)
    if "AccessDenied" in message or "UnauthorizedOperation" in message:
        return {
            "error": message,
            "hint": (
                "The tool role needs ce:GetSavingsPlansPurchaseRecommendation, "
                "ce:GetReservationPurchaseRecommendation, the coverage and "
                "utilization reads, and cost-optimization-hub:ListRecommendations. "
                "Cost Explorer must also be enabled for the payer account. "
                "Expiry inventory additionally needs "
                "savingsplans:DescribeSavingsPlans plus the per-service "
                "Describe* reads (ec2, rds, elasticache, redshift, es, "
                "memorydb) in each swept region."
            ),
        }
    if "DataUnavailable" in message:
        return {
            "error": message,
            "hint": (
                "Cost Explorer has no usage history for the requested lookback "
                "window. Try a shorter lookback, or accept that the account has "
                "no commitment-addressable usage yet."
            ),
        }
    return {"error": message}
