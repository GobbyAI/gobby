"""Tests for condition helper functions used in rule engine expressions.

Covers:
- _normalize_task_id: int → '#N', str passthrough
- task_tree_complete: int task_id, str task_id, list of mixed, None, missing task
- task_needs_user_review: int task_id normalization
- is_task_complete: status-based completion logic
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from gobby.workflows.condition_helpers import (
    _normalize_task_id,
    is_task_complete,
    task_needs_user_review,
    task_tree_complete,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass
class FakeTask:
    id: str
    status: str = "open"
    requires_user_review: bool = False


class FakeTaskManager:
    """In-memory task manager for testing condition helpers."""

    def __init__(self) -> None:
        self._tasks: dict[str, FakeTask] = {}
        self._children: dict[str, list[str]] = {}  # parent_id → [child_ids]

    def add(self, task: FakeTask, parent_id: str | None = None) -> None:
        self._tasks[task.id] = task
        if parent_id is not None:
            self._children.setdefault(parent_id, []).append(task.id)

    def get_task(self, task_id: str) -> FakeTask | None:
        return self._tasks.get(task_id)

    def list_tasks(self, parent_task_id: str | None = None, **kwargs: Any) -> list[FakeTask]:
        if parent_task_id is None:
            return list(self._tasks.values())
        child_ids = self._children.get(parent_task_id, [])
        return [self._tasks[cid] for cid in child_ids if cid in self._tasks]


# ---------------------------------------------------------------------------
# _normalize_task_id
# ---------------------------------------------------------------------------


class TestNormalizeTaskId:
    def test_int_to_hash_format(self) -> None:
        assert _normalize_task_id(9438) == "#9438"

    def test_zero(self) -> None:
        assert _normalize_task_id(0) == "#0"

    def test_string_passthrough(self) -> None:
        assert _normalize_task_id("#9438") == "#9438"

    def test_uuid_passthrough(self) -> None:
        uuid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        assert _normalize_task_id(uuid) == uuid


# ---------------------------------------------------------------------------
# is_task_complete
# ---------------------------------------------------------------------------


class TestIsTaskComplete:
    def test_closed_is_complete(self) -> None:
        assert is_task_complete(FakeTask(id="1", status="closed")) is True

    def test_needs_review_no_hitl_is_complete(self) -> None:
        assert is_task_complete(FakeTask(id="1", status="needs_review", requires_user_review=False)) is True

    def test_needs_review_with_hitl_is_not_complete(self) -> None:
        assert is_task_complete(FakeTask(id="1", status="needs_review", requires_user_review=True)) is False

    def test_open_is_not_complete(self) -> None:
        assert is_task_complete(FakeTask(id="1", status="open")) is False

    def test_in_progress_is_not_complete(self) -> None:
        assert is_task_complete(FakeTask(id="1", status="in_progress")) is False


# ---------------------------------------------------------------------------
# task_tree_complete — int task_id handling (the bug fix)
# ---------------------------------------------------------------------------


class TestTaskTreeCompleteIntHandling:
    """Regression tests for int task_id (e.g. auto_task_ref=9438)."""

    def test_int_task_id_closed(self) -> None:
        """task_tree_complete(9438) should work when task #9438 is closed."""
        mgr = FakeTaskManager()
        mgr.add(FakeTask(id="#9438", status="closed"))
        assert task_tree_complete(mgr, 9438) is True

    def test_int_task_id_open(self) -> None:
        """task_tree_complete(9438) should return False when task is open."""
        mgr = FakeTaskManager()
        mgr.add(FakeTask(id="#9438", status="open"))
        assert task_tree_complete(mgr, 9438) is False

    def test_int_task_id_not_found(self) -> None:
        """task_tree_complete(9438) returns False when task doesn't exist."""
        mgr = FakeTaskManager()
        assert task_tree_complete(mgr, 9438) is False

    def test_int_does_not_raise_type_error(self) -> None:
        """The original bug: iterating over an int caused TypeError."""
        mgr = FakeTaskManager()
        mgr.add(FakeTask(id="#9438", status="open"))
        # This used to raise: TypeError: 'int' object is not iterable
        result = task_tree_complete(mgr, 9438)
        assert result is False


# ---------------------------------------------------------------------------
# task_tree_complete — string task_id (existing behavior)
# ---------------------------------------------------------------------------


class TestTaskTreeCompleteString:
    def test_string_task_id_closed(self) -> None:
        mgr = FakeTaskManager()
        mgr.add(FakeTask(id="#100", status="closed"))
        assert task_tree_complete(mgr, "#100") is True

    def test_string_task_id_open(self) -> None:
        mgr = FakeTaskManager()
        mgr.add(FakeTask(id="#100", status="open"))
        assert task_tree_complete(mgr, "#100") is False


# ---------------------------------------------------------------------------
# task_tree_complete — list of task_ids
# ---------------------------------------------------------------------------


class TestTaskTreeCompleteList:
    def test_list_of_strings_all_closed(self) -> None:
        mgr = FakeTaskManager()
        mgr.add(FakeTask(id="#1", status="closed"))
        mgr.add(FakeTask(id="#2", status="closed"))
        assert task_tree_complete(mgr, ["#1", "#2"]) is True

    def test_list_of_ints_all_closed(self) -> None:
        mgr = FakeTaskManager()
        mgr.add(FakeTask(id="#1", status="closed"))
        mgr.add(FakeTask(id="#2", status="closed"))
        assert task_tree_complete(mgr, [1, 2]) is True

    def test_mixed_list(self) -> None:
        mgr = FakeTaskManager()
        mgr.add(FakeTask(id="#1", status="closed"))
        mgr.add(FakeTask(id="#2", status="closed"))
        assert task_tree_complete(mgr, ["#1", 2]) is True

    def test_list_one_open(self) -> None:
        mgr = FakeTaskManager()
        mgr.add(FakeTask(id="#1", status="closed"))
        mgr.add(FakeTask(id="#2", status="open"))
        assert task_tree_complete(mgr, [1, 2]) is False


# ---------------------------------------------------------------------------
# task_tree_complete — edge cases
# ---------------------------------------------------------------------------


class TestTaskTreeCompleteEdgeCases:
    def test_none_returns_true(self) -> None:
        mgr = FakeTaskManager()
        assert task_tree_complete(mgr, None) is True

    def test_no_task_manager_returns_false(self) -> None:
        assert task_tree_complete(None, "#1") is False

    def test_subtree_all_closed(self) -> None:
        """Parent open but all subtasks closed -> tree is complete."""
        mgr = FakeTaskManager()
        mgr.add(FakeTask(id="#100", status="open"))
        mgr.add(FakeTask(id="#101", status="closed"), parent_id="#100")
        mgr.add(FakeTask(id="#102", status="closed"), parent_id="#100")
        assert task_tree_complete(mgr, "#100") is True

    def test_subtree_one_open(self) -> None:
        mgr = FakeTaskManager()
        mgr.add(FakeTask(id="#100", status="open"))
        mgr.add(FakeTask(id="#101", status="closed"), parent_id="#100")
        mgr.add(FakeTask(id="#102", status="open"), parent_id="#100")
        assert task_tree_complete(mgr, "#100") is False

    def test_nested_subtree(self) -> None:
        """Grandchild must also be complete."""
        mgr = FakeTaskManager()
        mgr.add(FakeTask(id="#100", status="open"))
        mgr.add(FakeTask(id="#101", status="open"), parent_id="#100")
        mgr.add(FakeTask(id="#102", status="closed"), parent_id="#101")
        assert task_tree_complete(mgr, "#100") is True

    def test_nested_subtree_incomplete(self) -> None:
        mgr = FakeTaskManager()
        mgr.add(FakeTask(id="#100", status="open"))
        mgr.add(FakeTask(id="#101", status="open"), parent_id="#100")
        mgr.add(FakeTask(id="#102", status="open"), parent_id="#101")
        assert task_tree_complete(mgr, "#100") is False


# ---------------------------------------------------------------------------
# task_needs_user_review — int task_id handling
# ---------------------------------------------------------------------------


class TestTaskNeedsUserReview:
    def test_int_task_id_in_review(self) -> None:
        mgr = FakeTaskManager()
        mgr.add(FakeTask(id="#9438", status="needs_review", requires_user_review=True))
        assert task_needs_user_review(mgr, 9438) is True

    def test_int_task_id_not_in_review(self) -> None:
        mgr = FakeTaskManager()
        mgr.add(FakeTask(id="#9438", status="open"))
        assert task_needs_user_review(mgr, 9438) is False

    def test_string_task_id(self) -> None:
        mgr = FakeTaskManager()
        mgr.add(FakeTask(id="#100", status="needs_review", requires_user_review=True))
        assert task_needs_user_review(mgr, "#100") is True

    def test_none_returns_false(self) -> None:
        assert task_needs_user_review(FakeTaskManager(), None) is False

    def test_no_manager_returns_false(self) -> None:
        assert task_needs_user_review(None, "#100") is False
