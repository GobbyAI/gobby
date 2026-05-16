"""Tests for projected task-state condition helpers."""

from __future__ import annotations

import pytest

from gobby.storage.tasks import LocalTaskManager
from gobby.workflows.condition_helpers import task_state_in
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers

pytestmark = pytest.mark.unit


def _manager(temp_db) -> LocalTaskManager:
    """Create a task manager over the test database."""
    return LocalTaskManager(temp_db)


def _task(manager: LocalTaskManager, sample_project: dict):
    """Create a minimal task for state-helper assertions."""
    return manager.create_task(project_id=sample_project["id"], title="Task state helper")


def _start_development_stage(manager: LocalTaskManager, task_id: str) -> None:
    manager.initialize_task_manifest(task_id)
    manager.stage_states.start_stage(task_id, "development", by_session_id=None)


def test_task_state_in_matches_current_stage_state(temp_db, sample_project) -> None:
    manager = _manager(temp_db)
    task = _task(manager, sample_project)
    _start_development_stage(manager, task.id)
    manager.submit_for_review(task.id)

    assert task_state_in(manager, task.id, "needs_review", "closed") is True
    assert task_state_in(manager, task.id, "ready", "review_approved") is False


def test_task_state_in_projects_closed_and_escalated(temp_db, sample_project) -> None:
    manager = _manager(temp_db)
    closed = _task(manager, sample_project)
    escalated = _task(manager, sample_project)
    manager.close_task(closed.id, force=True)
    manager.escalate_task(escalated.id, reason="blocked")

    assert task_state_in(manager, closed.id, "closed") is True
    assert task_state_in(manager, escalated.id, "escalated") is True


def test_task_state_in_uses_real_stage_native_task_fields(temp_db, sample_project) -> None:
    manager = _manager(temp_db)
    task = _task(manager, sample_project)

    assert not hasattr(task, "status")
    assert task.closed_at is None
    assert task.is_escalated is False
    assert task_state_in(manager, task.id, "ready") is True

    _start_development_stage(manager, task.id)
    in_progress = manager.get_task(task.id)
    assert not hasattr(in_progress, "status")
    assert task_state_in(manager, task.id, "in_progress") is True

    manager.escalate_task(task.id, reason="blocked")
    escalated = manager.get_task(task.id)
    assert not hasattr(escalated, "status")
    assert escalated.is_escalated is True
    assert task_state_in(manager, task.id, "escalated") is True


def test_task_state_in_defaults_to_ready_without_current_stage(temp_db, sample_project) -> None:
    manager = _manager(temp_db)
    task = _task(manager, sample_project)
    temp_db.execute("DELETE FROM task_stage_states WHERE task_id = ?", (task.id,))

    assert task_state_in(manager, task.id, "ready") is True


def test_helpers_return_false_without_task_manager() -> None:
    assert task_state_in(None, "#42", "ready") is False


def test_task_state_in_returns_false_for_invalid_uuid_bytes(temp_db) -> None:
    """Invalid UUID bytes normalize safely and fail closed."""
    assert task_state_in(_manager(temp_db), b"short", "ready") is False


def test_safe_evaluator_exposes_task_state_in(temp_db, sample_project) -> None:
    manager = _manager(temp_db)
    task = _task(manager, sample_project)
    ctx = {"variables": {"task_id": task.id}}
    helpers = build_condition_helpers(task_manager=manager, context=ctx)
    evaluator = SafeExpressionEvaluator(ctx, helpers)

    assert evaluator.evaluate("task_state_in(variables.task_id, 'ready')") is True


def test_safe_evaluator_stubs_task_state_helpers_without_manager() -> None:
    ctx = {"variables": {}}
    helpers = build_condition_helpers(task_manager=None, context=ctx)
    evaluator = SafeExpressionEvaluator(ctx, helpers)

    assert evaluator.evaluate("task_state_in('#42', 'ready')") is False
