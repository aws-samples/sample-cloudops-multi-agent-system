# Lambda Runtime Upgrade skill: MCP server deployment

This folder is the deployment asset for the
[`lambda-runtime-upgrade`](../SKILL.md) skill. The skill is the playbook. This
folder deploys the tools it calls, which a hosted agent like AWS DevOps Agent needs
because it has no shell to scan regions or unzip code itself.

A standalone CloudFormation stack that deploys only the MCP server behind the
`/lambda-runtime-upgrade` skill and the `lambda-upgrade-agent`: an Amazon
Bedrock AgentCore Gateway (SigV4 / `AWS_IAM` auth) plus one read-only tool
Lambda. You don't need Terraform, agent containers, a frontend, or Cognito.

Use this path when you want to plug the Lambda runtime tools into
[AWS DevOps Agent](#connect-to-aws-devops-agent) or another MCP client, without
deploying the full platform. For the Terraform path, see
`DEPLOY_MODE=gateway-only DEPLOY_TOOLS=lambda-runtime make deploy-auto` in the
[runbook](../../../docs/runbooks/lambda-runtime-mcp-devops-agent.md).

## Files

Everything needed to deploy is in this folder. You don't need to build anything.

| File | Purpose |
|---|---|
| `lambda-runtime-mcp.yaml` | CloudFormation template. Generated, do not hand-edit. |
| `lambda-runtime-tool.zip` | Lambda deployment package: `handler.py` plus `shared/cross_account.py`. No third-party dependencies (only `boto3`, which the Lambda runtime provides). Generated, do not hand-edit. |
| `deployer-permissions.json` | Least-privilege IAM policy for the person or role creating the stack. Assumes the default `lambda-upgrade` prefix. |

You can download just this folder from GitHub, or copy it out of a clone, and hand
it to someone who only has AWS Console access.

### For maintainers: regenerating

The YAML and zip are generated from `src/lambda/mcp/tools.json` and
`src/lambda/mcp/lambda-runtime/`. After changing either, run:

```bash
make cfn-mcp-package   # rewrites lambda-runtime-mcp.yaml + lambda-runtime-tool.zip here
make cfn-mcp-check     # exits 1 if the checked-in files are stale
```

Builds are byte-reproducible (fixed zip timestamps and sorted entries), so
regenerating without source changes produces no git diff.
`tests/unit/test_cfn_mcp_package.py` runs the same check in `make test`, so a
stale zip or template fails CI.

## What gets created

| Resource | Name | Notes |
|---|---|---|
| AgentCore Gateway | `<Prefix>-gateway` | MCP protocol, `AWS_IAM` authorizer. Every call must be SigV4-signed by a principal with `bedrock-agentcore:InvokeGateway`. |
| Gateway target | `lambda-runtime` | Inline schema for all 8 tools, so you don't need a post-deploy sync. |
| Lambda function | `<Prefix>-lambda-runtime-tool` | Python 3.12, 512 MB, 120 s. |
| IAM role | `<Prefix>-lambda-runtime-tool-role` | `lambda:ListFunctions`, `lambda:GetFunction`, `lambda:GetFunctionConfiguration`, `ec2:DescribeRegions`, `cloudwatch:GetMetricData`. Read-only. |
| IAM role | `<Prefix>-agentcore-gateway-role` | Lets the gateway invoke only the tool Lambda. |
| Log group | `/aws/lambda/<Prefix>-lambda-runtime-tool` | Configurable retention. |
| IAM role (optional) | `<Prefix>-devops-agent-gateway-invoke` | Trusted by `aidevops.amazonaws.com`, scoped by `aws:SourceAccount`, allowed only `InvokeGateway` on this gateway. |

Idle cost is close to zero. You pay only for Lambda invocations and CloudWatch Logs.

### Parameters

| Parameter | Default | Description |
|---|---|---|
| `CodeBucket` | (required) | S3 bucket holding `lambda-runtime-tool.zip`, same region as the stack. |
| `CodeKey` | `lambda-runtime-tool.zip` | Object key of the zip. |
| `Prefix` | `lambda-upgrade` | Name prefix for every resource. |
| `LogRetentionDays` | `30` | Tool log retention. |
| `CrossAccountRoleArn` | empty | Optional role in another account for the tool to assume. |
| `CrossAccountExternalId` | empty | Optional external ID for that role. |
| `CreateDevOpsAgentRole` | `true` | Create the IAM role AWS DevOps Agent assumes. |
| `DevOpsAgentAccountId` | empty (this account) | Account that owns the DevOps Agent Space. |

## Deploy

### Option A: AWS CLI

Run from the repository root:

```bash
REGION=us-east-1
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET=lambda-upgrade-artifacts-${ACCOUNT_ID}

aws s3 mb "s3://${BUCKET}" --region "${REGION}"
aws s3 cp skills/lambda-runtime-upgrade/mcp-server/lambda-runtime-tool.zip "s3://${BUCKET}/lambda-runtime-tool.zip"

aws cloudformation deploy \
  --region "${REGION}" \
  --stack-name lambda-runtime-mcp \
  --template-file skills/lambda-runtime-upgrade/mcp-server/lambda-runtime-mcp.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides CodeBucket="${BUCKET}" CreateDevOpsAgentRole=true

aws cloudformation describe-stacks --region "${REGION}" --stack-name lambda-runtime-mcp \
  --query "Stacks[0].Outputs" --output table
```

New S3 buckets have Block Public Access on and SSE-S3 encryption by default.
Leave both as they are. The bucket only has to be readable by CloudFormation and Lambda in your account.

### Option B: AWS Console only

1. S3 console: create a bucket named `lambda-upgrade-artifacts-<account-id>` in
   the target region (defaults are fine), then upload `lambda-runtime-tool.zip`.
2. CloudFormation console (same region): **Create stack**, then **With new resources**.
   Upload `lambda-runtime-mcp.yaml`.
3. Stack name `lambda-runtime-mcp`. Set `CodeBucket` to the bucket from step 1.
   Leave the other parameters at their defaults unless the Agent Space is in a different account.
4. Acknowledge IAM resource creation, then submit. The stack reaches
   `CREATE_COMPLETE` in 2-4 minutes.

### Outputs

| Output | Use |
|---|---|
| `GatewayEndpointUrl` | MCP endpoint to register in DevOps Agent or an IDE. |
| `GatewayArn` / `GatewayId` | Reference for IAM policies and troubleshooting. |
| `DevOpsAgentRoleArn` | Role to select when registering with SigV4 auth. |
| `SigV4ServiceName` | Always `bedrock-agentcore`. |
| `ToolsExposed` | The 8 tool names. |

## Connect to AWS DevOps Agent

1. DevOps Agent console: **Capability Providers**, then **MCP Server**, then **Register**.
   - Name: `cloudops-lambda-runtime`
   - Endpoint URL: `GatewayEndpointUrl`
   - Leave Dynamic Client Registration and private connection unchecked.
2. Authorization flow: **AWS SigV4**.
3. Authorization configuration:
   - IAM role: **Use an existing role**, then pick `DevOpsAgentRoleArn`
   - Region: the stack's region
   - Service name: `bedrock-agentcore`
4. Review and submit. DevOps Agent assumes the role and calls `tools/list` to validate.
5. Open your **Agent Space**, then **Capabilities**, then **MCP Servers**, then **Add**. Select
   `cloudops-lambda-runtime` and **Allow all tools**. All 8 tools are read-only.
6. Add the skill itself to the Agent Space: [`../SKILL.md`](../SKILL.md) and
   [`../reference/runtime-migration-kb.md`](../reference/runtime-migration-kb.md).
   With it, the agent prefers the one-shot `generate_upgrade_analysis` call and
   reads source from the tool response. You don't need to upload this
   `mcp-server/` folder to the Agent Space; it's only for deploying.

Test with: `Find Lambda functions on deprecated runtimes in us-east-1 and generate a migration report.`

If registration fails with `Invalid STS role configuration for session
sigv4McpServerSession`, confirm `DevOpsAgentAccountId` is the account that owns
the Agent Space. Then wait about 60 seconds for IAM propagation and retry. For more
failure modes, see the troubleshooting table in the
[runbook](../../../docs/runbooks/lambda-runtime-mcp-devops-agent.md#10-troubleshooting).

## Scanning other accounts

The tool scans the account the stack is deployed in. To scan another account,
create a role there with the five read-only permissions listed above. Make it trusted by
`arn:aws:iam::<this-account>:role/<Prefix>-lambda-runtime-tool-role`, then
update the stack with `CrossAccountRoleArn` (and `CrossAccountExternalId` if
the role requires one).

## Remove

Remove the MCP server from the Agent Space and deregister it from Capability
Providers first. Then delete the stack and the artifact bucket:

```bash
aws cloudformation delete-stack --region "${REGION}" --stack-name lambda-runtime-mcp
aws s3 rb "s3://${BUCKET}" --force
```
