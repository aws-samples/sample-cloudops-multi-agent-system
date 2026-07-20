"""Unit tests for the health-events collector.

Covers:
  * _assess_risk rules across every category/scope/status/time permutation
  * _days_until — ISO-8601 and RFC-2822 parsing
  * _enrich_with_llm — success, failure modes, disabled path, JSON-in-fence
  * _process_health_event — end-to-end dict → DDB put_item (mocked)

The collector module imports from `shared.cross_account` when run inside
the packaged zip (where shared/ sits next to handler.py). The test harness
prepends both paths to sys.path so `import handler` resolves the same
shared module that MCP Lambdas use.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# AWS_REGION is set for the whole unit package in tests/unit/conftest.py.
# These two are test-specific config the collector reads at import time.
os.environ.setdefault("HEALTH_EVENTS_TABLE_NAME", "test-table")
# Disable LLM by default so tests don't hit Bedrock. Individual tests opt in.
os.environ["ENRICHMENT_MODEL_ID"] = ""

# Load the collector handler under a namespaced module name to avoid colliding
# with the network-resilience `handler` module (both are packaged as top-level
# `handler.py` inside their respective Lambda zips). Also prepend the shared/
# path so `from shared.cross_account import get_aws_client` resolves — same
# as what happens inside the packaged zip.
sys.path.insert(0, str(_REPO_ROOT / "src" / "lambda" / "mcp"))

_COLLECTOR_PATH = _REPO_ROOT / "src" / "lambda" / "collectors" / "health-events" / "handler.py"
_spec = importlib.util.spec_from_file_location("health_events_collector", _COLLECTOR_PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


# ---------------------------------------------------------------------------
# _assess_risk — full rule coverage
# ---------------------------------------------------------------------------
class TestAssessRisk:
    def test_acct_specific_open_issue_is_critical_regardless_of_service(self):
        # ACCOUNT_SPECIFIC issues escalate even for non-core services
        assert handler._assess_risk(
            category="issue",
            status="open",
            service="ROUTE53",  # not in _CORE_SERVICES
            scope_code="ACCOUNT_SPECIFIC",
        ) == "CRITICAL"

    def test_core_service_open_issue_is_critical_even_without_acct_scope(self):
        assert handler._assess_risk(
            category="issue",
            status="open",
            service="EC2",
            scope_code="PUBLIC",
        ) == "CRITICAL"

    def test_public_open_non_core_issue_is_high(self):
        assert handler._assess_risk(
            category="issue",
            status="open",
            service="ROUTE53",
            scope_code="PUBLIC",
        ) == "HIGH"

    def test_closed_issue_is_low(self):
        assert handler._assess_risk(
            category="issue",
            status="closed",
            service="EC2",
            scope_code="ACCOUNT_SPECIFIC",
        ) == "LOW"

    def test_upcoming_issue_is_medium(self):
        assert handler._assess_risk(
            category="issue",
            status="upcoming",
            service="EC2",
            scope_code="ACCOUNT_SPECIFIC",
        ) == "MEDIUM"

    def test_open_investigation_is_high(self):
        assert handler._assess_risk(
            category="investigation", status="open", service="EC2", scope_code="PUBLIC"
        ) == "HIGH"

    def test_closed_investigation_is_medium(self):
        assert handler._assess_risk(
            category="investigation", status="closed", service="EC2", scope_code="PUBLIC"
        ) == "MEDIUM"

    def test_imminent_acct_specific_scheduled_change_is_high(self):
        # <= 3 days + ACCOUNT_SPECIFIC → HIGH
        soon = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        assert handler._assess_risk(
            category="scheduledChange",
            status="upcoming",
            service="RDS",
            scope_code="ACCOUNT_SPECIFIC",
            start_time_iso=soon,
        ) == "HIGH"

    def test_imminent_public_scheduled_change_is_medium(self):
        # <= 3 days but NOT ACCOUNT_SPECIFIC → MEDIUM
        soon = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        assert handler._assess_risk(
            category="scheduledChange",
            status="upcoming",
            service="RDS",
            scope_code="PUBLIC",
            start_time_iso=soon,
        ) == "MEDIUM"

    def test_distant_scheduled_change_is_medium(self):
        far = (datetime.now(timezone.utc) + timedelta(days=60)).isoformat()
        assert handler._assess_risk(
            category="scheduledChange",
            status="upcoming",
            service="RDS",
            scope_code="ACCOUNT_SPECIFIC",
            start_time_iso=far,
        ) == "MEDIUM"

    def test_closed_scheduled_change_is_low(self):
        assert handler._assess_risk(
            category="scheduledChange",
            status="closed",
            service="RDS",
            scope_code="ACCOUNT_SPECIFIC",
        ) == "LOW"

    def test_account_notification_is_always_low(self):
        for scope in ("ACCOUNT_SPECIFIC", "PUBLIC", "NONE"):
            for status in ("open", "upcoming", "closed"):
                assert handler._assess_risk(
                    category="accountNotification",
                    status=status,
                    service="IAM",
                    scope_code=scope,
                ) == "LOW", f"unexpected escalation: scope={scope} status={status}"

    def test_unknown_category_is_low(self):
        assert handler._assess_risk(
            category="novelCategory",
            status="open",
            service="EC2",
            scope_code="ACCOUNT_SPECIFIC",
        ) == "LOW"


# ---------------------------------------------------------------------------
# _days_until — time parsing
# ---------------------------------------------------------------------------
class TestDaysUntil:
    def test_iso_future(self):
        future = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
        assert 4.5 < handler._days_until(future) < 5.5

    def test_iso_with_z_suffix(self):
        future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat().replace("+00:00", "Z")
        assert 2.5 < handler._days_until(future) < 3.5

    def test_rfc2822(self):
        # AWS Health EventBridge events sometimes emit RFC-2822 startTime
        result = handler._days_until("Mon, 6 Apr 2099 07:00:00 GMT")
        assert result is not None
        assert result > 10000  # in the far future

    def test_empty_string_returns_none(self):
        assert handler._days_until("") is None

    def test_na_returns_none(self):
        assert handler._days_until("N/A") is None

    def test_garbage_returns_none(self):
        assert handler._days_until("not a date") is None


# ---------------------------------------------------------------------------
# _enrich_with_llm — disabled, success, failure modes
# ---------------------------------------------------------------------------
class TestEnrichmentDisabled:
    def test_empty_model_id_returns_empty_dict(self, monkeypatch):
        monkeypatch.setattr(handler, "ENRICHMENT_MODEL_ID", "")
        out = handler._enrich_with_llm(
            service="EC2", event_type_code="X", category="issue",
            scope_code="NONE", status="open",
            description="A real description", resources=["i-abc"],
        )
        assert out == {}

    def test_empty_description_skips_llm(self, monkeypatch):
        monkeypatch.setattr(handler, "ENRICHMENT_MODEL_ID", "some-model")
        handler._bedrock_client = None  # reset
        out = handler._enrich_with_llm(
            service="EC2", event_type_code="X", category="issue",
            scope_code="NONE", status="open",
            description="", resources=[],
        )
        assert out == {}

    def test_no_description_sentinel_skips_llm(self, monkeypatch):
        monkeypatch.setattr(handler, "ENRICHMENT_MODEL_ID", "some-model")
        out = handler._enrich_with_llm(
            service="EC2", event_type_code="X", category="issue",
            scope_code="NONE", status="open",
            description="No description", resources=[],
        )
        assert out == {}


def _make_bedrock_response(content_text: str, usage: dict | None = None):
    """Build a fake Bedrock invoke_model response payload."""
    payload = {
        "content": [{"type": "text", "text": content_text}],
        "usage": usage or {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 0},
    }
    body = MagicMock()
    body.read.return_value = json.dumps(payload).encode()
    return {"body": body}


class TestEnrichmentSuccess:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        monkeypatch.setattr(handler, "ENRICHMENT_MODEL_ID", "test-model")
        # Reset module-level cache
        handler._bedrock_client = None
        yield
        handler._bedrock_client = None

    def test_well_formed_response_parses_all_fields(self):
        raw = json.dumps({
            "impactSummary": "EC2 instance retirement scheduled.",
            "remediationHint": "Stop and start before the retirement date.",
            "affectedResourceTypes": ["ec2-instance"],
        })
        fake_client = MagicMock()
        fake_client.invoke_model.return_value = _make_bedrock_response(raw)
        handler._bedrock_client = fake_client

        out = handler._enrich_with_llm(
            service="EC2", event_type_code="AWS_EC2_RETIREMENT",
            category="scheduledChange", scope_code="ACCOUNT_SPECIFIC",
            status="upcoming", description="Retirement on 2099-01-01",
            resources=["i-abc123"],
        )
        assert out["impactSummary"] == "EC2 instance retirement scheduled."
        assert out["remediationHint"] == "Stop and start before the retirement date."
        assert out["affectedResourceTypes"] == ["ec2-instance"]

    def test_empty_remediation_hint_is_dropped_from_output(self):
        raw = json.dumps({
            "impactSummary": "Informational notice.",
            "remediationHint": "",
            "affectedResourceTypes": [],
        })
        fake_client = MagicMock()
        fake_client.invoke_model.return_value = _make_bedrock_response(raw)
        handler._bedrock_client = fake_client

        out = handler._enrich_with_llm(
            service="LAMBDA", event_type_code="X",
            category="accountNotification", scope_code="ACCOUNT_SPECIFIC",
            status="open", description="FYI",
            resources=[],
        )
        assert "impactSummary" in out
        # Empty values get stripped before DDB write
        assert "remediationHint" not in out
        assert "affectedResourceTypes" not in out

    def test_markdown_code_fence_is_stripped(self):
        # Some models wrap JSON in ```json ... ``` despite the prompt
        raw = "```json\n" + json.dumps({
            "impactSummary": "Wrapped in fence.",
            "remediationHint": "",
            "affectedResourceTypes": [],
        }) + "\n```"
        fake_client = MagicMock()
        fake_client.invoke_model.return_value = _make_bedrock_response(raw)
        handler._bedrock_client = fake_client

        out = handler._enrich_with_llm(
            service="S3", event_type_code="X",
            category="issue", scope_code="NONE",
            status="open", description="test",
            resources=[],
        )
        assert out["impactSummary"] == "Wrapped in fence."

    def test_length_limits_are_enforced(self):
        long_impact = "x" * 500
        long_remediation = "y" * 500
        raw = json.dumps({
            "impactSummary": long_impact,
            "remediationHint": long_remediation,
            "affectedResourceTypes": ["type-" + str(i) for i in range(20)],
        })
        fake_client = MagicMock()
        fake_client.invoke_model.return_value = _make_bedrock_response(raw)
        handler._bedrock_client = fake_client

        out = handler._enrich_with_llm(
            service="EC2", event_type_code="X",
            category="issue", scope_code="NONE",
            status="open", description="test", resources=[],
        )
        assert len(out["impactSummary"]) == 140
        assert len(out["remediationHint"]) == 200
        assert len(out["affectedResourceTypes"]) == 10  # capped at 10


class TestEnrichmentFailures:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        monkeypatch.setattr(handler, "ENRICHMENT_MODEL_ID", "test-model")
        handler._bedrock_client = None
        yield
        handler._bedrock_client = None

    def test_bedrock_exception_returns_empty_dict(self):
        fake_client = MagicMock()
        fake_client.invoke_model.side_effect = RuntimeError("bedrock down")
        handler._bedrock_client = fake_client

        out = handler._enrich_with_llm(
            service="EC2", event_type_code="X",
            category="issue", scope_code="NONE",
            status="open", description="test", resources=[],
        )
        assert out == {}

    def test_invalid_json_response_returns_empty_dict(self):
        fake_client = MagicMock()
        fake_client.invoke_model.return_value = _make_bedrock_response("not json at all")
        handler._bedrock_client = fake_client

        out = handler._enrich_with_llm(
            service="EC2", event_type_code="X",
            category="issue", scope_code="NONE",
            status="open", description="test", resources=[],
        )
        assert out == {}

    def test_non_list_affected_types_normalises_to_empty(self):
        raw = json.dumps({
            "impactSummary": "ok",
            "remediationHint": "",
            "affectedResourceTypes": "not-a-list",  # wrong type
        })
        fake_client = MagicMock()
        fake_client.invoke_model.return_value = _make_bedrock_response(raw)
        handler._bedrock_client = fake_client

        out = handler._enrich_with_llm(
            service="EC2", event_type_code="X",
            category="issue", scope_code="NONE",
            status="open", description="test", resources=[],
        )
        assert out.get("impactSummary") == "ok"
        # Non-list is sanitised to empty list, then dropped from output
        assert "affectedResourceTypes" not in out


# ---------------------------------------------------------------------------
# _process_health_event — end-to-end (mocked DDB + Bedrock + Orgs)
# ---------------------------------------------------------------------------
class TestProcessHealthEvent:
    @pytest.fixture
    def table_mock(self):
        return MagicMock()

    def test_acct_specific_issue_writes_critical_row(self, table_mock, monkeypatch):
        monkeypatch.setattr(handler, "ENRICHMENT_MODEL_ID", "")  # skip LLM
        # Avoid Organizations API call
        monkeypatch.setattr(handler, "_get_account_name", lambda aid: f"acct-{aid}")

        detail = {
            "eventArn": "arn:aws:health:us-east-1::event/EC2/TEST/TEST_001",
            "service": "EC2",
            "eventTypeCode": "AWS_EC2_OPERATIONAL_ISSUE",
            "eventTypeCategory": "issue",
            "eventScopeCode": "ACCOUNT_SPECIFIC",
            "statusCode": "open",
            "startTime": "2099-01-01T00:00:00Z",
            "lastUpdatedTime": "2099-01-01T00:00:00Z",
            "eventRegion": "us-east-1",
            "eventDescription": {"latestDescription": "test event"},
            "affectedEntities": [
                {"entityValue": "i-abc", "awsAccountId": "123456789012"}
            ],
        }
        envelope = {"account": "123456789012", "region": "us-east-1"}

        handler._process_health_event(detail, envelope, table_mock)

        # Exactly one put_item call per affected account
        assert table_mock.put_item.call_count == 1
        written = table_mock.put_item.call_args.kwargs["Item"]
        assert written["eventArn"] == detail["eventArn"]
        assert written["accountId"] == "123456789012"
        assert written["eventScopeCode"] == "ACCOUNT_SPECIFIC"
        assert written["riskLevel"] == "CRITICAL"

    def test_multi_account_event_writes_one_row_per_account(self, table_mock, monkeypatch):
        monkeypatch.setattr(handler, "ENRICHMENT_MODEL_ID", "")
        monkeypatch.setattr(handler, "_get_account_name", lambda aid: f"acct-{aid}")

        detail = {
            "eventArn": "arn:aws:health:us-east-1::event/RDS/MULTI/MULTI_001",
            "service": "RDS",
            "eventTypeCategory": "scheduledChange",
            "eventScopeCode": "ACCOUNT_SPECIFIC",
            "statusCode": "upcoming",
            "startTime": "2099-06-01T00:00:00Z",
            "lastUpdatedTime": "2099-01-01T00:00:00Z",
            "eventDescription": {"latestDescription": "multi-account scheduled event"},
            "affectedEntities": [
                {"entityValue": "db-a", "awsAccountId": "111111111111"},
                {"entityValue": "db-b", "awsAccountId": "222222222222"},
                {"entityValue": "db-c", "awsAccountId": "111111111111"},  # dup
            ],
        }
        envelope = {"account": "111111111111", "region": "us-east-1"}

        handler._process_health_event(detail, envelope, table_mock)

        # Two unique accounts, two rows
        assert table_mock.put_item.call_count == 2
        written_accts = {
            call.kwargs["Item"]["accountId"]
            for call in table_mock.put_item.call_args_list
        }
        assert written_accts == {"111111111111", "222222222222"}

    def test_missing_event_arn_is_silently_skipped(self, table_mock):
        detail = {"service": "EC2", "eventTypeCategory": "issue"}  # no eventArn
        envelope = {"account": "123456789012"}

        handler._process_health_event(detail, envelope, table_mock)

        assert table_mock.put_item.call_count == 0

    def test_no_affected_entities_falls_back_to_envelope_account(self, table_mock, monkeypatch):
        monkeypatch.setattr(handler, "ENRICHMENT_MODEL_ID", "")
        monkeypatch.setattr(handler, "_get_account_name", lambda aid: f"acct-{aid}")

        detail = {
            "eventArn": "arn:aws:health:us-east-1::event/EC2/NOENT/001",
            "service": "EC2",
            "eventTypeCategory": "accountNotification",
            "statusCode": "open",
            "eventDescription": {"latestDescription": "fyi"},
            "affectedEntities": [],  # empty
        }
        envelope = {"account": "999999999999", "region": "us-east-1"}

        handler._process_health_event(detail, envelope, table_mock)

        assert table_mock.put_item.call_count == 1
        assert table_mock.put_item.call_args.kwargs["Item"]["accountId"] == "999999999999"
