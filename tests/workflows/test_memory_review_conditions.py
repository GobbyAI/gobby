"""Tests for post-close memory review classification."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.workflows.memory_review_conditions import (
    classify_memory_review_close,
    pending_memory_reviews,
    pending_memory_reviews_complete,
)

pytestmark = pytest.mark.unit


def _task(**overrides: Any) -> SimpleNamespace:
    values = {
        "id": "22222222-2222-4222-8222-222222220001",
        "seq_num": 42,
        "task_type": "task",
        "category": "code",
        "closed_reason": "completed",
        "closed_at": datetime(2026, 8, 25, tzinfo=UTC),
        "commits": ["abc1234"],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _input(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "task_id": "#42",
        "changes_summary": "Implemented layered memory guidance.",
        "reason": "completed",
        "commit_shas": [],
    }
    values.update(overrides)
    return values


def _manager(task: SimpleNamespace, *, has_children: bool = False) -> MagicMock:
    manager = MagicMock()
    manager.get_task.return_value = task
    manager.list_tasks.return_value = [SimpleNamespace(id="child")] if has_children else []
    return manager


@pytest.mark.parametrize("category", ["code", "config", "docs", "refactor", "test"])
def test_worked_repository_leaf_queues(category: str) -> None:
    result = classify_memory_review_close(_manager(_task(category=category)), **_input())
    assert result == {
        "closure_id": "22222222-2222-4222-8222-222222220001:2026-08-25T00:00:00+00:00",
        "task_id": "22222222-2222-4222-8222-222222220001",
        "task_ref": "#42",
        "changes_summary": "Implemented layered memory guidance.",
    }


@pytest.mark.parametrize("category", ["research", "planning", None])
def test_completed_non_repository_leaf_queues_without_commits(category: str | None) -> None:
    assert (
        classify_memory_review_close(_manager(_task(category=category, commits=None)), **_input())
        is not None
    )


@pytest.mark.parametrize(
    ("task", "has_children"),
    [
        (_task(commits=None), False),
        (_task(task_type="epic"), False),
        (_task(), True),
        *[
            (_task(closed_reason=reason), False)
            for reason in (
                "duplicate",
                "already_implemented",
                "wont_fix",
                "obsolete",
                "out_of_repo",
            )
        ],
    ],
)
def test_non_work_and_structural_closures_skip(
    task: SimpleNamespace,
    has_children: bool,
) -> None:
    assert (
        classify_memory_review_close(_manager(task, has_children=has_children), **_input()) is None
    )


def test_supplied_commits_qualify_repository_close() -> None:
    assert (
        classify_memory_review_close(
            _manager(_task(commits=None)), **_input(commit_shas=["abc1234"])
        )
        is not None
    )


@pytest.mark.parametrize("overrides", [{"task_id": ""}, {"changes_summary": "  "}])
def test_missing_close_evidence_skips(overrides: dict[str, str]) -> None:
    assert classify_memory_review_close(_manager(_task()), **_input(**overrides)) is None


def _closure(closure_id: str) -> dict[str, str]:
    return {"closure_id": closure_id, "task_id": "task", "task_ref": "#1", "changes_summary": "x"}


@pytest.mark.parametrize(
    ("pending", "reviewed", "expected"),
    [
        pytest.param([_closure("a")], [{"closure_id": "a"}], True, id="single_reviewed"),
        pytest.param(
            [_closure("a"), _closure("b")],
            [{"closure_id": "b"}, {"closure_id": "a"}],
            True,
            id="all_reviewed_any_order",
        ),
        pytest.param([_closure("a"), _closure("b")], [{"closure_id": "a"}], False, id="partial"),
        pytest.param([], [{"closure_id": "a"}], False, id="nothing_pending"),
        pytest.param([_closure("a")], [], False, id="nothing_reviewed"),
        pytest.param("not-a-list", [{"closure_id": "a"}], False, id="malformed_pending"),
        pytest.param([_closure("a")], {"closure_id": "a"}, False, id="malformed_records"),
    ],
)
def test_pending_reviews_complete_only_when_every_closure_has_a_record(
    pending: Any, reviewed: Any, expected: bool
) -> None:
    variables = {
        "_memory_pending_task_reviews": pending,
        "_memory_task_review_records": reviewed,
    }

    assert pending_memory_reviews_complete(variables) is expected


def test_pending_memory_reviews_returns_unique_unreviewed_task_refs_in_queue_order() -> None:
    variables = {
        "_memory_pending_task_reviews": [
            _closure("reviewed"),
            {**_closure("second:first"), "task_id": "second", "task_ref": "#2"},
            {**_closure("second:duplicate"), "task_id": "second", "task_ref": "#2"},
            {**_closure("third"), "task_id": "third", "task_ref": "#3"},
            {"closure_id": "malformed", "task_id": "missing-ref"},
        ],
        "_memory_task_review_records": [{"closure_id": "reviewed"}],
    }

    assert pending_memory_reviews(variables) == [
        {"task_id": "second", "task_ref": "#2"},
        {"task_id": "third", "task_ref": "#3"},
    ]
