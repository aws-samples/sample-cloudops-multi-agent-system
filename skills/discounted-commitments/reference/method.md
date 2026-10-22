# How the risk adjustment works

Apply this to every recommendation before reporting it. The AWS APIs return a
best case; these steps turn it into a figure a workload can actually sustain,
and into the two warnings the raw API will never give you.

Every input is a field from the responses in `SKILL.md`. Nothing here is
estimated, and no step needs data you did not query.

**Reading rules for Cost Explorer values.** Money and percentages arrive as JSON
*strings*, and absent values arrive as `""` — treat `""` and `null` as "no
data", never as `0`. A `""` floor is not a floor of zero; it means the floor
could not be measured, which is the `unknown` band below.

## Size each line, not the family

Every `detail[]` entry is one purchasable line — one specification, or one
member account under `LINKED` scope. **Size each line against its own floor
and average, then add the lines up.** A family-wide ratio would let a flat
`m5.large` fleet subsidize a spiky `r6g.4xlarge` one, and the per-line
results would not add back up to it.

The bands, on each line's `ratio = floor ÷ avg` (both in the line's
commitment unit — see the Savings Plans conversion below):

| ratio | Profile | Commitment to report | Confidence |
|---|---|---|---|
| ≥ 0.80 | stable | `commit` — take the AWS figure as-is | High |
| ≥ 0.50 | moderate | `floor + (commit − floor) × 0.5` | Medium |
| < 0.50 | spiky | `min(commit, floor)` — clamp to the floor | Low |
| floor or avg not returned | unknown | `commit`, marked explicitly unvalidated | Low |

Never report a commitment above what AWS recommended. A **measured** floor of 0
is a real trough and clamps to 0; a **blank** floor is unknown and keeps the
AWS figure at Low confidence — there is no evidence to trim it with.

Savings and upfront scale with each line's achievable size:

```
line_safe_mo      = line_savings_mo × (line_achievable ÷ line_commit)
line_safe_upfront = line_upfront    × (line_achievable ÷ line_commit)
```

Family totals are the sums of the lines. Family confidence is the
**savings-weighted** blend of its lines (High = 2, Medium = 1, Low = 0; average
≥ 1.5 → High, ≥ 0.5 → Medium, else Low).

If every line leaves a figure blank but the summary has it (savings, or an SP
commitment), allocate the summary figure across the lines pro rata. Never
treat a *negative* line savings figure as blank — it is a purchase that loses
money.

## Savings Plans

Constants: **730 hours per month**. Term length: `ONE_YEAR` = 12 months,
`THREE_YEARS` = 36 months. Lookback hours: 168 / 720 / 1440 for 7 / 30 / 60
days.

**1. Convert the floor into commitment dollars.** `commit_hr` is priced at
Savings Plan rates; `floor_od_hr` and `avg_od_hr` are **on-demand** dollars. A
commitment of $c/hr is only fully used once on-demand usage reaches c ÷ (1 − d),
so compare like with like. Per detail:

```
r         = sp_cost × (est_util ÷ 100) ÷ (sp_cost + sp_savings)   ← = 1 − effective discount
floor_hr  = floor_od_hr × r
avg_hr    = avg_od_hr × r
```

If a detail lacks `sp_cost`, use the `sp_cost`-weighted `r` of the other
details. If none has it, **do not use the floor at all** — keep the AWS figure
at Low confidence and say the floor could not be converted. Comparing an
on-demand floor against an SP commitment directly overstates what the trough
can absorb.

With a single detail and no `avg_od_hr`, fall back to
`avg_od_hr = ondemand_lookback ÷ lookback_hours` — **not** ÷ 730, which is only
right for a 30-day lookback and is 4.3× off for seven days. If `commit_hr` and
`savings_mo` are both 0, there is no recommendation — drop it.

Worked: 3-year plan, 55% off, on-demand floor $4/hr, AWS commitment $3.60/hr.
$3.60 < $4 looks safe in on-demand dollars; in commitment dollars the trough
absorbs $4 × 0.45 = **$1.80/hr**, leaving $1.80/hr ($1,314/mo) unused in the
quietest hour.

**2. Classify and size each line** with the bands above, on
`floor_hr ÷ avg_hr`.

**3. Waste exposure** — what a commitment costs per month if usage sits at the
trough, in the same unit as the floor. Report it at both sizes, and only when a
floor was measured:

```
waste_mo     = max(0, achievable_hr − floor_hr) × 730    ← at the size you recommend
aws_waste_mo = max(0, commit_hr     − floor_hr) × 730    ← at the AWS size
```

**4. Cash break-even**, when the line's `upfront` is above 0. `savings_mo` is
savings **net of the amortized upfront**; the cash saved each month is that plus
the amortized upfront:

```
break_even_months = upfront ÷ (savings_mo + upfront ÷ term_months)
```

Never divide the upfront by `savings_mo` alone — that overstates break-even by
1/d and rejects every All-Upfront plan with a discount under 50%. (1-year All
Upfront at 28% off on $1,000/mo on-demand: $8,640 upfront, $280/mo net — 30.9
months the wrong way, **8.6 months** in cash.) It does not depend on the line's
size. With no upfront, break-even is **null**, not 0: reporting 0 reads as "pays
back instantly". If the denominator is ≤ 0 the purchase never pays back. For
the family, weight each line's break-even by its upfront.

## Reserved Instances

Same per-line sizing, in whole units, **rounded down** per line — rounding up
would land a line above the level just judged safe.

```
commit = RecommendedNumberOfInstancesToPurchase
floor  = MinimumNumberOfInstancesUsedPerHour
avg    = AverageNumberOfInstancesUsedPerHour
```

If the instance fields are blank, read the capacity-unit names
(`RecommendedNumberOfCapacityUnitsToPurchase`,
`MinimumNumberOfCapacityUnitsUsedPerHour`,
`AverageNumberOfCapacityUnitsUsedPerHour`) — that is how DynamoDB reports.

RI-specific differences:

- **Break-even comes from the API** (`break_even_mo`) so it ties to the
  console; compute the cash formula above only when AWS returns none. For the
  family, **weight by upfront** — an unweighted mean lets a $50 line's two
  months pull a $50,000 line's eleven down to six and a half.
- **Waste is priced per idle reservation**, amortized upfront included:
  `unit_cost_mo = (upfront ÷ term_months + monthly) ÷ commit` and
  `waste_mo = max(0, achievable − floor) × unit_cost_mo`. The recurring fee alone
  is $0 for every All-Upfront RI and would price its waste at nothing.
- **For the family's descriptive volatility label**, use the
  `*NormalizedUnitsUsedPerHour` fields if you queried them; raw counts across
  sizes do not add (4 × large and 4 × 4xlarge are not "8 of something").

### The total is a budget; the lines are the order

A reservation discounts only usage matching its **exact** specification, so a
total spanning `db.r6g.large Multi-AZ` and `db.t4g.medium Single-AZ` is a budget,
not an order. Report one line per `detail[]` entry with its spec, its own floor,
achievable size and achievable savings, ranked by achievable savings. Because
each line is sized on its own, the lines add up to the total exactly — there is
no rounding shortfall to explain.

Read the spec fields per the table in `SKILL.md` step 2. A line whose
sub-structure is missing entirely is reported as *specification not returned* and
treated as unpurchasable until confirmed in the console — not as a spec of
nothing.

For Savings Plans the same breakdown applies without the rounding: dollar
commitments are divisible, and only an `EC2_INSTANCE_SP` line has a family and
region to name at all.

## Two guards that override the numbers

- **Break-even beyond the term.** A line whose break-even exceeds its term (12
  or 36 months) cannot pay back: exclude it (achievable 0) and say so. If every
  line fails, the finding is **Do not buy** — Low confidence, the rationale
  leads with it, and it is never selected and never totalled. Recommend the
  no-upfront option, a shorter term, or nothing.
- **Posture gate.** From the step-3 posture queries: existing utilization below
  **95%**, or Savings Plans coverage above **90%**, is a blocker. Report blockers
  *before* any savings figure. Reservation posture is **per service** — use the
  filtered figure for the service you are sizing, never the unfiltered (EC2)
  one. A measured 0% utilization is a blocker, not missing data. If a posture
  query failed for any reason other than "no data", posture is **unmeasured**:
  say so, and do not present the purchase as cleared.

## One purchase per usage pool — never add alternatives

Cost Explorer sizes each Savings Plan type and each RI service independently
against the **same** on-demand usage. Group findings by the usage they discount:

| Pool | Options — alternatives to each other |
|---|---|
| EC2 / Fargate / Lambda compute | Compute SP · EC2 Instance SP · EC2 RIs |
| Database instances | Database SP · RDS + ElastiCache + DynamoDB RIs (these add up — they cover different usage) |
| SageMaker | SageMaker SP |
| Redshift · MemoryDB | their RIs |
| OpenSearch | OpenSearch RIs · Elasticsearch (legacy) RIs |

In each pool, recommend the option with the most achievable savings and list
the others as **alternatives — instead of, not in addition to**. Every total
(achievable, AWS best case, High-confidence subset) adds only the recommended
option per pool. Summing a Compute SP, an EC2 Instance SP and EC2 RIs counts the
same EC2 spend three times.

## Renewals — deciding what to do with an expiring commitment

This is a different question from sizing a new purchase, and the difference is
the whole point: **a lapsing commitment has zero switching cost.** Expiry is the
one moment the size can change without buying out of anything, so the bands here
are allowed to be less conservative than a net-new buy — and equally, "let it
lapse" is a real answer rather than a failure.

**1. Derive the end date.** Only EC2 returns `End`. Everywhere else:

```
end = StartTime + Duration seconds       (31536000 = 1 year, 94608000 = 3 years)
```

An unparseable or absent date makes the commitment **undated** — list it as
needing manual checking. Do not silently drop it, and do not guess.

**2. Bucket by `days_remaining = end − today`:**

| days_remaining | Bucket | Why it matters |
|---|---|---|
| < 0 | **expired** | Already back at on-demand rates. Report separately and first — this is a live cost, not a deadline. |
| ≤ 30 | urgent | Too close to run a full sizing exercise before it lapses. |
| ≤ 60 | soon | Enough time to size a replacement properly. |
| ≤ horizon (default 90) | upcoming | Note it; no action this month. |
| > horizon | not listed | Counted in the total, kept out of the table. |

**3. Verdict, from utilization:**

| Utilization | Action | Reasoning to report |
|---|---|---|
| ≥ 95% | **renew** | The commitment is being consumed; letting it lapse returns that spend to on-demand rates. |
| 50–95% | **renew smaller** | Partly wasted. Renew at roughly the consumed portion — expiry is a zero-cost resize point, which mid-term it never is. |
| < 50% | **let lapse** | More than half the commitment is unused. Re-size from current usage instead of renewing the mistake. |
| not measured | **review** | State that plainly. A verdict with no utilization behind it is a guess wearing a recommendation's clothes. |
| any, with a matching **queued** purchase | **already queued** | A renewal is scheduled. Recommending it again risks a second, non-cancellable purchase of the same usage. |

A queued purchase matches when family, service, region, instance type and the
full spec agree — a queued Single-AZ reservation does not renew a Multi-AZ one.
If it covers less than the expiring quantity, say how much.

Use the **Savings Plans** utilization figure for Savings Plans and **that
service's** filtered reservation figure for each reservation — they are separate
metrics, and crossing them (or judging an RDS renewal on the unfiltered EC2
figure) produces a confident wrong answer.

**4. Quantify the exposure, in the right unit.**

```
hourly_expiring  = Σ commitment            (Savings Plans only, USD/hour)
monthly_expiring = hourly_expiring × 730
units_expiring   = Σ instance/node count   (reservations only)
```

Do **not** convert reservation unit counts to dollars. That needs per-instance
pricing this method never queries, and an invented rate is a fabricated figure.
Report units as units.

**5. Carry the specification into the verdict.** "Renew" means renew *the same
thing*: the same instance class, the same deployment option (Multi-AZ vs
Single-AZ), the same engine, and the same Availability Zone when the reservation
is zonal. A renewal that changes any of those is a new purchase and needs the
sizing method above, not a renewal verdict. So state the spec on every row —
`db.r6g.large · Multi-AZ · postgresql`, not "2 RDS units" — and read `MultiAZ` as
the boolean it is: `false` means Single-AZ, absent means AWS did not say.

**6. State the limitation once.** AWS publishes utilization per Savings Plan
portfolio and per reservation service — there is no per-commitment utilization
API. So every row of one service shares one figure, and the verdicts are
directional. Say this in
the section; never attribute the account figure to an individual commitment.

**Precedence.** An urgent expiry outranks every new-purchase recommendation in
the report. A renewal deadline is fixed; a purchase is optional.

## Reconciliation — like for like

Cost Optimization Hub prices every commitment at **one** preferred term and
payment option (`get-preferences`; 3-year All Upfront by default), and has no
Database Savings Plan type. A raw total-vs-total comparison is therefore usually
an artifact. Compare:

1. only your findings **at COH's term and payment** — if you have none, the
   verdict is *not comparable*;
2. **excluding Database Savings Plans**, and naming the exclusion;
3. **one option per usage pool on both sides** (above), at the AWS best case —
   COH publishes a best case too, so comparing your adjusted figure would
   manufacture a variance that is really this method's own adjustment.

```
delta_pct = |ce_total − coh_total| ÷ max(|ce_total|, |coh_total|) × 100
```

| delta_pct | Verdict |
|---|---|
| both totals 0 | agree-zero |
| ≤ 10% | reconciled |
| ≤ 30% | minor variance |
| > 30% | material variance |
| no finding at COH's term/payment | not comparable |

A material variance is **flagged, never averaged away**. Usual causes once the
above is applied: a differing account scope (`PAYER` vs `LINKED`), COH read in
a different account, or COH lagging a recent usage change.

## Choosing what to report

Every (type, term, payment) permutation is a separate query and the winner is
not knowable in advance — a 3-year all-upfront plan can lose to a 1-year
no-upfront one once break-even is accounted for. Keep **one finding per
(family, label)**: purchases that pay back first, then the highest achievable
savings; break ties toward the **shorter term**, then the **less upfront
cash**. Then apply the usage pools and list recommended rows first.

## Worked example

One Compute SP detail: `commit_hr = 10.00`, `sp_cost = 7300`,
`sp_savings = 3128.57`, `est_util = 100` (30% effective discount),
`floor_od_hr = 4.00`, `avg_od_hr = 15.00`, `savings_mo = 3128.57`, `upfront = 0`:

```
r          = 7300 × 1.00 ÷ (7300 + 3128.57) = 0.70
floor_hr   = 4.00 × 0.70 = $2.80/hr          avg_hr = 15.00 × 0.70 = $10.50/hr
ratio      = 2.80 ÷ 10.50 = 0.27             → below 0.50 → spiky, Low confidence
achievable = min(10.00, 2.80) = $2.80/hr
safe_mo    = 3128.57 × 0.28 = $876.00/mo     ← the recommendation
aws_waste  = (10.00 − 2.80) × 730 = $5,256/mo stranded at the AWS size
break_even = null (no upfront)
```

Reported as: **$876/mo achievable** (AWS ceiling $3,129/mo), Low confidence,
because the quietest hour absorbs only 28% of the AWS commitment. Comparing
the $4.00 on-demand floor to the $10.00 commitment unconverted would have
reported $1,251/mo — 43% too high.
