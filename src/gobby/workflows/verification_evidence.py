"""Helpers for verification evidence session variables."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

MAX_VERIFICATION_EVIDENCE_ITEMS = 50
VERIFICATION_EVIDENCE_VARIABLE = "verification_evidence"
VERIFICATION_EVIDENCE_RECORDED_VARIABLE = "verification_evidence_recorded"
ERRORS_RESOLVED_VARIABLE = "errors_resolved"
VERIFICATION_EVIDENCE_TYPE_VALIDATION_COMMAND = "validation_command"
VERIFICATION_EVIDENCE_TYPE_MANUAL_DIFF_REVIEW = "manual_diff_review"
VERIFICATION_EVIDENCE_RESET_UPDATES = {
    VERIFICATION_EVIDENCE_RECORDED_VARIABLE: False,
    VERIFICATION_EVIDENCE_VARIABLE: [],
}
_EVIDENCE_TYPE_RE = re.compile(r"^[a-z][a-z0-9_:-]{1,63}$")
logger = logging.getLogger(__name__)


def append_verification_evidence(
    existing: list[Any] | None,
    evidence: dict[str, Any],
    *,
    session_id: str | None = None,
) -> list[Any]:
    """Append one evidence item, retaining only the newest entries."""
    if error := validate_verification_evidence(evidence):
        raise ValueError(error)
    if isinstance(existing, list):
        items = existing
    elif existing is None:
        items = []
    else:
        logger.warning(
            "Ignoring malformed verification_evidence value",
            extra={"stored_type": type(existing).__name__, "session_id": session_id},
        )
        items = []
    return [*items, evidence][-MAX_VERIFICATION_EVIDENCE_ITEMS:]


def validate_verification_evidence(evidence: Mapping[str, Any]) -> str | None:
    """Return a validation error for malformed verification evidence, if any."""
    evidence_type = evidence.get("evidence_type")
    if not isinstance(evidence_type, str) or not evidence_type.strip():
        return "verification evidence requires a non-empty evidence_type"
    if not _EVIDENCE_TYPE_RE.fullmatch(evidence_type.strip()):
        return "verification evidence evidence_type must be snake-case text"

    success = evidence.get("success")
    if not isinstance(success, bool):
        return "verification evidence requires a boolean success field"

    timestamp = evidence.get("timestamp")
    if timestamp is not None and not isinstance(timestamp, str):
        return "verification evidence timestamp must be a string when provided"

    command = evidence.get("command")
    summary = evidence.get("summary")
    if command is not None and not isinstance(command, str):
        return "verification evidence command must be a string when provided"
    if summary is not None and not isinstance(summary, str):
        return "verification evidence summary must be a string when provided"
    if not command and not summary:
        return "verification evidence requires command or summary"

    return None
