"""Output redaction for sensitive AWS patterns.

Strips genuine SECRETS — access keys, cross-account external IDs, and role
session names — from LLM-generated text before it is persisted to memory or
saved reports. IDENTIFIERS (AWS account IDs, IAM/resource ARNs) are the
substance of a cloud-ops report, so they are shown by default; set
REDACT_IDENTIFIERS=true to scrub them too. Patterns are replaced with safe
placeholders so the response stays coherent.
"""

import os
import re

_AWS_ACCOUNT_ID = re.compile(r"\b\d{12}\b")

_IAM_ARN = re.compile(
    r"arn:aws[a-z\-]*:[a-z0-9\-]+:[a-z0-9\-]*:\d{12}:[a-zA-Z0-9\-_/:.+=@]+"
)

_ROLE_SESSION = re.compile(
    r"(?:RoleSessionName|roleSessionName|role_session_name)\s*[=:]\s*['\"]?[A-Za-z0-9_\-.]+"
)

_EXTERNAL_ID = re.compile(
    r"(?:ExternalId|externalId|external_id)\s*[=:]\s*['\"]?[A-Za-z0-9_\-]+"
)

_ACCESS_KEY = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")

_PLACEHOLDER = "[REDACTED]"


def _redact_identifiers_enabled() -> bool:
    """Whether AWS identifiers (account IDs and ARNs) should be redacted.

    OFF by default: this platform's reports and findings are ABOUT the user's
    own resources (e.g. "top accounts by spend", "which role is over-permissioned",
    tag-governance breakdowns). Account IDs and ARNs are the identifiers that make
    that output actionable — blanking them makes the data meaningless. They are
    NOT secrets (unlike access keys or external IDs, which are always stripped).

    Set REDACT_IDENTIFIERS=true for deployments that persist/share transcripts
    and want account numbers and resource ARNs scrubbed as well.
    """
    return os.environ.get("REDACT_IDENTIFIERS", "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def redact(text: str) -> str:
    """Remove sensitive AWS patterns from text, returning sanitized version.

    ALWAYS strips genuine secrets — access keys, external IDs, and role-session
    names. AWS account IDs and IAM/resource ARNs are IDENTIFIERS, not secrets,
    and are the substance of a cloud-ops report, so they are preserved unless
    REDACT_IDENTIFIERS is enabled (see _redact_identifiers_enabled).
    """
    if not text:
        return text
    # Genuine secrets / credentials — always redacted.
    text = _ACCESS_KEY.sub(_PLACEHOLDER, text)
    text = _EXTERNAL_ID.sub(_PLACEHOLDER, text)
    text = _ROLE_SESSION.sub(_PLACEHOLDER, text)
    # Identifiers — kept by default so reports stay meaningful; opt-in scrub.
    if _redact_identifiers_enabled():
        text = _IAM_ARN.sub(_PLACEHOLDER, text)
        text = _AWS_ACCOUNT_ID.sub(_PLACEHOLDER, text)
    return text
