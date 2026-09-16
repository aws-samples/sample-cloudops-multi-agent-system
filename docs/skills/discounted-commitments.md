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

Representative prompts:

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
        ├─ sweep      16 SP + 32 RI recommendation permutations (6 threads)
        ├─ posture    SP/RI coverage + utilization, COH enrollment, spend
        ├─ expiry     active commitment inventory per region (opt-in, free)
        ├─ risk-adjust every recommendation down to its measured floor
        ├─ reconcile  CE total vs Cost Optimization Hub total
        └─ render     complete markdown report
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
| Form | 3 markdown files, 838 lines, **zero code** | 5 Python modules, 3,510 lines |
| Dependency | AWS CLI v2 + read-only credentials | Lambda runtime, boto3, `shared.cross_account` |
| Who executes | The host agent, via Bash | The Lambda, called through the gateway |
| Arithmetic by | The agent, following `reference/method.md` | `commitments/analyze.py` |
| Portable | Yes — copy the directory anywhere | No — platform-coupled |
| Tests | None (no code to test) | 331 tests, `tests/unit/test_commitments_*.py` |

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
rehearsal for the deployed tool.

The seven `Describe*` actions serve the expiry inventory only. Sizing works
without them: omit `regions` and the tool makes zero inventory calls and
returns `expiry: null`. Note that **OpenSearch's IAM prefix is `es:`**, not
`opensearch:` — the API is `opensearch describe-reserved-instances` but the
permission is `es:DescribeReservedInstances`.

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

Cost Explorer bills **$0.01 per recommendation request**. Coverage,
utilization, cost-and-usage, and every commitment-inventory `Describe*` call are
not billed at that rate. That distinction is enforced in code, not just
documented: `collect_all` snapshots `sweep_errors` before the expiry inventory
runs, and `queries_run` is computed from that snapshot — folding a failed free
`Describe*` into the counter would overstate the bill.

| Run | Recommendation requests | Cost |
|---|---:|---:|
| Skill default (`COMPUTE_SP` × 2 terms × 2 payments) | 4 | $0.04 |
| MCP tool default (4 SP types + 8 RI services, × 2 terms × 2 payments) | 48 | $0.48 |
| Adding `PARTIAL_UPFRONT` to the tool default | 72 | $0.72 |

The skill sweeps narrow by default and widens only on request. The tool
sweeps wide because it parallelizes and returns a full report in one
call — narrow it with `savings_plan_types` / `ri_services` when cost per
invocation matters.

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
  in `api.INVENTORY_BLIND_SPOTS` and printed in the report, because silently
  omitting it would render as "nothing expiring" for a reservation that does.
- **Utilization is account-level only.** AWS publishes no per-commitment
  utilization API, so every Savings Plan in the account shares one figure and
  every reservation shares another. The verdicts are directional and the report
  says so inline.

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
[`commitments/analyze.py`](../../src/lambda/mcp/commitments/commitments/analyze.py).
Applying it is **required**, not optional — emitting the AWS figure as
achievable is the exact error the feature exists to prevent.

Constants: `HOURS_PER_MONTH = 730.0`; `TERM_MONTHS = {ONE_YEAR: 12,
THREE_YEARS: 36}`.

### Savings Plans

Sum across all `detail[]` entries:

```
floor_hr = Σ CurrentMinimumHourlyOnDemandSpend
avg_hr   = Σ CurrentAverageHourlyOnDemandSpend      (fallback: ondemand_mo ÷ 730)
```

Classify on `ratio = floor_hr ÷ avg_hr`:

| ratio | Profile | Commitment to report | Confidence |
|---|---|---|---|
| ≥ 0.80 | stable | `commit_hr` — take the AWS figure as-is | High |
| ≥ 0.50 | moderate | `floor_hr + (commit_hr − floor_hr) × 0.5` | Medium |
| < 0.50 | spiky | `min(commit_hr, floor_hr)` — clamp to the floor | Low |
| `avg_hr` ≤ 0 | unknown | `commit_hr`, marked explicitly unvalidated | Low |

Thresholds are `STABLE_FLOOR_RATIO = 0.80` and
`MODERATE_FLOOR_RATIO = 0.50`. The moderate case also caps at `commit_hr`
— never report a commitment above what AWS recommended.

Savings scale linearly with commitment size, because the discount rate is
fixed per plan:

```
scale    = safe_commit_hr ÷ commit_hr
safe_mo  = savings_mo × scale                    ← the recommendation
waste_mo = max(0, commit_hr − floor_hr) × 730    ← stranded at the AWS figure
break_even_months = upfront ÷ safe_mo            ← null, not 0, when upfront = 0
```

Break-even uses the **adjusted** `safe_mo`. Paying back against savings
you will not achieve is the error being guarded against. With no upfront
cost break-even is `null` — reporting `0` reads as "pays back instantly".

### Reserved Instances

Same shape in whole units instead of dollars per hour, with three
differences:

- **Round down.** Rounding up lands the commitment above the level just
  judged safe.
- **Break-even comes from the API** (average the positive
  `EstimatedBreakEvenInMonths` across `detail[]`), not from your own
  division, so the figure matches the console.
- **Waste is a unit count**, so convert:
  `waste_mo = (Σ monthly ÷ recommended) × (recommended − floor_units)`.

If `recommended` comes out 0, re-read using the capacity-unit field names
(`RecommendedNumberOfCapacityUnitsToPurchase`, etc.) — that is how
DynamoDB reports.

### Two guards that override the numbers

- **Break-even beyond the term.** If `break_even_months` exceeds 12 or 36
  as applicable, the purchase expires before it pays back. Force
  confidence to **Low** and lead the rationale with **"Do not buy"**.
  This is the one case where the raw AWS API will endorse a purchase that
  loses money outright, so it is checked every time.
- **Posture gate.** Existing utilization below `UTILIZATION_WARN_PCT =
  95.0`, or coverage above `COVERAGE_SATURATED_PCT = 90.0`, is a blocker,
  reported *before* any savings figure.

### Reconciliation

Compare the **unadjusted** Cost Explorer total (Σ `savings_mo`) against
the COH total (Σ `estimatedMonthlySavings`). Like-for-like: COH also
publishes a best case, so comparing the adjusted figure would manufacture
a variance that is really just this method's own adjustment.

```
delta_pct = |ce_total − coh_total| ÷ max(|ce_total|, |coh_total|) × 100
```

| `delta_pct` | Verdict |
|---|---|
| both totals 0 | agree-zero |
| ≤ 10% | reconciled |
| ≤ 30% | minor variance |
| > 30% | material variance |

A material variance is **flagged, never averaged away** — a blended
number is defensible to nobody. Usual causes: differing account scope
(`PAYER` vs `LINKED`), or COH lagging a recent usage change.

### Choosing what to report

Keep **one finding per (family, label)**: highest `safe_mo` wins; break
ties toward the **shorter term**, then the **less upfront cash**, since
that is the lower-risk purchase. Sort the report by `safe_mo` descending.

### Renewals — a different decision from a new purchase

Implemented in `analyze.analyze_expiry`, which takes `as_of` as a parameter
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

Verdicts, on the family's utilization figure:

| Utilization | Action | Reasoning |
|---|---|---|
| ≥ `UTILIZATION_WARN_PCT` (95%) | `renew` | Being consumed; lapsing returns that spend to on-demand |
| ≥ `RENEW_LAPSE_PCT` (50%) | `renew-smaller` | Partly wasted — renew at the consumed portion, using the free resize point |
| < 50% | `let-lapse` | Over half unused; re-size from current usage instead of renewing the mistake |
| `None` | `review` | Stated plainly. A verdict with no utilization behind it is a guess. |

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

`commit_hr = 10.00`, `savings_mo = 1000.00`, `floor_hr = 2.00`,
`avg_hr = 10.00`, `upfront = 0`:

```
ratio      = 2.00 ÷ 10.00 = 0.20         → below 0.50 → spiky, Low confidence
safe       = min(10.00, 2.00) = $2.00/hr
scale      = 2.00 ÷ 10.00 = 0.20
safe_mo    = 1000.00 × 0.20 = $200.00/mo   ← the recommendation
waste_mo   = (10.00 − 2.00) × 730 = $5,840/mo stranded at the AWS figure
break_even = null (no upfront)
```

Reported as **$200/mo achievable** against an AWS ceiling of $1,000/mo,
Low confidence, because the quietest hour runs at 20% of average.

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
`commitments___generate_commitment_analysis` and its three siblings.

Deployed as `${PROJECT_TAG}-commitments-tool`: `python3.12`, **300 s**
timeout, **1024 MB**, both cross-account role aliases wired
(`CROSS_ACCOUNT_ROLE_ARN` for Cost Explorer,
`CROSS_ACCOUNT_ROLE_ARN_COH` for Cost Optimization Hub — same split as
the `cost-optimization-hub` tool, because COH can be enabled on a
delegated admin account separate from the payer).

### The five tools

| Tool | Use when | Returns |
|---|---|---|
| `generate_commitment_analysis` | Default. Any purchase question. | `report_markdown` (complete report) + structured envelope + posture + blockers + reconciliation + `expiry` when `regions` is passed |
| `size_savings_plans` | The question is narrowed to Savings Plans | Risk-adjusted SP findings, **no posture gate** |
| `size_reservations` | The question is narrowed to reservations | Risk-adjusted RI findings in whole units |
| `get_commitment_posture` | Only existing coverage/utilization is asked about | Coverage, utilization, blockers, `safe_to_buy_more`, COH enrollment, eligible spend |
| `get_commitment_expiry` | "What's expiring?", "What should we renew?" | Active commitment inventory bucketed urgent/soon/upcoming with a renew / renew-smaller / let-lapse / review verdict each, plus exposure totals and declared blind spots |

`generate_commitment_analysis` is the preferred path and the agent prompt
says so. It is one call, and it is the only one that includes both the
posture gate and the reconciliation the numbers are defensible with. It
takes up to ~90 s for a full sweep.

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
├── handler.py                 603 lines — event → params → clients → collect
└── commitments/
    ├── api.py                 661 — the read-only AWS calls, enums, labels,
    │                                 InventorySpec table, inventory getters
    ├── analyze.py             694 — bands, sizing, break-even, posture,
    │                                 reconcile, expiry buckets + verdicts
    ├── collect.py             552 — permutation sweep, thread pool, region
    │                                 validation, expiry sweep, envelope
    └── report.py              556 — markdown rendering
```

The `commitments/` subpackage stays boto3-free even though the inventory needs
regional clients. `api.Clients` carries a `make_client: Callable[[str, str], Any]`
factory as a **defaulted trailing field**, so `collect.py` and `analyze.py` never
import boto3 and every pre-existing `Clients(...)` construction still works
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

  "recommendations": [ ... ],
  "count": 3,
  "total_estimated_monthly_savings": 1840.22,
  "aws_best_case_monthly_savings": 3011.75,

  "reconciliation": {"status": "reconciled", "delta_pct": 4.1, ...},
  "existing_commitment_posture": { ... },
  "blockers": ["Savings Plans utilization 82.0% is below the 95% floor"],
  "expiry": null,          // populated only when `regions` was passed
  "queries_run": 48,
  "collection_warnings": [],
  "data_source": "live"
}
```

`queries_run` counts **billable** Cost Explorer recommendation requests only. It
is derived from `payload["sweep_errors"]`, a snapshot taken before the free
expiry `Describe*` calls run, so a region with no reservations does not appear
as $0.07 of nonexistent spend.

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
  "counts": {"urgent": 1, "soon": 2, "upcoming": 0},
  "actions": {"renew": 2, "renew-smaller": 1, "let-lapse": 0, "review": 0},
  "hourly_commitment_expiring": 5.5,
  "monthly_committed_spend_expiring": 4015.0,
  "reserved_units_expiring": 4.0,
  "blind_spots": ["DynamoDB reserved capacity (no describe API exists)"],
  "renewal_actions": { ... },
  "collection_warnings": [],
  "note": "...account-level utilization, read-only...",
  "data_source": "live"
}
```

Per commitment: `family` (`savings-plan` | `reservation`), `service`, `label`,
`commitment_id`, `arn`, `instance_type`, `attributes`, `spec`, `quantity`,
`unit`, `region`, `state`, `payment_option`, `start`, `end`, `term_months`,
`days_remaining`, `urgency` (`urgent` | `soon` | `upcoming` | `expired`),
`utilization_pct`, `action` (`renew` | `renew-smaller` | `let-lapse` |
`review`), `rationale`.

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
  "commitment_family": "sp",
  "commitment_type": "Compute Savings Plan",
  "term": "ONE_YEAR",
  "payment_option": "NO_UPFRONT",
  "aws_recommended_commitment": 10.0,
  "achievable_commitment": 2.0,
  "commitment_unit": "USD/hour",
  "estimated_monthly_savings": 200.0,
  "aws_best_case_monthly_savings": 1000.0,
  "estimated_savings_percentage": 21.4,
  "upfront_cost": 0.0,
  "break_even_months": null,
  "waste_exposure_monthly": 5840.0,
  "confidence": "Low",
  "spend_profile": "spiky",
  "implementation_effort": "Low",
  "rationale": ["..."],
  "line_items": [ ... ]
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
`aws_recommended_commitment`, `achievable_commitment`, `minimum_observed_units`,
`average_observed_units`, `estimated_monthly_savings`, `upfront_cost`,
`monthly_on_demand_cost`, `estimated_utilization_percentage` (`null` when AWS did
not return it), `size_flexible`, `current_generation`, `account_id`. See §5b for
where each field comes from.

Key names deliberately mirror a Cost Optimization Hub recommendation
list, so a host that already renders those needs no translation layer.
`reconciliation` is omitted when there was nothing to reconcile, so a
sizing-only response does not carry an empty key.

### Report structure

Eight sections, in this order, emitted by `report.py:render()` and
mirrored in
[`reference/output-template.md`](../../skills/discounted-commitments/reference/output-template.md):

1. **Bottom line** — AWS best case vs risk-adjusted achievable vs
   high-confidence-only, monthly and annual; leads with the urgent-expiry
   count when there is one
2. **Reconciliation against AWS native tools**
3. **Existing commitment health** — blockers first
4. **Commitment expiry and renewal** — regions swept, exposure, the
   expiring table (8 columns: ends, days, commitment, **spec**, size, region,
   utilization, action), the account-level-utilization caveat, per-row
   reasoning, already-ended commitments with their spec, declared blind
   spots. Omitted entirely when the inventory did not run.
5. **Recommended commitments** — summary table, then per-finding detail
   ending in a **line items** table (`Buy | Region | AWS units | Floor |
   Achievable | Utilization | Savings/mo`) that names the instance type and
   deployment option for each purchase, flags size-flexible and
   previous-generation lines, and discloses any whole-unit rounding shortfall.
   The table is omitted when no line has anything to buy.
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
   first; then High-confidence rows in descending achievable-savings
   order; treat Medium as a smaller first tranche and re-measure after 30
   days; never buy a row whose break-even exceeds its own term.

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
# 331 passed
```

| File | Covers |
|---|---|
| `test_commitments_analyze.py` | Bands at their boundaries, sizing, break-even, posture, reconciliation, selection |
| `test_commitments_api.py` | The Cost Explorer / COH calls, enum validation, response parsing |
| `test_commitments_collect.py` | Service/type resolvers, permutation sweep, partial-failure handling, envelope key names and units, plus `test_module_builds_no_clients_and_touches_no_files` — the guard that keeps `collect.py` free of clients and filesystem access |
| `test_commitments_expiry.py` | Per-family field mapping and derived end dates, date coercion across SDK/CLI shapes, expiry buckets and renewal verdicts at their boundaries, unit separation, region validation, expiry rendering, and that the Savings Plans job is **not** multiplied per region |
| `test_commitments_spec.py` | The per-service spec shapes (all seven, including the OpenSearch two-field type and the DynamoDB container), `MultiAZ` as a boolean, per-line breakdown and ranking, whole-unit rounding and its disclosure, the report's line-items and expiry `Spec` columns, and `line_items` in the envelope |
| `test_commitments_tool.py` | Handler parameter validation, error normalization, `TestHandlerDiscipline`, and `tools.json` wiring |
| `test_commitments_contracts.py` | Every AWS field name in `RECOMMENDATION_SPECS` and `RESERVATION_INVENTORY` checked against botocore's shipped service models — see below |

The band tests pin the risk-adjustment thresholds *at* their boundaries
(0.80, 0.50, 95%, 90%, and the 30/60/90-day expiry cutoffs), which is what
makes a constant change visible rather than silent. Three expiry tests exist
specifically because their failure modes are silent rather than loud:

- **Derived end dates, per family.** A wrong field name yields rows with blank
  IDs and no expiry instead of an error, so every family is asserted, not just
  EC2.
- **One Savings Plans call regardless of region count.** Sweeping SPs per region
  would return the same plans N times and inflate every total by N.
- **`queries_run` excludes free `Describe*` failures.** Asserted directly,
  because folding them in overstates a real dollar figure.

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
- **`ACTIVE_SP_STATES` are valid `SavingsPlanState` enum values.** These go to
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
| A `$0` / no-recommendation result | A real result, not a failure | Check eligible spend and explain there is nothing to commit against |
| Recommendation figure is not purchasable by the account | `PAYER` scope aggregates the whole org | Re-run with `--account-scope LINKED` |
| Endpoint resolution failure | Called outside us-east-1 | CE purchase recommendations and COH are us-east-1-only |
| COH empty or `NOT_ENROLLED` | Not enrolled | Continue; state that figures rest on Cost Explorer alone |
| Response blows up the context window | `--query` omitted | Always send it — bare responses run to tens of KB of per-instance detail |
| Unexpectedly large Cost Explorer bill | Wide sweep, repeated | $0.01 per recommendation request; note results as you go and never re-query a number you already have |
| Tool times out | Full 48-permutation sweep on a slow account | 300 s Lambda timeout; narrow `savings_plan_types` / `ri_services` |
| Agent reformats or summarizes the report | Model ignoring the verbatim rule | The template and agent prompt both mandate verbatim `report_markdown`; tighten if it recurs |
| Skill and Lambda disagree on a figure | Band constants drifted between `method.md` and `analyze.py` | They are two copies of one method — reconcile both |
| Expiry inventory finds nothing on an account that has reservations | Swept the wrong region — reservations only exist in the region they were bought in | Pass every region the account runs in; the report names the regions swept for exactly this reason |
| `AccessDenied` on `es:DescribeReservedInstances` | Granted `opensearch:` — OpenSearch's IAM prefix is `es:` | The API is `opensearch describe-reserved-instances`; the permission is `es:DescribeReservedInstances` |
| A commitment shows a blank expiry | Looked for `End`; only EC2 and Savings Plans return one | Derive it: `StartTime + Duration` seconds |
| Savings Plan exposure looks ~1000× too small | `commitment` read as a unit count | It is USD/hour — multiply by 730 for monthly |
| `expiry` is `null` on a `generate_commitment_analysis` response | `regions` was not passed — the inventory is opt-in | Pass `regions`, or call `get_commitment_expiry` |
| DynamoDB reserved capacity missing from the inventory | No describe API exists for it, in any SDK | Permanent blind spot, declared in `INVENTORY_BLIND_SPOTS` and printed in the report |
| Every expiring row shows the same utilization | Correct — AWS publishes utilization account-wide, never per commitment | Treat it as the portfolio signal it is; the report states this inline |
| A recommendation total cannot be purchased as one reservation | `RecommendationSummary` sums every specification in `details[]` — a single RDS finding routinely spans Multi-AZ and Single-AZ | Quote the total as a budget and buy from `line_items`; each line is one purchasable spec |
| OpenSearch line has no instance type | `ESInstanceDetails` has no `InstanceType` field | Join `InstanceClass` + `InstanceSize` (`r6g` + `large` → `r6g.large`) |
| DynamoDB recommendation has an empty spec | Its spec is not under `InstanceDetails` | Read `ReservedCapacityDetails.DynamoDBCapacityDetails` — capacity units and a region, no instance |
| A Single-AZ reservation renders as Multi-AZ-unknown, or vice versa | `MultiAZ` is a **boolean**, so a truthiness test collapses `False` and absent | Test against `None`: `False` means Single-AZ, absent means AWS did not say |
| Line items sum below the finding's achievable total | Each line is floored to whole reservations | Expected; the report names the shortfall and the line that should absorb it — do not pad a line or restate the headline |
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
- **Never fabricate figures.** Every number traces to a documented call
  or to arithmetic that is shown. A query you did not run is not a number
  you have.
- **Lead with blockers.** Under-utilized existing commitments come before
  the savings figure, always. A commitment expiring inside 30 days outranks
  any new purchase — it is a deadline, not an option.
- **Never imply per-commitment utilization.** AWS publishes it account-wide
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
