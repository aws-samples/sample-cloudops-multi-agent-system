# MR: Add read-only CloudWatch alarm agent (recommend + tune → CloudFormation)

## Summary

Adds a new **read-only** `cloudwatch-agent` leaf that (1) recommends AWS
best-practice alarms for a tagged workload and (2) tunes existing alarm
thresholds from real metric history to cut false-positive noise. It **never**
mutates AWS state — it reads metrics/alarms and hands back a CloudFormation
template the user reviews and applies themselves.

Delivery of the (large) CFN template is out-of-band: the YAML is intercepted
from the model stream, persisted to the reports table, and surfaced as a
clickable ReportCard via a `<report-pending>` marker — never re-serialized by
an intermediate LLM and never bloating chat Memory.

## What's included

### New MCP tool — `src/lambda/mcp/cloudwatch/` (~1,870 LOC)
One Lambda, 8 tools registered as AgentCore Gateway targets:
- `get_metric_data`, `get_metric_metadata`, `get_recommended_metric_alarms`
- `analyse_metric` (14-day stats: mean/median/p99/stddev/trend/seasonality/noise)
- `get_active_alarms`, `get_alarm_history`
- `build_cfn_alarm`, `assemble_cfn_template` (deterministic YAML serializer —
  the single source of the emitted template)

Modules: `handler.py` (630), `cfn.py` (442), `arn.py` (423), `analysis.py`
(275), `recommendations.py` (100). Deps: numpy, pandas, pyyaml, boto3.

### tag-governance — new `find_resources_by_tag` tool
`src/lambda/mcp/tag-governance/handler.py` (+170). Resolves a tag selector to
resource ARNs via `tag:GetResources`; used by the cloudwatch-agent to fan out
alarm recommendations across a workload. (Lives in tag-governance but exists
to serve cloudwatch.)

### Agent wiring
- `src/agents/hierarchy.json` — new `cloudwatch-agent` leaf under
  `ops-excellence-agent`; routing trigger words; supervisor rule to pass
  `<cfn-artifact>` blocks through verbatim.
- `src/lambda/mcp/tools.json` — schemas for the 8 cloudwatch tools +
  `find_resources_by_tag`.

### CFN-artifact delivery pipeline (the largest / riskiest surface)
- `src/agents/shared/agui_server.py` (+722) — `<cfn-artifact>` streaming
  interceptor → persist to reports table → emit `<report-pending>` ReportCard;
  `_abbreviate_bulky_tool_traces`; `_ReportPendingStripper`.
- `src/agents/shared/agent_base.py` — `_NO_RELOAD_TRACE_TOOLS` (drops bulky
  tool output from Memory at source); no-fabrication rule 9 hardening.
- `src/agents/shared/memory.py` — `_scrub_cfn_artifacts` safety net before the
  100k-char AgentCore Memory `create_event` limit.
- `src/agents/shared/registry.py` — out-of-band cfn-artifact extraction/queueing
  (`_pending_cfn_artifacts` / `_outbound_cfn_artifacts`).
- `src/frontend/src/app/page.tsx` — `splitReportPendingParts` so a
  `<report-pending>` marker glued to surrounding markdown still renders a
  ReportCard on reload (de-duped by report_id).

### Two bugs fixed in this pipeline (see docs/… memory)
1. **"No response" on reload** — a many-alarm run's ~26× `get_recommended_metric_alarms`
   fan-out pushed the saved assistant turn past AgentCore Memory's 100k-char
   limit; `create_event` threw, the turn silently vanished on reload. Fixed by
   widening `_NO_RELOAD_TRACE_TOOLS`.
2. **Broken ReportCard on tune follow-up** — once markers persisted to Memory,
   the model mimicked the format and typed its OWN `<report-pending>` with a
   fabricated id → card polled a 404 forever. Fixed with `_ReportPendingStripper`
   (drops model-typed markers; platform-injected markers bypass it) + prompt guard.

### Cross-account support
`cross_account_role_arn_cloudwatch` threaded through: `terraform/main.tf`,
`shared-config/{main,variables}.tf` (+ SSM mirror), `scripts/lib/commands.sh`
& `shared_config.sh` (configure prompt), `.env.example`. Empty → falls back to
the Lambda execution role (single-account).

### Build fix (required by this feature)
`Makefile` — pins `pip install` to `manylinux_2_28_x86_64 / manylinux2014_x86_64`,
`--python-version 3.12 --only-binary=:all:`, and adds `|| exit 1`. Necessary
because cloudwatch is the first Lambda with compiled deps (numpy/pandas):
building on macOS without this ships Mac wheels that crash at import on Lambda.

### Docs & tests
- `docs/agents/cloudwatch.md` (new), `docs/agents/README.md` (+1).
- New tests: `test_cloudwatch_{analysis,arn,cfn,handler,recommendations}.py`
  (11/5/34/19/14), `test_agui_cfn_artifact_intercept.py` (49),
  `test_tag_governance_find_resources.py` (22).
- Edited: `test_memory.py`, `test_prompt.py`, `test_tag_governance_tool.py`.
- Full unit suite: 659 passing.

## Suggested review order
1. `src/lambda/mcp/cloudwatch/` — the tool itself (handler → cfn → analysis).
2. `tools.json` + `hierarchy.json` — how it's exposed and routed.
3. `agui_server.py` cfn-artifact pipeline — largest surface, both bug fixes.
4. `page.tsx` — reload rendering.
5. terraform / scripts / Makefile — deploy wiring.

## Deploy note
Shared-agent files changed → rebuilds all 10 agent images. Run with
`AWS_REGION=us-east-1` (see repo memory on the region-env deploy footgun).
