"""Tests for verification evidence variable helpers."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from gobby.workflows.verification_evidence import (
    VERIFICATION_EVIDENCE_TYPE_MANUAL_DIFF_REVIEW,
    append_verification_evidence,
    validate_verification_evidence,
)

pytestmark = pytest.mark.unit


def test_append_verification_evidence_warns_for_malformed_existing_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Malformed stored evidence is dropped and logged before appending new evidence."""
    evidence = {
        "summary": "Reviewed diff",
        "evidence_type": VERIFICATION_EVIDENCE_TYPE_MANUAL_DIFF_REVIEW,
        "success": True,
    }
    malformed: Any = {"bad": "shape"}

    with caplog.at_level(logging.WARNING, logger="gobby.workflows.verification_evidence"):
        result = append_verification_evidence(malformed, evidence, session_id="sess-1")

    assert result == [evidence]
    assert "Ignoring malformed verification_evidence value" in caplog.text
    assert caplog.records[0].session_id == "sess-1"


def test_append_verification_evidence_preserves_metadata_fields() -> None:
    """Extra evidence metadata remains available to downstream observers."""
    evidence = {
        "command": "GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_x.py",
        "cwd": "/repo",
        "evidence_type": "validation_command",
        "scope": "focused",
        "success": True,
        "supports": ["task-1"],
        "task_id": "#15175",
        "tool_name": "pytest",
    }

    result = append_verification_evidence([], evidence)

    assert result == [evidence]


def test_validate_verification_evidence_requires_type_and_success() -> None:
    assert validate_verification_evidence({"summary": "Reviewed diff", "success": True}) == (
        "verification evidence requires a non-empty evidence_type"
    )
    assert (
        validate_verification_evidence(
            {
                "summary": "Reviewed diff",
                "evidence_type": VERIFICATION_EVIDENCE_TYPE_MANUAL_DIFF_REVIEW,
                "success": "yes",
            }
        )
        == "verification evidence requires a boolean success field"
    )
