"""Tests for projected task-state condition helpers."""

from __future__ import annotations

import pytest

from gobby.storage.tasks import LocalTaskManager
from gobby.workflows.condition_helpers import task_state_in, task_status_in
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers

pytestmark = pytest.mark.unit


def _manager(temp_db) -> LocalTaskManager:
    return LocalTaskManager(temp_db)


def _task(manager: LocalTaskManager, sample_project: dict):
    return manager.create_task(project_id=sample_project["id"], title="Task state helper")


def test_task_state_in_matches_current_stage_state(temp_db, sample_project) -> None:
    manager = _manager(temp_db)
    task = _task(manager, sample_project)
    manager.stage_states.start_stage(task.id, "development", by_session_id=None)
    manager.mark_task_needs_review(task.id)

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


def test_task_state_in_defaults_to_ready_without_current_stage(temp_db, sample_project) -> None:
    manager = _manager(temp_db)
    task = _task(manager, sample_project)
    temp_db.execute("DELETE FROM task_stage_states WHERE task_id = ?", (task.id,))

    assert task_state_in(manager, task.id, "ready") is True


def test_task_status_in_compatibility_alias_maps_open_to_ready(temp_db, sample_project) -> None:
    manager = _manager(temp_db)
    task = _task(manager, sample_project)

    assert task_status_in(manager, task.id, "open") is True
    assert task_status_in(manager, task.id, "closed") is False


def test_helpers_return_false_without_task_manager() -> None:
    assert task_state_in(None, "#42", "ready") is False
    assert task_status_in(None, "#42", "open") is False


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
    assert evaluator.evaluate("task_status_in('#42', 'open')") is False
