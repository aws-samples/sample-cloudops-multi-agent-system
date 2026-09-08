---
name: discounted-commitments
description: "AWS discounted commitment sizing and renewal — Savings Plans and Reserved Instances sized to what a workload can actually sustain, not the AWS best case, plus which existing commitments expire soon and what to renew. Drives the AWS CLI's Cost Explorer, Cost Optimization Hub and reservation-inventory read-only APIs, then risk-adjusts the result into a report. Use when the user asks what Savings Plans or RIs to buy, how much a commitment could save, whether a commitment is worth it, whether existing commitments are being wasted, what is expiring or needs renewing, or to size/validate an RI/SP purchase across compute, database, and analytics services."
argument-hint: "[what do you want to know? e.g. 'what savings plans should we buy', 'is a 3-year all-upfront worth it', 'are our existing RIs wasted', 'what expires in the next 90 days']"
user-invokable: true
---

# AWS Discounted Commitments

Size achievable AWS commitment purchases — Savings Plans and Reserved Instances — and report what a workload can actually sustain rather than what the AWS API recommends in its best case.

Needs only the **AWS CLI v2** and read-only credentials. Nothing to install, nothing bundled.

The distinction this skill exists for: `GetSavingsPlansPurchaseRecommendation` assumes the lookback window repeats forever and recommends the commitment that maximizes savings under that assumption. On a spiky workload that means committing above the trough, which strands spend in quiet hours. The same response also carries the *minimum* and *average* hourly on-demand spend — so you can size to the quietest hour instead, and report what the over-commitment would have cost.

## Routing — read first

```
Does your host expose this analysis as a tool
(a "commitment analysis" / "size savings plans" / "commitment expiry" tool
returning a report)?
├── Yes → Call it. Emit its report verbatim. Nothing below is needed.
└── No
    ├── Can you run `aws` with read-only credentials? → Query it (below)
    └── No
        ├── Is a coding agent with a shell available? → Delegate (below)
        └── No → Stop. Say sizing needs Cost Explorer access you do not have.
```

Generic cost tools are **not** a substitute. Cost Explorer and Cost Optimization Hub tool wrappers expose spend, coverage, and COH's own recommendations, but neither exposes `GetSavingsPlansPurchaseRecommendation` / `GetReservationPurchaseRecommendation`. Without those APIs nothing can size a commitment — you can still report eligible spend and existing coverage, and you must then say plainly: *"Sizing and risk adjustment are unavailable here — these figures are AWS best case, unadjusted."*

Do **not** use this skill for rightsizing, idle-resource, or Graviton-migration findings — Cost Optimization Hub covers those. For general cost breakdowns, trends, forecasts, and anomalies, use a general FinOps skill.

## Prerequisites

- `aws --version` → 2.x. Confirm identity with `aws sts get-caller-identity`.
- Permissions: `ce:Get*`, `cost-optimization-hub:ListRecommendations`, `cost-optimization-hub:ListEnrollmentStatuses`, `sts:GetCallerIdentity`. Read-only suffices.
- For the expiry inventory (step 6) only, also: `savingsplans:DescribeSavingsPlans`, `ec2:DescribeReservedInstances`, `rds:DescribeReservedDBInstances`, `elasticache:DescribeReservedCacheNodes`, `redshift:DescribeReservedNodes`, `es:DescribeReservedInstances` (OpenSearch's IAM prefix is `es:`, not `opensearch:`), `memorydb:DescribeReservedNodes`. Sizing works without these — skip step 6 and say so.
- Cost Explorer enabled (default; ~24h to populate on a new account).
- Cost Optimization Hub enrollment is optional — without it, say the figures could not be reconciled.

**Every call in steps 1–5 needs `--region us-east-1`.** Cost Explorer purchase recommendations and Cost Optimization Hub are us-east-1-only regardless of where the resources live. The one exception is the reservation inventory in step 6: reservations are regional and must be queried in the region they were bought in. Add `--profile <name>` to every command if the user named a profile.

**Cost:** Cost Explorer bills **$0.01 per recommendation request**. Sweep narrow by default; widen only when asked. The step 3 posture calls and the step 6 `Describe*` calls are free — never quote them in a cost estimate.

**Production caution:** use the least-privileged read-only profile available. Commitment purchases are non-cancellable financial obligations — this report is input to a decision, never authorization for one.

## Query it

Enums, verified against the CLI: `--savings-plans-type` is `COMPUTE_SP` | `EC2_INSTANCE_SP` | `SAGEMAKER_SP` | `DATABASE_SP`; `--term-in-years` is `ONE_YEAR` | `THREE_YEARS`; `--payment-option` is `NO_UPFRONT` | `PARTIAL_UPFRONT` | `ALL_UPFRONT`; `--lookback-period-in-days` is `SEVEN_DAYS` | `THIRTY_DAYS` | `SIXTY_DAYS`; `--account-scope` is `PAYER` (whole org) | `LINKED` (this account).

### 1. Savings Plans sizing

One call per (type, term, payment). `--query` trims the response to the eight fields the method needs — send it, or you will pull tens of KB of instance detail into context.

```bash
aws ce get-savings-plans-purchase-recommendation --region us-east-1 \
  --savings-plans-type COMPUTE_SP --term-in-years ONE_YEAR \
  --payment-option NO_UPFRONT --lookback-period-in-days THIRTY_DAYS \
  --account-scope PAYER \
  --query 'SavingsPlansPurchaseRecommendation.{
      commit_hr: SavingsPlansPurchaseRecommendationSummary.HourlyCommitmentToPurchase,
      savings_mo: SavingsPlansPurchaseRecommendationSummary.EstimatedMonthlySavingsAmount,
      savings_pct: SavingsPlansPurchaseRecommendationSummary.EstimatedSavingsPercentage,
      ondemand_mo: SavingsPlansPurchaseRecommendationSummary.CurrentOnDemandSpend,
      detail: SavingsPlansPurchaseRecommendationDetails[].{
          floor_hr: CurrentMinimumHourlyOnDemandSpend,
          avg_hr: CurrentAverageHourlyOnDemandSpend,
          upfront: UpfrontCost, est_util: EstimatedAverageUtilization,
          commit_hr: HourlyCommitmentToPurchase,
          spec: SavingsPlansDetails}}'
```

`SavingsPlansDetails` carries `InstanceFamily` and `Region` for an
`EC2_INSTANCE_SP` — the two things that plan is pinned to, and therefore the two
things a purchase has to name. For a `COMPUTE_SP` it comes back empty, which is
the plan being flexible by design, not data going missing: report it as *any
instance family*.

Sweep several permutations in **one** Bash call rather than one call each — same billing, far fewer round-trips:

```bash
for term in ONE_YEAR THREE_YEARS; do for pay in NO_UPFRONT ALL_UPFRONT; do
  echo "== COMPUTE_SP $term $pay"
  aws ce get-savings-plans-purchase-recommendation --region us-east-1 \
    --savings-plans-type COMPUTE_SP --term-in-years "$term" --payment-option "$pay" \
    --lookback-period-in-days THIRTY_DAYS --account-scope PAYER \
    --query 'SavingsPlansPurchaseRecommendation.SavingsPlansPurchaseRecommendationSummary.[HourlyCommitmentToPurchase,EstimatedMonthlySavingsAmount,EstimatedSavingsPercentage]' \
    --output text
done; done
```

**Default scope: `COMPUTE_SP` across both terms × `NO_UPFRONT`/`ALL_UPFRONT` — 4 calls, $0.04.** Compute SPs are the flexible instrument that fits most accounts. Add `EC2_INSTANCE_SP` when the fleet is stable and single-family, `SAGEMAKER_SP`/`DATABASE_SP` only when that spend exists. Add `PARTIAL_UPFRONT` only on request — it rarely wins and doubles the sweep.

### 2. Reserved Instance sizing

`--service` takes exactly these 8 values, no others:

`Amazon Elastic Compute Cloud - Compute`, `Amazon Relational Database Service`, `Amazon Redshift`, `Amazon ElastiCache`, `Amazon Elasticsearch Service`, `Amazon OpenSearch Service`, `Amazon MemoryDB Service`, `Amazon DynamoDB Service`

```bash
aws ce get-reservation-purchase-recommendation --region us-east-1 \
  --service "Amazon Relational Database Service" \
  --term-in-years ONE_YEAR --payment-option NO_UPFRONT \
  --lookback-period-in-days THIRTY_DAYS --account-scope PAYER \
  --query 'Recommendations[0].{
      savings_mo: RecommendationSummary.TotalEstimatedMonthlySavingsAmount,
      savings_pct: RecommendationSummary.TotalEstimatedMonthlySavingsPercentage,
      detail: RecommendationDetails[].{
          buy_units: RecommendedNumberOfInstancesToPurchase,
          floor_units: MinimumNumberOfInstancesUsedPerHour,
          avg_units: AverageNumberOfInstancesUsedPerHour,
          upfront: UpfrontCost, monthly: RecurringStandardMonthlyCost,
          break_even_mo: EstimatedBreakEvenInMonths, util: AverageUtilization,
          savings_mo: EstimatedMonthlySavingsAmount,
          spec: InstanceDetails, capacity: ReservedCapacityDetails}}'
```

**`spec` is not optional.** `RecommendationSummary` gives one total across every
line item, and a reservation only discounts usage matching its *exact*
specification — so "buy 4 RDS reservations" is a budget, not an order. Report one
row per `RecommendationDetails` entry, each naming what to buy, and say the total
spans several specifications when it does.

`InstanceDetails` holds exactly one service-specific sub-object, and each names
its fields differently:

| Service | Sub-object | Read |
|---|---|---|
| EC2 | `EC2InstanceDetails` | `InstanceType`, `AvailabilityZone`, `Platform`, `Tenancy` |
| RDS / Aurora | `RDSInstanceDetails` | `InstanceType`, **`DeploymentOption`** (`Multi-AZ` / `Single-AZ`), `DatabaseEngine`, `DatabaseEdition`, `LicenseModel` |
| ElastiCache | `ElastiCacheInstanceDetails` | `NodeType`, `ProductDescription` |
| Redshift | `RedshiftInstanceDetails` | `NodeType` |
| MemoryDB | `MemoryDBInstanceDetails` | `NodeType` |
| OpenSearch / Elasticsearch | `ESInstanceDetails` | `InstanceClass` **+** `InstanceSize` — there is no `InstanceType` field; join them |
| DynamoDB | `DynamoDBCapacityDetails`, under `ReservedCapacityDetails` (**not** `InstanceDetails`) | `CapacityUnits`, `Region` |

Every sub-object except `ESInstanceDetails` and `DynamoDBCapacityDetails` also
carries `Family`, `Region`, `CurrentGeneration` and `SizeFlexEligible`. Report the
last two per line: `SizeFlexEligible: true` means the recommended size is not
binding (the discount follows any size in the family), and
`CurrentGeneration: false` means a three-year commitment locks the account out of
the cheaper current generation for the whole term.

For **EC2 only**, add `--service-specification OfferingClass=STANDARD`; RDS, Redshift and the rest reject it. For **DynamoDB**, the unit fields are named `RecommendedNumberOfCapacityUnitsToPurchase` / `MinimumNumberOfCapacityUnitsUsedPerHour` / `AverageNumberOfCapacityUnitsUsedPerHour` instead. Query only services the account actually uses — check step 4 first.

### 3. Existing commitment posture — run this before quoting any savings

Four calls, not billed as recommendations. Substitute real dates (30 days back → today):

```bash
aws ce get-savings-plans-coverage --region us-east-1 \
  --time-period Start=2026-08-04,End=2026-09-03 --granularity MONTHLY \
  --query 'SavingsPlansCoverages[-1].Coverage.[CoveragePercentage,OnDemandCost]' --output text

aws ce get-savings-plans-utilization --region us-east-1 \
  --time-period Start=2026-08-04,End=2026-09-03 --granularity MONTHLY \
  --query 'Total.Utilization.[UtilizationPercentage,UnusedCommitment]' --output text

aws ce get-reservation-coverage --region us-east-1 \
  --time-period Start=2026-08-04,End=2026-09-03 --granularity MONTHLY \
  --query 'Total.CoverageHours.[CoverageHoursPercentage,OnDemandHours]' --output text

aws ce get-reservation-utilization --region us-east-1 \
  --time-period Start=2026-08-04,End=2026-09-03 --granularity MONTHLY \
  --query 'Total.[UtilizationPercentage,UnusedHours,RealizedSavings]' --output text
```

All-zero results mean **no commitment exists**, not 0% utilization on a broken one. Say which it is.

### 4. Eligible spend — what is even commitable

```bash
aws ce get-cost-and-usage --region us-east-1 \
  --time-period Start=2026-07-05,End=2026-09-03 --granularity MONTHLY \
  --metrics UnblendedCost --group-by Type=DIMENSION,Key=SERVICE \
  --query 'ResultsByTime[].Groups[?to_number(Metrics.UnblendedCost.Amount)>`1000`].[Keys[0],Metrics.UnblendedCost.Amount]' \
  --output text
```

`to_number()` is required, not decoration: `Amount` is a JSON string, and comparing a string to a number raises `TypeError: '>' not supported` instead of filtering.

Run this first when the user has no specific target: an account whose bill is serverless, storage and support has nothing to commit against, and you can say so for $0.

### 5. Reconciliation (optional second opinion)

```bash
aws cost-optimization-hub list-enrollment-statuses --region us-east-1 \
  --query 'items[0].status' --output text

aws cost-optimization-hub list-recommendations --region us-east-1 \
  --filter '{"actionTypes":["PurchaseSavingsPlans","PurchaseReservedInstances"]}' \
  --query 'items[].[currentResourceType,estimatedMonthlySavings]' --output text
```

`NOT_ENROLLED` or an empty list is fine — report that reconciliation was unavailable.

### 6. Expiry inventory — what is already committed, and when it lapses

Run this whenever the user asks what is expiring or what to renew, and before recommending any purchase: a commitment that lapses next month returns that spend to on-demand rates, and expiry is the one moment resizing costs nothing. Free — these are `Describe*` calls, not billed recommendation requests.

**Savings Plans are account-level and live on the global us-east-1 endpoint. Reservations are regional and only appear in the region they were bought in** — a us-east-1 sweep finds nothing for an ap-northeast-1 fleet. Query each region the account actually runs in, and say which regions you swept.

Savings Plans — one call, `end` comes back directly:

```bash
aws savingsplans describe-savings-plans --region us-east-1 \
  --states active payment-pending \
  --query 'savingsPlans[].[savingsPlanId,savingsPlanType,commitment,end,state,paymentOption,ec2InstanceFamily,region]' \
  --output text
```

`ec2InstanceFamily` and `region` are populated only for an `EC2Instance` plan;
blank on a `Compute` plan is correct.

Reservations — one call per (service, region). Only EC2 returns an end date; the rest return `StartTime` plus `Duration` in **seconds**, so derive `end = StartTime + Duration` yourself (31536000s = 1 year, 94608000s = 3 years):

```bash
REGION=ap-northeast-1

aws ec2 describe-reserved-instances --region $REGION \
  --query 'ReservedInstances[?State==`active`].[ReservedInstancesId,InstanceType,InstanceCount,End,OfferingType,Scope,AvailabilityZone,ProductDescription,OfferingClass,InstanceTenancy]' \
  --output text

aws rds describe-reserved-db-instances --region $REGION \
  --query 'ReservedDBInstances[?State==`active`].[ReservedDBInstanceId,DBInstanceClass,DBInstanceCount,StartTime,Duration,MultiAZ,ProductDescription]' \
  --output text

aws elasticache describe-reserved-cache-nodes --region $REGION \
  --query 'ReservedCacheNodes[?State==`active`].[ReservedCacheNodeId,CacheNodeType,CacheNodeCount,StartTime,Duration,ProductDescription]' \
  --output text

aws redshift describe-reserved-nodes --region $REGION \
  --query 'ReservedNodes[?State==`active`].[ReservedNodeId,NodeType,NodeCount,StartTime,Duration,ReservedNodeOfferingType]' \
  --output text

aws opensearch describe-reserved-instances --region $REGION \
  --query 'ReservedInstances[?State==`active`].[ReservedInstanceId,InstanceType,InstanceCount,StartTime,Duration,PaymentOption]' \
  --output text

aws memorydb describe-reserved-nodes --region $REGION \
  --query 'ReservedNodes[?State==`active`].[ReservationId,NodeType,NodeCount,StartTime,Duration]' \
  --output text
```

The id, count and type fields are named differently in every one of these — copy them as written rather than reusing EC2's. A typo yields rows with blank ids and no expiry rather than an error.

The trailing fields are what a **renewal has to match**, and they are the whole
point of listing them: an RDS reservation covers one deployment option and one
engine, and an EC2 reservation is pinned to a single Availability Zone when
`Scope` is `Availability Zone`. Renew against a different value and the discount
silently does not apply. `MultiAZ` comes back as a JSON **boolean**: `true` is
Multi-AZ, `false` is Single-AZ, and an absent field means AWS did not report it —
do not read absence as Single-AZ. OpenSearch and MemoryDB reservations expose no
engine or product field at all, so their spec is the node type alone.

**DynamoDB reserved capacity cannot be inventoried at all** — no describe API exists for it, in any SDK. If the account uses it, say so explicitly rather than reporting "nothing expiring".

Then read the renewal rules in [`reference/method.md`](reference/method.md) to turn each expiring commitment into renew / renew-smaller / let-lapse. Utilization is published only account-wide, never per commitment, so every verdict is directional — state that once, in the section, and do not imply otherwise per row.

## Then risk-adjust

Read [`reference/method.md`](reference/method.md) and apply it. **It is required, not optional** — the AWS numbers are unadjusted best case, and emitting them as achievable is the exact error this skill exists to prevent. It gives you the volatility bands, the commitment formula, break-even and waste-exposure arithmetic, the posture gate, and the reconciliation thresholds.

Then format per [`reference/output-template.md`](reference/output-template.md).

Show your arithmetic for each finding — the trough÷average ratio, the band it lands in, and the resulting commitment. A reviewer must be able to recheck a figure without re-querying AWS.

## Delegate

Hand the coding agent this brief:

> "Use the `discounted-commitments` skill to size AWS commitments for {{ACCOUNT_OR_PROFILE}} and report what expires within {{HORIZON_DAYS|90}} days. Read its `SKILL.md` and `reference/method.md`, run the Cost Explorer queries with `--region us-east-1` and read-only credentials, and run the step 6 reservation inventory in {{REGIONS}}. Return the finished markdown report. Read-only: no purchases, no renewals, no `StartCommitmentPurchaseAnalysis`. Report existing-commitment blockers and expiring commitments before any savings figure, name the regions you swept, and list any query that failed."

Interpret what they return; if it carries an error (no credentials, `AccessDenied`, expired SSO), pass it through with the fix from the Failure modes table rather than retrying in your own sandbox.

## Constraints — do not violate

- **Read-only.** Only `ce:Get*`, `cost-optimization-hub:List*`, the `Describe*` reads in step 6, and `sts:GetCallerIdentity`. Never `CreateSavingsPlan`, `PurchaseReservedInstances*`, `StartCommitmentPurchaseAnalysis`, `ModifyReservedInstances`, `DeleteQueuedSavingsPlan`, `ReturnSavingsPlan`, or any other mutating or billable API. This skill *sizes* and *reports on* commitments; it never buys, renews, modifies or cancels one.
- **Never present the AWS best case as achievable.** Quote the risk-adjusted figure as the recommendation and the AWS figure as the ceiling, in that order.
- **Never fabricate figures.** Every number traces to a command above or to arithmetic you show. A query you did not run is not a number you have.
- **Lead with blockers.** Under-utilized existing commitments come before the savings figure, always. A commitment expiring inside 30 days outranks any new purchase — it is a deadline, not an option.
- **Never recommend a reservation without its specification.** Instance type, and the deployment option for RDS/Aurora (Multi-AZ vs Single-AZ) plus the Availability Zone for a zonal EC2 reservation, decide whether the discount applies at all. A unit count with no spec is not a recommendation anyone can act on — and never present a total that spans several specifications as a single purchase.
- **Never imply per-commitment utilization.** AWS publishes it account-wide only. Quoting one commitment as "98% utilized" when that is the account figure is a fabricated attribution.
- **Do not average Cost Explorer and Cost Optimization Hub when they disagree materially.** Identify the cause. A blended number is defensible to nobody.
- **Region is pinned** to us-east-1 for every Cost Explorer, Cost Optimization Hub and Savings Plans command. Only the step 6 reservation inventory varies by region, and it must name the regions swept.
- **No credential exfiltration.** Never emit access keys, session tokens, or SSO refresh tokens. Account ID and profile name are fine.
- **Report, don't authorize.** Close with: commitments are non-cancellable, verify in the console, and confirm the workload is not scheduled for migration or decommissioning within the term.

## Failure modes — handle explicitly

| Situation | Behavior |
|-----------|----------|
| `aws sts get-caller-identity` fails | Stop. Tell the user to authenticate (`aws sso login` or set credentials). Produce no report. |
| `aws` missing or 1.x | Stop. `aws --version` must show 2.x; v1 lacks `cost-optimization-hub` entirely. |
| `DataUnavailableException`, blank message | Cost Explorer returns it with an empty `Message` when the account holds no commitment of that kind. Report "no existing commitment of this type", not a blank error. |
| `ValidationException` on `--service` | The name is not one of the 8 above. Do not guess — copy the API's own "Supported value(s)" list from the error. |
| COH not enrolled | Continue. State that figures rest on Cost Explorer alone. |
| Throttling / `AccessDenied` on some queries | Continue with what succeeded. List every failed query and present totals explicitly as a lower bound. |
| A `$0` / no-recommendation result | A real result. Check eligible spend (step 4) and say why there is nothing to commit against. |
| Posture queries all fail | Say posture could not be measured and that recommendations are unvalidated against existing commitments. |
| Member account, `PAYER` scope | The figure is org-aggregated and not purchasable by that account. Re-run with `--account-scope LINKED`. |
| Expiry inventory returns nothing | Distinguish "no commitment exists" from "wrong region" — reservations only appear in the region they were bought in. Name the regions you swept before concluding nothing expires. |
| `AccessDenied` on a `Describe*` | Report the expiry section as partial, naming the families you could not read. Do not report the surviving families as the complete picture. |
| An active commitment whose end date has already passed | Real and worth flagging on its own: that spend is already back at on-demand rates. Do not fold it in with future expiries. |

## Common mistakes

- **The 4th Savings Plan type is `DATABASE_SP`.** Not `MACHINE_LEARNING_SP` — that value does not exist and the call fails.
- **Do not guess RI service names.** They are inconsistent: `Amazon MemoryDB Service` and `Amazon DynamoDB Service` carry a ` Service` suffix that `Amazon Redshift` does not. SageMaker, Lambda, Fargate, Aurora, CloudFront, S3, Neptune, DocumentDB, MSK, Kinesis and Timestream are all rejected.
- **Cost Explorer returns money and percentages as JSON strings**, and `""` for absent values. Treat empty string as "no data", not zero.
- **`OfferingClass` is EC2-only.** Sending it elsewhere is a `ValidationException`.
- **`PAYER` aggregates the organization.** A member account cannot purchase a payer-scoped recommendation.
- **Don't drop `--query`.** A bare recommendation response runs to tens of KB of per-instance detail.
- **Don't re-query for a number you already have.** Each recommendation request costs $0.01; note results as you go.
- **Only EC2 reservations return an end date.** Everywhere else it is `StartTime` + `Duration` seconds. Reporting a blank expiry because you looked for `End` is how a lapsing commitment gets missed.
- **`RecommendationSummary` is a sum, not a purchase.** One RDS recommendation routinely spans `db.r6g.large Multi-AZ` and `db.t4g.medium Single-AZ`. Reporting only the total hides which to buy, and the two are not interchangeable.
- **OpenSearch has no `InstanceType` in its recommendation detail** — `ESInstanceDetails` splits it across `InstanceClass` and `InstanceSize`. Looking for `InstanceType` there returns nothing and reads as "no spec available".
- **DynamoDB's spec is not under `InstanceDetails`** — it is `ReservedCapacityDetails.DynamoDBCapacityDetails`, and it has capacity units rather than an instance.
- **A Savings Plan's `commitment` is dollars per hour, not a unit count.** Multiply by 730 for a monthly figure; reading `5.5` as "5.5 units" understates the exposure by roughly 1000x. Reservation counts are the opposite — they are units, and converting them to money needs per-instance pricing you have not queried.

## Layout

```
discounted-commitments/
├── SKILL.md                      # this file — routing, queries, constraints
└── reference/
    ├── method.md                 # required: risk adjustment + guards
    └── output-template.md        # report structure and field names
```

Three markdown files, no code and no dependencies beyond the AWS CLI. Copy the directory anywhere — into another repo, another agent's skill folder — and it works unchanged.
