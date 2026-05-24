"""Tests for active step workflow context helpers."""

from __future__ import annotations

import logging
import sqlite3

import pytest

from gobby.workflows.step_context import get_active_step_workflow_context

pytestmark = pytest.mark.unit


class _FailingDb:
    def fetchall(self, *_args: object, **_kwargs: object) -> list[object]:
        raise sqlite3.DatabaseError("database unavailable")


def test_get_active_step_workflow_context_logs_db_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Synchronous DB driver failures are logged before being re-raised."""
    with caplog.at_level(logging.WARNING, logger="gobby.workflows.step_context"):
        with pytest.raises(sqlite3.DatabaseError):
            get_active_step_workflow_context(_FailingDb(), "session-1")  # type: ignore[arg-type]

    assert "Failed to read active step workflow context for session session-1" in caplog.text
