"""Unit tests for the lambda-runtime MCP tool.

Covers the 7 functional tools + dispatcher:
  * Dispatcher — unknown tool returns error + tool list; known tool routes
  * _classify_runtime — deprecated vs EOL vs active vs unknown (pure logic)
  * handle_get_function_configuration — success + missing function_name
  * handle_get_function_code — success, missing function_name, HTTP failure,
    50KB per-file cap, 200KB total cap
  * handle_get_deprecated_functions_multi_region — success + per-region failure
  * handle_discover_lambda_regions — success + region scan error
  * handle_list_functions_by_runtime — success + function_name contains filter

The handler imports boto3/urllib3 at module level. Tests mock
_get_lambda_client() directly so each test starts from a clean client.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Default region before importing the handler.
os.environ.setdefault("AWS_REGION", "us-east-1")

# Load the handler under a namespaced module name so it doesn't collide with
# other `handler.py` modules.
_HANDLER_PATH = _REPO_ROOT / "src" / "lambda" / "mcp" / "lambda-runtime" / "handler.py"
_spec = importlib.util.spec_from_file_location("lambda_runtime_handler", _HANDLER_PATH)
handler = importlib.util.module_from_spec(_spec)

# Mock the shared.cross_account import that happens at module level
sys.modules["shared"] = MagicMock()
sys.modules["shared.cross_account"] = MagicMock()
_spec.loader.exec_module(handler)
# Register the module so patch() can resolve it
sys.modules["lambda_runtime_handler"] = handler


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _make_context(tool_name: str) -> SimpleNamespace:
    """Build the AgentCore Gateway context shape the dispatcher reads."""
    return SimpleNamespace(
        client_context=SimpleNamespace(
            custom={"bedrockAgentCoreToolName": f"lambda-runtime___{tool_name}"}
        )
    )


def _make_zip(files: dict[str, str | bytes]) -> bytes:
    """Create an in-memory zip file from a filename→content mapping."""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            if isinstance(content, str):
                content = content.encode("utf-8")
            zf.writestr(name, content)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class TestDispatcher:
    def test_unknown_tool_returns_error_with_tool_list(self):
        ctx = _make_context("nonexistent_tool")
        result = handler.handler({}, ctx)
        assert result["error"].startswith("Unknown tool: nonexistent_tool")
        assert "discover_lambda_regions" in result["available_tools"]
        assert "get_function_code" in result["available_tools"]
        assert len(result["available_tools"]) == 7

    def test_known_tool_routes_correctly(self, monkeypatch):
        """Dispatcher routes to the correct handler function."""
        called = {}

        def fake(event):
            called["routed"] = True
            return {"ok": True}

        monkeypatch.setattr(handler, "handle_get_runtime_support_status", fake)
        ctx = _make_context("get_runtime_support_status")
        result = handler.handler({}, ctx)
        assert called.get("routed") is True
        assert result == {"ok": True}

    def test_routing_strips_target_prefix(self):
        """bedrockAgentCoreToolName is `target___tool_name` — dispatcher splits on ___."""
        ctx = SimpleNamespace(
            client_context=SimpleNamespace(
                custom={"bedrockAgentCoreToolName": "lambda-runtime___get_runtime_support_status"}
            )
        )
        # Should not raise; routes to the real handler
        result = handler.handler({}, ctx)
        assert "runtimes" in result or "error" not in result


# ---------------------------------------------------------------------------
# _classify_runtime — pure logic, no mocks needed (most important)
# ---------------------------------------------------------------------------


class TestClassifyRuntime:
    """Tests for _classify_runtime — the core classification logic."""

    def test_unknown_runtime_returns_unknown_status(self):
        result = handler._classify_runtime("go1.x")
        assert result["status"] == "unknown"
        assert result["runtime"] == "go1.x"

    def test_active_runtime(self):
        """python3.13 has no deprecation/EOL dates — always active."""
        result = handler._classify_runtime("python3.13")
        assert result["status"] == "active"
        assert result["runtime"] == "python3.13"
        assert result["family"] == "python"
        assert result["upgrade_target"] is None

    def test_deprecated_runtime(self):
        """python3.8 was deprecated 2024-10-14 — should show as deprecated or EOL."""
        result = handler._classify_runtime("python3.8")
        # As of 2025+, python3.8 is past both deprecation and EOL dates
        assert result["status"] in ("deprecated", "end_of_life")
        assert result["upgrade_target"] == "python3.12"
        assert result["family"] == "python"

    def test_end_of_life_runtime(self):
        """nodejs14.x has EOL date 2024-03-11 — should be end_of_life."""
        result = handler._classify_runtime("nodejs14.x")
        assert result["status"] == "end_of_life"
        assert result["upgrade_target"] == "nodejs20.x"
        assert result["family"] == "nodejs"

    def test_eol_takes_precedence_over_deprecated(self):
        """If both deprecation and EOL dates have passed, status is end_of_life."""
        # dotnet6: deprecated 2024-02-29, EOL 2024-05-29
        result = handler._classify_runtime("dotnet6")
        assert result["status"] == "end_of_life"

    def test_deprecated_but_not_yet_eol(self):
        """A runtime can be deprecated without being EOL if eol_date is None."""
        # Patch a runtime with deprecation in the past but no EOL
        with patch.dict(handler.RUNTIME_SUPPORT_DATA, {
            "test_rt": {
                "family": "test", "version": "1.0", "status": "active",
                "deprecation_date": "2020-01-01", "eol_date": None,
                "upgrade_target": "test_rt2",
            }
        }):
            result = handler._classify_runtime("test_rt")
            assert result["status"] == "deprecated"

    def test_active_with_future_deprecation(self):
        """A runtime with a future deprecation date is still active."""
        with patch.dict(handler.RUNTIME_SUPPORT_DATA, {
            "future_rt": {
                "family": "test", "version": "2.0", "status": "active",
                "deprecation_date": "2099-12-31", "eol_date": None,
                "upgrade_target": "future_rt2",
            }
        }):
            result = handler._classify_runtime("future_rt")
            assert result["status"] == "active"

    def test_all_fields_present_for_known_runtime(self):
        """Known runtimes return all expected fields."""
        result = handler._classify_runtime("nodejs20.x")
        expected_keys = {"runtime", "family", "version", "status",
                         "deprecation_date", "eol_date", "upgrade_target"}
        assert set(result.keys()) == expected_keys


# ---------------------------------------------------------------------------
# handle_get_function_configuration
# ---------------------------------------------------------------------------


class TestGetFunctionConfiguration:
    def test_missing_function_name_returns_error(self):
        result = handler.handle_get_function_configuration({})
        assert result["error"] == "function_name is required"

    def test_success(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.get_function_configuration.return_value = {
            "FunctionName": "my-func",
            "FunctionArn": "arn:aws:lambda:us-east-1:123:function:my-func",
            "Runtime": "python3.12",
            "Handler": "index.handler",
            "CodeSize": 4096,
            "MemorySize": 256,
            "Timeout": 30,
            "LastModified": "2024-01-01T00:00:00Z",
            "Architectures": ["arm64"],
            "Layers": [{"Arn": "arn:aws:lambda:us-east-1:123:layer:my-layer:1", "CodeSize": 1024}],
            "Environment": {"Variables": {"ENV": "prod", "LOG_LEVEL": "info"}},
            "PackageType": "Zip",
            "EphemeralStorage": {"Size": 1024},
        }
        monkeypatch.setattr(handler, "_get_lambda_client", lambda region=None: mock_client)

        result = handler.handle_get_function_configuration({"function_name": "my-func"})
        assert result["function_name"] == "my-func"
        assert result["runtime"] == "python3.12"
        assert result["memory_mb"] == 256
        assert result["timeout_seconds"] == 30
        assert len(result["layers"]) == 1
        assert "ENV" in result["environment_variables"]

    def test_client_exception_returns_error(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.get_function_configuration.side_effect = Exception("ResourceNotFoundException")
        monkeypatch.setattr(handler, "_get_lambda_client", lambda region=None: mock_client)

        result = handler.handle_get_function_configuration({"function_name": "no-such-func"})
        assert "ResourceNotFoundException" in result["error"]


# ---------------------------------------------------------------------------
# handle_get_function_code
# ---------------------------------------------------------------------------


class TestGetFunctionCode:
    def test_missing_function_name_returns_error(self):
        result = handler.handle_get_function_code({})
        assert result["error"] == "function_name is required"

    def test_success_extracts_code_files(self, monkeypatch):
        zip_content = _make_zip({
            "index.py": "def handler(event, ctx): return 'ok'",
            "utils.py": "import os",
            "requirements.txt": "boto3==1.28.0",
            "data.bin": b"\x00\x01\x02",  # non-code, should be skipped
        })

        mock_client = MagicMock()
        mock_client.get_function.return_value = {
            "Code": {"Location": "https://example.com/code.zip"}
        }
        monkeypatch.setattr(handler, "_get_lambda_client", lambda region=None: mock_client)

        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.data = zip_content
        mock_http.request.return_value = mock_resp

        with patch("lambda_runtime_handler.urllib3.PoolManager", return_value=mock_http):
            result = handler.handle_get_function_code({"function_name": "my-func"})

        assert result["function_name"] == "my-func"
        assert "index.py" in result["source_files"]
        assert "utils.py" in result["source_files"]
        assert "requirements.txt" in result["source_files"]
        # .bin file has no code extension and isn't a dep file
        assert "data.bin" not in result["source_files"]

    def test_http_download_failure(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.get_function.return_value = {
            "Code": {"Location": "https://example.com/code.zip"}
        }
        monkeypatch.setattr(handler, "_get_lambda_client", lambda region=None: mock_client)

        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 403
        mock_http.request.return_value = mock_resp

        with patch("lambda_runtime_handler.urllib3.PoolManager", return_value=mock_http):
            result = handler.handle_get_function_code({"function_name": "my-func"})

        assert "Failed to download code: HTTP 403" in result["error"]

    def test_per_file_size_cap_50kb(self, monkeypatch):
        """Files larger than max_file_size_kb (default 50KB) are skipped."""
        big_content = "x" * (51 * 1024)  # 51KB — exceeds default 50KB
        zip_content = _make_zip({
            "big.py": big_content,
            "small.py": "print('hi')",
        })

        mock_client = MagicMock()
        mock_client.get_function.return_value = {
            "Code": {"Location": "https://example.com/code.zip"}
        }
        monkeypatch.setattr(handler, "_get_lambda_client", lambda region=None: mock_client)

        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.data = zip_content
        mock_http.request.return_value = mock_resp

        with patch("lambda_runtime_handler.urllib3.PoolManager", return_value=mock_http):
            result = handler.handle_get_function_code({"function_name": "my-func"})

        assert "[SKIPPED:" in result["source_files"]["big.py"]
        assert "exceeds limit" in result["source_files"]["big.py"]
        assert result["source_files"]["small.py"] == "print('hi')"

    def test_total_size_cap_200kb(self, monkeypatch):
        """Total extracted content capped at 200KB."""
        # Create files that individually are under 50KB but together exceed 200KB
        files = {}
        for i in range(10):
            files[f"file{i}.py"] = "y" * (25 * 1024)  # 25KB each = 250KB total
        zip_content = _make_zip(files)

        mock_client = MagicMock()
        mock_client.get_function.return_value = {
            "Code": {"Location": "https://example.com/code.zip"}
        }
        monkeypatch.setattr(handler, "_get_lambda_client", lambda region=None: mock_client)

        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.data = zip_content
        mock_http.request.return_value = mock_resp

        with patch("lambda_runtime_handler.urllib3.PoolManager", return_value=mock_http):
            result = handler.handle_get_function_code({"function_name": "my-func"})

        # Some files should be skipped due to total limit
        skipped = [f for f, c in result["source_files"].items()
                   if isinstance(c, str) and "total extraction limit" in c]
        assert len(skipped) > 0
        assert result["total_size_kb"] <= 200

    def test_no_code_location_returns_error(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.get_function.return_value = {"Code": {}}
        monkeypatch.setattr(handler, "_get_lambda_client", lambda region=None: mock_client)

        result = handler.handle_get_function_code({"function_name": "container-func"})
        assert "Unable to retrieve function code location" in result["error"]


# ---------------------------------------------------------------------------
# handle_get_deprecated_functions_multi_region
# ---------------------------------------------------------------------------


class TestGetDeprecatedFunctionsMultiRegion:
    def test_missing_regions_returns_error(self):
        result = handler.handle_get_deprecated_functions_multi_region({})
        assert "regions array is required" in result["error"]

    def test_success_scans_multiple_regions(self, monkeypatch):
        """Successfully scans multiple regions and aggregates results."""
        def mock_client_factory(region=None):
            client = MagicMock()
            paginator = MagicMock()
            if region == "us-east-1":
                paginator.paginate.return_value = iter([{
                    "Functions": [
                        {"FunctionName": "old-func", "Runtime": "python3.8",
                         "LastModified": "2023-06-01", "CodeSize": 2048,
                         "Handler": "index.handler", "MemorySize": 128},
                    ]
                }])
            else:
                paginator.paginate.return_value = iter([{
                    "Functions": [
                        {"FunctionName": "legacy-node", "Runtime": "nodejs14.x",
                         "LastModified": "2022-01-01", "CodeSize": 4096,
                         "Handler": "app.handler", "MemorySize": 256},
                    ]
                }])
            client.get_paginator.return_value = paginator
            return client

        monkeypatch.setattr(handler, "_get_lambda_client", mock_client_factory)

        result = handler.handle_get_deprecated_functions_multi_region({
            "regions": ["us-east-1", "eu-west-1"]
        })

        assert result["regions_scanned"] == 2
        assert result["total_functions"] == 2
        assert len(result["results_by_region"]) == 2

        # Check by_runtime aggregation
        assert "python3.8" in result["by_runtime"] or "nodejs14.x" in result["by_runtime"]

    def test_per_region_failure_graceful(self, monkeypatch):
        """A failing region returns error but doesn't crash the whole scan."""
        call_count = {"n": 0}

        def mock_client_factory(region=None):
            call_count["n"] += 1
            client = MagicMock()
            if region == "ap-southeast-1":
                # Simulate access denied for this region
                paginator = MagicMock()
                paginator.paginate.side_effect = Exception("AccessDeniedException")
                client.get_paginator.return_value = paginator
            else:
                paginator = MagicMock()
                paginator.paginate.return_value = iter([{
                    "Functions": [
                        {"FunctionName": "func-ok", "Runtime": "nodejs16.x",
                         "LastModified": "2023-01-01", "CodeSize": 1024,
                         "Handler": "index.handler", "MemorySize": 128},
                    ]
                }])
                client.get_paginator.return_value = paginator
            return client

        monkeypatch.setattr(handler, "_get_lambda_client", mock_client_factory)

        result = handler.handle_get_deprecated_functions_multi_region({
            "regions": ["us-east-1", "ap-southeast-1"]
        })

        assert result["regions_scanned"] == 2
        # One region succeeded, one failed
        region_results = {r["region"]: r for r in result["results_by_region"]}
        assert region_results["ap-southeast-1"]["count"] == 0
        assert "error" in region_results["ap-southeast-1"]
        assert region_results["us-east-1"]["count"] == 1


# ---------------------------------------------------------------------------
# handle_discover_lambda_regions
# ---------------------------------------------------------------------------


class TestDiscoverLambdaRegions:
    def test_success(self, monkeypatch):
        """Discovers regions with Lambda functions."""
        mock_ec2 = MagicMock()
        mock_ec2.describe_regions.return_value = {
            "Regions": [
                {"RegionName": "us-east-1"},
                {"RegionName": "eu-west-1"},
            ]
        }

        def mock_get_client(service, region_name=None):
            if service == "ec2":
                return mock_ec2
            # Lambda client
            client = MagicMock()
            paginator = MagicMock()
            if region_name == "us-east-1":
                paginator.paginate.return_value = iter([{
                    "Functions": [
                        {"FunctionName": "f1", "Runtime": "python3.8"},
                        {"FunctionName": "f2", "Runtime": "python3.12"},
                    ]
                }])
            else:
                paginator.paginate.return_value = iter([{"Functions": []}])
            client.get_paginator.return_value = paginator
            return client

        # Patch the shared.cross_account.get_aws_client used inside
        with patch("lambda_runtime_handler.boto3"):
            monkeypatch.setattr(
                sys.modules["shared.cross_account"], "get_aws_client", mock_get_client
            )
            result = handler.handle_discover_lambda_regions({})

        assert result["total_functions"] == 2
        assert result["total_regions"] == 1  # only us-east-1 has functions

    def test_region_scan_error_handled(self, monkeypatch):
        """A region that throws during scan doesn't crash discovery."""
        mock_ec2 = MagicMock()
        mock_ec2.describe_regions.return_value = {
            "Regions": [{"RegionName": "us-east-1"}]
        }

        def mock_get_client(service, region_name=None):
            if service == "ec2":
                return mock_ec2
            raise Exception("Connection timeout")

        with patch("lambda_runtime_handler.boto3"):
            monkeypatch.setattr(
                sys.modules["shared.cross_account"], "get_aws_client", mock_get_client
            )
            result = handler.handle_discover_lambda_regions({})

        # Should return empty results, not crash
        assert result["total_functions"] == 0


# ---------------------------------------------------------------------------
# handle_list_functions_by_runtime
# ---------------------------------------------------------------------------


class TestListFunctionsByRuntime:
    def test_success_with_runtime_filter(self, monkeypatch):
        mock_client = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = iter([{
            "Functions": [
                {"FunctionName": "py-func", "Runtime": "python3.12",
                 "Handler": "app.handler", "LastModified": "2024-01-01",
                 "MemorySize": 128, "CodeSize": 2048, "Architectures": ["arm64"]},
                {"FunctionName": "node-func", "Runtime": "nodejs20.x",
                 "Handler": "index.handler", "LastModified": "2024-01-01",
                 "MemorySize": 256, "CodeSize": 4096, "Architectures": ["x86_64"]},
            ]
        }])
        mock_client.get_paginator.return_value = paginator
        monkeypatch.setattr(handler, "_get_lambda_client", lambda region=None: mock_client)

        result = handler.handle_list_functions_by_runtime({"runtime": "python3.12"})
        assert result["count"] == 1
        assert result["functions"][0]["function_name"] == "py-func"

    def test_function_name_filter_contains_match(self, monkeypatch):
        """function_name_filter does a substring match for friendlier lookup."""
        mock_client = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = iter([{
            "Functions": [
                {"FunctionName": "SpringCleanLambda", "Runtime": "java11",
                 "Handler": "com.App", "LastModified": "2024-01-01",
                 "MemorySize": 512, "CodeSize": 8192, "Architectures": ["x86_64"]},
                {"FunctionName": "OtherFunc", "Runtime": "java11",
                 "Handler": "com.Other", "LastModified": "2024-01-01",
                 "MemorySize": 128, "CodeSize": 1024, "Architectures": ["x86_64"]},
            ]
        }])
        mock_client.get_paginator.return_value = paginator
        monkeypatch.setattr(handler, "_get_lambda_client", lambda region=None: mock_client)

        result = handler.handle_list_functions_by_runtime({"function_name_filter": "SpringClean"})
        assert result["count"] == 1
        assert result["functions"][0]["function_name"] == "SpringCleanLambda"


# ---------------------------------------------------------------------------
# handle_get_runtime_support_status
# ---------------------------------------------------------------------------


class TestGetRuntimeSupportStatus:
    def test_returns_all_runtimes(self):
        result = handler.handle_get_runtime_support_status({})
        assert result["total"] == len(handler.RUNTIME_SUPPORT_DATA)
        assert result["summary"]["active"] > 0

    def test_family_filter(self):
        result = handler.handle_get_runtime_support_status({"family": "python"})
        for rt in result["runtimes"]:
            assert rt["family"] == "python"
        assert result["total"] > 0


# ---------------------------------------------------------------------------
# handle_get_deprecated_functions
# ---------------------------------------------------------------------------


class TestGetDeprecatedFunctions:
    def test_finds_deprecated_functions(self, monkeypatch):
        mock_client = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = iter([{
            "Functions": [
                {"FunctionName": "old", "Runtime": "python3.8",
                 "LastModified": "2023-01-01", "CodeSize": 2048},
                {"FunctionName": "new", "Runtime": "python3.13",
                 "LastModified": "2024-06-01", "CodeSize": 1024},
            ]
        }])
        mock_client.get_paginator.return_value = paginator
        monkeypatch.setattr(handler, "_get_lambda_client", lambda region=None: mock_client)

        result = handler.handle_get_deprecated_functions({})
        # python3.8 is deprecated/EOL, python3.13 is active
        assert result["count"] == 1
        assert result["deprecated_functions"][0]["function_name"] == "old"

    def test_client_error(self, monkeypatch):
        mock_client = MagicMock()
        paginator = MagicMock()
        paginator.paginate.side_effect = Exception("AccessDenied")
        mock_client.get_paginator.return_value = paginator
        monkeypatch.setattr(handler, "_get_lambda_client", lambda region=None: mock_client)

        result = handler.handle_get_deprecated_functions({})
        assert "AccessDenied" in result["error"]
