# Lambda Runtime Upgrade Agent

Discovers Lambda functions running on deprecated or end-of-life runtimes,
analyzes their source code for compatibility issues with newer runtimes,
and provides step-by-step migration guidance with concrete code changes.

**Representative prompts:**
- "Which of my Lambda functions are on deprecated runtimes?"
- "Analyze my-function for upgrading from Python 3.8 to 3.12"
- "Generate a runtime upgrade report"
- "How do I migrate my Node.js 16 Lambda to Node.js 20?"

## User Interaction Flow

The agent follows a **region-selection-first** workflow:

1. **Region Discovery** — Scans all enabled AWS regions in parallel to find
   which have Lambda functions and how many are deprecated/EOL.
2. **Region Selection** — Presents a summary table and asks the user which
   regions to include in the report.
3. **Multi-Region Scan** — Scans selected regions in parallel for deprecated
   functions using `get_deprecated_functions_multi_region`.
4. **Code Analysis** — Downloads code for the top 5 CRITICAL/HIGH priority
   functions only (speed optimization).
5. **Report Generation** — Produces a structured report with executive summary,
   region inventory, code analysis, and migration playbook.

**Target report generation time: 5-10 minutes** (achieved via parallel region
scanning, selective code download, and knowledge-base-driven guidance for
lower-priority functions).

## Supported runtimes

| Family | Versions tracked | Key migration concerns |
|--------|-----------------|----------------------|
| Python | 3.8 → 3.13 | Removed stdlib modules (3.12), boto3 bundling |
| Node.js | 14.x → 22.x | AWS SDK v2→v3, CommonJS→ESM, OpenSSL 3.0 |
| Java | 8 → 21 | JPMS modules, javax.* removal, SDK v1→v2 |
| .NET | 6 → 8 | AOT compilation, System.Text.Json changes |
| Ruby | 3.2 → 3.3 | Minimal breaking changes |
| Custom | provided.al2 → al2023 | glibc 2.34+, OpenSSL 3.0, recompile natives |

## Tools (7)

| Tool | Purpose | Performance |
|------|---------|-------------|
| `discover_lambda_regions` | Find all regions with Lambda functions | ~15-30s (parallel scan) |
| `list_functions_by_runtime` | List functions filtered by runtime/region | ~5-10s per region |
| `get_function_configuration` | Detailed function config | ~1-2s per function |
| `get_function_code` | Download source for analysis | ~3-10s per function |
| `get_runtime_support_status` | All runtimes with dates | Instant (static data) |
| `get_deprecated_functions` | Deprecated functions in one region | ~5-10s per region |
| `get_deprecated_functions_multi_region` | Deprecated functions across regions (parallel) | ~15-30s total |

## Deploy mode

Single mode — uses the Lambda execution role (or cross-account role if
configured) to read function metadata and code. Read-only; does NOT
modify any functions.

## Prerequisites

No special setup beyond standard deployment. The agent needs:
- `lambda:ListFunctions` — enumerate functions
- `lambda:GetFunction` — download deployment package
- `lambda:GetFunctionConfiguration` — read runtime/layer/handler config
- `ec2:DescribeRegions` — discover enabled regions

For cross-account scanning, configure `CROSS_ACCOUNT_ROLE_ARN` with the
above permissions in the target account.

## Speed Optimizations

The agent targets 5-10 minute report generation through:

1. **Parallel region discovery** — All regions scanned concurrently (10 threads)
2. **Multi-region deprecated scan** — Single tool call scans all selected regions in parallel
3. **Selective code analysis** — Only top 5 CRITICAL/HIGH functions get code downloaded
4. **Knowledge-base guidance** — MEDIUM/LOW functions get migration advice from the
   embedded runtime knowledge base without code download
5. **Reduced report sections** — 3 sections (down from 5) with only one dependency chain

## Data model

### Tool: `discover_lambda_regions`

```json
{
  "regions_with_functions": [
    {
      "region": "us-east-1",
      "total_functions": 45,
      "deprecated_count": 8,
      "eol_count": 3,
      "needs_attention": 11,
      "runtimes": {"python3.8": 3, "nodejs16.x": 5, "python3.12": 37}
    }
  ],
  "total_regions": 5,
  "total_functions": 120,
  "total_deprecated": 15,
  "total_eol": 5,
  "total_needing_attention": 20
}
```

### Tool: `get_deprecated_functions_multi_region`

```json
{
  "results_by_region": [
    {
      "region": "us-east-1",
      "functions": [
        {
          "function_name": "my-api-handler",
          "runtime": "python3.8",
          "runtime_status": "end_of_life",
          "upgrade_target": "python3.12",
          "handler": "handler.lambda_handler",
          "last_modified": "2024-03-15T10:30:00Z",
          "code_size_bytes": 45000
        }
      ],
      "count": 8
    }
  ],
  "total_functions": 15,
  "regions_scanned": 3,
  "by_runtime": {
    "python3.8": {"count": 5, "status": "end_of_life", "upgrade_target": "python3.12"},
    "nodejs16.x": {"count": 10, "status": "deprecated", "upgrade_target": "nodejs20.x"}
  },
  "by_priority": {"end_of_life": 5, "deprecated": 10, "approaching": 0}
}
```

## Known gotchas

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `discover_lambda_regions` slow | Many enabled regions with functions | Normal — scanning 15+ regions takes ~30s |
| `get_function_code` returns empty files | Function uses container image packaging (`PackageType: Image`) | Container images can't be downloaded via GetFunction. Check `package_type` in config first. |
| Code extraction hits size limit | Large deployment package with bundled deps | Use `include_patterns` param to target specific files, or increase `max_file_size_kb`. |
| Cross-account functions not listed | Missing IAM permissions in target account | Ensure the cross-account role has `lambda:ListFunctions` + `lambda:GetFunction` + `ec2:DescribeRegions`. |
| Runtime shows "unknown" | New runtime not yet in the support data table | Update `RUNTIME_SUPPORT_DATA` in the handler. |
| Report takes >10 min | Too many CRITICAL/HIGH functions triggering code download | The agent limits to top 5; if you have more, run a second pass. |
