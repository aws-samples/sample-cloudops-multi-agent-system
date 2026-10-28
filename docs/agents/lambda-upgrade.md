# Lambda Runtime Upgrade Agent

Discovers Lambda functions running on deprecated or end-of-life runtimes,
analyzes their source code for compatibility issues with newer runtimes,
and provides step-by-step migration guidance with concrete code changes.

**Representative prompts:**
- "Which of my Lambda functions are on deprecated runtimes?"
- "Analyze my-function for upgrading from Python 3.8 to 3.12"
- "Generate a runtime upgrade report"
- "How do I migrate my Node.js 16 Lambda to Node.js 20?"

## User Interaction Flow

The agent follows a **region-selection-first** workflow. For full reports it
prefers the one-shot `generate_upgrade_analysis` tool, which runs steps 3-5
server-side in a single call. Steps 1-2 still happen first, so the user picks the regions.

1. **Region Discovery** — Scans all enabled AWS regions in parallel to find
   which have Lambda functions and how many are deprecated/EOL.
2. **Region Selection** — Presents a summary table and asks the user which
   regions to include in the report.
3. **Multi-Region Scan** — Scans selected regions in parallel for deprecated
   functions using `get_deprecated_functions_multi_region`.
4. **Code Analysis** — Downloads code for the top 5 CRITICAL/HIGH priority
   functions only (speed optimization).
5. **Report Generation** — Produces a structured report with executive summary,
   region inventory, code analysis, and migration playbook.

**Target report generation time: 5-10 minutes** (achieved via parallel region
scanning, selective code download, and knowledge-base-driven guidance for
lower-priority functions).

## Supported runtimes

| Family | Versions tracked | Key migration concerns |
|--------|-----------------|----------------------|
| Python | 3.8 → 3.13 | Removed stdlib modules (3.12), boto3 bundling |
| Node.js | 14.x → 22.x | AWS SDK v2→v3, CommonJS→ESM, OpenSSL 3.0 |
| Java | 8 → 21 | JPMS modules, javax.* removal, SDK v1→v2 |
| .NET | 6 → 8 | AOT compilation, System.Text.Json changes |
| Ruby | 3.2 → 3.3 | Minimal breaking changes |
| Custom | provided.al2 → al2023 | glibc 2.34+, OpenSSL 3.0, recompile natives |

## Tools (8)

| Tool | Purpose | Performance |
|------|---------|-------------|
| `generate_upgrade_analysis` | One-shot: discover/scan regions in parallel, enrich with effort bucket + idle days, download code for the top functions, run breaking-change detection, return a pre-analyzed dataset + `report_markdown` | Single call; replaces the multi-tool chain |
| `discover_lambda_regions` | Find all regions with Lambda functions | ~15-30s (parallel scan) |
| `list_functions_by_runtime` | List functions filtered by runtime/region | ~5-10s per region |
| `get_function_configuration` | Detailed function config | ~1-2s per function |
| `get_function_code` | Download source for analysis | ~3-10s per function |
| `get_runtime_support_status` | All runtimes with dates | Instant (static data) |
| `get_deprecated_functions` | Deprecated functions in one region | ~5-10s per region |
| `get_deprecated_functions_multi_region` | Deprecated functions across regions (parallel) | ~15-30s total |

## Deploy modes

In every mode the same read-only tool Lambda (`src/lambda/mcp/lambda-runtime/`) does
the work behind an AgentCore Gateway (MCP, `AWS_IAM` auth). The modes differ
in what calls it.

```
Do you run the full CloudOps platform (chat UI, supervisor, other agents)?
├── Yes → Mode 1: Platform. lambda-upgrade-agent under ops-excellence.
└── No
    ├── Using AWS DevOps Agent? → Mode 3: Skill + MCP in an Agent Space.
    └── Using Kiro / Claude Code / another MCP client? → Mode 2: Standalone MCP server.
```

| Mode | What you deploy | How |
|------|-----------------|-----|
| **1. Platform** | Full stack. `lambda-upgrade-agent` is a worker under `ops-excellence-agent`. | `make deploy-auto` |
| **2. Standalone MCP server** | Gateway + tool Lambda only | CloudFormation: [`skills/lambda-runtime-upgrade/mcp-server/`](../../skills/lambda-runtime-upgrade/mcp-server/README.md) (template + prebuilt zip, nothing to build). Terraform alternative: `DEPLOY_MODE=gateway-only DEPLOY_TOOLS=lambda-runtime make deploy-auto` |
| **3. Skill + MCP in AWS DevOps Agent** | Mode 2, plus the MCP server registered in DevOps Agent with SigV4 auth, plus the [`skills/lambda-runtime-upgrade/`](../../skills/lambda-runtime-upgrade/SKILL.md) skill added to the Agent Space | [Runbook](../runbooks/lambda-runtime-mcp-devops-agent.md); summary in the [root README](../../README.md#lambda-runtime-upgrade-with-aws-devops-agent-skill--mcp) |

In Mode 3, the skill matters as much as the MCP server. DevOps Agent has no shell,
so it can't open the presigned URL `lambda:GetFunction` returns. The skill tells it
to read source from the tool response's `source_files` instead, and to say so when the
tools aren't in its tool list. The skill must ship with
`reference/runtime-migration-kb.md`, which it checks deprecation dates and
breaking changes against.

The CloudFormation stack can also create the IAM role DevOps Agent assumes
(`CreateDevOpsAgentRole=true`). That role trusts `aidevops.amazonaws.com`,
scoped by `aws:SourceAccount`, and can call only `bedrock-agentcore:InvokeGateway`
on this gateway.

## Prerequisites

No special setup beyond the deploy itself. The tool Lambda's role needs:
- `lambda:ListFunctions`: enumerate functions
- `lambda:GetFunction`: download the deployment package
- `lambda:GetFunctionConfiguration`: read runtime/layer/handler config
- `ec2:DescribeRegions`: discover enabled regions
- `cloudwatch:GetMetricData`: days since last invocation (deletion-candidate detection)

For cross-account scanning, create a role with the permissions above in the target
account, then set `CROSS_ACCOUNT_ROLE_ARN` (platform, via `make configure`) or
the `CrossAccountRoleArn` stack parameter (CloudFormation).

## Speed Optimizations

The agent targets 5-10 minute report generation through:

1. **Parallel region discovery** — All regions scanned concurrently (10 threads)
2. **Multi-region deprecated scan** — Single tool call scans all selected regions in parallel
3. **Selective code analysis** — Only top 5 CRITICAL/HIGH functions get code downloaded
4. **Knowledge-base guidance** — MEDIUM/LOW functions get migration advice from the
   embedded runtime knowledge base without code download
5. **Reduced report sections** — 3 sections (down from 5) with only one dependency chain

## Data model

### Tool: `discover_lambda_regions`

```json
{
  "regions_with_functions": [
    {
      "region": "us-east-1",
      "total_functions": 45,
      "deprecated_count": 8,
      "eol_count": 3,
      "needs_attention": 11,
      "runtimes": {"python3.8": 3, "nodejs16.x": 5, "python3.12": 37}
    }
  ],
  "total_regions": 5,
  "total_functions": 120,
  "total_deprecated": 15,
  "total_eol": 5,
  "total_needing_attention": 20
}
```

### Tool: `get_deprecated_functions_multi_region`

```json
{
  "results_by_region": [
    {
      "region": "us-east-1",
      "functions": [
        {
          "function_name": "my-api-handler",
          "runtime": "python3.8",
          "runtime_status": "end_of_life",
          "upgrade_target": "python3.12",
          "handler": "handler.lambda_handler",
          "last_modified": "2024-03-15T10:30:00Z",
          "code_size_bytes": 45000
        }
      ],
      "count": 8
    }
  ],
  "total_functions": 15,
  "regions_scanned": 3,
  "by_runtime": {
    "python3.8": {"count": 5, "status": "end_of_life", "upgrade_target": "python3.12"},
    "nodejs16.x": {"count": 10, "status": "deprecated", "upgrade_target": "nodejs20.x"}
  },
  "by_priority": {"end_of_life": 5, "deprecated": 10, "approaching": 0}
}
```

## Known gotchas

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `discover_lambda_regions` slow | Many enabled regions with functions | Normal — scanning 15+ regions takes ~30s |
| `get_function_code` returns empty files | Function uses container image packaging (`PackageType: Image`) | Container images can't be downloaded via GetFunction. Check `package_type` in config first. |
| Code extraction hits size limit | Large deployment package with bundled deps | Use `include_patterns` param to target specific files, or increase `max_file_size_kb`. |
| Cross-account functions not listed | Missing IAM permissions in target account | Ensure the cross-account role has `lambda:ListFunctions` + `lambda:GetFunction` + `ec2:DescribeRegions`. |
| Runtime shows "unknown" | New runtime not yet in the support data table | Update `RUNTIME_SUPPORT_DATA` in the handler, then run `make cfn-mcp-package` so the CloudFormation zip picks it up. |
| DevOps Agent says it can't download or unzip source | MCP tools not allowlisted in the Agent Space, or the skill isn't loaded | Add the `lambda-runtime___*` tools to the Agent Space and load the skill (Mode 3). |
| DevOps Agent registration fails: `Invalid STS role configuration for session sigv4McpServerSession` | Trust policy account doesn't match the Agent Space owner | Use only an `aws:SourceAccount` condition set to the Agent Space account, then wait about 60 s. See the [runbook](../runbooks/lambda-runtime-mcp-devops-agent.md#10-troubleshooting). |
| `make test` fails on `test_cfn_mcp_package.py` | Tool code or `tools.json` changed without regenerating the CloudFormation kit | `make cfn-mcp-package` and commit the updated YAML and zip. |
| Report takes >10 min | Too many CRITICAL/HIGH functions triggering code download | The agent limits to top 5; if you have more, run a second pass. |
