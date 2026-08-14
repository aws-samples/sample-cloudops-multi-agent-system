"""Tests for typed CloudWatch template artifact extraction."""

from __future__ import annotations

import json

from agents.shared.agent_base import (
    _artifact_acknowledgement,
    _extract_cloudformation_artifact,
)


def _assembler_result(payload: dict) -> dict:
    return {
        "status": "success",
        "toolUseId": "tool-1",
        "content": [{"text": json.dumps(payload)}],
    }


def test_extracts_typed_artifact_from_raw_assembler_result():
    template_yaml = "AWSTemplateFormatVersion: '2010-09-09'\nResources: {}\n"
    artifact = _extract_cloudformation_artifact(
        _assembler_result(
            {
                "template_yaml": template_yaml,
                "summary": {"alarm_count": 2, "logical_ids": ["Errors", "Latency"]},
            }
        )
    )

    assert artifact == {
        "kind": "cloudformation-template",
        "title": "CloudWatch alarms (2)",
        "template_yaml": template_yaml,
        "summary": {"alarm_count": 2, "logical_ids": ["Errors", "Latency"]},
    }


def test_model_acknowledgement_excludes_template_yaml():
    template_yaml = "secret-template-body"
    artifact = _extract_cloudformation_artifact(
        _assembler_result({"template_yaml": template_yaml, "summary": {}})
    )

    acknowledgement = _artifact_acknowledgement(artifact)
    assert template_yaml not in json.dumps(acknowledgement)
    assert acknowledgement["artifact"]["kind"] == "cloudformation-template"
    assert acknowledgement["artifact"]["summary"] == {}


def test_does_not_capture_unsuccessful_or_non_template_result():
    assert (
        _extract_cloudformation_artifact(
            {"status": "error", "content": [{"text": '{"error":"bad request"}'}]}
        )
        is None
    )
    assert (
        _extract_cloudformation_artifact(
            _assembler_result({"summary": {"alarm_count": 0}})
        )
        is None
    )
