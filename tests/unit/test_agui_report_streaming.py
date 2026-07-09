"""Unit tests for report detection and streaming in agui_server.py."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agents.shared.agui_server import (
    _extract_variables,
    create_agui_app,
)


def _wait_for_report_workers(timeout: float = 5.0) -> None:
    """Block until any ``report-gen-*`` background threads finish.

    Reports run asynchronously now — the HTTP response returns as soon as
    the row is pre-created and the worker thread is started. Tests that
    assert on side effects (DynamoDB writes, memory saves) must wait for
    the worker to finish before checking.
    """
    deadline = threading.Event()
    workers = [
        t for t in threading.enumerate() if t.name.startswith("report-gen-")
    ]
    for t in workers:
        t.join(timeout=timeout)
    # Sanity: re-check and fail loudly if anything is still alive.
    for t in threading.enumerate():
        if t.name.startswith("report-gen-") and t.is_alive():
            raise AssertionError(
                f"report worker {t.name} did not finish within {timeout}s"
            )
    _ = deadline  # suppress unused


# ---------------------------------------------------------------------------
# _extract_variables tests
# ---------------------------------------------------------------------------


class TestExtractVariables:
    def test_extracts_month_year(self):
        variables = _extract_variables("month: February, year: 2026")
        assert variables["month"] == "February"
        assert variables["year"] == "2026"

    def test_extracts_multiple_variables(self):
        variables = _extract_variables("month: March, year: 2025, region: us-east-1")
        assert variables["month"] == "March"
        assert variables["year"] == "2025"
        assert variables["region"] == "us-east-1"

    def test_falls_back_to_current_date(self):
        variables = _extract_variables("Generate a report please")
        now = datetime.now(timezone.utc)
        assert variables["month"] == now.strftime("%B")
        assert variables["year"] == str(now.year)

    def test_empty_prompt_falls_back(self):
        variables = _extract_variables("")
        assert "month" in variables
        assert "year" in variables

    def test_keys_lowercased(self):
        variables = _extract_variables("Month: January, Year: 2026")
        assert variables["month"] == "January"
        assert variables["year"] == "2026"


# ---------------------------------------------------------------------------
# Report detection in /invocations tests
# ---------------------------------------------------------------------------


def _make_agent_builder():
    """Create a mock agent_builder that returns a mock Agent."""

    def agent_builder(payload):
        mock_agent = MagicMock()
        mock_agent.return_value = MagicMock(
            message={"content": [{"text": "Mock section content"}]}
        )
        return mock_agent, "system prompt", None

    return agent_builder


def _make_agui_payload(template_id=None, user_msg="month: January, year: 2026"):
    """Build a minimal AG-UI payload."""
    forwarded = {"session_id": "sess-1", "actor_id": "user1"}
    if template_id:
        forwarded["template_id"] = template_id
    return {
        "threadId": "thread-1",
        "runId": "run-1",
        "messages": [
            {
                "id": "msg-1",
                "role": "user",
                "content": user_msg,
            }
        ],
        "state": {},
        "tools": [],
        "context": [],
        "forwardedProps": forwarded,
    }


SAMPLE_TEMPLATE = {
    "template_id": "test_template",
    "name": "Test Report",
    "sections": [
        {"id": "s1", "title": "Section One", "prompt": "Analyze {month} {year}"},
        {"id": "s2", "title": "Section Two", "prompt": "Review {month} {year}"},
    ],
    "dependencies": {},
}


class TestReportDetection:
    """Test that template_id in forwardedProps triggers report mode."""

    @patch("agents.shared.agui_server.REPORT_TABLE_NAME", "test-table")
    @patch("agents.shared.agui_server.AGENTCORE_MEMORY_ID", "")
    @patch("agents.shared.agui_server.reports.load_template")
    @patch("agents.shared.agui_server.reports.save_report")
    @patch("agents.shared.agui_server.reports.update_report_section")
    def test_report_mode_triggered_with_template_id(
        self, mock_update, mock_save, mock_load
    ):
        """template_id in forwardedProps triggers async report mode: SSE
        returns a pending marker immediately; actual section work happens
        on a background thread and writes section updates to DynamoDB via
        update_report_section. (No `report_enabled` flag — the presence
        of template_id is the trigger.)"""
        mock_load.return_value = SAMPLE_TEMPLATE
        mock_save.return_value = True
        mock_update.return_value = True

        app = create_agui_app(
            agent_builder=_make_agent_builder(),
            config={
                "agent_name": "test",
                "memory_enabled": False,
                "suggestions_enabled": False,
            },
        )
        client = TestClient(app)
        payload = _make_agui_payload(template_id="test_template")
        resp = client.post("/invocations", json=payload)

        assert resp.status_code == 200
        body = resp.text

        # SSE must carry the structured pending marker the frontend parses.
        assert "<report-pending" in body
        assert "report_id=" in body
        # Sections must NOT stream — they run on the background worker.
        assert "Mock section content" not in body

        # Wait for the background worker to finish its writes.
        _wait_for_report_workers()

        mock_load.assert_called_once()
        # save_report is called twice by the worker: once to write the
        # pending shell, once to flip the row to complete (plus an
        # intermediate "in_progress" flip before the loop). Just require
        # at least the pending + final writes.
        assert mock_save.call_count >= 2
        # One update_report_section per template section (2 here).
        assert mock_update.call_count == len(SAMPLE_TEMPLATE["sections"])

    @patch("agents.shared.agui_server.REPORT_TABLE_NAME", "test-table")
    @patch("agents.shared.agui_server.AGENTCORE_MEMORY_ID", "")
    @patch("agents.shared.reports.load_template")
    def test_report_mode_not_triggered_without_template_id(self, mock_load):
        """Without template_id, normal chat flow should be used (not report mode)."""
        app = create_agui_app(
            agent_builder=_make_agent_builder(),
            config={
                "agent_name": "test",
                "memory_enabled": False,
                "suggestions_enabled": False,
            },
        )
        client = TestClient(app)
        payload = _make_agui_payload(template_id=None)

        # This will fail in the normal AG-UI path (no real agent), but
        # the key assertion is that load_template was NOT called
        resp = client.post("/invocations", json=payload)
        mock_load.assert_not_called()

class TestReportStreaming:
    """Test the report streaming event generation."""

    @patch("agents.shared.agui_server.REPORT_TABLE_NAME", "test-table")
    @patch("agents.shared.agui_server.AGENTCORE_MEMORY_ID", "mem-1")
    @patch("agents.shared.agui_server.save_user_message")
    @patch("agents.shared.agui_server.save_assistant_message")
    @patch("agents.shared.agui_server.reports.load_template")
    @patch("agents.shared.agui_server.reports.save_report")
    @patch("agents.shared.agui_server.reports.update_report_section")
    def test_memory_saved_for_report(
        self,
        mock_update,
        mock_save_report,
        mock_load,
        mock_save_assistant,
        mock_save_user,
    ):
        """User message saves synchronously (before the HTTP response).
        A tiny <artifact> kickoff marker also saves at kickoff so the
        assistant turn is durable across reload — the full report body
        is NOT pushed to AgentCore Memory (it lives in DynamoDB and is
        rehydrated on demand by getReport)."""
        mock_load.return_value = SAMPLE_TEMPLATE
        mock_save_report.return_value = True
        mock_update.return_value = True

        app = create_agui_app(
            agent_builder=_make_agent_builder(),
            config={
                "agent_name": "test",
                "memory_enabled": True,
                "suggestions_enabled": False,
            },
        )
        client = TestClient(app)
        payload = _make_agui_payload(template_id="test_template")
        resp = client.post("/invocations", json=payload)

        assert resp.status_code == 200
        # User message and the kickoff marker both save before the HTTP
        # response returns, so they can be asserted without waiting.
        mock_save_user.assert_called_once()
        mock_save_assistant.assert_called_once()
        kickoff_text = mock_save_assistant.call_args[0][3]
        assert "<report-pending" in kickoff_text
        assert "report_id=" in kickoff_text
        # Body content must NOT be in Memory — that's the whole point.
        assert "<report-body>" not in kickoff_text
        # And we must NOT use <artifact> as the kickoff tag — that would
        # render as a completed report immediately.
        assert "<artifact>" not in kickoff_text

        # The background worker must NOT push another Memory event.
        _wait_for_report_workers()
        mock_save_assistant.assert_called_once()

    @patch("agents.shared.agui_server.REPORT_TABLE_NAME", "test-table")
    @patch("agents.shared.agui_server.AGENTCORE_MEMORY_ID", "")
    @patch("agents.shared.reports.load_template")
    def test_template_not_found_streams_error(self, mock_load):
        """When template is not found, an error event should be streamed."""
        mock_load.return_value = None

        app = create_agui_app(
            agent_builder=_make_agent_builder(),
            config={
                "agent_name": "test",
                "memory_enabled": False,
                "suggestions_enabled": False,
            },
        )
        client = TestClient(app)
        payload = _make_agui_payload(template_id="nonexistent")
        resp = client.post("/invocations", json=payload)

        assert resp.status_code == 200
        assert "not found" in resp.text

    @patch("agents.shared.agui_server.REPORT_TABLE_NAME", "test-table")
    @patch("agents.shared.agui_server.AGENTCORE_MEMORY_ID", "")
    @patch("agents.shared.agui_server.reports.load_report")
    @patch("agents.shared.agui_server.reports.save_report")
    @patch("agents.shared.agui_server.reports.update_report_section")
    def test_edit_mode_creates_new_version(
        self, mock_update, mock_save, mock_load
    ):
        """When forwardedProps carries edit_report_id, the handler loads
        the parent, creates a new versioned record, and emits a
        <report-pending> marker carrying the new report_id + version."""
        parent = {
            "report_id": "report_parent",
            "user_id": "user1",
            "title": "Monthly CloudOps - Feb 2026",
            "month": "Feb",
            "year": "2026",
            "version": 1,
            "sections": [
                {"id": "s1", "title": "Section One", "content": "Original text", "status": "complete"},
                {"id": "s2", "title": "Section Two", "content": "More text", "status": "complete"},
            ],
        }
        mock_load.return_value = parent
        mock_save.return_value = True
        mock_update.return_value = True

        # Agent that echoes back the full report with both headings.
        def edit_builder(payload):
            mock_agent = MagicMock()
            mock_agent.return_value = MagicMock(
                message={
                    "content": [
                        {
                            "text": (
                                "## Section One\n\nBrand new text.\n\n"
                                "## Section Two\n\nStill the same.\n"
                            )
                        }
                    ]
                }
            )
            return mock_agent, "system prompt", None

        app = create_agui_app(
            agent_builder=edit_builder,
            config={
                "agent_name": "test",
                "memory_enabled": False,
                "suggestions_enabled": False,
            },
        )
        client = TestClient(app)

        # Request: user types "shorten section one", with edit_report_id set.
        payload = _make_agui_payload(user_msg="shorten section one")
        payload["forwardedProps"]["edit_report_id"] = "report_parent"
        resp = client.post("/invocations", json=payload)

        assert resp.status_code == 200
        body = resp.text
        # The marker is embedded inside a JSON-encoded SSE event, so
        # quotes are backslash-escaped.
        assert "<report-pending" in body
        assert 'version=\\"2\\"' in body
        assert 'parent_report_id=\\"report_parent\\"' in body

        _wait_for_report_workers()

        # Edit creates one save_report for the pending shell + one final save.
        # Plus per-section updates (2 here).
        assert mock_save.call_count >= 2
        assert mock_update.call_count == 2

        # Final save_report captures the revised section bodies.
        final_call = mock_save.call_args_list[-1]
        saved_record = final_call.args[0]
        assert saved_record["version"] == 2
        assert saved_record["parent_report_id"] == "report_parent"
        # Agent edited section one; second section comes through because
        # the response emitted it too.
        section_contents = {s["title"]: s["content"] for s in saved_record["sections"]}
        assert section_contents["Section One"] == "Brand new text."

    @patch("agents.shared.agui_server.REPORT_TABLE_NAME", "test-table")
    @patch("agents.shared.agui_server.AGENTCORE_MEMORY_ID", "")
    @patch("agents.shared.agui_server.reports.load_report")
    def test_edit_mode_missing_parent_streams_error(self, mock_load):
        """If the parent_report_id doesn't exist, return a plain error
        SSE — don't start a worker."""
        mock_load.return_value = None

        app = create_agui_app(
            agent_builder=_make_agent_builder(),
            config={
                "agent_name": "test",
                "memory_enabled": False,
                "suggestions_enabled": False,
            },
        )
        client = TestClient(app)
        payload = _make_agui_payload(user_msg="tighten it")
        payload["forwardedProps"]["edit_report_id"] = "does_not_exist"
        resp = client.post("/invocations", json=payload)

        assert resp.status_code == 200
        assert "not found" in resp.text

    @patch("agents.shared.agui_server.REPORT_TABLE_NAME", "test-table")
    @patch("agents.shared.agui_server.AGENTCORE_MEMORY_ID", "")
    @patch("agents.shared.agui_server.reports.load_report")
    @patch("agents.shared.agui_server.reports.save_report")
    @patch("agents.shared.agui_server.reports.update_report_section")
    def test_edit_preserves_unchanged_sections_when_agent_skips_them(
        self, mock_update, mock_save, mock_load
    ):
        """Fallback behaviour: if the agent only emits one section's
        heading, untouched sections fall back to the parent content so
        nothing is silently lost."""
        parent = {
            "report_id": "parent_x",
            "user_id": "u",
            "title": "T",
            "month": "M",
            "year": "Y",
            "version": 1,
            "sections": [
                {"id": "s1", "title": "Alpha", "content": "ORIGINAL ALPHA", "status": "complete"},
                {"id": "s2", "title": "Beta", "content": "ORIGINAL BETA", "status": "complete"},
            ],
        }
        mock_load.return_value = parent
        mock_save.return_value = True
        mock_update.return_value = True

        def partial_builder(payload):
            mock_agent = MagicMock()
            mock_agent.return_value = MagicMock(
                message={
                    "content": [
                        {"text": "## Alpha\n\nREWRITTEN ALPHA\n"}
                    ]
                }
            )
            return mock_agent, "system prompt", None

        app = create_agui_app(
            agent_builder=partial_builder,
            config={
                "agent_name": "test",
                "memory_enabled": False,
                "suggestions_enabled": False,
            },
        )
        client = TestClient(app)
        payload = _make_agui_payload(user_msg="just alpha")
        payload["forwardedProps"]["edit_report_id"] = "parent_x"
        resp = client.post("/invocations", json=payload)
        assert resp.status_code == 200
        _wait_for_report_workers()

        final_call = mock_save.call_args_list[-1]
        saved_record = final_call.args[0]
        contents = {s["title"]: s["content"] for s in saved_record["sections"]}
        assert contents["Alpha"] == "REWRITTEN ALPHA"
        # Beta wasn't in the response, so it's preserved from the parent.
        assert contents["Beta"] == "ORIGINAL BETA"

    @patch("agents.shared.agui_server.REPORT_TABLE_NAME", "test-table")
    @patch("agents.shared.agui_server.AGENTCORE_MEMORY_ID", "")
    @patch("agents.shared.agui_server.reports.load_template")
    @patch("agents.shared.agui_server.reports.save_report")
    @patch("agents.shared.agui_server.reports.update_report_section")
    def test_failed_section_persists_error(
        self, mock_update, mock_save, mock_load
    ):
        """Failed sections are persisted to DynamoDB via update_report_section
        with status='error' and the exception message — verifying
        per-section persistence gives us crash-survival."""
        mock_load.return_value = SAMPLE_TEMPLATE
        mock_save.return_value = True
        mock_update.return_value = True

        # Agent builder that fails on the second section.
        call_count = {"n": 0}

        def failing_builder(payload):
            call_count["n"] += 1
            mock_agent = MagicMock()
            if call_count["n"] == 2:
                mock_agent.side_effect = RuntimeError("API timeout")
            else:
                mock_agent.return_value = MagicMock(
                    message={"content": [{"text": "OK"}]}
                )
            return mock_agent, "system prompt", None

        app = create_agui_app(
            agent_builder=failing_builder,
            config={
                "agent_name": "test",
                "memory_enabled": False,
                "suggestions_enabled": False,
            },
        )
        client = TestClient(app)
        payload = _make_agui_payload(template_id="test_template")
        resp = client.post("/invocations", json=payload)

        assert resp.status_code == 200
        _wait_for_report_workers()

        # Two sections → two update_report_section calls. One succeeds,
        # one carries status='error' with the exception message.
        statuses = [
            call.kwargs["section"]["status"] for call in mock_update.call_args_list
        ]
        errors = [
            call.kwargs["section"]["error"] for call in mock_update.call_args_list
        ]
        assert "error" in statuses
        assert any("API timeout" in e for e in errors)


# ---------------------------------------------------------------------------
# Free-form report mode — synthetic template construction
# ---------------------------------------------------------------------------


class TestBuildFreeformTemplate:
    """The free-form composer toggle reuses the templated report flow by
    synthesising a single-section template from the user's prompt. The
    template must be valid input for ``generate_report_sections`` so the
    DDB row, ``<report-pending>`` marker, edit lineage, and ``get_report``
    follow-ups all work the same as a real template.
    """

    def test_basic_prompt_produces_valid_template(self):
        from agents.shared.agui_server import _build_freeform_template

        tpl = _build_freeform_template("show me the top 3 cost drivers")
        assert tpl["sections"]
        assert len(tpl["sections"]) == 1
        section = tpl["sections"][0]
        assert section["id"] == "report"
        # Section prompt leads with the user's prompt verbatim, then appends a
        # title-heading instruction so the report always opens with an H1
        # (which the freeform title-derivation block lifts into the title).
        assert section["prompt"].startswith("show me the top 3 cost drivers")
        assert "Markdown H1 title" in section["prompt"]
        # Dependencies map must exist (even empty) so build_dependency_graph works.
        assert tpl["dependencies"] == {}

    def test_section_prompt_appends_title_instruction(self):
        from agents.shared.agui_server import (
            _FREEFORM_TITLE_INSTRUCTION,
            _build_freeform_template,
        )

        tpl = _build_freeform_template("april spend")
        prompt = tpl["sections"][0]["prompt"]
        # The instruction is appended after the user's prompt.
        assert prompt == "april spend" + _FREEFORM_TITLE_INSTRUCTION
        assert prompt.endswith(_FREEFORM_TITLE_INSTRUCTION)
        # No curly braces — reports.py runs str.format_map on the prompt, so a
        # stray brace in the instruction would raise KeyError at generation.
        assert "{" not in _FREEFORM_TITLE_INSTRUCTION
        assert "}" not in _FREEFORM_TITLE_INSTRUCTION

    def test_title_derived_from_first_line(self):
        from agents.shared.agui_server import _build_freeform_template

        tpl = _build_freeform_template(
            "Q1 cost review\nfor the FinOps team meeting next week"
        )
        # Multi-line prompts use only the first line for the report title;
        # subsequent lines stay in the section prompt for the agent.
        assert tpl["name"] == "Q1 cost review"

    def test_long_title_truncated(self):
        from agents.shared.agui_server import _build_freeform_template

        long_prompt = "x" * 200
        tpl = _build_freeform_template(long_prompt)
        # Title cap prevents sidebar / ReportCard layout breakage.
        assert len(tpl["name"]) <= 80
        assert tpl["name"].endswith("...")

    def test_empty_prompt_falls_back_to_default_title(self):
        from agents.shared.agui_server import _build_freeform_template

        tpl = _build_freeform_template("")
        assert tpl["name"] == "Custom Report"
        # Empty user prompt still carries the appended title instruction.
        from agents.shared.agui_server import _FREEFORM_TITLE_INSTRUCTION

        assert tpl["sections"][0]["prompt"] == _FREEFORM_TITLE_INSTRUCTION

    def test_trailing_punctuation_stripped(self):
        from agents.shared.agui_server import _build_freeform_template

        tpl = _build_freeform_template("Show me April spend!")
        # Cleaner sidebar rendering when the prompt ends in a sentence stop.
        assert tpl["name"] == "Show me April spend"
