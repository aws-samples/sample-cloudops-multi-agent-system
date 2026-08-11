# Tag governance — architecture, deploy modes, operations

End-to-end reference for the `tag-governance-agent` feature. Read the
**Deploy modes** section first to pick your architecture; the rest is
reference material.

> **Read-only by design.** This feature inspects AWS tagging state and
> surfaces gaps as recommendations. It NEVER writes tags, NEVER
> activates cost-allocation tags, and NEVER modifies organization
> policies. Every finding is advisory — an operator applies changes.

---

## 1. What the feature does

Answers tag-compliance questions from a **scheduled snapshot** when one is
available, falling back to live AWS API queries otherwise.

By default (`enable_tag_snapshots = true`), a collector Lambda sweeps the
expensive scans on a schedule (`rate(6 hours)`) and stores the responses in
DynamoDB. Canonical (unfiltered) queries then answer in milliseconds and carry
`data_as_of` + `data_source: "scheduled_snapshot"`. Three things always go
live: queries with caller filters (`resource_types`, `regions`,
`required_tags` override, `use_aws_evaluation`…), `force_refresh=true`, and
any cache miss (collector disabled, snapshot expired, table unreachable —
the fallback is silent and the tool behaves exactly as it did pre-collector).

The collector does not reimplement scan logic: it invokes the SAME tool
Lambda with gateway-shaped requests and stores what comes back, so the live
and snapshot paths cannot drift. Snapshot staleness is bounded by the item
TTL (default 48h): if the collector stops running, snapshots expire and the
tool reverts to live queries rather than serving stale data forever.

Representative prompts:

- `"What tag keys are required by our org policy?"`
- `"Check tag compliance against Environment, Owner, Project."`
- `"What's our org-wide tag compliance posture?"`
- `"Which of our required tags aren't activated in Cost Explorer?"`
- `"Find resources with no tags at all."`
- `"How do we fix the missing tags in bulk?"`

Behavior:

```
"Check tag compliance against Environment, Owner, Project."
  → supervisor → governance-agent → tag-governance-agent
    → check_tag_compliance(required_tags=["Environment","Owner","Project"])
      → resource-explorer-2:Search    (inventory)
      → in-Python classifier          (per-resource compliance)
    → response: compliance_pct, breakdowns, non_compliant_resources,
                remediation_buckets
  → (follow-up) get_remediation_guidance(remediation_buckets, ...)
    → response: console deep-links pre-filtered per violation bucket
```

Two valid sources for the required-tag policy:

1. **Caller-supplied** — the user names tags in the prompt
   (`"required_tags": ["Environment", "Owner"]`).
2. **AWS Organizations Tag Policy** — when no caller input is given,
   `_resolve_policy()` calls
   `organizations:DescribeEffectivePolicy(PolicyType="TAG_POLICY")`.

If neither resolves, every tool that needs a policy returns
`error: "No required-tag policy found"` with a starter-tag hint. The
handler **never** assumes default tag keys.

---

## 2. Deploy modes — pick one

Two independent decisions:

```
Decision 1 — which account runs the cloudops Lambda?
├── Management (payer) account     → org-wide APIs work natively
└── Member / dedicated ops account  → org-wide APIs fail with payer-only errors

Decision 2 — is Resource Explorer multi-account search enabled?
├── Yes → check_tag_compliance + find_untagged_resources see the whole org
└── No  → those tools see only the Lambda's own account
```

### Mode A — mgmt account + multi-account RE (full feature)

cloudops runs in the payer account; Resource Explorer multi-account
search is enabled there.

- **What works:** every tool. Org-wide rollups, per-resource detail
  across the org, cost-allocation activation health, remediation
  deep-links.
- **Setup:** run cloudops in the payer; enable Resource Explorer
  multi-account search (see §2.1).

### Mode B — mgmt account + single-account RE

cloudops runs in the payer but Resource Explorer is only indexed
locally.

- **What works:** `get_org_tag_compliance_summary` and
  `list_cost_allocation_tag_status` return org-wide aggregates.
  `check_tag_compliance` and `find_untagged_resources` see only the
  payer account.
- **Setup:** run cloudops in the payer; enable a Resource Explorer
  index in the Lambda's region but skip the aggregator step in §2.1.

### Mode C — member / ops account (partial data)

cloudops runs in any account that is NOT the payer.

- **What works:** `check_tag_compliance`, `find_untagged_resources`,
  `get_required_tags` (per the Resource Explorer config).
- **What doesn't:** `get_org_tag_compliance_summary` returns
  `AccessDenied` (mgmt-only API). `list_cost_allocation_tag_status`
  returns the payer-only error. Cross-account assume-role is **not**
  supported by this handler — for org-wide rollups, run from the payer.

> **`CROSS_ACCOUNT_ROLE_ARN_TAG_GOVERNANCE` is unused.** Wired through
> `tools.json` and the shared-config Terraform module for compatibility
> with the previous handler design. The current handler does NOT consume
> it. Will be removed in a future MR.

### 2.1 Prerequisites — Resource Explorer multi-account search

`check_tag_compliance` (default mode) and `find_untagged_resources`
need an **aggregator index** in the Lambda's account to work at all,
plus a **multi-account view** for org-wide reach. Without these,
`check_tag_compliance` returns `status: 'resource_explorer_not_indexed'`.

**Recommended: AWS Quick Setup.** From the management account:

> AWS console → Systems Manager → Quick Setup → "Resource Explorer" →
> Choose your aggregator-index region → Targets: Entire organization
> → Create.

This provisions trusted access, per-account indexes, the aggregator
index, and an org-scoped view in one StackSet.

**Manual setup via CLI** — see the
[AWS Resource Explorer multi-account guide](https://docs.aws.amazon.com/resource-explorer/latest/userguide/manage-service-multi-account.html)
for the per-step commands. The cloudops deploy does NOT run these
commands itself; org-wide trusted access and aggregator setup belong
to the management account.

**Verify it's working** from the cloudops Lambda's account:

```bash
aws resource-explorer-2 list-indexes
# Expect at least one index of Type=AGGREGATOR.
aws resource-explorer-2 search --query-string "tag:none" --max-results 5
# Expect resources from multiple accounts; each has an OwningAccountId.
```

Resource Explorer Search returns up to 1000 results per query
(server-side cap). The handler surfaces `api_limit_reached: true` when
hit — narrow with `resource_type` or `region` to push filters
server-side.

---

## 3. Tag policy prerequisites (for `get_org_tag_compliance_summary`)

The org-wide rollup tool only returns data when at least one TAG_POLICY
is **attached** somewhere in the Organization (root, OU, or account)
and AWS has run the ~48h evaluation cycle. `check_tag_compliance` is
unaffected — it scans Resource Explorer live.

Minimum bring-up from the management account — **YOU run these,
cloudops does not automate organization-level config:**

```bash
# 1. Enable TAG_POLICY on the root (one-time)
ROOT_ID=$(aws organizations list-roots --query 'Roots[0].Id' --output text)
aws organizations enable-policy-type \
  --root-id "$ROOT_ID" --policy-type TAG_POLICY

# 2. Enable the tag-policies service principal (one-time)
aws organizations enable-aws-service-access \
  --service-principal tagpolicies.tag.amazonaws.com

# 3. Author + create + attach a minimal policy (e.g. require Environment)
#    See the AWS docs for the policy JSON syntax.
aws organizations create-policy --type TAG_POLICY ...
aws organizations attach-policy --policy-id <ID> --target-id "$ROOT_ID"
```

After ~48h, `get_org_tag_compliance_summary` returns populated rows.
For live results before then, use `check_tag_compliance`.

A policy is **not strictly required** for `check_tag_compliance` — the
agent can scan against caller-supplied required tags. The policy path
is for ongoing governance; caller-supplied is for ad-hoc checks.

---

## 4. Tool surface (7 tools)

| Tool | Purpose | API surface | Account scope |
|---|---|---|---|
| `get_required_tags` | Resolve required-tag policy from caller input or Organizations Tag Policy | `organizations:DescribeEffectivePolicy` | Single (caller account) |
| `list_tag_keys_in_use` | Enumerate every tag key currently applied. Useful for seeding a policy | `tag:GetTagKeys` | Single (account + region) |
| `check_tag_compliance` | Scan + classify resources. Default uses Resource Explorer + in-Python classifier; `use_aws_evaluation=true` switches to `GetResources(IncludeComplianceDetails=True)` | `resource-explorer-2:Search` (default) / `tag:GetResources` (AWS-evaluated) | Multi-account if RE multi-account is on; single account in AWS-eval mode |
| `get_org_tag_compliance_summary` | Aggregated counts via `GetComplianceSummary`. Cheap rollup grouped by account / region / resource type | `tag:GetComplianceSummary` (us-east-1) | Org-wide; **management-account-only API** |
| `find_untagged_resources` | Resource Explorer `tag:none` query — only way to surface zero-tagged resources | `resource-explorer-2:Search` | Multi-account if RE multi-account is on |
| `list_cost_allocation_tag_status` | Cost-allocation-tag activation state. Compliance ≠ billing activation | `ce:ListCostAllocationTags` (us-east-1) | Org-wide; **payer-account-only API** |
| `get_remediation_guidance` | Resource Explorer console deep-links pre-filtered per violation bucket | None (URL composer) | Same scope as preceding `check_tag_compliance` |

`tag:GetComplianceSummary` and `ce:ListCostAllocationTags` are
global-but-us-east-1-only — the handler hardcodes `us-east-1`
regardless of `AWS_REGION`.

---

## 5. Data model — what tools return

### `check_tag_compliance` (in-Python mode, default)

```
{
  "total_resources": 412,
  "compliant_count": 287,
  "non_compliant_count": 125,
  "compliance_pct": 69.7,
  "by_violation_type": {"missing_tag": 80, "invalid_value": 45},
  "by_required_tag": {
    "Environment": {"missing": 30, "invalid": 12},
    "Owner": {"missing": 50, "invalid": 33}
  },
  "top_non_compliant_resource_types": [{"name": "ec2:instance", "count": 42}, ...],
  "top_non_compliant_accounts":       [{"name": "111111111111", "count": 60}, ...],
  "top_non_compliant_regions":        [{"name": "us-east-1", "count": 88}, ...],
  "non_compliant_resources": [
    {"arn": "...", "account_id": "...", "region": "...", "resource_type": "...",
     "existing_tags": {...}, "compliance_status": "non_compliant",
     "violations": [{"tag_key": "...", "violation_type": "missing_tag", ...}]},
    ...
  ],
  "remediation_buckets": [
    {"tag_key": "Environment", "violation_type": "missing_tag",
     "account_id": "...", "region": "...", "resource_type": "ec2:instance",
     "count": 42},
    ...
  ],
  "scan_method": "resource_explorer",
  "system_managed_excluded": 14,
  "system_managed_note": "Excluded 14 system-managed resource(s) ...",
  "effective_policy": {"required_tag_keys": [...], "source": "aws_organizations", ...}
}
```

Degraded:

```
{"status": "resource_explorer_not_indexed",
 "error": "Resource Explorer is not configured with an aggregator index. ..."}
```

`use_aws_evaluation=true` returns the same shape but `total_resources`,
`compliant_count`, and `compliance_pct` are `null` (the API only
returns non-compliant rows), `scan_method` is
`"aws_server_side_evaluation"`, and a `global_resources_note` flags
that IAM / Route 53 / CloudFront are out of scope.

### `get_org_tag_compliance_summary`

```
{
  "group_by": ["TARGET_ID"],
  "count": 3,
  "total_noncompliant_resources": 42,
  "summary": [
    {"TargetId": "...", "TargetIdType": "ACCOUNT",
     "NonCompliantResources": 25, "LastUpdated": "..."},
    ...
  ],
  "accuracy_caveat": "These numbers depend on Tag Policy configuration ..."
}
```

### `get_remediation_guidance`

```
{
  "summary": "80 violation(s) for missing required tags, 45 for invalid values. ...",
  "links": [
    {
      "kind": "resource_explorer",
      "url": "https://console.aws.amazon.com/resource-explorer/home?...",
      "description": "Add 'Environment' tag to 42 ec2:instance resource(s) in us-east-1. Open link, select all, Actions > Manage tags > Add new tag.",
      "tag_key": "Environment",
      "violation_type": "missing_tag",
      "account_id": "...",
      "region": "us-east-1",
      "resource_type": "ec2:instance",
      "resource_count": 42
    },
    ...
  ],
  "total_buckets": 23,
  "links_truncated": true
}
```

Other tools (`get_required_tags`, `list_tag_keys_in_use`,
`find_untagged_resources`, `list_cost_allocation_tag_status`) follow
the same shape conventions — see `src/lambda/mcp/tag-governance/handler.py`
for full schemas.

---

## 6. Report template — `org_tag_governance`

The bundled report produces a four-section tag-governance review
([`src/agents/shared/report_templates/org_tag_governance.json`](../../src/agents/shared/report_templates/org_tag_governance.json)).
All sections run in parallel — no cross-section dependencies.

1. **Executive Summary** — `get_required_tags` → `check_tag_compliance(max_resources=0)`.
   Headline (compliance %, totals), required tags evaluated, per-tag-key
   missing/invalid breakdown, top 3 worst accounts.
2. **Non-Compliance Drill-Down** — `check_tag_compliance(max_resources=0)`
   plus a `get_cost_and_usage` cross-reference. Four artifacts: top 10
   resource types, by region, by account, highest-cost services with
   non-compliant resources.
3. **Key Recommendations** — `check_tag_compliance(max_resources=0)`
   for `compliance_pct`, then renders a tone-calibrated playbook (≥95%
   = maintain, 70–94% = backlog, <70% = aggressive remediation). Static
   best-practice content otherwise.
4. **Coverage Caveats** — narrative-only; Resource Explorer scope and
   ~200-service coverage.

The supervisor container rebuilds whenever `report_templates/`
changes (hash-invalidated). If a stale template ID lingers in DynamoDB
under your actor_id (from a UI save), it shadows the JSON file —
delete the row to fall back to the JSON.

---

## 7. Known gotchas

| Symptom | Likely cause | Fix |
|---|---|---|
| `check_tag_compliance` returns `status: 'resource_explorer_not_indexed'` | No Resource Explorer aggregator index | Enable RE multi-account search (§2.1) |
| `check_tag_compliance` returns `error: "No required-tag policy found"` | No caller-supplied tags AND no Organizations Tag Policy | Pass `required_tags=["Env","Owner",...]` or attach a TAG_POLICY (§3) |
| `get_org_tag_compliance_summary` returns `ConstraintViolationException: Tag policies may not be enabled` | No TAG_POLICY attached anywhere | Attach at least one TAG_POLICY to root, OU, or account (§3) |
| `get_org_tag_compliance_summary` returns `code: TagPoliciesServiceAccessDisabled` | `tagpolicies.tag.amazonaws.com` service access not enabled | `aws organizations enable-aws-service-access --service-principal tagpolicies.tag.amazonaws.com` |
| Empty `SummaryList` after fresh policy attachment | AWS evaluates org-wide every ~48h | Wait, or use `check_tag_compliance` for live results |
| `get_org_tag_compliance_summary` / `list_cost_allocation_tag_status` return `AccessDenied` | Lambda not running from the payer account | Run cloudops from the payer (Mode A or B). Cross-account assume-role is not supported |
| `check_tag_compliance` returns far fewer resources than expected | Resource Explorer is account-scoped | Enable multi-account search (§2.1) |
| Non-compliant counts look low for a service | Resource Explorer covers ~200 services — gaps include S3 objects, IAM, recently-launched services | Surface this in reporting; don't claim "all resources" |
| Tag is compliant per policy but doesn't show on the bill | Cost-allocation activation is a separate payer action | Use `list_cost_allocation_tag_status`; activate in Billing → Cost allocation tags |
| `api_limit_reached: true` in `find_untagged_resources` or `check_tag_compliance` | Resource Explorer Search capped at 1000 results | Pass single `resource_type` or `region` to filter server-side |
| Report still shows old section count after editing the JSON | DynamoDB shadow row under your actor_id (created by UI save) | Delete the row from `<prefix>-<env>-report-templates` and redeploy |
| Answers carry `data_source: "scheduled_snapshot"` but numbers look stale | Snapshot age within the schedule window (default 6h) | Ask again with `force_refresh=true`, or check the collector's `LAST_RUN` meta row / CloudWatch logs |
| Snapshot never serves (every call is slow/live) despite `enable_tag_snapshots = true` | Collector failing (see its log group) or snapshot TTL expired; also every filtered query is live BY DESIGN | `aws logs tail /aws/lambda/<prefix>-tag-governance-collector`; confirm the CACHE rows exist in `<prefix>-tag-compliance-snapshots` |
| Snapshot responses carry `snapshot_note` about trimmed detail lists | Full detail list exceeded the DynamoDB 400 KB item cap | Counts/breakdowns are still complete; use `force_refresh=true` when the full detail list matters |
