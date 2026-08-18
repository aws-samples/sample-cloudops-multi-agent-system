# Leaf agent references

One file per leaf agent. Each file is the end-to-end reference for that
agent's feature surface: what it does, how it's deployed, what APIs it
wraps, data model, known gotchas, and troubleshooting. Keep
user-facing operational content here; keep platform-internals content
in `../architecture.md`.

## Current agents

| File | Agent | Summary |
|---|---|---|
| [`cost-operations.md`](cost-operations.md) | `cost-operations-agent` | AWS spend analysis via Cost Explorer, CUR / Athena, and Cost Optimization Hub. Two deploy modes, CUR setup guide, COH enrollment gate. |
| [`pricing.md`](pricing.md) | `pricing-agent` | AWS Pricing catalog lookups, Cost Anomaly Detection, AWS Budgets status. One deploy mode — no cross-account needed. |
| [`health-events.md`](health-events.md) | `health-events-agent` | AWS Health events ingestion + query. Four deploy modes (single-account / org-mgmt / org-delegated / cross-account), rules-based risk scoring, LLM enrichment, backfill. |
| [`network-resiliency.md`](network-resiliency.md) | `network-resiliency-agent` | Direct Connect topology discovery + 22-rule resilience assessment + DX pricing. Shared rule engine across MCP and frontend REST Lambdas. |
| [`tag-governance.md`](tag-governance.md) | `tag-governance-agent` | Read-only org-wide tag compliance via Resource Explorer + Organizations Tag Policies, with remediation deep-links and cost-allocation activation health. Three deploy modes, requires Resource Explorer multi-account search. |
| [`cloudwatch.md`](cloudwatch.md) | `cloudwatch-agent` | Read-only CloudWatch alarm assistant — recommends best-practice alarms from a vendored metric catalogue and tunes thresholds from real metric history, emitting CloudFormation the user applies. Single-account or one cross-account spoke per deploy. |

## When to add a file here

Add a new reference file whenever a leaf agent has any of:

- Multiple deploy modes (single-account vs org-wide vs cross-account).
- An AWS Support-plan dependency or other pre-deploy gate the user has to clear.
- A non-trivial data model the user queries against (e.g. a DynamoDB
  table with GSIs, or an MCP-surfaced schema with tiering or scoping rules).
- Known gotchas that a user following the happy path will likely hit.

If the agent is a thin wrapper over one AWS API with no special setup,
its MCP tool entry in `tools.json` plus the agent prompt in
`hierarchy.json` is usually enough — no file needed.

## Template to follow

Both existing files share this section shape; mirror it when adding a new one:

1. **What the feature does** — one paragraph + one representative prompt
   + ASCII sketch of the pipeline if non-trivial.
2. **Deploy modes** — decision tree for which mode applies to the user.
3. **Prerequisites / bring-up** — AWS CLI commands to enable the
   feature on a cold account if any extra setup is required.
4. **Data model** — tool response shapes, DynamoDB rows with indexes
   (if applicable). Real JSON shapes, not prose descriptions.
5. **Report template** — if the agent ships a report template, list
   its sections and their tool calls here.
6. **Known gotchas / troubleshooting** — symptom → likely cause →
   fix, as a table.

Lead with the user-facing statement of what the feature does. Put
internals second. Skip any section that doesn't apply.
