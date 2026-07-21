---
name: developer-guide
description: "Project-specific developer guide for extending the CloudOps Multi-Agent System. NOT a generic reusable skill — this contains architecture, procedures, and gotchas specific to this repository. Walks users through adding agents, Lambda MCP tools, data collectors, report templates, and deployment workflows. Use when the user asks how to add/modify agents, tools, or capabilities, how the platform works, how to deploy, or when they encounter build/deploy issues."
argument-hint: "[what do you want to do? e.g. 'add an agent', 'add a tool', 'deploy only tools']"
allowed-tools: Bash, Write, Read, Glob, Grep, Edit
user-invocable: true
---

# CloudOps Multi-Agent System — Developer Guide

> **Scope**: This skill is specific to the CloudOps Multi-Agent System repository. The procedures, file paths, conventions, and gotchas below apply only to this project.

You are a developer assistant for this platform. Guide users step-by-step through extending and operating it. After classifying their request, walk them through one step at a time — don't dump the entire guide. Offer to make file changes for them. Read `src/agents/hierarchy.json` or `src/lambda/mcp/tools.json` before suggesting edits so you're working from current state.

---

## How the Platform Works

```
User → CloudFront (Next.js SPA) → Cognito (JWT auth)
  Chat path:  → AgentCore Runtime (supervisor, AG-UI SSE) → Orchestrators (SigV4) → Workers (SigV4) → AgentCore Gateway (MCP) → Lambda Tools → AWS APIs
  REST path:  → API Gateway → Lambda (Core API + Network Resilience API) → DynamoDB
```

- **Supervisor**: User-facing agent. AG-UI protocol, manages memory, delegates to domain agents, generates reports.
- **Mid-level agents** (FinOps, Governance, Ops Excellence): Route requests to specialized leaf agents. Discover children from DynamoDB registry at startup.
- **Leaf agents** (Cost Operations, Pricing, Health Events, etc.): Call AWS APIs via Lambda tools through the AgentCore Gateway.
- **Lambda tools**: Thin Python handlers wrapping AWS APIs. Registered on a single gateway.

All agent behavior is defined in `src/agents/hierarchy.json`. Three generic code folders handle all agent types — no per-agent code.

### Key services
- Amazon Bedrock AgentCore Runtime (agent containers)
- Amazon Bedrock AgentCore Gateway (MCP tool routing)
- Amazon Bedrock AgentCore Memory (session conversations)
- Amazon Bedrock (Claude Opus for supervisor, Claude Sonnet for sub-agents)

- Amazon Cognito (federated SSO + JWT auth)
- Amazon DynamoDB (reports, templates, agent registry)
- Amazon CloudFront + S3 (Next.js SPA)
- AWS Lambda (MCP tools + frontend REST APIs)

---

## Quick Reference: What to Change

| I want to... | What to edit | Deploy command |
|--------------|-------------|----------------|
| Add a new leaf agent | `hierarchy.json` only | `make deploy-auto` |
| Add a new mid-level agent | `hierarchy.json` only | `make deploy-auto` |
| Add a new Lambda tool | `src/lambda/mcp/<tool>/handler.py` + `tools.json` | `make deploy-auto` |
| Change an agent's prompt | `hierarchy.json` → `prompt` field | `make deploy-auto` |
| Change an agent's model | `hierarchy.json` → `model` field | `make deploy-auto` |
| Restrict which tools an agent uses | `hierarchy.json` → `tools` array | `make deploy-auto` |
| Deploy only backend (skip frontend) | Set `DEPLOY_MODE=agents-only` in `.env` | `make deploy-auto` |
| Deploy only gateway + tools | Set `DEPLOY_MODE=gateway-only` in `.env` | `make deploy-auto` |
| Deploy specific agents only | Set `DEPLOY_AGENTS=finops-agent` in `.env` | `make deploy-auto` |
| Deploy specific tools only | Set `DEPLOY_TOOLS=cost-explorer,billing` in `.env` | `make deploy-auto` |

---

## Adding a New Agent (No Code Required)

### Step 1: Add the agent entry to hierarchy.json

```json
{
    "my-new-agent": {
        "type": "worker",
        "dir": "agents/worker",
        "protocol": "http",
        "description": "One-sentence description of what this agent does",
        "model": "global.anthropic.claude-sonnet-4-6",
        "tools": ["cost-explorer", "billing"],
        "prompt": "You are the My New Agent.\n\nYour responsibilities:\n- Do X\n- Do Y\n\nRules:\n- Do NOT proactively run extra queries.\n- Only use tools when the user explicitly asks.\n"
    }
}
```

### Step 2: Add it as a child of its parent

```json
{
    "finops-agent": {
        "children": ["cost-operations-agent", "pricing-agent", "my-new-agent"]
    }
}
```

### Step 3: Deploy

```bash
make deploy-auto
```

That's it. The deploy script builds a container, creates an AgentCore Runtime + endpoint + IAM role, registers it in DynamoDB, and the parent discovers it automatically.

### Agent Types

| Type | `"type"` value | Code folder | What it does |
|------|----------------|-------------|-------------|
| Frontend | `"frontend"` | `agents/frontend/` | User-facing AG-UI agent with memory, suggestions, reports |
| Orchestrator | `"orchestrator"` | `agents/orchestrator/` | Mid-level agent that delegates to ONE child per request |
| Worker | `"worker"` | `agents/worker/` | Leaf agent that calls gateway MCP tools |

### Key Fields in hierarchy.json

| Field | Required | Description |
|-------|----------|-------------|
| `type` | Yes | `"frontend"`, `"orchestrator"`, or `"worker"` |
| `dir` | Yes | Code folder (always one of the three generic folders) |
| `protocol` | Yes | Always `"http"` (deploy.sh handles AGUI for the supervisor) |
| `description` | Yes | One-line description (shown in parent agent's prompt) |
| `model` | No | Bedrock model ID (default: `us.anthropic.claude-sonnet-4-20250514-v1:0`) |
| `prompt` | Yes | System prompt. Use `{agent_listing}` for orchestrators |
| `children` | No | Array of child agent names (for orchestrators/supervisor) |
| `tools` | No | Array of gateway target names this agent can use (for workers). Omit = all tools |
| `memory` | No | Enable conversation memory (only for frontend type) |
| `suggestions` | No | Enable follow-up suggestions (only for frontend type) |

### Canonical example

The `tag-governance-agent` is the complete reference: leaf worker in hierarchy.json, Lambda at `src/lambda/mcp/tag-governance/handler.py` with 6 tools, cross-account role wiring, report template at `src/agents/shared/report_templates/org_tag_governance.json`, 18 unit tests. See `docs/agents/tag-governance.md`.

### Documenting your new leaf agent

Any leaf agent with multiple deploy modes, AWS Support-plan dependency, non-trivial data model, or non-obvious gotchas gets its own file under `docs/agents/`. See `docs/agents/README.md` for the template.

---

## Adding a New Lambda Tool (Code Required)

### Step 1: Create the handler

```
src/lambda/mcp/my-tool/
  handler.py
  requirements.txt
```

**handler.py** pattern:

```python
import json
import boto3

def handler(event, context):
    """Gateway passes tool name via context, params via event body."""
    extended_tool_name = context.client_context.custom["bedrockAgentCoreToolName"]
    tool_name = extended_tool_name.split("___")[1]

    handlers = {
        "my_action": handle_my_action,
        "my_other_action": handle_my_other_action,
    }
    fn = handlers.get(tool_name)
    if fn:
        return fn(event)
    return {"error": f"Unknown tool: {tool_name}", "available_tools": list(handlers.keys())}

def handle_my_action(event):
    """Each tool function receives the event body directly (not wrapped)."""
    param1 = event.get("param1", "")
    client = boto3.client("my-service")
    try:
        result = client.some_api_call(Param1=param1)
        return {"status": "success", "data": result}
    except Exception as e:
        return {"error": str(e)}
```

### Step 2: Register in tools.json

Add to `src/lambda/mcp/tools.json`:

```json
{
    "my-tool": {
        "handler": "handler.handler",
        "runtime": "python3.12",
        "timeout": 30,
        "memory": 256,
        "iam_actions": ["my-service:SomeApiCall"],
        "tools": [
            {
                "name": "my_action",
                "description": "What this tool does — be specific, the model reads this",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "param1": {
                            "type": "string",
                            "description": "Description of param1"
                        }
                    },
                    "required": ["param1"]
                }
            }
        ]
    }
}
```

The `tools` array defines MCP tool schemas visible to the model. The `env_vars` field passes environment variables (values starting with `$` resolve from `.env` at deploy time).

### Step 3: Assign the tool to an agent

In `hierarchy.json`, add the tool name to the agent's `tools` array.

### Step 4: Deploy

```bash
make deploy-auto
```

---

## Adding a Data Collection Module

For data that needs background collection (not real-time API calls).

### Pattern

1. Lambda in `src/lambda/collectors/<name>/handler.py`
2. Terraform module in `terraform/modules/custom/<name>/`
3. Wire into `terraform/main.tf`
4. Create MCP tool that queries the collected data

### Example: Health Events Collector

EventBridge (`aws.health`) → SQS → Lambda (enrich + normalize with Claude Haiku) → DynamoDB. The `health-events` MCP tool then queries that table. See `docs/agents/health-events.md` for deploy modes and `make backfill-health`.

### When to use

- Event-driven data (AWS Health events arrive via EventBridge)
- Expensive or rate-limited APIs you don't want to call real-time
- Data that needs enrichment before querying (risk scoring, LLM summarization)

---

## Adding a Report Template

Templates are JSON files auto-discovered by the platform.

### Create the file

`src/agents/shared/report_templates/<template_id>.json`:

```json
{
    "name": "My Report",
    "description": "What this report covers",
    "sections": [
        {"id": "section_1", "title": "Section One", "prompt": "Analyze X using the Y tool..."},
        {"id": "section_2", "title": "Section Two", "prompt": "Based on section 1, summarize..."}
    ],
    "dependencies": {
        "section_2": "section_1"
    }
}
```

- Independent sections run in parallel
- Dependent sections wait for prerequisites
- No code changes needed — just deploy

---

## Deployment

### Commands
```bash
make setup              # First time: writes .env + installs deps
make configure          # First time: writes shared config to SSM
make deploy-auto        # Full non-interactive deploy
make plan               # Terraform plan only
make run-local          # Local dev server
make test-unit          # Unit tests
make test-integration   # Integration tests (needs deployed stack)
make clean              # Wipe build artifacts
```

### Deploy modes (set in .env)
| Mode | What deploys |
|------|-------------|
| `full` (default) | Everything |
| `agents-only` | Agents + gateway + tools (no frontend) |
| `gateway-only` | Gateway + Lambda tools (MCP server for external clients) |
| `tools-only` | Lambda functions only |

### Selective deployment
```bash
DEPLOY_AGENTS=pricing-agent          # Auto-includes parent chain + supervisor
DEPLOY_TOOLS=cost-explorer,billing   # Only these Lambda tools
DEPLOY_MODE=gateway-only             # Standalone MCP gateway
GATEWAY_AUTH=oauth                   # JWT auth on gateway (for external clients)
```

### Dependency resolution
When you specify agents in `DEPLOY_AGENTS`, the deploy script auto-includes the frontend agent and resolves parent/child dependencies.

---

## Changing Agent Behavior

| Change | Edit | Effect |
|--------|------|--------|
| Prompt | `hierarchy.json` → `prompt` | Only that agent's container rebuilds |
| Model | `hierarchy.json` → `model` | Container rebuild |
| Tool access | `hierarchy.json` → `tools` array | Container rebuild |
| Promote to frontend | `hierarchy.json` → `type: "frontend"` | Agent becomes user-facing entry point |

Prompt-only changes rebuild only the affected agent (~13 min saved over full rebuild).

---

## Common Workflows

### "I want to add a new AWS domain (e.g., Security)"

1. Add an orchestrator to `hierarchy.json` with `"type": "orchestrator"` and `"children": [...]`
2. Add leaf worker agents for each capability
3. Add the orchestrator as a child of the supervisor
4. Create Lambda tools for the AWS APIs
5. `make deploy-auto`

### "I want to expose the gateway as a standalone MCP server"

1. Set `DEPLOY_MODE=gateway-only` in `.env`
2. Optionally set `GATEWAY_AUTH=oauth` for JWT auth
3. `make deploy-auto`
4. Gateway endpoint URL in Terraform output

### "I want to deploy a single agent as the user-facing entry point"

1. Change the agent's `type` to `"frontend"` in `hierarchy.json`
2. `make deploy-auto`

---

## Prompt Design Rules (Critical)

These prevent the most common behavioral regressions:

**Orchestrators:**
- "Pick ONE agent per request"
- "NEVER ask clarifying questions"
- "Pass the user's question through as-is"

**Workers:**
- Gate each tool on the user explicitly asking for it
- Key line: "Do NOT proactively run extra queries."
- Without this, "be helpful" becomes "run every possible analysis"

**Supervisor:**
- Must resolve ambiguous references ("this", "last month") from memory BEFORE delegating
- Sub-agents are stateless — they see a single prompt with no history

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Agent returns hallucinated data | Tools not loading (filter mismatch) | Check `tool.tool_name` (NOT `tool.name`) |
| Silent 424 errors | Protocol mismatch | Re-run `make deploy-auto` |
| "client failed to initialize" | MCPClient passed to Agent | Pass `tools` list, not the client |
| Agent sees 0 tools | Gateway pagination (30/page) not drained | Check `load_gateway_tools` loop |
| Model fabricates XML `<function_calls>` | Wrong import (`streamable_http_client`) | Use `streamablehttp_client` (no underscores) |
| Container crash on startup | Missing env var | Add to BOTH runtime TF modules |
| `terraform destroy` fails | Missing Lambda zip | Run `make package` first |
| Stale agent after rename | Old registry entry | Next deploy auto-cleans orphans |
| Health events table empty | EventBridge only catches NEW events | Run `make backfill-health DAYS=90` |

---

## Critical Gotchas

1. **`hierarchy.json` is sliced per-agent at build time.** Each container only has its own entry. Never read `hierarchy[sibling_name]` — use `shared/registry.py::load_agent_registry()`.

2. **`protocol` must be `"http"` for ALL agents** including the supervisor. AGUI switch is post-deploy.

3. **Gateway paginates `tools/list` at 30.** `load_gateway_tools` drains pages in a loop.

4. **`update_agent_runtime` is NOT partial.** Omitting `environmentVariables` strips all vars.

5. **Sub-agents are stateless.** Supervisor resolves references from memory before delegating.

6. **Strands `@tool` functions run in ThreadPoolExecutor.** Thread-locals don't propagate. Use module-level variable.

7. **Tool output escaping.** Up to 3 layers of JSON escaping. Frontend loops `JSON.parse` up to 3 times.

8. **`AWS_REGION` must NOT be set in Lambda env vars** (reserved). But MUST be set on Runtimes.

9. **`MemoryClient(region_name=...)` must be explicit** — defaults to `us-west-2`.

10. **Bedrock Guardrail (standalone ApplyGuardrail API).** Integrated via `shared/guardrail.py::check_user_input()` — calls `bedrock-runtime:ApplyGuardrail` on ONLY the raw user message at the supervisor entry point. Prompt attack detection + sensitive info filters + topic policy. System prompts never reach the classifier (eliminates false positives on multi-agent delegation). Layered with `_NO_FABRICATION_PREAMBLE` + IAM tool scoping + `redact.py`.

11. **`make package` before ANY Terraform operation** including destroy.

12. **Two-tier config.** `.env` = per-developer identity only. Everything else in SSM Parameter Store.

---

## Project Structure

```
src/
  agents/
    hierarchy.json           # THE config file — all agent definitions
    frontend/                # Generic AG-UI agent (user-facing)
    orchestrator/            # Generic mid-level agent
    worker/                  # Generic leaf agent
    shared/                  # Memory, tracing, reports, gateway, registry, redact
  lambda/
    mcp/                     # Lambda MCP tools (one per folder)
      tools.json             # Tool schemas and IAM config
      shared/                # Cross-account helper (copied into every zip)
    frontend/                # Browser-facing REST Lambdas
      core-api/              # Sessions, templates, reports CRUD
      network-resilience/    # /reassess + /live-status
    collectors/              # Background data collectors
      health-events/         # EventBridge → enrichment → DynamoDB
  frontend/                  # Next.js SPA

terraform/
  main.tf                    # Root stack
  modules/core/              # Platform modules (runtime, gateway, memory, cognito, etc.)
  modules/custom/            # health-events-collection, network-resilience-api

scripts/
  deploy.sh + lib/           # Deploy orchestrator
  backfill_health.py         # Health events backfill
  debug/                     # invoke_agent.py, fetch_logs.py, fix-gateway-tools.py

skills/                      # Claude Code / Kiro skills
  developer-guide/           # This skill
  resilience-report/         # DX resilience report generator
```
