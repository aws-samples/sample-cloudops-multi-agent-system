"""Frontend API Lambda — handles session, template, and report CRUD.

Invoked via API Gateway HTTP API with Cognito JWT authorizer.
The actor_id (user identity) is extracted from the JWT claims.

Routes:
  GET    /sessions                  → list sessions
  GET    /sessions/{id}/history     → session history
  DELETE /sessions/{id}             → delete session
  GET    /templates                 → list templates
  POST   /templates                 → create template
  PUT    /templates/{id}            → update template
  DELETE /templates/{id}            → delete template
  GET    /reports                   → list reports
  GET    /reports/{id}              → get report
  GET    /reports/{id}/status       → report status
  DELETE /reports/{id}              → delete report
  GET    /threads/{id}/activity     → per-thread busy state (running/idle/error)
"""

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
AGENTCORE_MEMORY_ID = os.environ.get("AGENTCORE_MEMORY_ID", "")
REPORT_TABLE_NAME = os.environ.get("REPORT_TABLE_NAME", "")
TEMPLATE_DIR = os.environ.get("TEMPLATE_DIR", "")

_memory_client = None
_ddb_client = None

# Frontend-only display tags saved as ASSISTANT events but never meant to
# render in the chat transcript. Mirrors the strip in shared/memory.py used
# by the agent's own history loader.
_SESSION_TITLE_RE = re.compile(
    r"\n?<session-title>.*?</session-title>", re.DOTALL
)
# Frontend-only marker appended to a user turn saved in report mode. We strip
# it from the content and surface it as an ``is_report`` flag so the UI can
# badge the prompt bubble (persists across reload). See memory.save_user_message.
_REPORT_REQUEST_RE = re.compile(r"\n?<report-request/>")


def _get_memory_client():
    global _memory_client
    if _memory_client is None:
        _memory_client = boto3.client("bedrock-agentcore", region_name=AWS_REGION)
    return _memory_client


def _get_ddb():
    global _ddb_client
    if _ddb_client is None:
        _ddb_client = boto3.client("dynamodb", region_name=AWS_REGION)
    return _ddb_client


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
        },
        "body": json.dumps(body, default=str),
    }


def _get_actor_id(event: dict) -> str:
    """Extract actor_id from JWT claims (Cognito email, sanitized)."""
    claims = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    )
    email = claims.get("email", "")
    if email:
        return email.replace("@", "_at_").replace(".", "_")
    # Fallback to sub
    return claims.get("sub", "anonymous")


def handler(event, context):
    """Main Lambda handler — routes based on HTTP method + path."""
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path = event.get("rawPath", "")
    actor_id = _get_actor_id(event)
    body = {}
    if event.get("body"):
        try:
            body = json.loads(event["body"])
        except (json.JSONDecodeError, TypeError):
            pass

    # Extract path parameters
    path_params = event.get("pathParameters", {}) or {}

    logger.info("Request: %s %s actor=%s", method, path, actor_id)

    try:
        # --- Sessions ---
        if path == "/sessions" and method == "GET":
            return _list_sessions(actor_id)
        if (
            path.startswith("/sessions/")
            and path.endswith("/history")
            and method == "GET"
        ):
            session_id = path_params.get("id", "")
            return _get_session_history(session_id, actor_id)
        if path.startswith("/sessions/") and method == "DELETE":
            session_id = path_params.get("id", "")
            return _delete_session(session_id, actor_id)

        # --- Templates ---
        if path == "/templates" and method == "GET":
            return _list_templates(actor_id)
        if path == "/templates" and method == "POST":
            return _create_template(actor_id, body)
        if path.startswith("/templates/") and method == "PUT":
            template_id = path_params.get("id", "")
            return _update_template(actor_id, template_id, body)
        if path.startswith("/templates/") and method == "DELETE":
            template_id = path_params.get("id", "")
            return _delete_template(actor_id, template_id)

        # --- Reports ---
        if path == "/reports" and method == "GET":
            return _list_reports(actor_id)
        if (
            path.startswith("/reports/")
            and path.endswith("/status")
            and method == "GET"
        ):
            report_id = path_params.get("id", "")
            return _get_report_status(actor_id, report_id)
        if path.startswith("/reports/") and method == "GET":
            report_id = path_params.get("id", "")
            return _get_report(actor_id, report_id)
        if path.startswith("/reports/") and method == "DELETE":
            report_id = path_params.get("id", "")
            return _delete_report(actor_id, report_id)

        # --- Threads ---
        if (
            path.startswith("/threads/")
            and path.endswith("/activity")
            and method == "GET"
        ):
            thread_id = path_params.get("id", "")
            return _get_thread_activity(actor_id, thread_id)

        return _response(404, {"error": f"Not found: {method} {path}"})
    except Exception as exc:
        logger.error("Handler error: %s", exc, exc_info=True)
        return _response(500, {"error": str(exc)})


# =============================================================================
# Session handlers
# =============================================================================


def _list_sessions(actor_id: str) -> dict:
    if not AGENTCORE_MEMORY_ID:
        return _response(200, {"sessions": []})
    try:
        client = _get_memory_client()
        resp = client.list_sessions(
            memoryId=AGENTCORE_MEMORY_ID, actorId=actor_id, maxResults=50
        )
        raw = resp.get("sessionSummaries", resp.get("sessions", []))
        sessions = []
        for s in raw:
            sid = s.get("sessionId", "")
            preview = ""
            try:
                # Pull a few more events than strictly needed so we can scan
                # both the <session-title> marker (set once per session after
                # the first assistant turn, at the START of the event list)
                # and the first user message as a fallback for sessions that
                # haven't been titled yet.
                events = client.list_events(
                    memoryId=AGENTCORE_MEMORY_ID,
                    actorId=actor_id,
                    sessionId=sid,
                    includePayloads=True,
                    maxResults=20,
                ).get("events", [])
                if not events:
                    continue
                # Prefer a Haiku-generated `<session-title>` if the session
                # has one. Scan in natural order (oldest first) — the title
                # is always written near the start of the session.
                title_found = ""
                for ev in events:
                    for item in ev.get("payload", []):
                        conv = item.get("conversational", {})
                        text = (conv.get("content") or {}).get("text", "") or ""
                        if "<session-title>" in text:
                            start = text.find("<session-title>") + len(
                                "<session-title>"
                            )
                            end = text.find("</session-title>", start)
                            if end > start:
                                candidate = text[start:end].strip()
                                if candidate:
                                    title_found = candidate
                                    break
                    if title_found:
                        break
                if title_found:
                    preview = title_found
                else:
                    # Fallback: first user message. Sessions pre-dating the
                    # title feature or where Haiku generation failed.
                    events.reverse()
                    for ev in events:
                        for item in ev.get("payload", []):
                            conv = item.get("conversational", {})
                            if conv.get("role") == "USER":
                                text = conv.get("content", {}).get("text", "")
                                # Strip the frontend-only report-mode marker so
                                # it never shows up in the sidebar preview.
                                text = _REPORT_REQUEST_RE.sub("", text).strip()
                                preview = text[:50] + (
                                    "..." if len(text) > 50 else ""
                                )
                                break
                        if preview:
                            break
            except Exception:
                pass
            created_at = s.get("createdAt", "")
            if hasattr(created_at, "isoformat"):
                created_at = created_at.isoformat()
            sessions.append(
                {
                    "session_id": sid,
                    "created_at": created_at,
                    "message_count": s.get("eventCount", 0),
                    "preview": preview or sid[:12] + "...",
                }
            )
        return _response(200, {"sessions": sessions})
    except Exception as exc:
        if "not found" in str(exc).lower():
            return _response(200, {"sessions": []})
        return _response(500, {"error": str(exc)})


def _get_session_history(session_id: str, actor_id: str) -> dict:
    if not AGENTCORE_MEMORY_ID:
        return _response(200, {"messages": []})
    try:
        client = _get_memory_client()
        all_events = []
        next_token = None
        while True:
            params = {
                "memoryId": AGENTCORE_MEMORY_ID,
                "actorId": actor_id,
                "sessionId": session_id,
                "includePayloads": True,
                "maxResults": 100,
            }
            if next_token:
                params["nextToken"] = next_token
            resp = client.list_events(**params)
            all_events.extend(resp.get("events", []))
            next_token = resp.get("nextToken")
            if not next_token:
                break
        all_events.reverse()

        # Collect tool results
        tool_results_map = {}
        for event in all_events:
            for item in event.get("payload", []):
                conv = item.get("conversational", {})
                if not conv:
                    continue
                content = conv.get("content", {})
                text = (
                    content.get("text", "")
                    if isinstance(content, dict)
                    else str(content)
                )
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict) and "message" in parsed:
                        for b in parsed["message"].get("content", []):
                            if isinstance(b, dict) and "toolResult" in b:
                                tr = b["toolResult"]
                                tid = tr.get("toolUseId", "")
                                c = tr.get("content", "")
                                if isinstance(c, list):
                                    c = " ".join(
                                        str(x.get("text", x))
                                        for x in c
                                        if isinstance(x, dict)
                                    )
                                elif not isinstance(c, str):
                                    c = str(c)
                                tool_results_map[tid] = c[:2000]
                except (json.JSONDecodeError, TypeError):
                    pass

        # Build messages
        messages = []
        for event in all_events:
            for item in event.get("payload", []):
                conv = item.get("conversational", {})
                if not conv or conv.get("role") == "TOOL":
                    continue
                content = conv.get("content", {})
                text = (
                    content.get("text", "")
                    if isinstance(content, dict)
                    else str(content)
                )
                role = "assistant" if conv.get("role") == "ASSISTANT" else "user"
                # Report-request badge marker lives on user turns. Detect +
                # strip before any downstream parsing so it never reaches the
                # content the UI renders.
                is_report = False
                if role == "user" and "<report-request/>" in text:
                    is_report = True
                    text = _REPORT_REQUEST_RE.sub("", text)
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict) and "message" in parsed:
                        blocks = parsed["message"].get("content", [])
                        text_parts = []
                        tool_invocations = []
                        for b in blocks:
                            if not isinstance(b, dict):
                                continue
                            if "text" in b:
                                text_parts.append(b["text"])
                            elif "toolUse" in b:
                                tu = b["toolUse"]
                                tid = tu.get("toolUseId", "")
                                raw_name = tu.get("name", "unknown")
                                server, tool = (
                                    raw_name.split("___", 1)
                                    if "___" in raw_name
                                    else ("mcp", raw_name)
                                )
                                tool_invocations.append(
                                    {
                                        "tool_name": tool,
                                        "server_name": server,
                                        "parameters": tu.get("input", {}),
                                        "result": tool_results_map.get(tid, ""),
                                    }
                                )
                        text = "\n".join(text_parts) if text_parts else ""
                        if role == "assistant":
                            text = _SESSION_TITLE_RE.sub("", text).strip()
                        if role == "user" and not text.strip():
                            continue
                        if (
                            role == "assistant"
                            and not text.strip()
                            and not tool_invocations
                        ):
                            continue
                        msg = {"role": role, "content": text}
                        if tool_invocations:
                            msg["tool_invocations"] = tool_invocations
                        if is_report:
                            msg["is_report"] = True
                        messages.append(msg)
                        continue
                except (json.JSONDecodeError, TypeError):
                    pass
                if role == "assistant":
                    text = _SESSION_TITLE_RE.sub("", text).strip()
                if not text.strip():
                    continue
                msg = {"role": role, "content": text}
                if is_report:
                    msg["is_report"] = True
                messages.append(msg)
        return _response(200, {"messages": messages})
    except Exception as exc:
        return _response(500, {"error": str(exc)})


def _delete_session(session_id: str, actor_id: str) -> dict:
    if not AGENTCORE_MEMORY_ID:
        return _response(400, {"error": "Memory not configured"})
    try:
        client = _get_memory_client()
        all_events = []
        next_token = None
        while True:
            params = {
                "memoryId": AGENTCORE_MEMORY_ID,
                "actorId": actor_id,
                "sessionId": session_id,
                "maxResults": 100,
            }
            if next_token:
                params["nextToken"] = next_token
            resp = client.list_events(**params)
            all_events.extend(resp.get("events", []))
            next_token = resp.get("nextToken")
            if not next_token:
                break
        for event in all_events:
            try:
                client.delete_event(
                    memoryId=AGENTCORE_MEMORY_ID,
                    actorId=actor_id,
                    sessionId=session_id,
                    eventId=event["eventId"],
                )
            except Exception:
                pass
        return _response(200, {"status": "deleted", "events_deleted": len(all_events)})
    except Exception as exc:
        return _response(500, {"error": str(exc)})


# =============================================================================
# Template handlers
# =============================================================================


def _parse_template_item(item: dict) -> dict:
    result = {
        "template_id": item.get("templateId", {}).get("S", ""),
        "user_id": item.get("userId", {}).get("S", ""),
        "name": item.get("name", {}).get("S", ""),
        "description": item.get("description", {}).get("S", ""),
        "created_at": item.get("createdAt", {}).get("S", ""),
        "updated_at": item.get("updatedAt", {}).get("S", ""),
    }
    if "sections" in item:
        try:
            result["sections"] = json.loads(item["sections"].get("S", "[]"))
        except (json.JSONDecodeError, TypeError):
            result["sections"] = []
    if "dependencies" in item:
        try:
            result["dependencies"] = json.loads(item["dependencies"].get("S", "{}"))
        except (json.JSONDecodeError, TypeError):
            result["dependencies"] = {}
    return result


def _load_builtin_templates() -> list:
    """Load built-in templates bundled with the Lambda."""
    import glob

    templates = []
    template_dir = os.path.join(os.path.dirname(__file__), "report_templates")
    if not os.path.isdir(template_dir):
        return templates
    for path in glob.glob(os.path.join(template_dir, "*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            template_id = os.path.splitext(os.path.basename(path))[0]
            templates.append(
                {
                    "template_id": template_id,
                    "user_id": "system",
                    "name": data.get("name", template_id),
                    "description": data.get("description", ""),
                    "sections": data.get("sections", []),
                    "dependencies": data.get("dependencies", {}),
                    "created_at": "",
                    "updated_at": "",
                }
            )
        except Exception as exc:
            logger.warning("Failed to load template %s: %s", path, exc)
    return templates


def _list_templates(actor_id: str) -> dict:
    templates = _load_builtin_templates()
    if REPORT_TABLE_NAME:
        ddb = _get_ddb()
        # User templates
        try:
            resp = ddb.query(
                TableName=REPORT_TABLE_NAME,
                KeyConditionExpression="userId = :uid",
                ExpressionAttributeValues={":uid": {"S": actor_id}},
            )
            for item in resp.get("Items", []):
                templates.append(_parse_template_item(item))
        except Exception as exc:
            logger.error("list user templates failed: %s", exc)
        # System templates from DynamoDB (if any were created via API)
        try:
            resp = ddb.query(
                TableName=REPORT_TABLE_NAME,
                KeyConditionExpression="userId = :uid",
                ExpressionAttributeValues={":uid": {"S": "system"}},
            )
            for item in resp.get("Items", []):
                templates.append(_parse_template_item(item))
        except Exception as exc:
            logger.error("list system templates failed: %s", exc)
    templates.sort(key=lambda t: t.get("name", "").lower())
    # Deduplicate by template_id (built-in templates may also exist in DynamoDB)
    seen = set()
    unique = []
    for t in templates:
        tid = t.get("template_id", "")
        if tid not in seen:
            seen.add(tid)
            unique.append(t)
    return _response(200, {"templates": unique})


def _create_template(actor_id: str, body: dict) -> dict:
    if not REPORT_TABLE_NAME:
        return _response(400, {"error": "REPORT_TABLE_NAME not configured"})
    sections = body.get("sections", [])
    if not sections:
        return _response(400, {"error": "sections is required and must be non-empty"})
    ddb = _get_ddb()
    template_id = f"tmpl_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()
    item = {
        "userId": {"S": actor_id},
        "templateId": {"S": template_id},
        "name": {"S": body.get("name", "Untitled")},
        "description": {"S": body.get("description", "")},
        "sections": {"S": json.dumps(sections)},
        "dependencies": {"S": json.dumps(body.get("dependencies", {}))},
        "createdAt": {"S": now},
        "updatedAt": {"S": now},
    }
    ddb.put_item(TableName=REPORT_TABLE_NAME, Item=item)
    return _response(201, {"template_id": template_id})


def _update_template(actor_id: str, template_id: str, body: dict) -> dict:
    if not REPORT_TABLE_NAME:
        return _response(400, {"error": "REPORT_TABLE_NAME not configured"})
    if not template_id:
        return _response(400, {"error": "template_id is required"})
    ddb = _get_ddb()
    now = datetime.now(timezone.utc).isoformat()
    ddb.update_item(
        TableName=REPORT_TABLE_NAME,
        Key={"userId": {"S": actor_id}, "templateId": {"S": template_id}},
        UpdateExpression="SET #n = :n, description = :d, sections = :s, dependencies = :dep, updatedAt = :u",
        ExpressionAttributeNames={"#n": "name"},
        ExpressionAttributeValues={
            ":n": {"S": body.get("name", "")},
            ":d": {"S": body.get("description", "")},
            ":s": {"S": json.dumps(body.get("sections", []))},
            ":dep": {"S": json.dumps(body.get("dependencies", {}))},
            ":u": {"S": now},
        },
    )
    return _response(200, {"status": "updated"})


def _delete_template(actor_id: str, template_id: str) -> dict:
    if not REPORT_TABLE_NAME:
        return _response(400, {"error": "REPORT_TABLE_NAME not configured"})
    ddb = _get_ddb()
    ddb.delete_item(
        TableName=REPORT_TABLE_NAME,
        Key={"userId": {"S": actor_id}, "templateId": {"S": template_id}},
    )
    return _response(200, {"status": "deleted"})


# =============================================================================
# Report handlers (read-only — report generation stays in the supervisor)
# =============================================================================


def _parse_report_item(item: dict) -> dict:
    sections = []
    for s in item.get("sections", {}).get("L", []):
        m = s.get("M", {})
        traces_str = m.get("traces", {}).get("S", "")
        try:
            traces = json.loads(traces_str) if traces_str else []
        except (json.JSONDecodeError, TypeError):
            traces = []
        sections.append(
            {
                "id": m.get("id", {}).get("S", ""),
                "title": m.get("title", {}).get("S", ""),
                "status": m.get("status", {}).get("S", "pending"),
                "content": m.get("content", {}).get("S", ""),
                "error": m.get("error", {}).get("S", ""),
                "generated_at": m.get("generated_at", {}).get("S", ""),
                "traces": traces,
            }
        )
    raw_uid = item.get("userId", {}).get("S", "")
    actual_uid = (
        raw_uid.replace("report:", "", 1) if raw_uid.startswith("report:") else raw_uid
    )
    return {
        "report_id": item.get("templateId", {}).get("S", ""),
        "user_id": actual_uid,
        "title": item.get("title", {}).get("S", ""),
        "status": item.get("status", {}).get("S", ""),
        "month": item.get("month", {}).get("S", ""),
        "year": item.get("year", {}).get("S", ""),
        "created_at": item.get("createdAt", {}).get("S", ""),
        "updated_at": item.get("updatedAt", {}).get("S", ""),
        "sections": sections,
        "current_section": int(item.get("currentSection", {}).get("N", "0")),
        "total_sections": int(item.get("totalSections", {}).get("N", "0")),
        "parent_report_id": item.get("parentReportId", {}).get("S", ""),
        "version": int(item.get("version", {}).get("N", "1") or 1),
        "edit_prompt": item.get("editPrompt", {}).get("S", ""),
    }


def _list_reports(actor_id: str) -> dict:
    """List reports, collapsing edit lineages into a single row.

    Each row carries the LATEST version's identity plus ``version_count``
    and ``versions`` (a list of prior report_ids) so the frontend can
    render a version-history chevron without a second round trip. Root
    reports are self-rooted; edits point at their immediate parent via
    ``parent_report_id``. To find the root we follow the chain until
    ``parent_report_id`` is empty.
    """
    if not REPORT_TABLE_NAME:
        return _response(200, {"reports": []})
    ddb = _get_ddb()
    try:
        resp = ddb.query(
            TableName=REPORT_TABLE_NAME,
            KeyConditionExpression="userId = :uid",
            ExpressionAttributeValues={":uid": {"S": f"report:{actor_id}"}},
        )
        raw = [_parse_report_item(item) for item in resp.get("Items", [])]
        by_id = {r["report_id"]: r for r in raw}

        def _root_id(report_id: str) -> str:
            seen: set[str] = set()
            cur = report_id
            while cur in by_id:
                if cur in seen:
                    break
                seen.add(cur)
                parent = by_id[cur].get("parent_report_id", "")
                if not parent:
                    return cur
                cur = parent
            return cur

        lineages: dict[str, list[dict]] = {}
        for r in raw:
            lineages.setdefault(_root_id(r["report_id"]), []).append(r)

        reports = []
        for root_id, versions in lineages.items():
            versions.sort(key=lambda x: x.get("version", 1))
            latest = versions[-1]
            reports.append(
                {
                    "report_id": latest["report_id"],
                    "title": latest["title"],
                    "status": latest["status"],
                    "month": latest["month"],
                    "year": latest["year"],
                    "created_at": latest["created_at"],
                    "version": latest.get("version", 1),
                    "version_count": len(versions),
                    "root_report_id": root_id,
                    "versions": [
                        {
                            "report_id": v["report_id"],
                            "version": v.get("version", 1),
                            "created_at": v.get("created_at", ""),
                            "edit_prompt": v.get("edit_prompt", ""),
                        }
                        for v in versions
                    ],
                }
            )
        reports.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return _response(200, {"reports": reports})
    except Exception as exc:
        return _response(500, {"error": str(exc)})


def _get_report(actor_id: str, report_id: str) -> dict:
    if not REPORT_TABLE_NAME:
        return _response(400, {"error": "REPORT_TABLE_NAME not configured"})
    ddb = _get_ddb()
    try:
        resp = ddb.get_item(
            TableName=REPORT_TABLE_NAME,
            Key={"userId": {"S": f"report:{actor_id}"}, "templateId": {"S": report_id}},
        )
        item = resp.get("Item")
        if not item:
            return _response(404, {"error": "Report not found"})
        return _response(200, _parse_report_item(item))
    except Exception as exc:
        return _response(500, {"error": str(exc)})


def _get_report_status(actor_id: str, report_id: str) -> dict:
    if not REPORT_TABLE_NAME:
        return _response(400, {"error": "REPORT_TABLE_NAME not configured"})
    ddb = _get_ddb()
    try:
        resp = ddb.get_item(
            TableName=REPORT_TABLE_NAME,
            Key={"userId": {"S": f"report:{actor_id}"}, "templateId": {"S": report_id}},
        )
        item = resp.get("Item")
        if not item:
            return _response(404, {"error": "Report not found"})
        r = _parse_report_item(item)
        return _response(
            200,
            {
                "report_id": r["report_id"],
                "status": r["status"],
                "current_section": r["current_section"],
                "total_sections": r["total_sections"],
                "title": r["title"],
                "updated_at": r["updated_at"],
                "sections": [
                    {"id": s["id"], "title": s["title"], "status": s["status"]}
                    for s in r["sections"]
                ],
            },
        )
    except Exception as exc:
        return _response(500, {"error": str(exc)})


def _delete_report(actor_id: str, report_id: str) -> dict:
    if not REPORT_TABLE_NAME:
        return _response(400, {"error": "REPORT_TABLE_NAME not configured"})
    ddb = _get_ddb()
    ddb.delete_item(
        TableName=REPORT_TABLE_NAME,
        Key={"userId": {"S": f"report:{actor_id}"}, "templateId": {"S": report_id}},
    )
    return _response(200, {"status": "deleted"})


# =============================================================================
# Thread activity handler
# =============================================================================
#
# Schema (written by src/agents/shared/thread_activity.py, lives in the same
# report_templates DynamoDB table):
#   userId     = "thread-activity:{actor_id}"
#   templateId = "{thread_id}"
#   status     = "running" | "idle" | "error"
#   currentStep (S) — human-readable current action
#   startedAt / updatedAt (S)
#   runId / reportId / errorMsg (S, optional)

_ACTIVITY_STALE_SECONDS = 10 * 60  # running rows older than this → treat as idle


def _get_thread_activity(actor_id: str, thread_id: str) -> dict:
    if not REPORT_TABLE_NAME:
        return _response(200, {"status": "idle"})
    if not thread_id or not actor_id:
        return _response(400, {"error": "missing thread_id or actor_id"})

    try:
        ddb = _get_ddb()
        resp = ddb.get_item(
            TableName=REPORT_TABLE_NAME,
            Key={
                "userId": {"S": f"thread-activity:{actor_id}"},
                "templateId": {"S": thread_id},
            },
        )
    except Exception as exc:
        logger.warning("get_thread_activity failed: %s", exc)
        return _response(200, {"status": "idle"})

    item = resp.get("Item")
    if not item:
        return _response(200, {"status": "idle"})

    status = item.get("status", {}).get("S", "idle")
    updated_at = item.get("updatedAt", {}).get("S", "")

    if status == "running" and _is_activity_stale(updated_at):
        return _response(200, {"status": "idle", "stale": True})

    return _response(
        200,
        {
            "status": status,
            "current_step": item.get("currentStep", {}).get("S", ""),
            "started_at": item.get("startedAt", {}).get("S", ""),
            "updated_at": updated_at,
            "run_id": item.get("runId", {}).get("S", ""),
            "report_id": item.get("reportId", {}).get("S", ""),
            "error_msg": item.get("errorMsg", {}).get("S", ""),
        },
    )


def _is_activity_stale(iso_ts: str) -> bool:
    if not iso_ts:
        return True
    try:
        ts = datetime.fromisoformat(iso_ts)
    except ValueError:
        return True
    now = datetime.now(timezone.utc)
    return (now - ts).total_seconds() > _ACTIVITY_STALE_SECONDS
