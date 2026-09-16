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

## Savings Plans

Constants: **730 hours per month**. Term length: `ONE_YEAR` = 12 months,
`THREE_YEARS` = 36 months.

**1. Measure the hourly envelope.** Sum across all entries of `detail[]`:

```
floor_hr = Σ CurrentMinimumHourlyOnDemandSpend
avg_hr   = Σ CurrentAverageHourlyOnDemandSpend
```

If `avg_hr` is 0 or absent, fall back to `avg_hr = ondemand_mo ÷ 730`. If
`commit_hr` and `savings_mo` are both 0, there is no recommendation — drop it.

**2. Classify the workload** on `ratio = floor_hr ÷ avg_hr`:

| ratio | Profile | Commitment to report | Confidence |
|---|---|---|---|
| ≥ 0.80 | stable | `commit_hr` — take the AWS figure as-is | High |
| ≥ 0.50 | moderate | `floor_hr + (commit_hr − floor_hr) × 0.5` | Medium |
| < 0.50 | spiky | `min(commit_hr, floor_hr)` — clamp to the floor | Low |
| `avg_hr` ≤ 0 | unknown | `commit_hr`, marked explicitly unvalidated | Low |

The moderate case also caps at `commit_hr` — never report a commitment above
what AWS recommended.

**3. Scale the savings.** The discount rate is fixed per plan, so savings move
linearly with commitment size:

```
scale       = safe_commit_hr ÷ commit_hr
safe_mo     = savings_mo × scale
```

Report `safe_mo` as the recommendation and `savings_mo` as the AWS ceiling.

**4. Waste exposure** — what committing at the AWS figure costs per month if
usage sits at the trough. Only meaningful when a floor was measured:

```
waste_mo = max(0, commit_hr − floor_hr) × 730        [only if floor_hr > 0]
```

**5. Break-even**, when `upfront = Σ UpfrontCost` is above 0:

```
break_even_months = upfront ÷ safe_mo
```

Use the **adjusted** `safe_mo`, not `savings_mo` — paying back against savings
you will not achieve is the error being guarded against. With no upfront cost,
break-even is **null**, not 0: reporting 0 reads as "pays back instantly".

## Reserved Instances

Identical shape, in whole units instead of dollars per hour.

```
recommended = Σ RecommendedNumberOfInstancesToPurchase
floor_units = Σ MinimumNumberOfInstancesUsedPerHour
avg_units   = Σ AverageNumberOfInstancesUsedPerHour
```

If `recommended` comes out 0, re-read using the capacity-unit field names
(`RecommendedNumberOfCapacityUnitsToPurchase`,
`MinimumNumberOfCapacityUnitsUsedPerHour`,
`AverageNumberOfCapacityUnitsUsedPerHour`) — that is how DynamoDB reports.

Same bands on `floor_units ÷ avg_units`, then **round the result down to a whole
number**. Rounding up would land the commitment above the level just judged
safe. Savings scale the same way: `safe_mo = savings_mo × (safe_units ÷ recommended)`.

Two RI-specific differences:

- **Break-even comes from the API**, not from your division: average the
  positive `break_even_mo` values across `detail[]`. This keeps the figure tied
  to what the console shows.
- **Waste is a unit count**, so convert to money:
  `waste_mo = (Σ monthly ÷ recommended) × (recommended − floor_units)`.

### Then break the total back down — the sum is not purchasable

The bands above are applied to the family total so it can be ranked against
other services. But a reservation discounts only usage matching its **exact**
specification, so a total spanning `db.r6g.large Multi-AZ` and
`db.t4g.medium Single-AZ` is a budget, not an order. Apply the same `scale` to
each `detail[]` entry and report one line per specification:

```
line_safe_units = floor(line_recommended × scale)      [whole reservations]
```

Each line carries its own `MinimumNumberOfInstancesUsedPerHour` — the count that
line never dropped below, and therefore the part of it that carries no
unused-commitment risk — plus its own `EstimatedMonthlySavingsAmount` and
`AverageUtilization`. Rank the lines by savings so the largest decision is first.

Flooring each line can leave the lines summing **below** the family total. Say by
how much and which line should absorb the remainder (the one with the highest
floor); do not pad a line silently, and do not restate the headline figure to
match — the family total is what AWS costed.

Read the spec fields per the table in `SKILL.md` step 2. A line whose
sub-structure is missing entirely is reported as *specification not returned* and
treated as unpurchasable until confirmed in the console — not as a spec of
nothing.

For Savings Plans the same breakdown applies without the rounding: dollar
commitments are divisible, and only an `EC2_INSTANCE_SP` line has a family and
region to name at all.

## Two guards that override the numbers

- **Break-even beyond the term.** If `break_even_months` exceeds the term (12 or
  36), the purchase expires before it pays back. Force confidence to **Low** and
  lead the rationale with **"Do not buy"** — recommend the no-upfront option, a
  shorter term, or nothing. This is the one case where the raw AWS API will
  endorse a purchase that loses money outright, so check it every time.
- **Posture gate.** From the step-3 posture queries: existing utilization below
  **95%**, or coverage above **90%**, is a blocker. Report blockers *before* any
  savings figure. Buying on top of an under-used commitment compounds the waste
  rather than reducing it.

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

Use the **Savings Plans** utilization figure for Savings Plans and the
**reservation** figure for reservations — they are separate metrics and crossing
them produces a confident wrong answer.

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

**6. State the limitation once.** AWS publishes SP and RI utilization at the
**account level only** — there is no per-commitment utilization API. So every
row in a family shares one figure, and the verdicts are directional. Say this in
the section; never attribute the account figure to an individual commitment.

**Precedence.** An urgent expiry outranks every new-purchase recommendation in
the report. A renewal deadline is fixed; a purchase is optional.

## Reconciliation

Compare the **unadjusted** Cost Explorer total (Σ `savings_mo`) against the
Cost Optimization Hub total (Σ `estimatedMonthlySavings`). Like-for-like: COH
also publishes a best case, so comparing your adjusted figure would manufacture
a variance that is really just this method's own adjustment.

```
delta_pct = |ce_total − coh_total| ÷ max(|ce_total|, |coh_total|) × 100
```

| delta_pct | Verdict |
|---|---|
| both totals 0 | agree-zero |
| ≤ 10% | reconciled |
| ≤ 30% | minor variance |
| > 30% | material variance |

A material variance is **flagged, never averaged away**. Usual causes: a
differing account scope (`PAYER` vs `LINKED`), or COH lagging a recent usage
change.

## Choosing what to report

Every (type, term, payment) permutation is a separate query and the winner is
not knowable in advance — a 3-year all-upfront plan can lose to a 1-year
no-upfront one once break-even is accounted for. Keep **one finding per
(family, label)**: highest `safe_mo` wins; break ties toward the **shorter term**,
then the **less upfront cash**, since that is the lower-risk purchase. Sort the
report by `safe_mo` descending.

## Worked example

`commit_hr = 10.00`, `savings_mo = 1000.00`, `floor_hr = 2.00`,
`avg_hr = 10.00`, `upfront = 0`:

```
ratio    = 2.00 ÷ 10.00 = 0.20            → below 0.50 → spiky, Low confidence
safe     = min(10.00, 2.00) = $2.00/hr
scale    = 2.00 ÷ 10.00 = 0.20
safe_mo  = 1000.00 × 0.20 = $200.00/mo    ← the recommendation
waste_mo = (10.00 − 2.00) × 730 = $5,840/mo stranded if committed at the AWS figure
break_even = null (no upfront)
```

Reported as: **$200/mo achievable** (AWS ceiling $1,000/mo), Low confidence,
because the quietest hour runs at 20% of average and committing to the AWS
figure would strand $5,840/mo in quiet hours.
