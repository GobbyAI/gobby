"""Tests for active step workflow context helpers."""

from __future__ import annotations

import sqlite3

import pytest

from gobby.workflows.step_context import get_active_step_workflow_context

pytestmark = pytest.mark.unit


class _FailingDb:
    def fetchall(self, *_args: object, **_kwargs: object) -> list[object]:
        raise sqlite3.DatabaseError("database unavailable")


def test_get_active_step_workflow_context_propagates_db_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Synchronous DB driver failures propagate without duplicate local logging."""
    with pytest.raises(sqlite3.DatabaseError):
        get_active_step_workflow_context(_FailingDb(), "session-1")  # type: ignore[arg-type]

    assert not caplog.records
