"""Unit tests for agents.shared.memory — manual memory management."""

from unittest.mock import MagicMock, patch

import pytest

from agents.shared.memory import (
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


class TestMemoryEventSizeGuard:
    """Regression coverage for AgentCore Memory's 100,000-char per-event
    payload limit. A many-alarm CFN template inside `<cfn-artifact>` plus
    the surrounding `<tool>`/`<think>`/text segments routinely pushed the
    enriched text past the limit, the CreateEvent call failed with
    ValidationException, and the entire assistant turn was lost from
    history on reload — leaving the user prompt visible but no response.
    """

    @patch("agents.shared.memory._get_client")
    def test_strips_cfn_artifact_block_before_save(self, mock_get):
        client = MagicMock()
        mock_get.return_value = client
        big_yaml = "x" * 60_000
        text = (
            "Summary text.\n"
            f"<cfn-artifact title=\"big\">{big_yaml}</cfn-artifact>\n"
            "Trailing summary."
        )
        save_assistant_message("mem", "sess", "actor", text)
        saved = client.create_event.call_args[1]["payload"][0][
            "conversational"
        ]["content"]["text"]
        assert big_yaml not in saved
        assert "<cfn-artifact" not in saved
        assert "</cfn-artifact>" not in saved
        assert "Summary text." in saved
        assert "Trailing summary." in saved
        assert "ReportPanel" in saved

    @patch("agents.shared.memory._get_client")
    def test_strips_cfn_artifact_inside_tool_segment(self, mock_get):
        # The <cfn-artifact> can land anywhere in the enriched text —
        # including nested inside a sub-agent's saved <tool> output JSON.
        # The strip must catch every embedding layer.
        client = MagicMock()
        mock_get.return_value = client
        big_yaml = "y" * 70_000
        tool_payload = (
            '{"name":"cloudwatch-agent","input":{},"output":'
            f'"<cfn-artifact title=\\"x\\">{big_yaml}</cfn-artifact>"' "}"
        )
        text = f"Header\n<tool>{tool_payload}</tool>\nFooter"
        save_assistant_message("mem", "sess", "actor", text)
        saved = client.create_event.call_args[1]["payload"][0][
            "conversational"
        ]["content"]["text"]
        # The artifact body inside the tool's `output` field must be gone.
        assert big_yaml not in saved
        # The surrounding chat text and tool wrapper survive — only the
        # artifact body is replaced with the placeholder.
        assert "Header" in saved
        assert "Footer" in saved
        assert "<tool>" in saved
        assert len(saved) < 10_000

    @patch("agents.shared.memory._get_client")
    def test_preserves_short_text_unchanged(self, mock_get):
        client = MagicMock()
        mock_get.return_value = client
        text = (
            "Short answer with a <tool>{\"name\":\"x\"}</tool> trace and "
            "<suggestions>[\"q\"]</suggestions>."
        )
        save_assistant_message("mem", "sess", "actor", text)
        saved = client.create_event.call_args[1]["payload"][0][
            "conversational"
        ]["content"]["text"]
        # No `<cfn-artifact>` and well under the limit — must save verbatim.
        assert saved == text

    @patch("agents.shared.memory._get_client")
    def test_no_op_when_no_cfn_artifact_present(self, mock_get):
        client = MagicMock()
        mock_get.return_value = client
        text = "Just a regular response with <tool>{\"a\":1}</tool>."
        save_assistant_message("mem", "sess", "actor", text)
        saved = client.create_event.call_args[1]["payload"][0][
            "conversational"
        ]["content"]["text"]
        assert saved == text
