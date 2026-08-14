"""Shared agent registry for hierarchical agent discovery.

Loads agent configurations from a DynamoDB registry table or falls back
to environment variables. Supports hierarchical filtering via the
``parent_agent`` attribute so that mid-level agents can discover their
children and the Supervisor can discover top-level agents.

Builds ``@tool``-wrapped functions that invoke child agents via the
``InvokeAgentRuntime`` API (SigV4-signed).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any
from urllib.parse import unquote

import boto3
from botocore.config import Config
from strands import tool

logger = logging.getLogger(__name__)

# Module-level handler for the current request. AgentCore Runtime processes
# one request at a time per container, so module-level is safe. Thread-local
# does NOT work because Strands runs tools in a ThreadPoolExecutor.
_current_handler = None


# ---------------------------------------------------------------------------
# Registry loading
# ---------------------------------------------------------------------------


def load_agent_registry(
    table_name: str | None = None,
    parent_filter: str | None = None,
) -> list[dict]:
    """Load agent configs from DynamoDB or environment variables.

    Tries DynamoDB first. If the table is unavailable, falls back to the
    ``SUB_AGENT_CONFIGS`` environment variable (JSON array).

    Args:
        table_name: DynamoDB table name. Defaults to the
            ``AGENT_REGISTRY_TABLE`` env var, then ``"cloudops-agent-registry"``.
        parent_filter: Controls hierarchical filtering:
            - If a non-empty string, return agents whose ``parent_agent``
              matches that value (i.e. children of a specific parent).
            - If ``None`` or empty string, return agents with empty or
              absent ``parent_agent`` (i.e. top-level agents).

    Returns:
        A list of normalised agent configuration dicts.
    """
    table_name = table_name or os.environ.get(
        "AGENT_REGISTRY_TABLE", "cloudops-agent-registry"
    )

    # --- Try DynamoDB first ---------------------------------------------------
    try:
        dynamodb = boto3.resource(
            "dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1")
        )
        table = dynamodb.Table(table_name)
        response = table.scan()
        items: list[dict] = response.get("Items", [])

        # Handle pagination
        while "LastEvaluatedKey" in response:
            response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
            items.extend(response.get("Items", []))

        if items:
            logger.info(
                "Loaded %d agent(s) from DynamoDB table '%s'", len(items), table_name
            )
            normalised = _normalise_items(items)
            return _filter_by_parent(normalised, parent_filter)

        logger.warning(
            "DynamoDB table '%s' is empty, falling back to env vars", table_name
        )
    except Exception:
        logger.warning(
            "Could not read DynamoDB table '%s', falling back to env vars",
            table_name,
            exc_info=True,
        )

    # --- Fallback: environment variables --------------------------------------
    return _filter_by_parent(_load_from_env(), parent_filter)


def _filter_by_parent(items: list[dict], parent_filter: str | None) -> list[dict]:
    """Filter normalised items by ``parent_agent``.

    Args:
        items: Normalised agent config dicts.
        parent_filter: If a non-empty string, keep items whose
            ``parent_agent`` equals it. Otherwise keep items with
            empty/absent ``parent_agent`` (top-level agents).
    """
    if parent_filter:
        return [i for i in items if i.get("parent_agent", "") == parent_filter]
    # Top-level: parent_agent is empty or absent
    return [i for i in items if not i.get("parent_agent", "")]


def _normalise_items(items: list[dict]) -> list[dict]:
    """Ensure every registry item has the expected keys with correct types."""
    normalised: list[dict] = []
    for item in items:
        normalised.append(
            {
                "agent_name": str(item.get("agent_name", "")),
                "a2a_endpoint": str(item.get("a2a_endpoint", "")),
                "enabled": _to_bool(item.get("enabled", True)),
                "description": str(item.get("description", "")),
                "parent_agent": str(item.get("parent_agent", "")),
            }
        )
    return normalised


def _to_bool(value: Any) -> bool:
    """Convert DynamoDB boolean (may arrive as string) to Python bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value)


def _load_from_env() -> list[dict]:
    """Load agent configs from the ``SUB_AGENT_CONFIGS`` env var (JSON array)."""
    raw = os.environ.get("SUB_AGENT_CONFIGS", "")
    if not raw:
        logger.info("No SUB_AGENT_CONFIGS env var set — no Sub-Agents available")
        return []

    try:
        configs = json.loads(raw)
        if not isinstance(configs, list):
            logger.error("SUB_AGENT_CONFIGS must be a JSON array")
            return []
        logger.info("Loaded %d agent(s) from SUB_AGENT_CONFIGS env var", len(configs))
        return _normalise_items(configs)
    except json.JSONDecodeError:
        logger.error("SUB_AGENT_CONFIGS is not valid JSON")
        return []


# ---------------------------------------------------------------------------
# Tool building
# ---------------------------------------------------------------------------


def build_agent_tools(registry: list[dict], timeout: int = 120) -> list:
    """Create ``@tool``-wrapped ``A2AAgent`` instances from registry entries.

    Only *enabled* agents with a non-empty ``a2a_endpoint`` are included.

    Args:
        registry: List of agent config dicts from :func:`load_agent_registry`.
        timeout: Per-agent invocation timeout in seconds.

    Returns:
        A list of tool functions that the orchestrating agent can call.
    """
    tools: list = []
    for entry in registry:
        if not entry.get("enabled", False):
            logger.info("Skipping disabled agent '%s'", entry.get("agent_name"))
            continue

        endpoint = entry.get("a2a_endpoint", "")
        if not endpoint:
            logger.warning(
                "Agent '%s' has no a2a_endpoint, skipping", entry.get("agent_name")
            )
            continue

        agent_tool = _make_agent_tool(
            agent_name=entry["agent_name"],
            endpoint=endpoint,
            description=entry.get("description", ""),
            timeout=timeout,
        )
        tools.append(agent_tool)
        logger.info(
            "Registered tool for agent '%s' at %s", entry["agent_name"], endpoint
        )

    return tools


def set_current_handler(handler: Any) -> None:
    """Set the TracingCallbackHandler for the current request."""
    global _current_handler
    _current_handler = handler


def get_current_handler() -> Any:
    """Get the TracingCallbackHandler for the current request, or None."""
    return _current_handler


# ---------------------------------------------------------------------------
# Typed CloudFormation artifact transport
#
# CloudWatch extracts templates from its raw MCP tool result before that result
# is exposed to an agent model or a trace. The envelope below is internal-only:
# leaf agents attach it to their runtime response, each orchestrator forwards
# it one hop, and the supervisor persists it as a report. The template body is
# never parsed from model text.
#
# Module-level state is safe because AgentCore Runtime processes one request
# at a time per container (same rationale as _current_handler).
# ---------------------------------------------------------------------------

_pending_cfn_artifacts: list[dict] = []
_outbound_cfn_artifacts: list[dict] = []
_is_frontend_container = False


def mark_frontend_container() -> None:
    """Flag this process as the supervisor/frontend container."""
    global _is_frontend_container
    _is_frontend_container = True


def get_pending_cfn_artifacts() -> list[dict]:
    """Pop typed artifacts awaiting supervisor report persistence."""
    global _pending_cfn_artifacts
    items = list(_pending_cfn_artifacts)
    _pending_cfn_artifacts = []
    return items


def drain_outbound_cfn_artifacts() -> list[dict]:
    """Pop typed artifacts an intermediate agent must forward upstream."""
    global _outbound_cfn_artifacts
    items = list(_outbound_cfn_artifacts)
    _outbound_cfn_artifacts = []
    return items


def _queue_cfn_artifacts(artifacts: list[dict]) -> None:
    """Route validated typed CloudFormation artifacts by container role."""
    valid = [
        artifact
        for artifact in artifacts
        if isinstance(artifact, dict)
        and artifact.get("kind") == "cloudformation-template"
        and isinstance(artifact.get("template_yaml"), str)
        and artifact["template_yaml"]
    ]
    if len(valid) != len(artifacts):
        logger.warning("Ignoring malformed CloudFormation artifact envelope")
    if not valid:
        return
    if _is_frontend_container:
        _pending_cfn_artifacts.extend(valid)
    else:
        _outbound_cfn_artifacts.extend(valid)


def _make_agent_tool(
    agent_name: str,
    endpoint: str,
    description: str,
    timeout: int,
):
    """Build a single ``@tool`` function that invokes a child agent via InvokeAgentRuntime."""
    # Extract runtime ARN and qualifier from the endpoint URL
    # Format: https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded_arn}/invocations?qualifier={endpoint_name}
    runtime_arn = ""
    qualifier = "DEFAULT"
    try:
        parts = endpoint.split("/runtimes/")
        if len(parts) == 2:
            rest = parts[1]
            arn_part, _, query = rest.partition("/invocations")
            runtime_arn = unquote(arn_part)
            if "qualifier=" in query:
                qualifier = query.split("qualifier=")[1].split("&")[0]
    except Exception:
        logger.warning(
            "Could not parse endpoint URL for '%s': %s", agent_name, endpoint
        )

    region = os.environ.get("AWS_REGION", "us-east-1")

    @tool(
        name=agent_name,
        description=description or f"Delegate tasks to the {agent_name}",
    )
    def _delegate(prompt: str) -> dict:
        """Send *prompt* to the Sub-Agent via InvokeAgentRuntime and return the result.

        Args:
            prompt: The task description or question to delegate.

        Returns:
            A dict with ``agent_name``, ``status``, and ``data`` or ``error_message``.
            If the sub-agent includes a ``tool_trace`` in its response, it is
            forwarded so the caller can surface nested tool call metadata.
        """
        if not runtime_arn:
            return {
                "agent_name": agent_name,
                "status": "error",
                "error_message": f"No valid runtime ARN for agent '{agent_name}'",
            }
        try:
            # Heavy tool chains (discover_dx_topology + assess_dx_resiliency,
            # 22-rule best-practice eval + model summary across a multi-region
            # topology) can exceed the default 60s boto3 socket timeout. Raise
            # read_timeout to 5 min so a deep chain on a large topology
            # completes in-stream instead of aborting the supervisor→leaf
            # delegation with a Read timeout error. See
            # temp/nr-delegation-read-timeout.md for the failure mode this
            # fixes. retries.max_attempts=1 means no auto-retry — a 5-min
            # call that already failed is almost certainly not recoverable
            # by retrying.
            cfg = Config(
                read_timeout=300,
                connect_timeout=10,
                retries={"max_attempts": 1},
            )
            client = boto3.client("bedrock-agentcore", region_name=region, config=cfg)
            payload_bytes = json.dumps({"prompt": prompt}).encode("utf-8")
            response = client.invoke_agent_runtime(
                agentRuntimeArn=runtime_arn,
                qualifier=qualifier,
                payload=payload_bytes,
                contentType="application/json",
                accept="application/json",
            )
            # Response body is in the 'response' key (streaming body)
            result_bytes = response.get("response", b"")
            if hasattr(result_bytes, "read"):
                result_text = result_bytes.read().decode("utf-8")
            elif isinstance(result_bytes, bytes):
                result_text = result_bytes.decode("utf-8")
            else:
                result_text = str(result_bytes)

            # Try to parse structured response with tool_trace
            # The response may be: plain text, JSON dict, or double-encoded JSON string
            result = {
                "agent_name": agent_name,
                "status": "success",
                "data": result_text,
            }
            try:
                parsed = json.loads(result_text)
                # If parsed is a string, it was double-encoded — parse again
                if isinstance(parsed, str):
                    try:
                        parsed = json.loads(parsed)
                    except (json.JSONDecodeError, TypeError):
                        result["data"] = parsed
                        parsed = None
                if isinstance(parsed, dict):
                    if "tool_trace" in parsed:
                        result["tool_trace"] = parsed["tool_trace"]
                    # An intermediate (orchestrator) child forwards already
                    # extracted artifacts out of band under "cfn_artifacts".
                    # Re-queue them here so they continue travelling up to
                    # the supervisor (where they finally get persisted +
                    # emitted). The child already stripped the YAML from its
                    # text, so the model never sees it at this hop either.
                    forwarded_artifacts = parsed.get("cfn_artifacts")
                    if isinstance(forwarded_artifacts, list) and forwarded_artifacts:
                        _queue_cfn_artifacts(forwarded_artifacts)
                        logger.info(
                            "Delegate %s: forwarded %d cfn-artifact(s) from child",
                            agent_name,
                            len(forwarded_artifacts),
                        )
                    result["data"] = parsed.get(
                        "response", parsed.get("data", result_text)
                    )
            except (json.JSONDecodeError, TypeError):
                pass

            logger.info(
                "Delegate %s result: has_trace=%s, data_len=%d",
                agent_name,
                "tool_trace" in result,
                len(str(result.get("data", ""))),
            )

            # Register completion on the current handler so the trace
            # gets input, output, duration, and nested sub-agent traces.
            handler = get_current_handler()
            if handler:
                output_val = str(result.get("data", ""))
                nested_val = result.get("tool_trace")
                handler.complete_tool(
                    tool_name=agent_name,
                    output=output_val,
                    nested_trace=nested_val,
                    input_data={"prompt": prompt},
                )

            return result
        except TimeoutError:
            logger.error("Timeout calling agent '%s'", agent_name)
            handler = get_current_handler()
            if handler:
                handler.fail_tool(agent_name, f"Timed out after {timeout}s")
            return {
                "agent_name": agent_name,
                "status": "timeout",
                "error_message": f"Agent '{agent_name}' timed out after {timeout}s",
            }
        except Exception as exc:
            logger.error("Error calling agent '%s': %s", agent_name, exc, exc_info=True)
            handler = get_current_handler()
            if handler:
                handler.fail_tool(agent_name, str(exc))
            return {
                "agent_name": agent_name,
                "status": "error",
                "error_message": f"Agent '{agent_name}' failed: {exc}",
            }

    return _delegate
