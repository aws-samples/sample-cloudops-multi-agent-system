#!/usr/bin/env python3
"""
Build a console-only deployment package for the Lambda Runtime Upgrade MCP server.

Writes into skills/lambda-runtime-upgrade/mcp-server/ (checked in, a complete deploy kit):
  lambda-runtime-mcp.yaml   CloudFormation template (gateway, target with full
                            8-tool schema, tool Lambda, IAM roles, log group,
                            optional DevOps Agent invoke role)
  lambda-runtime-tool.zip   Lambda deployment package (handler + shared/)
Hand-maintained in the same folder (not generated):
  deployer-permissions.json Least-privilege policy for whoever creates the stack
  README.md                 Deploy + AWS DevOps Agent steps

Why this exists: customers who cannot install Terraform, Python, or a shell
can still deploy via the AWS Console by uploading one zip to S3 and creating
one CloudFormation stack. The tool schema is embedded in the template, so no
post-deploy gateway sync is needed (unlike the Terraform path).

Both artifacts are built deterministically (fixed zip timestamps/permissions,
sorted entries) so regenerating without source changes produces identical
bytes and no git churn.

Usage:
  .venv/bin/python scripts/build_cfn_mcp_package.py           # regenerate
  .venv/bin/python scripts/build_cfn_mcp_package.py --check   # exit 1 if stale
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS_JSON = ROOT / "src" / "lambda" / "mcp" / "tools.json"
TOOL_DIR = ROOT / "src" / "lambda" / "mcp" / "lambda-runtime"
SHARED_DIR = ROOT / "src" / "lambda" / "mcp" / "shared"
OUT_DIR = ROOT / "skills" / "lambda-runtime-upgrade" / "mcp-server"
TEMPLATE_NAME = "lambda-runtime-mcp.yaml"
ZIP_NAME = "lambda-runtime-tool.zip"
TOOL_KEY = "lambda-runtime"
# handler.py imports only shared.cross_account (which imports only boto3), so
# ship exactly that instead of the whole shared/ tree (saves ~800 KB of
# cloudwatch_domain data the Lambda tool never loads).
SHARED_FILES = ["__init__.py", "cross_account.py"]
# Fixed zip entry timestamp (zip epoch) so builds are byte-reproducible.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


# ---------------------------------------------------------------------------
# Schema conversion: tools.json -> CFN ToolDefinition / SchemaDefinition
# ---------------------------------------------------------------------------
def to_cfn_schema(schema: dict) -> dict:
    """Mirror sync.sh's sanitize_schema but emit CFN PascalCase keys.

    CFN SchemaDefinition supports: Type, Description, Properties, Items, Required.
    """
    out: dict = {}
    if "type" in schema:
        out["Type"] = schema["type"]
    if "description" in schema:
        out["Description"] = schema["description"]
    if schema.get("required"):
        out["Required"] = list(schema["required"])
    props = schema.get("properties")
    if isinstance(props, dict) and props:
        out["Properties"] = {k: to_cfn_schema(v) for k, v in props.items()}
    items = schema.get("items")
    if isinstance(items, dict):
        out["Items"] = to_cfn_schema(items)
    return out


def tool_definitions() -> list[dict]:
    cfg = json.loads(TOOLS_JSON.read_text())[TOOL_KEY]
    defs = []
    for tool in cfg["tools"]:
        input_schema = tool.get("input_schema") or {"type": "object"}
        defs.append(
            {
                "Name": tool["name"],
                "Description": tool.get("description", f"Tool: {tool['name']}"),
                "InputSchema": to_cfn_schema(input_schema),
            }
        )
    return defs, cfg


# ---------------------------------------------------------------------------
# Lambda zip
# ---------------------------------------------------------------------------
def zip_sources() -> list[tuple[Path, str]]:
    """(source path, archive name) pairs, in a stable order."""
    pairs = [(py, py.name) for py in sorted(TOOL_DIR.glob("*.py"))]
    pairs += [(SHARED_DIR / name, f"shared/{name}") for name in SHARED_FILES]
    return pairs


def build_zip_bytes() -> bytes:
    """Zip handler.py + shared/ deterministically (fixed mtime + mode)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for src, arcname in zip_sources():
            info = zipfile.ZipInfo(arcname, date_time=_ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16  # rw-r--r--
            zf.writestr(info, src.read_bytes())
    return buf.getvalue()


# ---------------------------------------------------------------------------
# CloudFormation template
# ---------------------------------------------------------------------------
def build_template(defs: list[dict], cfg: dict) -> dict:
    iam_actions = cfg["iam_actions"]
    handler = cfg["handler"]
    runtime = cfg["runtime"]
    timeout = cfg["timeout"]
    memory = cfg["memory"]

    def sub(s: str) -> dict:
        return {"Fn::Sub": s}

    def ref(r: str) -> dict:
        return {"Ref": r}

    def getatt(r: str, a: str) -> dict:
        return {"Fn::GetAtt": [r, a]}

    tool_statements = [
        {"Effect": "Allow", "Action": iam_actions, "Resource": "*"},
        {
            "Effect": "Allow",
            "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
            "Resource": sub(
                "arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/lambda/${Prefix}-lambda-runtime-tool:*"
            ),
        },
    ]

    template = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": (
            "Lambda Runtime Upgrade MCP server: Bedrock AgentCore Gateway (SigV4) + "
            "read-only Lambda tool that discovers deprecated Lambda runtimes, reads "
            "function code, and detects breaking changes. Console-only deploy; no CLI needed."
        ),
        "Metadata": {
            "AWS::CloudFormation::Interface": {
                "ParameterGroups": [
                    {
                        "Label": {"default": "Deployment package"},
                        "Parameters": ["CodeBucket", "CodeKey"],
                    },
                    {
                        "Label": {"default": "Naming"},
                        "Parameters": ["Prefix", "LogRetentionDays"],
                    },
                    {
                        "Label": {"default": "Cross-account scanning (optional)"},
                        "Parameters": ["CrossAccountRoleArn", "CrossAccountExternalId"],
                    },
                    {
                        "Label": {"default": "AWS DevOps Agent integration (optional)"},
                        "Parameters": ["CreateDevOpsAgentRole", "DevOpsAgentAccountId"],
                    },
                ],
                "ParameterLabels": {
                    "CodeBucket": {"default": "S3 bucket containing lambda-runtime-tool.zip"},
                    "CodeKey": {"default": "S3 key of the zip"},
                    "Prefix": {"default": "Resource name prefix"},
                    "LogRetentionDays": {"default": "CloudWatch Logs retention (days)"},
                    "CrossAccountRoleArn": {"default": "Role ARN to assume in a target account"},
                    "CrossAccountExternalId": {"default": "External ID for that role"},
                    "CreateDevOpsAgentRole": {"default": "Create IAM role for AWS DevOps Agent?"},
                    "DevOpsAgentAccountId": {"default": "Account ID that owns the DevOps Agent Space"},
                },
            }
        },
        "Parameters": {
            "CodeBucket": {
                "Type": "String",
                "Description": "Name of the S3 bucket where you uploaded lambda-runtime-tool.zip. Must be in the same region as this stack.",
            },
            "CodeKey": {
                "Type": "String",
                "Default": "lambda-runtime-tool.zip",
                "Description": "Object key (path) of the zip inside the bucket.",
            },
            "Prefix": {
                "Type": "String",
                "Default": "lambda-upgrade",
                "AllowedPattern": "^[a-z][a-z0-9-]{1,30}$",
                "ConstraintDescription": "lowercase letters, digits, hyphens; 2-31 chars; must start with a letter",
                "Description": "Prefix for all resource names (gateway, Lambda, roles).",
            },
            "LogRetentionDays": {
                "Type": "Number",
                "Default": 30,
                "AllowedValues": [7, 14, 30, 60, 90, 180, 365],
            },
            "CrossAccountRoleArn": {
                "Type": "String",
                "Default": "",
                "Description": "Optional. IAM role ARN in another account for the tool to assume (read-only Lambda/EC2 permissions). Leave blank to scan this account only.",
            },
            "CrossAccountExternalId": {
                "Type": "String",
                "Default": "",
                "NoEcho": True,
                "Description": "Optional. External ID required by the cross-account role, if any.",
            },
            "CreateDevOpsAgentRole": {
                "Type": "String",
                "Default": "true",
                "AllowedValues": ["true", "false"],
                "Description": "Create the IAM role that AWS DevOps Agent assumes to call this gateway (SigV4).",
            },
            "DevOpsAgentAccountId": {
                "Type": "String",
                "Default": "",
                "AllowedPattern": "^$|^[0-9]{12}$",
                "Description": "Account that owns the DevOps Agent Space. Leave blank to use this account.",
            },
        },
        "Conditions": {
            "HasCrossAccountRole": {"Fn::Not": [{"Fn::Equals": [ref("CrossAccountRoleArn"), ""]}]},
            "CreateDevOpsRole": {"Fn::Equals": [ref("CreateDevOpsAgentRole"), "true"]},
            "DevOpsAccountIsThisAccount": {"Fn::Equals": [ref("DevOpsAgentAccountId"), ""]},
        },
        "Resources": {
            # --- Tool Lambda -------------------------------------------------
            "ToolLogGroup": {
                "Type": "AWS::Logs::LogGroup",
                "DeletionPolicy": "Delete",
                "UpdateReplacePolicy": "Delete",
                "Properties": {
                    "LogGroupName": sub("/aws/lambda/${Prefix}-lambda-runtime-tool"),
                    "RetentionInDays": ref("LogRetentionDays"),
                },
            },
            "ToolRole": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "RoleName": sub("${Prefix}-lambda-runtime-tool-role"),
                    "AssumeRolePolicyDocument": {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Principal": {"Service": "lambda.amazonaws.com"},
                                "Action": "sts:AssumeRole",
                            }
                        ],
                    },
                    "Policies": [
                        {
                            "PolicyName": "lambda-runtime-tool-readonly",
                            "PolicyDocument": {
                                "Version": "2012-10-17",
                                "Statement": tool_statements,
                            },
                        }
                    ],
                },
            },
            "ToolRoleCrossAccountPolicy": {
                "Type": "AWS::IAM::Policy",
                "Condition": "HasCrossAccountRole",
                "Properties": {
                    "PolicyName": "lambda-runtime-tool-assume-cross-account",
                    "Roles": [ref("ToolRole")],
                    "PolicyDocument": {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": "sts:AssumeRole",
                                "Resource": ref("CrossAccountRoleArn"),
                            }
                        ],
                    },
                },
            },
            "ToolFunction": {
                "Type": "AWS::Lambda::Function",
                "DependsOn": ["ToolLogGroup"],
                "Properties": {
                    "FunctionName": sub("${Prefix}-lambda-runtime-tool"),
                    "Description": "Lambda Runtime Upgrade MCP tool (read-only). Discovers deprecated runtimes, downloads code, detects breaking changes.",
                    "Runtime": runtime,
                    "Handler": handler,
                    "Timeout": timeout,
                    "MemorySize": memory,
                    "Role": getatt("ToolRole", "Arn"),
                    "Code": {"S3Bucket": ref("CodeBucket"), "S3Key": ref("CodeKey")},
                    "Environment": {
                        "Variables": {
                            "CROSS_ACCOUNT_ROLE_ARN": ref("CrossAccountRoleArn"),
                            "CROSS_ACCOUNT_EXTERNAL_ID": ref("CrossAccountExternalId"),
                        }
                    },
                },
            },
            # --- Gateway -----------------------------------------------------
            "GatewayRole": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "RoleName": sub("${Prefix}-agentcore-gateway-role"),
                    "AssumeRolePolicyDocument": {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Principal": {
                                    "Service": ["bedrock-agentcore.amazonaws.com", "bedrock.amazonaws.com"]
                                },
                                "Action": "sts:AssumeRole",
                                "Condition": {
                                    "StringEquals": {"aws:SourceAccount": ref("AWS::AccountId")}
                                },
                            }
                        ],
                    },
                    "Policies": [
                        {
                            "PolicyName": "invoke-tool-lambda",
                            "PolicyDocument": {
                                "Version": "2012-10-17",
                                "Statement": [
                                    {
                                        "Effect": "Allow",
                                        "Action": "lambda:InvokeFunction",
                                        "Resource": getatt("ToolFunction", "Arn"),
                                    }
                                ],
                            },
                        }
                    ],
                },
            },
            "Gateway": {
                "Type": "AWS::BedrockAgentCore::Gateway",
                "Properties": {
                    "Name": sub("${Prefix}-gateway"),
                    "Description": "MCP gateway for the Lambda Runtime Upgrade tool (SigV4 auth)",
                    "ProtocolType": "MCP",
                    "AuthorizerType": "AWS_IAM",
                    "RoleArn": getatt("GatewayRole", "Arn"),
                },
            },
            "GatewayTarget": {
                "Type": "AWS::BedrockAgentCore::GatewayTarget",
                "Properties": {
                    "GatewayIdentifier": getatt("Gateway", "GatewayIdentifier"),
                    "Name": TOOL_KEY,
                    "Description": "Lambda runtime upgrade discovery, code analysis, migration guidance",
                    "CredentialProviderConfigurations": [
                        {"CredentialProviderType": "GATEWAY_IAM_ROLE"}
                    ],
                    "TargetConfiguration": {
                        "Mcp": {
                            "Lambda": {
                                "LambdaArn": getatt("ToolFunction", "Arn"),
                                "ToolSchema": {"InlinePayload": defs},
                            }
                        }
                    },
                },
            },
            # --- DevOps Agent invoke role (optional) --------------------------
            "DevOpsAgentInvokeRole": {
                "Type": "AWS::IAM::Role",
                "Condition": "CreateDevOpsRole",
                "Properties": {
                    "RoleName": sub("${Prefix}-devops-agent-gateway-invoke"),
                    "Description": "Assumed by AWS DevOps Agent to SigV4-sign MCP calls to the gateway",
                    "AssumeRolePolicyDocument": {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Principal": {"Service": "aidevops.amazonaws.com"},
                                "Action": "sts:AssumeRole",
                                "Condition": {
                                    "StringEquals": {
                                        "aws:SourceAccount": {
                                            "Fn::If": [
                                                "DevOpsAccountIsThisAccount",
                                                ref("AWS::AccountId"),
                                                ref("DevOpsAgentAccountId"),
                                            ]
                                        }
                                    }
                                },
                            }
                        ],
                    },
                    "Policies": [
                        {
                            "PolicyName": "invoke-gateway",
                            "PolicyDocument": {
                                "Version": "2012-10-17",
                                "Statement": [
                                    {
                                        "Effect": "Allow",
                                        "Action": "bedrock-agentcore:InvokeGateway",
                                        "Resource": getatt("Gateway", "GatewayArn"),
                                    }
                                ],
                            },
                        }
                    ],
                },
            },
        },
        "Outputs": {
            "GatewayEndpointUrl": {
                "Description": "MCP endpoint. Register this in AWS DevOps Agent (Capability Providers > MCP Server) or any MCP client.",
                "Value": getatt("Gateway", "GatewayUrl"),
            },
            "GatewayArn": {"Value": getatt("Gateway", "GatewayArn")},
            "GatewayId": {"Value": getatt("Gateway", "GatewayIdentifier")},
            "ToolFunctionArn": {"Value": getatt("ToolFunction", "Arn")},
            "DevOpsAgentRoleArn": {
                "Condition": "CreateDevOpsRole",
                "Description": "Select this role when registering the MCP server in DevOps Agent with SigV4 auth. Service name: bedrock-agentcore.",
                "Value": getatt("DevOpsAgentInvokeRole", "Arn"),
            },
            "SigV4ServiceName": {"Value": "bedrock-agentcore"},
            "ToolsExposed": {"Value": ", ".join(d["Name"] for d in defs)},
        },
    }
    return template



def render_template_yaml(template: dict) -> str:
    # YAML is required: the checked-in template is YAML, and silently falling
    # back to JSON would leave a stale .yaml in the repo.
    import yaml  # type: ignore

    class _Dumper(yaml.SafeDumper):
        pass

    def _str(dumper, data):
        style = "|" if "\n" in data else None
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)

    _Dumper.add_representer(str, _str)
    return yaml.dump(template, Dumper=_Dumper, sort_keys=False, width=100, allow_unicode=True)


def build_artifacts() -> dict[str, bytes]:
    """Return {filename: bytes} for every generated file in OUT_DIR."""
    defs, cfg = tool_definitions()
    body = render_template_yaml(build_template(defs, cfg))
    return {TEMPLATE_NAME: body.encode("utf-8"), ZIP_NAME: build_zip_bytes()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; exit 1 if the checked-in artifacts are missing or stale.",
    )
    args = parser.parse_args(argv)

    try:
        artifacts = build_artifacts()
    except ImportError:
        print("error: PyYAML is required (run `make setup` or `pip install pyyaml`)", file=sys.stderr)
        return 1

    if args.check:
        stale = [
            name
            for name, data in artifacts.items()
            if not (OUT_DIR / name).exists() or (OUT_DIR / name).read_bytes() != data
        ]
        if stale:
            rel = OUT_DIR.relative_to(ROOT)
            print(f"stale or missing in {rel}: {', '.join(stale)}", file=sys.stderr)
            print("run `make cfn-mcp-package` and commit the result", file=sys.stderr)
            return 1
        print(f"{OUT_DIR.relative_to(ROOT)} is up to date")
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, data in artifacts.items():
        (OUT_DIR / name).write_bytes(data)

    tpl_size = len(artifacts[TEMPLATE_NAME])
    print(f"wrote {OUT_DIR / TEMPLATE_NAME} ({tpl_size:,} bytes)")
    print(f"wrote {OUT_DIR / ZIP_NAME} ({len(zip_sources())} files, {len(artifacts[ZIP_NAME]):,} bytes)")
    if tpl_size > 51_200:
        print("note: template exceeds 51,200 bytes; console upload still works (uploads to S3 automatically)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
