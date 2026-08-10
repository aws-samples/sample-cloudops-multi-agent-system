# CloudWatch alarms — recommend, tune, and emit CloudFormation

End-to-end reference for the `cloudwatch-agent`. Lives under
`ops-excellence-agent` (peer of `health-events-agent` and
`network-resiliency-agent`). Recommends AWS best-practice CloudWatch
alarms for a workload and tunes existing alarm thresholds against real
metric history — always emitting CloudFormation the user reviews and
applies themselves.

> **Read-only by design.** This agent reads metrics and alarms and
> hands back CloudFormation. It NEVER calls `PutMetricAlarm`,
> `PutAnomalyDetector`, or any state-changing API. Its IAM grants only
> `cloudwatch:Describe*`, `cloudwatch:Get*`, and `cloudwatch:List*` — it
> physically cannot create, modify, or delete an alarm. You apply every
> change.

---

## 1. What the agent does

Two read-only capabilities, both producing CloudFormation:

1. **Recommend** — "what alarms should I have for this workload?" The
   agent resolves your tag selector (or explicit ARN list) to resources,
   looks up AWS-recommended alarms per resource from a vendored metric
   catalogue, and emits a `monitoring.yaml` of `AWS::CloudWatch::Alarm`
   resources wired to your SNS topic.
2. **Tune** — "my alarms are noisy / flapping / not firing." The agent
   pulls your alarms currently in ALARM state, reads their state-change
   history to spot flapping, analyses the backing metric over a 14-day
   window (mean / median / p99 / std-dev / trend / seasonality / noise
   score), and returns a markdown table of proposed config changes plus
   a CFN snippet of the modified alarms.

Representative prompts:

- `"Recommend CloudWatch alarms for tag App=payments in us-east-1, SNS arn:aws:sns:us-east-1:123456789012:ops-alarms"`
- `"My alarms for App=payments are flapping — tune them."`
- `"What's a good threshold for the p99 latency on my payments API?"`

Behavior — recommend path:

```
"Recommend alarms for tag App=payments, SNS arn:...:ops-alarms"
  → supervisor → ops-excellence-agent → cloudwatch-agent
    → find_resources_by_tag(tag_filters={App: payments})   (tag-governance)
    → get_recommended_metric_alarms(resource_arn=...)  per resource
    → assemble_cfn_template(alarms=[...all agreed alarms...], sns_topic_arn, tags)  ONCE
  → response: summary + selector echo + SNS ARN + alarm list + a clickable
              ReportCard. Click the card to open the artifact panel and view
              the full CloudFormation template.
```

Behavior — tune path:

```
"My alarms for App=payments are flapping"
  → cloudwatch-agent
    → find_resources_by_tag(tag_filters={App: payments})
    → get_active_alarms(max_items=50)            (alarms in ALARM state)
    → get_alarm_history(alarm_name=...)  per suspicious alarm
    → analyse_metric(namespace, metric_name, dimensions, statistic)  per backing metric
    → assemble_cfn_template(alarms=[...modified alarms...], sns_topic_arn)  ONCE
  → response: markdown diff table (current → proposed) + a clickable
              ReportCard. Click the card to view the modified-alarm template.
```

Every alarm the agent emits is tagged `ManagedBy=cloudwatch-agent`. When
you supply a tag selector, the agent also stamps a `Workload` tag (a
short fingerprint of your selector) and a `GeneratedAt` ISO timestamp so
you can trace which run produced a given alarm.

The `cloudwatch` Lambda is responsive — the metric catalogue and the
numpy/pandas analysis stack load once at cold start (~1s), and warm
invocations return in milliseconds.

### What it does NOT do (v1)

- **Does not apply anything.** No `PutMetricAlarm`. You run
  `aws cloudformation deploy` yourself (§6).
- **Does not scan everything.** It always requires a selector — a
  `tag_filters` map or an explicit ARN list. Ask it to "recommend
  alarms" with no scope and it asks you for one.
- **Does not invent an SNS topic.** It requires an SNS topic ARN before
  emitting any CFN and never defaults to an arbitrary one.
- **No Logs Insights tools, no manifest diffing across runs, no
  Terraform/CDK output (CFN only), no composite alarms, no
  per-call multi-account fan-out.** See §8 for the cross-account story.

---

## 2. When to use this agent vs. other ops-excellence children

| You're asking about… | Agent |
|---|---|
| What alarms should I have? Tune noisy/flapping alarms. Good threshold for a metric. | **`cloudwatch-agent`** (this one) |
| AWS Health events, outages, scheduled maintenance, incident triage | `health-events-agent` |
| Direct Connect topology, resiliency tiers, DX pricing | `network-resiliency-agent` |
| Tag compliance, required-tag policy, cost-allocation-tag health | `tag-governance-agent` |
| Actual AWS spend, cost breakdowns, savings recommendations | `cost-operations-agent` |

Trigger words that route here: `alarm`, `alarms`, `cloudwatch`,
`monitoring setup`, `alert`, `alerting`, `threshold tuning`,
`noisy alarm`, `flapping`, `false positive`, `tune alarm`.

The agent shares the `tag-governance` Lambda's `find_resources_by_tag`
tool for resource discovery, but it does NOT do tag-compliance
analysis — that's `tag-governance-agent`. It only uses
`find_resources_by_tag` to turn a tag selector into the ARN list it
fans alarm recommendations across.

---

## 3. Tool surface

The agent sees nine tools — eight from the `cloudwatch` Lambda plus one
borrowed from `tag-governance`.

| Tool | Lambda | Purpose | AWS call? |
|---|---|---|---|
| `find_resources_by_tag` | tag-governance | Resolve a `{key: value}` tag selector to a list of resource ARNs (`resourcegroupstaggingapi:GetResources`). Caps at 1000 results. | `tag:GetResources` |
| `get_recommended_metric_alarms` | cloudwatch | AWS-recommended alarms for a `(namespace, metric_name)` pair, or pass a `resource_arn` to derive the namespace + dimensions. From the vendored catalogue. | No (local lookup) |
| `get_metric_metadata` | cloudwatch | Metric description, recommended statistic, and unit for a metric. From the catalogue. | No (local lookup) |
| `analyse_metric` | cloudwatch | 14-day stats (mean, median, p50/p90/p99, std-dev, coefficient of variation, trend, seasonality period, noise score, data density). numpy/pandas. | `cloudwatch:GetMetricData` |
| `get_active_alarms` | cloudwatch | Alarms currently in `ALARM` state (metric + composite). `max_items` default 50. | `cloudwatch:DescribeAlarms` |
| `get_alarm_history` | cloudwatch | Alarm state-transition history — spot flapping. Defaults to 24h. | `cloudwatch:DescribeAlarmHistory` |
| `get_metric_data` | cloudwatch | Raw metric datapoints. Supports percentiles (`p50`…`p99.9`, `tm99`) and metric-math via a `queries` array. Defaults to 3h lookback. | `cloudwatch:GetMetricData` |
| `build_cfn_alarm` | cloudwatch | Pure dict assembler — produce ONE `AWS::CloudWatch::Alarm` resource. Used internally by `assemble_cfn_template`; rarely called directly. | No (pure Python) |
| `assemble_cfn_template` | cloudwatch | Pure Python template assembler — produce the COMPLETE CFN template (all alarms) as a deterministic YAML string. Single source of the emitted YAML. | No (pure Python) |

The agent picks tools from your intent — there's no rigid phase-by-phase
workflow encoded in its prompt.

---

## 4. Two example interactions (truncated)

### 4.1 Recommend

> **You:** Recommend CloudWatch alarms for tag App=payments in
> us-east-1, SNS arn:aws:sns:us-east-1:123456789012:ops-alarms

> **Agent:**
> **Summary:** 4 best-practice alarms across 1 Lambda function, 1 RDS
> instance, and 1 ALB, all wired to your `ops-alarms` topic.
> **Resource selector:** `App=payments` (us-east-1) — 3 resources
> **SNS topic:** `arn:aws:sns:us-east-1:123456789012:ops-alarms`
> **Alarms generated:** 4
>
> - `Errors-3f2a9c1b` — AWS/Lambda Errors ≥ 1 over 5×1min (payments-api)
> - `Duration-3f2a9c1b` — AWS/Lambda Duration p99 > 3000ms over 5×1min (payments-api)
> - `CPUUtilization-7d4e0a55` — AWS/RDS CPUUtilization > 80% over 3×5min (payments-db)
> - `HTTPCode_Target_5XX-9b1c224e` — AWS/ApplicationELB 5XX > 10 over 5×1min (payments-alb)
>
> *(Click the **CloudFormation Template** card below to open the full
> template in the artifact panel. Copy the YAML to `monitoring.yaml` and
> apply with `aws cloudformation deploy`.)*

The full worked example with the complete YAML is in §7.

### 4.2 Tune

> **You:** My alarms for App=payments are flapping — tune them.

> **Agent:**
> **Summary:** 2 of 5 active alarms are flapping. Both back metrics with
> a clear daily cycle that the current static thresholds don't account
> for. Proposed changes below; the modified-alarm template is in the
> artifact panel.
>
> | Alarm | Finding | Current | Proposed |
> |---|---|---|---|
> | `Duration-3f2a9c1b` | 14 transitions in 24h; p99=2,950ms, noise score 0.42 | Threshold 1500ms, 1×1min | Threshold 3200ms, 5×1min, datapoints 3 |
> | `CPUUtilization-7d4e0a55` | Daily seasonality (~86,400s); fires every peak | Static 70%, 1×5min | Switch to anomaly-detection band (2σ) |
>
> *(The CFN for the 2 modified alarms is in the **CloudFormation Template**
> card below — click to open the artifact panel. Apply only the changes
> you agree with — re-deploy the same stack to update the existing alarms.)*

The agent surfaces `analyse_metric`'s `insufficient_history` note
verbatim and skips any alarm whose backing metric doesn't have enough
datapoints to analyse.

---

## 5. IAM requirements

The `cloudwatch` Lambda's execution role grants exactly three read-only
action families (declared in `tools.json`, rendered by the
`lambda-tool-base` Terraform module):

```
cloudwatch:Describe*
cloudwatch:Get*
cloudwatch:List*
```

No `cloudwatch:Put*`, `cloudwatch:Set*`, `cloudwatch:Delete*`, no
`logs:*`, no write actions of any kind. This is enforced in CI by
`tests/unit/test_topology.py::TestToolsJson::test_no_write_iam_actions_on_read_only_tools`.

Resource discovery (`find_resources_by_tag`) runs on the
`tag-governance` Lambda, which already holds `tag:GetResources`. The
`cloudwatch-agent` adds no new write actions to either Lambda.

When `CROSS_ACCOUNT_ROLE_ARN_CLOUDWATCH` is set, `lambda-tool-base`
auto-grants `sts:AssumeRole` scoped to that single ARN — no manual IAM
edits. The spoke-account role you assume into should itself be
read-only (the same three `cloudwatch:*` families plus, if you want
cross-account tag discovery, `tag:GetResources`).

---

## 6. Cross-account setup

v1 targets **one spoke account per deploy** via
`CROSS_ACCOUNT_ROLE_ARN_CLOUDWATCH` — the same static-aliased-role
pattern `cost-optimization-hub` uses. When the env var is unset, the
Lambda transparently falls back to its own execution role
(single-account mode).

### Configure it via `make configure`

The cross-account ARN is shared project state, so it lives in SSM —
not your personal `.env`. Set it through the interactive config flow:

```bash
make configure            # first-run config
# or, to change it later:
make reconfigure-shared
```

When `cloudwatch` is in your `SELECTED_TOOLS`, the flow prompts for
`CROSS_ACCOUNT_ROLE_ARN_CLOUDWATCH`. The value is written to
`terraform/config.auto.tfvars.json` and mirrored to SSM Parameter Store
at:

```
/<project>/<env>/config/cross_account/cloudwatch_role_arn
```

At deploy time the `$VAR` reference in the `cloudwatch` entry of
`tools.json` resolves to this value and is injected as the Lambda's
`CROSS_ACCOUNT_ROLE_ARN_CLOUDWATCH` env var. The Lambda's boto3 calls
flow through `shared.cross_account.get_aws_client(role_alias="CLOUDWATCH")`,
which assumes the role when set and uses the execution role when not.

### One-off override

To point at a different spoke for a single deploy without touching the
team's shared config, set the var in your `.env` (it's documented in
`.env.example`):

```bash
# Optional — overrides the SSM-stored value for your deploys only.
CROSS_ACCOUNT_ROLE_ARN_CLOUDWATCH=arn:aws:iam::111111111111:role/cloudops-cloudwatch-readonly
```

> Resource discovery for cross-account work uses the `tag-governance`
> Lambda's own `CROSS_ACCOUNT_ROLE_ARN_TAG_GOVERNANCE` — point both at
> the same spoke account when you want `find_resources_by_tag` and the
> cloudwatch tools to operate on the same account's resources.

---

## 7. How to apply the generated CloudFormation

The agent never applies anything — it hands you a template via a
clickable **CloudFormation Template** card in the chat. The card opens
the artifact panel which displays the full template, deterministically
assembled by the `assemble_cfn_template` tool. The template is always
complete (every alarm in your scope, no excerpt or abbreviation),
regardless of size. Applying it keeps you in control of exactly what
changes in your account.

1. **Click the CloudFormation Template card** in the chat. The artifact
   panel opens on the right with the full template. Copy the YAML.
2. **Save the template.** Paste it into a file, e.g. `monitoring.yaml`.
3. **Review it.** Confirm the `AlarmActions` SNS ARN, thresholds,
   dimensions, and tags are what you want. Adjust thresholds to your
   workload before deploying (see the FAQ on threshold values).
4. **Deploy from your terminal:**

   ```bash
   aws cloudformation deploy \
     --template-file monitoring.yaml \
     --stack-name payments-alarms \
     --capabilities CAPABILITY_NAMED_IAM   # only if your template needs it
   ```

   `aws cloudformation deploy` creates the stack on first run and updates
   it in place on subsequent runs. The alarm template here is plain
   `AWS::CloudWatch::Alarm` resources, so `--capabilities` is usually
   unnecessary — include it only if you've added IAM resources.
5. **Evolve the alarm set over time.** Re-run the agent later, take the
   new template output, and `aws cloudformation deploy` against the
   *same* `--stack-name`. CloudFormation diffs and applies only what
   changed. Alarm names are stable (derived from a hash of the metric
   dimensions), so the same resource maps to the same alarm across
   re-runs — re-deploys are idempotent.

### Fully worked example

> **You:** Recommend CloudWatch alarms for tag App=payments in
> us-east-1, SNS arn:aws:sns:us-east-1:123456789012:ops-alarms

> **Agent (chat):**
>
> **Summary:** 4 best-practice alarms across 1 Lambda function, 1 RDS
> instance, and 1 ALB, wired to your `ops-alarms` topic and tagged
> `ManagedBy=cloudwatch-agent`.
> **Resource selector:** `App=payments` (us-east-1) — 3 resources
> **SNS topic:** `arn:aws:sns:us-east-1:123456789012:ops-alarms`
> **Alarms generated:** 4
>
> - `Errors-3f2a9c1b` — AWS/Lambda Errors ≥ 1 over 5×1min (payments-api)
> - `Duration-3f2a9c1b` — AWS/Lambda Duration p99 > 3000ms over 5×1min (payments-api)
> - `CPUUtilization-7d4e0a55` — AWS/RDS CPUUtilization > 80% over 3×5min (payments-db)
> - `HTTPCode_Target_5XX-9b1c224e` — AWS/ApplicationELB HTTPCode_Target_5XX_Count > 10 over 5×1min (payments-alb)
>
> The full CloudFormation template appears as a clickable card in the
> chat — click it to open the artifact panel and copy the YAML. Save it
> as `monitoring.yaml` and deploy with
> `aws cloudformation deploy --template-file monitoring.yaml --stack-name payments-alarms`.

> **Agent (artifact panel — full template, deterministically assembled):**
>
> ```yaml
> AWSTemplateFormatVersion: '2010-09-09'
> Description: CloudWatch alarms generated by cloudwatch-agent. Review thresholds before deploying.
> Resources:
>   Errors3f2a9c1b:
>     Type: AWS::CloudWatch::Alarm
>     Properties:
>       AlarmName: Errors-3f2a9c1b
>       AlarmDescription: Lambda function errors indicate failed invocations.
>       Namespace: AWS/Lambda
>       MetricName: Errors
>       Dimensions:
>         - Name: FunctionName
>           Value: payments-api
>       Statistic: Sum
>       ComparisonOperator: GreaterThanOrEqualToThreshold
>       Threshold: 1
>       EvaluationPeriods: 5
>       DatapointsToAlarm: 5
>       Period: 60
>       TreatMissingData: notBreaching
>       AlarmActions:
>         - arn:aws:sns:us-east-1:123456789012:ops-alarms
>       Tags:
>         - Key: ManagedBy
>           Value: cloudwatch-agent
>         - Key: Workload
>           Value: App-payments
>         - Key: GeneratedAt
>           Value: '2026-05-12T14:03:11Z'
>   CPUUtilization7d4e0a55:
>     Type: AWS::CloudWatch::Alarm
>     Properties:
>       AlarmName: CPUUtilization-7d4e0a55
>       AlarmDescription: High CPU on the payments RDS instance.
>       Namespace: AWS/RDS
>       MetricName: CPUUtilization
>       Dimensions:
>         - Name: DBInstanceIdentifier
>           Value: payments-db
>       Statistic: Average
>       ComparisonOperator: GreaterThanThreshold
>       Threshold: 80
>       EvaluationPeriods: 3
>       DatapointsToAlarm: 3
>       Period: 300
>       TreatMissingData: missing
>       AlarmActions:
>         - arn:aws:sns:us-east-1:123456789012:ops-alarms
>       Tags:
>         - Key: ManagedBy
>           Value: cloudwatch-agent
>         - Key: Workload
>           Value: App-payments
>         - Key: GeneratedAt
>           Value: '2026-05-12T14:03:11Z'
>   # Duration3f2a9c1b and HTTPCodeTarget5XX9b1c224e shown here for brevity in this
>   # documentation snippet — the actual artifact body contains every alarm in full.
> ```

> The artifact body is the COMPLETE template, regardless of size. The
> `assemble_cfn_template` tool builds the YAML deterministically in
> Python, so the agent never abbreviates it — what you see in the panel
> is exactly what you deploy.

---

## 8. FAQ

**Why doesn't this auto-apply the alarms?**
By design, and enforced by IAM. The platform recommends; it never
remediates. The `cloudwatch` Lambda's role has only `cloudwatch:Describe*`,
`Get*`, and `List*` — no `PutMetricAlarm`, so the agent physically
cannot create or change an alarm even if asked. You review the
CloudFormation and run `aws cloudformation deploy` yourself, which keeps
you in control of exactly what changes in your account and means you own
the stack (and its teardown).

**How is the threshold value calculated when the catalogue says "set to
a percentage"?**
The AWS-recommended catalogue gives you the comparison operator,
statistic, period, and evaluation-period counts — plus a threshold
*justification* in prose (often "set to ~90% of provisioned capacity" or
similar). It does not always carry a hard numeric value. The agent picks
a sensible number: for utilization-style metrics it uses the percentage
from the justification; for others it can run `analyse_metric` and use
the observed **p99** as a data-driven starting point. Treat the emitted
threshold as a starting point — review and adjust it to your workload
before you deploy.

**What if my service isn't in the catalogue?**
The vendored catalogue covers ~1,179 metrics across the common AWS
services. On a catalogue miss, `get_recommended_metric_alarms` returns
an empty recommendation list with a note rather than an error. The agent
tells you it has no built-in recommendation for that metric, and can
still help: it can build a custom alarm for you via `build_cfn_alarm`
(you supply the threshold), or run `analyse_metric` on the metric's
history to suggest a sensible threshold. The catalogue is a verbatim
snapshot of `awslabs.cloudwatch-mcp-server`'s `metric_metadata.json`
(Apache 2.0 — see `src/lambda/mcp/cloudwatch/THIRD_PARTY_NOTICES.md`);
updating it as AWS adds metrics is a data-file swap, no code change.

**Does this support cross-account?**
Yes — one spoke account per deploy, via
`CROSS_ACCOUNT_ROLE_ARN_CLOUDWATCH` (the standard platform
cross-account pattern, the same one `cost-optimization-hub` uses). See
§6 for setup. Organization-wide multi-account fan-out (discovering spoke
accounts at runtime and parallelizing across them) is a documented
future enhancement, not part of v1.

---

## 9. Known gotchas

| Symptom | Likely cause | Fix |
|---|---|---|
| Agent asks for an SNS topic ARN before doing anything | Safety rule — it never defaults to an arbitrary topic | Supply an SNS ARN, e.g. `arn:aws:sns:us-east-1:123456789012:ops-alarms` |
| Agent asks for a tag selector or ARN list | Safety rule — it never scans the whole account | Pass `tag_filters` (e.g. `App=payments`) or an explicit ARN list |
| `find_resources_by_tag` returns `truncated: true` | Selector matched more than 1000 resources | Narrow with more `tag_filters` pairs or a `resource_types` filter |
| `find_resources_by_tag` returns `selector_required` | Empty `tag_filters` | Provide at least one tag key/value pair |
| `analyse_metric` returns `insufficient_history` | Fewer than ~50 datapoints in the window | Use a longer lookback, or confirm the metric is actually being published |
| `get_recommended_metric_alarms` returns `no_recommendations_in_catalogue` | Metric not in the vendored catalogue | Build a custom alarm with `build_cfn_alarm`, or analyse the metric for a threshold (see FAQ) |
| `build_cfn_alarm` returns `invalid_sns_topic_arn` | SNS ARN doesn't match `arn:aws:sns:<region>:<12-digit-account>:<topic>` | Pass a well-formed SNS topic ARN |
| Cross-account calls fail with `AccessDenied` | `CROSS_ACCOUNT_ROLE_ARN_CLOUDWATCH` set but the role isn't assumable | Verify the ARN and the spoke role's trust policy; confirm the spoke role is read-only |
| Recommendations span only one account | Single-account mode (env var unset) | Set `CROSS_ACCOUNT_ROLE_ARN_CLOUDWATCH` for the spoke (§6); point `tag-governance`'s var at the same account |
| Anomaly-detection alarm in the output instead of a static threshold | The metric's recommended `comparisonOperator` is an anomaly variant | Expected — `build_cfn_alarm` emits the `Metrics` + `ThresholdMetricId` shape for these |
