# Architecture — internals

Reference for engineers working on the platform itself. For "how do I add
an agent or tool?" questions, see `skills/developer-guide/SKILL.md`.

---

## Agent composition

### Config-driven agents

All agents are defined in `src/agents/hierarchy.json` — prompt, model,
type, children, allowed tools, capability flags. Three generic code
folders handle the three agent roles; there's no per-agent Python code:

```
src/agents/
  hierarchy.json             # Single source of truth
  frontend/                  # Generic AG-UI agent (user-facing)
  orchestrator/              # Generic mid-level agent (delegates to children)
  worker/                    # Generic leaf agent (calls gateway MCP tools)
  shared/
    agent_base.py            # create_frontend_agent / create_mid_level_agent / create_leaf_agent
    agui_server.py           # AG-UI FastAPI wrapper (streaming, memory, suggestions, reports)
    memory.py                # Manual memory management (create_event/list_events)
    suggestions.py           # Follow-up question generation
    reports.py               # Report generation engine (parallel sections, dependencies)
    redact.py                # Output redaction (secrets: keys/external-ids/sessions) before persistence
    tracing.py               # TracingCallbackHandler
    registry.py              # Agent registry + @tool delegation wrappers
    gateway.py               # SigV4 MCP gateway client
    prompt.py                # Dynamic system prompt builder + date injection
    agent_hierarchy.py       # hierarchy.json loader
    thread_activity.py       # Thread busy/idle state for multi-tab coordination
    session_title.py         # Auto-generated session titles via Haiku
    report_tool.py           # get_report @tool for inline report access
    aws_utils.py             # AWS utility helpers
    report_templates/        # Built-in report template JSONs
```

Each `server.py` reads `AGENT_NAME` from env, loads its config from
`hierarchy.json`, and calls the appropriate factory.

### Runtime topology

- Each agent runs on AgentCore Runtime (container on port 8080).
- Inter-agent calls use `bedrock-agentcore:InvokeAgentRuntime` (SigV4-signed
  HTTP) — NOT Strands `A2AAgent`.
- Supervisor uses AG-UI protocol; all other agents use HTTP.
- Sub-agents are **stateless**: the supervisor resolves ambiguous
  references ("this", "last month") from memory before delegating.

### Runtime protocol

The supervisor runtime serves the AG-UI streaming protocol; every other
agent runtime serves HTTP. Both are set by Terraform via
`protocol_configuration`. AGUI is a native `server_protocol` enum as of
AWS provider v6.50.0 (pinned in `terraform/versions.tf`), so there is no
post-deploy protocol adjustment.

The `protocol` field in `hierarchy.json` is unrelated to the runtime
protocol — no code reads it — so it stays `"http"` for every agent.

---

## Memory

Memory is supervisor-only, managed via `agents/shared/memory.py`. The
AG-UI server (`agui_server.py`) handles the lifecycle:

- **Load**: `list_events()` → convert to Strands message format → pass
  as `messages` kwarg to the Agent.
- **Save user message**: `create_event()` BEFORE streaming starts (so
  mid-processing returns show the pending question).
- **Save assistant response**: `create_event()` AFTER streaming completes
  with enriched content — `<tool>`, `<think>`, `<suggestions>` tags
  embedded inline.

### Why not `AgentCoreMemorySessionManager`?

The `ag_ui_strands.StrandsAgent` wrapper creates its own internal agents
per thread, discarding `session_manager`, `messages`, and
`callback_handler` from the passed Agent. The `_agents_by_thread`
injection is the workaround that passes history messages through.

---

## Tool call tracing

Sub-agent tool calls happen in separate runtime containers, so the
parent's AG-UI stream can't see them natively. The tracing mechanism:

1. Each agent uses `TracingCallbackHandler` as its Strands
   `callback_handler`.
2. Tool functions call `handler.complete_tool()` directly after
   execution — Strands `ToolResultEvent` has `is_callback_event=False`
   so it won't fire the callback otherwise.
3. `build_traced_response()` returns a dict with `response` +
   `tool_trace` array (NOT a pre-serialised JSON string — the
   entrypoint serialises once; pre-serialising adds an escape layer
   per hop).
4. The parent's `_delegate` wrapper parses and forwards nested traces.
5. The supervisor's enriched memory save embeds tool data as `<tool>`
   tags for history rendering.

### Threading gotcha

Strands runs `@tool` functions in a `ThreadPoolExecutor`. Thread-local
storage doesn't propagate the handler. Use the module-level variable
pattern (`set_current_handler`/`get_current_handler` in
`shared/registry.py`). Safe because AgentCore Runtime serves one
request at a time per container.

### Tool output escape

Tool output strings pick up up to 3 layers of JSON escaping (leaf →
orchestrator → supervisor → memory). Frontend `TracePanel`/`ReportPanel`
and `_normalizeToolInfo` loop `JSON.parse` up to 3 times until
non-string, then pretty-print.

---

## Build hash & selective rebuilds

`.lambda-hashes/<name>.sha` tracks what's been built. Two categories:

**Lambda tools** (`src/lambda/mcp/`): Each tool hashes its own source
plus `src/lambda/mcp/shared/`. Edit shared → every tool rebuilds. Same
pattern for collectors under `src/lambda/collectors/` — they also get
the shared directory copied into their zip.

**Agent containers**: `scripts/lib/build.sh` writes
`src/agents/.hierarchy-<agent>.json` (gitignored) per agent containing
only that agent's own entry. The hash covers `agent_dir/ +
src/agents/shared/ + .hierarchy-<agent>.json`. **Edit one agent's
prompt → only that agent's hash flips.** The Dockerfile COPYs the slice
via an `AGENT_HIERARCHY_PATH` build-arg (defaults to the full file for
ad-hoc `finch build` outside the deploy pipeline).

Slicing invariant: **containers hold ONLY their own agent's entry**.
Never `hierarchy[sibling_name]` at runtime — use
`shared/registry.py::load_agent_registry()` for sibling data. Violation
fails loudly at container startup (`KeyError`), not silently.

Hashes are type-aware (`scripts/lib/build.sh`):
- Frontend agents hash everything under `shared/` (including report
  templates).
- Orchestrator/worker agents exclude frontend-only files
  (`agui_server.py`, `reports.py`, `memory.py`, `suggestions.py`,
  `report_templates/`).

Changing `dir` fields in `hierarchy.json` does NOT auto-invalidate —
the per-agent hash still matches. After such a change: `rm
.lambda-hashes/*.sha`.

Builds run in parallel batches. `MAX_PARALLEL_BUILDS` is auto-detected
from CPU count (cores/2, clamped 1–5) by `_detect_max_parallel_builds`
in `scripts/lib/config.sh`.

---

## Gateway tool schema sync

`aws_bedrockagentcore_gateway_target` (Terraform) only supports ONE
`inline_payload` tool schema per target. For multi-tool Lambdas,
`terraform apply` overwrites gateway target schemas with a single-tool
placeholder every time. `scripts/deploy.sh` deletes
`.lambda-hashes/gateway-tools.sha` immediately after apply so
`sync_gateway_tools` always re-uploads the full multi-tool schemas
from `tools.json`.

---

## Frontend architecture

Three data planes:

- **Live chat**: Frontend → AgentCore Runtime → AG-UI SSE stream →
  real-time rendering.
- **CRUD operations** (sessions, templates, reports): Frontend → API
  Gateway → Frontend API Lambda → AgentCore Memory / DynamoDB.
- **History loading**: Via the Frontend API Lambda
  (`GET /sessions/{id}/history`), NOT the supervisor runtime. The
  `actor_id` is extracted from the JWT `email` claim (sanitised: `@`
  → `_at_`, `.` → `_`).
- **Visualizer fast-path**: Target-tier flips + live BGP polls go
  Frontend → API Gateway → dedicated network-resilience Lambda
  (<500ms) without triggering a chat turn.

### Panel state

The right sidebar has three mutually-exclusive modes:

- **Trace** — thinking/reasoning + tool call hierarchy (from the inline
  trace button on a message).
- **Artifact** — rendered report; download as PDF / HTML / Markdown /
  PNG.
- **Visualizer** — React Flow topology canvas with 19 node types,
  resilience scorecard, maintenance calendar, ghost-node
  recommendations, failure simulation, live-status overlay. Mounts
  when an assistant message contains a `<visualizer-state>` tag.
  Reconstructed on history reload by scanning saved `<tool>` traces.

Session state (tokens + refresh) is persisted to `sessionStorage` so
page reload doesn't force a Cognito round-trip. Panel state resets on
thread switch.

---

## Reports

When an AG-UI request has `forwardedProps.template_id`, `agui_server.py`
switches to report mode. Sections run in a `ThreadPoolExecutor` +
`as_completed` inline in the SSE generator — independents run parallel,
dependents wait on prerequisites.

- Progress → `REASONING_MESSAGE_CONTENT` (thinking card).
- Section body → `TEXT_MESSAGE_CONTENT` (report panel).
- Memory save format: `<tool>` tags (OUTSIDE `<report-body>`) +
  `<report-body>` + `<artifact>`.

Reports and templates share a DynamoDB table — reports partition by
`report:{actor_id}`, templates by raw `actor_id`, built-in templates
by `"system"`.

---

## Authentication

| Edge | Auth |
|---|---|
| Frontend → Supervisor | JWT bearer (Cognito `CUSTOM_JWT` authoriser) |
| Agent → Agent | `AWS_IAM` (SigV4 via execution role) |
| Agent → Gateway | `AWS_IAM` (SigV4 via custom `httpx.Auth` subclass) |
| Frontend → API Gateway | JWT (Cognito authoriser, same user pool) |

### Cognito JWT authoriser gotcha

In the `custom_jwt_authorizer` block, set ONLY `allowed_audience` —
NOT `allowed_clients`. Cognito ID tokens carry client ID in `aud`, not
`client_id`; setting both fails.

---

## Infrastructure

- **AgentCore Runtime** — one runtime per agent,
  `network_mode = "PUBLIC"` (no VPC).
- **AgentCore Gateway** — single gateway with Lambda tool targets.
- **AgentCore Memory** — manual save/load via `create_event` /
  `list_events`.
- **Cognito** — user pool with OIDC, MFA optional, admin-created users
  only, SRP auth flow.
- **API Gateway HTTP API** — Frontend API with Cognito JWT authoriser.
- **DynamoDB** — agent registry, report templates, reports,
  health-events. CMK-encrypted, resource-based policies deny
  cross-account access.
- **KMS** — customer-managed key with annual rotation for DynamoDB
  server-side encryption. Key policy grants DynamoDB + CloudWatch
  Logs service access.
- **Bedrock Guardrail** — standalone `ApplyGuardrail` API on user input
  (prompt attack HIGH, sensitive info BLOCK, topic deny). Not
  model-level.
- **S3 + CloudFront** — static frontend hosting with HSTS, X-Frame
  DENY, X-Content-Type nosniff, Referrer-Policy headers.
- **CloudWatch Logs** — 30-day retention on all Lambda + vended log
  groups (pre-created by Terraform).
- **ECR** — container image registry for agent images.
- **Terraform** — all infra under `terraform/modules/core/` (platform)
  and `terraform/modules/custom/` (optional add-ons).

### Scripts anatomy

`scripts/deploy.sh` orchestrates the deployment. Sub-scripts under
`scripts/lib/`:

- `common.sh` — logging, `tf_output` helper (strips Terraform
  deprecation warnings that would otherwise corrupt `-raw` output).
- `hierarchy.sh` — agent tree from `hierarchy.json`.
- `config.sh` — interactive config, agent/tool resolution.
- `build.sh` — ECR login, agent images (type-aware hashing, per-agent
  hierarchy slicing), frontend build, `.env.local` auto-generation.
- `terraform.sh` — bootstrap, tfvars generation, plan/apply/destroy,
  fingerprinted init skip.
- `sync.sh` — runtime sync, gateway tool schemas, observability, S3
  deploy.
- `teardown.sh` — ECR cleanup, memory cleanup, state backend cleanup.
- `shared_config.sh` / `commands.sh` — `make setup` / `make configure`
  / `make reconfigure-shared` interactive flows, `-var` override
  translation, legacy `.env` migration.

---

## High-signal gotchas

Load-bearing constraints that aren't obvious from reading the code:

- **MCP import name collision**: use `streamablehttp_client` (no
  underscores) from `mcp.client.streamable_http` for SigV4 `auth=`
  support. The similarly-named `streamable_http_client` silently drops
  `auth=`, tools fail to load, and the model hallucinates fake
  `<function_calls>` XML with fabricated data.
- **MCPClient lifecycle in leaf agents**: manual `__enter__()` →
  `list_tools_sync()` → pass the **tool list** (not the client) to
  `Agent(tools=tools)` → `__exit__` in `finally`. Passing `MCPClient`
  directly to `Agent` causes `"client failed to initialize"` in
  runtime containers.
- **`MCPAgentTool` attribute**: use `tool.tool_name` (NOT `tool.name`)
  when filtering by `tools: [...]` in `hierarchy.json`. Wrong
  attribute = all tools silently excluded = model hallucinates.
- **`update_agent_runtime` is NOT partial**: omitting
  `authorizerConfiguration` strips JWT. `environmentVariables` is
  replaced entirely — `deploy.sh` uses `{**current_env,
  **desired_updates}` merge to avoid stripping Terraform-managed vars
  like `REPORT_TABLE_NAME`.
- **`invoke_agent_runtime` response key** is `response` (blob), NOT
  `payload`. Terraform outputs `agent_runtime_ids` as **names**, not
  ARNs — call `get_agent_runtime(agentRuntimeId=name)` first to get
  the ARN.
- **`AWS_REGION` + `AWS_DEFAULT_REGION`** must be explicitly set on
  runtimes (AgentCore doesn't inject them). Do NOT set `AWS_REGION`
  in Lambda `environment.variables` — it's reserved and Lambda
  injects it automatically; setting it fails `CreateFunction`.
- **`MemoryClient(region_name=...)`** must be passed explicitly — it
  defaults to `us-west-2`, not `AWS_REGION`.
- **Supervisor-only env vars**:
  `agent-runtime-base/main.tf` (sub-agents) and
  `agentcore-runtime/main.tf` (supervisor) are SEPARATE modules. New
  env vars must be added to BOTH or the supervisor crashes on
  startup with no useful error (`AGENT_NAME` required at import time).
- **`filebase64sha256`** in Terraform evaluates during plan/destroy,
  not just apply — always `make package` before ANY terraform
  operation including destroy, or destroy fails with missing zip.
- **Agent registry cleanup**: renaming an agent in `hierarchy.json`
  leaves the old DynamoDB registry entry behind.
  `sync_agent_registry()` in `sync.sh` scans and deletes orphans at
  the start of `post_deploy_sync()`.

---

## Design rationale & extension guidance

Why the tree is shaped the way it is, and how to decide where new work
belongs. These are judgement calls that aren't obvious from the code.

### Keep the tree shallow

Two levels (supervisor → orchestrator → leaf) is the recommended maximum.
The HTTP cost of an extra hop is negligible, but every mid-level agent on
the path adds a **full LLM inference call** (~2–5s depending on model and
prompt). A two-level path is three inference calls instead of two — the
latency compounds with depth, not the network.

### When to add a mid-level (orchestrator) agent

Keep a domain **flat under the supervisor** until it has 2–3+ specialised
children. Don't introduce an orchestrator just to sit in front of a single
leaf — register that leaf directly under the supervisor instead. Because
child discovery is registry-driven (`shared/registry.py` + the DynamoDB
registry), reorganising later is cheap and doesn't require changing
supervisor code. Add the orchestrator when the routing logic between
siblings genuinely needs its own reasoning step.

### Agent vs. MCP tool — which to build

- **MCP tool server** — pure data access. No reasoning, no delegation. It
  fetches and returns data from an AWS API or data store. Examples:
  Cost Explorer, CUR/Athena, Resource Groups Tagging, the Health Events
  table.
- **Agent** — the reasoning layer. It uses tools to gather data, then
  interprets, correlates, prioritises, and recommends.
- **Rule of thumb**: if a component doesn't need to reason or decide, it's
  a tool server, not an agent.
- **Cross-domain data sources belong in shared tools**, not domain-specific
  agents — so any agent in any domain can query them through the gateway.

### Synchronous delegation and long-running queries

Delegation is **synchronous**: the supervisor blocks until the full chain
(orchestrator → leaf → tool) returns. A long-running tool call — e.g. a
large CUR/Athena scan — can approach the runtime invocation timeout. Keep
leaf tool calls bounded; if a query is inherently slow, page it or narrow
the scan rather than relying on a single long blocking call.

### Integrating an external or managed agent

A leaf or orchestrator can call an agent it doesn't own (see
`shared/frontier.py`, which wraps external agents as `@tool` functions).
Two integration paths:

- **AgentCore Runtime (`invoke_agent_runtime`)** — wrap the boto3 call in a
  `@tool` function. Needs `bedrock-agentcore:InvokeAgentRuntime` on the
  caller's role plus the target ARN. Responses stream, so process chunks
  incrementally.
- **External A2A/HTTP endpoint** — register it in the DynamoDB registry
  like any other child.

Either way, **normalise the response** to the platform's
`{agent_name, status, data | error_message}` shape so callers handle every
integration path identically.

### One gateway, tools named per-API

All Lambda tools register as targets on a **single** AgentCore Gateway.
Agents self-select tools via their system prompt and the `tools` filter in
`hierarchy.json` — no per-domain gateways. Multiple gateways are only
warranted for genuinely different auth boundaries (e.g. internal IAM vs.
external JWT). Name each tool after the **AWS API surface it wraps**
(`cost-explorer`, `cur-athena`, `tag-governance`), not the agent domain
that happens to use it — any agent can reach any tool through the gateway.

---

## Related reading

- `skills/developer-guide/SKILL.md` — how to add agents, tools, collectors.
- `docs/agents/` — per-leaf-agent references (deploy modes, data model, gotchas).
- `docs/agents/health-events.md` — health-events deploy modes, support plan
  matrix, backfill flow.
- `docs/agents/tag-governance.md` — tag governance deploy modes,
  tag-policy bring-up, Tier-1/Tier-2 API strategy.
- `docs/observability-tuning.md` — X-Ray sampling and Transaction
  Search indexing.
- `docs/development.md` — terse project conventions and high-signal gotchas.
