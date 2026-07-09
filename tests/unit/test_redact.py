"""Tests for agents.shared.redact.

Redaction policy: genuine SECRETS (access keys, external IDs, role-session
names) are ALWAYS stripped. IDENTIFIERS (account IDs, ARNs) are the substance
of a cloud-ops report and are shown by default, redacted only when the opt-in
REDACT_IDENTIFIERS flag is set.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "agents"))

from agents.shared.redact import redact


# --- Identifiers: shown by default (kept meaningful) ------------------------


def test_account_ids_not_redacted_by_default():
    # Bare account IDs are kept by default so cost/governance reports (which are
    # ABOUT the user's own accounts) stay meaningful. See REDACT_IDENTIFIERS.
    assert redact("Account 123456789012 has costs") == "Account 123456789012 has costs"


def test_arn_not_redacted_by_default():
    # ARNs (and their resource names) are identifiers, not secrets — the useful
    # part of a findings report. Kept by default so the report stays actionable.
    text = "Role arn:aws:iam::123456789012:role/CloudOpsAgent-CostExplorerTool is over-permissioned"
    assert redact(text) == text


# --- Identifiers: scrubbed when the opt-in flag is set ----------------------


def test_redacts_aws_account_id_when_enabled(monkeypatch):
    monkeypatch.setenv("REDACT_IDENTIFIERS", "true")
    assert redact("Account 123456789012 has costs") == "Account [REDACTED] has costs"


def test_redacts_iam_arn_when_enabled(monkeypatch):
    monkeypatch.setenv("REDACT_IDENTIFIERS", "true")
    text = "Role arn:aws:iam::123456789012:role/CloudOpsAgent-CostExplorerTool is used"
    result = redact(text)
    assert "arn:aws" not in result
    assert "[REDACTED]" in result


# --- Secrets: ALWAYS redacted regardless of the flag ------------------------


def test_redacts_access_key():
    akia_key = "AKIA" + "IOSFODNN7EXAMPLE"
    assert "[REDACTED]" in redact(f"Key is {akia_key}")
    temp_key = "ASIA" + "TESTKEY123456789"
    assert "[REDACTED]" in redact(f"Temp key {temp_key}")


def test_redacts_external_id():
    text = "ExternalId: my-secret-ext-id-12345"
    result = redact(text)
    assert "my-secret-ext-id" not in result


def test_redacts_role_session_name():
    text = "RoleSessionName=cloudops-deploy-session"
    result = redact(text)
    assert "cloudops-deploy-session" not in result


def test_secrets_redacted_even_with_identifiers_shown():
    # Default (identifiers shown): the ARN survives but the access key inside the
    # same string is still stripped — proves secret-vs-identifier split.
    text = (
        "Function arn:aws:lambda:us-east-1:123456789012:function:CostTool "
        "used key AKIA" + "IOSFODNN7EXAMPLE"
    )
    result = redact(text)
    assert "arn:aws:lambda" in result  # identifier preserved
    assert "123456789012" in result  # account id preserved
    assert "AKIAIOSFODNN7EXAMPLE" not in result  # secret stripped


# --- General behavior -------------------------------------------------------


def test_preserves_normal_text():
    text = "Your AWS costs increased by 15% last month due to EC2 usage."
    assert redact(text) == text


def test_handles_empty_and_none():
    assert redact("") == ""
    assert redact(None) is None


def test_all_patterns_scrubbed_when_identifiers_enabled(monkeypatch):
    # With the opt-in flag on, EVERYTHING sensitive is scrubbed at once.
    monkeypatch.setenv("REDACT_IDENTIFIERS", "true")
    text = (
        "Lambda arn:aws:lambda:us-east-1:123456789012:function:CostTool "
        "assumed role with ExternalId=abc-123 in account 987654321098"
    )
    result = redact(text)
    assert "123456789012" not in result
    assert "987654321098" not in result
    assert "arn:aws" not in result
    assert "abc-123" not in result


def test_does_not_redact_short_numbers():
    text = "Found 42 resources costing $1500 per month"
    assert redact(text) == text
