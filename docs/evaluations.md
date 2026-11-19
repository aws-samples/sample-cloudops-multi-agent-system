# AgentCore Evaluations (on-demand)

A repeatable, opt-in suite that scores the CloudOps multi-agent workflow with
[Amazon Bedrock AgentCore Evaluations](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/on-demand-evaluations.html).
It measures *quality* (delegation, tool selection, grounding, task completion),
not just whether a request returned 200.

Nothing here runs during `make test` or `make deploy`. Every real run is paid.

| | Fixture mode | Live mode |
|---|---|---|
| Command | `make eval-fixtures` | `EVAL_LIVE=1 make eval-live` |
| Suite | `evals/suites/fixture.yaml` | `evals/suites/live.yaml` |
| Agents | Real supervisor prompt + model, run in-process; child agents stubbed with canned responses | Deployed supervisor and its real child runtimes |
| Data | Synthetic, defined in the suite (`data_source: fixture`) | Your account, read-only questions (`data_source: live`) |
| Spans | In-memory OTel exporter → ADOT conversion | CloudWatch (`aws/spans` + runtime log group) |
| Needs a deployed stack | No | Yes |
| Repeatable comparisons | Yes (only the model varies) | Indicative only — live data changes |

`make eval-dry-run` validates both suites and prints the plan without any AWS call.

## Layout

```
evals/
  runner.py           CLI: python -m evals.runner --mode fixture|live
  scenarios.py        suite schema + validation
  fixture_harness.py  in-process supervisor with stubbed children
  live_harness.py     AG-UI invocation + CloudWatch span collection
  telemetry.py        span capture, trace-coverage and route checks
  agentcore.py        Evaluate wrapper with explicit failure statuses
  results.py          results.jsonl, summary.md, provenance, redaction
  suites/{fixture,live}.yaml
  results/            run output (gitignored)
```

## Scenarios

Each scenario declares its `mode`, `domain`, `kind`, the `prompt`, the
`expected_route` (child agents the supervisor should delegate to), and
natural-language `assertions`. Fixture scenarios also define the canned
child-agent responses; any delegation to an agent without a fixture returns
an error, so wrong routing is visible.

| Kind | Purpose |
|---|---|
| `success` | Correct delegation and a grounded answer (FinOps, governance, ops troubleshooting, cross-domain) |
| `tool_failure` | Child agent returns an error; the answer must surface it, not invent data |
| `insufficient_evidence` | Child returns empty data; the answer must say so |
| `unsupported_request` | No agent fits; supervisor should decline without delegating |
| `negative_control` | Fixture only. Supervisor prompt deliberately degraded (mis-routing, fabrication) and the platform no-fabrication preamble omitted. Low scores here are the *expected* outcome and show the evaluators discriminate |

Live assertions are qualitative. Never assert live cost totals, counts, or
other values that change.

## Evaluators

Built-in evaluators are resolved at run time with `GetEvaluator`; the level
and availability actually returned are recorded in every run. An evaluator
that does not exist in the region is reported as `EVALUATOR_ERROR`, not
dropped.

| Evaluator | Level | Signal here | Does **not** establish |
|---|---|---|---|
| `Builtin.GoalSuccessRate` | Session | Did the session satisfy the scenario `assertions` | That the data is correct in the real account |
| `Builtin.TrajectoryAnyOrderMatch` | Session | Called the expected child agents (deterministic, no LLM tokens) | Whether the delegation prompt was good |
| `Builtin.ToolSelectionAccuracy` | Tool call | Each delegation went to an appropriate child agent | Anything about leaf-level MCP tool choice (children are separate sessions) |
| `Builtin.Helpfulness` | Trace | Response usefulness to the user | Correctness |
| `Builtin.Correctness` | Trace | Agreement with `expected_response` (fixture scenarios that define one) | Correctness against live data |
| `Builtin.Faithfulness` | Trace | Response grounded in tool output, no invented values | That the tool output itself was right |

Delegation in this system is a Strands tool call (each child agent is a tool
named after it), so tool-level and trajectory evaluators measure routing.

Alongside the LLM-judged scores, every scenario gets a deterministic **route
check** (expected vs. actual child-agent calls from the spans). It is free and
exactly repeatable.

Override per run with `--evaluator` (repeatable) or per scenario with
`evaluators:` in the suite.

## Result statuses

Infrastructure outcomes are never folded into scores:

| Status | Meaning |
|---|---|
| `SCORED` | Evaluator returned a value/label and explanation |
| `NOT_APPLICABLE` | Nothing to score at that level (e.g. tool-call evaluator on a session with no delegation, trajectory evaluator with no expected route) |
| `EVALUATOR_ERROR` | API error (`AccessDenied`, `Validation`, throttling after retries), inline `errorCode` in the result, unknown evaluator, or an empty result |
| `SERVICE_UNAVAILABLE` | Endpoint/service not reachable or internal service error |
| `TELEMETRY_MISSING` | No usable spans, so the evaluator was **not called** |
| `SKIPPED_EXECUTION_ERROR` | The agent invocation itself failed |

Telemetry coverage per scenario:

- `MISSING`: no spans for the session.
- `INCOMPLETE` (blocking): no `invoke_agent` span, no model span, or no
  prompt/response content records. Not evaluated.
- `INCOMPLETE` (non-blocking, live): the supervisor delegated to a child but
  no spans from that child runtime were found in the same trace in
  `aws/spans`. The supervisor session is still scored, but the result is
  flagged and must not be read as full multi-agent coverage.
- `COMPLETE`.

## Output

`evals/results/<run_id>/`:

- `results.jsonl`: one `run` record (schema version, provenance, evaluator
  configuration), one `scenario` record per scenario (mode, telemetry
  verdict, span summary, route check, every evaluator record with value,
  label, explanation, error code/message, token usage, trace/span ids), and a
  `run_end` record. **Restricted artifact**: it holds full session/trace ids
  and raw response excerpts. Do not publish it.
- `summary.md`: readable table plus per-scenario explanations. ARNs, account
  ids, emails, and JWTs are masked and ids are shortened. Review it before
  sharing anyway.

Provenance recorded per run: git commit and dirty flag, suite sha256,
`hierarchy.json` sha256, region, model override, Python and package versions
(`bedrock-agentcore`, `strands-agents`, `boto3`, OTel SDK, ADOT). Fixture
records also include the supervisor model id and system-prompt sha256.

Exit code is non-zero only if the run cannot operate (invalid suite, no
evaluator available, live guard). **There is no quality threshold**; this
first version records a baseline only.

## Prerequisites

### Region and SDK

- AgentCore Evaluations must be available in the region you run in. Check the
  [AgentCore Regions page](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html)
  at run time; built-in evaluators use cross-Region inference in some Regions.
- `bedrock-agentcore >= 1.18.1` (evaluation client + ADOT converter) and
  PyYAML. Both are in the `evals` extra: `.venv/bin/pip install -e ".[evals]"`.
  The project venv from `make setup` already satisfies this.
- AWS credentials for the target account in the shell (`AWS_PROFILE` /
  `AWS_REGION` or `.env`). The runner never writes credentials anywhere.

### IAM for the caller

- `bedrock-agentcore:Evaluate`, `bedrock-agentcore:GetEvaluator`
- `bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream`, `bedrock:Converse`, `bedrock:ConverseStream`
  (fixture mode runs the supervisor model locally; evaluators also need model access)
- Live mode, additionally: `logs:StartQuery`, `logs:GetQueryResults` on
  `aws/spans` and `/aws/bedrock-agentcore/runtimes/*`, and
  `cognito-idp:InitiateAuth` (via the app client) for the AG-UI call.

See [evaluation prerequisites](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/evaluations-prerequisites.html).

### Telemetry (live mode only)

- CloudWatch **Transaction Search** enabled so spans land in `aws/spans`.
- Sampling/indexing: at the AWS defaults (5% × 1%) most sessions have **no**
  indexed spans and live runs report `TELEMETRY_MISSING`. These are
  account-wide settings and this suite **does not change them**. Get approval
  before applying the recommendations in
  [observability-tuning.md](observability-tuning.md); prefer the
  service-scoped sampling rule over changing `Default`.
- Span ingestion takes minutes. The runner polls up to `--max-wait` seconds
  (default 600).
- The runtimes ship `aws-opentelemetry-distro==0.17.0`. AWS documents
  0.18.0+ for the unified telemetry destination; if spans or content records
  are missing, check this first (not changed by this suite).

## Costs

- Fixture mode: supervisor model calls on Bedrock (the `hierarchy.json`
  supervisor model unless `--model` is given) plus one `Evaluate` call per
  evaluator per scenario (more for trace/tool batches). Built-in evaluators
  are billed per input/output token; trajectory evaluators use no tokens.
  See [AgentCore pricing](https://aws.amazon.com/bedrock/agentcore/pricing/).
- Live mode: the above, plus normal AgentCore Runtime, Gateway, Lambda tool,
  Memory, and model usage for every agent in the delegation chain, plus
  CloudWatch Logs Insights queries.
- Reduce cost while iterating with `EVAL_ARGS="--scenario <id>"` and/or a
  cheaper `--model` (fixture mode).

## Effects of a live run and cleanup

Live scenarios only ask read-only questions; no prompt asks an agent to
create, change, or delete anything. A live run still behaves like user
traffic:

- conversation turns are written to AgentCore Memory under actor
  `agentcore-evals` (override with `EVAL_ACTOR_ID`) and session ids prefixed
  `eval-<scenario-id>-`;
- telemetry is emitted to CloudWatch/X-Ray;
- read-only AWS API calls are made by the tools (Cost Explorer, CloudWatch,
  tagging, etc.).

Cleanup: delete `evals/results/<run_id>/` locally when no longer needed;
eval sessions in Memory follow the memory resource's normal expiry. The
suite creates no evaluators, configurations, or other AWS resources.

## Interpreting results

- Compare runs on the same suite sha256 and `hierarchy.json` sha256; a
  changed prompt or model is a different experiment.
- Fixture scores move only with the model and prompts, so they are the
  right signal for prompt/routing changes. Live scores are indicative.
- LLM-judge scores vary between identical runs. Look at explanations and
  trends across several runs, not single-point differences.
- Negative controls should score clearly lower than their `success`
  counterparts. If they do not, the evaluator is not discriminating for that
  behaviour and its score should not be relied on.
- A route-check FAIL with a high `ToolSelectionAccuracy` (or vice versa) is
  worth reading: the deterministic check is ground truth for *which* agents
  were called; the evaluator judges whether each call was reasonable.

- Session/tool-level evaluators and trace-level evaluators answer different
  questions. In the mis-routing control the supervisor called the wrong
  agent (route FAIL; GoalSuccessRate, Trajectory, ToolSelectionAccuracy all
  0.00), yet Helpfulness, Correctness and Faithfulness scored 1.00 because
  the final answer honestly reported the irrelevant data it received.
  Trace-level scores judge the answer *given its evidence*, not whether the
  evidence came from the right place. Read them together.
- Expect judge variance: across two otherwise identical baseline runs,
  Helpfulness for `fx-unsupported-request` moved 0.50 → 1.00.

### Baseline (fixture, 2026-11-19)

Supervisor model `global.anthropic.claude-opus-4-6-v1` (from
`hierarchy.json`), region us-east-1, `bedrock-agentcore` 1.18.1,
`strands-agents` 1.40.0. Evaluator usage for the full suite: ~49K input /
~8K output tokens (≈ $0.21 at built-in evaluator pricing), plus supervisor
model calls.

| Scenario | Kind | Route | GoalSuccess | Trajectory | ToolSelection | Helpfulness | Correctness | Faithfulness |
|---|---|---|---|---|---|---|---|---|
| `fx-finops-monthly-spend` | success | pass | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| `fx-governance-tag-compliance` | success | pass | 1.00 | 1.00 | 1.00 | 0.83 | 1.00 | 1.00 |
| `fx-ops-alarm-triage` | success | pass | 1.00 | 1.00 | 1.00 | 0.83 | 1.00 | 1.00 |
| `fx-multi-domain` | success | pass | 1.00 | 1.00 | 1.00 (n=2) | 1.00 | 1.00 | 1.00 |
| `fx-finops-tool-failure` | tool_failure | pass | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| `fx-ops-insufficient-evidence` | insufficient_evidence | pass | 1.00 | 1.00 | 1.00 | 0.83 | 1.00 | 1.00 |
| `fx-unsupported-request` | unsupported_request | pass | 1.00 | n/a | n/a | 1.00 | 1.00 | 1.00 |
| `fx-negctl-misroute` | negative_control | **FAIL** | **0.00** | **0.00** | **0.00** | 1.00 | 1.00 | 1.00 |
| `fx-negctl-fabrication` | negative_control | pass | **0.00** | 1.00 | 1.00 | **0.00** | **0.00** | **0.00** |

Reading it: every non-control scenario, including tool failure, empty
evidence, and the unsupported request, is handled well. Both negative
controls separate cleanly from their success counterparts on the
evaluators that target the degraded behaviour (routing for mis-routing;
grounding/correctness for fabrication), which is evidence the evaluators
discriminate. The first version of the mis-routing control used a soft
instruction that the supervisor model ignored, and scored like a success;
it now forbids the correct agent explicitly.

Live baseline: not yet recorded.

## Access control (pending #29)

Permitted/denied access scenarios will be added with the enterprise
authorization example (#29). Enforcement will be proven by deterministic
allow/deny assertions; evaluator scores (e.g. refusal quality) are supporting
evidence only, never a security assertion.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `TELEMETRY_MISSING` in live mode | Sampling/indexing defaults, Transaction Search off, or ingestion slower than `--max-wait` |
| Non-blocking `INCOMPLETE`: child runtime not in trace | Trace context not propagated across `InvokeAgentRuntime`, or child spans not sampled |
| `EVALUATOR_ERROR` `ResourceNotFoundException` | Evaluator id not offered in this region |
| `EVALUATOR_ERROR` `AccessDeniedException` | Caller lacks `bedrock-agentcore:Evaluate` / model access |
| `SpanEventParsingException`, `AgentSpanMappingException`, `LogEventMissingException` | Span/content shape not recognised; check ADOT version and that content records are present |
| Fixture mode: "global OpenTelemetry tracer provider was already installed" | Don't run the runner under `opentelemetry-instrument` |
