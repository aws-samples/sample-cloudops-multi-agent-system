"""Unit tests for the tag-governance collector Lambda.

The collector orchestrates: invoke the tag-governance TOOL Lambda for each
canonical operation, shrink oversized responses, persist to DynamoDB. All
AWS calls are mocked — these tests pin the orchestration contract.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

_HANDLER_PATH = (
    _REPO_ROOT / "src" / "lambda" / "collectors" / "tag-governance" / "handler.py"
)
_spec = importlib.util.spec_from_file_location(
    "tag_governance_collector_handler", _HANDLER_PATH
)
collector = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(collector)


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setattr(collector, "TABLE_NAME", "snap-table")
    monkeypatch.setattr(collector, "TOOL_FUNCTION_NAME", "cloudops-tag-governance-tool")


def _lambda_response(payload: dict, function_error: str | None = None):
    body = MagicMock()
    body.read.return_value = json.dumps(payload).encode()
    resp = {"Payload": body}
    if function_error:
        resp["FunctionError"] = function_error
    return resp


class TestInvokeTool:
    def test_sends_gateway_style_client_context(self, env, monkeypatch):
        """The tool routes on bedrockAgentCoreToolName from ClientContext —
        the collector must impersonate the gateway's naming exactly."""
        import base64

        lam = MagicMock()
        lam.invoke.return_value = _lambda_response({"ok": True})
        monkeypatch.setattr(collector, "_lambda_client", lambda: lam)

        out = collector._invoke_tool("check_tag_compliance", {"max_resources": 1000})
        assert out == {"ok": True}
        ctx = json.loads(base64.b64decode(lam.invoke.call_args.kwargs["ClientContext"]))
        assert (
            ctx["custom"]["bedrockAgentCoreToolName"]
            == "tag-governance___check_tag_compliance"
        )

    def test_function_error_raises(self, env, monkeypatch):
        lam = MagicMock()
        lam.invoke.return_value = _lambda_response({"errorMessage": "boom"}, "Unhandled")
        monkeypatch.setattr(collector, "_lambda_client", lambda: lam)
        with pytest.raises(RuntimeError, match="invoke failed"):
            collector._invoke_tool("get_required_tags", {})


class TestShrinkToFit:
    def test_small_response_untouched(self):
        resp, trimmed = collector._shrink_to_fit({"a": 1})
        assert resp == {"a": 1} and trimmed is False

    def test_oversized_detail_list_trimmed_counts_kept(self):
        big = {
            "non_compliant_count": 5000,
            "non_compliant_resources": [
                {"arn": "x" * 200, "violations": ["v"]} for _ in range(3000)
            ],
        }
        resp, trimmed = collector._shrink_to_fit(big)
        assert trimmed is True
        assert resp["non_compliant_count"] == 5000  # counts never trimmed
        assert len(resp["non_compliant_resources"]) < 3000
        assert "snapshot_note" in resp
        assert len(json.dumps(resp).encode()) <= collector._MAX_ITEM_BYTES


class TestHandler:
    def test_sweeps_all_canonical_ops_and_writes_meta(self, env, monkeypatch):
        lam = MagicMock()
        lam.invoke.return_value = _lambda_response({"ok": True})
        ddb = MagicMock()
        monkeypatch.setattr(collector, "_lambda_client", lambda: lam)
        monkeypatch.setattr(collector, "_ddb", lambda: ddb)

        result = collector.handler({}, None)

        assert len(result["results"]) == len(collector.CANONICAL_OPS)
        assert all(r.startswith("ok") for r in result["results"].values())
        # one CACHE item per op + one META row
        assert ddb.put_item.call_count == len(collector.CANONICAL_OPS) + 1
        meta = ddb.put_item.call_args_list[-1].kwargs["Item"]
        assert meta["pk"]["S"] == "META" and meta["sk"]["S"] == "LAST_RUN"

    def test_one_failed_op_does_not_kill_the_sweep(self, env, monkeypatch):
        """get_org_tag_compliance_summary legitimately fails without an
        attached Tag Policy — the other snapshots must still be written."""
        lam = MagicMock()

        def _invoke(**kwargs):
            import base64

            ctx = json.loads(base64.b64decode(kwargs["ClientContext"]))
            op = ctx["custom"]["bedrockAgentCoreToolName"].split("___")[1]
            if op == "get_org_tag_compliance_summary":
                raise RuntimeError("no policy")
            return _lambda_response({"ok": op})

        lam.invoke.side_effect = _invoke
        ddb = MagicMock()
        monkeypatch.setattr(collector, "_lambda_client", lambda: lam)
        monkeypatch.setattr(collector, "_ddb", lambda: ddb)

        result = collector.handler({}, None)
        failed = [op for op, r in result["results"].items() if r.startswith("FAILED")]
        assert failed == ["get_org_tag_compliance_summary"]
        ok = [op for op, r in result["results"].items() if r.startswith("ok")]
        assert len(ok) == len(collector.CANONICAL_OPS) - 1

    def test_tool_error_payload_is_cached_distinctly(self, env, monkeypatch):
        """An error RESPONSE (vs an invoke failure) is a valid snapshot — the
        tool would return the same error live, just slower."""
        lam = MagicMock()
        lam.invoke.return_value = _lambda_response({"error": "No required-tag policy found"})
        ddb = MagicMock()
        monkeypatch.setattr(collector, "_lambda_client", lambda: lam)
        monkeypatch.setattr(collector, "_ddb", lambda: ddb)

        result = collector.handler({}, None)
        assert all(r.startswith("error-cached") for r in result["results"].values())

    def test_missing_env_raises(self, monkeypatch):
        monkeypatch.setattr(collector, "TABLE_NAME", "")
        with pytest.raises(RuntimeError, match="must be set"):
            collector.handler({}, None)
