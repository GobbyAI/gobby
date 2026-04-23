"""Tests for task_status_in condition helper."""

from dataclasses import dataclass

import pytest

from gobby.workflows.condition_helpers import task_status_in
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers

pytestmark = pytest.mark.unit


@dataclass
class FakeTask:
    id: str
    status: str


class FakeTaskManager:
    def __init__(self, tasks: dict[str, FakeTask]):
        self._tasks = tasks

    def get_task(self, task_id: str) -> FakeTask | None:
        return self._tasks.get(task_id)


def test_task_status_in_matches_current_status() -> None:
    manager = FakeTaskManager({"#42": FakeTask(id="#42", status="review_approved")})
    assert task_status_in(manager, 42, "review_approved", "closed") is True
    assert task_status_in(manager, "#42", "open", "needs_review") is False


def test_task_status_in_returns_false_without_task_manager() -> None:
    assert task_status_in(None, "#42", "open") is False


def test_safe_evaluator_stubs_task_status_in_to_false_without_manager() -> None:
    ctx = {"variables": {}}
    helpers = build_condition_helpers(task_manager=None, context=ctx)
    evaluator = SafeExpressionEvaluator(ctx, helpers)
    assert evaluator.evaluate("task_status_in('#42', 'open')") is False
