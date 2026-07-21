"""Unit tests for the tag-governance MCP tool.

Covers the 7 functional tools + dispatcher:
  * Dispatcher — unknown tool + exception handling
  * get_required_tags — caller-supplied, org-policy, no-policy
  * list_tag_keys_in_use — pagination + client error
  * check_tag_compliance (in-Python) — classifier, no-policy, RE-not-indexed
  * check_tag_compliance (AWS-evaluated) — no-tag-policy-attached
  * get_org_tag_compliance_summary — pagination + tag-policies-disabled
  * find_untagged_resources — success, RE-not-indexed graceful degrade
  * list_cost_allocation_tag_status — pagination + payer-only error
  * get_remediation_guidance — bucket → link generation

The handler imports boto3/botocore at module level. Tests mock
_client() directly (it's @lru_cache'd) rather than swapping boto3
so each test starts from a clean client slate.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

_REPO_ROOT = Path(__file__).resolve().parents[2]

# AWS_REGION is set for the whole unit package in tests/unit/conftest.py
# (imported before this module), so the handler sees it at import time.

# Load the handler under a namespaced module name so it doesn't collide with
# other `handler.py` modules (network-resilience, collectors, etc.).
_HANDLER_PATH = _REPO_ROOT / "src" / "lambda" / "mcp" / "tag-governance" / "handler.py"
_spec = importlib.util.spec_from_file_location("tag_governance_handler", _HANDLER_PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_client_cache():
    """Reset @lru_cache between tests so each test gets a fresh client mock."""
    handler._client.cache_clear()
    handler._default_region.cache_clear()
    handler._re_aggregator_region.cache_clear() if hasattr(
        handler._re_aggregator_region, "cache_clear"
    ) else None
    yield
    handler._client.cache_clear()
    handler._default_region.cache_clear()


def _make_context(tool_name: str) -> SimpleNamespace:
    """Build the AgentCore Gateway context shape the dispatcher reads."""
    return SimpleNamespace(
        client_context=SimpleNamespace(
            custom={"bedrockAgentCoreToolName": f"tag-governance___{tool_name}"}
        )
    )


def _client_error(code: str, msg: str = "") -> ClientError:
    return ClientError(
        error_response={"Error": {"Code": code, "Message": msg or code}},
        operation_name="Test",
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

class TestDispatcher:
    def test_unknown_tool_returns_error_with_tool_list(self):
        ctx = _make_context("nonexistent_tool")
        result = handler.handler({}, ctx)
        assert result["error"].startswith("Unknown tool: nonexistent_tool")
        assert "get_required_tags" in result["available_tools"]
        assert len(result["available_tools"]) == 7

    def test_exception_in_handler_surfaces_as_error(self, monkeypatch):
        def boom(_event):
            raise RuntimeError("kaboom")

        monkeypatch.setitem(handler._TOOL_HANDLERS, "get_required_tags", boom)
        ctx = _make_context("get_required_tags")
        result = handler.handler({}, ctx)
        assert "get_required_tags failed" in result["error"]
        assert "kaboom" in result["error"]

    def test_routing_strips_target_prefix(self, monkeypatch):
        """bedrockAgentCoreToolName is `target___tool_name` — dispatcher splits."""
        called = {}

        def fake(event):
            called["yes"] = True
            return {"ok": True}

        monkeypatch.setitem(handler._TOOL_HANDLERS, "get_required_tags", fake)
        ctx = _make_context("get_required_tags")
        result = handler.handler({"required_tags": ["X"]}, ctx)
        assert called.get("yes") is True
        assert result == {"ok": True}


# ---------------------------------------------------------------------------
# get_required_tags
# ---------------------------------------------------------------------------

class TestGetRequiredTags:
    def test_caller_supplied_flat_list(self):
        result = handler.handle_get_required_tags(
            {"required_tags": ["Environment", "Owner"]}
        )
        assert result["source"] == "caller"
        assert result["required_tag_keys"] == ["Environment", "Owner"]
        assert result["allowed_values"] == {}

    def test_caller_supplied_object_form_with_allowed_values(self):
        result = handler.handle_get_required_tags({
            "required_tags": [
                {"key": "Environment", "allowed_values": ["prod", "dev"]},
                "Owner",
            ]
        })
        assert result["source"] == "caller"
        assert set(result["required_tag_keys"]) == {"Environment", "Owner"}
        assert result["allowed_values"]["Environment"] == ["prod", "dev"]

    def test_no_caller_input_and_no_org_policy_returns_error_with_hint(
        self, monkeypatch
    ):
        # No required_tags in event + org policy fetch returns nothing.
        monkeypatch.setattr(
            handler, "_fetch_effective_tags_block", lambda: None
        )
        result = handler.handle_get_required_tags({})
        assert "No required-tag policy found" in result["error"]
        assert "Environment" in result["hint"]  # starter-tag hint

    def test_org_policy_parsed_when_no_caller_input(self, monkeypatch):
        monkeypatch.setattr(
            handler,
            "_fetch_effective_tags_block",
            lambda: {
                "Environment": {
                    "tag_value": {"@@assign": ["prod", "dev"]},
                    "case_sensitive": {"@@assign": True},
                },
                "Owner": {},
            },
        )
        result = handler.handle_get_required_tags({})
        assert result["source"] == "aws_organizations"
        assert set(result["required_tag_keys"]) == {"Environment", "Owner"}
        assert result["allowed_values"] == {"Environment": ["prod", "dev"]}
        assert result["case_sensitive"] == {"Environment": True}


# ---------------------------------------------------------------------------
# list_tag_keys_in_use
# ---------------------------------------------------------------------------

class TestListTagKeysInUse:
    def test_paginates_and_dedupes(self, monkeypatch):
        paginator = MagicMock()
        paginator.paginate.return_value = iter([
            {"TagKeys": ["Environment", "Owner"]},
            {"TagKeys": ["Owner", "Project"]},  # duplicate
        ])
        client = MagicMock()
        client.get_paginator.return_value = paginator
        monkeypatch.setattr(handler, "_client", lambda *a, **kw: client)

        result = handler.handle_list_tag_keys_in_use({"region": "us-east-1"})
        assert result["count"] == 3
        assert result["tag_keys"] == ["Environment", "Owner", "Project"]
        assert result["region"] == "us-east-1"

    def test_client_error_returns_error_payload(self, monkeypatch):
        paginator = MagicMock()
        paginator.paginate.side_effect = _client_error("AccessDenied")
        client = MagicMock()
        client.get_paginator.return_value = paginator
        monkeypatch.setattr(handler, "_client", lambda *a, **kw: client)

        result = handler.handle_list_tag_keys_in_use({})
        assert "AccessDenied" in result["error"]


# ---------------------------------------------------------------------------
# check_tag_compliance — in-Python mode
# ---------------------------------------------------------------------------

class TestCheckTagCompliancePython:
    def test_no_policy_returns_no_policy_error(self, monkeypatch):
        monkeypatch.setattr(handler, "_fetch_effective_tags_block", lambda: None)
        result = handler.handle_check_tag_compliance({})
        assert "No required-tag policy found" in result["error"]

    def test_resource_explorer_not_indexed_returns_status(self, monkeypatch):
        monkeypatch.setattr(handler, "_re_aggregator_region", lambda: None)
        result = handler.handle_check_tag_compliance(
            {"required_tags": ["Environment"]}
        )
        assert result["status"] == "resource_explorer_not_indexed"

    def test_classifier_marks_missing_and_invalid(self):
        policy = {
            "required_tag_keys": ["Environment", "Owner"],
            "allowed_values": {"Environment": ["prod", "dev"]},
            "case_sensitive": {},
        }
        # Missing Environment, invalid Owner (no allowed_values so any value OK).
        r = {
            "arn": "arn:aws:ec2:us-east-1:111:instance/i-1",
            "account_id": "111",
            "region": "us-east-1",
            "resource_type": "ec2:instance",
            "tags": {"Owner": "alice"},
        }
        classification = handler._classify(r, policy)
        assert classification["compliance_status"] == "non_compliant"
        violation_keys = {v["tag_key"] for v in classification["violations"]}
        assert violation_keys == {"Environment"}

    def test_classifier_invalid_value_when_allowed_values_set(self):
        policy = {
            "required_tag_keys": ["Environment"],
            "allowed_values": {"Environment": ["prod", "dev"]},
            "case_sensitive": {},
        }
        r = {
            "arn": "arn:aws:ec2:us-east-1:111:instance/i-1",
            "account_id": "111",
            "region": "us-east-1",
            "resource_type": "ec2:instance",
            "tags": {"Environment": "staging"},
        }
        classification = handler._classify(r, policy)
        v = classification["violations"][0]
        assert v["violation_type"] == "invalid_value"
        assert v["actual_value"] == "staging"

    def test_classifier_case_insensitive_by_default(self):
        policy = {
            "required_tag_keys": ["Environment"],
            "allowed_values": {"Environment": ["Prod"]},
            "case_sensitive": {},
        }
        r = {
            "arn": "arn",
            "account_id": "1",
            "region": "r",
            "resource_type": "t",
            "tags": {"Environment": "prod"},
        }
        classification = handler._classify(r, policy)
        assert classification["compliance_status"] == "compliant"

    def test_classifier_case_sensitive_when_flagged(self):
        policy = {
            "required_tag_keys": ["Environment"],
            "allowed_values": {"Environment": ["Prod"]},
            "case_sensitive": {"Environment": True},
        }
        r = {
            "arn": "arn",
            "account_id": "1",
            "region": "r",
            "resource_type": "t",
            "tags": {"Environment": "prod"},
        }
        classification = handler._classify(r, policy)
        assert classification["compliance_status"] == "non_compliant"


# ---------------------------------------------------------------------------
# check_tag_compliance — AWS-evaluated mode
# ---------------------------------------------------------------------------

class TestCheckTagComplianceAwsEvaluated:
    def test_no_tag_policy_attached_returns_canonical_error(self, monkeypatch):
        # _has_effective_tag_policy returns False → canonical error payload.
        monkeypatch.setattr(handler, "_has_effective_tag_policy", lambda: False)
        result = handler.handle_check_tag_compliance({"use_aws_evaluation": True})
        assert result["code"] == "NoTagPoliciesAttached"


# ---------------------------------------------------------------------------
# get_org_tag_compliance_summary
# ---------------------------------------------------------------------------

class TestGetOrgTagComplianceSummary:
    def test_paginates_and_aggregates(self, monkeypatch):
        client = MagicMock()
        client.get_compliance_summary.side_effect = [
            {
                "SummaryList": [
                    {
                        "TargetId": "111",
                        "NonCompliantResources": 5,
                        "LastUpdated": "2026-05-01",
                    }
                ],
                "PaginationToken": "next",
            },
            {
                "SummaryList": [
                    {
                        "TargetId": "222",
                        "NonCompliantResources": 3,
                        "LastUpdated": "2026-05-01",
                    }
                ],
                "PaginationToken": "",
            },
        ]
        monkeypatch.setattr(handler, "_client", lambda *a, **kw: client)

        result = handler.handle_get_org_tag_compliance_summary({})
        assert result["total_noncompliant_resources"] == 8
        assert len(result["summary"]) == 2
        # Second call should have carried the PaginationToken.
        second_kwargs = client.get_compliance_summary.call_args_list[1].kwargs
        assert second_kwargs.get("PaginationToken") == "next"

    def test_tag_policies_service_access_disabled(self, monkeypatch):
        client = MagicMock()
        client.get_compliance_summary.side_effect = _client_error(
            "ConstraintViolationException",
            "Tag policies may not be enabled. EnableAWSServiceAccess is required.",
        )
        monkeypatch.setattr(handler, "_client", lambda *a, **kw: client)

        result = handler.handle_get_org_tag_compliance_summary({})
        assert result["code"] == "TagPoliciesServiceAccessDisabled"


# ---------------------------------------------------------------------------
# find_untagged_resources
# ---------------------------------------------------------------------------

class TestFindUntaggedResources:
    def test_resource_explorer_not_indexed_graceful_degrade(self, monkeypatch):
        monkeypatch.setattr(handler, "_re_aggregator_region", lambda: None)
        result = handler.handle_find_untagged_resources({})
        assert result["status"] == "resource_explorer_not_indexed"

    def test_success_returns_resources(self, monkeypatch):
        monkeypatch.setattr(handler, "_re_aggregator_region", lambda: "us-east-1")
        monkeypatch.setattr(
            handler,
            "_re_search",
            lambda *a, **kw: (
                [
                    {
                        "Arn": "arn:aws:s3:::bucket-1",
                        "OwningAccountId": "111",
                        "Region": "us-east-1",
                        "ResourceType": "s3:bucket",
                        "Service": "s3",
                    }
                ],
                False,
            ),
        )
        # _client lookup happens inside the handler — give it a stub.
        monkeypatch.setattr(handler, "_client", lambda *a, **kw: MagicMock())

        result = handler.handle_find_untagged_resources({})
        assert result["status"] == "ok"
        assert result["count"] == 1
        assert result["resources"][0]["resource_arn"] == "arn:aws:s3:::bucket-1"

    def test_api_limit_reached_surfaces_note(self, monkeypatch):
        monkeypatch.setattr(handler, "_re_aggregator_region", lambda: "us-east-1")
        monkeypatch.setattr(
            handler, "_re_search", lambda *a, **kw: ([], True)
        )
        monkeypatch.setattr(handler, "_client", lambda *a, **kw: MagicMock())

        result = handler.handle_find_untagged_resources({})
        assert result.get("api_limit_reached") is True
        assert "1000" in result["api_limit_note"]


# ---------------------------------------------------------------------------
# list_cost_allocation_tag_status
# ---------------------------------------------------------------------------

class TestListCostAllocationTagStatus:
    def test_paginates_and_counts_active(self, monkeypatch):
        client = MagicMock()
        client.list_cost_allocation_tags.side_effect = [
            {
                "CostAllocationTags": [
                    {"TagKey": "Environment", "Status": "Active", "Type": "UserDefined"},
                    {"TagKey": "Owner", "Status": "Inactive", "Type": "UserDefined"},
                ],
                "NextToken": "next",
            },
            {
                "CostAllocationTags": [
                    {"TagKey": "CostCentre", "Status": "Active", "Type": "UserDefined"},
                ],
                "NextToken": "",
            },
        ]
        monkeypatch.setattr(handler, "_client", lambda *a, **kw: client)

        result = handler.handle_list_cost_allocation_tag_status({})
        assert result["count"] == 3
        assert result["active_count"] == 2
        assert result["inactive_count"] == 1

    def test_access_denied_maps_to_payer_only_hint(self, monkeypatch):
        client = MagicMock()
        client.list_cost_allocation_tags.side_effect = _client_error("AccessDenied")
        monkeypatch.setattr(handler, "_client", lambda *a, **kw: client)

        result = handler.handle_list_cost_allocation_tag_status({})
        assert "payer-only" in result["error"]


# ---------------------------------------------------------------------------
# get_remediation_guidance
# ---------------------------------------------------------------------------

class TestGetRemediationGuidance:
    def test_buckets_produce_ordered_links(self):
        result = handler.handle_get_remediation_guidance({
            "remediation_buckets": [
                {
                    "tag_key": "Environment",
                    "violation_type": "missing_tag",
                    "account_id": "111",
                    "region": "us-east-1",
                    "resource_type": "ec2:instance",
                    "count": 10,
                },
                {
                    "tag_key": "Owner",
                    "violation_type": "missing_tag",
                    "account_id": "111",
                    "region": "us-east-1",
                    "resource_type": "s3:bucket",
                    "count": 2,
                },
            ],
            "scan_method": "resource_explorer",
        })
        # Higher-count bucket appears first (ordered by bucket size desc).
        assert result["links"][0]["resource_count"] == 10
        assert result["links"][1]["resource_count"] == 2
        # Every bucket emits one link.
        assert len(result["links"]) == 2

    def test_max_links_truncation_flag(self):
        buckets = [
            {
                "tag_key": f"K{i}",
                "violation_type": "missing_tag",
                "account_id": "111",
                "region": "us-east-1",
                "resource_type": "ec2:instance",
                "count": i + 1,
            }
            for i in range(5)
        ]
        result = handler.handle_get_remediation_guidance({
            "remediation_buckets": buckets,
            "scan_method": "resource_explorer",
            "max_links": 2,
        })
        assert len(result["links"]) == 2
        assert result["links_truncated"] is True
        assert result["links_dropped"] == 3

    def test_self_fetches_compliance_when_no_input(self, monkeypatch):
        """No buckets and no resources → runs check_tag_compliance internally."""
        called = {}

        def fake_compliance(event):
            called["event"] = event
            return {
                "scan_method": "resource_explorer",
                "remediation_buckets": [
                    {
                        "tag_key": "Environment",
                        "violation_type": "missing_tag",
                        "account_id": "111",
                        "region": "us-east-1",
                        "resource_type": "ec2:instance",
                        "count": 7,
                    },
                ],
            }

        monkeypatch.setattr(handler, "handle_check_tag_compliance", fake_compliance)

        result = handler.handle_get_remediation_guidance(
            {"required_tags": ["Environment"], "resource_types": ["ec2:instance"]}
        )
        # The compliance scan received the same event (params threaded through).
        assert called["event"]["required_tags"] == ["Environment"]
        assert called["event"]["resource_types"] == ["ec2:instance"]
        # Regenerated bucket produced a link.
        assert len(result["links"]) == 1
        assert result["links"][0]["resource_count"] == 7

    def test_self_fetch_propagates_no_policy_error(self, monkeypatch):
        """A non-success compliance response is surfaced verbatim, not swallowed."""
        monkeypatch.setattr(
            handler,
            "handle_check_tag_compliance",
            lambda event: handler._NO_POLICY_FOUND_ERROR,
        )
        result = handler.handle_get_remediation_guidance({})
        assert result == handler._NO_POLICY_FOUND_ERROR
        assert "links" not in result

    def test_self_fetch_all_compliant_returns_no_links(self, monkeypatch):
        """Self-fetch yielding zero buckets falls through to the clean summary."""
        monkeypatch.setattr(
            handler,
            "handle_check_tag_compliance",
            lambda event: {"scan_method": "resource_explorer", "remediation_buckets": []},
        )
        result = handler.handle_get_remediation_guidance({})
        assert result["links"] == []
        assert "compliant" in result["summary"].lower()
