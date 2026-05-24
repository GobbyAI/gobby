"""Tests for verification evidence variable helpers."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from gobby.workflows.verification_evidence import append_verification_evidence

pytestmark = pytest.mark.unit


def test_append_verification_evidence_warns_for_malformed_existing_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Malformed stored evidence is dropped and logged before appending new evidence."""
    evidence = {"summary": "Reviewed diff", "success": True}
    malformed: Any = {"bad": "shape"}

    with caplog.at_level(logging.WARNING, logger="gobby.workflows.verification_evidence"):
        result = append_verification_evidence(malformed, evidence, session_id="sess-1")

    assert result == [evidence]
    assert "Ignoring malformed verification_evidence value" in caplog.text
    assert caplog.records[0].session_id == "sess-1"
