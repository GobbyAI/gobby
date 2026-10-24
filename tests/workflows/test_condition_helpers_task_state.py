"""Tests for projected task-state condition helpers."""

from __future__ import annotations

from typing import Any

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.workflows.condition_helpers import task_state_in
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers

pytestmark = pytest.mark.unit


def _manager(temp_db: HubDatabase) -> LocalTaskManager:
    """Create a task manager over the test database."""
    return LocalTaskManager(temp_db)


def _task(manager: LocalTaskManager, sample_project: dict[str, Any]) -> Task:
    """Create a minimal task for state-helper assertions."""
    return manager.create_task(
        project_id=sample_project["id"],
        title="Task state helper",
        validation_criteria="Test task completion is observable.",
    )


def test_task_state_in_projects_closed_and_escalated(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    manager = _manager(temp_db)
    closed = _task(manager, sample_project)
    escalated = _task(manager, sample_project)
    manager.close_task(closed.id, force=True)
    manager.escalate_task(escalated.id, reason="blocked")

    assert task_state_in(manager, closed.id, "closed") is True
    assert task_state_in(manager, escalated.id, "escalated") is True


def test_task_state_in_reports_a_new_task_as_open(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    manager = _manager(temp_db)
    task = _task(manager, sample_project)

    assert task_state_in(manager, task.id, "open") is True


def test_helpers_return_false_without_task_manager() -> None:
    assert task_state_in(None, "#42", "open") is False


def test_task_state_in_returns_false_for_invalid_uuid_bytes(temp_db: HubDatabase) -> None:
    """Invalid UUID bytes normalize safely and fail closed."""
    assert task_state_in(_manager(temp_db), b"short", "open") is False


def test_safe_evaluator_exposes_task_state_in(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    manager = _manager(temp_db)
    task = _task(manager, sample_project)
    ctx: dict[str, Any] = {"variables": {"task_id": task.id}}
    helpers = build_condition_helpers(task_manager=manager, context=ctx)
    evaluator = SafeExpressionEvaluator(ctx, helpers)

    assert evaluator.evaluate("task_state_in(variables.task_id, 'open')") is True


def test_safe_evaluator_stubs_task_state_helpers_without_manager() -> None:
    ctx: dict[str, Any] = {"variables": {}}
    helpers = build_condition_helpers(task_manager=None, context=ctx)
    evaluator = SafeExpressionEvaluator(ctx, helpers)

    assert evaluator.evaluate("task_state_in('#42', 'open')") is False


def test_no_commit_close_requires_a_task_manager() -> None:
    helpers = build_condition_helpers(task_manager=None)
    evaluator = SafeExpressionEvaluator({}, helpers)

    assert evaluator.evaluate("task_has_no_commits('#42')") is False
