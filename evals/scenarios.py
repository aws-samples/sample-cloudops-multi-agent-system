"""Scenario definitions and loading.

A scenario is one user prompt plus everything needed to judge the response:
which child agents the supervisor should delegate to, natural-language
assertions for the evaluators, and (fixture mode only) the canned child-agent
responses that make the run repeatable.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

MODES = ("fixture", "live")

# What the scenario is designed to exercise. ``negative_control`` scenarios
# deliberately degrade the agent so the suite can show that evaluators
# actually discriminate between good and poor behaviour.
KINDS = (
    "success",
    "tool_failure",
    "insufficient_evidence",
    "unsupported_request",
    "negative_control",
)

FIXTURE_STATUSES = ("success", "error", "timeout")

SUITES_DIR = Path(__file__).parent / "suites"


@dataclass(frozen=True)
class ChildFixture:
    """Canned response returned by a stubbed child-agent tool.

    Mirrors the dict shape returned by ``agents.shared.registry._delegate`` so
    the supervisor sees exactly what it would see in production.
    """

    status: str = "success"
    data: str = ""
    error_message: str = ""

    def as_tool_result(self, agent_name: str) -> dict[str, Any]:
        if self.status == "success":
            return {"agent_name": agent_name, "status": "success", "data": self.data}
        return {
            "agent_name": agent_name,
            "status": self.status,
            "error_message": self.error_message or f"Agent '{agent_name}' failed",
        }


@dataclass(frozen=True)
class Scenario:
    id: str
    mode: str
    domain: str
    kind: str
    prompt: str
    description: str = ""
    expected_route: tuple[str, ...] = ()
    assertions: tuple[str, ...] = ()
    expected_response: str | None = None
    evaluators: tuple[str, ...] = ()
    fixtures: dict[str, ChildFixture] = field(default_factory=dict)
    # Negative controls only: replaces the supervisor's routing prompt.
    system_prompt_override: str | None = None
    # Free-form expectation of how scores should look, for the summary.
    expected_outcome: str = ""

    @property
    def data_source(self) -> str:
        return "fixture" if self.mode == "fixture" else "live"


class ScenarioError(ValueError):
    """Raised when a suite file is malformed."""


def _tuple_of_str(raw: Any, where: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ScenarioError(f"{where} must be a list of strings")
    return tuple(raw)


def _parse_scenario(raw: dict[str, Any], suite_mode: str, where: str) -> Scenario:
    if not isinstance(raw, dict):
        raise ScenarioError(f"{where}: scenario must be a mapping")
    for key in ("id", "domain", "kind", "prompt"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            raise ScenarioError(f"{where}: '{key}' is required")
    sid = raw["id"]
    where = f"{where} ({sid})"
    mode = raw.get("mode", suite_mode)
    if mode not in MODES:
        raise ScenarioError(f"{where}: mode must be one of {MODES}")
    if mode != suite_mode:
        raise ScenarioError(f"{where}: mode '{mode}' does not match suite '{suite_mode}'")
    if raw["kind"] not in KINDS:
        raise ScenarioError(f"{where}: kind must be one of {KINDS}")

    fixtures: dict[str, ChildFixture] = {}
    raw_fixtures = raw.get("fixtures") or {}
    if mode == "live" and raw_fixtures:
        raise ScenarioError(f"{where}: live scenarios must not define fixtures")
    if not isinstance(raw_fixtures, dict):
        raise ScenarioError(f"{where}: fixtures must be a mapping of agent name -> response")
    for agent_name, fx in raw_fixtures.items():
        if not isinstance(fx, dict):
            raise ScenarioError(f"{where}: fixture for '{agent_name}' must be a mapping")
        status = fx.get("status", "success")
        if status not in FIXTURE_STATUSES:
            raise ScenarioError(f"{where}: fixture status must be one of {FIXTURE_STATUSES}")
        fixtures[agent_name] = ChildFixture(
            status=status,
            data=str(fx.get("data", "")),
            error_message=str(fx.get("error_message", "")),
        )

    expected_route = _tuple_of_str(raw.get("expected_route"), f"{where}: expected_route")
    if mode == "fixture":
        missing = [a for a in expected_route if a not in fixtures]
        if missing:
            raise ScenarioError(f"{where}: expected_route agents without fixtures: {missing}")

    override = raw.get("system_prompt_override")
    if override is not None and raw["kind"] != "negative_control":
        raise ScenarioError(f"{where}: system_prompt_override is only allowed for negative_control")
    if override is not None and mode != "fixture":
        raise ScenarioError(f"{where}: system_prompt_override is fixture-only")

    return Scenario(
        id=sid,
        mode=mode,
        domain=raw["domain"],
        kind=raw["kind"],
        prompt=raw["prompt"].strip(),
        description=str(raw.get("description", "")).strip(),
        expected_route=expected_route,
        assertions=_tuple_of_str(raw.get("assertions"), f"{where}: assertions"),
        expected_response=raw.get("expected_response"),
        evaluators=_tuple_of_str(raw.get("evaluators"), f"{where}: evaluators"),
        fixtures=fixtures,
        system_prompt_override=override,
        expected_outcome=str(raw.get("expected_outcome", "")).strip(),
    )


@dataclass(frozen=True)
class Suite:
    mode: str
    path: Path
    sha256: str
    default_evaluators: tuple[str, ...]
    scenarios: tuple[Scenario, ...]


def load_suite(path: Path) -> Suite:
    """Load and validate a suite YAML file."""
    text = path.read_text(encoding="utf-8")
    doc = yaml.safe_load(text) or {}
    if not isinstance(doc, dict):
        raise ScenarioError(f"{path}: top level must be a mapping")
    mode = doc.get("mode")
    if mode not in MODES:
        raise ScenarioError(f"{path}: 'mode' must be one of {MODES}")
    raw_scenarios = doc.get("scenarios") or []
    if not isinstance(raw_scenarios, list) or not raw_scenarios:
        raise ScenarioError(f"{path}: 'scenarios' must be a non-empty list")

    scenarios = tuple(
        _parse_scenario(raw, mode, f"{path.name}[{i}]")
        for i, raw in enumerate(raw_scenarios)
    )
    ids = [s.id for s in scenarios]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ScenarioError(f"{path}: duplicate scenario ids: {dupes}")

    return Suite(
        mode=mode,
        path=path,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        default_evaluators=_tuple_of_str(doc.get("default_evaluators"), f"{path}: default_evaluators"),
        scenarios=scenarios,
    )


def default_suite_path(mode: str) -> Path:
    return SUITES_DIR / f"{mode}.yaml"
