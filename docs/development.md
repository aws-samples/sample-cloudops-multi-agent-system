# Development Guide

This file documents project-specific conventions, architecture, and gotchas for anyone working with code in this repository.

## Project

CloudOps Multi-Agent System — a hierarchical multi-agent system for AWS cloud operations built on Amazon Bedrock AgentCore with Strands Agents SDK and an AG-UI streaming Next.js frontend. See `README.md` for architecture diagrams and `skills/developer-guide/SKILL.md` for step-by-step how-tos. `docs/agents/` has one reference file per leaf agent (deploy modes, data model, gotchas) — `health-events.md` and `tag-governance.md` today; add a new file here for any new leaf with non-trivial deploy or operational surface.

The richest source of project-specific conventions and gotchas is this file plus the `docs/` directory — treat them as authoritative. `docs/architecture.md` covers agent topology decisions.

## Commands

All build/deploy/test operations **must** go through Makefile targets. Do NOT run standalone `npm run build`, `finch build`, `aws s3 sync`, or `terraform apply` directly — the Makefile and `scripts/deploy.sh` orchestrate ordering, hash-based skip logic, and post-apply sync steps that standalone commands bypass.

```bash
make setup                                 # Interactive identity setup (writes .env) + install deps
make configure                             # First-run shared project config (writes to SSM)
make reconfigure-shared                    # Change shared config with diff + APPLY CHANGES gate
make deploy-auto                           # Non-interactive full deploy (packages lambdas, builds, applies)
make plan                                  # terraform plan only
make destroy                               # terraform destroy (infra only)
make destroy-all                           # infra + ECR + memory + state backend + log groups
make package                               # Package Lambda tools (hash-based, parallel)
make build-agents                          # Build agent container images only
make test                                  # All unit tests
make test-unit                             # Unit tests only
make test-integration                      # Integration tests (needs deployed stack + Cognito creds)
make run-local                             # Local Next.js dev server with Cognito auth
make clean                                 # Wipe build artifacts, caches, zips, hashes
```

Run a single test: `.venv/bin/pytest tests/unit/test_reports.py::test_name -v`. All Python commands must use `.venv/bin/` — never system Python.

Prerequisites: AWS CLI, Terraform, Finch (`finch vm start` before deploy), Node.js, Python 3.12.

**Two-tier config.** `.env` at project root holds only per-developer identity: `AWS_PROFILE`, `PROJECT_PREFIX`, `ENVIRONMENT`. Everything else — `AWS_REGION`, `IDP_TYPE`, cross-account role ARNs, CUR settings, `DEPLOY_MODE`, `DEPLOY_TOOLS`, `GATEWAY_AUTH` — lives in SSM Parameter Store under `/$PROJECT_PREFIX/$ENVIRONMENT/config/*`, managed by the Terraform module at `terraform/modules/core/shared-config/`. `make configure` writes both the tfvars JSON (auto-loaded by Terraform) and SSM in one targeted apply. Env-var overrides beat SSM for single-invocation deploys (e.g. `DEPLOY_TOOLS=cost-explorer make deploy-auto`).

## Architecture (Big Picture)

**Config-driven agents.** `src/agents/hierarchy.json` is the single source of truth for the entire agent tree — prompts, models, `type`, `protocol`, `children`, `tools`. Three generic agent folders replace per-agent boilerplate:

- `src/agents/frontend/` — user-facing AG-UI entry point (streaming, memory, suggestions, reports)
- `src/agents/orchestrator/` — mid-level delegator (routes to ONE child per request)
- `src/agents/worker/` — leaf agent (calls gateway MCP tools)

Each generic `server.py` reads `AGENT_NAME` from env and loads config from `hierarchy.json`, then calls the matching factory in `src/agents/shared/agent_base.py` (`create_frontend_agent` / `create_mid_level_agent` / `create_leaf_agent`). Adding a new agent = add a JSON entry + `make deploy-auto`. Any agent's `type` can be flipped to `"frontend"` to make it the user-facing entry point (standalone deployment).

**Runtime topology.** Each agent is a container on AgentCore Runtime (port 8080). Agent-to-agent calls use `boto3.client('bedrock-agentcore').invoke_agent_runtime()` (SigV4-signed) — NOT Strands `A2AAgent`. Sub-agents are **stateless**: the supervisor resolves ambiguous references ("this", "last month") from memory before delegating. Memory lives on the supervisor only, managed manually via `create_event`/`list_events` in `shared/memory.py` — `AgentCoreMemorySessionManager` is NOT used because `ag_ui_strands.StrandsAgent` discards `session_manager`/`messages`/`callback_handler` and creates its own internal agents per thread (injected via `_agents_by_thread[thread_id]`).

**Runtime protocol.** The frontend/supervisor runtime serves the AG-UI streaming protocol; every other agent runtime serves HTTP. Both are set by Terraform via `protocol_configuration { server_protocol = ... }` — AGUI is a native provider enum as of AWS provider v6.50.0 (pinned in `terraform/versions.tf`), so there's no post-deploy protocol dance. The `protocol` field in `hierarchy.json` is unrelated to the runtime protocol (nothing reads it); leave it `"http"`.

If you see silent 424 errors with no useful logs: check for a protocol mismatch on the runtime.

**Tool layer.** Lambda MCP tools live under `src/lambda/mcp/<tool>/` (one per AWS API surface). Schemas are in `src/lambda/mcp/tools.json`. Tools are registered as targets on a single AgentCore Gateway. Gateway targets reset to single-tool placeholder schemas on every `terraform apply`, so `deploy.sh` deletes `.lambda-hashes/gateway-tools.sha` immediately after apply to force `sync_gateway_tools` to re-upload the full multi-tool schemas.

Lambda handler routing: `tool_name = context.client_context.custom["bedrockAgentCoreToolName"].split("___")[1]`. The event body contains tool params directly — NOT wrapped.

**Frontend CRUD vs chat split.** The supervisor runtime only handles live AG-UI chat and legacy `{prompt}` sync chat. Browser REST operations go through a separate API Gateway HTTP API. Each feature gets its own Lambda under `src/lambda/frontend/<feature>/`; currently: `core-api/` (sessions/templates/reports CRUD, agentic-platform features) and `network-resilience/` (tier-toggle reassess, live BGP polling — Phase 4). History loading uses `GET /sessions/{id}/history` on the core-api Lambda — NOT the supervisor runtime. The `actor_id` is extracted from the JWT `email` claim (sanitized: `@` → `_at_`, `.` → `_`) — if this sanitization diverges from what the supervisor saves, sessions won't list.

**Tool call tracing across containers.** Sub-agent tool calls happen in separate runtime containers, so the parent's AG-UI stream can't see them natively. Mechanism: each agent uses `TracingCallbackHandler` as its Strands `callback_handler`; tool functions call `handler.complete_tool()` directly (because Strands `ToolResultEvent` has `is_callback_event=False`); `build_traced_response()` returns a dict with `tool_trace`; the parent's `_delegate` wrapper forwards nested traces; the supervisor's enriched memory save embeds them as `<tool>` tags. Trace output is truncated via `_smart_truncate()` (default 3000 chars) to preserve JSON validity — NEVER use naive `[:N]` slicing.

**Reports.** When AG-UI request has `forwardedProps.template_id`, `agui_server.py` switches to report mode. Sections run in `ThreadPoolExecutor` + `as_completed` inline in the SSE generator; independents run parallel, dependents wait on prerequisites. Progress → `REASONING_MESSAGE_CONTENT` (thinking card), section body → `TEXT_MESSAGE_CONTENT` (report panel). Memory save format: `<tool>` tags (OUTSIDE `<report-body>`) + `<report-body>` + `<artifact>`. Reports and templates share a DynamoDB table; reports partition by `report:{actor_id}`, templates by raw `actor_id`, built-in templates by `"system"`.

## Build Hash / Selective Rebuilds

`.lambda-hashes/<name>.sha` tracks what's been built. Hashes are **type-aware** (`scripts/lib/build.sh` + `hierarchy.sh`):
- Frontend agents hash `agent_dir + src/agents/shared/ + src/agents/hierarchy.json` (everything)
- Orchestrator/worker agents exclude frontend-only files (`agui_server.py`, `reports.py`, `memory.py`, `suggestions.py`, `report_templates/`)

`hierarchy.json` is SLICED per-agent at build time — `scripts/lib/build.sh::_write_agent_hierarchy_slice` writes `src/agents/.hierarchy-<agent>.json` (gitignored) containing only that agent's own entry, and each Dockerfile COPYs the slice via the `AGENT_HIERARCHY_PATH` build-arg. A prompt-only change to one agent rebuilds ONLY that agent (~13 min saved on prompt tweaks). **Inside a container, `hierarchy.json` holds exactly one key — never read `hierarchy[sibling_name]` at runtime; use `shared/registry.py::load_agent_registry` instead.** Changing `dir` fields does NOT auto-invalidate; you must `rm .lambda-hashes/*.sha` after such changes. Builds run in parallel batches with `MAX_PARALLEL_BUILDS` auto-detected from CPU count (cores/2, clamped 1–5) by `_detect_max_parallel_builds` in `scripts/lib/config.sh`. Override via env var only when tuning for a specific machine. To force a single agent rebuild: `rm .lambda-hashes/<agent-name>.sha`.

## High-Signal Gotchas

These are load-bearing and not obvious from reading the code:

- **MCP import name collision**: use `streamablehttp_client` (no underscores) from `mcp.client.streamable_http` for SigV4 `auth=` support. The similarly-named `streamable_http_client` silently drops `auth=`, tools fail to load, and the model hallucinates fake `<function_calls>` XML with fabricated data.
- **AgentCore Gateway paginates `tools/list` at 30 per page.** `MCPClient.list_tools_sync()` returns a `PaginatedList` — `len()` is the current page only, and the cursor is exposed as `.pagination_token`. Any target whose tools land on page 2+ is invisible unless you drain the cursor. `agent_base.py::load_gateway_tools` drains pages in a loop; do NOT revert that to a single call. Symptom when skipped: filter reports `Filtered 0/30 gateway tools` even though the gateway has the target READY with correct inline schemas, model has zero tools, platform no-fabrication preamble fires "no tools available" or model fabricates plausible numbers.
- **Hallucination guardrail is platform-level, not per-prompt.** `agent_base.py` prepends a non-negotiable `_NO_FABRICATION_PREAMBLE` to every agent's system prompt AND refuses to invoke a leaf with zero tools (returns a clear error instead of letting the model improvise). Agent authors do not opt in; the factories apply this unconditionally. If you add a new agent factory, call `_apply_platform_preamble(_inject_tool_inventory(prompt, tools))` — do not bypass.
- **Bedrock Guardrail (standalone ApplyGuardrail API).** NOT attached to the model (which caused false positives on system prompts). Instead, `shared/guardrail.py::check_user_input()` calls `bedrock-runtime:ApplyGuardrail` on ONLY the raw user message at the supervisor entry point (in `agui_server.py`, BEFORE the agent is built/run). Prompt attack detection + sensitive info filters + topic policy. System prompts never reach the classifier (they're assembled later in `agent_base.py` and aren't user-modifiable). Env vars: `BEDROCK_GUARDRAIL_ID`, `BEDROCK_GUARDRAIL_VERSION`, `GUARDRAIL_MODE` (supervisor-only). `GUARDRAIL_MODE` is `block` (default — refuse flagged input) or `detect` (log-only, non-blocking; set via root tfvar `guardrail_mode`). Layered with: (a) `_NO_FABRICATION_PREAMBLE` behavioral constraints, (b) IAM least-privilege per-tool (hard backstop), (c) `shared/redact.py` strips genuine secrets (access keys, external IDs, role-session names) from persisted data (memory + reports); account IDs and ARNs are identifiers kept by default, scrubbed only when `REDACT_IDENTIFIERS=true`. **The CMK on DynamoDB requires every agent role to have `kms:Decrypt` on the platform key — without it, registry reads fail closed and the agent reports "no child agents deployed".**
- **MCPClient lifecycle in leaf agents**: manual `__enter__()` → `list_tools_sync()` → pass tools (not the client) to `Agent(tools=tools)` → `__exit__` in `finally`. Passing `MCPClient` directly to `Agent` causes `"client failed to initialize"` in Runtime containers.
- **`MCPAgentTool` attribute**: use `tool.tool_name` (NOT `tool.name`) when filtering by `tools: [...]` in `hierarchy.json`. Wrong attribute = all tools silently excluded = model hallucinates.
- **`update_agent_runtime` is NOT partial**: omitting `authorizerConfiguration` strips JWT. `environmentVariables` is replaced entirely — `deploy.sh` uses `{**current_env, **desired_updates}` merge to avoid stripping Terraform-managed vars like `REPORT_TABLE_NAME`.
- **`invoke_agent_runtime` response key** is `response` (blob), NOT `payload`. Terraform outputs `agent_runtime_ids` as **names**, not ARNs — call `get_agent_runtime(agentRuntimeId=name)` first to get the ARN.
- **Cognito ID tokens**: in the `custom_jwt_authorizer` block, set ONLY `allowed_audience` — NOT `allowed_clients`. ID tokens have client ID in `aud`, not `client_id`; setting both fails.
- **`AWS_REGION` + `AWS_DEFAULT_REGION`** must be explicitly set on Runtimes (AgentCore doesn't inject them). But do NOT set `AWS_REGION` in Lambda `environment.variables` — it's reserved and Lambda injects it automatically; setting it fails `CreateFunction`.
- **`MemoryClient(region_name=...)`** must be passed explicitly — it defaults to `us-west-2`, not `AWS_REGION`.
- **Strands tool threading**: `@tool` functions run in a `ThreadPoolExecutor`. Thread-locals don't propagate from the entrypoint. Use a module-level variable for the handler (safe because Runtime processes one request at a time per container).
- **Tool output unescape**: tool output strings pick up up to 3 layers of JSON escaping (leaf → orchestrator → supervisor → memory). Frontend `TracePanel`/`ReportPanel` and `_normalizeToolInfo` loop `JSON.parse` up to 3 times until non-string, then pretty-print. Single-level parse shows `{\"key\":\"value\"}`.
- **Supervisor-only env vars**: `agent-runtime-base/main.tf` (sub-agents) and `agentcore-runtime/main.tf` (supervisor) are SEPARATE modules. New env vars must be added to BOTH or the supervisor crashes on startup with no useful error (`AGENT_NAME` required at import time).
- **`filebase64sha256` in Terraform** evaluates during plan/destroy, not just apply — always `make package` before ANY terraform operation including destroy, or destroy fails with missing zip.
- **Agent registry cleanup**: renaming an agent in `hierarchy.json` leaves the old DynamoDB registry entry behind (Terraform sees it as a new resource, not a rename). `sync_agent_registry()` in `sync.sh` scans and deletes orphans at the start of `post_deploy_sync()` — don't try to do this via Terraform `when = destroy` provisioner (breaks on trigger-map changes).
- **Models**: supervisor uses `global.anthropic.claude-opus-4-6-v1`, sub-agents use `global.anthropic.claude-sonnet-4-6-v1`. `us.anthropic.claude-3-7-sonnet-20250219-v1:0` is legacy and inaccessible.

## Prompt Design Rules

These three rules prevent the most common behavioral regressions:

- **Orchestrators**: "pick ONE agent per request", "NEVER ask clarifying questions", "pass the user's question through as-is". Orchestrators are routers, not conversationalists.
- **Workers**: gate each tool on the user asking for it. The single most effective constraint is literally: "Do NOT proactively run extra queries."
- **Supervisor**: resolve ambiguous references ("this", "last month") from memory BEFORE delegating — sub-agents are stateless.

`_inject_date()` in `shared/prompt.py` auto-injects today's date into every agent's prompt.

## Deploy Modes

`DEPLOY_MODE` gates build phases via `DEPLOY_FLAG_*` flags and maps to Terraform `deploy_*` vars. Modes: `full` (default), `agents-only` (skips Next.js/S3/CloudFront), `gateway-only`, `tools-only`. `DEPLOY_TOOLS` filters Lambda tools by `tools.json` keys. `GATEWAY_AUTH` is `iam` (default) or `oauth` (adds Cognito JWT authorizer for external clients).

**Pass `DEPLOY_MODE` as a command-line override, NOT in `.env`** — e.g. `DEPLOY_MODE=gateway-only make deploy-auto`. `DEPLOY_MODE` (like `DEPLOY_TOOLS`, `GATEWAY_AUTH`, `AWS_REGION`) is a **shared key** (`scripts/shared-keys.txt`): `deploy.sh` captures CLI-set values before sourcing `.env`, then **unsets any shared key that came from `.env`** so stale `.env` values never win for SSM-owned config (see the restore-or-strip loop in `deploy.sh`). A `DEPLOY_MODE=…` line in `.env` is therefore silently ignored and the deploy runs `full`.

## Observability

Runtime → X-Ray trace delivery and runtime → CWL application-log delivery are wired in `terraform/modules/core/observability/main.tf`. Spans land in `aws/spans`; application logs vend to `/aws/vendedlogs/bedrock-agentcore/runtime/{project}`. Runtime execution roles (both `agent-runtime-base` and `agentcore-runtime`) grant four X-Ray actions — `PutTraceSegments`, `PutTelemetryRecords`, `GetSamplingRules`, `GetSamplingTargets`. The two `Get*` are load-bearing: ADOT's sampler calls them every ~10s; without them spans get silently dropped.

But **X-Ray sampling + Transaction Search indexing rules are account-wide and not managed by Terraform.** AWS defaults (5% × 1% = 0.05% effective) are too low for session-level tracing to land reliably. See `docs/observability-tuning.md` for the exact CLI commands to set both to 100% and the cost rationale.

## Debugging

- CloudWatch logs: each runtime has TWO log groups — `...-DEFAULT` (request/response pairs, `null` = 424 crash) and `...-<endpoint>` (`otel-rt-logs` stream with exception type + message). Check `otel-rt-logs` first for 424s.
