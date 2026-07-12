# Agents

All agents are config-driven via `hierarchy.json`. Three generic code folders — `frontend/`, `orchestrator/`, `worker/` — read `AGENT_NAME` from env and load their config (prompt, model, tools, children) from `hierarchy.json` at startup.

## Adding an agent (JSON only, no new code)

1. Add an entry to `hierarchy.json` with `type`, `dir`, `protocol`, `description`, `model`, `prompt` (+ optional `tools`, `children`, capability flags).
2. Add its name to the parent's `children` array.
3. `make deploy-auto`.

`type`:
- `"worker"` — leaf agent, calls gateway MCP tools. Optional `tools: [...]` filters which gateway targets it sees.
- `"orchestrator"` — mid-level delegator, routes to ONE child per request.
- `"frontend"` — user-facing AG-UI entry point (adds memory, suggestions, reports).

`protocol` **must** be `"http"` for every agent, even the frontend. This field is NOT the runtime protocol — no code reads it. The runtime protocol is set by Terraform (`protocol_configuration`): the frontend runtime serves AG-UI, all others serve HTTP.

**Promoting to frontend**: any agent (orchestrator OR worker) can be `type: "frontend"`. `src/agents/frontend/server.py` derives the runtime execution mode from the promoted agent's hierarchy entry — if `children` is non-empty, it runs in mid-level mode (registry-based child delegation); otherwise it runs in leaf mode (gateway MCP tools, same code path as a regular worker). Do NOT hardcode `agent_type="mid_level"` in the frontend factory — a worker promoted to frontend that way loads zero tools and the hallucination guardrail refuses every prompt. See `test_promoted_frontend_has_either_children_or_tools` in `tests/unit/test_topology.py` for the regression guard.

## Prompt rules that prevent behavioral regressions

- **Orchestrator prompts**: "pick ONE agent per request", "NEVER ask clarifying questions", "pass the user's question through as-is". Listing children with soft guidelines causes parallel fan-out on trivial questions.
- **Worker prompts**: gate each tool on the user asking for it. The single most effective line is literally: "Do NOT proactively run extra queries." Without it, "be helpful" becomes "run every possible analysis."
- **Frontend (supervisor) prompt**: must resolve ambiguous references ("this", "last month", "compare to before") from memory BEFORE delegating. Sub-agents are **stateless** — they see a single prompt with no history.

`_inject_date()` in `shared/prompt.py` auto-injects today's date into every agent's prompt at startup. Don't add `{today_date}` placeholders — the injection handles both cases.

## Memory & tracing: don't accidentally break these

- Memory is supervisor-only, managed manually via `shared/memory.py` (`list_events`/`create_event`). `AgentCoreMemorySessionManager` is NOT used — `ag_ui_strands.StrandsAgent` discards `session_manager`/`messages`/`callback_handler` and builds its own internal agents per thread. The `_agents_by_thread[thread_id] = strands_agent` injection is the workaround that lets us pass `messages` history through.
- Sub-agents are stateless. Do not add memory calls to orchestrators or workers.
- Every agent uses `TracingCallbackHandler` as its Strands `callback_handler`. Tool functions call `handler.complete_tool()` directly — Strands `ToolResultEvent` has `is_callback_event=False` so it won't fire the callback otherwise.
- `build_traced_response()` MUST return a dict, not a pre-serialized JSON string. `@app.entrypoint` JSON-serializes return values via `_safe_serialize_to_json_string()`; pre-serializing adds an escape layer per hop (leaf → orchestrator → supervisor → memory gets up to 3 layers).
- Strands `@tool` functions run in a `ThreadPoolExecutor`, so `threading.local()` does NOT propagate the handler from the entrypoint. Use the module-level variable pattern (`set_current_handler`/`get_current_handler` in `shared/registry.py`). Safe because AgentCore Runtime serves one request at a time per container.

## Worker tool loading: MCP gotchas

- `MCPClient` lifecycle in workers: explicit `__enter__()` → `list_tools_sync()` → pass the **tool list** (not the client) to `Agent(tools=tools)` → `__exit__` in `finally`. Passing `MCPClient` directly to `Agent` causes `"client failed to initialize"` with no useful traceback.
- Use `streamablehttp_client` (no underscores) from `mcp.client.streamable_http` — the similarly-named `streamable_http_client` silently drops `auth=` and tools fail to load. When tools fail to load, the model hallucinates fake `<function_calls>` XML with fabricated data.
- `hierarchy.json` tool filter matches on `tool.tool_name` (NOT `tool.name`). Wrong attribute → all tools silently excluded → hallucination.

## Build hash (don't get burned by this)

`hierarchy.json` is SLICED per-agent at build time: `scripts/lib/build.sh` writes `src/agents/.hierarchy-<agent>.json` containing ONLY that agent's own entry, and the Dockerfile COPYs that slice (via `AGENT_HIERARCHY_PATH` build-arg) instead of the full file. Editing one agent's prompt therefore invalidates only that agent's hash — prompt-only changes rebuild 1 container instead of 9 (~13 min saved). Hashes are type-aware (frontend hashes all of `shared/`; orchestrator/worker exclude `agui_server.py`, `reports.py`, `memory.py`, `suggestions.py`, `report_templates/`).

**Containers only have their own entry.** `hierarchy.json` inside the image holds exactly one key — the agent's own name. Never write `hierarchy[sibling_name]` or `hierarchy.items()` inside agent code — the sibling data isn't there. For sibling/child lookup use the DynamoDB registry (`shared/registry.py::load_agent_registry`), which is the authoritative source for topology at runtime anyway. Violating this fails loudly at container startup (`KeyError`), not silently.

Changing `dir` fields in `hierarchy.json` does NOT invalidate hashes — the per-agent hash still matches. After such a change: `rm .lambda-hashes/*.sha`. Same after any Finch VM reset. The sliced `.hierarchy-*.json` files are gitignored and auto-regenerated each build.

## Security: Redaction + Behavioral Controls

- **Bedrock Guardrail (standalone ApplyGuardrail API)** — `shared/guardrail.py::check_user_input()` calls `bedrock-runtime:ApplyGuardrail` on ONLY the raw user message before any agent logic. System prompts never reach the classifier. Prompt attack + sensitive info + topic policy. Env vars: `BEDROCK_GUARDRAIL_ID`, `BEDROCK_GUARDRAIL_VERSION` (supervisor-only). Layered with `_NO_FABRICATION_PREAMBLE` + IAM least-privilege + `redact.py`.
- **Output redaction** (`shared/redact.py`): strips AWS account IDs, IAM ARNs, access keys, external IDs from text before persistence (memory + reports). Applied in `memory.py::build_enriched_text()` and `reports.py::save_report()`. Does NOT affect the live SSE stream to the user.

## When adding env vars to agent runtimes

Add to BOTH `terraform/modules/core/agent-runtime-base/main.tf` (sub-agents) AND `terraform/modules/core/agentcore-runtime/main.tf` (supervisor). They're separate modules. Missing this crashes the supervisor on startup with no useful error (`AGENT_NAME` required at import time in `frontend/server.py`).
