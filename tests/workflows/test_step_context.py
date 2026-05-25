"""Tests for active step workflow context helpers."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock

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


@pytest.mark.parametrize(
    ("definition_json", "expected_log"),
    [
        ("{not-json", "invalid JSON"),
        ('{"name": "workflow", "steps": "bad"}', "validation failed"),
    ],
)
def test_get_active_step_workflow_context_logs_malformed_definitions(
    definition_json: str,
    expected_log: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Malformed step workflow definitions are visible in warning logs."""
    instance_manager = MagicMock()
    instance_manager.get_active_instances.return_value = [
        SimpleNamespace(workflow_name="workflow", current_step="review")
    ]
    definition_manager = MagicMock()
    definition_manager.get_by_name.return_value = SimpleNamespace(
        workflow_type="step",
        definition_json=definition_json,
    )
    monkeypatch.setattr(
        "gobby.workflows.step_context.WorkflowInstanceManager",
        lambda _db: instance_manager,
    )
    monkeypatch.setattr(
        "gobby.workflows.step_context.LocalWorkflowDefinitionManager",
        lambda _db: definition_manager,
    )

    with caplog.at_level("WARNING", logger="gobby.workflows.step_context"):
        result = get_active_step_workflow_context(MagicMock(), "session-1")

    assert result is None
    assert "Skipping malformed step workflow definition workflow" in caplog.text
    assert expected_log in caplog.text
