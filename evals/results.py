"""Machine-readable results, provenance, redaction, and the readable summary.

``results/<run_id>/results.jsonl`` keeps full correlation identifiers
(session/trace/span ids) for debugging and is gitignored — treat it as a
restricted artifact. ``summary.md`` is the shareable view: account ids and
ARNs are masked and correlation ids are shortened.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = REPO_ROOT / "evals" / "results"
SCHEMA_VERSION = 1

_ACCOUNT_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")
_ARN_RE = re.compile(r"arn:aws[a-zA-Z-]*:[a-z0-9-]+:[a-z0-9-]*:\d{12}:[^\s\"',)]+")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_TOKEN_RE = re.compile(r"(eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})")


def redact(text: str | None) -> str | None:
    """Mask ARNs, account ids, emails, and JWTs in free text."""
    if not text:
        return text
    text = _TOKEN_RE.sub("<jwt>", text)
    text = _ARN_RE.sub(lambda m: _ACCOUNT_RE.sub("<account>", m.group(0)), text)
    text = _ACCOUNT_RE.sub("<account>", text)
    return _EMAIL_RE.sub("<email>", text)


def short_id(value: str | None, keep: int = 8) -> str:
    if not value:
        return "-"
    return value if len(value) <= keep else f"{value[:keep]}…"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=REPO_ROOT, text=True, stderr=subprocess.DEVNULL).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def _version(pkg: str) -> str:
    try:
        return importlib.metadata.version(pkg)
    except importlib.metadata.PackageNotFoundError:
        return ""


def provenance(*, mode: str, region: str, suite_path: Path, suite_sha: str, model_override: str | None) -> dict[str, Any]:
    """Non-secret context needed to compare runs."""
    return {
        "git_commit": _git("rev-parse", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain", "--untracked-files=no")),
        "mode": mode,
        "data_source": mode,
        "region": region,
        "suite": str(suite_path.relative_to(REPO_ROOT)) if suite_path.is_relative_to(REPO_ROOT) else suite_path.name,
        "suite_sha256": suite_sha,
        "hierarchy_sha256": _sha256_file(REPO_ROOT / "src" / "agents" / "hierarchy.json"),
        "model_override": model_override,
        "python": platform.python_version(),
        "packages": {
            p: _version(p)
            for p in ("bedrock-agentcore", "strands-agents", "boto3", "opentelemetry-sdk", "aws-opentelemetry-distro")
        },
    }


@dataclass
class ScenarioOutcome:
    scenario_id: str
    mode: str
    domain: str
    kind: str
    session_id: str
    execution_error: str | None
    telemetry: dict[str, Any]
    span_summary: dict[str, Any]
    checks: list[dict[str, Any]]
    evaluations: list[dict[str, Any]]
    expected_outcome: str = ""
    response_excerpt: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class ResultWriter:
    def __init__(self, run_id: str, prov: dict[str, Any], evaluator_config: list[dict[str, Any]], out_dir: Path | None = None):
        self.run_id = run_id
        self.dir = (out_dir or RESULTS_DIR) / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.prov = prov
        self.evaluator_config = evaluator_config
        self.outcomes: list[ScenarioOutcome] = []
        self._jsonl = (self.dir / "results.jsonl").open("w", encoding="utf-8")
        self._write({"type": "run", "schema_version": SCHEMA_VERSION, "run_id": run_id,
                     "started_at": datetime.now(timezone.utc).isoformat(),
                     "provenance": prov, "evaluators": evaluator_config})

    def _write(self, obj: dict[str, Any]) -> None:
        self._jsonl.write(json.dumps(obj, default=str) + "\n")
        self._jsonl.flush()

    def add(self, outcome: ScenarioOutcome) -> None:
        self.outcomes.append(outcome)
        self._write({"type": "scenario", "run_id": self.run_id, **outcome.__dict__})

    def close(self) -> Path:
        self._write({"type": "run_end", "run_id": self.run_id, "finished_at": datetime.now(timezone.utc).isoformat()})
        self._jsonl.close()
        summary = self.dir / "summary.md"
        summary.write_text(render_summary(self.run_id, self.prov, self.evaluator_config, self.outcomes), encoding="utf-8")
        return summary


def _fmt_value(e: dict[str, Any]) -> str:
    if e["status"] != "SCORED":
        return e["status"]
    v = e.get("value")
    label = e.get("label")
    if v is None:
        return str(label)
    return f"{v:.2f}" + (f" ({label})" if label else "")


def render_summary(run_id: str, prov: dict[str, Any], evaluator_config: list[dict[str, Any]], outcomes: list[ScenarioOutcome]) -> str:
    lines = [
        f"# AgentCore Evaluations run `{run_id}`",
        "",
        f"- Mode / data source: **{prov['mode']}**",
        f"- Commit: `{short_id(prov.get('git_commit'), 12)}`{' (dirty)' if prov.get('git_dirty') else ''}",
        f"- Suite: `{prov.get('suite')}` (sha256 `{short_id(prov.get('suite_sha256'), 12)}`)",
        f"- hierarchy.json sha256: `{short_id(prov.get('hierarchy_sha256'), 12)}`",
        f"- Region: `{prov.get('region')}`",
        f"- Packages: " + ", ".join(f"{k} {v or 'n/a'}" for k, v in prov.get("packages", {}).items()),
        "",
        "Scores are LLM-judge signals for comparing runs, not pass/fail gates. "
        "Statuses other than SCORED are infrastructure outcomes and carry no quality signal.",
        "",
        "## Evaluators",
        "",
        "| Evaluator | Level | Available |",
        "|---|---|---|",
    ]
    for ev in evaluator_config:
        lines.append(f"| `{ev['evaluator_id']}` | {ev.get('level') or '-'} | {'yes' if ev.get('available') else 'no: ' + redact(ev.get('error', '')) } |")

    ev_ids = [ev["evaluator_id"] for ev in evaluator_config]
    lines += ["", "## Scenario results", "",
              "| Scenario | Kind | Telemetry | Route check | " + " | ".join(i.replace("Builtin.", "") for i in ev_ids) + " |",
              "|---|---|---|---|" + "---|" * len(ev_ids)]
    for o in outcomes:
        by_ev: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for e in o.evaluations:
            by_ev[e["evaluator_id"]].append(e)
        route = next((c for c in o.checks if c["check"] == "route"), None)
        route_s = "-" if route is None else ("pass" if route["passed"] else "FAIL")
        cells = []
        for ev in ev_ids:
            items = by_ev.get(ev, [])
            if not items:
                cells.append("-")
            elif len(items) == 1:
                cells.append(_fmt_value(items[0]))
            else:
                scored = [i["value"] for i in items if i["status"] == "SCORED" and i.get("value") is not None]
                cells.append(f"mean {sum(scored) / len(scored):.2f} (n={len(scored)}/{len(items)})" if scored else items[0]["status"])
        tel = o.telemetry["status"] if not o.execution_error else "EXEC_ERROR"
        lines.append(f"| `{o.scenario_id}` | {o.kind} | {tel} | {route_s} | " + " | ".join(cells) + " |")

    lines += ["", "## Details", ""]
    for o in outcomes:
        lines.append(f"### `{o.scenario_id}` ({o.domain}, {o.kind})")
        lines.append("")
        lines.append(f"- Session: `{short_id(o.session_id, 24)}`")
        if o.expected_outcome:
            lines.append(f"- Expected: {o.expected_outcome}")
        if o.execution_error:
            lines.append(f"- Execution error: {redact(o.execution_error)}")
        if o.telemetry.get("problems"):
            lines.append("- Telemetry problems: " + "; ".join(o.telemetry["problems"]))
        for c in o.checks:
            if c["check"] == "route":
                lines.append(f"- Route: expected {c['expected']} → actual {c['actual']} ({'pass' if c['passed'] else 'FAIL'})")
        if o.response_excerpt:
            lines.append(f"- Response excerpt: {redact(o.response_excerpt)}")
        for e in o.evaluations:
            detail = e.get("explanation") or e.get("error_message") or ""
            detail = redact(detail.replace("\n", " "))
            if len(detail) > 400:
                detail = detail[:400] + "…"
            lines.append(f"  - `{e['evaluator_id']}` [{_fmt_value(e)}] {detail}")
        lines.append("")
    return "\n".join(lines) + "\n"
