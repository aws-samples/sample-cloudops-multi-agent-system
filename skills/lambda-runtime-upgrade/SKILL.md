---
name: lambda-runtime-upgrade
description: "AWS Lambda runtime upgrade assessment — discovers functions on deprecated/EOL runtimes across regions, analyzes code compatibility, and produces migration guidance for Python, Node.js, Java, .NET, Ruby, and custom runtimes. Three paths: (1) MCP tools via deployed gateway (preferred); (2) direct AWS CLI; (3) delegation to coding agent. Uses parallel multi-region scanning and an upfront region-selection Q&A to keep report generation fast (5-10 min). Produces a structured markdown migration report. Use when the user asks about Lambda runtime upgrades, deprecated runtimes, EOL runtimes, runtime migration, or Lambda function modernization."
argument-hint: "[what do you want? e.g. 'find deprecated lambda functions', 'upgrade report for us-east-1', 'migrate my-function from python3.8']"
allowed-tools: Bash, Write, Read, Glob, Grep
user-invocable: true
---

# AWS Lambda Runtime Upgrade Assessment

Discover AWS Lambda functions running on deprecated or end-of-life (EOL) runtimes, analyze their code for compatibility issues, and produce a prioritized migration report with concrete code changes. Output is structured markdown — tables, before/after code snippets, and migration checklists.

This skill is designed to be **fast** through two mechanisms:
1. **Upfront Q&A** — ask which regions to scan *before* doing heavy work, so you never analyze regions the user doesn't care about.
2. **Parallel processing** — scan all selected regions concurrently rather than one at a time.

## Routing — read first, before anything else

This skill works in three environments. Decide which path you are on **before** you start.

```
Are lambda-runtime MCP tools available (discover_lambda_regions,
get_deprecated_functions_multi_region, get_function_code, etc.)?
├── Yes → Path M (use MCP tools — fastest, handles cross-account + parallel scan)
└── No
    ├── Can you run `aws --version`?
    │   ├── Yes → Path A (AWS CLI directly — parallelize with background jobs)
    │   └── No
    │       ├── Is a coding agent enabled? → Path B (delegate — see Appendix)
    │       └── No → Stop. Tell the user to deploy with DEPLOY_MODE=gateway-only
    │                    and connect the gateway, or run in a shell-capable agent.
```

## Interactive Q&A flow (do this on every path)

The single biggest speed win is scoping the work before you start. Ask these questions **up front**, then proceed without further interruption:

**Question 1 — Scope.** "Do you want me to scan a specific function, specific regions, or discover all regions with Lambda functions first?"
- If the user names a **single function** → skip discovery, go straight to code analysis for that one function.
- If the user names **specific regions** → skip discovery, scan those regions directly.
- If the user says **"all" / "discover" / is unsure** → run region discovery (Q2).

**Question 2 — Region selection (only when discovering).** Run region discovery, then present a table:

```
| Region | Total Functions | Deprecated | EOL | Needs Attention |
|--------|----------------:|-----------:|----:|----------------:|
| us-east-1 | 45 | 8 | 3 | 11 |
| eu-west-1 | 12 | 2 | 0 | 2 |
```

Then ask: "Which regions should I include? (all with issues / specific regions / all)". Wait for the answer.

**Question 3 — Depth (optional, offer a default).** "Full report with code analysis (5-10 min), or a quick inventory only (under 1 min)?" Default to full if they don't answer.

Once you have the answers, run the rest without further questions.

## Path M — MCP Tools (preferred)

When these tools are available, use them directly. They handle cross-account access and run region scans in parallel server-side.

| Tool | Use for |
|------|---------|
| `discover_lambda_regions` | Find all regions with Lambda functions + per-region deprecated/EOL counts. Runs a parallel scan across all enabled regions. Call this for Q2. |
| `get_deprecated_functions_multi_region` | Scan MULTIPLE regions in parallel for deprecated/EOL functions. The main workhorse — pass the user's selected regions. |
| `get_deprecated_functions` | Scan a SINGLE region (use only if the user picked exactly one region). |
| `list_functions_by_runtime` | List functions filtered by runtime/region with status. |
| `get_function_configuration` | Function setup — runtime, handler, layers, memory, architecture, env var keys. |
| `get_function_code` | Download source + dependency manifests for compatibility analysis. |
| `get_runtime_support_status` | All runtimes with deprecation/EOL dates and upgrade targets. |

### Workflow

1. **Q&A** — run the interactive flow above to scope regions and depth.
2. **Discover** (if needed) — `discover_lambda_regions`, present the region table, get the user's selection.
3. **Scan (parallel)** — `get_deprecated_functions_multi_region` with the selected regions and `include_approaching_deprecation=true`.
4. **Analyze (selective, for speed)** — for the **top 5** CRITICAL/HIGH priority functions only, call `get_function_configuration` then `get_function_code`, and check the code against `reference/runtime-migration-kb.md`. For MEDIUM/LOW functions, give guidance from the knowledge base without downloading code.
5. **Report** — format using the output template below.

### Single-function flow

If the user named one function: `get_function_configuration` → `get_function_code` → analyze against the KB → produce just that function's section (skip the executive summary and region grouping).

## Path A — AWS CLI

If MCP tools aren't available but the AWS CLI is. **Parallelize** per-region calls with background jobs to stay fast.

```bash
# Q2 — discover which regions have functions (parallel across regions)
regions=$(aws ec2 describe-regions \
  --filters Name=opt-in-status,Values=opt-in-not-required,opted-in \
  --query 'Regions[].RegionName' --output text)

for r in $regions; do
  (
    count=$(aws lambda list-functions --region "$r" \
      --query 'length(Functions)' --output text 2>/dev/null)
    echo "$r $count"
  ) &
done
wait   # let all regions finish in parallel

# Scan a chosen region for runtimes (repeat per selected region, in parallel)
aws lambda list-functions --region us-east-1 \
  --query 'Functions[].{name:FunctionName,runtime:Runtime,modified:LastModified,size:CodeSize}' \
  --output json

# Get a function's configuration
aws lambda get-function-configuration --function-name my-func --region us-east-1

# Download a function's code (returns a presigned URL under Code.Location)
url=$(aws lambda get-function --function-name my-func --region us-east-1 \
  --query 'Code.Location' --output text)
curl -s "$url" -o /tmp/my-func.zip && unzip -o /tmp/my-func.zip -d /tmp/my-func
```

Classify each runtime against the deprecation table in `reference/runtime-migration-kb.md` (the AWS CLI does not return deprecation status — you must map it yourself). Then analyze the extracted code for the breaking changes listed in the KB.

**Container-image functions:** `aws lambda get-function` returns `Code.ImageUri` instead of `Code.Location`. You cannot download the code — note this and give generic KB-based guidance.

## Path B — Delegate

Hand the coding agent the brief in the [Appendix](#appendix--path-b-brief-for-coding-agent) and relay only its final summary back to the user.

## Priority assignment rules

- 🔴 **CRITICAL** — runtime is past its End-of-Life date. No security patches. Upgrade immediately.
- 🟠 **HIGH** — runtime is deprecated (past deprecation, before EOL). Limited support.
- 🟡 **MEDIUM** — runtime deprecates within 6 months. Plan the upgrade now.
- 🟢 **LOW** — runtime is active but a newer LTS exists. Upgrade at convenience.

## Bundled reference file

Ships with the skill in the `reference/` folder. **Read it when you need it — do not paraphrase deprecation dates or breaking changes from memory.**

| File | When to load |
|------|--------------|
| `reference/runtime-migration-kb.md` | Before classifying runtimes (Path A must map status manually) and before analyzing code. Authoritative deprecation/EOL dates, upgrade targets, and per-family breaking changes for Python, Node.js, Java, .NET, Ruby, and custom runtimes. |

## Output Template

```markdown
# Lambda Runtime Upgrade Report

**Regions scanned:** [list]
**Account:** [account ID]
**Generated:** [timestamp]

## How to Use This Report
1. Review the Executive Summary for scope and urgency.
2. Address CRITICAL priority functions first (EOL = security risk).
3. For each function, follow its Migration Checklist.
4. Test each upgraded function in a non-production environment before deploying.
5. Update CI/CD pipelines that pin the old runtime.
6. Verify Lambda layers are compatible with the new runtime.
7. Monitor CloudWatch metrics post-upgrade for errors or latency.

## Executive Summary

| Metric | Value |
|--------|-------|
| Total functions needing upgrade | [n] |
| 🔴 CRITICAL (EOL) | [n] |
| 🟠 HIGH (deprecated) | [n] |
| 🟡 MEDIUM (approaching) | [n] |
| Regions affected | [list] |

### By Runtime
| Runtime | Status | Target | Count |
|---------|--------|--------|------:|
| python3.8 | EOL | python3.12 | [n] |

## Region: [region-name]

### Function: [function-name]
| Field | Value |
|-------|-------|
| Current Runtime | [runtime] |
| Target Runtime | [target] |
| Priority | 🔴/🟠/🟡 |
| Risk Level | LOW / MEDIUM / HIGH |
| Estimated Effort | 1-2 hours / half day / 1-2 days |

**Code Changes Required:** (top 5 CRITICAL/HIGH only — show before/after snippets in a fenced code block, e.g. `from cgi import parse_header` → `from email.message import Message` since cgi was removed in 3.12)

**Library Upgrades:**
| Library | Current | Required | Reason |
|---------|---------|----------|--------|
| boto3 | 1.26.0 | >=1.34.0 | Pin in requirements.txt |

**Migration Checklist:**
- [ ] Update runtime setting to [target]
- [ ] Apply code changes above
- [ ] Update dependencies
- [ ] Update Lambda layers
- [ ] Test in non-production
- [ ] Deploy and monitor 48h

## Migration Playbook
- **Phase 1 (This Week):** CRITICAL functions
- **Phase 2 (This Month):** HIGH functions
- **Phase 3 (This Quarter):** MEDIUM functions

## Common Pitfalls
- [3-5 pitfalls specific to the runtime families found]

## Rollback Plan
- Use Lambda versioning and aliases
- Set CloudWatch error-rate alarms before switching
- Keep the previous version for instant rollback
```

Adapt the template — for a single-function request, emit just that function's section. For a quick inventory, emit only the Executive Summary + region tables (skip code analysis).

## Constraints — do not violate

- **Read-only.** Only call `lambda:ListFunctions`, `lambda:GetFunction`, `lambda:GetFunctionConfiguration`, and `ec2:DescribeRegions`. Never call `UpdateFunctionConfiguration`, `UpdateFunctionCode`, `DeleteFunction`, `PublishVersion`, or any other mutating API. This skill only *recommends* upgrades — it never performs them.
- **Never fabricate.** Only list code changes you can VERIFY from actual downloaded source. If code can't be downloaded (container image), say so and give generic KB-based guidance. Don't invent library versions, deprecation dates, or breaking changes.
- **Deprecation dates come from the KB.** Use `reference/runtime-migration-kb.md` as the source of truth — the AWS CLI/API does not return runtime support status.
- **No credential exfiltration.** Never put access keys, session tokens, or SSO refresh tokens in output. Account ID is fine.
- **Scope before scanning.** Always run the region-selection Q&A before a multi-region scan. Don't scan every region unprompted when the user only cares about one.
- **Speed budget.** Full report targets 5-10 minutes. Only download code for the top 5 CRITICAL/HIGH functions; use KB guidance for the rest.

## Failure modes — handle explicitly

| Situation | Behavior |
|-----------|----------|
| `aws sts get-caller-identity` fails | Stop. Tell the user to authenticate (`aws sso login` or set credentials). Do not produce a report. |
| One region returns AccessDenied | Continue. Log the gap. Mark that region as "data unavailable" and scan the rest. |
| `get_function_code` returns empty / ImageUri | Container-image packaging — code isn't downloadable. Note it, give generic KB guidance for that function. |
| Runtime not in the KB table | Label it "unknown status" — don't guess a deprecation date. Suggest checking the AWS Lambda runtime docs. |
| No deprecated functions found | Report "All scanned functions are on active runtimes" — don't invent issues. |
| Cross-account role not configured (Path A) | Results are single-account only — inform the user. |

---

## Appendix — Path B brief (for coding agent)

Use this section only when you are on **Path B** (a sandboxed host with no shell, but a coding agent is available). Send the block below to the coding agent in one shot.

### Brief — send verbatim

```
You are a coding agent running on the user's local machine with shell access
and (assuming the user authenticated) AWS CLI credentials. The host calling
you does not — that is why I am routing this task to you.

Produce an AWS Lambda runtime upgrade report by following the skill spec on disk:

  skills/lambda-runtime-upgrade/SKILL.md   (relative to the repo root)

Read SKILL.md first (skip the "Routing" section — you are on Path A). Then load:

  reference/runtime-migration-kb.md   — deprecation/EOL dates + breaking changes

Execute:
  1. Prereq checks (`aws --version`, `aws sts get-caller-identity`). If any
     fails, stop and tell the user how to fix it.
  2. Ask the user (via the host) which regions to scan, OR use the regions
     they already specified: {{REGIONS_OR_ASK}}
  3. Discover regions in parallel if needed (background jobs + wait).
  4. For each selected region (in parallel), list functions and classify each
     runtime against reference/runtime-migration-kb.md (CRITICAL=EOL,
     HIGH=deprecated, MEDIUM=approaching).
  5. For the top 5 CRITICAL/HIGH functions: download code via
     `aws lambda get-function` presigned URL, unzip, analyze against the KB
     breaking-changes list. Container images can't be downloaded — note and
     give generic guidance.
  6. Produce the markdown report using the template in SKILL.md.

Hard constraints (from SKILL.md, do not violate):
- Read-only. Only List/Get Lambda APIs + ec2:DescribeRegions. No Update*,
  Delete*, PublishVersion, or any mutating call.
- Never fabricate. Verify code changes from actual source. Deprecation dates
  come only from reference/runtime-migration-kb.md.
- Never put credentials in output. Account ID is fine.
- Only download code for the top 5 CRITICAL/HIGH functions (speed budget).

When done, return ONLY:
- The report (markdown), or its file path if you saved it
- Top 3 highest-priority functions (one line each)
- One-sentence next step

User context (filled by host):
- Regions: {{REGIONS_OR_ASK}}
- AWS profile (if specified): {{AWS_PROFILE_OR_DEFAULT}}
```

If the user did not specify regions or profile, fill those slots with `(ask the user which regions)` and `(use the default profile / current AWS_PROFILE env)` before sending.

### After the coding agent returns

Surface only: the report (or its path), the top 3 highest-priority functions verbatim, and the one-sentence next step. If it reports an error (no creds, AccessDenied, expired SSO), pass it through unchanged with the relevant fix from the Failure modes table. Do not retry the work in your own sandbox — Path B is the only route in this environment.
