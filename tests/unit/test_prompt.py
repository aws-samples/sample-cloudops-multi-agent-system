"""Unit tests for agents.shared.prompt — dynamic system prompt generation."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agents.shared.prompt import _build_agent_listing, build_dynamic_prompt

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HIERARCHY_PATH = _REPO_ROOT / "src" / "agents" / "hierarchy.json"


class TestBuildAgentListing:
    """Tests for the internal _build_agent_listing helper."""

    def test_empty_registry_returns_no_agents_message(self):
        result = _build_agent_listing([])
        assert "No child agents are currently deployed" in result
        assert "checking the deployment status" in result

    def test_single_agent(self):
        registry = [
            {"agent_name": "finops-agent", "description": "Financial operations"}
        ]
        result = _build_agent_listing(registry)
        assert result.startswith("Available agents:")
        assert "- **finops-agent**: Financial operations" in result

    def test_multiple_agents(self):
        registry = [
            {"agent_name": "finops-agent", "description": "Financial ops"},
            {"agent_name": "governance-agent", "description": "Governance ops"},
        ]
        result = _build_agent_listing(registry)
        assert "- **finops-agent**: Financial ops" in result
        assert "- **governance-agent**: Governance ops" in result

    def test_missing_description_defaults_to_empty(self):
        registry = [{"agent_name": "test-agent"}]
        result = _build_agent_listing(registry)
        assert "- **test-agent**: " in result


class TestBuildDynamicPrompt:
    """Tests for the public build_dynamic_prompt function."""

    TEMPLATE = "You are an agent.\n\n{agent_listing}\n\nDelegation rules apply."

    @patch("agents.shared.prompt.load_agent_registry")
    def test_supervisor_uses_own_name_as_parent_filter(self, mock_load):
        mock_load.return_value = [
            {"agent_name": "finops-agent", "description": "FinOps", "enabled": True}
        ]
        build_dynamic_prompt(self.TEMPLATE, "supervisor", "my-table")
        mock_load.assert_called_once_with(
            table_name="my-table", parent_filter="supervisor"
        )

    @patch("agents.shared.prompt.load_agent_registry")
    def test_midlevel_uses_own_name_as_parent_filter(self, mock_load):
        mock_load.return_value = []
        build_dynamic_prompt(self.TEMPLATE, "finops-agent", "my-table")
        mock_load.assert_called_once_with(
            table_name="my-table", parent_filter="finops-agent"
        )

    @patch("agents.shared.prompt.load_agent_registry")
    def test_replaces_placeholder_with_listing(self, mock_load):
        mock_load.return_value = [
            {"agent_name": "sec-agent", "description": "Security", "enabled": True}
        ]
        result = build_dynamic_prompt(self.TEMPLATE, "supervisor", "t")
        assert "{agent_listing}" not in result
        assert "- **sec-agent**: Security" in result
        assert "You are an agent." in result
        assert "Delegation rules apply." in result

    @patch("agents.shared.prompt.load_agent_registry")
    def test_filters_disabled_agents(self, mock_load):
        mock_load.return_value = [
            {"agent_name": "enabled-agent", "description": "On", "enabled": True},
            {"agent_name": "disabled-agent", "description": "Off", "enabled": False},
        ]
        result = build_dynamic_prompt(self.TEMPLATE, "supervisor", "t")
        assert "enabled-agent" in result
        assert "disabled-agent" not in result

    @patch("agents.shared.prompt.load_agent_registry")
    def test_no_agents_produces_fallback_message(self, mock_load):
        mock_load.return_value = []
        result = build_dynamic_prompt(self.TEMPLATE, "supervisor", "t")
        assert "No child agents are currently deployed" in result
        assert "Delegation rules apply." in result

    @patch("agents.shared.prompt.load_agent_registry")
    def test_all_disabled_produces_fallback_message(self, mock_load):
        mock_load.return_value = [
            {"agent_name": "a", "description": "x", "enabled": False},
        ]
        result = build_dynamic_prompt(self.TEMPLATE, "supervisor", "t")
        assert "No child agents are currently deployed" in result


@pytest.fixture(scope="module")
def hierarchy() -> dict:
    """Parsed src/agents/hierarchy.json — single source of truth for agents."""
    with open(_HIERARCHY_PATH) as f:
        return json.load(f)


class TestCloudwatchAgentConfig:
    """The cloudwatch-agent worker entry matches the spec (Requirements 4.1, 4.3)."""

    def test_cloudwatch_agent_exists(self, hierarchy):
        assert "cloudwatch-agent" in hierarchy, (
            "cloudwatch-agent is missing from hierarchy.json"
        )

    def test_cloudwatch_agent_topology_fields(self, hierarchy):
        entry = hierarchy["cloudwatch-agent"]
        assert entry["type"] == "worker"
        assert entry["dir"] == "agents/worker"
        assert entry["protocol"] == "http"
        assert entry["model"] == "global.anthropic.claude-sonnet-4-6"

    def test_cloudwatch_agent_tools(self, hierarchy):
        entry = hierarchy["cloudwatch-agent"]
        assert entry["tools"] == ["cloudwatch", "tag-governance"]


class TestCloudwatchAgentPrompt:
    """The cloudwatch-agent prompt has the required structural elements
    from Requirement 4.4 / 4.5 / 7 and codifies no multi-phase workflow."""

    @pytest.fixture(scope="class")
    def prompt(self, hierarchy) -> str:
        return hierarchy["cloudwatch-agent"]["prompt"]

    def test_role_description_is_read_only(self, prompt):
        lowered = prompt.lower()
        assert "read-only" in lowered
        assert "cloudwatch alarm" in lowered

    def test_three_safety_rules_present(self, prompt):
        # Distinctive substrings for each of the three safety rules.
        assert "Never auto-apply" in prompt
        assert "Always require an SNS topic ARN" in prompt
        assert "Never scan everything" in prompt

    def test_no_proactive_queries_line(self, prompt):
        assert "Do NOT proactively run extra queries" in prompt

    def test_tool_list_present(self, prompt):
        for tool in (
            "find_resources_by_tag",
            "get_recommended_metric_alarms",
            "analyse_metric",
            "get_active_alarms",
            "get_alarm_history",
            "build_cfn_alarm",
            "assemble_cfn_template",
        ):
            assert tool in prompt, f"prompt is missing tool {tool!r}"

    def test_behavioural_hints_present(self, prompt):
        lowered = prompt.lower()
        # recommend / tune / calibrate intents.
        assert "recommend alarms" in lowered
        assert "flapping" in lowered
        assert "calibrate" in lowered

    def test_response_format_single_artifact_path(self, prompt):
        # Per Task 12 (Option C): single delivery path. The agent always
        # calls assemble_cfn_template once and places template_yaml verbatim
        # inside <cfn-artifact title="...">…</cfn-artifact>. The supervisor's
        # interceptor persists the YAML to the reports table and emits a
        # <report-pending> marker in its place.
        assert "<cfn-artifact" in prompt
        assert "assemble_cfn_template" in prompt
        # The prompt must explicitly call out "verbatim" (or equivalent
        # forbidding of re-serialization) so the model doesn't abbreviate.
        assert "VERBATIM" in prompt or "verbatim" in prompt
        # The prior nested <artifact><report-body> pattern must be GONE.
        assert "<artifact><report-body>" not in prompt
        assert "</report-body></artifact>" not in prompt
        # The 20 KB two-path wording must also be GONE.
        assert "20 KB" not in prompt, (
            "Task 12 collapsed the two-path delivery; '20 KB' wording must be removed"
        )

    def test_no_phased_workflow_keywords(self, prompt):
        for banned in ("Phase 1", "Phase 2", "Phase 3", "Workflow A", "Workflow B"):
            assert banned not in prompt, (
                f"prompt codifies a multi-phase workflow ({banned!r}); the "
                "cloudwatch-agent prompt must be intent-driven, not phased."
            )


class TestOpsExcellenceRoutesToCloudwatch:
    """ops-excellence-agent gains cloudwatch-agent as a child and routes to
    it on the cloudwatch trigger words (Requirement 4.2)."""

    def test_cloudwatch_agent_is_a_child(self, hierarchy):
        children = hierarchy["ops-excellence-agent"]["children"]
        assert "cloudwatch-agent" in children

    def test_prompt_has_cloudwatch_trigger_words(self, hierarchy):
        prompt = hierarchy["ops-excellence-agent"]["prompt"]
        for word in ("alarm", "cloudwatch", "flapping"):
            assert word in prompt, (
                f"ops-excellence-agent prompt missing trigger word {word!r}"
            )


class TestSupervisorCfnArtifactPassthrough:
    """The supervisor prompt instructs the model to pass <cfn-artifact>
    blocks from sub-agents through verbatim — the chat-mode AG-UI loop's
    interceptor handles them. (Task 12, Option C)"""

    def test_passthrough_rule_present(self, hierarchy):
        prompt = hierarchy["supervisor"]["prompt"]
        assert "<cfn-artifact" in prompt
        # Verbatim forwarding is the operative requirement.
        assert "verbatim" in prompt.lower()
