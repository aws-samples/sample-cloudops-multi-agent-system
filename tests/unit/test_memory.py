"""Unit tests for agents.shared.memory — manual memory management."""

import re
from unittest.mock import MagicMock, patch

import pytest

from agents.shared.memory import (
    _MAX_EVENT_TEXT,
    _fit_event_text,
    build_enriched_text,
    load_history,
    save_assistant_message,
    save_user_message,
)


class TestBuildEnrichedText:
    def test_text_only(self):
        segments = [{"type": "text", "value": "Hello world"}]
        assert build_enriched_text(segments) == "Hello world"

    def test_thinking_wrapped_in_tags(self):
        segments = [{"type": "thinking", "value": "reasoning here"}]
        assert build_enriched_text(segments) == "<think>reasoning here</think>"

    def test_tool_wrapped_in_tags(self):
        segments = [{"type": "tool", "value": '{"name":"test"}'}]
        assert build_enriched_text(segments) == '<tool>{"name":"test"}</tool>'

    def test_suggestions_wrapped_in_tags(self):
        segments = [{"type": "suggestions", "value": '["q1","q2"]'}]
        assert build_enriched_text(segments) == '<suggestions>["q1","q2"]</suggestions>'

    def test_interleaved_segments(self):
        segments = [
            {"type": "thinking", "value": "hmm"},
            {"type": "tool", "value": '{"n":"t"}'},
            {"type": "text", "value": "result"},
            {"type": "suggestions", "value": '["a"]'},
        ]
        result = build_enriched_text(segments)
        assert "<think>hmm</think>" in result
        assert '<tool>{"n":"t"}</tool>' in result
        assert "result" in result
        assert '<suggestions>["a"]</suggestions>' in result

    def test_consecutive_text_merged(self):
        segments = [
            {"type": "text", "value": "Hello "},
            {"type": "text", "value": "world"},
        ]
        result = build_enriched_text(segments)
        assert "Hello world" in result

    def test_empty_segments(self):
        assert build_enriched_text([]) == ""


class TestLoadHistory:
    def test_returns_empty_when_no_memory_id(self):
        assert load_history("", "sess", "actor") == []

    def test_returns_empty_when_no_session_id(self):
        assert load_history("mem", "", "actor") == []

    def test_returns_empty_when_no_actor_id(self):
        assert load_history("mem", "sess", "") == []

    @patch("agents.shared.memory._get_client")
    def test_strips_artifact_tags(self, mock_get):
        client = MagicMock()
        client.list_events.return_value = {
            "events": [
                {
                    "payload": [
                        {
                            "conversational": {
                                "role": "ASSISTANT",
                                "content": {"text": "Hello<artifact>meta</artifact>"},
                            }
                        }
                    ]
                }
            ]
        }
        mock_get.return_value = client
        msgs = load_history("mem", "sess", "actor")
        assert len(msgs) == 1
        assert "<artifact>" not in msgs[0]["content"][0]["text"]

    @patch("agents.shared.memory._get_client")
    def test_strips_visualizer_state_from_model_history(self, mock_get):
        # The compact topology JSON is for the browser card, NOT the model —
        # leaving it in would feed 30-80KB of topology into every follow-up.
        client = MagicMock()
        client.list_events.return_value = {
            "events": [
                {
                    "payload": [
                        {
                            "conversational": {
                                "role": "ASSISTANT",
                                "content": {"text": "Here is the setup.\n<visualizer-state>{\"topology\":{\"connections\":[1,2,3]}}</visualizer-state>"},
                            }
                        }
                    ]
                }
            ]
        }
        mock_get.return_value = client
        msgs = load_history("mem", "sess", "actor")
        assert len(msgs) == 1
        assert "<visualizer-state>" not in msgs[0]["content"][0]["text"]
        assert "Here is the setup." in msgs[0]["content"][0]["text"]

    @patch("agents.shared.memory._get_client")
    def test_strips_suggestions_tags(self, mock_get):
        client = MagicMock()
        client.list_events.return_value = {
            "events": [
                {
                    "payload": [
                        {
                            "conversational": {
                                "role": "ASSISTANT",
                                "content": {
                                    "text": 'Answer\n<suggestions>["q"]</suggestions>'
                                },
                            }
                        }
                    ]
                }
            ]
        }
        mock_get.return_value = client
        msgs = load_history("mem", "sess", "actor")
        assert "<suggestions>" not in msgs[0]["content"][0]["text"]

    @patch("agents.shared.memory._get_client")
    def test_preserves_tool_tags(self, mock_get):
        # `<tool>` tags are evidence of prior-turn tool calls — they let the
        # model resolve references like "the diagram above" or "that chart"
        # via the no-fabrication preamble's vocabulary clause. Stripping them
        # was the old behaviour (display-layer cleanup); now they're kept.
        client = MagicMock()
        client.list_events.return_value = {
            "events": [
                {
                    "payload": [
                        {
                            "conversational": {
                                "role": "ASSISTANT",
                                "content": {"text": "<tool>{}</tool>Real answer"},
                            }
                        }
                    ]
                }
            ]
        }
        mock_get.return_value = client
        msgs = load_history("mem", "sess", "actor")
        assert "<tool>{}</tool>" in msgs[0]["content"][0]["text"]
        assert "Real answer" in msgs[0]["content"][0]["text"]

    @patch("agents.shared.memory._get_client")
    def test_preserves_report_body_tags(self, mock_get):
        client = MagicMock()
        client.list_events.return_value = {
            "events": [
                {
                    "payload": [
                        {
                            "conversational": {
                                "role": "ASSISTANT",
                                "content": {
                                    "text": "<report-body>Report content</report-body>"
                                },
                            }
                        }
                    ]
                }
            ]
        }
        mock_get.return_value = client
        msgs = load_history("mem", "sess", "actor")
        assert "<report-body>" in msgs[0]["content"][0]["text"]

    @patch("agents.shared.memory._get_client")
    def test_strips_report_request_marker(self, mock_get):
        # The frontend-only report-mode marker on a USER turn must never reach
        # the model — load_history strips it (like <artifact>/<suggestions>).
        client = MagicMock()
        client.list_events.return_value = {
            "events": [
                {
                    "payload": [
                        {
                            "conversational": {
                                "role": "USER",
                                "content": {"text": "What are my costs?\n<report-request/>"},
                            }
                        }
                    ]
                }
            ]
        }
        mock_get.return_value = client
        msgs = load_history("mem", "sess", "actor")
        assert len(msgs) == 1
        text = msgs[0]["content"][0]["text"]
        assert "<report-request/>" not in text
        assert text == "What are my costs?"

    @patch("agents.shared.memory._get_client")
    def test_returns_empty_on_not_found(self, mock_get):
        client = MagicMock()
        client.list_events.side_effect = Exception("Session not found")
        mock_get.return_value = client
        assert load_history("mem", "sess", "actor") == []


class TestSaveUserMessage:
    @patch("agents.shared.memory._get_client")
    def test_calls_create_event(self, mock_get):
        client = MagicMock()
        mock_get.return_value = client
        save_user_message("mem", "sess", "actor", "hello")
        client.create_event.assert_called_once()
        call_kwargs = client.create_event.call_args[1]
        assert call_kwargs["memoryId"] == "mem"
        assert call_kwargs["sessionId"] == "sess"
        assert call_kwargs["actorId"] == "actor"

    def test_skips_when_no_memory_id(self):
        # Should not raise
        save_user_message("", "sess", "actor", "hello")

    @patch("agents.shared.memory._get_client")
    def test_no_report_marker_by_default(self, mock_get):
        client = MagicMock()
        mock_get.return_value = client
        save_user_message("mem", "sess", "actor", "hello")
        saved = client.create_event.call_args[1]["payload"][0]["conversational"]["content"]["text"]
        assert saved == "hello"
        assert "<report-request/>" not in saved

    @patch("agents.shared.memory._get_client")
    def test_appends_report_marker_when_is_report(self, mock_get):
        client = MagicMock()
        mock_get.return_value = client
        save_user_message("mem", "sess", "actor", "hello", is_report=True)
        saved = client.create_event.call_args[1]["payload"][0]["conversational"]["content"]["text"]
        assert saved == "hello\n<report-request/>"


class TestSaveAssistantMessage:
    @patch("agents.shared.memory._get_client")
    def test_calls_create_event(self, mock_get):
        client = MagicMock()
        mock_get.return_value = client
        save_assistant_message("mem", "sess", "actor", "response text")
        client.create_event.assert_called_once()

    def test_skips_empty_text(self):
        # Should not raise
        save_assistant_message("mem", "sess", "actor", "   ")


class TestFitEventText:
    """AgentCore Memory rejects events over 100k chars; oversized assistant
    turns (large report follow-ups) used to be dropped whole, so the turn
    vanished on history reload. _fit_event_text keeps them under the cap."""

    def test_small_text_untouched(self):
        t = "a short answer"
        assert _fit_event_text(t) == t

    def test_trims_largest_tool_blocks_first(self):
        # A modest report body + one enormous tool output over the cap.
        body = "<report-body>" + ("x" * 5000) + "</report-body>"
        huge_tool = "<tool>" + ("y" * 150_000) + "</tool>"
        out = _fit_event_text(body + huge_tool)
        assert len(out) <= _MAX_EVENT_TEXT
        assert "<report-body>" in out and "</report-body>" in out  # body preserved
        assert '<tool>{"truncated":true}</tool>' in out            # tool sacrificed
        # No half-open tags left for the frontend parser.
        assert out.count("<tool>") == out.count("</tool>")

    def test_hard_cut_when_body_itself_too_big(self):
        giant = "<report-body>" + ("z" * 200_000) + "</report-body>"
        out = _fit_event_text(giant)
        assert len(out) <= _MAX_EVENT_TEXT
        assert "truncated for storage" in out

    def test_oversized_assistant_message_still_saves(self, monkeypatch):
        client = MagicMock()
        monkeypatch.setattr("agents.shared.memory._get_client", lambda region="us-east-1": client)
        save_assistant_message("mem", "sess", "actor", "<tool>" + ("q" * 200_000) + "</tool>", "us-east-1")
        # create_event was called (not skipped) and with text under the cap.
        assert client.create_event.called
        saved = client.create_event.call_args.kwargs["payload"][0]["conversational"]["content"]["text"]
        assert len(saved) <= _MAX_EVENT_TEXT


class TestFitEventPreservesUI:
    """The trim protects the compact <visualizer-state> the card rehydrates from,
    and only sacrifices bulky raw <tool> trace blocks."""

    def test_visualizer_state_tag_survives_trim(self):
        from agents.shared.memory import _fit_event_text, _MAX_EVENT_TEXT
        viz = "<visualizer-state>" + ("v" * 32000) + "</visualizer-state>"
        huge_tool = "<tool>" + ("y" * 150000) + "</tool>"
        out = _fit_event_text("Here is the demo. " + huge_tool + "\n" + viz)
        assert len(out) <= _MAX_EVENT_TEXT
        assert viz in out                                   # card state intact
        assert '<tool>{"truncated":true}</tool>' in out     # trace sacrificed
        assert out.count("<tool>") == out.count("</tool>")

    def test_visualizer_state_survives_even_hard_cut(self):
        from agents.shared.memory import _fit_event_text, _MAX_EVENT_TEXT
        # No <tool> blocks to trim; a giant report body + trailing viz-state.
        body = "<report-body>" + ("z" * 200000) + "</report-body>"
        viz = "<visualizer-state>" + ("v" * 30000) + "</visualizer-state>"
        out = _fit_event_text(body + "\n" + viz)
        assert len(out) <= _MAX_EVENT_TEXT
        assert viz in out                                   # protected on hard-cut
        assert "truncated for storage" in out
