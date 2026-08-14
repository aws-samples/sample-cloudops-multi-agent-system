"""Shared base classes for mid-level and leaf agent entrypoints.

Eliminates boilerplate duplication across agents. Each agent's server.py
only needs to define AGENT_NAME, SYSTEM_PROMPT, and agent type.

Mid-level agents (orchestrators that delegate to child agents)::

    from shared.agent_base import create_mid_level_agent
    app, entrypoint = create_mid_level_agent(
        agent_name="finops-agent",
        prompt_template=FINOPS_PROMPT_TEMPLATE,
    )

Leaf agents (agents that call gateway MCP tools directly)::

    from agents.shared.agent_base import create_leaf_agent
    app, entrypoint = create_leaf_agent(
        agent_name="cost-operations-agent",
        system_prompt=COST_OPS_SYSTEM_PROMPT,
    )

Frontend-facing agents (AG-UI protocol with memory, suggestions, reports)::

    from agents.shared.agent_base import create_frontend_agent
    app = create_frontend_agent(
        agent_name="supervisor",
        agent_description="CloudOps Supervisor Agent",
        prompt_template=SUPERVISOR_PROMPT_TEMPLATE,
        agent_type="mid_level",
        model_id="global.anthropic.claude-opus-4-6-v1",
    )
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPAgentTool

from agents.shared.prompt import build_dynamic_prompt
from agents.shared.registry import (
    build_agent_tools,
    drain_outbound_cfn_artifacts,
    load_agent_registry,
    set_current_handler,
)
from agents.shared.tracing import TracingCallbackHandler, build_traced_response

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Common config from environment
# ---------------------------------------------------------------------------
AGENT_REGISTRY_TABLE = os.environ.get("AGENT_REGISTRY_TABLE", "cloudops-agent-registry")
DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("DEFAULT_TIMEOUT_SECONDS", "120"))
DEFAULT_MODEL_ID = os.environ.get(
    "BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0"
)
AGENTCORE_MEMORY_ID = os.environ.get("AGENTCORE_MEMORY_ID", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Tools whose raw output must NOT be persisted into the tool trace. They return
# very large payloads (a many-alarm CFN template runs 100k+ chars) AND have no
# functional consumer on reload — the saved trace is a display-only collapsed
# view, and the actual artifact is persisted separately to the reports DynamoDB
# table (rendered via the <report-pending> marker). We drop the output at the
# SOURCE (the leaf agent that calls the tool), so it never gets serialized into
# this agent's trace and therefore never appears in any enclosing agent's trace
# or the AgentCore Memory event. Matching is by bare tool name (the gateway
# "<target>___" prefix is stripped before comparison).
#
# The CloudWatch alarm-recommendation read tools belong here too: a single
# many-resource run fans out get_recommended_metric_alarms ~26× (one per
# metric), and each result is a full AWS recommendation-catalogue JSON. That
# nested trace bubbles worker -> orchestrator -> supervisor and, even after the
# CFN YAML itself is dropped, was the dominant term that pushed the saved
# assistant turn past AgentCore Memory's 100k-char per-event limit (observed
# live: a 26-alarm run produced a ~155 KB <tool> blob and a CreateEvent
# ValidationException, silently dropping the turn on reload). Their output has
# NO reload consumer — the recommendations are reflected in the generated CFN
# template, which is persisted separately to the reports table and rendered via
# ReportPanel. analyse_metric / get_metric_metadata are included for the same
# reason (verbose 14-day stats / metadata with no reload-time render path).
#
# Contrast: the DX-topology tools (discover_dx_topology / assess_dx_resiliency)
# are deliberately NOT here — the frontend rebuilds the diagram from their saved
# trace output on reload, so they must keep full fidelity.
_NO_RELOAD_TRACE_TOOLS = frozenset(
    {
        "assemble_cfn_template",
        "prepare_alarm_deployment",
        "build_cfn_alarm",
        "get_recommended_metric_alarms",
        "get_metric_metadata",
        "analyze_alarm_coverage",
        "analyse_metric",
    }
)
_ABBREVIATED_TOOL_OUTPUT = "[tool output omitted from chat memory — see ReportPanel]"


def _build_model(model_id: str) -> "BedrockModel":
    """Build a BedrockModel."""
    kwargs: dict[str, Any] = {"model_id": model_id}
    return BedrockModel(**kwargs)


# ---------------------------------------------------------------------------
# Platform-wide no-fabrication preamble
#
# Injected ahead of every agent's system prompt by the factories below.
# Agent authors do NOT opt in — this is non-negotiable, applied by the
# platform. The rules below are what distinguishes a grounded agent
# response from a plausible-sounding hallucination.
# ---------------------------------------------------------------------------
_NO_FABRICATION_PREAMBLE = """\
[PLATFORM RULES — NON-NEGOTIABLE, applied by the platform to every agent]

1. NEVER fabricate data. Specific values — numbers, percentages,
   counts, costs, timestamps, dates, account IDs, ARNs, resource
   names, event IDs, tag keys/values, region codes, IP addresses,
   instance types — MUST come from a tool call made in THIS turn.
2. If you need a value you do not have, call the appropriate tool.
   If no tool can provide it, say so explicitly: \"I don't have a tool
   that can answer that.\" Do NOT substitute a plausible value.
3. If a tool returns an error, surface the error verbatim. Do NOT
   retry silently with made-up inputs and do NOT paper over the
   failure with invented data.
4. If a tool returns empty data (e.g. an empty list or count = 0),
   report it honestly. Do NOT invent non-empty results to look helpful.
5. If you have zero tools available to you, do NOT answer the user's
   data question. Return: \"I have no tools available; I cannot
   answer this request.\" Mention that the operator should check
   the gateway and agent configuration.
6. When quoting a value returned by a tool, quote it exactly. Do
   NOT round, summarise, or rewrite numbers in a way that could
   mislead.
7. Conversation history may contain `<tool>` markers — records of
   prior-turn tool calls (name, input, output). These are evidence of
   what you did AND of what the UI rendered alongside the text
   (diagrams, reports, charts, dashboards, any visual artefact).
   When the user refers to something visual — \"the diagram\", \"that
   chart\", \"the report above\", \"this\", \"the result\" — resolve the
   reference by scanning recent `<tool>` markers before claiming you
   can't see it. Do NOT tell the user \"there is no diagram / chart /
   report\" if a corresponding tool call exists in your history.
8. If a prior-turn tool output carries a field that marks it as
   demonstration data (e.g. `mockScenario`, `sample: true`, a
   `demo_*` prefix, or any equivalent signal that the data did not
   come from the user's live environment), make this explicit in
   your response. Do NOT present demo account IDs, resource IDs,
   scores, or dollar figures as if they were real.
9. If your message history contains a `<report-pending report_id="..."
   title="..."/>` marker and the user references that report (by
   title, topic, section, or recency — "the FinOps report", "section
   2 of the cost report", "the latest report", "the one I just
   generated"), you MUST call `get_report` with the matching
   `report_id` BEFORE answering. Do NOT delegate to a domain agent
   to recompute the analysis — the rendered report is already in
   storage and `get_report` is far cheaper. Resolution rules:
     - title/topic match: match the user's phrasing against the
       `title` attribute of markers in your history (case-insensitive
       substring).
     - "the latest" / "the most recent" / "the one I just generated"
       / unqualified "the report" with multiple markers: pick the
       LAST `<report-pending>` marker in your history (most recent).
     - "the original" / "v1" with an edit lineage: a report whose
       marker has a `parent_report_id` is an edit; the parent is the
       original. Highest `version` is the latest revision.
   If the user references a report and your history contains NO
   `<report-pending>` markers, do NOT guess a `report_id`. Tell the
   user the report isn't in this conversation.
   NEVER write a `<report-pending>` marker yourself. These markers are
   injected by the platform when it persists a report; they are records
   you READ, never output. When a tool reports that a CloudFormation
   template artifact was generated, summarize the result but never output
   its YAML or a `<cfn-artifact>` block. The platform stores the typed
   artifact separately and injects the matching report marker.

Breaking any of these rules constitutes a serious operational failure.
These rules override every other instruction in this prompt.
"""


def _apply_platform_preamble(system_prompt: str) -> str:
    """Prepend the non-negotiable no-fabrication rules to every prompt."""
    return f"{_NO_FABRICATION_PREAMBLE}\n---\n\n{system_prompt}"


def _format_available_tool_names(tools: list) -> str:
    """Human-readable enumeration of tool names, used in the startup check."""
    names = []
    for t in tools or []:
        name = getattr(t, "tool_name", getattr(t, "name", ""))
        if name:
            names.append(name)
    return ", ".join(names) if names else "(none)"


def _inject_tool_inventory(system_prompt: str, tools: list) -> str:
    """Append the authoritative tool inventory to the system prompt.

    This is the SINGLE source of truth the model sees for what tools
    exist. If this list is empty, the no-fabrication preamble directs
    the model to refuse to answer.
    """
    inventory = _format_available_tool_names(tools)
    block = (
        "\n\n[AVAILABLE TOOLS — authoritative list for this turn]\n"
        f"{inventory}\n"
        "Only tools listed above are callable. Do not reference any other "
        "tool by name.\n"
    )
    return system_prompt + block


# ---------------------------------------------------------------------------
# Memory helper (shared by mid-level agents)
# ---------------------------------------------------------------------------
def build_session_manager(memory_id: str = "", memory_mode: str = "STM_ONLY"):
    """Create an AgentCoreMemorySessionManager if memory is configured."""
    memory_id = memory_id or AGENTCORE_MEMORY_ID
    if not memory_id:
        return None
    try:
        from bedrock_agentcore.memory.integrations.strands.config import (
            AgentCoreMemoryConfig,
        )
        from bedrock_agentcore.memory.integrations.strands.session_manager import (
            AgentCoreMemorySessionManager,
        )

        config = AgentCoreMemoryConfig(
            memory_id=memory_id,
            memory_mode=memory_mode,
        )
        return AgentCoreMemorySessionManager(
            agentcore_memory_config=config,
            region_name=AWS_REGION,
        )
    except Exception:
        logger.warning("Failed to initialise AgentCore Memory", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Gateway tool loading (shared by leaf agents)
# ---------------------------------------------------------------------------
def load_gateway_tools(allowed_tools: list[str] | None = None):
    """Load MCP tools from the AgentCore Gateway with SigV4 auth.

    Args:
        allowed_tools: Optional list of tool prefixes (gateway target names)
            to include. E.g. ``["cost-explorer", "cur-athena"]``. If None,
            all tools are loaded. Tool names from the gateway are prefixed
            as ``<target>___<tool_name>`` — filtering matches on the prefix.

    Returns:
        Tuple of (tools_list, gateway_client). The caller MUST call
        ``gateway_client.__exit__(None, None, None)`` in a finally block.
    """
    from agents.shared.gateway import get_gateway_mcp_client

    gateway_client = get_gateway_mcp_client()
    tools = []
    if gateway_client:
        try:
            gateway_client.__enter__()
            # AgentCore Gateway paginates tools/list at 30 per page. The
            # `PaginatedList` returned by `list_tools_sync()` is only the
            # first page — any tool whose target's schemas land on page 2+
            # is invisible to the agent unless we drain the cursor. This
            # matters as soon as the org has more than 30 gateway tools.
            all_tools = []
            page = gateway_client.list_tools_sync()
            all_tools.extend(page)
            while getattr(page, "pagination_token", None):
                page = gateway_client.list_tools_sync(
                    pagination_token=page.pagination_token
                )
                all_tools.extend(page)
            if allowed_tools:
                # Filter: keep tools whose name starts with an allowed prefix
                prefixes = tuple(f"{t}___" for t in allowed_tools)
                tools = [
                    t
                    for t in all_tools
                    if any(
                        getattr(t, "tool_name", getattr(t, "name", "")).startswith(p)
                        for p in prefixes
                    )
                ]
                logger.info(
                    "Filtered %d/%d gateway tools (allowed: %s)",
                    len(tools),
                    len(all_tools),
                    allowed_tools,
                )
            else:
                tools = all_tools
                logger.info("Loaded %d gateway tools", len(tools))
        except Exception as exc:
            logger.error("Gateway tool loading failed: %s", exc)
    if not tools:
        logger.warning("No tools available — agent will have limited capability")
    return tools, gateway_client


def cleanup_gateway(gateway_client: Any) -> None:
    """Safely close a gateway MCP client."""
    if gateway_client and not isinstance(gateway_client, list):
        try:
            gateway_client.__exit__(None, None, None)
        except Exception:
            pass


def _decode_json_content(value: Any) -> dict[str, Any] | None:
    """Decode an MCP JSON result that may have been serialized repeatedly."""
    decoded = value
    for _ in range(3):
        if isinstance(decoded, dict):
            return decoded
        if not isinstance(decoded, str):
            return None
        try:
            decoded = json.loads(decoded)
        except json.JSONDecodeError:
            return None
    return decoded if isinstance(decoded, dict) else None


def _extract_cloudformation_artifact(tool_result: dict) -> dict[str, Any] | None:
    """Return a typed artifact from a successful assembler result, if present."""
    if tool_result.get("status") != "success":
        return None

    candidates: list[Any] = [tool_result.get("structuredContent")]
    for content in tool_result.get("content", []):
        if isinstance(content, dict):
            candidates.append(content.get("json"))
            candidates.append(content.get("text"))

    for candidate in candidates:
        payload = _decode_json_content(candidate)
        if not payload:
            continue
        template_yaml = payload.get("template_yaml")
        if not isinstance(template_yaml, str) or not template_yaml:
            continue
        summary = payload.get("summary")
        if not isinstance(summary, dict):
            summary = {}
        alarm_count = summary.get("alarm_count")
        title = (
            f"CloudWatch alarms ({alarm_count})"
            if isinstance(alarm_count, int)
            else "CloudWatch alarm template"
        )
        return {
            "kind": "cloudformation-template",
            "title": title,
            "template_yaml": template_yaml,
            "summary": summary,
        }
    return None


def _artifact_acknowledgement(artifact: dict[str, Any]) -> dict[str, Any]:
    """Build the compact result that is safe to send back to the worker model."""
    return {
        "artifact": {
            "kind": artifact["kind"],
            "title": artifact["title"],
            "summary": artifact["summary"],
        },
        "message": "CloudFormation template generated and attached as an artifact.",
    }


class _CloudFormationArtifactTool(MCPAgentTool):
    """MCP adapter that diverts template YAML before an LLM receives it."""

    def __init__(self, source: MCPAgentTool, artifacts: list[dict[str, Any]]) -> None:
        super().__init__(
            source.mcp_tool,
            source.mcp_client,
            name_override=source.tool_name,
            timeout=source.timeout,
        )
        self._artifacts = artifacts

    async def stream(
        self, tool_use: Any, invocation_state: dict[str, Any], **kwargs: Any
    ):
        async for event in super().stream(tool_use, invocation_state, **kwargs):
            tool_result = getattr(event, "tool_result", None)
            artifact = (
                _extract_cloudformation_artifact(tool_result)
                if isinstance(tool_result, dict)
                else None
            )
            if not artifact:
                yield event
                continue

            self._artifacts.append(artifact)
            sanitized_result = dict(tool_result)
            sanitized_result.pop("structuredContent", None)
            sanitized_result["content"] = [
                {"text": json.dumps(_artifact_acknowledgement(artifact))}
            ]
            yield type(event)(sanitized_result, getattr(event, "exception", None))


def _wrap_cloudformation_template_tool(
    tools: list, artifacts: list[dict[str, Any]]
) -> list:
    """Wrap only the CloudWatch template assembler; all other tools are unchanged."""
    wrapped = []
    for gateway_tool in tools:
        tool_name = getattr(gateway_tool, "tool_name", "")
        bare_name = tool_name.split("___")[-1]
        if bare_name in {"assemble_cfn_template", "prepare_alarm_deployment"} and isinstance(
            gateway_tool, MCPAgentTool
        ):
            wrapped.append(_CloudFormationArtifactTool(gateway_tool, artifacts))
        else:
            wrapped.append(gateway_tool)
    return wrapped


# ---------------------------------------------------------------------------
# Mid-level agent factory
# ---------------------------------------------------------------------------
def create_mid_level_agent(
    agent_name: str,
    prompt_template: str,
    model_id: str = "",
    use_memory: bool = True,
) -> tuple[BedrockAgentCoreApp, Any]:
    """Create a mid-level orchestrator agent with registry-based child discovery.

    Returns:
        Tuple of (app, entrypoint_fn) — the app is the BedrockAgentCoreApp
        instance, entrypoint_fn is registered as ``@app.entrypoint``.
    """
    _model_id = model_id or DEFAULT_MODEL_ID
    app = BedrockAgentCoreApp()

    # Lazy-init shared components
    _cache: dict[str, Any] = {}

    def _get_components():
        if "model" not in _cache:
            base_prompt = build_dynamic_prompt(
                prompt_template, agent_name, AGENT_REGISTRY_TABLE
            )
            registry = load_agent_registry(
                table_name=AGENT_REGISTRY_TABLE, parent_filter=agent_name
            )
            _cache["tools"] = build_agent_tools(
                registry, timeout=DEFAULT_TIMEOUT_SECONDS
            )
            _cache["model"] = _build_model(_model_id)
            _cache["prompt"] = _apply_platform_preamble(
                _inject_tool_inventory(base_prompt, _cache["tools"])
            )
            if not _cache["tools"]:
                logger.warning(
                    "No child agent tools for %s — limited capability", agent_name
                )
        return _cache["model"], _cache["tools"], _cache["prompt"]

    @app.entrypoint
    def entrypoint(payload: dict) -> str:
        prompt = payload.get("prompt", "")
        if not prompt:
            return "No prompt provided"
        try:
            model, tools, system_prompt = _get_components()
            handler = TracingCallbackHandler()
            set_current_handler(handler)

            kwargs: dict[str, Any] = dict(
                model=model,
                tools=tools,
                system_prompt=system_prompt,
                callback_handler=handler,
            )
            if use_memory:
                sm = build_session_manager()
                if sm is not None:
                    kwargs["session_manager"] = sm

            agent = Agent(**kwargs)
            response = agent(prompt)
            traced = build_traced_response(response, handler)

            # Forward any CFN artifacts extracted during this run up to the
            # parent (ultimately the supervisor). Orchestrators have no event
            # loop draining the queue, so we attach them to the response dict;
            # the parent's _delegate re-queues them one hop up. Promote a
            # plain-string response to a dict when needed so the field
            # survives the hop.
            outbound = drain_outbound_cfn_artifacts()
            if outbound:
                if isinstance(traced, str):
                    traced = {"response": traced}
                traced["cfn_artifacts"] = outbound
            return traced
        except Exception as exc:
            logger.error("%s failed: %s", agent_name, exc, exc_info=True)
            return json.dumps({"error": str(exc)})

    return app, entrypoint


# ---------------------------------------------------------------------------
# Leaf agent factory
# ---------------------------------------------------------------------------
def create_leaf_agent(
    agent_name: str,
    system_prompt: str,
    model_id: str = "",
    allowed_tools: list[str] | None = None,
) -> tuple[BedrockAgentCoreApp, Any]:
    """Create a leaf agent that calls gateway MCP tools directly.

    Args:
        agent_name: Agent identifier.
        system_prompt: System prompt for the agent.
        model_id: Bedrock model ID.
        allowed_tools: Optional list of gateway target names to filter tools.
            E.g. ``["cost-explorer", "billing"]``. If None, all tools loaded.

    Returns:
        Tuple of (app, entrypoint_fn).
    """
    _model_id = model_id or DEFAULT_MODEL_ID
    app = BedrockAgentCoreApp()

    @app.entrypoint
    def entrypoint(payload: dict) -> str:
        prompt = payload.get("prompt", "")
        if not prompt:
            return "No prompt provided"
        gateway_client = None
        try:
            from agents.shared.prompt import _inject_date

            tools, gateway_client = load_gateway_tools(allowed_tools=allowed_tools)
            captured_artifacts: list[dict[str, Any]] = []
            tools = _wrap_cloudformation_template_tool(tools, captured_artifacts)

            # Fail-closed: a leaf worker with zero tools cannot answer a data
            # question without fabricating. Refuse rather than let the model
            # invent values. This catches misconfigured `tools: [...]` filters
            # AND gateway discovery failures (the exact bug that caused
            # tag-governance-agent to hallucinate on first deploy).
            if not tools:
                logger.error(
                    "%s starting with ZERO tools (allowed_tools=%s). Refusing to answer.",
                    agent_name,
                    allowed_tools,
                )
                return json.dumps(
                    {
                        "error": (
                            f"{agent_name} has no tools available — cannot "
                            "answer this request. Check the AgentCore "
                            "Gateway and the agent's `tools` filter in "
                            "hierarchy.json."
                        ),
                        "allowed_tools": allowed_tools or [],
                    }
                )

            model = _build_model(_model_id)
            handler = TracingCallbackHandler()
            set_current_handler(handler)

            final_prompt = _apply_platform_preamble(
                _inject_tool_inventory(_inject_date(system_prompt), tools)
            )
            agent = Agent(
                model=model,
                tools=tools,
                system_prompt=final_prompt,
                callback_handler=handler,
            )
            response = agent(prompt)

            # Map toolUseId -> tool name from the assistant's toolUse blocks so
            # we can identify each result by tool name (the toolResult block
            # itself carries no name). Names are gateway-prefixed here, e.g.
            # "cloudwatch___assemble_cfn_template".
            tool_names_by_id: dict[str, str] = {}
            for msg in getattr(agent, "messages", []):
                for block in msg.get("content", []):
                    if isinstance(block, dict) and "toolUse" in block:
                        tu = block["toolUse"]
                        tool_names_by_id[tu.get("toolUseId", "")] = tu.get("name", "")

            # Extract MCP tool results from agent's message history
            # (callback_handler can't capture them — ToolResultEvent.is_callback_event=False)
            for msg in getattr(agent, "messages", []):
                if msg.get("role") != "user":
                    continue
                for block in msg.get("content", []):
                    if not isinstance(block, dict) or "toolResult" not in block:
                        continue
                    tr = block["toolResult"]
                    tid = tr.get("toolUseId", "")
                    status = tr.get("status", "success")
                    # Drop the output of no-reload-consumer tools (e.g.
                    # assemble_cfn_template) AT THE SOURCE — before it is ever
                    # serialized into this leaf's trace. The CFN body is already
                    # persisted to the reports table and rendered via the
                    # ReportPanel; keeping it in the trace only bloats every
                    # enclosing agent's trace (worker -> orchestrator ->
                    # supervisor) and the AgentCore Memory event past its 100k
                    # char limit, which silently drops the whole turn on reload.
                    # Killing it here means no enclosing level ever has to find
                    # it inside a deeply string-encoded nested trace.
                    raw_name = tool_names_by_id.get(tid, "")
                    bare_name = (
                        raw_name.split("___")[-1] if "___" in raw_name else raw_name
                    )
                    if bare_name in _NO_RELOAD_TRACE_TOOLS:
                        handler.complete_tool_by_id(
                            tid, output=_ABBREVIATED_TOOL_OUTPUT, status=status
                        )
                        continue
                    content = tr.get("content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            (
                                str(x.get("text", x.get("json", "")))
                                if isinstance(x, dict)
                                else str(x)
                            )
                            for x in content
                        )
                    elif not isinstance(content, str):
                        content = str(content)
                    handler.complete_tool_by_id(tid, output=content, status=status)

            traced = build_traced_response(response, handler)
            if captured_artifacts:
                if isinstance(traced, str):
                    traced = {"response": traced}
                traced["cfn_artifacts"] = captured_artifacts
            return traced
        except Exception as exc:
            logger.error("%s failed: %s", agent_name, exc, exc_info=True)
            return json.dumps({"error": str(exc)})
        finally:
            cleanup_gateway(gateway_client)

    return app, entrypoint


# ---------------------------------------------------------------------------
# Frontend-facing agent factory (AG-UI protocol)
# ---------------------------------------------------------------------------
def create_frontend_agent(
    agent_name: str,
    agent_description: str,
    prompt_template: str,
    agent_type: str = "mid_level",
    model_id: str = "",
    memory_enabled: bool = True,
    suggestions_enabled: bool = True,
    parent_filter: str | None = None,
    allowed_tools: list[str] | None = None,
) -> "FastAPI":
    """Create a frontend-facing AG-UI agent with memory, suggestions, and reports.

    This is a higher-level factory that wires together the agent builder,
    AG-UI server, memory, suggestions, and report generation. The caller
    only needs to provide the agent name, prompt template, and feature flags.

    Unlike ``create_mid_level_agent()`` and ``create_leaf_agent()`` which
    produce HTTP-protocol agents for agent-to-agent communication, this
    factory produces a FastAPI app serving the AG-UI protocol for direct
    frontend interaction.

    Args:
        agent_name: Agent identifier (used for registry lookup and logging).
        agent_description: Human-readable description for the AG-UI wrapper.
        prompt_template: System prompt template. Supports ``{today_date}``
            and ``{agent_listing}`` placeholders. For ``mid_level`` agents,
            ``build_dynamic_prompt()`` handles both. For ``leaf`` agents,
            only ``{today_date}`` is substituted.
        agent_type: ``"mid_level"`` for orchestrators with registry-based
            child discovery, ``"leaf"`` for workers using gateway MCP tools
            directly. Callers should derive this from the promoted agent's
            ``children`` list in hierarchy.json — a non-empty ``children``
            list means ``"mid_level"``; an empty / missing ``children`` list
            means ``"leaf"``. Hardcoding ``"mid_level"`` for a worker
            promoted to frontend silently loads zero tools (frontend has no
            children to delegate to and never reaches the gateway).
        model_id: Bedrock model ID. Falls back to ``BEDROCK_MODEL_ID`` env
            var or the default model.
        memory_enabled: Whether to load/save conversation history.
        suggestions_enabled: Whether to generate follow-up suggestions.
        parent_filter: Override for registry parent filter. Defaults to
            ``agent_name`` for mid_level agents.

    Reports are always supported — the frontend triggers report mode
    by sending ``template_id`` or ``edit_report_id`` in
    ``forwardedProps``. No flag; the presence of the field is the
    trigger. Reports are request-scoped with no idle cost, so there's
    no reason to gate them on a capability flag.

    Returns:
        A FastAPI app with ``/ping`` and ``/invocations`` routes.
    """
    from agents.shared.agui_server import create_agui_app
    from agents.shared.memory import load_history
    from agents.shared.report_tool import make_get_report_tool

    _model_id = model_id or DEFAULT_MODEL_ID
    _parent_filter = parent_filter or agent_name
    _report_table_name = os.environ.get("REPORT_TABLE_NAME", "")

    # Lazy-init shared components
    _cache: dict[str, Any] = {}

    # `get_report` is added to every request's tool list when the runtime
    # has REPORT_TABLE_NAME set. The system prompt's tool inventory
    # listing reflects what the model will actually see, so include
    # `get_report` in the inventory at build time even though the bound
    # function is constructed per-request. Without this, the inventory
    # contradicts the actual tool list and the model may refuse to call
    # `get_report` (the no-fabrication preamble treats the inventory as
    # authoritative).
    class _GetReportInventoryStub:
        tool_name = "get_report"

    def build_components():
        """Lazy-init model, tools, and system prompt."""
        if "model" not in _cache:
            _cache["model"] = _build_model(_model_id)

            inventory_tools: list[Any]

            if agent_type == "mid_level":
                registry = load_agent_registry(
                    table_name=AGENT_REGISTRY_TABLE,
                    parent_filter=_parent_filter,
                )
                _cache["tools"] = build_agent_tools(
                    registry, timeout=DEFAULT_TIMEOUT_SECONDS
                )
                base_prompt = build_dynamic_prompt(
                    prompt_template, _parent_filter, AGENT_REGISTRY_TABLE
                )
                inventory_tools = list(_cache["tools"])
                if _report_table_name:
                    inventory_tools.append(_GetReportInventoryStub())
                _cache["system_prompt"] = _apply_platform_preamble(
                    _inject_tool_inventory(base_prompt, inventory_tools)
                )
                _cache["gateway_client"] = None
                if not _cache["tools"]:
                    logger.warning(
                        "No child agent tools for %s — limited capability",
                        agent_name,
                    )
            else:
                # Leaf agent: gateway MCP tools + direct prompt
                tools, gateway_client = load_gateway_tools(allowed_tools=allowed_tools)
                _cache["tools"] = tools
                _cache["gateway_client"] = gateway_client
                from agents.shared.prompt import _inject_date

                inventory_tools = list(tools)
                if _report_table_name:
                    inventory_tools.append(_GetReportInventoryStub())
                _cache["system_prompt"] = _apply_platform_preamble(
                    _inject_tool_inventory(_inject_date(prompt_template), inventory_tools)
                )
                if not tools:
                    logger.error(
                        "Frontend leaf %s starting with ZERO tools (allowed_tools=%s).",
                        agent_name,
                        allowed_tools,
                    )

        return _cache["model"], _cache["tools"], _cache["system_prompt"]

    def agent_builder(payload: dict):
        """Build a Strands Agent for each AG-UI request.

        Returns:
            Tuple of ``(Agent, system_prompt, cleanup_fn)``.
        """
        model, tools, system_prompt = build_components()

        # Extract session_id and actor_id from AG-UI forwardedProps or legacy payload
        forwarded = payload.get("forwardedProps", {})
        session_id = forwarded.get("session_id", payload.get("threadId", ""))
        actor_id = forwarded.get("actor_id", "")

        if "prompt" in payload and "threadId" not in payload:
            session_id = payload.get("session_id", "")
            actor_id = payload.get("actor_id", "")

        # Load conversation history
        history = load_history(AGENTCORE_MEMORY_ID, session_id, actor_id, AWS_REGION)

        # Per-request `get_report` tool — bound to the requester's
        # actor_id at construction time so the partition-key scope is
        # baked into the closure and the model can never escape it.
        # Built every request because actor_id varies; the cost is one
        # function-object allocation, not a boto3 client (DDB client is
        # constructed inside the tool on first call).
        request_tools = list(tools or [])
        if _report_table_name and actor_id:
            request_tools.append(
                make_get_report_tool(
                    actor_id=actor_id,
                    session_id=session_id,
                    region=AWS_REGION,
                    table_name=_report_table_name,
                )
            )

        agent_kwargs: dict[str, Any] = dict(
            model=model,
            tools=request_tools,
            system_prompt=system_prompt,
            callback_handler=None,
        )
        if history:
            agent_kwargs["messages"] = history

        agent = Agent(**agent_kwargs)

        # Leaf agents need a cleanup function to close the gateway client
        if agent_type == "leaf":
            gw = _cache.get("gateway_client")
            return agent, system_prompt, (lambda: cleanup_gateway(gw)) if gw else None
        return agent, system_prompt, None

    app = create_agui_app(
        agent_builder=agent_builder,
        config={
            "agent_name": agent_name,
            "agent_description": agent_description,
            "memory_enabled": memory_enabled,
            "suggestions_enabled": suggestions_enabled,
            "model_id": _model_id,
        },
    )
    return app
