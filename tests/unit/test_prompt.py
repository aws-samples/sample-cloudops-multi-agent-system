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
        assert entry["tools"] == ["cloudwatch"]


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
        lowered = prompt.lower()
        assert "never modify aws state" in lowered
        assert "require an sns topic arn" in lowered
        assert "never invent a numeric threshold" in lowered

    def test_no_proactive_queries_line(self, prompt):
        assert "do not run extra queries" in prompt.lower()

    def test_tool_list_present(self, prompt):
        for tool in (
            "query_alarm_inventory",
            "analyze_alarm_coverage",
            "get_active_alarms",
            "get_metric_data",
            "analyze_alarm_tuning",
            "prepare_alarm_deployment",
        ):
            assert tool in prompt, f"prompt is missing tool {tool!r}"

    def test_low_level_metadata_is_not_used_for_coverage_fanout(self, prompt):
        assert "low-level metric, metadata" in prompt.lower()
        assert "never fan out recommendation calls per resource" in prompt.lower()

    def test_behavioural_hints_present(self, prompt):
        lowered = prompt.lower()
        assert "account mode" in lowered
        assert "tags mode" in lowered
        assert "threshold tuning" in lowered
        assert "calibrates selected thresholds" in lowered

    def test_response_format_uses_typed_artifact_delivery(self, prompt):
        assert "prepare_alarm_deployment once" in prompt.lower()
        assert "typed cloudformation artifact" in prompt.lower()
        assert "never emit template_yaml" in prompt.lower()
        assert "VERBATIM" not in prompt
        assert "verbatim" not in prompt.lower()
        assert "<artifact><report-body>" not in prompt
        assert "</report-body></artifact>" not in prompt
        assert "20 KB" not in prompt

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

    def test_description_advertises_cloudwatch_capability(self, hierarchy):
        description = hierarchy["ops-excellence-agent"]["description"].lower()
        assert "cloudwatch" in description
        assert "alarm inventory" in description

    def test_supervisor_routes_cloudwatch_to_ops_excellence(self, hierarchy):
        prompt = hierarchy["supervisor"]["prompt"].lower()
        assert "cloudwatch" in prompt
        assert "must be delegated to\n  ops-excellence-agent" in prompt


class TestSupervisorTypedArtifactDelivery:
    """The supervisor must leave typed artifact persistence to the platform."""

    def test_platform_owned_delivery_rule_present(self, hierarchy):
        prompt = hierarchy["supervisor"]["prompt"]
        assert "typed artifact" in prompt.lower()
        assert "never emit yaml" in prompt.lower()
        assert "<cfn-artifact" in prompt
        assert "<report-pending" in prompt
        assert "verbatim" not in prompt.lower()


class TestCloudwatchInventoryCoveragePrompt:
    @pytest.fixture(scope="class")
    def prompt(self, hierarchy) -> str:
        return hierarchy["cloudwatch-agent"]["prompt"]

    def test_uses_one_batched_inventory_handoff(self, prompt):
        assert "call each coverage operation once" in prompt.lower()
        assert "never fan out recommendation calls per resource" in prompt.lower()
        assert "never call a separate live tag-discovery tool first" in prompt.lower()

    def test_requires_zero_alarm_and_completeness_reporting(self, prompt):
        assert "snapshot age, source, and completeness" in prompt.lower()
        assert "inventory_incomplete" in prompt
        assert "both resource and alarm inventories are complete" in prompt.lower()
