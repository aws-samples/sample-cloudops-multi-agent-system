"""
AWS Discounted Commitments MCP Tool — Lambda Implementation for AgentCore Gateway

Sizes Savings Plan and Reserved Instance purchases the workload can actually
sustain, rather than the best case the AWS recommendation APIs return.

Tools (5):
- generate_commitment_analysis: ONE-SHOT aggregator — sweeps every requested
  (term, payment) permutation across Savings Plan types and RI-eligible
  services in parallel, measures existing commitment posture per service,
  reconciles against Cost Optimization Hub, plans one purchase per usage pool,
  and returns a fully rendered markdown report plus the structured envelope.
- size_savings_plans: Savings Plans purchase recommendations only, risk-adjusted.
- size_reservations: Reserved Instance purchase recommendations only, risk-adjusted.
- get_commitment_posture: Coverage/utilization of EXISTING commitments, the
  blockers they imply, COH enrollment, and commitment-addressable spend.
- get_commitment_expiry: Which existing commitments expire soon, and whether
  to renew each — including renewals already queued.

Design note:
  All of the deterministic work — the permutation sweep, volatility
  classification, floor-based commitment sizing, break-even math, COH
  reconciliation, and markdown rendering — runs in Python here. The agent makes
  ONE tool call and gets a finished report, so the LLM spends its cycles on
  judgment rather than orchestrating dozens of sequential Cost Explorer calls.

  This file owns only the two things that are specific to being a gateway tool:
  turning a caller-supplied JSON event into validated parameters, and building
  AWS clients from the platform's cross-account helper. Everything between
  those — the sweep, the thread pools, the analysis, the envelope — comes from
  `commitments.collect`. Logic added here rather than there is logic that
  tests/unit/test_commitments_{api,analyze,collect}.py do not cover, and
  `TestHandlerDiscipline` fails the build over it.

Read-only: every AWS call is a Get*/List*/Describe* operation. Nothing is
purchased and no billable analysis is started. Cost Explorer does bill $0.01
per request; every response reports `queries_run` and its estimated cost.

Required IAM permissions: see `iam_actions` for `commitments` in
src/lambda/mcp/tools.json, and docs/agents/cost-operations.md (Mode B) for the
cross-account role's trust policy and action list.
"""

import json
import os

from commitments import api, collect, report
from shared.cross_account import CrossAccountRoleError, get_aws_client

# Aliased, not restated. A copy of these values here would let the gateway tool
# accept a term the shared pipeline rejects (or default to a different sweep
# than the skill), and nothing would catch it.
TERMS = collect.TERMS
PAYMENTS = collect.PAYMENTS
LOOKBACKS = collect.LOOKBACKS
ACCOUNT_SCOPES = collect.ACCOUNT_SCOPES
FAMILIES = collect.FAMILIES
INVENTORY_KEYS = collect.INVENTORY_KEYS

DEFAULT_TERMS = collect.DEFAULT_TERMS
DEFAULT_PAYMENTS = collect.DEFAULT_PAYMENTS
DEFAULT_LOOKBACK = collect.DEFAULT_LOOKBACK
DEFAULT_ACCOUNT_SCOPE = collect.DEFAULT_ACCOUNT_SCOPE
DEFAULT_POSTURE_DAYS = collect.DEFAULT_POSTURE_DAYS
DEFAULT_SPEND_DAYS = collect.DEFAULT_SPEND_DAYS
DEFAULT_EXPIRY_HORIZON_DAYS = collect.DEFAULT_EXPIRY_HORIZON_DAYS


def _default_region() -> str:
    """The region the standalone expiry tool defaults to.

    Reservations are regional and Lambda always sets AWS_REGION, so the tool's
    own region is the one region we can assume is interesting. Widening the
    sweep multiplies Describe* calls, so it stays opt-in via `regions`.
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
        # A summary, not the body: a full report runs to hundreds of KB, past
        # CloudWatch's 256 KB event limit, and the body is returned anyway.
        size = len(json.dumps(response, default=str))
        print(
            f"Response: {size} bytes, keys={sorted(response)}, "
            f"error={response.get('error')!r}, queries_run={response.get('queries_run')}"
        )
        return response
    return {
        "error": f"Unknown tool: {tool_name}",
        "available_tools": list(handlers.keys()),
    }


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------


def _client(service: str, region: str, alias: str | None = None):
    """One read-only client through the platform's cross-account helper.

    `require_role=True`: when a cross-account role is configured and cannot be
    assumed, fail loudly. Falling back to the execution role would analyze
    the ops account and present it as the payer's.
    """
    return get_aws_client(
        service,
        region_name=region,
        role_alias=alias,
        require_role=True,
        config=api.RETRY_CONFIG,
    )


def _account_id(alias: str | None = None) -> str:
    """Resolve the account a role speaks for.

    A failure here must not abort the report — the recommendations are still
    valid, they just carry an unknown account label. A role that cannot be
    assumed at all is different, and propagates.
    """
    try:
        return _client("sts", api.CE_REGION, alias).get_caller_identity()["Account"]
    except CrossAccountRoleError:
        raise
    except Exception as exc:  # noqa: BLE001 - label only, never fatal
        print(f"Could not resolve account id: {exc}")
        return "unknown"


def _get_clients() -> api.Clients:
    """Build the `api.Clients` record the analysis package expects.

    Built per invocation, never cached at module scope: a warm container
    outlives the 15-minute assumed-role credentials, and a cached client keeps
    the expired ones. The cross-account helper caches the *session* and
    re-assumes it before expiry, so this costs no extra STS call.

    Cost Optimization Hub uses its own role (`CROSS_ACCOUNT_ROLE_ARN_COH`) only
    when one is configured — a delegated-admin COH account. Otherwise it reads
    in the same account as Cost Explorer, so the reconciliation compares like
    with like.
    """
    coh_alias = "COH" if os.environ.get("CROSS_ACCOUNT_ROLE_ARN_COH") else None
    ce = _client("ce", api.CE_REGION)
    account_id = _account_id()
    return api.Clients(
        ce=ce,
        coh=_client("cost-optimization-hub", api.COH_REGION, coh_alias),
        account_id=account_id,
        profile=None,
        # Reservation and Savings Plan inventory are regional Describe* calls
        # with no single fixed client, so the factory routes each through the
        # same cross-account role Cost Explorer uses. Commitments live in the
        # payer/linked account being analyzed, not in the ops account.
        make_client=lambda service, region: _client(service, region),
        coh_account_id=_account_id(coh_alias) if coh_alias else account_id,
    )


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


def _resolve_regions(spec, default_region: str = "") -> list[str] | None:
    """Adapt the event's region parameter to the shared resolver.

    Region strings become part of an SDK endpoint, so they are validated by the
    shared resolver rather than passed through — the same reason the enums
    above are aliased instead of restated. With no `default_region`, an
    omitted parameter returns None: the full analysis then skips the expiry
    inventory, as its schema promises.
    """
    values = _as_list(spec, ())
    if not values and not default_region:
        return None
    try:
        return collect.resolve_regions(values, default_region)
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
        cost = {
            "queries_run": payload["queries_run"],
            "estimated_api_cost_usd": payload["estimated_api_cost_usd"],
        }
        if payload.get("fatal"):
            return {
                **payload["fatal"],
                "account_id": clients.account_id,
                "collection_warnings": payload["errors"],
                **cost,
            }

        posture = payload["posture"]
        return {
            "report_markdown": report.render(payload),
            "account_id": clients.account_id,
            "generated_at": payload["meta"]["generated_at"],
            "lookback": params["lookback"],
            "account_scope": params["account_scope"],
            # The markdown carries the line items; repeating them here doubled
            # the response for no reader.
            **collect.envelope(
                payload["findings"], pools=payload["pools"], include_lines=False
            ),
            "reconciliation": payload["reconciliation"],
            "existing_commitment_posture": posture,
            "safe_to_buy_more": payload["safe_to_buy_more"],
            "expiry": payload["expiry"],
            "blockers": posture["blockers"],
            **cost,
            "collection_warnings": payload["errors"],
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
        result = collect.size_commitments(
            clients,
            "sp",
            sp_types,
            params["terms"],
            params["payments"],
            params["lookback"],
            params["account_scope"],
        )
        if result.get("error"):
            return {"account_id": clients.account_id, **result}
        return {
            "account_id": clients.account_id,
            "lookback": params["lookback"],
            "account_scope": params["account_scope"],
            "savings_plan_types": sp_types,
            **result,
            "note": (
                "Achievable figures are risk-adjusted against each line's "
                "measured hourly floor, converted to commitment dollars. Only "
                "rows with role 'recommended' add up; 'alternative' rows cover "
                "the same usage. Check get_commitment_posture before acting — "
                "buying on top of an under-utilized commitment compounds waste."
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
        result = collect.size_commitments(
            clients,
            "ri",
            ri_services,
            params["terms"],
            params["payments"],
            params["lookback"],
            params["account_scope"],
        )
        if result.get("error"):
            return {"account_id": clients.account_id, **result}
        return {
            "account_id": clients.account_id,
            "lookback": params["lookback"],
            "account_scope": params["account_scope"],
            "services": [api.RI_SERVICE_LABELS.get(s, s) for s in ri_services],
            **result,
            "note": (
                "Reservation commitments are quoted in whole instance or "
                "capacity units, each line rounded down to the count it "
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
        ri_services = _resolve_ri_services(event.get("ri_services"))
    except ParamError as exc:
        return {"error": str(exc)}

    try:
        clients = _get_clients()
        snapshot = collect.posture_snapshot(
            clients, posture_days, spend_days, ri_services
        )
        return {
            "account_id": clients.account_id,
            "window_days": posture_days,
            **snapshot,
            "spend_note": (
                "Commitments only apply to compute and database instance "
                "usage. Serverless, storage, data transfer, and managed-API "
                "spend cannot be committed against, so a large bill with "
                "little instance usage correctly yields no recommendation."
            ),
            "safe_to_buy_more_note": (
                "null means existing commitment health could not be measured "
                "(see 'unmeasured') — not that it is healthy."
            ),
            "data_source": "live",
        }
    except Exception as e:
        return _aws_error(e)


# ---------------------------------------------------------------------------
# Tool: get_commitment_expiry
# ---------------------------------------------------------------------------


def handle_get_commitment_expiry(event):
    """When existing commitments lapse, and what to renew.

    The one tool here that is mostly not Cost Explorer: expiry dates only exist
    on the per-service Describe* APIs, and those are regional. Utilization for
    the renewal call still comes from Cost Explorer, one query per service
    found in the inventory.
    """
    try:
        regions = _resolve_regions(event.get("regions"), _default_region())
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
        snapshot = collect.expiry_snapshot(
            clients, regions, services, horizon_days, posture_days
        )
        return {
            "account_id": clients.account_id,
            **snapshot,
            "note": (
                "Utilization is per service (Cost Explorer publishes no "
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
    if isinstance(exc, CrossAccountRoleError):
        return {
            "error": message,
            "hint": (
                "The payer-account role could not be assumed. Its trust policy "
                "must name this tool's role (<project>-commitments-tool-role); "
                "see docs/agents/cost-operations.md, Mode B."
            ),
        }
    if "AccessDenied" in message or "UnauthorizedOperation" in message:
        return {
            "error": message,
            "hint": (
                "The tool role (or, cross-account, the payer-account role) is "
                "missing a permission. The full action list is `iam_actions` "
                "for `commitments` in src/lambda/mcp/tools.json; the "
                "cross-account trust policy is in docs/agents/cost-operations.md, "
                "Mode B. Cost Explorer must also be enabled for the payer account."
            ),
        }
    if "ExpiredToken" in message:
        return {
            "error": message,
            "hint": "Assumed-role credentials expired mid-call. Retry.",
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
