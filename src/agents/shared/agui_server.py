"""Shared AG-UI FastAPI server factory for frontend-facing agents.

Extracts the AG-UI streaming, event tracking, memory save/load,
suggestions injection, and legacy chat fallback into a reusable
``create_agui_app()`` function. Any agent can become a frontend-facing
AG-UI agent by providing an ``agent_builder`` callable and a config dict.

Usage::

    from agents.shared.agui_server import create_agui_app

    def my_agent_builder(payload):
        # Build and return (Agent, system_prompt, cleanup_fn)
        agent = Agent(model=model, tools=tools, system_prompt=prompt)
        return agent, prompt, None

    app = create_agui_app(
        agent_builder=my_agent_builder,
        config={
            "agent_name": "supervisor",
            "agent_description": "CloudOps Supervisor Agent",
            "memory_enabled": True,
            "suggestions_enabled": True,
            "model_id": "global.anthropic.claude-opus-4-6-v1",
        },
    )
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from ag_ui_strands import StrandsAgent
from ag_ui_strands.config import StrandsAgentConfig as AGUIStrandsAgentConfig
from ag_ui.core import (
    EventType,
    RunAgentInput,
    TextMessageContentEvent,
    RunFinishedEvent,
)
from ag_ui.encoder import EventEncoder
from strands import Agent

from agents.shared.guardrail import GuardrailBlocked, check_user_input
from agents.shared.memory import (
    build_enriched_text,
    load_history,
    save_assistant_message,
    save_user_message,
)
from agents.shared.suggestions import generate_suggestions
from agents.shared import reports
from agents.shared.agent_base import (
    _ABBREVIATED_TOOL_OUTPUT,
    _NO_RELOAD_TRACE_TOOLS,
)
from agents.shared.registry import mark_frontend_container
from agents.shared.thread_activity import (
    mark_thread_error,
    mark_thread_idle,
    mark_thread_running,
    update_thread_step,
)

logger = logging.getLogger(__name__)

# This module is imported only by create_frontend_agent (the supervisor).
# Flag the process so registry._delegate routes extracted CFN artifacts to
# the local _pending queue (drained by this module's event loop) rather than
# forwarding them up via the response dict (which is what orchestrators do).
mark_frontend_container()

# Environment-level defaults
AGENTCORE_MEMORY_ID = os.environ.get("AGENTCORE_MEMORY_ID", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
REPORT_TABLE_NAME = os.environ.get("REPORT_TABLE_NAME", "")

# Type alias for the agent builder callable.
# Signature: (payload: dict) -> (Agent, system_prompt: str, cleanup_fn: Optional[Callable])
AgentBuilder = Callable[[dict], tuple[Agent, str, Optional[Callable]]]


def create_agui_app(
    agent_builder: AgentBuilder,
    config: dict[str, Any],
) -> FastAPI:
    """Create a FastAPI app that serves AG-UI protocol.

    Args:
        agent_builder: Callable ``(payload) -> (Agent, system_prompt, cleanup_fn)``.
            Called for every request. The caller is responsible for building the
            Strands Agent with the correct model, tools, system prompt, and
            conversation history. ``cleanup_fn`` (optional) is called in a
            ``finally`` block after streaming completes (e.g. to close gateway
            clients).
        config: Dict controlling behavior:
            - ``agent_name`` (str): Name for the StrandsAgent wrapper.
            - ``agent_description`` (str): Description for the StrandsAgent wrapper.
            - ``memory_enabled`` (bool): Whether to load/save conversation history.
            - ``suggestions_enabled`` (bool): Whether to generate follow-up suggestions.
            - ``model_id`` (str): Bedrock model ID for suggestion generation.

    Report mode is always enabled and triggered purely by the frontend
    sending ``template_id`` or ``edit_report_id`` in ``forwardedProps``.
    Reports are request-scoped — they produce no idle cost and need no
    backend initialization, so there is no capability flag to gate them.

    Returns:
        A FastAPI app with ``/ping`` and ``/invocations`` routes.
    """
    agent_name = config.get("agent_name", "agent")
    agent_description = config.get("agent_description", "AG-UI Agent")
    memory_enabled = config.get("memory_enabled", True)
    suggestions_enabled = config.get("suggestions_enabled", True)
    model_id = config.get("model_id", "")

    memory_id = AGENTCORE_MEMORY_ID
    region = AWS_REGION

    app = FastAPI(title=f"{agent_name} (AG-UI)")

    def ping() -> dict:
        return {"status": "healthy"}

    async def invocations(request: Request):
        """AG-UI chat entrypoint. Also supports legacy {prompt} format."""
        payload = await request.json()

        # Legacy sync chat — {prompt} payload without AG-UI threadId
        if "prompt" in payload and "threadId" not in payload:
            return _handle_legacy_chat(
                payload, agent_builder, memory_enabled, memory_id, region
            )

        # AG-UI chat
        forwarded = payload.get("forwardedProps", {})
        session_id = forwarded.get("session_id", payload.get("threadId", ""))
        actor_id = forwarded.get("actor_id", "")

        # Extract user prompt from AG-UI messages
        user_prompt = _extract_user_prompt(payload)

        accept_header = request.headers.get("accept", "")
        encoder = EventEncoder(accept=accept_header)

        # Guardrail pre-flight: evaluate raw user input BEFORE it enters
        # the agent pipeline. Only the user message is sent to the guardrail
        # classifier — system prompts are never evaluated, eliminating false
        # positives on multi-agent delegation patterns.
        try:
            check_user_input(user_prompt)
        except GuardrailBlocked as e:
            logger.warning("Guardrail blocked user input: %s", e.message)
            # Capture into locals: the `except ... as e` name is deleted when
            # the block exits, but the generator below runs lazily AFTER that —
            # referencing `e` inside it raises NameError. Bind the message and
            # IDs to plain locals the closure can safely capture. AG-UI sends
            # snake_case `thread_id`/`run_id` (matching RunAgentInput), not the
            # camelCase forms.
            blocked_message = e.message
            thread_id = payload.get("thread_id", payload.get("threadId", "default"))
            run_id = payload.get("run_id", payload.get("runId", ""))

            def _guardrail_blocked_gen():
                yield encoder.encode(
                    TextMessageContentEvent(
                        type=EventType.TEXT_MESSAGE_CONTENT,
                        message_id="guardrail-blocked",
                        delta=blocked_message,
                    )
                )
                yield encoder.encode(
                    RunFinishedEvent(
                        type=EventType.RUN_FINISHED,
                        threadId=thread_id,
                        runId=run_id,
                    )
                )

            return StreamingResponse(
                _guardrail_blocked_gen(),
                media_type=encoder.get_content_type(),
            )

        # Report edit mode — when edit_report_id is set, regenerate from
        # the existing report + user prompt and save as a new version.
        edit_report_id = forwarded.get("edit_report_id", "")
        if edit_report_id:
            return _handle_report_edit(
                payload=payload,
                parent_report_id=edit_report_id,
                session_id=session_id,
                actor_id=actor_id,
                user_prompt=user_prompt,
                agent_builder=agent_builder,
                encoder=encoder,
                memory_enabled=memory_enabled,
                memory_id=memory_id,
                region=region,
            )

        # Report generation mode — when template_id is present in forwardedProps
        template_id = forwarded.get("template_id", "")
        if template_id:
            return _handle_report_streaming(
                payload=payload,
                template_id=template_id,
                session_id=session_id,
                actor_id=actor_id,
                user_prompt=user_prompt,
                agent_builder=agent_builder,
                encoder=encoder,
                memory_enabled=memory_enabled,
                memory_id=memory_id,
                region=region,
            )

        # Free-form report mode — when the user hit the report-mode toggle
        # without picking a template. We build a synthetic single-section
        # template from the user's prompt and route through the same async
        # path as templated reports, so persistence (DynamoDB row +
        # `<report-pending>` marker), reload behaviour, edit lineage, and
        # `get_report` follow-ups all work uniformly.
        if forwarded.get("chat_mode") == "report":
            synthetic_template = _build_freeform_template(user_prompt)
            return _handle_report_streaming(
                payload=payload,
                template_id="freeform",
                session_id=session_id,
                actor_id=actor_id,
                user_prompt=user_prompt,
                agent_builder=agent_builder,
                encoder=encoder,
                memory_enabled=memory_enabled,
                memory_id=memory_id,
                region=region,
                template_override=synthetic_template,
            )

        # Build the agent for this request
        strands_agent, system_prompt, cleanup_fn = agent_builder(payload)

        # `replay_history_into_strands` (default True since ag-ui-strands
        # 0.1.4) reconciles the cached agent's `self.messages` with the
        # AG-UI protocol-level `RunAgentInput.messages` right before
        # invoking the model — overwriting whatever the agent_builder
        # injected. Our frontend deliberately sends only the latest user
        # message in the AG-UI payload (history is rebuilt server-side
        # from AgentCore Memory in `agent_builder` via `load_history`),
        # so leaving this on would clobber our 4–6 message history with
        # a 1-message reconciliation every turn — the LLM then "doesn't
        # see" prior `<report-pending>` markers and refuses follow-ups
        # about reports it actually generated.
        # Disabling this puts us back on the legacy `stream_async(user_msg)`
        # path which APPENDS the new user message to `self.messages` —
        # which is exactly what we want, given that we own history.
        agui_agent = StrandsAgent(
            agent=strands_agent,
            name=agent_name,
            description=agent_description,
            config=AGUIStrandsAgentConfig(replay_history_into_strands=False),
        )

        # WORKAROUND: StrandsAgent creates its own internal agents per thread,
        # ignoring messages/session_manager from the passed agent. Inject our
        # agent (with history messages) directly into the per-thread cache.
        thread_id = payload.get("threadId", "default")
        agui_agent._agents_by_thread[thread_id] = strands_agent

        # Save user message to Memory BEFORE streaming
        if memory_enabled:
            save_user_message(memory_id, session_id, actor_id, user_prompt, region)

        # Mark thread busy so other tabs see "still running" when they poll
        run_id = payload.get("runId", "")
        mark_thread_running(thread_id, actor_id, "Processing your request…", run_id)

        # Track tool calls, text, and reasoning during streaming for enriched save
        ordered_segments: list[dict] = []
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_starts: dict[str, dict] = {}
        buffered_run_finished = None
        # `<cfn-artifact>` interceptor — see comment block at the bottom of
        # this module. No-op for any agent that doesn't emit the tag.
        cfn_interceptor = _CfnArtifactInterceptor()
        # Strips any `<report-pending>` marker the MODEL types (it sometimes
        # mimics the platform-injected markers in its history with a
        # fabricated id). Runs only on model-origin text — the platform's
        # own injected markers are constructed separately and bypass it.
        report_pending_stripper = _ReportPendingStripper()
        # Request-scoped dedup of CFN artifacts. Two delivery paths can fire
        # for the SAME artifact in one turn: (1) the _delegate extraction
        # path (TOOL_CALL_RESULT → _handle_cfn_artifacts_from_tool_result),
        # and (2) the streaming interceptor catching a <cfn-artifact> the
        # supervisor model re-emits in its text reply (the supervisor prompt
        # tells it to pass the tag through verbatim). Without dedup, both
        # persist a report row and emit a marker — two ReportCards (one
        # orphaned) per generation. Keyed by artifact title; first writer
        # wins, the second path skips.
        persisted_cfn_titles: set[str] = set()

        async def event_generator():
            nonlocal buffered_run_finished
            try:
                run_input = RunAgentInput(**payload)
                async for event in agui_agent.run(run_input):
                    encoded = encoder.encode(event)

                    # Check if this is RUN_FINISHED — buffer it to inject suggestions
                    is_run_finished = False
                    is_text_message = False
                    if hasattr(event, "type"):
                        etype = (
                            str(event.type)
                            if hasattr(event.type, "value")
                            else str(getattr(event, "type", ""))
                        )
                        if "RUN_FINISHED" in etype:
                            buffered_run_finished = encoded
                            is_run_finished = True
                        elif "TEXT_MESSAGE_CONTENT" in etype:
                            is_text_message = True
                        elif "TOOL_CALL_START" in etype:
                            tc_name = getattr(
                                event, "tool_call_name", ""
                            ) or getattr(event, "toolCallName", "")
                            if tc_name:
                                update_thread_step(
                                    thread_id,
                                    actor_id,
                                    _humanize_tool_step(tc_name),
                                )

                    # TEXT_MESSAGE_CONTENT: route through the cfn-artifact
                    # interceptor. Non-text events pass through untouched.
                    if is_text_message:
                        delta = getattr(event, "delta", "")
                        message_id = getattr(event, "message_id", "") or getattr(
                            event, "messageId", ""
                        )
                        forwards, completed = cfn_interceptor.feed(delta)

                        # Forward any text outside the artifact block — and
                        # track it into the Memory enriched-save state. Strip
                        # any model-typed <report-pending> marker first (the
                        # only valid markers are platform-injected below).
                        for chunk in forwards:
                            chunk = report_pending_stripper.feed(chunk)
                            if not chunk:
                                continue
                            forward_event = TextMessageContentEvent(
                                type=EventType.TEXT_MESSAGE_CONTENT,
                                message_id=message_id or "assistant-text",
                                delta=chunk,
                            )
                            yield encoder.encode(forward_event)
                            text_parts.append(chunk)
                            ordered_segments.append({"type": "text", "value": chunk})

                        # For each completed <cfn-artifact>: persist to
                        # the reports table, then emit a synthetic
                        # <report-pending> delta in its place.
                        for title, yaml_text in completed:
                            # Skip if the primary (_delegate) path already
                            # persisted this artifact this turn.
                            dedup_key = title or "CloudFormation alarm template"
                            if dedup_key in persisted_cfn_titles:
                                continue
                            report_id = _new_cfn_artifact_report_id()
                            persisted = False
                            try:
                                persisted = _persist_cfn_artifact_as_report(
                                    report_id=report_id,
                                    title=title,
                                    template_yaml=yaml_text,
                                    actor_id=actor_id,
                                    report_table=REPORT_TABLE_NAME,
                                    region=region,
                                )
                            except Exception as exc:
                                logger.error(
                                    "cfn-artifact persistence failed: %s",
                                    exc,
                                    exc_info=True,
                                )
                            if persisted:
                                persisted_cfn_titles.add(dedup_key)
                                # Use the same shape _handle_report_streaming
                                # emits for templated reports — frontend's
                                # MyRuntimeProvider already detects this
                                # delta and renders a polling ReportCard.
                                pending_tag = (
                                    f'<report-pending report_id="{report_id}" '
                                    f'title="{title or "CloudFormation alarm template"}"/>'
                                )
                                pending_event = TextMessageContentEvent(
                                    type=EventType.TEXT_MESSAGE_CONTENT,
                                    message_id=f"report-pending-{report_id}",
                                    delta=pending_tag,
                                )
                                yield encoder.encode(pending_event)
                                text_parts.append(pending_tag)
                                ordered_segments.append(
                                    {"type": "text", "value": pending_tag}
                                )
                            else:
                                # Persistence failed: fall back to streaming
                                # the YAML as plain text so the user at
                                # least sees the output, even though the
                                # artifact panel won't render it.
                                fallback = (
                                    f"\n```yaml\n{yaml_text}\n```\n"
                                )
                                fallback_event = TextMessageContentEvent(
                                    type=EventType.TEXT_MESSAGE_CONTENT,
                                    message_id=message_id or "cfn-artifact-fallback",
                                    delta=fallback,
                                )
                                yield encoder.encode(fallback_event)
                                text_parts.append(fallback)
                                ordered_segments.append(
                                    {"type": "text", "value": fallback}
                                )
                        # Skip the default track+yield for this event —
                        # the interceptor handled everything above.
                        continue

                    # Track AG-UI events for enriched memory save
                    if hasattr(event, "type") and not is_run_finished:
                        _track_event(
                            event,
                            ordered_segments,
                            text_parts,
                            thinking_parts,
                            tool_starts,
                        )

                    # TOOL_CALL_RESULT: check for _cfn_artifacts extracted
                    # by _delegate. Persist each to the reports table and
                    # emit <report-pending> markers so the frontend renders
                    # a ReportCard immediately (before the model's text
                    # reply arrives).
                    if hasattr(event, "type") and not is_run_finished:
                        etype_check = (
                            str(event.type)
                            if hasattr(event.type, "value")
                            else str(getattr(event, "type", ""))
                        )
                        if "TOOL_CALL_RESULT" in etype_check:
                            cfn_events = _handle_cfn_artifacts_from_tool_result(
                                event,
                                actor_id,
                                REPORT_TABLE_NAME,
                                region,
                                persisted_cfn_titles,
                            )
                            for pending_event in cfn_events:
                                yield encoder.encode(pending_event)
                                pending_delta = getattr(pending_event, "delta", "")
                                text_parts.append(pending_delta)
                                ordered_segments.append(
                                    {"type": "text", "value": pending_delta}
                                )

                    if not is_run_finished:
                        yield encoded

                # Stream ended — flush any text the interceptor was holding
                # (e.g. an unterminated <cfn-artifact> block, or trailing
                # text after a partial open-tag prefix). Run the held cfn
                # text back through the report-pending stripper too, then
                # release whatever the stripper itself was holding.
                for chunk in cfn_interceptor.flush():
                    chunk = report_pending_stripper.feed(chunk)
                    if not chunk:
                        continue
                    flush_event = TextMessageContentEvent(
                        type=EventType.TEXT_MESSAGE_CONTENT,
                        message_id="cfn-artifact-flush",
                        delta=chunk,
                    )
                    yield encoder.encode(flush_event)
                    text_parts.append(chunk)
                    ordered_segments.append({"type": "text", "value": chunk})
                stripper_tail = report_pending_stripper.flush()
                if stripper_tail:
                    tail_event = TextMessageContentEvent(
                        type=EventType.TEXT_MESSAGE_CONTENT,
                        message_id="assistant-text-flush",
                        delta=stripper_tail,
                    )
                    yield encoder.encode(tail_event)
                    text_parts.append(stripper_tail)
                    ordered_segments.append(
                        {"type": "text", "value": stripper_tail}
                    )
            except Exception as exc:
                logger.error("AG-UI chat failed: %s", exc, exc_info=True)
                mark_thread_error(thread_id, actor_id, str(exc))
            finally:
                try:
                    # Generate suggestions and emit before RUN_FINISHED
                    if suggestions_enabled:
                        response_text = "".join(text_parts)
                        suggestions = generate_suggestions(
                            model_id, user_prompt, response_text
                        )
                        if suggestions:
                            ordered_segments.append(
                                {
                                    "type": "suggestions",
                                    "value": json.dumps(suggestions),
                                }
                            )
                            suggestions_tag = f"\n<suggestions>{json.dumps(suggestions)}</suggestions>"

                            suggestions_event = TextMessageContentEvent(
                                type=EventType.TEXT_MESSAGE_CONTENT,
                                message_id="suggestions",
                                delta=suggestions_tag,
                            )
                            yield encoder.encode(suggestions_event)

                    # Emit the buffered RUN_FINISHED
                    if buffered_run_finished:
                        yield buffered_run_finished

                    # Save enriched assistant response to Memory
                    if memory_enabled:
                        _save_enriched_response(
                            memory_id,
                            session_id,
                            actor_id,
                            ordered_segments,
                            text_parts,
                            region,
                        )
                        # Best-effort: on the first assistant turn of this
                        # session, ask Haiku for a short summary title and
                        # save it as a tagged memory event. `maybe_generate_*`
                        # is idempotent (checks for an existing title first)
                        # and fail-safe (never raises — the sidebar falls
                        # back to the first-user-msg preview on any error).
                        try:
                            from agents.shared.session_title import (
                                maybe_generate_and_save_title,
                            )
                            maybe_generate_and_save_title(
                                memory_id=memory_id,
                                session_id=session_id,
                                actor_id=actor_id,
                                user_prompt=user_prompt,
                                assistant_text="".join(text_parts),
                                region=region,
                            )
                        except Exception as exc:
                            logger.warning(
                                "session-title generation failed: %s", exc
                            )
                finally:
                    mark_thread_idle(thread_id, actor_id)
                    # Clean up resources (e.g. gateway clients)
                    if cleanup_fn:
                        try:
                            cleanup_fn()
                        except Exception:
                            pass

        return StreamingResponse(
            event_generator(),
            media_type=encoder.get_content_type(),
        )

    app.add_api_route("/ping", ping, methods=["GET"])
    app.add_api_route("/invocations", invocations, methods=["POST"])

    return app


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _humanize_tool_step(tool_name: str) -> str:
    """Turn a tool name into something nicer for the busy indicator."""
    if not tool_name:
        return "Working…"
    if tool_name == "_delegate":
        return "Delegating to a sub-agent…"
    pretty = tool_name.replace("_", " ").replace("-", " ")
    return f"Calling {pretty}…"


def _extract_user_prompt(payload: dict) -> str:
    """Extract the last user message text from AG-UI payload."""
    if not payload.get("messages"):
        return ""
    for msg in reversed(payload["messages"]):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                return " ".join(
                    c.get("text", "") if isinstance(c, dict) else str(c)
                    for c in content
                )
            return content
    return ""


def _extract_variables(user_prompt: str) -> dict[str, str]:
    """Extract key:value variables from user prompt text.

    Looks for patterns like ``month: February, year: 2026``.
    Falls back to current month/year if nothing is found.
    """
    variables: dict[str, str] = {}
    for match in re.finditer(r"(\w+):\s*([^,]+)", user_prompt):
        key = match.group(1).strip().lower()
        value = match.group(2).strip()
        variables[key] = value

    if not variables:
        now = datetime.now(timezone.utc)
        variables = {
            "month": now.strftime("%B"),
            "year": str(now.year),
        }

    return variables


# Appended to every freeform report section prompt so the model always opens
# with a single descriptive H1 title line. The leading heading is then lifted
# into the report title (see the freeform title-derivation block in
# `_run_report_async`), guaranteeing the sidebar entry and ReportCard get a
# clean, report-specific name instead of falling back to the raw user prompt.
# No curly braces — this string is passed through `str.format_map` in reports.py.
_FREEFORM_TITLE_INSTRUCTION = (
    "\n\nBegin the report with a single Markdown H1 title line "
    "(a leading '# ' followed by a concise, descriptive title of the report's "
    "subject) before any other content. Do not prefix it with 'Report:' or "
    "restate the request verbatim — write the title as a standalone headline."
)


def _build_freeform_template(user_prompt: str) -> dict:
    """Build a synthetic single-section template from a user's free-form prompt.

    Free-form report mode (composer toggle, no template picker) reuses the
    templated report flow so persistence / reload / edit / `get_report`
    behave uniformly. The user's prompt becomes the section's prompt (with a
    title-heading instruction appended, see ``_FREEFORM_TITLE_INSTRUCTION``);
    the report title is seeded from the first line of the prompt and then
    upgraded to the report's own leading Markdown heading once generated (so
    the sidebar entry and ReportCard show a clean, report-specific name).
    """
    first_line = user_prompt.strip().splitlines()[0] if user_prompt.strip() else ""
    title_seed = re.sub(r"\s+", " ", first_line).rstrip(".!?,").strip()
    if len(title_seed) > 80:
        title_seed = title_seed[:77].rstrip() + "..."
    name = title_seed or "Custom Report"
    return {
        "name": name,
        "description": "User-generated free-form report",
        "sections": [
            {
                "id": "report",
                "title": "Analysis",
                "prompt": user_prompt + _FREEFORM_TITLE_INSTRUCTION,
            }
        ],
        "dependencies": {},
    }


def _handle_report_streaming(
    *,
    payload: dict,
    template_id: str,
    session_id: str,
    actor_id: str,
    user_prompt: str,
    agent_builder: AgentBuilder,
    encoder: EventEncoder,
    memory_enabled: bool,
    memory_id: str,
    region: str,
    template_override: dict | None = None,
) -> StreamingResponse:
    """Kick off report generation asynchronously.

    Creates the report row with ``status='pending'`` in DynamoDB, spawns a
    background thread to run the sections, and returns a short SSE with a
    ``<report-pending report_id="...">`` marker + ``RUN_FINISHED``. The
    frontend polls ``GET /reports/{id}/status`` for progress rather than
    following a long-lived SSE stream.

    Rationale: report generation can take minutes; keeping it on the
    synchronous AG-UI path means (a) AG-UI timeouts can interrupt
    generation, (b) losing the browser stream loses all progress because
    nothing was persisted mid-flight. Splitting to async lets per-section
    ``update_report_section`` writes preserve progress across crashes and
    frees the tab to switch threads / close / open another report.

    Args:
        template_override: When set, skip the DynamoDB / built-in template
            lookup and use this dict directly. Used by the free-form report
            mode to inject a synthetic single-section template built from
            the user's prompt.
    """
    import threading

    report_table = REPORT_TABLE_NAME
    thread_id = payload.get("threadId", "default")
    run_id = payload.get("runId", "run-0")

    # Load template up front so we can fail fast and still return a clean SSE.
    if template_override is not None:
        template = template_override
    else:
        template = reports.load_template(template_id, actor_id, report_table, region)
    if not template:
        def _not_found_gen():
            yield encoder.encode(
                TextMessageContentEvent(
                    type=EventType.TEXT_MESSAGE_CONTENT,
                    message_id="report-error",
                    delta=f"Error: Template '{template_id}' not found.",
                )
            )
            yield encoder.encode(
                RunFinishedEvent(
                    type=EventType.RUN_FINISHED,
                    threadId=thread_id,
                    runId=run_id,
                )
            )

        return StreamingResponse(
            _not_found_gen(), media_type=encoder.get_content_type()
        )

    sections = template.get("sections", [])
    dependencies = template.get("dependencies", {})
    variables = _extract_variables(user_prompt)
    month = variables.get("month", datetime.now(timezone.utc).strftime("%B"))
    year = variables.get("year", str(datetime.now(timezone.utc).year))

    report_record = reports.create_report_record(
        actor_id, template.get("name", template_id), sections, month, year
    )
    report_record["status"] = "pending"
    # Persist the empty shell so the frontend can begin polling immediately.
    reports.save_report(report_record, report_table, region)
    report_id = report_record["report_id"]

    # Save the user message now — before the background thread runs, so the
    # chat transcript reflects what was asked even if generation fails later.
    # Tag it as a report request so the UI can badge the prompt bubble (and
    # the badge survives reload). Covers templated AND freeform reports —
    # freeform routes through this same handler with template_id="freeform".
    if memory_enabled:
        save_user_message(memory_id, session_id, actor_id, user_prompt, region, is_report=True)
        # Persist a tiny kickoff marker for the assistant turn. This is the
        # durability anchor — on history reload, Thread.tsx sees the
        # <report-pending> tag and renders a polling ReportCard. While the
        # worker is still running, the card shows "Generating…"; once
        # the row hits a terminal state in DynamoDB, it auto-swaps to
        # "View Full Report" which opens ReportPanel + getReport(reportId).
        # Tiny payload (~150 bytes), so we never hit Memory event size
        # limits even for large reports. Using <report-pending> instead of
        # <artifact> here is intentional — <artifact> would make the
        # message render as a completed-looking card immediately.
        report_title_full = f"{template.get('name', template_id)} - {month} {year}"
        save_assistant_message(
            memory_id,
            session_id,
            actor_id,
            f'<report-pending report_id="{report_id}" title="{report_title_full}"/>',
            region,
        )

    mark_thread_running(
        thread_id,
        actor_id,
        f"Generating report: {template.get('name', template_id)}",
        run_id,
        report_id=report_id,
    )

    worker = threading.Thread(
        target=_run_report_async,
        kwargs=dict(
            payload=payload,
            template=template,
            report_record=report_record,
            variables=variables,
            month=month,
            year=year,
            session_id=session_id,
            actor_id=actor_id,
            thread_id=thread_id,
            agent_builder=agent_builder,
            memory_enabled=memory_enabled,
            memory_id=memory_id,
            region=region,
            report_table=report_table,
        ),
        name=f"report-gen-{report_id}",
        daemon=True,
    )
    worker.start()

    def _pending_event_generator():
        # Emit a structured marker the frontend parses to dispatch the
        # polling ReportCard. Using a <report-pending> tag inside a
        # TEXT_MESSAGE_CONTENT keeps us on the AG-UI protocol without
        # inventing a new event type.
        yield encoder.encode(
            TextMessageContentEvent(
                type=EventType.TEXT_MESSAGE_CONTENT,
                message_id=f"report-pending-{report_id}",
                delta=(
                    f'<report-pending report_id="{report_id}" '
                    f'title="{template.get("name", template_id)} - {month} {year}"/>'
                ),
            )
        )
        yield encoder.encode(
            RunFinishedEvent(
                type=EventType.RUN_FINISHED,
                threadId=thread_id,
                runId=run_id,
            )
        )

    return StreamingResponse(
        _pending_event_generator(), media_type=encoder.get_content_type()
    )


def _run_report_async(
    *,
    payload: dict,
    template: dict,
    report_record: dict,
    variables: dict,
    month: str,
    year: str,
    session_id: str,
    actor_id: str,
    thread_id: str,
    agent_builder: AgentBuilder,
    memory_enabled: bool,
    memory_id: str,
    region: str,
    report_table: str,
) -> None:
    """Background worker that runs report sections and persists progress.

    Runs in a non-daemon thread started by ``_handle_report_streaming``.
    Each section completion triggers a targeted ``update_report_section``
    write; a final ``save_report`` flips the top-level status to ``complete``
    or ``error`` and writes the enriched memory event. Mirrors the old
    synchronous path minus all AG-UI event emission.
    """
    report_id = report_record["report_id"]
    sections = template.get("sections", [])
    dependencies = template.get("dependencies", {})
    var_map = defaultdict(str, **variables)
    report_tool_parts: list[str] = []
    report_body_parts: list[str] = []

    # Strip session/actor from per-section agent calls so each runs as an
    # independent invocation without history pollution — same rationale as
    # the old synchronous path.
    clean_payload = {k: v for k, v in payload.items() if k != "forwardedProps"}
    clean_payload["forwardedProps"] = {
        k: v
        for k, v in payload.get("forwardedProps", {}).items()
        if k not in ("session_id", "actor_id")
    }

    section_results: list[dict] = [{} for _ in sections]

    def _run_one_section(idx: int, section_def: dict) -> dict:
        section_id = section_def["id"]
        prompt = section_def["prompt"].format_map(var_map)
        report_prompt = (
            prompt + "\n\nIMPORTANT: This is for a report section. "
            "Provide ONLY the requested data and analysis. "
            "Do NOT include follow-up questions, suggestions, or "
            "conversational text like 'Would you like me to...'."
        )
        try:
            agent, _sp, cleanup_fn = agent_builder(clean_payload)
            try:
                result = agent(report_prompt)
                text = result.message["content"][0]["text"]
                tool_uses = _extract_tool_uses_from_messages(agent)
                return {
                    "idx": idx,
                    "id": section_id,
                    "title": section_def["title"],
                    "status": "complete",
                    "content": text,
                    "error": "",
                    "traces": list(tool_uses.values()),
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                }
            finally:
                if cleanup_fn:
                    try:
                        cleanup_fn()
                    except Exception:
                        pass
        except Exception as exc:
            logger.error("Section '%s' failed: %s", section_id, exc)
            return {
                "idx": idx,
                "id": section_id,
                "title": section_def["title"],
                "status": "error",
                "content": "",
                "error": str(exc),
                "traces": [],
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }

    def _persist_section(idx: int, section_result: dict) -> None:
        """Write the completed section back to DynamoDB + bump progress."""
        completed_count = sum(
            1
            for r in section_results
            if r and r.get("status") in ("complete", "error", "skipped")
        )
        reports.update_report_section(
            actor_id,
            report_id,
            section_idx=idx,
            section={
                "id": section_result.get("id", ""),
                "title": section_result.get("title", ""),
                "status": section_result.get("status", ""),
                "content": section_result.get("content", ""),
                "error": section_result.get("error", ""),
                "generated_at": section_result.get("generated_at", ""),
                "traces": section_result.get("traces") or [],
            },
            current_section=completed_count,
            report_table=report_table,
            region=region,
            status="in_progress",
        )

    # Flip row from pending → in_progress so pollers see movement.
    report_record["status"] = "in_progress"
    reports.save_report(report_record, report_table, region)

    # Heartbeat the activity row every 60s while sections run. Without
    # this, a single section that takes >10 minutes lets the row go
    # stale (see thread_activity._STALE_MINUTES), the frontend treats
    # the thread as idle, the busy banner unmounts, and the
    # running→idle transition forces a history reload that wipes the
    # in-memory <report-pending> ReportCard. update_thread_step is
    # already called at section boundaries, so this only matters
    # during long single-section work.
    import threading as _threading

    heartbeat_stop = _threading.Event()

    def _heartbeat_loop() -> None:
        while not heartbeat_stop.wait(60):
            update_thread_step(thread_id, actor_id, "Generating report…")

    heartbeat = _threading.Thread(
        target=_heartbeat_loop,
        name=f"report-hb-{report_id}",
        daemon=True,
    )
    heartbeat.start()

    from concurrent.futures import ThreadPoolExecutor, as_completed

    try:
        batches = reports.build_dependency_graph(sections, dependencies)

        for batch in batches:
            # Skip dependents of failed sections — same logic as reports.py.
            runnable: list[tuple[int, dict]] = []
            for idx, sdef in batch:
                prereq_id = dependencies.get(sdef["id"], "")
                prereq_idx = (
                    next(
                        (i for i, s in enumerate(sections) if s["id"] == prereq_id),
                        None,
                    )
                    if prereq_id
                    else None
                )
                if prereq_idx is not None and section_results[prereq_idx].get(
                    "status"
                ) in ("error", "skipped"):
                    skipped = {
                        "idx": idx,
                        "id": sdef["id"],
                        "title": sdef["title"],
                        "status": "skipped",
                        "content": "",
                        "error": f"Skipped: prerequisite section '{prereq_id}' failed.",
                        "traces": [],
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                    }
                    section_results[idx] = skipped
                    _persist_section(idx, skipped)
                    update_thread_step(
                        thread_id, actor_id, f"Skipped section: {sdef['title']}"
                    )
                else:
                    runnable.append((idx, sdef))

            if not runnable:
                continue

            if len(runnable) == 1:
                idx, sdef = runnable[0]
                update_thread_step(
                    thread_id, actor_id, f"Generating section: {sdef['title']}"
                )
                result = _run_one_section(idx, sdef)
                section_results[idx] = result
                _persist_section(idx, result)
            else:
                update_thread_step(
                    thread_id,
                    actor_id,
                    f"Running {len(runnable)} sections in parallel",
                )
                max_workers = min(len(runnable), 5)
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {
                        executor.submit(_run_one_section, idx, sdef): (idx, sdef)
                        for idx, sdef in runnable
                    }
                    for future in as_completed(futures):
                        result = future.result()
                        idx = result["idx"]
                        section_results[idx] = result
                        _persist_section(idx, result)

        # Build memory body + final row
        for r in section_results:
            if not r:
                continue
            title = r.get("title", "")
            content = r.get("content", "")
            if content:
                report_body_parts.append(f"## {title}\n\n{content}")
            for trace in r.get("traces", []) or []:
                try:
                    report_tool_parts.append(f"<tool>{json.dumps(trace)}</tool>")
                except (TypeError, ValueError):
                    pass

        any_error = any(
            (r.get("status") in ("error", "skipped")) for r in section_results if r
        )
        final_status = "error" if all(
            (r.get("status") == "error") for r in section_results if r
        ) else "complete"
        # "complete" is used even with partial errors — the row carries
        # per-section status so the frontend can render the mix.

        report_record["sections"] = [
            {
                "id": r.get("id", ""),
                "title": r.get("title", ""),
                "status": r.get("status", ""),
                "content": r.get("content", ""),
                "error": r.get("error", ""),
                "generated_at": r.get("generated_at", ""),
                "traces": r.get("traces") or [],
            }
            for r in section_results
            if r
        ]
        report_record["status"] = final_status
        report_record["current_section"] = len(section_results)
        report_record["updated_at"] = datetime.now(timezone.utc).isoformat()

        # Freeform reports are titled from the user's raw prompt by default
        # (see _build_freeform_template), which reads awkwardly as a report
        # title (e.g. "Can you generate a new report showing..."). If the
        # generated content leads with a Markdown H1, prefer that as the title
        # — it's the report's own headline (e.g. "Total AWS Spend — Last 6
        # Months"), matching what ReportPanel shows in the body. Only applied
        # to the freeform single-section template so multi-section templated
        # reports keep their canonical "<name> - <month> <year>" titles.
        if template.get("description") == "User-generated free-form report":
            for r in section_results:
                content = (r or {}).get("content", "")
                if content and r.get("status") == "complete":
                    # Use the report's leading Markdown heading (H1 or H2 — the
                    # model commonly opens with "## <Report Title>") as the
                    # title. Strip trailing "#" and surrounding whitespace.
                    m = re.search(r"^#{1,3}\s+(.+?)\s*#*\s*$", content, flags=re.MULTILINE)
                    if m:
                        heading = m.group(1).strip()
                        if heading:
                            report_record["title"] = heading[:120]
                    break

        reports.save_report(report_record, report_table, region)

        # Note: we deliberately do NOT duplicate the full enriched report
        # body or its tool traces to AgentCore Memory. The kickoff
        # <report-pending> marker saved before this worker started is
        # enough — ReportPanel rehydrates the full report (including
        # traces) from DynamoDB via getReport(reportId) on reload.
        # Saving the full body here would risk exceeding the per-event
        # Memory payload limit (100k chars) on large reports.
        _ = report_body_parts
        _ = report_tool_parts

        if any_error and final_status != "error":
            logger.warning("Report %s completed with partial section errors", report_id)
    except Exception as exc:
        logger.error("Report generation failed: %s", exc, exc_info=True)
        mark_thread_error(thread_id, actor_id, str(exc))
        report_record["status"] = "error"
        report_record["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            reports.save_report(report_record, report_table, region)
        except Exception:
            pass
    finally:
        heartbeat_stop.set()
        mark_thread_idle(thread_id, actor_id)


def _handle_report_edit(
    *,
    payload: dict,
    parent_report_id: str,
    session_id: str,
    actor_id: str,
    user_prompt: str,
    agent_builder: AgentBuilder,
    encoder: EventEncoder,
    memory_enabled: bool,
    memory_id: str,
    region: str,
) -> StreamingResponse:
    """Kick off an edit of an existing report.

    Loads the parent report, creates a new versioned record, and returns
    a ``<report-pending>`` marker immediately. The background worker
    regenerates the full report in a single supervisor call (giving it
    the parent content + the edit instruction as context) rather than
    running the original template's per-section loop — edits are cheap
    when they touch just one section, and unchanged sections stay stable
    because the agent sees them and chooses not to rewrite them.

    Requires Phase B's async infrastructure.
    """
    import threading

    report_table = REPORT_TABLE_NAME
    thread_id = payload.get("threadId", "default")
    run_id = payload.get("runId", "run-0")

    parent = reports.load_report(actor_id, parent_report_id, report_table, region)
    if parent is None:
        def _missing_gen():
            yield encoder.encode(
                TextMessageContentEvent(
                    type=EventType.TEXT_MESSAGE_CONTENT,
                    message_id="report-edit-error",
                    delta=f"Error: parent report '{parent_report_id}' not found.",
                )
            )
            yield encoder.encode(
                RunFinishedEvent(
                    type=EventType.RUN_FINISHED,
                    threadId=thread_id,
                    runId=run_id,
                )
            )

        return StreamingResponse(
            _missing_gen(), media_type=encoder.get_content_type()
        )

    edit_record = reports.create_edit_report_record(parent, user_prompt)
    reports.save_report(edit_record, report_table, region)
    report_id = edit_record["report_id"]

    if memory_enabled:
        save_user_message(memory_id, session_id, actor_id, user_prompt, region, is_report=True)
        # Same kickoff-marker pattern as the create path. See
        # _handle_report_streaming for rationale on <report-pending>.
        edit_title_full = (
            f"{edit_record.get('title', 'Report')} (v{edit_record.get('version', 2)})"
        )
        save_assistant_message(
            memory_id,
            session_id,
            actor_id,
            (
                f'<report-pending report_id="{report_id}" '
                f'title="{edit_title_full}" '
                f'version="{edit_record.get("version", 2)}" '
                f'parent_report_id="{parent.get("report_id", "")}"/>'
            ),
            region,
        )

    mark_thread_running(
        thread_id,
        actor_id,
        f"Editing report: {parent.get('title', parent_report_id)}",
        run_id,
        report_id=report_id,
    )

    worker = threading.Thread(
        target=_run_report_edit_async,
        kwargs=dict(
            payload=payload,
            parent=parent,
            edit_record=edit_record,
            user_prompt=user_prompt,
            session_id=session_id,
            actor_id=actor_id,
            thread_id=thread_id,
            agent_builder=agent_builder,
            memory_enabled=memory_enabled,
            memory_id=memory_id,
            region=region,
            report_table=report_table,
        ),
        name=f"report-gen-{report_id}",
        daemon=True,
    )
    worker.start()

    def _pending_event_generator():
        yield encoder.encode(
            TextMessageContentEvent(
                type=EventType.TEXT_MESSAGE_CONTENT,
                message_id=f"report-pending-{report_id}",
                delta=(
                    f'<report-pending report_id="{report_id}" '
                    f'title="{edit_record.get("title", "")}" '
                    f'version="{edit_record.get("version", 2)}" '
                    f'parent_report_id="{parent_report_id}"/>'
                ),
            )
        )
        yield encoder.encode(
            RunFinishedEvent(
                type=EventType.RUN_FINISHED,
                threadId=thread_id,
                runId=run_id,
            )
        )

    return StreamingResponse(
        _pending_event_generator(), media_type=encoder.get_content_type()
    )


def _run_report_edit_async(
    *,
    payload: dict,
    parent: dict,
    edit_record: dict,
    user_prompt: str,
    session_id: str,
    actor_id: str,
    thread_id: str,
    agent_builder: AgentBuilder,
    memory_enabled: bool,
    memory_id: str,
    region: str,
    report_table: str,
) -> None:
    """Run a report edit in one supervisor invocation.

    Serializes the parent report's sections as context, appends the
    user's edit prompt, and asks the agent to emit the full revised
    report body as markdown. Splits the response back into sections
    using ``## Section Title`` headings. Unchanged sections remain
    stable because the agent sees them and has no reason to rewrite.
    """
    report_id = edit_record["report_id"]
    parent_sections = parent.get("sections", []) or []

    def _section_header(title: str) -> str:
        return f"## {title}".strip()

    parent_body = "\n\n".join(
        f"{_section_header(s.get('title', ''))}\n\n{s.get('content', '')}".strip()
        for s in parent_sections
        if s.get("content")
    )

    section_titles = [s.get("title", "") for s in parent_sections]
    titles_list = "\n".join(f"- {t}" for t in section_titles if t)
    edit_prompt = (
        "You are revising an existing report. Apply the user's edit "
        "instruction and emit the FULL revised report as markdown. "
        "Preserve unchanged sections verbatim. Use the exact section "
        "titles below as level-2 markdown headings (e.g. `## Title`) so "
        "the server can split your response back into sections.\n\n"
        f"Section titles (in order):\n{titles_list}\n\n"
        "--- Current report ---\n\n"
        f"{parent_body}\n\n"
        "--- User edit instruction ---\n\n"
        f"{user_prompt}\n\n"
        "Output the revised report now. Do NOT include any preamble, "
        "follow-up questions, or suggestions."
    )

    clean_payload = {k: v for k, v in payload.items() if k != "forwardedProps"}
    clean_payload["forwardedProps"] = {
        k: v
        for k, v in payload.get("forwardedProps", {}).items()
        if k not in ("session_id", "actor_id", "edit_report_id")
    }

    now_iso = datetime.now(timezone.utc).isoformat()

    # Heartbeat the activity row every 60s — same rationale as
    # _run_report_async. The edit path is a single supervisor call
    # rather than per-section, so it's even more vulnerable to going
    # stale during large rewrites.
    import threading as _threading

    heartbeat_stop = _threading.Event()

    def _heartbeat_loop() -> None:
        while not heartbeat_stop.wait(60):
            update_thread_step(thread_id, actor_id, "Revising report…")

    heartbeat = _threading.Thread(
        target=_heartbeat_loop,
        name=f"report-edit-hb-{report_id}",
        daemon=True,
    )
    heartbeat.start()

    try:
        update_thread_step(thread_id, actor_id, "Revising report…")
        agent, _sp, cleanup_fn = agent_builder(clean_payload)
        try:
            result = agent(edit_prompt)
            raw_text = result.message["content"][0]["text"]
        finally:
            if cleanup_fn:
                try:
                    cleanup_fn()
                except Exception:
                    pass

        new_sections = _split_edit_response_into_sections(
            raw_text, section_titles
        )

        # Fill in new content per section; fall back to parent content for
        # titles the agent didn't emit (conservative — never lose data).
        revised = []
        for i, ps in enumerate(parent_sections):
            new_body = new_sections.get(ps.get("title", ""), "").strip()
            if new_body:
                revised.append(
                    {
                        "id": ps.get("id", ""),
                        "title": ps.get("title", ""),
                        "status": "complete",
                        "content": new_body,
                        "error": "",
                        "generated_at": now_iso,
                    }
                )
            else:
                revised.append(
                    {
                        "id": ps.get("id", ""),
                        "title": ps.get("title", ""),
                        "status": ps.get("status", "complete"),
                        "content": ps.get("content", ""),
                        "error": "",
                        "generated_at": ps.get("generated_at", now_iso),
                    }
                )
            reports.update_report_section(
                actor_id,
                report_id,
                section_idx=i,
                section=revised[-1],
                current_section=i + 1,
                report_table=report_table,
                region=region,
                status="in_progress",
            )

        edit_record["sections"] = revised
        edit_record["status"] = "complete"
        edit_record["current_section"] = len(revised)
        edit_record["updated_at"] = datetime.now(timezone.utc).isoformat()
        reports.save_report(edit_record, report_table, region)

        # Same rationale as _run_report_async: don't save the full
        # enriched body to Memory. The kickoff <artifact> marker saved
        # before this worker started is the durability anchor; the full
        # report rehydrates from DynamoDB via getReport(reportId).
    except Exception as exc:
        logger.error("Report edit failed: %s", exc, exc_info=True)
        mark_thread_error(thread_id, actor_id, str(exc))
        edit_record["status"] = "error"
        edit_record["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            reports.save_report(edit_record, report_table, region)
        except Exception:
            pass
    finally:
        heartbeat_stop.set()
        mark_thread_idle(thread_id, actor_id)


def _split_edit_response_into_sections(
    raw: str, section_titles: list[str]
) -> dict[str, str]:
    """Split an agent's markdown response into ``title -> body`` by finding
    ``## <title>`` headings that match the known section titles.

    The agent is asked to use the exact titles as level-2 headings; this
    splitter is permissive about whitespace/case but requires an exact
    title string match. Missing sections are left out so the caller can
    fall back to parent content.
    """
    by_title: dict[str, str] = {}
    if not raw or not section_titles:
        return by_title

    # Build a regex alternation of escaped titles for efficient matching.
    title_set = {t for t in section_titles if t}
    if not title_set:
        return by_title
    pattern = re.compile(
        r"^##\s+(" + "|".join(re.escape(t) for t in title_set) + r")\s*$",
        flags=re.MULTILINE | re.IGNORECASE,
    )
    matches = list(pattern.finditer(raw))
    for i, m in enumerate(matches):
        title_actual = m.group(1)
        # Case-insensitive match — snap to the canonical title from the set.
        canonical = next(
            (t for t in title_set if t.lower() == title_actual.lower()),
            title_actual,
        )
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        body = raw[start:end].strip()
        by_title[canonical] = body
    return by_title


def _extract_tool_uses_from_messages(agent: Any) -> dict[str, dict]:
    """Pull tool-use / tool-result pairs out of a Strands agent's history.

    Extracted from the old synchronous report path so both the async
    worker and (eventually) the edit path can reuse the same extractor.
    """
    tool_uses: dict[str, dict] = {}
    for msg in getattr(agent, "messages", []):
        for block in msg.get("content", []):
            if not isinstance(block, dict):
                continue
            if "toolUse" in block:
                tu = block["toolUse"]
                tid = tu.get("toolUseId", "")
                tool_uses[tid] = {
                    "tool_name": tu.get("name", "unknown"),
                    "input": tu.get("input", {}),
                    "status": "success",
                }
            elif "toolResult" in block:
                tr = block["toolResult"]
                tid = tr.get("toolUseId", "")
                if tid not in tool_uses:
                    continue
                cv = tr.get("content", "")
                if isinstance(cv, list):
                    cv = " ".join(
                        (
                            str(x.get("text", x.get("json", "")))
                            if isinstance(x, dict)
                            else str(x)
                        )
                        for x in cv
                    )
                out = str(cv) if cv else ""
                tool_uses[tid]["output"] = out
                try:
                    p = json.loads(out) if isinstance(out, str) else out
                    if isinstance(p, str):
                        p = json.loads(p)
                    if isinstance(p, dict) and "tool_trace" in p:
                        tool_uses[tid]["tool_trace"] = p["tool_trace"]
                        tool_uses[tid]["output"] = str(
                            p.get("data", p.get("response", ""))
                        )
                except (json.JSONDecodeError, TypeError):
                    pass
    return tool_uses



def _track_event(
    event: Any,
    ordered_segments: list[dict],
    text_parts: list[str],
    thinking_parts: list[str],
    tool_starts: dict[str, dict],
) -> None:
    """Track a single AG-UI event for enriched memory save."""
    etype = (
        str(event.type)
        if hasattr(event.type, "value")
        else str(getattr(event, "type", ""))
    )

    if "REASONING_MESSAGE_CONTENT" in etype or "REASONING_CONTENT" in etype:
        delta = getattr(event, "delta", "")
        if delta:
            thinking_parts.append(delta)
            ordered_segments.append({"type": "thinking", "value": delta})

    elif "TEXT_MESSAGE_CONTENT" in etype:
        delta = getattr(event, "delta", "")
        if delta:
            text_parts.append(delta)
            ordered_segments.append({"type": "text", "value": delta})

    elif "TOOL_CALL_START" in etype:
        tc_id = getattr(event, "tool_call_id", "") or getattr(event, "toolCallId", "")
        tc_name = getattr(event, "tool_call_name", "") or getattr(
            event, "toolCallName", ""
        )
        tool_starts[tc_id] = {"name": tc_name, "input": {}, "args": ""}

    elif "TOOL_CALL_ARGS" in etype:
        tc_id = getattr(event, "tool_call_id", "") or getattr(event, "toolCallId", "")
        delta = getattr(event, "delta", "")
        if tc_id in tool_starts and delta:
            tool_starts[tc_id]["args"] += delta

    elif "TOOL_CALL_END" in etype:
        tc_id = getattr(event, "tool_call_id", "") or getattr(event, "toolCallId", "")
        if tc_id in tool_starts:
            tc = tool_starts[tc_id]
            try:
                tc["input"] = json.loads(tc["args"]) if tc["args"] else {}
            except (json.JSONDecodeError, TypeError):
                tc["input"] = {"raw": tc["args"]}

    elif "TOOL_CALL_RESULT" in etype:
        tc_id = getattr(event, "tool_call_id", "") or getattr(event, "toolCallId", "")
        content = getattr(event, "content", "")
        tc = tool_starts.get(tc_id, {})

        # Parse the _delegate result to extract clean output and nested trace.
        # Output is forwarded at full fidelity — the frontend TracePanel /
        # ReportPanel abbreviate at render time. Preserving the full JSON
        # here is load-bearing for: (a) follow-up-turn LLM quoting exact
        # numbers from saved memory, (b) the visualizer-state extractor
        # re-parsing structured tool output, (c) any future data widget
        # that needs the raw payload.
        clean_output = content if content else ""
        nested_trace: list = []
        try:
            parsed_content = (
                json.loads(content) if isinstance(content, str) else content
            )
            # Handle double-encoding
            if isinstance(parsed_content, str):
                parsed_content = json.loads(parsed_content)
            if isinstance(parsed_content, dict):
                # Extract the actual response text
                clean_output = parsed_content.get(
                    "data", parsed_content.get("response", clean_output)
                )
                # If data is itself a traced response, extract further
                if isinstance(clean_output, str) and clean_output.startswith("{"):
                    try:
                        inner = json.loads(clean_output)
                        if isinstance(inner, dict) and "response" in inner:
                            clean_output = inner["response"]
                            if "tool_trace" in inner:
                                nested_trace = inner["tool_trace"]
                    except (json.JSONDecodeError, TypeError):
                        pass
                if not nested_trace and "tool_trace" in parsed_content:
                    nested_trace = parsed_content["tool_trace"]
        except (json.JSONDecodeError, TypeError):
            pass

        # Serialise non-string payloads so the <tool> tag value stays a
        # single JSON string; frontend unwraps it with the standard peel loop.
        if isinstance(clean_output, (dict, list)):
            output_str = json.dumps(clean_output)
        elif isinstance(clean_output, str):
            output_str = clean_output
        else:
            output_str = str(clean_output)

        tool_data: dict[str, Any] = {
            "name": tc.get("name", "unknown"),
            "input": tc.get("input", {}),
            "output": output_str,
        }
        if nested_trace:
            tool_data["tool_trace"] = nested_trace
        # Drop the raw output of bulky no-reload-consumer tools (e.g.
        # assemble_cfn_template) from the persisted trace — at the top level
        # AND in every nested sub-agent trace entry. The CFN body is already
        # in the reports table (rendered via <report-pending>), and the saved
        # <tool> trace is display-only for these tools. This is what keeps the
        # assistant turn under AgentCore Memory's 100k-char per-event limit;
        # without it the CreateEvent silently fails and the whole turn vanishes
        # on reload. DX-topology traces are intentionally exempt (the visualizer
        # rebuilds from them) — see _NO_RELOAD_TRACE_TOOLS.
        tool_data = _abbreviate_bulky_tool_traces(tool_data)
        ordered_segments.append({"type": "tool", "value": json.dumps(tool_data)})


def _save_enriched_response(
    memory_id: str,
    session_id: str,
    actor_id: str,
    ordered_segments: list[dict],
    text_parts: list[str],
    region: str,
) -> None:
    """Build enriched text and save to Memory."""
    if not text_parts and not ordered_segments:
        return
    enriched_text = build_enriched_text(ordered_segments)
    save_assistant_message(memory_id, session_id, actor_id, enriched_text, region)


# ---------------------------------------------------------------------------
# Primary CFN artifact delivery — intercept at TOOL_CALL_RESULT level
# ---------------------------------------------------------------------------
#
# The _delegate tool in registry.py extracts <cfn-artifact> blocks from the
# sub-agent response BEFORE the supervisor model sees it (model can't
# abbreviate what it never received). The extracted artifacts ride in the
# result dict under "_cfn_artifacts". This function is called from the
# event loop when a TOOL_CALL_RESULT arrives: it persists each artifact to
# DynamoDB and returns synthetic <report-pending> events for the frontend.


def _handle_cfn_artifacts_from_tool_result(
    event: Any,
    actor_id: str,
    report_table: str,
    region: str,
    persisted_titles: set[str] | None = None,
) -> list:
    """Drain pending CFN artifacts (extracted by _delegate) and persist them.

    Called from the event loop on every TOOL_CALL_RESULT. The artifacts
    were extracted from the sub-agent response by _delegate and stored in
    a module-level queue (never in the result dict the model sees).

    Returns a list of TextMessageContentEvent objects (one <report-pending>
    per artifact) that the caller yields into the SSE stream.

    Args:
        persisted_titles: Request-scoped set of artifact titles already
            persisted this turn. Populated here so the streaming-interceptor
            fallback can skip re-persisting the same artifact (the supervisor
            prompt tells the model to echo the <cfn-artifact> tag, which the
            interceptor would otherwise persist a second time).
    """
    from agents.shared.registry import get_pending_cfn_artifacts

    artifacts = get_pending_cfn_artifacts()
    if not artifacts:
        return []

    events_out = []
    for artifact in artifacts:
        title = artifact.get("title", "CloudFormation alarm template")
        yaml_text = artifact.get("yaml", "")
        if not yaml_text:
            continue

        dedup_key = title or "CloudFormation alarm template"
        if persisted_titles is not None and dedup_key in persisted_titles:
            continue

        report_id = _new_cfn_artifact_report_id()
        persisted = False
        try:
            persisted = _persist_cfn_artifact_as_report(
                report_id=report_id,
                title=title,
                template_yaml=yaml_text,
                actor_id=actor_id,
                report_table=report_table,
                region=region,
            )
        except Exception as exc:
            logger.error(
                "cfn-artifact persistence (tool-result path) failed: %s",
                exc,
                exc_info=True,
            )

        if persisted:
            if persisted_titles is not None:
                persisted_titles.add(dedup_key)
            pending_tag = (
                f'<report-pending report_id="{report_id}" '
                f'title="{title}"/>'
            )
            events_out.append(
                TextMessageContentEvent(
                    type=EventType.TEXT_MESSAGE_CONTENT,
                    message_id=f"report-pending-{report_id}",
                    delta=pending_tag,
                )
            )
        else:
            fallback = f"\n```yaml\n{yaml_text}\n```\n"
            events_out.append(
                TextMessageContentEvent(
                    type=EventType.TEXT_MESSAGE_CONTENT,
                    message_id="cfn-artifact-fallback",
                    delta=fallback,
                )
            )

    return events_out


# ---------------------------------------------------------------------------
# <cfn-artifact> streaming interceptor — DEFENSIVE FALLBACK
# ---------------------------------------------------------------------------
#
# If the primary path above fails (e.g. the model somehow re-emits
# <cfn-artifact> in its text reply despite the sanitized tool result), this
# streaming interceptor catches it and handles it the same way. It is a
# no-op for any agent that doesn't emit the tag in its text stream.
#
# When the cloudwatch-agent emits a CloudFormation template, it places the
# YAML inside a `<cfn-artifact title="...">…</cfn-artifact>` block in the
# chat stream. The supervisor's chat-mode AG-UI loop pipes every
# TEXT_MESSAGE_CONTENT delta through `_CfnArtifactInterceptor` which:
#
#   1. Buffers the YAML body between the open and close tags so it is NEVER
#      streamed to the client and NEVER tracked into Memory's enriched-save
#      `text_parts`.
#   2. On the close tag, persists the YAML to the existing reports DynamoDB
#      table as a complete single-section freeform report (matching the
#      shape `agents.shared.reports.save_report` already writes).
#   3. Emits a synthetic `<report-pending report_id="…" title="…"/>` delta
#      in place of the entire artifact block. The frontend's existing
#      ReportCard polling + ReportPanel rendering + history-reload paths
#      pick this up unchanged.
#
# Memory only stores the small `<report-pending …/>` marker (~150 bytes),
# well under AgentCore's 100k-char per-event limit. On reload, the history
# loader rehydrates the same ReportCard from the marker, the card polls
# `/reports/{id}/status`, finds status=complete, and becomes clickable —
# rendering the YAML from DynamoDB.
#
# Other agents are unaffected: the interceptor is a no-op unless the
# substring `<cfn-artifact` ever appears in a delta, which only the
# cloudwatch-agent emits (and the supervisor prompt explicitly tells the
# model to pass through verbatim).

# Open-tag pattern allows attributes in any order; only `title` is consumed.
_CFN_ARTIFACT_OPEN = re.compile(
    r"<cfn-artifact\b(?P<attrs>[^>]*)>", re.DOTALL
)
_CFN_ARTIFACT_CLOSE = "</cfn-artifact>"
_CFN_TITLE_ATTR = re.compile(r'title\s*=\s*"([^"]*)"')


class _CfnArtifactInterceptor:
    """Stateful streaming filter for `<cfn-artifact …>…</cfn-artifact>` tags.

    States:
      * PASSTHROUGH — forward deltas as-is.
      * BUFFERING   — the open tag has been seen; accumulate inner text,
        emit nothing until the close tag arrives.

    The matcher carries a cross-delta straddle buffer because the open or
    close tag may be split across deltas by the model's tokenizer. Every
    delta starts by appending to the carry and re-scanning.

    ``feed(delta)`` returns ``(forward_deltas, completed_artifacts)``:
      * forward_deltas — list of strings the supervisor should yield as
        TEXT_MESSAGE_CONTENT events to the client (and track into Memory).
        May be empty if the entire delta was buffered.
      * completed_artifacts — list of ``(title, yaml_text)`` for any
        ``<cfn-artifact>`` blocks that closed during this delta. The
        supervisor persists each and emits a ``<report-pending>`` delta.
    """

    # Maximum carry size — caps memory if a malformed open-tag never closes.
    _MAX_CARRY = 512

    def __init__(self) -> None:
        self._buffering = False
        self._carry = ""           # straddle buffer for tag detection
        self._captured_title = ""  # title from the current open tag
        self._captured_yaml: list[str] = []  # body parts inside current tag

    def feed(self, delta: str) -> tuple[list[str], list[tuple[str, str]]]:
        if not delta:
            return ([], [])

        forwards: list[str] = []
        completed: list[tuple[str, str]] = []

        # Append to the carry then re-scan from the start. The carry
        # captures a partial open or close tag from the previous delta.
        self._carry += delta

        while self._carry:
            if self._buffering:
                # Looking for </cfn-artifact>. If found, capture preceding
                # YAML, emit a completed artifact, reset state.
                idx = self._carry.find(_CFN_ARTIFACT_CLOSE)
                if idx == -1:
                    # No close tag yet. Keep most of the carry as YAML body
                    # but retain the tail (up to len(close_tag)-1 chars) in
                    # case a partial close is straddling the next delta.
                    keep = max(0, len(self._carry) - (len(_CFN_ARTIFACT_CLOSE) - 1))
                    if keep > 0:
                        self._captured_yaml.append(self._carry[:keep])
                        self._carry = self._carry[keep:]
                    break

                # Close tag found: everything before idx is YAML body.
                self._captured_yaml.append(self._carry[:idx])
                yaml_text = "".join(self._captured_yaml)
                completed.append((self._captured_title, yaml_text))

                # Consume up to and including the close tag.
                self._carry = self._carry[idx + len(_CFN_ARTIFACT_CLOSE):]
                self._buffering = False
                self._captured_title = ""
                self._captured_yaml = []
                # Loop continues — the carry may now contain text following
                # the close tag, or even another open tag.

            else:
                # PASSTHROUGH: look for an open tag.
                m = _CFN_ARTIFACT_OPEN.search(self._carry)
                if m is None:
                    # Regex didn't match — there's no complete `<cfn-artifact …>`
                    # in the carry yet. Two ways the carry could still be
                    # mid-tag:
                    #   (a) The full literal `<cfn-artifact` is present but
                    #       the closing `>` hasn't arrived yet (the model
                    #       is still streaming the title attribute). Hold
                    #       back everything from the literal onward.
                    #   (b) The carry's tail ends with a PREFIX of
                    #       `<cfn-artifact` (e.g. `…<cfn-art`). Hold that
                    #       tail back; forward the rest.
                    open_lit_idx = self._carry.find("<cfn-artifact")
                    if open_lit_idx >= 0:
                        # Case (a): forward everything before the literal,
                        # keep the literal-and-after as carry, and break
                        # (we need more deltas to complete the tag). The
                        # _MAX_CARRY guard below catches malformed tags
                        # that never close.
                        if open_lit_idx > 0:
                            forwards.append(self._carry[:open_lit_idx])
                            self._carry = self._carry[open_lit_idx:]
                        if len(self._carry) > self._MAX_CARRY:
                            forwards.append(self._carry)
                            self._carry = ""
                        break
                    # Case (b): no full literal anywhere. The carry might
                    # end with a partial `<cfn-art` prefix — hold that
                    # tail back, forward the rest.
                    safe_end = self._safe_passthrough_end(self._carry)
                    if safe_end > 0:
                        forwards.append(self._carry[:safe_end])
                        self._carry = self._carry[safe_end:]
                    if len(self._carry) > self._MAX_CARRY:
                        # Defensive: never let an unmatched partial grow
                        # unbounded. Flush as plain text.
                        forwards.append(self._carry)
                        self._carry = ""
                    break

                # Forward everything before the open tag.
                if m.start() > 0:
                    forwards.append(self._carry[: m.start()])

                # Capture the title attribute (optional).
                attrs = m.group("attrs") or ""
                title_match = _CFN_TITLE_ATTR.search(attrs)
                self._captured_title = title_match.group(1) if title_match else ""

                # Switch into BUFFERING. Discard the open tag itself.
                self._buffering = True
                self._carry = self._carry[m.end():]
                # Loop continues — the carry may already contain the body
                # and even the close tag for very small artifacts.

        return (forwards, completed)

    def flush(self) -> list[str]:
        """Called when the upstream stream ends.

        If we're still buffering (no close tag arrived — model truncated),
        flush the captured YAML AS PLAIN TEXT so the user at least sees
        the partial output. Defensive fallback only — the agent prompt
        makes truncation extremely rare.

        Also flushes any non-empty carry from the passthrough state.
        """
        flushed: list[str] = []
        if self._buffering:
            # While buffering, the carry holds the last few chars of the
            # body (kept back to detect a close-tag straddling the next
            # delta). At end-of-stream they're part of the body, not a
            # partial close tag — fold them into the partial YAML.
            partial_yaml_parts = list(self._captured_yaml)
            if self._carry:
                partial_yaml_parts.append(self._carry)
            partial = "".join(partial_yaml_parts)
            if partial:
                flushed.append(
                    f"\n[cfn-artifact unterminated — partial output]\n{partial}\n"
                )
            self._buffering = False
            self._captured_title = ""
            self._captured_yaml = []
            self._carry = ""
        elif self._carry:
            # Passthrough state at end-of-stream: the carry is a held-back
            # partial-open prefix (e.g. "<cfn-art"). Release it as text.
            flushed.append(self._carry)
            self._carry = ""
        return flushed

    @staticmethod
    def _safe_passthrough_end(carry: str) -> int:
        """Find the largest prefix of `carry` that's safe to forward.

        If the carry's tail matches a prefix of "<cfn-artifact", that tail
        is held back for the next delta — the rest is safe to forward.
        """
        marker = "<cfn-artifact"
        max_check = min(len(marker), len(carry))
        for back in range(max_check, 0, -1):
            tail = carry[-back:]
            if marker.startswith(tail):
                return len(carry) - back
        return len(carry)


# ---------------------------------------------------------------------------
# Model-emitted <report-pending> stripper
#
# `<report-pending …/>` markers are injected EXCLUSIVELY by the platform —
# the cfn-artifact interceptor (above) and _handle_report_streaming mint a
# real `rpt-cfn-{12 hex}` id, persist the row to DynamoDB, and emit the
# marker as their OWN TextMessageContentEvent (which never passes through a
# model delta). The model is never asked to type the marker.
#
# But once a turn's <report-pending> marker is saved to Memory (so the model
# can resolve "open that report" via no-fabrication rule 9), it shows up in
# the model's history. On a follow-up the model is liable to MIMIC the format
# and type its own `<report-pending report_id="rpt-cfn-tuned-…"/>` with a
# FABRICATED id alongside the legitimate <cfn-artifact> it emits. That id
# matches no DynamoDB row, so the frontend renders a ReportCard that polls a
# 404 forever (observed live: a tune follow-up produced both the real
# `rpt-cfn-77da92d85e4a` AND a bogus `rpt-cfn-tuned-a7b92c01`, same title).
#
# This stateful filter drops any `<report-pending …>` the MODEL emits from
# the chat stream (and therefore from the Memory save). It runs only on
# model-origin deltas — the platform's own injected markers bypass it.
# Self-closing tag: we drop from the literal `<report-pending` up to and
# including the next `>` (attribute values are quoted and contain no `>`).
class _ReportPendingStripper:
    """Stateful streaming filter that removes model-typed `<report-pending>`.

    ``feed(text)`` returns the text with any complete `<report-pending …>`
    marker removed, holding back a partial tag straddling the next delta.
    ``flush()`` releases anything still held when the stream ends.
    """

    _LITERAL = "<report-pending"
    _MAX_CARRY = 1024  # cap: a malformed never-closing tag is flushed as text

    def __init__(self) -> None:
        self._dropping = False
        self._carry = ""

    def feed(self, text: str) -> str:
        if not text:
            return ""
        out: list[str] = []
        self._carry += text
        while self._carry:
            if self._dropping:
                gt = self._carry.find(">")
                if gt == -1:
                    # Tag not closed yet. Defensive cap so a malformed marker
                    # that never closes can't swallow the rest of the reply.
                    if len(self._carry) > self._MAX_CARRY:
                        out.append(self._carry)
                        self._carry = ""
                        self._dropping = False
                    break
                # Drop through the closing '>'.
                self._carry = self._carry[gt + 1:]
                self._dropping = False
            else:
                idx = self._carry.find(self._LITERAL)
                if idx >= 0:
                    if idx > 0:
                        out.append(self._carry[:idx])
                    self._carry = self._carry[idx:]
                    self._dropping = True
                    continue
                # No full literal. Hold back a tail that's a prefix of the
                # literal (it may complete on the next delta); forward rest.
                safe_end = self._safe_end(self._carry)
                if safe_end > 0:
                    out.append(self._carry[:safe_end])
                    self._carry = self._carry[safe_end:]
                if len(self._carry) > self._MAX_CARRY:
                    out.append(self._carry)
                    self._carry = ""
                break
        return "".join(out)

    def flush(self) -> str:
        """Release any held text at end-of-stream.

        A held partial-open prefix (e.g. `<report-p`) is real text → release.
        A tag we were mid-drop on never closed → discard it (it was a
        malformed marker, not content worth showing).
        """
        if self._dropping:
            self._carry = ""
            self._dropping = False
            return ""
        held = self._carry
        self._carry = ""
        return held

    @classmethod
    def _safe_end(cls, carry: str) -> int:
        """Largest prefix of `carry` safe to forward without splitting the
        literal `<report-pending` across a delta boundary."""
        max_check = min(len(cls._LITERAL), len(carry))
        for back in range(max_check, 0, -1):
            if cls._LITERAL.startswith(carry[-back:]):
                return len(carry) - back
        return len(carry)


def _new_cfn_artifact_report_id() -> str:
    """Stable prefix lets a future migration script find these rows."""
    import uuid as _uuid

    return f"rpt-cfn-{_uuid.uuid4().hex[:12]}"


# Defense-in-depth backstop for the source-level drop in
# `agent_base.create_leaf_agent` (which removes no-reload-consumer tool output
# BEFORE it is ever serialized into a trace). This walker re-applies the same
# rule to the parsed `<tool>` structure at memory-save time, catching any path
# that bypasses the leaf loop (e.g. the report-mode tool extractor). It only
# traverses PARSED dict/list structure — it does NOT descend into
# string-encoded nested traces, which is exactly why the source-level drop is
# the primary fix. Shares `_NO_RELOAD_TRACE_TOOLS` / `_ABBREVIATED_TOOL_OUTPUT`
# with agent_base (imported above) so the two never diverge.
def _abbreviate_bulky_tool_traces(node: Any) -> Any:
    """Recursively replace the `output` of any no-reload-consumer tool (see
    `_NO_RELOAD_TRACE_TOOLS`) with a short placeholder, in a `<tool>` segment
    dict and every nested `tool_trace` entry beneath it.

    Keyed on the tool name (`name` at the top level, `tool_name` in nested
    trace entries), with the gateway "<target>___" prefix stripped before
    matching. Mutates a shallow copy so the live in-flight trace is untouched;
    only the about-to-be-persisted copy is abbreviated.
    """
    if isinstance(node, list):
        return [_abbreviate_bulky_tool_traces(x) for x in node]
    if not isinstance(node, dict):
        return node
    out = dict(node)
    name = out.get("name") or out.get("tool_name") or ""
    bare_name = name.split("___")[-1] if "___" in name else name
    if bare_name in _NO_RELOAD_TRACE_TOOLS and "output" in out:
        out["output"] = _ABBREVIATED_TOOL_OUTPUT
    if isinstance(out.get("tool_trace"), list):
        out["tool_trace"] = [
            _abbreviate_bulky_tool_traces(x) for x in out["tool_trace"]
        ]
    return out


def _persist_cfn_artifact_as_report(
    *,
    report_id: str,
    title: str,
    template_yaml: str,
    actor_id: str,
    report_table: str,
    region: str,
) -> bool:
    """Persist a `<cfn-artifact>` body as a complete single-section report.

    Writes a row to ``REPORT_TABLE_NAME`` shaped exactly like a freeform
    template report so the frontend's existing GET /reports/{id} +
    ReportPanel paths render it without any frontend changes:

      * ``user_id`` = ``actor_id`` (so ``userId`` partition key resolves
        to ``"report:<actor_id>"`` per the existing ``save_report`` schema).
      * ``status`` = ``"complete"`` so the ReportCard's first poll resolves
        immediately and the card becomes clickable.
      * One section: id=``"template"``, title=``"CloudFormation Template"``,
        ``status="complete"``, content wraps the YAML in a ```yaml fenced
        block (ReportPanel renders it with syntax highlighting).

    Returns True on success, False on any error (caller falls back to
    streaming the buffered YAML through as plain text).
    """
    if not report_table:
        logger.warning(
            "REPORT_TABLE_NAME not set — cannot persist cfn-artifact %s", report_id
        )
        return False

    now_iso = datetime.now(timezone.utc).isoformat()
    fenced_yaml = f"```yaml\n{template_yaml}\n```"
    section = {
        "id": "template",
        "title": "CloudFormation Template",
        "status": "complete",
        "content": fenced_yaml,
        "error": "",
        "generated_at": now_iso,
    }
    report_data = {
        "report_id": report_id,
        "user_id": actor_id,
        "title": title or "CloudFormation alarm template",
        "status": "complete",
        # Reuse the month/year fields (existing schema) — purely cosmetic
        # for the sidebar entry's date display.
        "month": datetime.now(timezone.utc).strftime("%B"),
        "year": datetime.now(timezone.utc).strftime("%Y"),
        "created_at": now_iso,
        "updated_at": now_iso,
        "sections": [section],
        "current_section": 1,
        "total_sections": 1,
        "parent_report_id": "",
        "version": 1,
    }
    return reports.save_report(
        report_data, report_table=report_table, region=region
    )


def _handle_legacy_chat(
    payload: dict,
    agent_builder: AgentBuilder,
    memory_enabled: bool,
    memory_id: str,
    region: str,
) -> JSONResponse:
    """Handle legacy {prompt, session_id, actor_id} chat requests."""
    prompt = payload.get("prompt", "")
    if not prompt:
        return JSONResponse(content={"error": "Missing required fields: prompt"})

    session_id = payload.get("session_id", "")
    actor_id = payload.get("actor_id", "")
    cleanup_fn = None

    try:
        # Build agent via the caller's builder
        agent, system_prompt, cleanup_fn = agent_builder(payload)

        # Save user message immediately
        if memory_enabled:
            save_user_message(memory_id, session_id, actor_id, prompt, region)

        response = agent(prompt)
        text = response.message["content"][0]["text"]

        # Save assistant response
        if memory_enabled:
            save_assistant_message(memory_id, session_id, actor_id, text, region)

        return JSONResponse(content={"status": "success", "response": text})
    except Exception as exc:
        logger.error("Legacy chat failed: %s", exc, exc_info=True)
        return JSONResponse(content={"status": "error", "error": str(exc)})
    finally:
        if cleanup_fn:
            try:
                cleanup_fn()
            except Exception:
                pass
