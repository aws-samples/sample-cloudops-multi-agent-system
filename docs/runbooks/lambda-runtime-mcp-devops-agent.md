# Runbook: Deploy the Lambda Runtime Upgrade MCP Server and Connect It to AWS DevOps Agent

This document walks through deploying only the MCP server portion of the CloudOps Multi-Agent Platform (the AgentCore Gateway plus the `lambda-runtime` tool Lambda) into an AWS account, then registering it as a capability provider in AWS DevOps Agent so the agent can discover deprecated Lambda runtimes, read function source code, and produce migration reports.

No agent containers, frontend, or Cognito are deployed. The footprint is fully serverless.

---

## 1. Overview

```
AWS DevOps Agent (Agent Space)
        │  SigV4 (assumes IAM role in your account)
        ▼
Bedrock AgentCore Gateway  ──MCP──▶  lambda-runtime tool Lambda
   (AWS_IAM authorizer)                 │ lambda:ListFunctions / GetFunction
                                        │ lambda:GetFunctionConfiguration
                                        ▼ ec2:DescribeRegions
                              Lambda functions in the account
```

The tool Lambda does the heavy work server-side: parallel region discovery, deprecated-runtime scanning, deployment-package download and unzip, and deterministic breaking-change detection. Source code is returned to the agent as text inside the MCP tool response, so the agent never needs a shell, `curl`, or `unzip`.

### Values used in this runbook

| Placeholder | Meaning | Example used here |
|---|---|---|
| `<ACCOUNT_ID>` | AWS account where the MCP server is deployed | `111122223333` |
| `<REGION>` | Deployment region | `us-east-1` |
| `<PREFIX>` | `PROJECT_PREFIX` from `.env` | `cops` |
| `<ENV>` | `ENVIRONMENT` from `.env` | `dev` |
| `<GATEWAY_ID>` | Gateway ID from Terraform output | `cops-gateway-abcde12345` |

> Prefer not to use Terraform? The same MCP server, including the DevOps Agent
> IAM role from section 5, can be deployed as a single CloudFormation stack.
> See [`skills/lambda-runtime-upgrade/mcp-server`](../../skills/lambda-runtime-upgrade/mcp-server/README.md).
> The template and the prebuilt Lambda zip are both in that folder.
> Then continue at section 6.

---

## 2. Prerequisites

On the deploying workstation:

- AWS CLI v2, authenticated to `<ACCOUNT_ID>` with admin-equivalent permissions for the first deploy
- Terraform >= 1.5
- Python 3.12
- Git

Not required for gateway-only mode: Finch/Docker (no container builds) and Node.js (no frontend build).

In the account:

- Amazon Bedrock AgentCore available in `<REGION>`
- No pre-existing resources named `<PREFIX>-*`

---

## 3. Deploy the MCP server

### 3.1 Clone and configure identity

```bash
git clone <cloudops-multi-agent-repo-url>
cd sample-cloudops-multi-agent-platform-main
make setup
```

`make setup` creates `.env` and installs Python dependencies. Edit `.env` and set:

```
AWS_PROFILE=<your-profile>
PROJECT_PREFIX=<PREFIX>
ENVIRONMENT=<ENV>
```

`PROJECT_PREFIX` and `ENVIRONMENT` together name the Terraform state backend and every resource, so choose a prefix that does not collide with an existing deployment in the account.

### 3.2 Configure shared settings

```bash
make configure
```

Accept defaults and select `<REGION>` when prompted. This writes `terraform/config.auto.tfvars.json` and mirrors the values to SSM Parameter Store under `/<PREFIX>/<ENV>/config/*`.

### 3.3 Deploy gateway + lambda-runtime tool only

```bash
DEPLOY_MODE=gateway-only DEPLOY_TOOLS=lambda-runtime make deploy-auto
```

- `DEPLOY_MODE=gateway-only` disables agents, frontend, Cognito, and memory.
- `DEPLOY_TOOLS=lambda-runtime` restricts the gateway to a single tool target.
- Both are shared keys and must be passed on the command line, not placed in `.env`.

Expected duration: 3-5 minutes.

### 3.4 Capture the outputs

```bash
terraform -chdir=terraform output -raw gateway_endpoint
# https://<GATEWAY_ID>.gateway.bedrock-agentcore.<REGION>.amazonaws.com/mcp
```

The gateway ARN follows the pattern:

```
arn:aws:bedrock-agentcore:<REGION>:<ACCOUNT_ID>:gateway/<GATEWAY_ID>
```

Confirm it in the Bedrock AgentCore console under Gateways if `terraform output gateway_arn` is not available.

---

## 4. Resources created

| Category | Resource | Name | Purpose |
|---|---|---|---|
| State backend (CloudFormation) | S3 bucket | `<PREFIX>-tf-state-<ENV>` | Terraform state |
| | DynamoDB table | `<PREFIX>-tf-lock-<ENV>` | State locking |
| MCP server | AgentCore Gateway | `<PREFIX>-gateway` | MCP endpoint, `AWS_IAM` authorizer |
| | Gateway Target | `lambda-runtime` | Exposes 8 tools |
| | IAM role + policy | `<PREFIX>-agentcore-gateway-role` | Gateway invokes the Lambda |
| | Lambda function | `<PREFIX>-lambda-runtime-tool` | Python 3.12 tool implementation |
| | IAM role + policy | `<PREFIX>-lambda-runtime-tool-role` | Read-only Lambda/EC2/CloudWatch access |
| | CloudWatch Log Group | `/aws/lambda/<PREFIX>-lambda-runtime-tool` | Tool logs |
| Platform (always created) | KMS CMK + alias | `alias/<PREFIX>-platform` | Log encryption |
| | Bedrock Guardrail + version | `<PREFIX>-<ENV>-guardrail` | Created but unused in this mode |
| | SSM parameters | `/<PREFIX>/<ENV>/config/*` | Shared configuration |

Not created: Cognito, AgentCore Runtime, ECR, AgentCore Memory, application DynamoDB tables, CloudFront, S3 frontend bucket, API Gateway, collector pipelines, VPC.

Idle cost is approximately $1/month (KMS key). Everything else is pay-per-invocation.

### Tools exposed by the gateway

Gateway tool names are prefixed with the target name and three underscores.

| Tool | Purpose |
|---|---|
| `lambda-runtime___generate_upgrade_analysis` | One-shot: discover regions, scan, download code, detect breaking changes, return `report_markdown` |
| `lambda-runtime___discover_lambda_regions` | Regions with functions plus deprecated/EOL counts |
| `lambda-runtime___get_deprecated_functions_multi_region` | Parallel multi-region deprecated scan |
| `lambda-runtime___get_deprecated_functions` | Single-region deprecated scan |
| `lambda-runtime___list_functions_by_runtime` | Filtered function list |
| `lambda-runtime___get_function_configuration` | Runtime, handler, layers, memory, architecture |
| `lambda-runtime___get_function_code` | Source files returned inline as text |
| `lambda-runtime___get_runtime_support_status` | Deprecation/EOL dates and upgrade targets |

All tools are read-only. The Lambda only calls `List*` and `Get*` APIs.

---

## 5. Create the IAM role for AWS DevOps Agent

AWS DevOps Agent assumes a role in `<ACCOUNT_ID>` to SigV4-sign requests to the gateway.

### 5.1 Trust policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "aidevops.amazonaws.com"
      },
      "Action": [
        "sts:AssumeRole"
      ],
      "Condition": {
        "StringEquals": {
          "aws:SourceAccount": "<ACCOUNT_ID>"
        }
      }
    }
  ]
}
```

`aws:SourceAccount` must be the account that owns the Agent Space. If the Agent Space lives in a different account than the gateway, use that account ID here; the role itself can still live in the gateway account.

> Note: the public documentation also shows an `ArnLike` condition on `aws:SourceArn` (`arn:aws:aidevops:<REGION>:<ACCOUNT_ID>:service/*`). In testing, including that condition caused registration to fail with `Invalid STS role configuration for session sigv4McpServerSession. Verify the role's trust policy.` Removing the `SourceArn` condition and keeping only `SourceAccount` resolved it.

### 5.2 Permissions policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "bedrock-agentcore:InvokeGateway",
      "Resource": "arn:aws:bedrock-agentcore:<REGION>:<ACCOUNT_ID>:gateway/<GATEWAY_ID>"
    }
  ]
}
```

### 5.3 Create the role (CLI alternative)

```bash
aws iam create-role \
  --role-name DevOpsAgent-CloudOpsGateway-Invoke \
  --assume-role-policy-document file://trust-policy.json

aws iam put-role-policy \
  --role-name DevOpsAgent-CloudOpsGateway-Invoke \
  --policy-name InvokeCloudOpsGateway \
  --policy-document file://permissions-policy.json
```

Wait about 60 seconds for IAM propagation before registering.

---

## 6. Register the MCP server in AWS DevOps Agent (account level)

In the AWS DevOps Agent console:

1. Open **Capability Providers** from the left navigation.
2. Under **Available**, find **MCP Server** and choose **Register**.
3. **MCP server details**:
   - Name: `cloudops-lambda-runtime`
   - Endpoint URL: `https://<GATEWAY_ID>.gateway.bedrock-agentcore.<REGION>.amazonaws.com/mcp`
   - Description (optional): `Lambda runtime upgrade discovery and migration analysis`
   - Leave **Enable Dynamic Client Registration** unchecked.
   - Leave **Connect to endpoint using private connection** unchecked.
   - Choose **Next**.
4. **Authorization flow**: select **AWS SigV4**, choose **Next**.
5. **Authorization configuration**:
   - IAM role: **Use an existing role**, select `DevOpsAgent-CloudOpsGateway-Invoke`
   - AWS Region: `<REGION>`
   - Service Name: `bedrock-agentcore`
   - Custom Headers: none
   - Choose **Next**.
6. **Review and submit**. DevOps Agent assumes the role and calls `tools/list` against the gateway to validate.

Registration succeeds when the server appears under **Registered** capability providers.

---

## 7. Add the MCP server to an Agent Space

1. Open the target **Agent Space**.
2. Go to the **Capabilities** tab.
3. In **MCP Servers**, choose **Add**.
4. Select `cloudops-lambda-runtime`.
5. Tool selection:
   - If the gateway was deployed with `DEPLOY_TOOLS=lambda-runtime`, choose **Allow all tools** (only the 8 Lambda runtime tools exist).
   - If the gateway exposes other targets as well, choose **Select specific tools** and allowlist only the `lambda-runtime___*` tools listed in section 4.
6. Leave tools classified as read-only.
7. Choose **Add**.

---

## 8. (Optional) Add the skill

Add `skills/lambda-runtime-upgrade/SKILL.md` from the repository as a skill in the Agent Space. The skill instructs the agent to:

- Ask which regions to scan before running heavy work.
- Prefer `generate_upgrade_analysis` as a single call.
- Read source from the tool response's `source_files` field rather than attempting to download presigned URLs.
- Emit the `report_markdown` verbatim and append a migration playbook and rollback plan.

The skill also includes a hard rule: if the `lambda-runtime` tools are absent from the tool list, the agent must say so rather than improvise with native `lambda:GetFunction` calls, which only return a presigned URL the agent cannot open.

---

## 9. Test

In a DevOps Agent chat within the Agent Space:

1. Sanity check (no AWS scan, instant):
   `Call get_runtime_support_status from cloudops-lambda-runtime.`
2. Full report:
   `Find Lambda functions on deprecated runtimes in <REGION> and generate a migration report.`
   Expected: one call to `lambda-runtime___generate_upgrade_analysis`, followed by a report with executive summary, per-function tables, and file/line breaking-change findings.
3. Source retrieval:
   `Show me the source code for <function-name> in <REGION>.`
   Expected: `lambda-runtime___get_function_code` returns file contents inline.

The agent should never mention shells, `curl`, `unzip`, or presigned URLs. If it does, the MCP tools are not in its tool list; recheck section 7.

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Invalid STS role configuration for session sigv4McpServerSession. Verify the role's trust policy.` | Trust policy conditions do not match the assume-role request | Use the trust policy in 5.1 with only `aws:SourceAccount`. Confirm the account ID is the Agent Space owner. Wait 60s after edits. |
| Registration validates but `tools/list` returns 403 | Wrong SigV4 service name or missing `InvokeGateway` | Service Name must be `bedrock-agentcore`. Verify the permissions policy resource ARN matches the gateway. |
| Registration fails with connection error | Wrong endpoint or region | Endpoint must end in `/mcp`. Region in the SigV4 config must match the gateway's region. |
| Agent says it cannot download or unzip source | MCP tools not allowlisted in the Agent Space | Complete section 7. Confirm `lambda-runtime___get_function_code` appears in the Agent Space tool list. |
| `get_function_code` returns empty files | Function uses container-image packaging | Expected. Code is not downloadable via `GetFunction`; the agent gives generic guidance. |
| Report covers the wrong account | Tool Lambda runs under its own execution role | See section 11 for cross-account scanning. |

To inspect a denied assume-role call:

```bash
aws cloudtrail lookup-events --region <REGION> \
  --lookup-attributes AttributeKey=EventName,AttributeValue=AssumeRole \
  --start-time "$(date -u -v-30M +%Y-%m-%dT%H:%M:%SZ)" \
  --query "Events[?contains(CloudTrailEvent,'sigv4McpServerSession')].CloudTrailEvent" \
  --output text
```

---

## 11. Scope of what gets scanned

The tool Lambda executes under `<PREFIX>-lambda-runtime-tool-role` in `<ACCOUNT_ID>`. It scans that account's Lambda functions regardless of which account the Agent Space is investigating.

To scan other accounts:

1. In each target account, create a role with `lambda:ListFunctions`, `lambda:GetFunction`, `lambda:GetFunctionConfiguration`, `ec2:DescribeRegions`, trusted by `<PREFIX>-lambda-runtime-tool-role`.
2. Re-run `make configure` and set `CROSS_ACCOUNT_ROLE_ARN` to the target role ARN.
3. Redeploy with the same `DEPLOY_MODE=gateway-only DEPLOY_TOOLS=lambda-runtime make deploy-auto`.

---

## 12. Teardown

Remove the MCP server from the Agent Space, then deregister it from Capability Providers. Then in the repository:

```bash
make destroy-all
```

This removes the gateway, Lambda, IAM roles, KMS key (30-day deletion window), guardrail, SSM parameters, and the Terraform state backend. Delete the `DevOpsAgent-CloudOpsGateway-Invoke` IAM role separately.

---

## Appendix: Command summary

```bash
# Deploy
make setup                      # then edit .env
make configure
DEPLOY_MODE=gateway-only DEPLOY_TOOLS=lambda-runtime make deploy-auto
terraform -chdir=terraform output -raw gateway_endpoint

# IAM role for DevOps Agent
aws iam create-role --role-name DevOpsAgent-CloudOpsGateway-Invoke \
  --assume-role-policy-document file://trust-policy.json
aws iam put-role-policy --role-name DevOpsAgent-CloudOpsGateway-Invoke \
  --policy-name InvokeCloudOpsGateway --policy-document file://permissions-policy.json

# Teardown
make destroy-all
```
