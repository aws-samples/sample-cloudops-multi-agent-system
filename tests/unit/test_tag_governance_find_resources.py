"""Unit tests for the tag-governance ``find_resources_by_tag`` tool.

This is the cloudwatch-agent's primary selector → ARN lookup. The tool
wraps ``tag:GetResources`` (boto3 ``resourcegroupstaggingapi.get_resources``)
with three behaviours that need to stay correct:

  * Empty / missing ``tag_filters`` returns the canonical
    ``selector_required`` error so the agent can prompt the user.
  * The ``tag:GetResources`` response is mapped into the
    ``{arn, tags, region, account_id}`` shape, with region+account_id
    extracted from the ARN.
  * Pagination loops over ``PaginationToken`` until empty.
  * Results are capped at 1000 — when the cap is hit, ``truncated``
    flips to True and a ``note`` field carries a hint to narrow filters.

The tests use moto's ``mock_aws`` to provide a real ``tag:GetResources``
mock (covers the happy path + selector validation), and direct stubbing
of the boto3 client for pagination + 1000-cap (moto's tag-API mock
doesn't paginate beyond a small fixture set, and seeding 1001 real
resources just to trip the cap would be slow and noisy).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The handler imports `from shared.cross_account import get_aws_client` —
# that module lives next to handler.py in the Lambda zip, but at test time
# we need to add src/lambda/mcp/ to sys.path so the import resolves.
_LAMBDA_MCP = _REPO_ROOT / "src" / "lambda" / "mcp"
if str(_LAMBDA_MCP) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_MCP))

# Default region before importing the handler (it reads AWS_REGION at import).
os.environ.setdefault("AWS_REGION", "us-east-1")

# Load the handler under a namespaced module name so it doesn't collide with
# the (already-loaded) tag_governance_handler module from test_tag_governance_tool.
_HANDLER_PATH = _LAMBDA_MCP / "tag-governance" / "handler.py"
_spec = importlib.util.spec_from_file_location(
    "tag_governance_handler_find", _HANDLER_PATH
)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_caches():
    """Reset module-level caches between tests so each starts clean."""
    handler._client.cache_clear()
    handler._default_region.cache_clear()
    yield
    handler._client.cache_clear()
    handler._default_region.cache_clear()


@pytest.fixture(autouse=True)
def _reset_cross_account_cache():
    """Reset the cross-account session lru_cache between tests."""
    from shared.cross_account import _reset_caches_for_testing

    _reset_caches_for_testing()
    yield
    _reset_caches_for_testing()


@pytest.fixture
def mock_aws_creds(monkeypatch):
    """Set fake AWS creds for moto / boto3 in tests that talk to the SDK."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    # No cross-account role configured → fall through to execution role,
    # which under moto means the mocked client.
    monkeypatch.delenv("CROSS_ACCOUNT_ROLE_ARN_TAG_GOVERNANCE", raising=False)


def _make_context(tool_name: str = "find_resources_by_tag") -> SimpleNamespace:
    """AgentCore Gateway context shape the dispatcher reads."""
    return SimpleNamespace(
        client_context=SimpleNamespace(
            custom={"bedrockAgentCoreToolName": f"tag-governance___{tool_name}"}
        )
    )


# ---------------------------------------------------------------------------
# Selector validation
# ---------------------------------------------------------------------------


class TestSelectorValidation:
    """Empty / missing tag_filters must return the canonical structured error.

    The cloudwatch-agent's prompt promises to prompt the user when no
    selector is provided — this is the contract that lets it happen.
    """

    def test_missing_tag_filters_returns_selector_required(self):
        result = handler.handle_find_resources_by_tag({})
        assert result == {
            "error": "selector_required",
            "message": "Provide at least one tag_filters entry.",
        }

    def test_empty_dict_tag_filters_returns_selector_required(self):
        result = handler.handle_find_resources_by_tag({"tag_filters": {}})
        assert result == {
            "error": "selector_required",
            "message": "Provide at least one tag_filters entry.",
        }

    def test_non_dict_tag_filters_returns_selector_required(self):
        # A list/string/None must not silently pass — the tool only accepts
        # the {key: value} dict shape.
        for bad in [["App", "test"], "App=test", None, 42]:
            result = handler.handle_find_resources_by_tag({"tag_filters": bad})
            assert result["error"] == "selector_required", (
                f"input {bad!r} should fail validation"
            )

    def test_only_empty_keys_returns_selector_required(self):
        # Non-empty dict but every key is falsy → no usable filter survives
        # _build_tag_filters' falsy-key skip, so we must still surface the
        # canonical selector_required error.
        result = handler.handle_find_resources_by_tag(
            {"tag_filters": {"": "test"}}
        )
        assert result["error"] == "selector_required"


# ---------------------------------------------------------------------------
# tag_filters → boto3 TagFilters conversion
# ---------------------------------------------------------------------------


class TestBuildTagFilters:
    """The {key: value} dict must be reshaped into boto3's [{Key, Values}] form."""

    def test_single_value_becomes_one_element_list(self):
        out = handler._build_tag_filters({"App": "test"})
        assert out == [{"Key": "App", "Values": ["test"]}]

    def test_list_value_passes_through(self):
        out = handler._build_tag_filters({"App": ["test", "prod"]})
        assert out == [{"Key": "App", "Values": ["test", "prod"]}]

    def test_skips_empty_keys(self):
        out = handler._build_tag_filters({"": "skip", "App": "test"})
        assert out == [{"Key": "App", "Values": ["test"]}]

    def test_coerces_non_string_values_to_string(self):
        out = handler._build_tag_filters({"Owner": 42})
        assert out == [{"Key": "Owner", "Values": ["42"]}]


# ---------------------------------------------------------------------------
# ARN-to-resource mapping
# ---------------------------------------------------------------------------


class TestResourceFromArn:
    """ARN parsing must extract region + account_id correctly."""

    def test_regional_resource(self):
        raw = {
            "ResourceARN": "arn:aws:ec2:us-east-1:111111111111:instance/i-abc",
            "Tags": [{"Key": "App", "Value": "test"}],
        }
        out = handler._resource_from_arn(raw, fallback_region="eu-west-1")
        assert out == {
            "arn": "arn:aws:ec2:us-east-1:111111111111:instance/i-abc",
            "tags": {"App": "test"},
            "region": "us-east-1",
            "account_id": "111111111111",
        }

    def test_global_resource_uses_fallback_region(self):
        # Global services (S3, IAM, CloudFront) leave the region segment
        # empty in their ARNs — fall back to the query region.
        raw = {
            "ResourceARN": "arn:aws:s3:::my-bucket",
            "Tags": [],
        }
        out = handler._resource_from_arn(raw, fallback_region="us-east-1")
        assert out["region"] == "us-east-1"
        assert out["account_id"] == ""  # no account in S3 ARNs

    def test_empty_tag_keys_are_dropped(self):
        raw = {
            "ResourceARN": "arn:aws:lambda:us-west-2:222:function:foo",
            "Tags": [
                {"Key": "App", "Value": "test"},
                {"Key": "", "Value": "ignored"},
            ],
        }
        out = handler._resource_from_arn(raw, fallback_region="us-west-2")
        assert out["tags"] == {"App": "test"}


# ---------------------------------------------------------------------------
# Happy path via moto
# ---------------------------------------------------------------------------


class TestFindResourcesViaMoto:
    """End-to-end happy path with moto mocking tag:GetResources.

    Seeds two tagged S3 buckets and verifies the tool returns them in the
    expected {arn, tags, region, account_id} shape.
    """

    def test_returns_resources_with_expected_shape(self, mock_aws_creds):
        with mock_aws():
            # Seed two tagged S3 buckets — moto's tagging API pulls these
            # in via the underlying service mock.
            s3 = boto3.client("s3", region_name="us-east-1")
            s3.create_bucket(Bucket="bucket-app-test-1")
            s3.put_bucket_tagging(
                Bucket="bucket-app-test-1",
                Tagging={"TagSet": [{"Key": "App", "Value": "test"}]},
            )
            s3.create_bucket(Bucket="bucket-app-test-2")
            s3.put_bucket_tagging(
                Bucket="bucket-app-test-2",
                Tagging={
                    "TagSet": [
                        {"Key": "App", "Value": "test"},
                        {"Key": "Owner", "Value": "alice"},
                    ]
                },
            )
            # And one resource with a different tag — must be excluded.
            s3.create_bucket(Bucket="bucket-app-prod")
            s3.put_bucket_tagging(
                Bucket="bucket-app-prod",
                Tagging={"TagSet": [{"Key": "App", "Value": "prod"}]},
            )

            result = handler.handle_find_resources_by_tag(
                {"tag_filters": {"App": "test"}}
            )

        assert "error" not in result
        assert result["truncated"] is False
        assert result["note"] is None
        arns = sorted(r["arn"] for r in result["resources"])
        assert arns == [
            "arn:aws:s3:::bucket-app-test-1",
            "arn:aws:s3:::bucket-app-test-2",
        ]
        # Each entry has the four-key shape — no extras, no missing.
        for r in result["resources"]:
            assert set(r.keys()) == {"arn", "tags", "region", "account_id"}
            assert r["region"] == "us-east-1"  # falls back to query region
        # Tags are mapped key-by-key into a dict.
        bucket_2 = next(
            r for r in result["resources"] if r["arn"].endswith("test-2")
        )
        assert bucket_2["tags"] == {"App": "test", "Owner": "alice"}

    def test_resource_types_filter_passed_through(self, mock_aws_creds):
        """resource_types should reach boto3 as ResourceTypeFilters."""
        with mock_aws():
            s3 = boto3.client("s3", region_name="us-east-1")
            s3.create_bucket(Bucket="bucket-with-tag")
            s3.put_bucket_tagging(
                Bucket="bucket-with-tag",
                Tagging={"TagSet": [{"Key": "App", "Value": "test"}]},
            )
            # Filter to s3:bucket — we should still see the bucket.
            result = handler.handle_find_resources_by_tag({
                "tag_filters": {"App": "test"},
                "resource_types": ["s3"],
            })
        assert "error" not in result
        assert len(result["resources"]) == 1


# ---------------------------------------------------------------------------
# Pagination + 1000-cap
# ---------------------------------------------------------------------------


def _stub_get_resources(pages):
    """Build a mock client whose get_resources returns the given pages.

    Each entry in `pages` is a (resource_count, next_token) tuple; the mock
    returns that many ResourceTagMappingList entries plus the token.
    """
    counter = {"i": 0}

    def fake_get_resources(**_kwargs):
        idx = counter["i"]
        counter["i"] += 1
        count, token = pages[idx]
        return {
            "ResourceTagMappingList": [
                {
                    "ResourceARN": (
                        f"arn:aws:ec2:us-east-1:111111111111:instance/"
                        f"i-page{idx}-{n}"
                    ),
                    "Tags": [{"Key": "App", "Value": "test"}],
                }
                for n in range(count)
            ],
            "PaginationToken": token,
        }

    client = MagicMock()
    client.get_resources.side_effect = fake_get_resources
    return client


class TestPagination:
    def test_paginates_until_empty_token(self, monkeypatch):
        """Two pages with PaginationToken = 'next' then '' must combine."""
        client = _stub_get_resources([(2, "next"), (3, "")])
        monkeypatch.setattr(
            handler, "get_aws_client", lambda **_kwargs: client
        )

        result = handler.handle_find_resources_by_tag(
            {"tag_filters": {"App": "test"}}
        )
        assert result["truncated"] is False
        assert result["note"] is None
        assert len(result["resources"]) == 5

        # Second call carries the PaginationToken.
        second_call_kwargs = client.get_resources.call_args_list[1].kwargs
        assert second_call_kwargs.get("PaginationToken") == "next"

    def test_first_call_does_not_carry_pagination_token(self, monkeypatch):
        client = _stub_get_resources([(1, "")])
        monkeypatch.setattr(
            handler, "get_aws_client", lambda **_kwargs: client
        )

        handler.handle_find_resources_by_tag({"tag_filters": {"App": "test"}})
        first_call_kwargs = client.get_resources.call_args_list[0].kwargs
        assert "PaginationToken" not in first_call_kwargs

    def test_resource_types_threaded_through_to_boto3(self, monkeypatch):
        client = _stub_get_resources([(0, "")])
        monkeypatch.setattr(
            handler, "get_aws_client", lambda **_kwargs: client
        )

        handler.handle_find_resources_by_tag({
            "tag_filters": {"App": "test"},
            "resource_types": ["ec2:instance", "lambda:function"],
        })
        kwargs = client.get_resources.call_args_list[0].kwargs
        assert kwargs["ResourceTypeFilters"] == [
            "ec2:instance",
            "lambda:function",
        ]
        assert kwargs["TagFilters"] == [{"Key": "App", "Values": ["test"]}]


class TestThousandCap:
    """Once 1000 resources have accumulated, stop paginating + flag truncation."""

    def test_caps_at_1000_and_flags_truncated(self, monkeypatch):
        # Three 600-record pages — would total 1800 if not capped.
        client = _stub_get_resources([(600, "p1"), (600, "p2"), (600, "")])
        monkeypatch.setattr(
            handler, "get_aws_client", lambda **_kwargs: client
        )

        result = handler.handle_find_resources_by_tag(
            {"tag_filters": {"App": "test"}}
        )
        assert len(result["resources"]) == 1000
        assert result["truncated"] is True
        assert result["note"] is not None
        assert "1000" in result["note"]
        # Should have stopped paginating before the third page (we hit
        # the cap mid-page-2, so page 3 must not be requested).
        assert client.get_resources.call_count == 2

    def test_exactly_1000_does_not_set_truncated(self, monkeypatch):
        # Exactly 1000 resources, no more pages → not truncated.
        client = _stub_get_resources([(1000, "")])
        monkeypatch.setattr(
            handler, "get_aws_client", lambda **_kwargs: client
        )

        result = handler.handle_find_resources_by_tag(
            {"tag_filters": {"App": "test"}}
        )
        assert len(result["resources"]) == 1000
        assert result["truncated"] is False
        assert result["note"] is None


# ---------------------------------------------------------------------------
# Cross-account routing
# ---------------------------------------------------------------------------


class TestCrossAccountRouting:
    """The tool must call get_aws_client with role_alias='TAG_GOVERNANCE'.

    This is the spec contract — the new tool reuses the existing
    cross-account env var rather than introducing a new one.
    """

    def test_get_aws_client_called_with_correct_kwargs(self, monkeypatch):
        captured: dict = {}

        def fake_get_aws_client(**kwargs):
            captured.update(kwargs)
            client = MagicMock()
            client.get_resources.return_value = {
                "ResourceTagMappingList": [],
                "PaginationToken": "",
            }
            return client

        monkeypatch.setattr(handler, "get_aws_client", fake_get_aws_client)

        handler.handle_find_resources_by_tag({
            "tag_filters": {"App": "test"},
            "region": "eu-west-1",
        })

        assert captured["service_name"] == "resourcegroupstaggingapi"
        assert captured["role_alias"] == "TAG_GOVERNANCE"
        assert captured["region_name"] == "eu-west-1"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestClientError:
    def test_client_error_returns_error_payload(self, monkeypatch):
        client = MagicMock()
        client.get_resources.side_effect = ClientError(
            error_response={
                "Error": {"Code": "AccessDenied", "Message": "denied"}
            },
            operation_name="GetResources",
        )
        monkeypatch.setattr(
            handler, "get_aws_client", lambda **_kwargs: client
        )

        result = handler.handle_find_resources_by_tag(
            {"tag_filters": {"App": "test"}}
        )
        assert "error" in result
        assert "AccessDenied" in result["error"]


# ---------------------------------------------------------------------------
# Dispatcher routing
# ---------------------------------------------------------------------------


class TestDispatcherRouting:
    """The dispatcher must route 'find_resources_by_tag' to the handler.

    Without this, the tool is invisible to the gateway.
    """

    def test_registered_in_tool_handlers(self):
        assert "find_resources_by_tag" in handler._TOOL_HANDLERS
        assert (
            handler._TOOL_HANDLERS["find_resources_by_tag"]
            is handler.handle_find_resources_by_tag
        )

    def test_dispatcher_routes_to_find_resources_by_tag(self, monkeypatch):
        client = MagicMock()
        client.get_resources.return_value = {
            "ResourceTagMappingList": [],
            "PaginationToken": "",
        }
        monkeypatch.setattr(
            handler, "get_aws_client", lambda **_kwargs: client
        )

        ctx = _make_context("find_resources_by_tag")
        result = handler.handler({"tag_filters": {"App": "test"}}, ctx)
        assert result == {
            "resources": [],
            "truncated": False,
            "note": None,
        }
