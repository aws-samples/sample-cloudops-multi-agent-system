# Terraform

Provider `aws` v6.35+ required. `default_tags` applies `auto-delete = "no"` (Isengard), `project`, `environment`, `managed_by` to every resource.

Modules live under `modules/core/` (platform) and `modules/custom/` (optional add-ons like `health-events-collection`, `network-resilience-api`).

## Run everything through `make`, not raw `terraform`

- `make plan` / `make deploy-auto` / `make destroy` / `make destroy-all` wrap `scripts/deploy.sh`, which handles: tfvars generation from `hierarchy.json` + `tools.json`, state backend bootstrap, post-apply syncs (runtime env, gateway tools, registry cleanup), and retry-safe teardown.
- Raw `terraform apply` skips all of that and leaves you with half-deployed state, stale gateway schemas, and runtime env-var drift.

## Runtime protocol

The supervisor runtime serves the AG-UI streaming protocol; all other agent runtimes serve HTTP. Both are set by Terraform via `protocol_configuration { server_protocol = ... }` — AGUI became a native `server_protocol` enum in AWS provider v6.50.0 (`versions.tf` pins `>= 6.50.0`), so no post-deploy protocol adjustment is needed.

The `protocol` field in `hierarchy.json` is unrelated to the runtime protocol — no code reads it — so it stays `"http"` for every agent, including the supervisor.

## Modules you'll touch most

- `agent-runtime-base` — per-agent runtime via `for_each` over `hierarchy.json`. Used for sub-agents.
- `agentcore-runtime` — **separate** module for the supervisor (frontend agent). Has its own env vars, JWT authorizer, `lifecycle { ignore_changes }` blocks.
  - **Gotcha**: when adding a new env var to runtimes, update BOTH modules. Missing this crashes the supervisor on startup with no useful CloudWatch error.
- `lambda-tool-base` — per-tool Lambda via `for_each` over `tools.json` (filtered by `selected_tools` variable).
- `memory` — native `aws_bedrockagentcore_memory` + `aws_bedrockagentcore_memory_strategy`. Memory ID flows to runtimes as `AGENTCORE_MEMORY_ID` env var.
- `guardrail` — Bedrock Guardrail (prompt attack + sensitive info + topic policy). Used via standalone `ApplyGuardrail` API on user input only (not model-level), avoiding false positives on system prompts.
- `kms` — Customer-managed KMS key for DynamoDB and CloudWatch Logs encryption with annual rotation.

## Runtime names must not have hyphens

AgentCore regex: `[a-zA-Z][a-zA-Z0-9_]{0,47}`. Use `replace("-", "_")` when deriving from agent names.

## Cognito JWT authorizer

In `custom_jwt_authorizer` block, set ONLY `allowed_audience` — NOT `allowed_clients`. Cognito ID tokens have client ID in `aud`, not `client_id`; setting both fails.

To update authorizer config, temporarily comment out `lifecycle { ignore_changes = [authorizer_configuration] }` on the supervisor runtime and `terraform taint` it.

## Gateway tool schemas

`aws_bedrockagentcore_gateway_target` supports ONE `inline_payload` per target. Multi-tool Lambdas get overwritten to the placeholder on every apply — `sync_gateway_tools` in `deploy.sh` re-uploads full schemas from `tools.json` after apply (by deleting `.lambda-hashes/gateway-tools.sha` to force re-run).

## Common Terraform traps

- **`jsondecode()` returns an `object`, not a `map`.** Causes `Inconsistent conditional result types` in `cond ? { for ... } : {}`. Fix: wrap in `{ for k, v in jsondecode(...) : k => v }` first, or embed the condition in the `for` filter.
- **`count` on derived ARNs.** `count = var.some_arn != "" ? 1 : 0` fails when the ARN is `(known after apply)`. Use a plan-time boolean variable instead (see `enable_runtime_tracing` / `enable_gateway_tracing` / `enable_memory_tracing` in `observability/`).
- **`filebase64sha256` on Lambda zips evaluates during plan AND destroy.** `make package` before ANY terraform op or destroy fails.
- **Deprecation warnings corrupt `-raw` output.** Terraform v1.10+ emits `dynamodb_table` deprecation warnings to stdout. Use the `tf_output` helper from `scripts/lib/common.sh` (strips warning blocks via `-no-color` + `sed`), never raw `terraform output -raw`.

## Teardown

`make destroy-all` does: terraform destroy → ECR repos → orphaned memory (poll until gone — `delete_memory` is async, ~60s) → AgentCore auto-created log groups → observability delivery pipelines → versioned S3 state bucket (must `object_versions.all().delete()` before `s3 rb`) → DynamoDB lock table → bootstrap CloudFormation stack.

Interrupted applies leave orphans. Next apply fails with `EntityAlreadyExists` / `ResourceInUseException`. Fix manually (IAM roles+policies, DynamoDB, log groups, memory, CloudFront OAC, delivery destinations) then retry. Use `terraform import` over delete+recreate where possible. If state is locked: `terraform force-unlock <lock-id>`.
