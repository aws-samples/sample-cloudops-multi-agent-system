# Cost Operations agent — architecture, deploy modes, operations

End-to-end reference for the `cost-operations-agent`. Part of the
FinOps domain under `finops-agent` (peer of `pricing-agent`).

---

## 1. What the feature does

Answers spending questions using four complementary surfaces, from
narrowest to broadest:

- **Cost Explorer API** — monthly / daily cost and usage, optional
  group-by on any Cost Explorer dimension, period comparisons,
  forecasts. Default path for most questions.
- **CUR via Athena** — detailed line-item SQL against the Cost and
  Usage Report. Used only when Cost Explorer can't answer (specific
  resource IDs, custom joins, per-usage-type analysis).
- **Cost Optimization Hub** — savings recommendations (right-sizing,
  idle resources, Savings Plans, Reserved Instances) aggregated
  across the org.
- **Commitments** — risk-adjusted Savings Plan / Reserved Instance
  purchase sizing. The only surface that reaches
  `GetSavingsPlansPurchaseRecommendation` and
  `GetReservationPurchaseRecommendation`, so it is the only one that can
  size a commitment rather than just report existing coverage. Full
  reference: [`docs/skills/discounted-commitments.md`](../skills/discounted-commitments.md).

Representative prompts:

- `"How much did I spend last month?"`
- `"Break down February spend by service."`
- `"Compare my spend for Feb 2026 vs Jan 2026."`
- `"Forecast my spend for April."`
- `"Query CUR for my top 10 most expensive EC2 instances by hours."`
- `"What Savings Plans do you recommend?"`

Behavior:

```
"How much did I spend last month?"
  → supervisor → finops-agent → cost-operations-agent
    → cost-explorer___get_cost_and_usage(MONTHLY, no group_by)
    → "$2,995.88 USD"
```

Tool selection is deliberately conservative — ONE call for simple
spend questions, no automatic group-by, no proactive breakdowns. The
worker prompt hard-gates each additional tool on the user asking for
that specific analysis.

---

## 2. Deploy modes — pick one

Depends on which account owns the billing data.

```
Which account runs the cloudops stack?
├── Management (payer) account        → Mode A (mgmt-hosted)
└── Dedicated ops account             → Mode B (cross-account)
```

### Mode A — mgmt-hosted

cloudops runs in the management (payer) account. Cost Explorer
returns payer-consolidated data natively; nothing to configure.

- **What works:** full Cost Explorer API surface, org-wide spend
  queries, forecasts, comparisons. CUR/Athena if configured.
- **Terraform:** leave `CROSS_ACCOUNT_ROLE_ARN` unset in
  `config.auto.tfvars.json`.

### Mode B — cross-account

cloudops runs in a dedicated ops account. Cost Explorer is a
payer-only API, so the `cost-explorer` Lambda assumes a role in the
payer via `CROSS_ACCOUNT_ROLE_ARN`. Cost Optimization Hub uses its
own alias, `CROSS_ACCOUNT_ROLE_ARN_COH`, because COH can be enabled
on a delegated admin account that's separate from the payer.

- **What works:** full Cost Explorer + CUR (if Athena set up in the
  payer or delegated account). COH works when the COH role points
  at the enrollment account.
- **Terraform steps:**
  1. Create an IAM role in the **payer** account trusting the
     ops-account `cost-explorer` Lambda execution role. Grant:
     ```
     ce:GetCostAndUsage
     ce:GetCostForecast
     ce:GetDimensionValues
     ce:GetTags
     ```
  2. Optionally create a separate role in the **COH enrollment
     account** for Cost Optimization Hub; grant:
     ```
     cost-optimization-hub:ListEnrollmentStatuses
     cost-optimization-hub:ListRecommendations
     cost-optimization-hub:GetRecommendation
     cost-optimization-hub:ListRecommendationSummaries
     ```
  3. `make reconfigure-shared` — fill in
     `CROSS_ACCOUNT_ROLE_ARN` and (optional)
     `CROSS_ACCOUNT_ROLE_ARN_COH`.
  4. `make deploy-auto` in the ops account.

---

## 3. CUR / Athena prerequisites

`cur-athena` is opt-in. It requires a configured Cost and Usage
Report with Athena integration already set up — the agent does NOT
create the CUR pipeline. To enable:

1. Create / identify a CUR export to S3 with Athena integration (or
   use AWS Data Exports 2.0 equivalent).
2. Run the CloudFormation crawler AWS provides, which creates a Glue
   database + table over the CUR S3 prefix.
3. Set four shared-config values via `make reconfigure-shared`:
   ```
   CUR_DATABASE_NAME       (Glue database)
   CUR_TABLE_NAME          (Glue table)
   ATHENA_WORKGROUP        (existing workgroup, or "primary")
   ATHENA_OUTPUT_LOCATION  (s3:// URI for query results)
   ```
4. `make deploy-auto` so the `cur-athena` Lambda gets the env vars.

Without these set, the `cur-athena` tool is still deployed but
returns a configuration error on invocation — the agent's prompt is
aware and defers to Cost Explorer.

---

## 4. Cost Optimization Hub — opt-in + us-east-1

COH must be enabled in the AWS Billing console before the tool
returns data. The worker prompt enforces calling
`get_enrollment_status` first — if not enrolled, other calls
short-circuit with a clear "Cost Optimization Hub is not enabled"
error.

COH is a **us-east-1-only** API regardless of deployment region. The
Lambda hardcodes `region_name="us-east-1"`. Same pattern as
`pricing-agent`.

---

## 5. Data model — what tools return

### `get_cost_and_usage`

```
{
  "time_period": {"start": "2026-04-01", "end": "2026-05-01"},
  "granularity": "MONTHLY",
  "metrics": ["UnblendedCost"],
  "results": [
    {
      "time_period": {"Start": "...", "End": "..."},
      "total": {"UnblendedCost": {"Amount": "2995.88", "Unit": "USD"}},
      "groups": []
    }
  ],
  "grand_total": 2995.88
}
```

### `list_recommendations` (COH)

```
{
  "recommendations": [
    {
      "recommendation_id": "...",
      "account_id": "...",
      "region": "...",
      "resource_id": "...",
      "current_resource_type": "Ec2Instance",
      "recommended_resource_type": "Ec2Instance",
      "estimated_monthly_savings": 42.17,
      "estimated_savings_percentage": 38,
      "action_type": "Rightsize",
      "implementation_effort": "Low"
    }
  ],
  "count": 12,
  "total_estimated_monthly_savings": 520.11
}
```

### `generate_commitment_analysis` (commitments)

Returns a `report_markdown` field holding the complete pre-formatted
report, alongside the structured envelope (`recommendations`, `count`,
`total_estimated_monthly_savings`, `aws_best_case_monthly_savings`,
`reconciliation`, `existing_commitment_posture`, `blockers`). The agent
prompt requires emitting `report_markdown` verbatim rather than rebuilding
the table from the structured fields. Response shape and the other three
commitment tools:
[`docs/skills/discounted-commitments.md`](../skills/discounted-commitments.md).

### `start_query_execution` (CUR/Athena)

Synchronous — the Lambda waits for the Athena query to complete (up
to 60s) and returns the result rows directly. Caller should always
include partition filters (`year`, `month`) in the SQL to bound the
scan.

---

## 6. Report template — `finops_monthly_report`

The `finops_monthly_report` template calls this agent for its spend,
savings, anomalies, and forecast sections. See
[`src/agents/shared/report_templates/finops_monthly_report.json`](../../src/agents/shared/report_templates/finops_monthly_report.json).

---

## 7. Known gotchas

| Symptom | Likely cause | Fix |
|---|---|---|
| `AccessDeniedException` on `GetCostAndUsage` from an ops account | Cost Explorer is payer-only | Switch to Mode B; set `CROSS_ACCOUNT_ROLE_ARN` |
| COH returns empty even though resources exist | COH not enrolled in the account | Enable in AWS Billing console; re-run `get_enrollment_status` |
| Athena query hits timeout | Missing partition filter, full-CUR scan | Add `WHERE year='2026' AND month='04'` to the SQL |
| Cost Explorer shows different totals than the bill | Metric mismatch (UnblendedCost vs AmortizedCost vs NetAmortizedCost) + Credits/Refunds exclusion | Specify the metric explicitly; match what the finance team uses for reconciliation |
| Forecast returns an error for start_date in the past | `get_cost_forecast` requires future start_date | Use `get_cost_and_usage` for historical; forecast is future-only |
| Model called `group_by=["SERVICE"]` when the user only asked "how much" | Model embellishing beyond the ask | The worker prompt already gates this; if it recurs, tighten the "ONE call, no group_by unless asked" rule |
| Model answered a "should we buy an SP?" question from `cost-optimization-hub` alone | COH publishes commitment recommendations but cannot size one | The worker prompt routes all SP/RI purchase questions to the `commitments` tools; see [`docs/skills/discounted-commitments.md`](../skills/discounted-commitments.md) |
| Unexpected Cost Explorer charges after a commitment question | CE bills $0.01 per recommendation request; a full default sweep is 48 | Narrow `savings_plan_types` / `ri_services` on `generate_commitment_analysis` |
