# Discounted Commitments — skill and MCP tool reference

End-to-end reference for the `discounted-commitments` skill
([`skills/discounted-commitments/`](../../skills/discounted-commitments/))
and the `commitments` Lambda MCP tool
([`src/lambda/mcp/commitments/`](../../src/lambda/mcp/commitments/)) that
implements the same method inside the platform.

Part of the FinOps domain: the MCP tool binds to `cost-operations-agent`
under `finops-agent`. See
[`docs/agents/cost-operations.md`](../agents/cost-operations.md) for that
agent's other three tool surfaces.

---

## 1. What the feature does

Sizes AWS discounted commitments — Savings Plans and Reserved Instances —
to what a workload can actually sustain, then reports that instead of the
number AWS returns.

The gap it exists to close: `GetSavingsPlansPurchaseRecommendation`
assumes the lookback window repeats forever and returns the commitment
that maximizes savings under that assumption. On a spiky workload that
means committing above the trough, and every quiet hour strands the
difference. The same API response also carries the **minimum** and
**average** hourly on-demand spend, which is enough to size against the
quietest hour instead — and to quantify what the AWS figure would have
wasted.

It also answers the other half of the commitment question — **what is already
committed, when it lapses, and what to renew.** A commitment that expires
unnoticed returns its covered spend to on-demand rates silently, and expiry is
the one moment a commitment can be resized at zero switching cost.

Representative prompts:

- `"What Savings Plans should we buy?"`
- `"Is a 3-year all-upfront worth it?"`
- `"Are our existing RIs being wasted?"`
- `"How much could we save with a Compute Savings Plan?"`
- `"Size an RDS reservation for us."`
- `"What commitments expire in the next 90 days?"`
- `"Which RIs should we renew?"`

Pipeline:

```
"What Savings Plans should we buy?"
  → supervisor → finops-agent → cost-operations-agent
    → commitments___generate_commitment_analysis()          (one call)
        ├─ sweep      16 SP + 32 RI recommendation permutations, every page
        │             (6 threads, 150 s budget)
        ├─ risk-adjust each line down to its own measured floor
        ├─ pools      one purchase per usage pool; the rest are alternatives
        ├─ expiry     active + queued inventory per region (opt-in, free)
        ├─ posture    SP + per-service RI coverage/utilization, COH, spend
        ├─ reconcile  like-for-like against Cost Optimization Hub
        └─ render     complete markdown report (or error + hint if every
                      recommendation query failed)
    → emit report_markdown verbatim
```

Two properties are load-bearing and worth stating up front:

- **Read-only, always.** Every call is a `Get*` / `List*` / `Describe*`.
  Nothing is purchased, renewed or cancelled, and no billable analysis job is
  started. The output is input to a purchase decision, never authorization
  for one.
- **Blockers precede savings.** Section order in the report is
  deliberate: a reader cannot reach the savings total without passing
  the existing-commitment health check. Buying on top of an
  under-utilized commitment compounds waste rather than reducing it.

---

## 2. Two artifacts, one method

This feature ships twice, on purpose, and the two copies are not
interchangeable.

| | Skill | MCP tool |
|---|---|---|
| Path | `skills/discounted-commitments/` | `src/lambda/mcp/commitments/` |
| Form | 3 markdown files, 985 lines, **zero code** | handler + 9 Python modules, 4,992 lines |
| Dependency | AWS CLI v2 + read-only credentials | Lambda runtime, boto3, `shared.cross_account` |
| Who executes | The host agent, via Bash | The Lambda, called through the gateway |
| Arithmetic by | The agent, following `reference/method.md` | `commitments/analyze.py` |
| Portable | Yes — copy the directory anywhere | No — platform-coupled |
| Tests | None (no code to test) | 409 tests, `tests/unit/test_commitments_*.py` |

The skill is the portable expression: it must run in any coding agent
with a shell, with nothing installed, so it drives the AWS CLI and states
the arithmetic as instructions. The MCP tool is the fast, deterministic
expression: the same method in Python, so the model makes one call
instead of orchestrating dozens of Cost Explorer requests and doing
floating-point work in its head.

Consequence to keep in mind when editing: **the band constants and
formulas exist in both places.** `reference/method.md` and
`commitments/analyze.py` must agree. A change to one is a change to both,
and only the Python side has tests to catch drift.

---

## 3. Execution paths — how the skill routes

`SKILL.md` opens with this tree, evaluated top-down:

```
Does your host expose this analysis as a tool?
├── Yes → call it, emit its report verbatim. Nothing else needed.
└── No
    ├── Can you run `aws` with read-only credentials? → query it
    └── No
        ├── Is a coding agent with a shell available? → delegate
        └── No → stop; say sizing needs Cost Explorer access you lack
```

**Generic cost tools are not a substitute.** Cost Explorer and Cost
Optimization Hub wrappers expose spend, coverage, and COH's own
recommendations — but neither exposes
`GetSavingsPlansPurchaseRecommendation` or
`GetReservationPurchaseRecommendation`. Without those two APIs nothing
can size a commitment. In that situation the skill still reports eligible
spend and existing coverage, and is required to say plainly: *"Sizing and
risk adjustment are unavailable here — these figures are AWS best case,
unadjusted."*

Out of scope by design: rightsizing, idle resources, and Graviton
migration (Cost Optimization Hub covers those), and general cost
breakdowns, trends, forecasts, and anomalies (use `/finops-analysis`).

---

## 4. Prerequisites and permissions

### Skill path (AWS CLI)

- `aws --version` → **2.x**. v1 has no `cost-optimization-hub` command at
  all.
- `aws sts get-caller-identity` must succeed. If it fails the skill stops
  and produces no report rather than guessing.
- Cost Explorer enabled (on by default; takes ~24h to populate on a new
  account).
- Cost Optimization Hub enrollment is **optional** — without it the skill
  reports that reconciliation was unavailable and continues.

### IAM — read-only is sufficient

```
ce:GetSavingsPlansPurchaseRecommendation
ce:GetReservationPurchaseRecommendation
ce:GetSavingsPlansCoverage
ce:GetSavingsPlansUtilization
ce:GetReservationCoverage
ce:GetReservationUtilization
ce:GetCostAndUsage
cost-optimization-hub:ListEnrollmentStatuses
cost-optimization-hub:ListRecommendations
cost-optimization-hub:GetPreferences
savingsplans:DescribeSavingsPlans
ec2:DescribeReservedInstances
rds:DescribeReservedDBInstances
elasticache:DescribeReservedCacheNodes
redshift:DescribeReservedNodes
es:DescribeReservedInstances
memorydb:DescribeReservedNodes
sts:GetCallerIdentity
```

This is exactly the `iam_actions` list on the `commitments` entry in
[`src/lambda/mcp/tools.json`](../../src/lambda/mcp/tools.json) — the two
paths are permission-identical, which is what makes a skill run a valid
rehearsal for the deployed tool. `GetPreferences` reads the term and payment
option Cost Optimization Hub prices at, so the reconciliation compares like with
like (§6).

The seven `Describe*` actions serve the expiry inventory only. Sizing works
without them: omit `regions` and the tool makes zero inventory calls and
returns `expiry: null`. Note that **OpenSearch's IAM prefix is `es:`**, not
`opensearch:` — the API is `opensearch describe-reserved-instances` but the
permission is `es:DescribeReservedInstances`.

In a cross-account deployment (cost-operations Mode B) the same list must be
granted to the **payer-account role**, and that role's trust policy must name
the commitments tool's role — see
[`docs/agents/cost-operations.md`](../agents/cost-operations.md#mode-b--cross-account).

### Region is pinned to us-east-1 — except reservations

Cost Explorer purchase recommendations and Cost Optimization Hub resolve
**only** in us-east-1, regardless of where the resources live or where
the stack is deployed. Savings Plans use a global endpoint that also resolves
in us-east-1. Every such CLI command in `SKILL.md` carries
`--region us-east-1`; the Lambda hardcodes `CE_REGION = COH_REGION =
"us-east-1"` in `commitments/api.py`. Same pattern as `pricing-agent`.

**Reservations are the exception.** They are regional resources and only appear
in the region they were purchased in, so the expiry inventory takes an explicit
`regions` list and sweeps one call per (family, region). A us-east-1-only sweep
of an ap-northeast-1 fleet finds nothing and would otherwise report "nothing
expiring" — which is why the report always names the regions it swept. Savings
Plans are account-level and are fetched **once**, not per region; multiplying
them would return the same plans N times and inflate every total by N.

### Cost

Cost Explorer bills **$0.01 per API request** — recommendation, coverage,
utilization and cost-and-usage alike, and **per page**: a LINKED-scope
recommendation for a 250-account organization is three billed requests. Cost
Optimization Hub and the inventory `Describe*` calls are free.

The tool counts requests at the client rather than estimating them:
`collect.counting()` wraps the Cost Explorer client in `api.CountingClient`, and
every response carries `queries_run` and `estimated_api_cost_usd`.

| Run | Cost Explorer requests | Cost |
|---|---:|---:|
| Skill default (`COMPUTE_SP` × 2 terms × 2 payments, plus 4 posture calls) | 8 | $0.08 |
| MCP tool default (4 SP types + 8 RI services, × 2 terms × 2 payments) | 48 recommendations + 3 posture/spend + 2 per reservation service measured | ~$0.51–0.67 |
| Adding `PARTIAL_UPFRONT` to the tool default | 72 + 3 + 2 per service | ~$0.75–0.91 |
| `get_commitment_posture` default (all 7 measurable reservation services) | 2 SP + 14 RI + 1 spend | $0.17 |

Reservation posture is two requests per service because Cost Explorer only
reports reservation utilization and coverage for one service per query (§6,
*Posture gate*). The coverage/utilization `SERVICE` filter takes different
names than the recommendation API (`Amazon MemoryDB`, not
`Amazon MemoryDB Service`) and has no value for DynamoDB, so DynamoDB
reservation posture is not queried — it is declared as a blind spot in the
notes and does not null the gate. `generate_commitment_analysis` measures only the services
that produced a finding or appear in the inventory; `get_commitment_posture`
takes `ri_services` to narrow it.

The skill sweeps narrow by default and widens only on request. The tool sweeps
wide because it parallelizes and returns a full report in one call — the agent
prompt tells the agent to narrow it with `savings_plan_types` / `ri_services`
when the question names specific services. See also
[`docs/cost-estimate.md`](../cost-estimate.md).

---

## 5. The AWS API surface

### Verified enums

Getting these wrong is the most common failure, so they are stated
literally in `SKILL.md` and in `commitments/api.py`.

| Parameter | Values |
|---|---|
| `--savings-plans-type` | `COMPUTE_SP`, `EC2_INSTANCE_SP`, `SAGEMAKER_SP`, `DATABASE_SP` |
| `--term-in-years` | `ONE_YEAR`, `THREE_YEARS` |
| `--payment-option` | `NO_UPFRONT`, `PARTIAL_UPFRONT`, `ALL_UPFRONT` |
| `--lookback-period-in-days` | `SEVEN_DAYS`, `THIRTY_DAYS`, `SIXTY_DAYS` |
| `--account-scope` | `PAYER` (whole org), `LINKED` (this account) |

`MACHINE_LEARNING_SP` **does not exist**. The fourth Savings Plan type is
`DATABASE_SP`.

RI `--service` takes exactly these eight strings and no others. Note the
inconsistent ` Service` suffix — it is the API's, not a typo:

| Cost Explorer `Service` | Short label |
|---|---|
| `Amazon Elastic Compute Cloud - Compute` | EC2 |
| `Amazon Relational Database Service` | RDS |
| `Amazon Redshift` | Redshift |
| `Amazon ElastiCache` | ElastiCache |
| `Amazon Elasticsearch Service` | Elasticsearch (legacy) |
| `Amazon OpenSearch Service` | OpenSearch |
| `Amazon MemoryDB Service` | MemoryDB |
| `Amazon DynamoDB Service` | DynamoDB |

SageMaker, Lambda, Fargate, Aurora, CloudFront, S3, Neptune, DocumentDB,
MSK, Kinesis and Timestream are all rejected. Do not guess a service
name — copy the `Supported value(s)` list out of the API's own
`ValidationException`.

### The six query groups

`SKILL.md` organizes the CLI work into six numbered groups. Each carries
a `--query` (JMESPath) expression that trims the response to the fields
the method consumes — a bare recommendation response runs to tens of KB
of per-instance detail.

1. **Savings Plans sizing** — `ce get-savings-plans-purchase-recommendation`,
   one call per (type, term, payment). Projects `commit_hr`, `savings_mo`,
   `savings_pct`, `ondemand_mo`, and per-detail `floor_hr`, `avg_hr`,
   `upfront`, `est_util`, `commit_hr`, `spec` (`SavingsPlansDetails`).
2. **Reserved Instance sizing** — `ce get-reservation-purchase-recommendation`.
   Projects `buy_units`, `floor_units`, `avg_units`, `upfront`, `monthly`,
   `break_even_mo`, `util`, `savings_mo`, plus `spec` (`InstanceDetails`) and
   `capacity` (`ReservedCapacityDetails`) — see §5b, without which a
   recommendation is a unit count nobody can purchase.
   `--service-specification OfferingClass=STANDARD`
   is **EC2-only**; sending it to RDS or Redshift is a `ValidationException`.
3. **Existing commitment posture** — four calls
   (`get-savings-plans-coverage`, `get-savings-plans-utilization`,
   `get-reservation-coverage`, `get-reservation-utilization`). Run before
   quoting any savings figure.
4. **Eligible spend** — `ce get-cost-and-usage` grouped by SERVICE. Run
   first when the user has no specific target: an account whose bill is
   serverless, storage and support has nothing to commit against, and
   saying so costs $0.
5. **Reconciliation** — `cost-optimization-hub list-enrollment-statuses`
   then `list-recommendations` filtered to
   `PurchaseSavingsPlans` / `PurchaseReservedInstances`.
6. **Expiry inventory** — `savingsplans describe-savings-plans` (once,
   us-east-1) plus one `describe-*` per (reservation family, region) across
   EC2, RDS, ElastiCache, Redshift, OpenSearch and MemoryDB. Free. Filtered to
   active states, then each end date is derived and bucketed. See §5a.

### 5a. Deriving an end date — the field names differ everywhere

Only Savings Plans and EC2 return an explicit end. The other five families
return `StartTime` plus `Duration` in **seconds**, so the end must be computed
as `StartTime + Duration` (31536000s = 1 year, 94608000s = 3 years). Every
family also names its id, count and type fields differently, which is why the
Lambda encodes them as a data table (`api.RESERVATION_INVENTORY`, a tuple of
frozen `InventorySpec` records) rather than six near-identical functions:

| Family | Call | ID field | Count field | Type field | End | Match attributes |
|---|---|---|---|---|---|---|
| Savings Plans | `savingsplans describe-savings-plans` | `savingsPlanId` | `commitment` (USD/hr) | `savingsPlanType` | `end` | `ec2InstanceFamily` (EC2Instance plans only) |
| EC2 | `ec2 describe-reserved-instances` | `ReservedInstancesId` | `InstanceCount` | `InstanceType` | `End` | `Scope`, `AvailabilityZone`, `ProductDescription`, `OfferingClass`, `InstanceTenancy` |
| RDS | `rds describe-reserved-db-instances` | `ReservedDBInstanceId` | `DBInstanceCount` | `DBInstanceClass` | derived | `MultiAZ` (**bool**), `ProductDescription` |
| ElastiCache | `elasticache describe-reserved-cache-nodes` | `ReservedCacheNodeId` | `CacheNodeCount` | `CacheNodeType` | derived | `ProductDescription` |
| Redshift | `redshift describe-reserved-nodes` | `ReservedNodeId` | `NodeCount` | `NodeType` | derived | `ReservedNodeOfferingType` |
| OpenSearch | `opensearch describe-reserved-instances` | `ReservedInstanceId` | `InstanceCount` | `InstanceType` | derived | none — the API exposes no engine field |
| MemoryDB | `memorydb describe-reserved-nodes` | `ReservationId` | `NodeCount` | `NodeType` | derived | none |

A field-name typo here fails silently — you get rows with blank IDs and no
expiry rather than an error — so `tests/unit/test_commitments_expiry.py`
asserts the derived end date per family rather than only for EC2.

`InventorySpec.attribute_fields` holds the last column. These are what a
**renewal has to match**: an RDS reservation covers one deployment option and one
engine, and an EC2 reservation is pinned to one Availability Zone when `Scope` is
zonal. Renew against a different value and the discount silently does not apply.
They are normalized onto each inventory row as `attributes` (a label→value map)
and joined into a display `spec`. Two traps:

- **`MultiAZ` is a boolean**, so it is tested against `None`, not truthiness —
  `False` is the meaningful value `Single-AZ`, and dropping it as falsy would
  leave a reader assuming Multi-AZ. `INVENTORY_ATTRIBUTE_VALUES` maps
  `True`/`False` to `Multi-AZ`/`Single-AZ`; an absent field yields no key at all,
  because absent is not the same claim as Single-AZ.
- **An empty `spec` on a Compute Savings Plan is correct**, not missing data — a
  Compute plan commits to dollars and nothing else. Only an EC2 Instance plan is
  family- and region-pinned.

**Two blind spots, both declared rather than hidden:**

- **DynamoDB reserved capacity has no describe API** in any SDK. It is listed
  in `inventory.INVENTORY_BLIND_SPOTS` and printed in the report, because silently
  omitting it would render as "nothing expiring" for a reservation that does.
- **Member-account purchases.** `Describe*` lists only the calling account's own
  commitments, while PAYER-scope coverage and utilization include every
  member's. Also declared in `INVENTORY_BLIND_SPOTS`.

**Utilization is per service, not per commitment.** AWS publishes no
per-commitment utilization API, so every Savings Plan shares the account's SP
figure and every RDS reservation shares the RDS figure (one `SERVICE`-filtered
query per family the inventory found). The verdicts are directional and the
report says so inline.

**Pagination and states.** RDS, ElastiCache, Redshift and MemoryDB are read
through botocore paginators; OpenSearch has none, so its `NextToken` is
followed by hand; EC2 returns everything at once. Retired rows count toward
each page's 100, so stopping at page one could drop live, expiring
reservations. Rows are kept in an active state — note Redshift spells it
`pending-payment` where the others use `payment-pending` — or `queued`.

### 5b. The purchasable spec inside a recommendation

`GetReservationPurchaseRecommendation` returns a count and a savings figure per
line item, but **the thing you buy is in a service-specific sub-structure** —
and `RecommendationSummary` sums across all of them. One RDS recommendation
routinely spans `db.r6g.large Multi-AZ` and `db.t4g.medium Single-AZ`; since a
reservation only discounts usage matching its exact specification, that total is
a budget, not an order.

`api.RECOMMENDATION_SPECS` is the data table for this (a tuple of frozen
`SpecShape` records), read by `api.describe_recommendation_spec(detail)`:

| Service | Sub-object | Container | Size fields | Attributes |
|---|---|---|---|---|
| EC2 | `EC2InstanceDetails` | `InstanceDetails` | `InstanceType` | `AvailabilityZone`, `Platform`, `Tenancy` |
| RDS / Aurora | `RDSInstanceDetails` | `InstanceDetails` | `InstanceType` | `DeploymentOption`, `DatabaseEngine`, `DatabaseEdition`, `LicenseModel`, `DeploymentModel` |
| ElastiCache | `ElastiCacheInstanceDetails` | `InstanceDetails` | `NodeType` | `ProductDescription` |
| Redshift | `RedshiftInstanceDetails` | `InstanceDetails` | `NodeType` | none |
| MemoryDB | `MemoryDBInstanceDetails` | `InstanceDetails` | `NodeType` | none |
| OpenSearch / Elasticsearch | `ESInstanceDetails` | `InstanceDetails` | `InstanceClass` **+** `InstanceSize` | none |
| DynamoDB | `DynamoDBCapacityDetails` | `ReservedCapacityDetails` | none | `CapacityUnits` |

Two services break the pattern, which is why this is a table and not one reader
per service: **OpenSearch has no `InstanceType`** (its type is split across
`InstanceClass` and `InstanceSize`, joined with a `.`) and no `Family`, and
**DynamoDB is not under `InstanceDetails` at all** — it has capacity units and a
region, no instance. `describe_recommendation_spec` returns `{}` for an unknown
shape so a caller degrades to the family-level figure instead of raising.

`analyze.LineItem` carries the result per line: `spec`, `region`, its own `floor`
(`MinimumNumberOfInstancesUsedPerHour` — the count that line never dropped below,
so the part with no unused-commitment risk), `achievable`, `monthly_savings`,
`utilization_pct`, plus `size_flex_eligible` and `current_generation`. The last
two are opposite signals worth reading per line: size flexibility means the
recommended size is not binding, while a previous-generation instance means a
three-year commitment locks the account out of the cheaper current generation.

Per-line `achievable` is `int(recommended × scale)` — reservations sell whole — so
the lines can sum **below** the family achievable total. The family math is left
untouched and the report discloses the shortfall, naming the line that should
absorb it, rather than padding a line or restating the headline savings. A line
whose sub-structure is absent renders as `analyze.SPEC_UNAVAILABLE`
(`(specification not returned)`), not as a blank.

### JMESPath: `to_number()` is mandatory on money

Cost Explorer returns money and percentages as JSON **strings**. A bare
ordering comparison does not silently return empty — it raises:

```
Groups[?Metrics.UnblendedCost.Amount>`1000`]
  → TypeError: '>' not supported between instances of 'str' and 'int'

Groups[?to_number(Metrics.UnblendedCost.Amount)>`1000`]
  → [[['EC2', '5000.00']]]
```

Absent values arrive as `""`. Treat `""` and `null` as *no data*, never as
`0` — a `""` floor is an unmeasured floor, not a floor of zero.

---

## 6. The method — risk adjustment

Full statement in
[`skills/discounted-commitments/reference/method.md`](../../skills/discounted-commitments/reference/method.md);
implementation in
[`commitments/analyze.py`](../../src/lambda/mcp/commitments/commitments/analyze.py)
(sizing), [`pools.py`](../../src/lambda/mcp/commitments/commitments/pools.py)
(alternatives) and
[`posture.py`](../../src/lambda/mcp/commitments/commitments/posture.py)
(posture gate, reconciliation). Applying it is **required**, not optional —
emitting the AWS figure as achievable is the exact error the feature exists to
prevent.

Constants: `HOURS_PER_MONTH = 730.0`; `TERM_MONTHS = {ONE_YEAR: 12,
THREE_YEARS: 36}`; `LOOKBACK_HOURS` = 168 / 720 / 1440 for 7 / 30 / 60 days.

### Size line by line, never by family ratio

Every recommendation is sized **per `detail[]` entry** — one purchasable
specification, or one member account under `LINKED` scope — against that
line's own floor and average. A family total is the **sum of its lines**. An
earlier version computed one family-wide ratio over raw instance counts summed
across sizes; that let a flat `m5.large` fleet subsidize a spiky `r6g.4xlarge`
one, ignored each line's floor, and needed a rounding-shortfall note because
the lines could not add up to the total. Per-line sizing removes all three.

The bands, applied to each line's `ratio = floor ÷ average`:

| ratio | Profile | Commitment to report | Confidence |
|---|---|---|---|
| ≥ 0.80 | stable | `commit` — take the AWS figure as-is | High |
| ≥ 0.50 | moderate | `floor + (commit − floor) × 0.5` | Medium |
| < 0.50 | spiky | `min(commit, floor)` — clamp to the floor | Low |
| floor or average **not returned** | unknown | `commit`, marked unvalidated | Low |

`STABLE_FLOOR_RATIO = 0.80`, `MODERATE_FLOOR_RATIO = 0.50`. Never report a
commitment above what AWS recommended. Reservations round **down** to whole
units per line.

**Unknown is not zero.** Cost Explorer returns `""` for an absent figure. A
blank floor read as 0 classifies the workload as spiky and clamps the
commitment to nothing — a confident answer built on missing data. `analyze._opt`
keeps "not returned" (`None`) distinct from a measured `0`, which *is* a real
trough and does clamp.

Savings scale linearly with each line's achievable size
(`line_savings × achievable ÷ commit`), and upfront at the achievable size
scales the same way — the report quotes upfront at the size it recommends,
with the AWS-size figure alongside.

Family confidence is the **savings-weighted** blend of its lines (High = 2,
Medium = 1, Low = 0; ≥ 1.5 High, ≥ 0.5 Medium). The weakest line would let
one small spiky line drag a large stable portfolio to Low; a plain majority
would hide a large spiky one.

### Savings Plans — compare in commitment dollars

`HourlyCommitmentToPurchase` is priced at **Savings Plan** rates;
`CurrentMinimumHourlyOnDemandSpend` is in **on-demand** dollars. A commitment
of $c/hr is only fully used once on-demand usage reaches c ÷ (1 − d), so the
floor must be converted before it can cap anything:

```
r        = EstimatedSPCost × (EstimatedAverageUtilization ÷ 100)
           ÷ (EstimatedSPCost + EstimatedSavingsAmount)          ← = 1 − effective discount
floor_sp = CurrentMinimumHourlyOnDemandSpend × r
avg_sp   = CurrentAverageHourlyOnDemandSpend × r                 (single detail: fallback
                                                                  CurrentOnDemandSpend ÷ LOOKBACK_HOURS)
```

The denominator is the on-demand cost of the usage the plan covers; the
numerator is the commitment actually used. A detail missing `EstimatedSPCost`
borrows the SP-cost-weighted `r` of its siblings; if no detail has one, the
floor is left unconverted and **unused** — the AWS figure is kept at Low
confidence, with a note — rather than compared in the wrong unit.

Worked: 3-year plan, 55% off, on-demand floor $4/hr, AWS commitment $3.60/hr.
In on-demand dollars $3.60 < $4 looks safe. In commitment dollars the trough
absorbs $4 × 0.45 = **$1.80/hr**, so $1.80/hr — **$1,314/mo** — of the AWS
commitment sits unused in the quietest hour.

Waste is priced in the same unit:

```
waste_mo     = max(0, achievable − floor_sp) × 730   ← at the size this report recommends
aws_waste_mo = max(0, commit    − floor_sp) × 730   ← at the AWS size
```

### Cash break-even

`EstimatedMonthlySavingsAmount` is savings **net of the amortized upfront**.
The cash actually saved each month is that plus the amortized upfront (the
recurring fee is lower by exactly that much):

```
break_even_months = upfront ÷ (net_monthly_savings + upfront ÷ term_months)
```

Dividing the upfront by the net figure alone overstates break-even by 1/d and
fails **every All-Upfront plan with a discount under 50%**: 1-year All Upfront
at 28% off on $1,000/mo of on-demand is $8,640 upfront and $280/mo net — 30.9
months by the wrong formula (a false "Do not buy"), **8.6 months** in cash. It
is scale-invariant, so it is computed per line; with no upfront it is `null`,
not 0 ("pays back instantly"); a purchase that never pays back is `inf`,
reported as "never".

### Reserved Instances

Same per-line sizing in whole units, with three differences:

- **Break-even comes from the API** (`EstimatedBreakEvenInMonths`) so it ties
  out to the console, falling back to the cash formula when AWS omits it. The
  family figure is **weighted by achievable upfront over the lines actually
  bought** — a do-not-buy line is excluded rather than averaged in, so it
  cannot push the figure past the term — and an unweighted mean lets a $50
  line's 2 months pull a $50,000 line's 11 months down to 6.5.
- **Waste is priced per idle reservation** at
  `(upfront ÷ term_months + RecurringStandardMonthlyCost) ÷ recommended`.
  Recurring alone is $0 for every All-Upfront RI and priced its waste at
  nothing.
- **The family volatility label** uses the `*NormalizedUnits*` fields when
  present, since raw counts across sizes do not add (4 × large and 4 ×
  4xlarge are not "8 of something"). It is descriptive only; confidence comes
  from the lines.

If a line's instance fields are blank, the capacity-unit names
(`RecommendedNumberOfCapacityUnitsToPurchase`, etc.) are read instead — that
is how DynamoDB reports. A line whose savings or commitment AWS left blank is
filled from the summary (pro rata) only when *every* line is blank; a negative
line is a purchase that loses money, not a missing one.

### Two guards that override the numbers

- **Break-even beyond the term.** A line whose break-even exceeds 12 or 36
  months is excluded (achievable 0, `do_not_buy`); a finding whose every line
  fails is `do_not_buy` and leads its rationale with **"Do not buy"**. A
  do-not-buy finding never wins selection, never counts toward a total, and is
  shown with the role *Do not buy*.
- **Posture gate.** Existing utilization below `UTILIZATION_WARN_PCT = 95.0`,
  or Savings Plans coverage above `COVERAGE_SATURATED_PCT = 90.0`, is a
  blocker, reported *before* any savings figure. Reservation utilization and
  coverage are queried **per service with a `SERVICE` filter** — omitted,
  Cost Explorer defaults the filter to EC2, and every RDS figure would really
  be EC2's. A measured 0% is a blocker (`is not None`, not truthiness).
  `DataUnavailableException` means no commitment of that kind is held — a
  safe answer. Any other failure lands in `unmeasured`, and
  `safe_to_buy_more` is then `null`: a gate that could not run must not read
  as a gate that passed.

### One purchase per usage pool

Cost Explorer sizes each Savings Plan type and each RI service independently
against the **same** on-demand usage, so summing them counts that usage two or
three times. `pools.py` places every finding in a usage pool and an option
within it:

| Pool | Options (alternatives to each other) |
|---|---|
| `compute` | Compute SP · EC2 Instance SP · EC2 reservations |
| `database` | Database SP · RDS + ElastiCache + DynamoDB reservations (one option; they cover different usage and add up) |
| `sagemaker` | SageMaker SP |
| `redshift`, `memorydb` | reservations |
| `opensearch` | OpenSearch reservations · Elasticsearch (legacy) reservations |

Database Savings Plans cover Aurora, RDS, DynamoDB and ElastiCache among the
services here, not MemoryDB, Redshift or OpenSearch. Within each pool the
option with the most achievable savings gets `role = "recommended"`; the
others are `"alternative"`. **Only recommended findings are totalled** —
envelope, report bottom line and High-confidence subset alike — and the report
labels the rest *Alternative — do not stack*.

### Reconciliation — like for like

Cost Optimization Hub prices every commitment at **one** preferred term and
payment (3-year All Upfront unless changed), returns every alternative per
resource when asked to, and has no Database Savings Plan type. A naive
comparison of the two totals is therefore usually an artifact. `posture.reconcile_with_coh`
narrows it on three axes:

1. **Term and payment** — read from `GetPreferences`, and only Cost Explorer
   findings at that permutation are compared (from *all* analyzed
   permutations, not just the selected ones). No match → `not-comparable`.
2. **Coverage** — Database SP findings are excluded and named.
3. **Overlap** — `includeAllRecommendations=False` on the COH side, and one
   option per usage pool on both sides, at the AWS best case (COH publishes a
   best case too).

It also notes when COH was read in a different account from Cost Explorer.

```
delta_pct = |ce_total − coh_total| ÷ max(|ce_total|, |coh_total|) × 100
```

| `delta_pct` | Verdict |
|---|---|
| both totals 0 | agree-zero |
| ≤ 10% | reconciled |
| ≤ 30% | minor variance |
| > 30% | material variance |
| no CE finding at COH's term/payment | not-comparable |

A material variance is **flagged, never averaged away**.

### Choosing what to report

Keep **one finding per (family, label)**: purchases that pay back first, then
highest achievable savings; break ties toward the **shorter term**, then the
**less upfront cash**. Then plan pools as above; the report lists
recommended rows first.

### Renewals — a different decision from a new purchase

Implemented in `expiry.analyze_expiry`, which takes `as_of` as a parameter
rather than calling `date.today()` so it stays a pure function a test can pin.

The premise: **a lapsing commitment has zero switching cost.** Mid-term, resizing
means buying out of an obligation; at expiry it is free. So expiry is the one
moment the size can change without penalty, and "let it lapse" is a legitimate
outcome rather than a failure to act.

Buckets, on `days_remaining = end − as_of`:

| `days_remaining` | Bucket | Constant |
|---|---|---|
| < 0 | `expired` — already back at on-demand rates, reported separately | — |
| ≤ 30 | `urgent` | `EXPIRY_URGENT_DAYS` |
| ≤ 60 | `soon` | `EXPIRY_SOON_DAYS` |
| ≤ 90 | `upcoming` | `EXPIRY_HORIZON_DAYS` |
| > horizon | counted in `total_active`, kept out of the table | — |

Verdicts, on that **service's** utilization figure — Savings Plans share the
account's SP figure, and each reservation family uses its own filtered query
(an unfiltered one reports EC2 only, so an RDS renewal was once judged on EC2's
99%):

| Utilization | Action | Reasoning |
|---|---|---|
| ≥ `UTILIZATION_WARN_PCT` (95%) | `renew` | Being consumed; lapsing returns that spend to on-demand |
| ≥ `RENEW_LAPSE_PCT` (50%) | `renew-smaller` | Partly wasted — renew at the consumed portion, using the free resize point |
| < 50% | `let-lapse` | Over half unused; re-size from current usage instead of renewing the mistake |
| `None` | `review` | Stated plainly. A verdict with no utilization behind it is a guess. |
| any, with a matching **queued** purchase | `renewal-queued` | Already scheduled. Buying again double-commits the usage for a full term. |

Queued purchases (`queued` state, on EC2 RIs and Savings Plans) are
inventoried and matched to what is expiring on family, service, region,
instance type and spec — a queued Single-AZ reservation does not renew a
Multi-AZ one. A partial match says how much it covers. Queued renewals are not
counted as exposure.

Exposure is quantified in the unit the data is actually in:

```
hourly_commitment_expiring     = Σ commitment          (Savings Plans, USD/hour)
monthly_committed_spend_expiring = hourly × 730
reserved_units_expiring        = Σ instance/node count (reservations, units)
```

Reservation unit counts are **deliberately not converted to dollars** — that
needs per-instance pricing this module never queries, and an invented rate would
be a fabricated figure. The two totals are separate keys and must never be
summed. An unparseable end date lands in `undated` and is reported as needing
manual checking rather than dropped.

**Precedence:** an urgent expiry outranks every new-purchase recommendation. The
report's Bottom line surfaces the urgent count above the savings table, because a
renewal deadline is fixed and a purchase is optional.

### Worked example

One Compute SP detail: `commit = $10.00/hr`, 30% effective discount at 100%
utilization (`r = 0.70`), on-demand floor `$4.00/hr`, average `$15.00/hr`, no
upfront:

```
floor_sp   = 4.00 × 0.70 = $2.80/hr      avg_sp = 15.00 × 0.70 = $10.50/hr
ratio      = 2.80 ÷ 10.50 = 0.27         → below 0.50 → spiky, Low confidence
achievable = min(10.00, 2.80) = $2.80/hr
savings    = $3,128.57 × 0.28 = $876.00/mo   ← the recommendation
aws_waste  = (10.00 − 2.80) × 730 = $5,256/mo stranded at the AWS size
break_even = null (no upfront)
```

Reported as **$876/mo achievable** against an AWS ceiling of $3,129/mo, Low
confidence, because the quietest hour absorbs only 28% of the AWS commitment.
Comparing the $4.00 on-demand floor against the $10.00 commitment directly
would have clamped to $4.00 and overstated achievable savings by 43%.

---

## 7. Platform deployment — the `commitments` MCP tool

### Where it sits

```
cost-operations-agent  (leaf, under finops-agent)
  tools: cost-explorer, cur-athena, cost-optimization-hub, commitments
```

Registered in
[`src/agents/hierarchy.json`](../../src/agents/hierarchy.json); tool
definitions in
[`src/lambda/mcp/tools.json`](../../src/lambda/mcp/tools.json). Gateway
tool names are target-prefixed —
`commitments___generate_commitment_analysis` and its four siblings.

Deployed as `${PROJECT_TAG}-commitments-tool`: `python3.12`, **300 s**
timeout, **1024 MB**, both cross-account role aliases wired
(`CROSS_ACCOUNT_ROLE_ARN` for Cost Explorer and the inventory,
`CROSS_ACCOUNT_ROLE_ARN_COH` for Cost Optimization Hub). COH uses its own role
**only when `CROSS_ACCOUNT_ROLE_ARN_COH` is set** — a delegated-admin COH
account; otherwise it reads in the same account as Cost Explorer, so the
reconciliation compares like with like.

Clients are built **per invocation**, never cached at module scope: a warm
container outlives the 15-minute assumed-role credentials, and a cached client
keeps the expired ones. `shared.cross_account` caches the *session*, re-assumes
it once it has under 330 s left, and never caches a failed AssumeRole. Every
client is built with `require_role=True` — if a role is configured but cannot
be assumed, the call raises `CrossAccountRoleError` instead of silently
analyzing the ops account — and with `RETRY_CONFIG` (adaptive mode, 10
attempts), because Cost Explorer throttles with `LimitExceededException`,
which legacy retry mode does not retry.

### The five tools

| Tool | Use when | Returns |
|---|---|---|
| `generate_commitment_analysis` | Default. Any purchase question. | `report_markdown` (complete report) + structured envelope + posture + blockers + reconciliation + `expiry` when `regions` is passed |
| `size_savings_plans` | The question is narrowed to Savings Plans | Risk-adjusted SP findings, **no posture gate** |
| `size_reservations` | The question is narrowed to reservations | Risk-adjusted RI findings in whole units |
| `get_commitment_posture` | Only existing coverage/utilization is asked about | Per-service coverage and utilization, blockers, `safe_to_buy_more` (`true` / `false` / `null` = not measured), `unmeasured`, COH enrollment, eligible spend |
| `get_commitment_expiry` | "What's expiring?", "What should we renew?" | Active and queued commitment inventory bucketed urgent/soon/upcoming with a renew / renew-smaller / let-lapse / review / renewal-queued verdict each, plus exposure totals and declared blind spots |

`generate_commitment_analysis` is the preferred path and the agent prompt
says so. It is one call, and it is the only one that includes both the
posture gate and the reconciliation the numbers are defensible with.

**Time budget.** The Lambda timeout, the gateway's MCP read and the
supervisor→leaf delegation (`read_timeout=300` in `shared/registry.py`) all
share one 300 s ceiling, and the agents still need time to read the result.
The recommendation sweep therefore runs under `DEFAULT_TIME_BUDGET_SECONDS =
150`: anything unfinished then is abandoned, reported as a
`TimeBudgetExceeded` collection warning, and excluded from the totals — a
partial, labelled report instead of a timeout.

**Failure is not "no opportunity".** When every recommendation query fails
(AccessDenied, expired credentials, Cost Explorer disabled, a window outside
its history), the tool returns `{"error", "hint", "collection_warnings"}` and no
report. A report with zero findings says "real result" only when nothing failed;
otherwise it says the result is incomplete.

If a sizing tool is called directly, `get_commitment_posture` **must**
also be called — `size_savings_plans` and `size_reservations`
deliberately skip the gate, and their responses carry a `note` saying so.

### Parameters (all five tools)

| Parameter | Values | Default |
|---|---|---|
| `lookback` | `SEVEN_DAYS` / `THIRTY_DAYS` / `SIXTY_DAYS` | `THIRTY_DAYS` |
| `terms` | `ONE_YEAR`, `THREE_YEARS` | both |
| `payment_options` | `NO_UPFRONT`, `PARTIAL_UPFRONT`, `ALL_UPFRONT` | `NO_UPFRONT` + `ALL_UPFRONT` |
| `account_scope` | `PAYER` / `LINKED` | `PAYER` |
| `families` | `sp`, `ri` | both |
| `savings_plan_types` | the 4 SP types, or `all` | `all` |
| `ri_services` | short labels, exact CE names, or `all` | `all` |
| `posture_days` | integer ≥ 1 | `30` |
| `ri_services` (posture) | short labels or `all` — which reservation services to measure | `all` |
| `spend_days` | integer ≥ 1 | `60` |
| `regions` | AWS region names | `generate_commitment_analysis`: none (expiry skipped); `get_commitment_expiry`: the Lambda's own region |
| `services` / `inventory_services` | `ec2`, `rds`, `elasticache`, `redshift`, `opensearch`, `memorydb` | all |
| `horizon_days` / `expiry_horizon_days` | integer ≥ 1 | `90` |

Every value arrives as caller-controlled JSON and is validated in
`handler.py` before any AWS call; a bad value returns
`{"error": "Invalid term: ...  Choose from ..."}` rather than a 500.
Lists accept either a JSON array or a comma-separated string.

`regions` gets stricter validation than the rest: region names are interpolated
into SDK endpoints, so `collect.resolve_regions` shape-checks each one against
`^[a-z]{2}(-[a-z]+)+-\d$`, lowercases, and de-duplicates (a repeated region would
otherwise double every total). On `generate_commitment_analysis` it is **opt-in**:
omit it and no inventory call is made at all, so a deployment granted only
`ce:Get*` keeps working and simply gets `expiry: null`.

### Module layout

```
src/lambda/mcp/commitments/
├── handler.py      event → params → clients → collect
└── commitments/
    ├── api.py        Cost Explorer + COH reads, paging, request counter,
    │                 enums, labels, SpecShape table
    ├── inventory.py  InventorySpec table, Describe* getters, states
    ├── analyze.py    per-line sizing, SP floor conversion, break-even
    ├── pools.py      usage pools, selection, recommended vs alternative
    ├── posture.py    per-service posture gate, COH reconciliation
    ├── expiry.py     expiry buckets, renewal verdicts, queued matching
    ├── collect.py    sweep, time budget, posture/expiry snapshots
    ├── shape.py      envelope, line-item capping, failure diagnosis
    │                 (re-exported by collect)
    └── report.py     markdown rendering
```

The `commitments/` subpackage stays boto3-free even though the inventory needs
regional clients. `api.Clients` carries a `make_client: Callable[[str, str], Any]`
factory as a **defaulted trailing field**, so no module but `api.py` imports
boto3 and every pre-existing `Clients(...)` construction still works
positionally. When the factory is absent the inventory getters return an
explained error record rather than raising — a host that granted only `ce:Get*`
gets a warning, not a crash.

Two design rules hold this together:

- **`handler.py` owns only what is specific to being a gateway tool** —
  turning a caller event into validated parameters, and building AWS
  clients from `shared.cross_account`. Everything between comes from
  `commitments.collect`. Logic added to the handler instead is logic the
  unit tests do not cover, and `TestHandlerDiscipline` fails the build
  over it.
- **The `commitments/` subpackage carries no platform imports**, so it
  stays independently testable and never opens a `boto3.Session` of its
  own.

Enum and default constants are **aliased** into `handler.py`, never
restated. A local copy would let the gateway tool accept a term the
shared pipeline rejects, and nothing would catch it.

Fan-out is a `ThreadPoolExecutor` with `MAX_WORKERS = 6`. A failed job
lands as an error record in its own slot, so a partial sweep still
produces a report with a `collection_warnings` table and totals
explicitly labelled a lower bound.

---

## 8. Data model

### `generate_commitment_analysis` response

```
{
  "report_markdown": "# AWS Discounted Commitments Report\n...",
  "account_id": "123456789012",
  "generated_at": "2026-09-03T06:11:20Z",
  "lookback": "THIRTY_DAYS",
  "account_scope": "PAYER",

  "recommendations": [ ... ],      // every selected finding, each with a role
  "count": 3,
  "total_estimated_monthly_savings": 1840.22,   // recommended rows only
  "aws_best_case_monthly_savings": 3011.75,     // recommended rows only
  "total_upfront_cost": 0.0,
  "usage_pools": [{"pool": "compute", "recommended_option": "EC2 Instance Savings Plan",
                   "monthly_savings": 1840.22, "alternatives": {"Compute Savings Plan": 1500.0}}],

  "reconciliation": {"status": "reconciled", "delta_pct": 4.1,
                     "basis": {"term": "THREE_YEARS", "payment": "ALL_UPFRONT"}, ...},
  "existing_commitment_posture": { ... },
  "safe_to_buy_more": false,       // null when posture could not be measured
  "blockers": ["Existing Savings Plans are only 82.0% utilized ..."],
  "expiry": null,                  // populated only when `regions` was passed
  "queries_run": 57,
  "estimated_api_cost_usd": 0.57,
  "collection_warnings": [],
  "data_source": "live"
}
```

`queries_run` is every Cost Explorer request actually sent — each page
included — counted at the client by `api.CountingClient`. The free `Describe*`
inventory calls and Cost Optimization Hub calls are not counted.

When every recommendation query fails the response is instead
`{"error", "hint", "account_id", "collection_warnings", "queries_run",
"estimated_api_cost_usd"}`, with no `report_markdown`.

The JSON omits `line_items` here — `report_markdown` carries them — so the
response is not doubled. `size_savings_plans` / `size_reservations` include
them.

### `get_commitment_expiry` response

```
{
  "account_id": "123456789012",
  "as_of": "2026-09-04",
  "horizon_days": 90,
  "regions": ["ap-northeast-1", "us-east-1"],
  "total_active": 11,
  "expiring": [ ... ],       // sorted by days_remaining, then label
  "expired": [ ... ],        // end date passed but still listed active
  "undated": [ ... ],        // no parseable end date — needs manual check
  "queued": [ ... ],         // scheduled purchases, matched to what expires
  "counts": {"urgent": 1, "soon": 2, "upcoming": 0},
  "actions": {"renew": 2, "renew-smaller": 1, "let-lapse": 0, "review": 0,
              "renewal-queued": 0},
  "hourly_commitment_expiring": 5.5,
  "monthly_committed_spend_expiring": 4015.0,
  "reserved_units_expiring": 4.0,
  "blind_spots": ["DynamoDB reserved capacity (no describe API exists)"],
  "renewal_actions": { ... },
  "utilization_unmeasured": [],
  "collection_warnings": [],
  "queries_run": 4,
  "note": "...per-service utilization, read-only...",
  "data_source": "live"
}
```

Per commitment: `family` (`savings-plan` | `reservation`), `service`, `label`,
`commitment_id`, `arn`, `instance_type`, `attributes`, `spec`, `quantity`,
`unit`, `region`, `state`, `payment_option`, `start`, `end`, `term_months`,
`days_remaining`, `urgency` (`urgent` | `soon` | `upcoming` | `expired`),
`utilization_pct`, `action` (`renew` | `renew-smaller` | `let-lapse` |
`review` | `renewal-queued`), `rationale`.

`utilization_pct` is `null` when unmeasured — `null` means unmeasured, never
zero, and `review` is the verdict that goes with it.

`attributes` is the map of dimensions a renewal has to match (RDS:
`deployment`, `engine`; EC2: `scope`, `AZ`, `platform`, `class`, `tenancy`), and
`spec` is those joined onto `instance_type` for display —
`db.r6g.large · Multi-AZ · postgresql`. "Renew" means renew *the same thing*, so
a row without its spec is not actionable. A key absent from `attributes` means
AWS did not return the field, which is not the same claim as `Single-AZ`; `spec`
is empty for a Compute Savings Plan by design.

### Per-recommendation shape

```
{
  "commitment_family": "savings-plan",
  "commitment_type": "Compute Savings Plan",
  "role": "recommended",           // | "alternative" | "do-not-buy"
  "usage_pool": "compute",
  "term": "ONE_YEAR",
  "payment_option": "NO_UPFRONT",
  "currency": "USD",
  "aws_recommended_commitment": 10.0,
  "achievable_commitment": 2.8,
  "commitment_unit": "USD/hour",
  "estimated_monthly_savings": 876.0,
  "aws_best_case_monthly_savings": 3128.57,
  "estimated_savings_percentage": 30.0,
  "upfront_cost": 0.0,             // at the achievable size
  "aws_upfront_cost": 0.0,
  "break_even_months": null,       // number, "never", or null (no upfront)
  "waste_exposure_monthly": 0.0,   // at the achievable size
  "aws_waste_exposure_monthly": 5256.0,
  "do_not_buy": false,
  "confidence": "Low",
  "spend_profile": "spiky",
  "implementation_effort": "Medium",
  "rationale": ["..."],
  "line_items_total": 1,
  "line_items": [ ... ],           // capped at LINE_ITEM_LIMIT = 25
  "line_items_omitted": 0
}
```

`commitment_unit` is `USD/hour` for Savings Plans and `units` for
reservations. Reading an hourly-dollar commitment as a unit count
misreads it by roughly 1000×, so the unit travels with the number
everywhere it goes.

**`line_items` is the purchasable part; the item-level total is not.** The total
sums every specification the service returned, and a reservation only discounts
usage matching its exact `spec` — so quote the item as a budget and its line
items as the order. Per entry: `spec`, `region`, `commitment_unit`,
`aws_recommended_commitment`, `achievable_commitment`, `minimum_observed` and
`average_observed` (commitment units — SP dollars for a Savings Plan; `null`
when AWS did not return them), `minimum_hourly_on_demand_spend` (SP, before
conversion), `estimated_monthly_savings` (achievable), `aws_best_case_monthly_savings`,
`upfront_cost`, `aws_upfront_cost`, `break_even_months`, `do_not_buy`,
`confidence`, `spend_profile`, `monthly_on_demand_cost`,
`aws_estimated_utilization_percentage` (AWS's projection at its own size; `null`
when not returned), `size_flexible`, `current_generation`, `account_id`. Lines
are sorted by achievable savings and capped at 25; `line_items_omitted` says
how many were cut. See §5b for where each field comes from.

Key names deliberately mirror a Cost Optimization Hub recommendation
list, so a host that already renders those needs no translation layer.
`reconciliation` is omitted when there was nothing to reconcile, so a
sizing-only response does not carry an empty key.

### Report structure

Eight sections, in this order, emitted by `report.py:render()` and
mirrored in
[`reference/output-template.md`](../../skills/discounted-commitments/reference/output-template.md):

1. **Bottom line** — AWS best case vs risk-adjusted achievable vs
   high-confidence-only, monthly and annual, over **recommended rows only**
   (one per usage pool, with the not-stacked caveat); leads with the
   urgent-expiry count when there is one. With no findings it says "real
   result" only if no query failed
2. **Reconciliation against AWS native tools**
3. **Existing commitment health** — blockers first
4. **Commitment expiry and renewal** — regions swept, exposure, the
   expiring table (8 columns: ends, days, commitment, **spec**, size, region,
   utilization, action), the service-level-utilization caveat, per-row
   reasoning, queued purchases, already-ended commitments with their spec,
   declared blind spots. Band labels follow the configured horizon. Omitted
   entirely when the inventory did not run.
5. **Recommended commitments** — summary table with a **Role** column
   (*Recommended* / *Alternative — do not stack* / *Do not buy*), then
   per-finding detail ending in a **line items** table (`Buy | Region | AWS
   size | Floor | Achievable | Confidence | Achievable savings/mo | AWS best
   case/mo`) that names the instance type and deployment option for each
   purchase, flags size-flexible, previous-generation and cannot-pay-back
   lines, and states how many smaller lines were not shown. The table is
   omitted when no line has anything to buy.
6. **Eligible spend**
7. **Method** — bands restated in-report so figures are auditable
8. **Collection warnings** — present only when a query failed

Emit only the sections you have data for, and name the ones you dropped.

Section 4 sits between existing-commitment health and the new-purchase
recommendations on purpose: a commitment lapsing in three weeks is a decision
with a deadline, and it belongs ahead of an optional purchase. `render()` reads
it with `data.get("expiry")`, so a payload produced before this section existed
still renders — the section is simply absent.

---

## 9. Report template

[`discounted_commitments.json`](../../src/agents/shared/report_templates/discounted_commitments.json)
(mirrored at `src/lambda/frontend/core-api/report_templates/`) — one
section, `full_commitment_analysis`, which:

1. Calls `generate_commitment_analysis` with **no arguments**.
2. Emits `report_markdown` **verbatim** — no summarizing, truncating,
   re-ordering, or dropping table rows and rationale bullets.
3. Appends one extra section, **Purchase sequence**: clear every blocker
   first; buy only *Recommended* rows — *Alternative* rows cover the same
   usage and are never stacked; High-confidence rows first, in descending
   achievable-savings order; treat Medium as a smaller first tranche and
   re-measure after 30 days; never buy a row whose break-even exceeds its own
   term; confirm no matching purchase is already queued.
4. If the tool returns `error` instead of `report_markdown`, reports the error
   and hint verbatim and says no analysis was produced — never "no
   opportunity".

The template explicitly forbids rebuilding the recommendation table from
the structured fields — that is what guarantees the risk-adjusted
figures, the non-cancellable disclaimer, and every rationale bullet
survive to the reader.

---

## 10. Portability — using the skill elsewhere

The skill directory is three markdown files and nothing else:

```
discounted-commitments/
├── SKILL.md                      # routing, queries, enums, constraints
└── reference/
    ├── method.md                 # required: risk adjustment + guards
    └── output-template.md        # report structure and field names
```

Copy the directory into another repo or another agent's skills folder and
it works unchanged. There is nothing to install, no `requirements.txt`,
no bundled scripts, and no reference to this platform anywhere in the
three files. The only runtime dependency is the AWS CLI, which the host
either has or does not — and the routing tree handles the case where it
does not.

To delegate rather than run it locally, `SKILL.md` supplies a brief for
handing to a coding agent with a shell. If the delegate returns an error
(missing credentials, `AccessDenied`, expired SSO), pass it through with
the fix from the failure-modes table rather than retrying in your own
sandbox.

---

## 11. Why "what skills do you have" never names this

Worth stating explicitly, because it surprises people.

**The gateway has no concept of skills.** Nothing in `src/`, `scripts/`,
or `terraform/` reads `skills/`. `sync_gateway_tools` uploads only tool
name, description, and `inputSchema`; the packaging step globs
`src/lambda/mcp/*/` and never sees the skill directory.

When an agent is asked what it can do, it answers from the authoritative
inventory that `_inject_tool_inventory()` appends to its system prompt
([`src/agents/shared/agent_base.py`](../../src/agents/shared/agent_base.py)),
and which list that is depends on the agent's tier:

- **Mid-level agents** list *child agents* from the
  `cloudops-agent-registry` DynamoDB table, filtered on `parent_agent`
  and `enabled`. The supervisor therefore names three children and
  nothing deeper — which makes each child's `description` in
  `hierarchy.json` the entire basis for what the supervisor believes it
  can do.
- **Leaf agents** list *gateway MCP tools*, filtered to the `tools`
  allowlist by `<target>___` prefix.

So the deepest name reachable is
`commitments___generate_commitment_analysis` — a tool name, not a skill
name. The skill reaches production only because its method was ported
into the Lambda. If commitment sizing needs to be discoverable from the
supervisor, the lever is `finops-agent.description`, not skill
registration.

---

## 12. Testing

```bash
.venv/bin/python -m pytest tests/unit/test_commitments_*.py -q
# 409 passed
```

| File | Covers |
|---|---|
| `test_commitments_analyze.py` | Bands at their boundaries; SP floor conversion to commitment dollars; per-line RI sizing; cash break-even across discounts and terms; do-not-buy exclusion; usage pools; per-service posture and `safe_to_buy_more`; like-for-like reconciliation; report roles, currency and empty-vs-incomplete wording. Fixtures are priced from a real discount (`sp_rec`) — see below |
| `test_commitments_api.py` | The Cost Explorer / COH calls, enum validation, response parsing |
| `test_commitments_collect.py` | Service/type resolvers, permutation sweep, partial-failure handling, all-failed diagnosis, the time budget, recommendation paging and the detail-count warning, per-service posture filters, envelope key names, units, roles and line-item capping, plus `test_module_builds_no_clients_and_touches_no_files` — the guard that keeps `collect.py` free of clients and filesystem access |
| `test_commitments_expiry.py` | Per-family field mapping and derived end dates, date coercion across SDK/CLI shapes, expiry buckets and renewal verdicts at their boundaries, per-service utilization queried only for families found, queued-renewal matching, Redshift's `pending-payment`, inventory pagination (paginator and hand-followed `NextToken`), unit separation, region validation, billed-request counting, expiry rendering, and that the Savings Plans job is **not** multiplied per region |
| `test_commitments_spec.py` | The per-service spec shapes (all seven, including the OpenSearch two-field type and the DynamoDB container), `MultiAZ` as a boolean, per-line breakdown and ranking, whole-unit rounding and its disclosure, the report's line-items and expiry `Spec` columns, and `line_items` in the envelope |
| `test_commitments_tool.py` | Handler parameter validation, per-invocation clients with `require_role` and the retry config, COH role selection, error-not-report on total failure, request counting, opt-in inventory, error normalization, `TestHandlerDiscipline`, and `tools.json` wiring |
| `test_cross_account.py` | (shared) session refresh near expiry, failures never cached, `require_role` fail-closed vs the default fallback |
| `test_commitments_contracts.py` | Every AWS field name in `RECOMMENDATION_SPECS` and `RESERVATION_INVENTORY` checked against botocore's shipped service models — see below |

**Why the fixtures derive their numbers.** An earlier version set the SP
commitment equal to average on-demand spend (a 0% discount) and picked upfront
figures unrelated to the term. Those fixtures *locked in* two pricing errors —
break-even divided by net savings, and an SP commitment compared against an
on-demand floor — instead of catching them. `sp_rec` now prices a detail the
way Cost Explorer does (commitment, discount, utilization → SP cost, savings,
upfront by payment option), and the break-even test is parametrized across
discounts from 10% to 49% on both terms.

The band tests pin the risk-adjustment thresholds *at* their boundaries
(0.80, 0.50, 95%, 90%, and the 30/60/90-day expiry cutoffs), which is what
makes a constant change visible rather than silent. Three expiry tests exist
specifically because their failure modes are silent rather than loud:

- **Derived end dates, per family.** A wrong field name yields rows with blank
  IDs and no expiry instead of an error, so every family is asserted, not just
  EC2.
- **One Savings Plans call regardless of region count.** Sweeping SPs per region
  would return the same plans N times and inflate every total by N.
- **`queries_run` equals the Cost Explorer requests actually sent** — coverage,
  utilization and spend included, free `Describe*` calls excluded. Asserted
  against a recording stub, because the figure is a real dollar amount.

The spec tests exist for the same reason: a mistyped field name in
`RECOMMENDATION_SPECS` yields a line with no instance type rather than an
exception, and writing them surfaced a real defect — `str(None)` rendering as the
literal `"None"`, which would have printed a fabricated specification into a
customer-facing report.

### Contract tests: field names, checked against botocore

`test_commitments_spec.py` proves the *logic* using fixtures, but a fixture
written from the same table as the code cannot catch a wrong field name — it
agrees with the typo and stays green. Every read goes through `.get(field)`,
which returns `None` for a misspelling exactly as it does for a field AWS
genuinely omitted, so `InstanceTpye` does not raise: it produces a
recommendation with no instance type, and the report renders around the hole.

`test_commitments_contracts.py` closes that gap without needing an account.
botocore ships the same service models the SDK dispatches on, so the real field
names are already on disk:

```python
model = session.get_service_model("ce").operation_model(
    "GetReservationPurchaseRecommendation"
)
```

Every name in `RECOMMENDATION_SPECS` and `RESERVATION_INVENTORY` is asserted to
be a member of the matching output shape — containers, size fields, attribute
fields, `Family`, `Region`, the six differently-named identifier/count/type
fields, and the `Duration` fallback for the five families that return no explicit
end date. Three further assertions pin things a rename would quietly break:

- **`MultiAZ` is still `boolean`.** This is why the code tests it against `None`
  rather than truthiness — `False` means Single-AZ, a real and expensive
  specification.
- **`commitment` is still a `string`.** AWS returns `"1.00000000"`, so the
  `float()` conversion is load-bearing; summing without it concatenates.
- **`INVENTORY_SP_STATES` (active plus queued) are valid `SavingsPlanState` enum values.** These go to
  the API as a server-side filter, so an invalid one is a ValidationException in
  a Lambda against a live account rather than a red test here.

Two coverage facts worth knowing, both asserted rather than assumed:

- **DynamoDB has no `SizeFlexEligible`/`CurrentGeneration`.** There is no
  instance, so there is no size to flex. `describe_recommendation_spec` reading
  both unconditionally yields `False`, which is the correct answer, not a data
  gap — pinned so nobody "fixes" the absence by inventing a field name.
- **`test_every_recommendation_container_is_covered`** compares the modelled
  `*InstanceDetails`/`*CapacityDetails` containers against the ones
  `RECOMMENDATION_SPECS` knows. `describe_recommendation_spec` degrades to `{}`
  for an unknown shape, so a service AWS adds later would cost the report its
  spec column silently; this turns that into a failing test instead.

What these tests do **not** do is prove a field is *populated* for a given
account — that needs an account holding the commitment or recommendation in
question. OpenSearch and DynamoDB are the two shapes with no live coverage,
because neither service is in scope for this deployment and there is no usage to
size a reservation against; both rest on the contract tests and unit fixtures.
That is the guarantee worth having here: a blank spec column can only mean AWS
omitted the field, never that the field name is wrong.

The contract file also pins the reads added for this review: every inventory
family either has a botocore paginator or pages on a `NextToken` that
`_inventory_rows` follows; each `ce_service` is a Cost Explorer reservation
service; `GetPreferences` models `preferredCommitment`; the reservation
posture APIs accept `Filter`; and `GetSavingsPlansCoverage` pages on
`NextToken` while the others use `NextPageToken`.

`test_tools_json_declares_every_dispatched_tool` reads the dispatcher table out
of `handler.py` rather than restating it, so a tool added to one and not the
other fails the build in both directions. The skill's markdown copy of the
constants has no test — verify it by hand against `analyze.py` after any change.

---

## 13. Known gotchas

| Symptom | Likely cause | Fix |
|---|---|---|
| `ValidationException` on `--savings-plans-type MACHINE_LEARNING_SP` | That value does not exist | The fourth type is `DATABASE_SP` |
| `ValidationException` on `--service` | Not one of the eight exact strings | Copy the API's own `Supported value(s)` list from the error; do not guess |
| `ValidationException` mentioning `OfferingClass` | `--service-specification` sent to a non-EC2 service | EC2-only; drop it elsewhere |
| `TypeError: '>' not supported between instances of 'str' and 'int'` | JMESPath ordering comparison on a money string | Wrap in `to_number()` |
| `DataUnavailableException` with a blank `Message` | No existing commitment of that kind | Report "no existing commitment of this type", not a blank error |
| All posture metrics zero | **No commitment exists** — not 0% utilization on a broken one | Say which it is; they are very different findings |
| A `$0` / no-recommendation result | A real result **only if no query failed** | Check eligible spend and explain there is nothing to commit against; with collection warnings the result is incomplete |
| Tool returns `error` + `hint`, no report | Every recommendation query failed (AccessDenied, expired credentials, Cost Explorer disabled, window outside history) | Fix what the hint names; this is not a "no opportunity" result |
| Headline total far above the on-demand spend | Alternatives summed — Compute SP, EC2 Instance SP and EC2 RIs cover the same usage | Total recommended rows only; the tool marks the rest `alternative` |
| All-Upfront plan flagged "Do not buy" at a modest discount | Break-even divided by **net** savings | Use cash break-even: upfront ÷ (net + upfront ÷ term) |
| Spiky SP commitment below the on-demand floor still shows waste | Correct — the floor was converted to commitment dollars | A $4/hr on-demand trough at 55% off absorbs only $1.80/hr |
| RDS renewal judged on a 99% figure that belongs to EC2 | Reservation utilization queried without a `SERVICE` filter (defaults to EC2) | One filtered query per service |
| Expiring commitment told to "renew" though already renewed | Queued purchases were not inventoried | `queued` is now inventoried and matched; the verdict reads `renewal-queued` |
| Warm container fails with `ExpiredToken` after 15 minutes | Clients cached at module scope with static assumed-role credentials | Clients are built per invocation; the shared session re-assumes before expiry |
| Results describe the ops account in a cross-account deployment | AssumeRole failed and the client fell back to the execution role | The tool passes `require_role=True` and fails with `CrossAccountRoleError` instead |
| Recommendation figure is not purchasable by the account | `PAYER` scope aggregates the whole org | Re-run with `--account-scope LINKED` |
| Endpoint resolution failure | Called outside us-east-1 | CE purchase recommendations and COH are us-east-1-only |
| COH empty or `NOT_ENROLLED` | Not enrolled | Continue; state that figures rest on Cost Explorer alone |
| Response blows up the context window | `--query` omitted | Always send it — bare responses run to tens of KB of per-instance detail |
| Unexpectedly large Cost Explorer bill | Wide sweep, repeated | $0.01 per request — every CE call and page, not just recommendations; read `queries_run`, and never re-query a number you already have |
| `TimeBudgetExceeded` collection warnings | Sweep still running at the 150 s budget on a slow or throttled account | Narrow `savings_plan_types` / `ri_services` / `terms`; the partial report is labelled a lower bound |
| Agent reformats or summarizes the report | Model ignoring the verbatim rule | The template and agent prompt both mandate verbatim `report_markdown`; tighten if it recurs |
| Skill and Lambda disagree on a figure | Band constants drifted between `method.md` and `analyze.py` | They are two copies of one method — reconcile both |
| Expiry inventory finds nothing on an account that has reservations | Swept the wrong region — reservations only exist in the region they were bought in | Pass every region the account runs in; the report names the regions swept for exactly this reason |
| `AccessDenied` on `es:DescribeReservedInstances` | Granted `opensearch:` — OpenSearch's IAM prefix is `es:` | The API is `opensearch describe-reserved-instances`; the permission is `es:DescribeReservedInstances` |
| A commitment shows a blank expiry | Looked for `End`; only EC2 and Savings Plans return one | Derive it: `StartTime + Duration` seconds |
| Savings Plan exposure looks ~1000× too small | `commitment` read as a unit count | It is USD/hour — multiply by 730 for monthly |
| `expiry` is `null` on a `generate_commitment_analysis` response | `regions` was not passed — the inventory is opt-in | Pass `regions`, or call `get_commitment_expiry` |
| DynamoDB reserved capacity missing from the inventory | No describe API exists for it, in any SDK | Permanent blind spot, declared in `INVENTORY_BLIND_SPOTS` and printed in the report |
| `ValidationException: Supported values for Service Filter are …` on reservation coverage/utilization | Used a purchase-recommendation service name in the `SERVICE` filter | Map through `RI_POSTURE_FILTER_VALUES`: MemoryDB is `Amazon MemoryDB` there, and DynamoDB has no filter value (not queried) |
| Every expiring row of one service shows the same utilization | Correct — AWS publishes utilization per service, never per commitment | Treat it as the portfolio signal it is; the report states this inline |
| A member account's RI is missing from the inventory | `Describe*` lists only the calling account's own purchases, although PAYER coverage and utilization include it | Declared in `INVENTORY_BLIND_SPOTS`; inventory from the purchasing account to see it |
| A recommendation total cannot be purchased as one reservation | `RecommendationSummary` sums every specification in `details[]` — a single RDS finding routinely spans Multi-AZ and Single-AZ | Quote the total as a budget and buy from `line_items`; each line is one purchasable spec |
| OpenSearch line has no instance type | `ESInstanceDetails` has no `InstanceType` field | Join `InstanceClass` + `InstanceSize` (`r6g` + `large` → `r6g.large`) |
| DynamoDB recommendation has an empty spec | Its spec is not under `InstanceDetails` | Read `ReservedCapacityDetails.DynamoDBCapacityDetails` — capacity units and a region, no instance |
| A Single-AZ reservation renders as Multi-AZ-unknown, or vice versa | `MultiAZ` is a **boolean**, so a truthiness test collapses `False` and absent | Test against `None`: `False` means Single-AZ, absent means AWS did not say |
| A report shows "N smaller line item(s) not shown" | Line items are capped at 25 per finding | Included in the totals; the full count is `line_items_total` |
| A line reads `(specification not returned)` | AWS gave a count without the service sub-structure | Treat the line as unpurchasable until confirmed in the console; it is not a spec of nothing |
| A Compute Savings Plan line shows `any instance family` | Correct — only `EC2_INSTANCE_SP` is pinned to a family and region | A Compute plan commits to dollars, not to a family; an empty spec is not missing data |

---

## 14. Constraints — do not violate

Restated from `SKILL.md` because they are the feature's contract, not
style preferences:

- **Read-only.** Never `CreateSavingsPlan`,
  `PurchaseReservedInstances*`, `StartCommitmentPurchaseAnalysis`,
  `ModifyReservedInstances`, `ReturnSavingsPlan`, or any other mutating or
  billable API. This feature *sizes* and *reports on* commitments; it never
  buys, renews, modifies or cancels one.
- **Never present the AWS best case as achievable.** Quote the
  risk-adjusted figure as the recommendation and the AWS figure as the
  ceiling, in that order.
- **Never add alternatives together.** Recommendations that discount the same
  usage pool are alternatives; a total counts one option per pool.
- **Never report a failed analysis as "no opportunity".**
- **Never fabricate figures.** Every number traces to a documented call
  or to arithmetic that is shown. A query you did not run is not a number
  you have.
- **Lead with blockers.** Under-utilized existing commitments come before
  the savings figure, always. A posture that could not be measured is not a
  posture that passed. A commitment expiring inside 30 days outranks
  any new purchase — it is a deadline, not an option.
- **Never imply per-commitment utilization.** AWS publishes it per service
  only. Attributing the account figure to one commitment is a fabricated
  attribution, even though the number itself is real.
- **Never convert reservation unit counts to money.** That needs per-instance
  pricing this feature does not query.
- **Never recommend a reservation without its specification.** Instance type,
  deployment option (Multi-AZ vs Single-AZ), engine and Availability Zone are
  what a reservation matches on, so "buy 4 RDS units" is not an actionable
  recommendation. And never present a multi-specification total as a single
  purchase — the total is a budget, the line items are the order.
- **Do not average Cost Explorer and COH when they disagree materially.**
  Identify the cause instead.
- **No credential exfiltration.** Never emit access keys, session tokens,
  or SSO refresh tokens. Account ID and profile name are fine.
- **Report, don't authorize.** Close every report by stating that
  commitments are non-cancellable for their full term, that figures
  should be verified in the console, and that the workload must not be
  scheduled for migration or decommissioning within the term.
