"""Live mode: invoke the deployed supervisor, then collect its telemetry.

Scenarios are read-only questions; nothing here mutates the AWS resources the
agents inspect. Invocations still behave like normal user traffic: they write
conversation turns to AgentCore Memory, emit telemetry, and incur Bedrock,
AgentCore, and CloudWatch charges (see docs/evaluations.md).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from evals.scenarios import Scenario

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"
AWS_SPANS_LOG_GROUP = "aws/spans"


# ---------------------------------------------------------------------------
# Deployment discovery
# ---------------------------------------------------------------------------


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _tf_output(name: str, as_json: bool = False) -> Any:
    args = ["terraform", f"-chdir={REPO_ROOT / 'terraform'}", "output", "-no-color"]
    args += ["-json", name] if as_json else ["-raw", name]
    try:
        raw = subprocess.check_output(args, stderr=subprocess.DEVNULL, text=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {} if as_json else ""
    raw = re.sub(r"(?s)╷.*?╵", "", raw).strip()
    if as_json:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    lines = [l for l in raw.splitlines() if l and not l.startswith(("Warning:", "│"))]
    return lines[0] if lines else ""


@dataclass(frozen=True)
class Deployment:
    region: str
    supervisor_url: str
    supervisor_runtime_id: str
    supervisor_endpoint: str
    cognito_client_id: str
    child_runtime_ids: dict[str, str]
    child_endpoints: dict[str, str]

    def log_group(self, runtime_id: str, endpoint: str) -> str:
        return f"/aws/bedrock-agentcore/runtimes/{runtime_id}-{endpoint}"

    @property
    def supervisor_log_group(self) -> str:
        return self.log_group(self.supervisor_runtime_id, self.supervisor_endpoint)

    def child_log_groups(self) -> dict[str, str]:
        return {
            name: self.log_group(rid, self.child_endpoints.get(name, "DEFAULT"))
            for name, rid in self.child_runtime_ids.items()
        }


def discover_deployment() -> Deployment:
    """Read the deployed stack's identifiers from Terraform outputs."""
    _load_env_file(REPO_ROOT / ".env")
    _load_env_file(REPO_ROOT / "scripts" / ".env")
    region = os.environ.get("AWS_REGION", "us-east-1")
    url = _tf_output("supervisor_url")
    if not url:
        raise RuntimeError("No deployed stack found: terraform output 'supervisor_url' is empty")
    arn = unquote(url.split("/runtimes/", 1)[1].split("/invocations", 1)[0])
    runtime_id = arn.rsplit("/", 1)[-1]
    endpoint = _tf_output("endpoint_name") or "DEFAULT"
    return Deployment(
        region=region,
        supervisor_url=url,
        supervisor_runtime_id=runtime_id,
        supervisor_endpoint=endpoint,
        cognito_client_id=_tf_output("cognito_app_client_id"),
        child_runtime_ids=_tf_output("agent_runtime_ids", as_json=True) or {},
        child_endpoints=_tf_output("agent_endpoint_names", as_json=True) or {},
    )


def cognito_token(dep: Deployment) -> str:
    import boto3

    user = os.environ.get("COGNITO_USERNAME", "")
    password = os.environ.get("COGNITO_PASSWORD", "")
    if not user or not password:
        raise RuntimeError("COGNITO_USERNAME / COGNITO_PASSWORD not set (scripts/.env)")
    resp = boto3.client("cognito-idp", region_name=dep.region).initiate_auth(
        ClientId=dep.cognito_client_id,
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": user, "PASSWORD": password},
    )
    return resp["AuthenticationResult"]["IdToken"]


# ---------------------------------------------------------------------------
# Invocation
# ---------------------------------------------------------------------------


@dataclass
class LiveRun:
    session_id: str
    started_at: datetime
    finished_at: datetime
    response_text: str = ""
    sse_tool_calls: list[str] = field(default_factory=list)
    error: str | None = None


def invoke(dep: Deployment, token: str, scenario: Scenario, timeout: float = 300.0) -> LiveRun:
    """Invoke the supervisor over AG-UI with an explicit runtime session id."""
    import httpx

    session_id = f"eval-{scenario.id}-{uuid.uuid4()}"
    actor = os.environ.get("EVAL_ACTOR_ID", "agentcore-evals")
    payload = {
        "threadId": session_id,
        "runId": str(uuid.uuid4()),
        "messages": [{"id": str(uuid.uuid4()), "role": "user", "content": scenario.prompt}],
        "state": {},
        "tools": [],
        "context": [],
        "forwardedProps": {"session_id": session_id, "actor_id": actor},
    }
    run = LiveRun(session_id=session_id, started_at=datetime.now(timezone.utc), finished_at=datetime.now(timezone.utc))
    try:
        with httpx.Client(timeout=timeout) as client, client.stream(
            "POST",
            dep.supervisor_url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                SESSION_HEADER: session_id,
            },
        ) as resp:
            if resp.status_code != 200:
                resp.read()
                run.error = f"HTTP {resp.status_code}: {resp.text[:300]}"
                return run
            buffer = ""
            for chunk in resp.iter_bytes():
                buffer += chunk.decode("utf-8", errors="replace")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    etype = event.get("type", "")
                    if etype == "TEXT_MESSAGE_CONTENT":
                        run.response_text += event.get("delta", "")
                    elif etype == "TOOL_CALL_START":
                        run.sse_tool_calls.append(str(event.get("toolCallName", "")))
                    elif etype == "RUN_ERROR":
                        run.error = str(event.get("message", "RUN_ERROR"))
    except Exception as exc:  # noqa: BLE001 - recorded as an execution error
        run.error = f"{type(exc).__name__}: {exc}"
    finally:
        run.finished_at = datetime.now(timezone.utc)
    return run


# ---------------------------------------------------------------------------
# Telemetry collection
# ---------------------------------------------------------------------------


def collect_session_spans(dep: Deployment, run: LiveRun, max_wait: int = 600, poll: int = 30) -> list[dict]:
    """Supervisor session spans + content records (aws/spans + runtime log group)."""
    from bedrock_agentcore.evaluation import CloudWatchAgentSpanCollector

    collector = CloudWatchAgentSpanCollector(
        log_group_name=dep.supervisor_log_group,
        region=dep.region,
        max_wait_seconds=max_wait,
        poll_interval_seconds=poll,
    )
    return collector.collect(
        session_id=run.session_id,
        start_time=run.started_at - timedelta(minutes=1),
        end_time=run.finished_at,
    )


def _insights(logs, log_group: str, query: str, start: datetime, end: datetime, wait: int = 60) -> list[dict]:
    qid = logs.start_query(
        logGroupName=log_group,
        startTime=int(start.timestamp()),
        endTime=int(end.timestamp()),
        queryString=query,
        limit=1000,
    )["queryId"]
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        res = logs.get_query_results(queryId=qid)
        if res["status"] in ("Complete", "Failed", "Cancelled", "Timeout"):
            return [{f["field"]: f["value"] for f in row} for row in res.get("results", [])]
        time.sleep(2)
    return []


def child_services_in_traces(dep: Deployment, trace_ids: list[str], run: LiveRun) -> list[str]:
    """service.name values seen in aws/spans for the supervisor's trace(s).

    If trace context propagates across ``InvokeAgentRuntime``, child runtime
    spans share the supervisor's trace id and show up here. If it does not,
    coverage is reported INCOMPLETE rather than silently evaluating only the
    supervisor.
    """
    if not trace_ids:
        return []
    import boto3

    logs = boto3.client("logs", region_name=dep.region)
    quoted = ", ".join(f'"{t}"' for t in trace_ids[:50])
    query = (
        f"filter traceId in [{quoted}]"
        "\n| stats count(*) as n by resource.attributes.service.name as svc"
    )
    try:
        rows = _insights(
            logs, AWS_SPANS_LOG_GROUP, query,
            run.started_at - timedelta(minutes=1), run.finished_at + timedelta(minutes=10),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Child-trace lookup failed: %s", exc)
        return []
    return [r.get("svc", "") for r in rows if r.get("svc")]


def normalize_child_services(services: list[str], child_names: list[str]) -> list[str]:
    """Map raw service.name values (which may carry runtime/endpoint suffixes)
    back to hierarchy agent names."""
    found = []
    for name in child_names:
        if any(svc == name or svc.startswith(f"{name}.") or name in svc for svc in services):
            found.append(name)
    return found
