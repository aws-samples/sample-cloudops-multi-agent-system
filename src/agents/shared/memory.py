"""Manual memory management for frontend-facing agents.

Provides load/save functions for conversation history using the AgentCore
Memory API (create_event/list_events). Does NOT use AgentCoreMemorySessionManager
— see project conventions for why.

Usage::

    from agents.shared.memory import load_history, save_user_message, save_assistant_message

    history = load_history(memory_id, session_id, actor_id, region)
    save_user_message(memory_id, session_id, actor_id, prompt, region)
    save_assistant_message(memory_id, session_id, actor_id, enriched_text, region)
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

import boto3

logger = logging.getLogger(__name__)

_memory_client = None


def _get_client(region: str = "us-east-1"):
    global _memory_client
    if _memory_client is None:
        _memory_client = boto3.client("bedrock-agentcore", region_name=region)
    return _memory_client


def load_history(
    memory_id: str, session_id: str, actor_id: str, region: str = "us-east-1"
) -> list[dict]:
    """Load conversation history and convert to Strands message format."""
    if not memory_id or not session_id or not actor_id:
        return []
    try:
        client = _get_client(region)
        all_events: list[dict] = []
        next_token = None
        while True:
            params: dict[str, Any] = {
                "memoryId": memory_id,
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
        messages = []
        for event in all_events:
            for item in event.get("payload", []):
                conv = item.get("conversational", {})
                if not conv:
                    continue
                role_raw = conv.get("role", "")
                content = conv.get("content", {})
                text = (
                    content.get("text", "")
                    if isinstance(content, dict)
                    else str(content)
                )
                if not text.strip():
                    continue
                # Strip frontend-only display tags (report metadata cards,
                # next-turn suggestion chips, sidebar session-title markers).
                # `<tool>` tags are deliberately preserved — they record what
                # the agent did in prior turns (inputs, outputs, mock-scenario
                # markers) and let the model resolve references like "the
                # diagram above" or "that report" without claiming nothing is
                # in its history. See the vocabulary/evidence clause in
                # `_NO_FABRICATION_PREAMBLE`.
                text = re.sub(r"\n?<artifact>.*?</artifact>", "", text, flags=re.DOTALL)
                text = re.sub(
                    r"\n?<suggestions>.*?</suggestions>", "", text, flags=re.DOTALL
                )
                text = re.sub(
                    r"\n?<session-title>.*?</session-title>", "", text, flags=re.DOTALL
                )
                # Frontend-only report-mode marker on user turns — the model
                # must never see it (it's not content, just a UI badge signal).
                text = re.sub(r"\n?<report-request/>", "", text)
                text = text.strip()
                if not text:
                    continue
                role = "assistant" if role_raw == "ASSISTANT" else "user"
                messages.append({"role": role, "content": [{"text": text}]})

        logger.info(
            "Loaded %d history messages for session %s", len(messages), session_id[:20]
        )
        return messages
    except Exception as exc:
        if "not found" not in str(exc).lower():
            logger.warning("Failed to load history: %s", exc)
        return []


def save_user_message(
    memory_id: str,
    session_id: str,
    actor_id: str,
    prompt: str,
    region: str = "us-east-1",
    is_report: bool = False,
) -> None:
    """Save user message to Memory immediately (before streaming starts).

    When ``is_report`` is True, a ``<report-request/>`` sentinel is appended to
    the saved text. It is a FRONTEND-ONLY marker (like ``<artifact>`` etc.):
    ``load_history`` strips it before the model ever sees it, and the browser
    REST path (core-api ``_get_session_history``) turns it into an ``is_report``
    flag it strips from the content, so the UI can badge the user's prompt
    bubble as a report request — and that badge survives a reload.
    """
    if not memory_id or not session_id or not actor_id:
        return
    try:
        text = prompt + "\n<report-request/>" if is_report else prompt
        client = _get_client(region)
        client.create_event(
            memoryId=memory_id,
            actorId=actor_id,
            sessionId=session_id,
            eventTimestamp=datetime.now(timezone.utc),
            payload=[{"conversational": {"content": {"text": text}, "role": "USER"}}],
        )
    except Exception as exc:
        logger.warning("Failed to save user message: %s", exc)


def save_assistant_message(
    memory_id: str,
    session_id: str,
    actor_id: str,
    enriched_text: str,
    region: str = "us-east-1",
) -> None:
    """Save enriched assistant response to Memory (after streaming completes)."""
    if not memory_id or not session_id or not actor_id or not enriched_text.strip():
        return
    try:
        client = _get_client(region)
        client.create_event(
            memoryId=memory_id,
            actorId=actor_id,
            sessionId=session_id,
            eventTimestamp=datetime.now(timezone.utc),
            payload=[
                {
                    "conversational": {
                        "content": {"text": enriched_text},
                        "role": "ASSISTANT",
                    }
                }
            ],
        )
        logger.info("Saved conversation to Memory for session %s", session_id[:20])
    except Exception as exc:
        logger.warning("Failed to save assistant message: %s", exc)


def build_enriched_text(ordered_segments: list[dict]) -> str:
    """Build enriched assistant text with <tool>, <think>, <suggestions> tags."""
    from agents.shared.redact import redact

    enriched_parts: list[str] = []
    text_buffer: list[str] = []
    thinking_buffer: list[str] = []

    def flush_text():
        nonlocal text_buffer
        if text_buffer:
            enriched_parts.append("".join(text_buffer))
            text_buffer = []

    def flush_thinking():
        nonlocal thinking_buffer
        if thinking_buffer:
            enriched_parts.append(f'<think>{"".join(thinking_buffer)}</think>')
            thinking_buffer = []

    for seg in ordered_segments:
        seg_type = seg.get("type", "")
        if seg_type == "text":
            flush_thinking()
            text_buffer.append(seg["value"])
        elif seg_type == "thinking":
            flush_text()
            thinking_buffer.append(seg["value"])
        elif seg_type == "tool":
            flush_text()
            flush_thinking()
            enriched_parts.append(f'<tool>{seg["value"]}</tool>')
        elif seg_type == "suggestions":
            flush_text()
            flush_thinking()
            enriched_parts.append(f'<suggestions>{seg["value"]}</suggestions>')

    flush_text()
    flush_thinking()
    return redact("\n".join(enriched_parts))
