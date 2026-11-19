"""Fixture mode: run the real supervisor in-process against stubbed children.

The supervisor is built the same way ``create_frontend_agent`` builds it in
production — ``hierarchy.json`` prompt, registry-style agent listing, the
platform no-fabrication preamble, and the tool inventory block — but each
child-agent tool returns a canned response from the scenario instead of
calling ``InvokeAgentRuntime``. The supervisor model still runs on Bedrock,
so routing and answer quality are genuinely exercised; only the evidence is
fixed. No AWS resources are inspected.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evals.scenarios import ChildFixture, Scenario

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
HIERARCHY_PATH = REPO_ROOT / "src" / "agents" / "hierarchy.json"

_UNSCRIPTED = ChildFixture(
    status="error",
    error_message="[eval fixture] no canned response for this agent in this scenario",
)


def load_hierarchy() -> dict[str, Any]:
    return json.loads(HIERARCHY_PATH.read_text(encoding="utf-8"))


def child_agents(hierarchy: dict[str, Any], root: str = "supervisor") -> list[str]:
    return list(hierarchy[root].get("children") or [])


@dataclass
class FixtureRun:
    session_id: str
    response_text: str
    spans: list[dict[str, Any]]
    delegations: list[dict[str, str]] = field(default_factory=list)
    system_prompt_sha256: str = ""
    model_id: str = ""
    error: str | None = None


def _make_stub_tool(agent_name: str, description: str, fixtures: dict[str, ChildFixture], calls: list):
    from strands import tool

    @tool(name=agent_name, description=description or f"Delegate tasks to the {agent_name}")
    def _delegate(prompt: str) -> dict:
        """Send *prompt* to the Sub-Agent and return its result.

        Args:
            prompt: The task description or question to delegate.
        """
        calls.append({"agent": agent_name, "prompt": prompt})
        return fixtures.get(agent_name, _UNSCRIPTED).as_tool_result(agent_name)

    return _delegate


def build_supervisor_prompt(hierarchy: dict[str, Any], tools: list, override: str | None) -> str:
    """Reproduce the production frontend prompt assembly.

    With ``override`` (negative controls only) the supervisor template is
    replaced AND the platform no-fabrication preamble is omitted, so the
    deliberately degraded agent is not silently corrected by it.
    """
    from agents.shared.agent_base import _apply_platform_preamble, _inject_tool_inventory
    from agents.shared.prompt import _build_agent_listing, _inject_date

    template = override if override is not None else hierarchy["supervisor"]["prompt"]
    listing = _build_agent_listing(
        [
            {"agent_name": name, "description": hierarchy[name].get("description", ""), "enabled": True}
            for name in child_agents(hierarchy)
        ]
    )
    base = _inject_tool_inventory(_inject_date(template.replace("{agent_listing}", listing)), tools)
    return base if override is not None else _apply_platform_preamble(base)


def run_fixture_scenario(scenario: Scenario, capture, *, model_id: str | None, region: str) -> FixtureRun:
    """Execute one fixture scenario and return its captured ADOT spans."""
    import hashlib

    from strands import Agent
    from strands.models import BedrockModel

    hierarchy = load_hierarchy()
    model_id = model_id or hierarchy["supervisor"]["model"]
    calls: list[dict[str, str]] = []
    tools = [
        _make_stub_tool(name, hierarchy[name].get("description", ""), scenario.fixtures, calls)
        for name in child_agents(hierarchy)
    ]
    system_prompt = build_supervisor_prompt(hierarchy, tools, scenario.system_prompt_override)
    # AgentCore Runtime session ids must be >= 33 chars; keep the same shape.
    session_id = f"eval-{scenario.id}-{uuid.uuid4()}"

    capture.drain_adot()  # discard anything left from a previous scenario
    agent = Agent(
        name="supervisor",
        model=BedrockModel(model_id=model_id, region_name=region),
        tools=tools,
        system_prompt=system_prompt,
        callback_handler=None,
        trace_attributes={"session.id": session_id, "eval.scenario_id": scenario.id},
    )
    text, error = "", None
    try:
        text = str(agent(scenario.prompt))
    except Exception as exc:  # noqa: BLE001 - recorded as an execution error
        logger.exception("Scenario %s failed during agent execution", scenario.id)
        error = f"{type(exc).__name__}: {exc}"

    return FixtureRun(
        session_id=session_id,
        response_text=text,
        spans=capture.drain_adot(),
        delegations=calls,
        system_prompt_sha256=hashlib.sha256(system_prompt.encode("utf-8")).hexdigest(),
        model_id=model_id,
        error=error,
    )
