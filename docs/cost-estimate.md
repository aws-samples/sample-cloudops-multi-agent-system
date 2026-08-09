# Running cost estimate

What it costs to run this platform, in AWS us-east-1, on-demand pricing. Ranges, not single numbers — actual spend depends on how many turns you chat, which tools run, and whether opt-in features like CUR / Athena or the health-events collector are enabled.

> **Numbers in this doc were checked against AWS and Anthropic pricing pages at the time of writing.** Verify current rates with the [AWS Pricing Calculator](https://calculator.aws/) before committing to a monthly number. **Bedrock model tokens are 95%+ of the bill in every realistic scenario — optimise there first.**

## TL;DR monthly numbers

| Profile | Users | Daily turns each | Reports / mo | **Estimated monthly** |
|---|---|---|---|---|
| Hobbyist / demo | 1 | 3 | 0 | **~$9** |
| Daily driver | 1 | 20 | 2 | **~$56** |
| Small team | 5 | 15 | 5 | **~$208** |
| Medium team | 20 | 15 | 20 | **~$818** |
| Heavy usage | 50 | 30 | 40 | **~$4 060** |

Assumptions behind these numbers are in §4; the component prices are in §2–§3. **For per-module costs — "what does enabling health-events cost me?" — see §6.**

## 1. How this platform spends money

Every user prompt fans out through an agent tree. A simple question walks a 6-hop chain:

```
User → Supervisor (Opus)
         → Orchestrator (Sonnet)           # e.g. finops-agent
             → Leaf worker (Sonnet)        # e.g. cost-operations-agent
                 → AgentCore Gateway       # lists tools, invokes one
                     → Lambda tool         # calls AWS API, returns data
             ← Leaf continuation (Sonnet)  # formats tool result
         ← Orchestrator wraps              # (Sonnet)
      ← Supervisor synthesises             # (Opus)
```

That's **6 Bedrock model calls, 2–4 AgentCore Gateway invocations, 1–3 Lambda invocations, 5–10 DynamoDB ops, and 1 AWS API call** — all for a single user turn. The vast majority of the cost is in the Bedrock model calls; everything else either lives inside the free tier or costs fractions of a cent.

A report generation run amplifies this: 4–6 report sections execute in parallel, each a mini version of the chain above.

## 2. Component-by-component prices

All rates are **us-east-1 on-demand**. Anthropic models use **global endpoints** (cheaper than regional) — this is the platform's default (`global.anthropic.claude-*` in `hierarchy.json`).

### 2.1 Bedrock models (dominant cost)

| Model | Used by | Input | Output | Cache write 5m | Cache read |
|---|---|---|---|---|---|
| **Claude Opus 4.6** | Supervisor (frontend) | $5.00 / M | $25.00 / M | $6.25 / M | $0.50 / M |
| **Claude Sonnet 4.6** | 9 sub-agents (orchestrators + leaves) | $3.00 / M | $15.00 / M | $3.75 / M | $0.30 / M |
| **Claude Haiku 4.5** | Health-events collector (optional enrichment) | $1.00 / M | $5.00 / M | $1.25 / M | $0.10 / M |

us-east-1 **regional** endpoints add +10% across the board (for data-residency requirements). Source: [platform.claude.com/docs/pricing](https://platform.claude.com/docs/en/docs/about-claude/pricing).

**Cache columns matter a lot** but we don't use them today. Enabling prompt caching on system prompts is tracked as the highest-leverage Bedrock optimization (see `temp/optimizations.md`).

### 2.2 AgentCore

| Item | Rate | Platform usage |
|---|---|---|
| Runtime (container) | $0.0895 / vCPU-hour + $0.00945 / GB-hour, per-second billing, 128 MB min | 10 agent containers; **billed only on actual CPU consumption, not wall-clock**. I/O wait (e.g. supervisor blocking on a sub-agent) is **free**. |
| Gateway | $0.005 / 1 000 invocations | ~3–10 invocations per user turn |
| Gateway tool indexing | $0.02 / 100 tools / month | 36 tools today → **~$0.01/mo flat** |
| Memory (short-term events) | $0.25 / 1 000 events | ~10 events per turn |
| Memory (long-term storage, built-in strategies) | $0.75 / 1 000 records / mo | Low growth |
| Memory (long-term retrieval) | $0.50 / 1 000 retrievals | ~1 retrieval per turn |
| Identity, Policy | via Runtime / Gateway — **no additional charge** when used as we do | — |

Source: [AgentCore pricing](https://aws.amazon.com/bedrock/agentcore/pricing/).

### 2.3 Lambda

- **$0.20 per 1 M requests** + **$0.0000166667 per GB-second** (x86).
- **Free tier: 1 M requests + 400 000 GB-seconds / month** (does not expire).
- Platform usage: ~3–6 Lambda invocations per user turn (gateway → MCP tool → optional frontend REST).
- **Effective cost: $0** at any realistic chat volume. 30K turns/month ≈ 150K invocations, well under the free tier.

### 2.4 DynamoDB (on-demand)

- **$0.625 / M writes, $0.125 / M reads, $0.25 / GB-month** storage.
- Free tier: 25 GB storage + limited R/W requests monthly.
- Platform tables: `cloudops-agent-registry` (tiny), `cloudops-reports` (session history + reports), `cloudops-health-events` (grows with org + retained 180 days via TTL).
- **Effective cost: pennies** except for enormous orgs with years of health-events data.

### 2.5 Cost Explorer API

- **$0.01 per API request** (against the primary billing view).
- **No free tier.**
- Platform usage: `cost-operations-agent` calls `ce:GetCostAndUsage` / `ce:GetCostForecast` / `ce:GetCostAndUsageComparisons` — typically 1–3 calls per spending-related turn.
- **This is the one AWS API we use that is NOT free.** It's not dominant, but it's visible at medium-team scale upwards.

Source: [Cost Explorer API pricing](https://aws.amazon.com/aws-cost-management/aws-cost-explorer/pricing/).

### 2.6 Athena (CUR path, only when `cur-athena` enabled)

- **$5 per TB scanned**, 10 MB minimum per query.
- **No free tier.**
- Agent prompt enforces partition filters (year, month) in every query — well-scoped queries scan ~50–500 MB → cents per query.
- **Effective cost: a few cents/month for normal usage**. A careless unfiltered query on a multi-year CUR can drop tens of dollars — the worker prompt is written to prevent this.

### 2.7 S3

- Standard storage: **~$0.023 / GB-month** (first 50 TB; us-east-1 reference rate).
- Requests: **$0.005 / 1K PUT**, **$0.0004 / 1K GET**.
- Buckets: frontend bucket (~5 MB static), TF state (<1 MB), Athena output (grows unless lifecycled).
- **Effective cost: under $0.10/month** for this platform's footprint. Set a 30-day lifecycle on the Athena output prefix if you run CUR queries frequently.

### 2.8 CloudFront

- **Free tier (forever, post-Oct 2024): 1 TB data transfer + 10 M HTTPS requests per month.**
- Paid: $0.085 / GB to North America, $0.0100 / 10K HTTPS requests (reference rates).
- Frontend is ~1 MB of static assets per full page load.
- **Effective cost: $0/month** for hundreds of users — the free tier is generous relative to a dashboard app.

### 2.9 Cognito

- **Lite tier free: 10 000 MAU/month**, $0.0055 / MAU above that for the next 100K.
- Platform usage: every authenticated user counts as 1 MAU.
- **Effective cost: $0/month** for any team under 10 000 users.

Source: [Cognito pricing](https://aws.amazon.com/cognito/pricing/).

### 2.10 KMS (Customer-Managed Key)

| Item | Rate | Free tier |
|---|---|---|
| Key storage | $1.00 / key / month | — |
| API requests (Encrypt, Decrypt, GenerateDataKey) | $0.03 / 10 000 requests | 20 000 / mo |
| Automatic rotation | Included | — |

- Platform usage: 1 CMK shared across 3 DynamoDB tables. Every DynamoDB write/read triggers a KMS decrypt/encrypt call (transparent, handled by DynamoDB service). At 30K turns/mo → ~60K DynamoDB ops → ~60K KMS calls → **~$0.18/mo + $1 key storage = ~$1.18/mo total**.
- **At any realistic usage: ~$1/mo flat.** The per-request cost only matters above 200K DynamoDB ops/month.

Source: [KMS pricing](https://aws.amazon.com/kms/pricing/).

### 2.11 Bedrock Guardrails

| Item | Rate | Free tier |
|---|---|---|
| Content filters (PROMPT_ATTACK, sensitive info) | $0.75 / 1 000 text units (1 unit = 1 000 chars) | — |
| Topic policy (denied topics) | $0.10 / 1 000 text units | — |
| ApplyGuardrail API call | Included in per-unit pricing | — |

- Platform usage: 1 `ApplyGuardrail` call per user turn, evaluating ONLY the user's message (~50–500 chars = 1 text unit per turn). Content filter + topic policy fire on the same call.
- **Per-turn cost: ~$0.00085** ($0.75 + $0.10 = $0.85 per 1000 text units; 1 unit per turn).
- **At 30K turns/mo: ~$0.85 × 30 = ~$25.50/mo.** At typical small-team (2 250 turns/mo): **~$1.90/mo**.
- **Negligible relative to Bedrock model costs** (which are $0.07–$0.20 per turn).

Source: [Bedrock Guardrails pricing](https://aws.amazon.com/bedrock/pricing/) (under "Guardrails").

### 2.12 CloudWatch

| Item | Rate | Free tier |
|---|---|---|
| Logs ingestion | $0.50 / GB | 5 GB / mo |
| Logs storage | $0.03 / GB-month | 5 GB / mo |
| Logs Insights queries | $0.01 / 1 000 metrics analyzed | — |
| X-Ray traces ingested | $0.000005 / trace | 100 K / mo |
| X-Ray traces scanned | $0.0000005 / trace | 1 M / mo |

- Agent runtime logs at INFO level ≈ 1–5 KB per turn. 30K turns/month ≈ 150 MB ingest ≈ **$0.08/mo**.
- X-Ray: sampling defaults to 100% after `make deploy-auto` (see [`docs/observability-tuning.md`](observability-tuning.md)). High-traffic deployments should dial down to 10% to stay inside the 100 K free trace tier.

Source: [CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/).

### 2.11 Miscellaneous — health-events pipeline

Only applicable when the `health-events` tool is deployed (default-on).

| Item | Rate | Free tier |
|---|---|---|
| SQS Standard | $0.40 / M requests | 1 M requests / mo |
| EventBridge Scheduler | $1.00 / M invocations | 14 M / mo |
| EventBridge default bus (aws.health events) | **Free** — AWS management events | — |
| Haiku 4.5 enrichment | ~$0.0015 per event enriched | — (disable with `enrichment_model_id = ""`) |
| STS AssumeRole (cross-account alias roles) | **Free** | — |

- For most orgs, SQS + EventBridge stay inside free tier.
- **Haiku enrichment** is the one variable — 10-account org ≈ 10 events/mo ≈ $0.02. 1000-account org ≈ 5 000 events/mo ≈ **~$7.50**.

## 3. Bedrock — deeper dive (the 95% of the bill)

### 3.1 Per-turn Bedrock cost derivation

A simple single-sub-agent, single-tool-call turn ("how much did I spend last month?"):

| Step | Model | Input tok | Output tok | Cost |
|---|---|---|---|---|
| Supervisor delegates | Opus | ~2 050 | ~150 | $0.014 |
| finops-agent routes | Sonnet | ~850 | ~100 | $0.004 |
| cost-ops plans tool call | Sonnet | ~4 050 | ~80 | $0.013 |
| cost-ops responds to tool result | Sonnet | ~4 330 | ~250 | $0.017 |
| finops-agent wraps result | Sonnet | ~1 200 | ~50 | $0.004 |
| Supervisor final synthesis | Opus | ~2 300 | ~200 | $0.017 |
| **Total per simple turn** | | | | **~$0.07** |

A more typical mix across a real chat:

| Turn type | Bedrock cost | When |
|---|---|---|
| Simple single-sub-agent | **~$0.05–$0.08** | "how much did I spend last month?" |
| Multi-tool drill-down | **~$0.10–$0.20** | "show me the worst 5 non-compliant accounts" |
| Report generation (6 sections, parallel) | **~$0.40–$1.00** | Every template run |

**Most of the per-turn cost lives in Sonnet leaf calls**, because they carry the full tool schema (~3 000 tokens of JSON for a 10-tool MCP target) on every invocation. That's where prompt caching (tabled) would pay off the most.

### 3.2 Token breakdown — why it's not lower

A single cost-operations-agent invocation carries on input:

- System prompt (~1 500 tok) — agent-specific behavior, tool selection rules.
- Platform no-fabrication preamble (~300 tok, added by `agent_base.py`).
- Authoritative tool inventory (~30–100 tok, added by `agent_base.py`).
- 10 MCP tool schemas via the AgentCore Gateway (~3 000 tok of JSON).
- User's forwarded query (~100–300 tok).
- Memory history on continued sessions (~500–2 000 tok per turn).

The **tool schemas are the biggest static-but-repeated block.** Prompt caching applied here would drop leaf input cost by 60–80% on every turn in a session after the first. See `temp/optimizations.md` for the proposed implementation.

## 4. Scaling factors

### 4.1 Number of turns per user per day

Linear. Every turn costs roughly the same regardless of how many came before (session history grows gradually but `STM_ONLY` memory mode caps it).

Rule of thumb: **$0.086 per turn** using the 80/20 simple-vs-drilldown mix assumed in §5.

### 4.2 Number of users

Also linear, almost perfectly. Each user's chats are independent — no shared state that compounds across users. The exception is that report generation tends to be async / scheduled rather than per-user, so 20 users running 1 report each is roughly the same Bedrock spend as 1 user running 20 reports.

### 4.3 Number of accounts in the Organization

**Direct AWS cost impact: near-zero.** The platform's AWS APIs are either:
- One payer-account call returning consolidated data (Cost Explorer, tag compliance summary) — **does not scale with accounts**; or
- Explicitly scoped by the user (Tier 2 tag drill-down requires `account_ids`) — **scales with what the user asks for, not with org size**; or
- Free (Organizations, STS, tag APIs).

The exception is the health-events collector: an org-view deploy receives EventBridge events for every member account, and if Haiku enrichment is enabled, each event costs ~$0.0015. **A 1 000-account org with typical event volume pays ~$7–10/month** extra in Bedrock-Haiku for this. Disable with `enrichment_model_id = ""` if this is a concern.

**Indirect Bedrock impact: modest.** Tool responses get bigger with more accounts. A tag-governance summary response for a 5-account org is ~1 KB; for 500 accounts it's ~15 KB. That's an extra ~3 500 input tokens into the next turn's Bedrock call = **~$0.01 extra per drill-down turn** on Sonnet.

**Net effect:** account count is a distant second-order factor compared to turn count and report count. A 500-account org with a tag-heavy user pays maybe 10–30% more per turn for that specific workload, not 10× more.

### 4.4 Reports per month

Each report (4–6 sections running in parallel, each a mini chat with 1–3 tool calls) costs **~$0.40–$1.00 in Bedrock**. Highly sensitive to section count, tool-call depth, and prompt length in each section's spec.

Prompt caching would likely drop this to **~$0.20–$0.40 per report** (see `temp/optimizations.md`) — the shared supervisor prompt + leaf tool schemas are identical across all sections within the same 5-minute window.

### 4.5 Which tools get used

Some tools are free; two are not:

- **Cost Explorer API** at $0.01/call adds up if your users chat heavily about spending. A FinOps-focused team could see **$15–50/month just in CE API calls** on top of Bedrock.
- **Athena** at $5/TB scanned adds up only if queries are unscoped. With partition filters: cents.
- Every other tool (pricing, billing, health-events, network-resilience, tag-governance) hits AWS APIs that are **free to call**.

## 5. Scenarios

**Assumptions applied uniformly to every row:**

- Turn mix: **80% simple sub-agent turn ($0.07), 20% drill-down ($0.15)** → blended **$0.086/turn** in Bedrock.
- AgentCore Runtime **CPU-active time: ~8–10 vCPU-seconds per turn** (supervisor ~4s + orchestrator ~2s + leaf ~3s). Wall-clock is longer (~20–30s) but blocking-on-downstream time is not billed. At 1 vCPU + 2 GB per container → **~$0.00033/turn**.
- Cost Explorer API: **10% of turns make ~2 CE calls** → 0.2 calls × $0.01 = **$0.002/turn**.
- Reports: **~$0.60 Bedrock per report**, 3 tool calls per section.
- "Other" column: DynamoDB + Lambda + S3 + CloudFront + Cognito + CloudWatch + AgentCore Gateway + AgentCore Memory.
- us-east-1, global Bedrock endpoints, no prompt caching, Haiku enrichment ON.

| Scenario | Users | Turns/day | Turns/mo | Reports/mo | Bedrock | AgentCore Runtime | CE API | Other | **Monthly total** |
|---|---|---|---|---|---|---|---|---|---|
| **Hobbyist / demo** | 1 | 3 | 90 | 0 | ~$8 | ~$0.03 | ~$0.09 | ~$1 | **~$9** |
| **Daily driver** | 1 | 20 | 600 | 2 | ~$53 | ~$0.20 | ~$1.20 | ~$2 | **~$56** |
| **Small team** | 5 | 15 | 2 250 | 5 | ~$197 | ~$0.75 | ~$4.50 | ~$5 | **~$208** |
| **Medium team** | 20 | 15 | 9 000 | 20 | ~$787 | ~$3 | ~$18 | ~$10 | **~$818** |
| **Heavy usage** | 50 | 30 | 45 000 | 40 | ~$3 920 | ~$15 | ~$90 | ~$30 | **~$4 060** |

### Patterns to notice

- **Bedrock is 95–97% of the bill in every scenario.** Every optimisation decision starts there.
- **AgentCore Runtime is nearly free at any realistic scale** — peaks at ~$15/mo for 45K turns/mo. The billing model charges only for actual CPU consumption; I/O wait (supervisor blocking on a sub-agent HTTP call) is free. This is a critical detail: naive per-second wall-clock billing would be 4–5× higher.
- **Cost Explorer API** is the only free-tier-missing AWS API — it adds up to ~$90/mo at heavy scale and ~$18/mo at medium. FinOps-heavy orgs should keep an eye on this.
- Everything else collectively stays under $30/month even at the heavy-usage scenario.

### How reports shift the picture

At 40 reports/month × $0.60 = $24/mo in Bedrock from reports alone. For the **heavy** scenario that's less than 1% of the total bill; for the **small team** (5 reports/mo × $0.60 = $3) it's about 1.5%. **Prompt caching — tabled as a pending optimization — would disproportionately benefit report-heavy usage**, since the 4–6 parallel section workers share identical system prompts within the 5-minute cache window.

## 6. Per-module cost — what each feature adds

The scenarios in §5 show total platform spend. This section answers the different question: **"if I enable only this one module, what does it cost?"** — useful when deciding whether to turn on an optional feature, or when budgeting a minimal deploy.

Every module's cost is split into two layers:

- **Infrastructure**: standing AWS resources the module provisions (Lambda, DynamoDB, SQS, etc.) — mostly free-tier or cents/month.
- **Usage**: per-invocation cost when a user's chat actually reaches this module. Almost always dominated by Bedrock Sonnet-4.6 calls on the leaf agent plus any non-free downstream AWS API (Cost Explorer, Athena, Haiku enrichment).

Numbers below assume **us-east-1, global Bedrock endpoints, ~$0.086 blended Bedrock cost per turn that reaches a sub-agent** (derived in §3). "Per-turn" = one user question that lands in this module.

### 6.1 Always-on core (non-optional)

Deployed in every topology. Not a module you opt into — listed for completeness so you can see where the "other $1–10/mo" in §5 comes from.

| Item | Infrastructure / mo | Per-turn overhead | Notes |
|---|---|---|---|
| Supervisor runtime + AgentCore Memory | $0 idle | Bedrock Opus share of every turn (~$0.031 avg) | Memory store: ~$0.25 / 1 000 events. Long-term records typically a few cents/mo. |
| AgentCore Gateway + tool indexing | $0.01 flat | ~$0.00003 / turn | 36 tools indexed; 3–10 gateway calls per turn @ $0.005 / 1 000. |
| Cognito (Lite tier, MFA optional) | $0 under 10 000 MAU | — | |
| CloudFront + S3 frontend (HSTS enabled) | $0 (free tier) | — | 1 TB/mo egress covered. |
| CloudWatch Logs + X-Ray (30-day retention) | ~$0.08–$1 | — | Scales with turn count. Tune retention + sampling for dev. |
| DynamoDB (registry + reports + templates, CMK encrypted) | ~$0.05–$0.50 | negligible | On-demand; tables are tiny except reports/sessions. |
| KMS (CMK for DynamoDB encryption) | $1.00 flat | ~$0.003 / 1000 ops | 1 key, auto-rotation. Per-request cost negligible. |
| Bedrock Guardrail (ApplyGuardrail per turn) | — | ~$0.00085 / turn | Content filter + topic policy. Evaluates only user message. |

**Idle always-on baseline: ~$2–3/mo** regardless of usage (KMS key is the main standing cost).

### 6.2 `cost-operations-agent` (Cost Explorer + CUR/Athena + Cost Optimization Hub)

**What it adds when enabled**

| Item | Cost | Trigger |
|---|---|---|
| Leaf Lambdas (cost-explorer, cur-athena, cost-optimization-hub) | $0 (Lambda free tier) | Always — but only run on invocation |
| Leaf Bedrock (Sonnet 4.6) on every FinOps turn | ~$0.034 / turn | User asks a spend / CUR / COH question |
| **Cost Explorer API** | **$0.01 / request** — 1–3 requests per spending turn | FinOps turns only |
| Athena scans (if `cur-athena` enabled + CUR configured) | $5 / TB scanned | CUR queries only — well-scoped queries ~50–500 MB → cents |
| Cost Optimization Hub API | Free | COH enrollment required separately |

**Worked examples (this module alone, FinOps-only usage)**

| Usage | FinOps turns / mo | CE API calls | Monthly module cost |
|---|---|---|---|
| Occasional check-ins | 30 | 60 | Bedrock ~$2.60 + CE ~$0.60 = **~$3** |
| Active FinOps user | 300 | 600 | Bedrock ~$26 + CE ~$6 = **~$32** |
| FinOps-heavy team (5 users × 200 turns) | 1 000 | 2 000 | Bedrock ~$86 + CE ~$20 = **~$106** |

**Notes / callouts**

- **Cost Explorer API is the one paid AWS API we hit.** No free tier. Every cost-ops turn makes 1–3 calls. Watch this line on FinOps-heavy teams.
- **Athena cost is user-scoped.** The worker prompt enforces partition filters (`year`, `month`) on every query — well-written queries scan ≤500 MB. An accidentally unfiltered `SELECT *` on a multi-year CUR can scan TBs. Audit Athena workgroup metrics if bills surprise you.
- **COH is free** to call but requires enrollment in AWS Billing console first. `get_enrollment_status` short-circuits gracefully if not enrolled.

### 6.3 `pricing-agent` (Pricing catalog + Anomaly + Budgets)

| Item | Cost | Trigger |
|---|---|---|
| Leaf Lambdas (pricing, billing) | $0 (free tier) | Always |
| Leaf Bedrock (Sonnet 4.6) on every pricing turn | ~$0.034 / turn | User asks pricing / anomaly / budget question |
| AWS Pricing API | Free | Every pricing turn |
| Cost Anomaly Detection + Budgets APIs | Free | Turns that ask about anomalies / budgets |

**Monthly module cost: Bedrock only.** 100 pricing turns/mo ≈ **~$3.40**.

Cheapest module to enable — zero paid AWS APIs, every downstream call is free.

### 6.4 `health-events-agent` + collector pipeline

Most variable module. Cost depends on **deploy mode** (single-account vs org-view) and **event volume**, not on how often users chat.

**Infrastructure (standing, per month)**

| Item | Cost | Notes |
|---|---|---|
| EventBridge default bus (aws.health events) | $0 | Free — AWS management events. |
| SQS Standard queue + DLQ | $0 | 1 M requests/mo free tier covers any realistic event volume. |
| Collector Lambda | $0 | Well inside free tier. |
| DynamoDB `cloudops-health-events` | ~$0.05–$1 | Scales with org size. 180-day TTL caps growth. A 500-account org with ~500 events/mo: ~$1/mo DDB. |
| Haiku 4.5 enrichment (optional, default ON) | **~$0.0015 / event** | Writes `impactSummary` / `remediationHint` / `affectedResourceTypes` at ingest. Disable with `enrichment_model_id = ""`. |

**Infrastructure totals by org size, Haiku enrichment ON**

| Org size | Typical events / mo | Haiku enrichment | DDB | **Infrastructure / mo** |
|---|---|---|---|---|
| Single account | ~10 | ~$0.02 | $0.05 | **~$0.10** |
| 10-account org | ~50 | ~$0.08 | $0.10 | **~$0.20** |
| 100-account org | ~200 | ~$0.30 | $0.25 | **~$0.55** |
| 500-account org | ~500 | ~$0.75 | $0.80 | **~$1.55** |
| 1000-account org | ~1 000 | ~$1.50 | $1.50 | **~$3** |

**Usage (per user turn)**

Each question that lands in `health-events-agent`: ~$0.034 leaf Bedrock. No per-call AWS charges (DDB Query is in free tier at this volume).

**One-time backfill cost**

`make backfill-health DAYS=90` runs Haiku enrichment on up to 90 days of historical events. For a busy 500-account org this could be ~5 000 events × $0.0015 = **~$7.50 one-time**.

**Notes**

- **Haiku enrichment is the one cost dial worth knowing.** Disable for dev deploys or large orgs that don't need narrative fields — saves $1–3/mo at 1000-account scale. Trade-off: agent falls back to raw `description` which is often verbose and less useful.
- **SQS + EventBridge stay inside free tier** for virtually all real orgs — you'd need 1 M+ events/mo (1000+ accounts with very high event rate) to break out.

### 6.5 `network-resiliency-agent`

| Item | Cost | Trigger |
|---|---|---|
| Leaf Lambda (network-resilience MCP) + frontend REST Lambda | $0 (free tier) | MCP fires on DX questions; REST fires when DX visualizer is open |
| Leaf Bedrock (Sonnet 4.6) on every DX turn | ~$0.034 / turn | User asks DX question |
| AWS APIs (DirectConnect, EC2, NetworkManager, Health, CloudWatch, Pricing) | Free | ~25–30 calls per `discover_dx_topology` × N regions |
| CloudWatch `GetMetricData` (live BGP overlay, frontend REST) | In free tier for <1M metric queries/mo | Only while DX visualizer is open |

**Monthly module cost: Bedrock only.** 50 DX turns/mo ≈ **~$1.70**.

**Notes**

- `discover_dx_topology` can fire **25–30 AWS API calls per reachable region** (so ~125 calls for a 5-region topology). All those APIs are free. The worker prompt hard-gates re-discovery to once per conversation — follow it, or a curious user can chew through 100+ API calls answering basic questions.
- The visualizer polls live BGP metrics every 60 s while open — `GetMetricData` in the free tier covers this for any realistic number of open tabs.

### 6.6 `tag-governance-agent`

| Item | Cost | Trigger |
|---|---|---|
| Leaf Lambda (tag-governance) | $0 (free tier) | Every tag-governance turn |
| Leaf Bedrock (Sonnet 4.6) on every tag turn | ~$0.034 / turn | User asks tag / compliance / cost-allocation question |
| All AWS APIs (ResourceGroupsTagging, Organizations, Cost Explorer's `ListCostAllocationTags`, Resource Explorer) | Free | Every turn that reaches this module |
| STS AssumeRole to spoke accounts (Tier 2 fan-out) | Free | Only when user explicitly drills into specific account_ids |

**Monthly module cost: Bedrock only.** 100 tag-governance turns/mo ≈ **~$3.40**.

**Notes**

- **`ListCostAllocationTags` is part of Cost Explorer but doesn't charge the $0.01/request.** The $0.01/request applies to the main usage / forecast APIs (`GetCostAndUsage`, `GetCostForecast`, etc.); `ListCostAllocationTags` is a free metadata call.
- **Tier 2 response-size scaling is the only thing that moves this number.** A tag drill-down against a 500-account org returns ~15 KB of JSON; the next turn's Bedrock input cost picks that up (~$0.01 extra per drill-down). Immaterial.
- Like `pricing-agent`, this is one of the cheapest modules to enable — zero paid AWS APIs, pure Bedrock leaf cost per turn.

**Snapshot collector (`enable_tag_snapshots`, default ON when this tool is deployed)**

A scheduled Lambda (default `rate(6 hours)`) re-runs the expensive sweeps — Resource Explorer inventory + classification — through the tool Lambda and stores the responses in a snapshot table, so canonical tag queries answer from DynamoDB in milliseconds instead of a 3–60s live scan. Filtered queries and `force_refresh=true` still go live.

| Item | Cost | Notes |
|---|---|---|
| Collector Lambda (schedule) | $0 | 4 runs/day × ~60s × 256 MB ≈ 1 800 GB-s/mo — inside the 400 000 GB-s free tier. |
| Tool Lambda re-invocations | $0 | ~5 invocations per sweep (600/mo) — free tier. |
| EventBridge schedule rule | $0 | Scheduler rules are free. |
| DynamoDB `tag-compliance-snapshots` | ~$0.01–$0.05 | ~5 items rewritten 4×/day (≤400 KB each): ~120 write-units/day. 48h TTL caps storage under 2 MB. |
| All AWS scan APIs (Resource Explorer, Tagging, Organizations) | Free | Same free APIs the live path uses — the schedule just moves them off the user's turn. |

**Module cost delta: effectively $0** (worst case ~$0.05/mo DDB). What it buys: tag-governance turns skip the 3–60s scan, so the leaf's Bedrock turn also gets slightly *cheaper* (no retry/continuation prompts on slow tool calls), and report sections stop triple-running the same sweep. At `rate(1 hour)` the numbers stay in free tier; the knob that would eventually cost money is snapshotting a very large estate (approaching the 400 KB item cap) every few minutes — don't do that.

### 6.7 Side-by-side module summary

Comparing typical module usage at **small-team scale** (5 users, ~1 500 total turns/mo spread across the modules each user actually uses):

| Module | Typical turns / mo (5 users) | Standing infra / mo | Bedrock (leaf) / mo | Paid AWS APIs / mo | **Module total** |
|---|---|---|---|---|---|
| Core (always-on) | — | ~$1.50 | Opus share ~$46 | — | **~$48** |
| `cost-operations-agent` | 300 | — | ~$10 | ~$6 | **~$16** |
| `pricing-agent` | 100 | — | ~$3.40 | $0 | **~$3** |
| `health-events-agent` (100-acct org) | 60 | ~$0.55 | ~$2 | $0 | **~$3** |
| `network-resiliency-agent` | 40 | — | ~$1.40 | $0 | **~$1** |
| `tag-governance-agent` (+ snapshot collector) | 100 | ~$0.05 | ~$3.40 | $0 | **~$3** |
| **Small-team total (all modules enabled)** | | | | | **~$75–$80** |

(Note this is lower than the §5 "Small team" $208 because §5 uses a heavier 2 250 turns/mo assumption and counts reports. Both are valid framings — §5 is "full platform, typical chatty team"; this table is "what does each feature contribute".)

### Reading the table

- **Core always-on is ~60% of small-team cost**, driven by the Opus supervisor running on every turn regardless of which module handles it. Switching supervisor to Sonnet 4.6 (see §7) is the biggest lever for cutting this.
- **`cost-operations-agent` is the only module with a non-trivial paid AWS API line** (Cost Explorer at $0.01/req). Every other module's marginal cost is pure Bedrock leaf spend.
- **The "enabling a module costs ~$1–$16 per 100 turns it handles" rule** holds across all modules. Multiply by expected monthly usage to estimate.
- **`health-events-agent` is the only one with meaningful standing infrastructure cost**, and it scales with org size (events/mo), not user count.

## 7. Cutting the bill

Ordered by expected impact, highest first. Most are tracked in `temp/optimizations.md`:

1. **Enable prompt caching on every agent's system prompt.** Expected 40–60% reduction in Bedrock spend — and 50%+ reduction on report generation specifically. Single biggest lever on the list.
2. **Downgrade the supervisor from Opus 4.6 → Sonnet 4.6.** One-line change in `hierarchy.json`. Opus is ~5× cost per token of Sonnet. Evaluate against your actual delegation accuracy needs before committing.
3. **Scope Cost Explorer queries narrowly.** The worker prompt already enforces "ONE call per simple question, no unsolicited group-by" — audit a session's CE call count if bills look high.
4. **Disable Haiku enrichment** on low-value deploys: `enrichment_model_id = ""` in `terraform/config.auto.tfvars.json`.
5. **Tune X-Ray sampling down** from 100% to 10% once the stack is stable (see [`docs/observability-tuning.md`](observability-tuning.md)).
6. **Shorten CloudWatch log retention** to 7 days on dev stacks.
7. **Lifecycle S3 Athena query results** to expire after 30 days.
8. **Use `DEPLOY_MODE=gateway-only`** for environments that only need the tools, not the chat agents. Cuts Bedrock to $0.

## 8. What's NOT in this estimate

- **Your AWS workload costs.** This platform reads AWS APIs; it doesn't run your EC2 fleet, store your backups, or move your data. Those are separate bills and often much larger than what this platform adds.
- **Data transfer charges.** Assumed $0 — cross-account API calls are free, CloudFront egress is within free tier for typical usage.
- **One-time setup spend.** `make backfill-health DAYS=90` on a large org could briefly spike Haiku enrichment spend during the backfill; after that, normal rates.
- **Regional endpoints (+10%).** All Bedrock numbers assume global endpoints. For compliance-driven data-residency deployments, add 10% across all Bedrock lines.
- **Reserved capacity / Savings Plans.** All numbers are on-demand. Bedrock Provisioned Throughput at higher scale can change the Bedrock numbers materially — evaluate when monthly spend crosses ~$1 000/mo sustained.

## 9. Verify for yourself

- [AWS Pricing Calculator](https://calculator.aws/#/) — build a plan for the specific services above.
- [Bedrock pricing (AWS)](https://aws.amazon.com/bedrock/pricing/) and [model pricing (Anthropic)](https://platform.claude.com/docs/en/docs/about-claude/pricing) — per-model token rates.
- [AgentCore pricing](https://aws.amazon.com/bedrock/agentcore/pricing/).
- [Lambda](https://aws.amazon.com/lambda/pricing/) · [DynamoDB](https://aws.amazon.com/dynamodb/pricing/on-demand/) · [Athena](https://aws.amazon.com/athena/pricing/) · [Cost Explorer](https://aws.amazon.com/aws-cost-management/aws-cost-explorer/pricing/) · [CloudWatch](https://aws.amazon.com/cloudwatch/pricing/) · [Cognito](https://aws.amazon.com/cognito/pricing/) · [CloudFront](https://aws.amazon.com/cloudfront/pricing/) · [S3](https://aws.amazon.com/s3/pricing/) · [SQS](https://aws.amazon.com/sqs/pricing/) · [EventBridge](https://aws.amazon.com/eventbridge/pricing/).

All numbers in this doc are estimates. Actual spend will vary with your usage patterns, prompt lengths, response sizes, and report cadence. Re-run the worked examples with your own telemetry before committing to a budget.
