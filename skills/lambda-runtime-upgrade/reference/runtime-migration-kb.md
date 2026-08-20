# Lambda Runtime Migration Knowledge Base

Authoritative reference for the `lambda-runtime-upgrade` skill. Use this to
(1) classify a runtime's support status and (2) identify breaking changes when
analyzing function code. The AWS Lambda API does **not** return deprecation
status, so this table is the source of truth for status mapping.

> **Note on dates:** These are based on AWS published schedules and are
> approximate. Always cross-check the [AWS Lambda runtimes doc](https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html)
> for the latest deprecation calendar. When a runtime is not in this table,
> label it "unknown status" — do not guess a date.

## Status classification rules

Given today's date and a runtime's dates from the table below:
- **end_of_life (🔴 CRITICAL)** — today > `eol_date`
- **deprecated (🟠 HIGH)** — today > `deprecation_date` but ≤ `eol_date` (or no EOL set yet)
- **approaching (🟡 MEDIUM)** — `deprecation_date` is within the next 180 days
- **active (🟢 LOW / none)** — deprecation is more than 180 days out or unset

## Runtime support table

| Runtime | Family | Status baseline | Deprecation date | EOL date | Upgrade target |
|---------|--------|-----------------|------------------|----------|----------------|
| python3.8 | python | deprecated | 2024-10-14 | 2025-02-28 | python3.12 |
| python3.9 | python | active | 2025-09-01 | — | python3.12 |
| python3.10 | python | active | 2026-06-01 | — | python3.13 |
| python3.11 | python | active | 2026-12-01 | — | python3.13 |
| python3.12 | python | active | — | — | python3.13 |
| python3.13 | python | active | — | — | — |
| nodejs14.x | nodejs | deprecated | 2023-12-04 | 2024-03-11 | nodejs20.x |
| nodejs16.x | nodejs | deprecated | 2024-06-12 | 2024-09-11 | nodejs20.x |
| nodejs18.x | nodejs | active | 2025-09-01 | — | nodejs22.x |
| nodejs20.x | nodejs | active | 2026-06-01 | — | nodejs22.x |
| nodejs22.x | nodejs | active | — | — | — |
| java8 | java | deprecated | 2024-01-08 | 2024-04-08 | java21 |
| java8.al2 | java | deprecated | 2024-08-01 | 2024-11-01 | java21 |
| java11 | java | active | 2025-09-01 | — | java21 |
| java17 | java | active | 2026-09-01 | — | java21 |
| java21 | java | active | — | — | — |
| dotnet6 | dotnet | deprecated | 2024-02-29 | 2024-05-29 | dotnet8 |
| dotnet8 | dotnet | active | — | — | — |
| ruby3.2 | ruby | active | 2026-03-01 | — | ruby3.3 |
| ruby3.3 | ruby | active | — | — | — |
| provided.al2 | custom | active | 2025-09-01 | — | provided.al2023 |
| provided.al2023 | custom | active | — | — | — |

## Breaking changes by family

### Python (3.8 → 3.9 → 3.10 → 3.11 → 3.12 → 3.13)
- **3.8→3.9:** No major breaks. New: dict union (`|`), `str.removeprefix/removesuffix`.
- **3.9→3.10:** `match`/`case` added. Typing changes — prefer `X | Y` over `Union`.
- **3.10→3.11:** Exception groups, `tomllib`. No major breaks.
- **3.11→3.12:** REMOVED stdlib modules — `distutils`, `imp`, `aifc`, `audioop`, `cgi`, `cgitb`, `chunk`, `crypt`, `imghdr`, `mailcap`, `msilib`, `nis`, `nntplib`, `ossaudiodev`, `pipes`, `sndhdr`, `spwd`, `sunau`, `telnetlib`, `uu`, `xdrlib`. Nested f-strings allowed.
- **3.12→3.13:** Removed deprecated `typing` aliases. Experimental free-threading.
- **AWS SDK:** boto3/botocore bundled version changes per runtime — pin in `requirements.txt`.
- **Powertools:** check the Lambda Powertools compatibility matrix per runtime.
- **Scan the code for:** `import distutils`, `from cgi import`, `import imp`, `import telnetlib`, and the other removed modules above.

### Node.js (14 → 16 → 18 → 20 → 22)
- **16→18:** `fetch()` is a global. OpenSSL 3.0 (breaks some legacy crypto). AWS SDK v3 required — v2 is no longer bundled.
- **18→20:** ESM stable. `url.parse()` deprecated. V8 11.3.
- **20→22:** `require(esm)` support. Built-in WebSocket.
- **AWS SDK migration (major):** `aws-sdk` (v2, single package) → `@aws-sdk/client-*` (v3, modular). Every `require('aws-sdk')` must change.
- **CommonJS→ESM:** add `"type": "module"` in `package.json` or rename to `.mjs`.
- **Scan the code for:** `require('aws-sdk')`, `require("aws-sdk")`, legacy `crypto.createCipher`, `url.parse(`.

### Java (8 → 11 → 17 → 21)
- **8→11:** JPMS modules. REMOVED: `javax.xml.bind`, `javax.activation`, CORBA — add as Maven/Gradle deps.
- **11→17:** Sealed classes, records. Removed: Nashorn, RMI Activation.
- **17→21:** Virtual threads, pattern matching, record patterns.
- **AWS SDK:** v1 (`com.amazonaws`) → v2 (`software.amazon.awssdk`). Handler interface unchanged.
- **Build:** update `maven-compiler-plugin` source/target; check reflection under modules.
- **Scan the code for:** `import javax.xml.bind`, `import javax.activation`, `com.amazonaws.` imports (SDK v1).

### .NET (6 → 8)
- `System.Text.Json` breaking changes. AOT compilation available. Lambda hosting package update.
- `Amazon.Lambda.AspNetCoreServer` → `Amazon.Lambda.AspNetCoreServer.Hosting`.
- **Scan the code for:** old `Amazon.Lambda.AspNetCoreServer` references, `.csproj` `<TargetFramework>net6.0</TargetFramework>`.

### Ruby (3.2 → 3.3)
- YJIT improvements, Prism parser. Minimal breaking changes.

### Custom (provided.al2 → provided.al2023)
- glibc 2.34+, OpenSSL 3.0, newer kernel. MUST recompile all native binaries.
- **Go:** ensure static linking or rebuild against AL2023.
- **Rust:** rebuild against AL2023.
- **Scan for:** dynamically linked native binaries, `bootstrap` files compiled against AL2.

## Dependency manifests to inspect per family

| Family | Manifest files |
|--------|----------------|
| Python | `requirements.txt`, `Pipfile`, `pyproject.toml` |
| Node.js | `package.json`, `package-lock.json` |
| Java | `pom.xml`, `build.gradle` |
| .NET | `*.csproj` |
| Ruby | `Gemfile`, `Gemfile.lock` |
| Custom (Go) | `go.mod`, `go.sum` |
