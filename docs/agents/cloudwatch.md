# CloudWatch alarm coverage

The `cloudwatch-agent` is read-only. It inventories CloudWatch alarms, compares
resource coverage with the vendored recommendation catalogue, analyzes live
alarm behavior, and emits CloudFormation for a user to review.

## Snapshot model

EventBridge starts a collection run every six hours. A coordinator discovers
the configured account's enabled Regions and sends an inventory job to SQS.
Workers use Resource Explorer's aggregator view when available, falling back to
the Resource Groups Tagging API with explicit incomplete-inventory status.

Each Region inventories all alarms, resolves resource tags and dimensions,
computes coverage, and writes immutable rows to DynamoDB. A run publishes the
`ACCOUNT#<id> / CURRENT` pointer only after every expected Region has completed.
A failed or partial run never replaces the previous successful snapshot.

Application freshness is independent of DynamoDB TTL:

- Fresh through 8 hours.
- Stale but servable through 48 hours; reads start an asynchronous refresh.
- Unusable after 48 hours.

## Coverage states

Recommendations have deterministic IDs and a catalogue version. Matching uses
resource type, the complete metric or metric-math signature, and exact resolved
dimensions. Namespace and metric name alone never establish coverage.

The possible states are `implemented`, `implemented_with_drift`, `missing`,
`unresolved_dimensions`, `unsupported_resource`, and `inventory_incomplete`.
Missing is authoritative only when both resource and alarm inventories are
complete.

## Tools

- `query_alarm_inventory`: snapshot-backed alarm configuration inventory.
- `analyze_alarm_coverage`: account, tags, or explicit-resources coverage.
- `get_alarm_snapshot_status`: current snapshot age, completeness, and run state.
- `prepare_alarm_deployment`: live-revalidates selected candidate IDs, batches
  threshold calibration, and emits one typed CloudFormation artifact.
- `analyze_alarm_tuning`: live alarm configuration, history, and metric baseline.

The existing metric data, metadata, recommendation, active alarm, history,
posture, analysis, and CFN assembly tools remain available.

Tag filters AND keys and OR values within one key. Responses default to 50 rows
and cap at 200, with cursors bound to both snapshot ID and query hash.
`force_refresh=true` starts or reuses an asynchronous run; it does not perform a
synchronous account scan. Explicit-resource mode can use a live fallback for at
most 100 ARNs.

## Deployment behavior

CloudFormation generation requires a snapshot ID, candidate IDs, and an SNS
topic ARN. Selected candidates are revalidated against live alarms before
generation. Already implemented candidates are excluded. Candidates without a
fixed catalogue threshold are calibrated from batched metric history; if data
is insufficient, the caller must provide an explicit threshold override.

The agent never calls `PutMetricAlarm` and never invents a numeric threshold.
The typed artifact transport keeps the full template out of model context and
chat memory.
