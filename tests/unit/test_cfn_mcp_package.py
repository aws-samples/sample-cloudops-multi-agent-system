"""Guard the checked-in Lambda Runtime MCP CloudFormation kit.

skills/lambda-runtime-upgrade/mcp-server/ ships a generated template and a prebuilt
Lambda zip so users can deploy without building anything. These tests fail
when either artifact drifts from src/lambda/mcp (fix: `make cfn-mcp-package`
and commit), and pin the security-relevant shape of the template.
"""

from __future__ import annotations

import importlib.util
import io
import json
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "build_cfn_mcp_package.py"


@pytest.fixture(scope="module")
def builder():
    spec = importlib.util.spec_from_file_location("build_cfn_mcp_package", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def template(builder):
    defs, cfg = builder.tool_definitions()
    return builder.build_template(defs, cfg)


def test_checked_in_artifacts_are_up_to_date(builder):
    """Committed yaml + zip must equal a fresh build (run `make cfn-mcp-package`)."""
    assert builder.main(["--check"]) == 0


def test_zip_build_is_deterministic(builder):
    assert builder.build_zip_bytes() == builder.build_zip_bytes()


def test_zip_contains_handler_and_shared_helper(builder):
    with zipfile.ZipFile(io.BytesIO(builder.build_zip_bytes())) as zf:
        names = set(zf.namelist())
    assert {"handler.py", "shared/__init__.py", "shared/cross_account.py"} <= names


def test_template_tools_match_tools_json(template):
    tools_json = json.loads((ROOT / "src/lambda/mcp/tools.json").read_text())["lambda-runtime"]
    expected = {t["name"] for t in tools_json["tools"]}
    payload = template["Resources"]["GatewayTarget"]["Properties"]["TargetConfiguration"]["Mcp"]["Lambda"][
        "ToolSchema"
    ]["InlinePayload"]
    assert {t["Name"] for t in payload} == expected


def test_gateway_requires_sigv4(template):
    gw = template["Resources"]["Gateway"]["Properties"]
    assert gw["AuthorizerType"] == "AWS_IAM"
    assert gw["ProtocolType"] == "MCP"


def test_devops_agent_role_is_scoped(template):
    role = template["Resources"]["DevOpsAgentInvokeRole"]["Properties"]
    trust = role["AssumeRolePolicyDocument"]["Statement"][0]
    assert trust["Principal"] == {"Service": "aidevops.amazonaws.com"}
    assert "aws:SourceAccount" in trust["Condition"]["StringEquals"]
    stmt = role["Policies"][0]["PolicyDocument"]["Statement"]
    assert stmt == [
        {
            "Effect": "Allow",
            "Action": "bedrock-agentcore:InvokeGateway",
            "Resource": {"Fn::GetAtt": ["Gateway", "GatewayArn"]},
        }
    ]


def test_tool_role_is_read_only(template):
    actions = template["Resources"]["ToolRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"][0][
        "Action"
    ]
    verbs = {a.split(":", 1)[1] for a in actions}
    assert all(v.startswith(("List", "Get", "Describe")) for v in verbs), actions
