# Output template

Emit this structure. A host tool that returns its own rendered report already
follows it — pass that through verbatim rather than reformatting.

Order is deliberate: a reader cannot reach the savings number without passing
the blockers. Preserve it. Emit only the sections you have data for, and name
the ones you dropped.

```markdown
# AWS Discounted Commitments Report

**Account:** [account ID]
**Profile:** `[profile]`
**Generated:** [timestamp]
**Lookback:** [7/30/60 days] | **Account scope:** [PAYER/LINKED]
**Source APIs:** Cost Explorer (purchase recommendations, coverage + utilization),
Cost Optimization Hub (ListRecommendations), Savings Plans and per-service
reservation inventory (`Describe*`)

All data is read-only. This report does not purchase anything.

## Bottom line

| Measure | Monthly | Annual |
|---|---:|---:|
| AWS best-case savings (as the console shows) | $[X,XXX.XX] | $[XX,XXX.XX] |
| **Risk-adjusted achievable savings** | **$[X,XXX.XX]** | **$[XX,XXX.XX]** |
| High-confidence subset only | $[X,XXX.XX] | $[XX,XXX.XX] |

The risk-adjusted figure is [X]% below the AWS best case. [+ "Do not act on
these numbers yet" when blockers exist]

[**[n] existing commitment(s) expire within 30 days.** That deadline comes before
any new purchase — see *Commitment expiry and renewal*. — present only when the
urgent count is above zero]

_(or, with no findings: "**No commitment opportunity found.**" — a real result,
not a failure; Eligible spend below shows why)_

## Reconciliation against AWS native tools

[verdict: reconciled / minor variance / MATERIAL VARIANCE / not reconciled]

| Pipeline | Recommended monthly savings |
|---|---:|
| Cost Explorer purchase recommendations | $[X,XXX] |
| Cost Optimization Hub ([n] commitment recs) | $[X,XXX] |
| Delta | $[X,XXX] ([X]%) |

## Existing commitment health

### Blockers
- **[blocker — e.g. Savings Plans utilization 82%, below the 95% floor]**

| Metric | Value |
|---|---:|
| Savings Plans coverage / utilization | [X]% / [X]% |
| Unused SP commitment | $[X] |
| Reservation coverage / utilization | [X]% / [X]% |
| Unused reservation hours | [n] |

_(or prose when no commitments exist — all-zero metrics mean nothing to
measure, not a wasted commitment)_

## Commitment expiry and renewal

Inventory taken [date] over a [90]-day horizon. Reservation regions swept:
[regions]. Savings Plans are account-level and are listed once regardless of
region.

**[n] commitment(s) expire within [90] days** — [n] within 30 days, [n] within
60, [n] within 90.

Exposure: $[X,XXX]/mo of committed Savings Plan spend ($[X.XXXX]/hr) reverts to
on-demand rates if not renewed; [n] reserved unit(s) lose their discount. The
dollar value of that is not stated because it needs per-instance pricing this
analysis does not query.

| Ends | Days | Commitment | Spec | Size | Region | Utilization | Action |
|---|---:|---|---|---:|---|---:|---|
| [YYYY-MM-DD] | [n] | [label] `[id]` | [m5 or —] | $[X.XXXX]/hr | [region] | [X]% | **renew** |
| [YYYY-MM-DD] | [n] | RDS `[id]` | db.r6g.large · Multi-AZ · postgresql | [n] unit(s) | [region] | [X]% | let lapse |

> Utilization is the account-level figure from Cost Explorer, not
> per-commitment — there is no API that reports utilization for an individual
> Savings Plan or reservation. Treat it as the portfolio signal it is, and
> confirm a specific commitment in the console before acting.

> *Spec* is what a renewal has to match. A reservation bought against a
> different instance class, deployment option (Single-AZ vs Multi-AZ) or engine
> does not cover the same usage, so a renewal that changes any of these is a new
> purchase and needs fresh sizing. An empty spec on a Compute Savings Plan is
> correct — it commits to dollars, not to a family.

### Why

- `[id]` — [rationale citing the utilization figure it relied on]

### Already ended but still listed as active

- `[id]` ([label] [spec]) — ended [n] days ago; that spend is already at on-demand rates

[n] commitment(s) returned no usable end date and could not be assessed: `[id]`.

Not covered by this inventory: DynamoDB reserved capacity (no describe API
exists).

_(or, when nothing is expiring: "**No commitment expires within [90] days.** [n]
active commitment(s) were inventoried." — omit the table entirely. Omit the whole
section when the expiry inventory was not run, and say in Method that it was
skipped.)_

## Recommended commitments

| # | Commitment | Term | Payment | AWS commitment | Achievable commitment | Achievable savings/mo | Discount | Confidence |
|---|---|---|---|---:|---:|---:|---:|---|
| 1 | [label] | 1-year | No upfront | $[X.XXXX]/hr | $[X.XXXX]/hr | $[X,XXX] | [X]% | High |

### Detail

#### 1. [label] — [term], [payment]

- **Confidence:** [High/Medium/Low] (spend profile: [stable/moderate/spiky])
- **Commitment:** AWS recommends [X]; this report recommends [Y]
- **Savings:** $[X]/mo achievable ($[Y]/mo at the AWS best case)
- **Upfront cost:** $[X]
- **Break-even:** [X] months [+ "longer than the [n]-month term, so this
  purchase cannot pay back"]
- **Waste exposure at the AWS figure:** up to $[X]/mo unused if usage falls to
  its observed floor
- **Line items analyzed:** [n]

Line items — what to buy:

| Buy | Region | AWS units | Floor | Achievable | Utilization | Savings/mo |
|---|---|---:|---:|---:|---:|---:|
| db.r6g.large · Multi-AZ · Aurora PostgreSQL (size-flexible) | [region] | 4 | 3 | 3 | [X]% | $[X,XXX] |
| db.t4g.medium · Single-AZ · PostgreSQL (**previous generation**) | [region] | 2 | 2 | 2 | [X]% | $[XXX] |

[Rounding each line down to whole reservations leaves [n] unit(s) unallocated
against the [n] achievable total — add them to the line with the highest floor.
— reservations only]

*Floor* is the count that line never dropped below during the lookback, so it is
the part of the recommendation that carries no unused-commitment risk.

_(Omit this table when no line item has anything to buy. A Savings Plan line
shows `any instance family` unless it is an EC2 Instance plan, which is pinned to
one family and region. `(specification not returned)` means AWS gave a count
without the sub-structure that names the instance — treat that line as
unpurchasable until confirmed in the console.)_

Why:

- [rationale lines]

## Eligible spend

| Period | Total unblended spend |
|---|---:|
| [start] → [end] | $[X,XXX] |

Top services, [start] → [end]:

| Service | Spend |
|---|---:|
| [service] | $[X,XXX] |

## Method

[risk-adjustment bands, reconciliation, and posture gate, restated in the
report so the numbers are auditable]

## Collection warnings

| Query | Error | Message |
|---|---|---|
| [query] | [code] | [message] |

_(present only when a query failed — the totals are then a lower bound)_
```

## JSON envelope

Use this shape when the caller asks for machine-readable output instead of (or
alongside) the markdown. It is also what a host tool returns, so the same key
names travel either route.

Top level: `recommendations`, `count`, `total_estimated_monthly_savings`,
`aws_best_case_monthly_savings`, and `reconciliation` (omitted when the caller
had nothing to reconcile, so a sizing-only response does not carry an empty key).

Per item: `commitment_family`, `commitment_type`, `term`, `payment_option`,
`aws_recommended_commitment`, `achievable_commitment`, `commitment_unit`,
`estimated_monthly_savings`, `aws_best_case_monthly_savings`,
`estimated_savings_percentage`, `upfront_cost`, `break_even_months`,
`waste_exposure_monthly`, `confidence`, `spend_profile`,
`implementation_effort`, `rationale`, `line_items`.

`commitment_unit` is `USD/hour` for Savings Plans and `units` for reservations.
Reading an hourly-dollar commitment as a unit count misreads it by roughly
1000x, so carry the unit wherever the number goes.

Per `line_items` entry: `spec`, `region`, `commitment_unit`,
`aws_recommended_commitment`, `achievable_commitment`, `minimum_observed_units`,
`average_observed_units`, `estimated_monthly_savings`, `upfront_cost`,
`monthly_on_demand_cost`, `estimated_utilization_percentage`, `size_flexible`,
`current_generation`, `account_id`.

A line item is the purchasable unit; the item-level figure is not. The
recommendation total sums every specification the service returned, and a
reservation only discounts usage matching its exact `spec`, so quote the total as
a budget and the line items as the order. `estimated_utilization_percentage` is
`null` when AWS did not return it. `size_flexible` means the recommended size is
not binding (the discount follows any size in the family);
`current_generation: false` means a long-term commitment locks the account out of
the cheaper current generation.

The key names deliberately mirror what an AWS Cost Optimization Hub style
recommendation list looks like, so a host that already renders those needs no
translation layer.

### Expiry envelope

Present under `expiry` when the inventory ran, and **absent (not empty) when it
did not** — an empty expiry block reads as "nothing expires", which is a
different and possibly wrong claim.

Top level: `as_of`, `horizon_days`, `regions`, `total_active`, `expiring`,
`expired`, `undated`, `counts` (`urgent` / `soon` / `upcoming`), `actions`
(counts per verdict), `hourly_commitment_expiring`,
`monthly_committed_spend_expiring`, `reserved_units_expiring`, `blind_spots`.

Per commitment: `family` (`savings-plan` | `reservation`), `service`, `label`,
`commitment_id`, `arn`, `instance_type`, `attributes`, `spec`, `quantity`,
`unit`, `region`, `state`, `payment_option`, `start`, `end`, `term_months`,
`days_remaining`, `urgency`, `utilization_pct`, `action`, `rationale`.

`attributes` is a small map of the dimensions a renewal must match — RDS carries
`deployment` (`Multi-AZ` / `Single-AZ`) and `engine`, EC2 carries `scope`, `AZ`,
`platform`, `class` and `tenancy` — and `spec` is those joined onto
`instance_type` for display. A missing key means AWS did not return the field:
absent is not the same as `Single-AZ`, so do not infer one from the other. `spec`
is empty for a Compute Savings Plan by design.

`action` is `renew` | `renew-smaller` | `let-lapse` | `review`; `urgency` is
`urgent` | `soon` | `upcoming` | `expired`. `unit` follows the same rule as
above — `USD/hour` for Savings Plans, `units` for reservations — which is why
`hourly_commitment_expiring` and `reserved_units_expiring` are separate totals
and must never be summed together. `utilization_pct` is `null` when it could not
be measured; `null` means unmeasured, not zero.
