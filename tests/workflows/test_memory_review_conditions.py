"""Tests for post-close memory review classification."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.workflows.memory_review_conditions import (
    classify_memory_review_close,
    queue_memory_review_close,
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


def _event(*, closed: bool = True, preview: bool = False) -> dict[str, Any]:
    return {
        "tool_output": {
            "success": closed,
            "closed": closed,
            "preview": preview,
            "task_id": "22222222-2222-4222-8222-222222220001",
            "commit_shas": ["abc1234"],
        }
    }


def _input(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "task_id": "#42",
        "changes_summary": "Implemented layered memory guidance.",
        "reason": "completed",
        "preview": False,
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
    result = classify_memory_review_close(_manager(_task(category=category)), _event(), _input())

    assert result == {
        "closure_id": "22222222-2222-4222-8222-222222220001:2026-08-25T00:00:00+00:00",
        "task_id": "22222222-2222-4222-8222-222222220001",
        "task_ref": "#42",
        "changes_summary": "Implemented layered memory guidance.",
    }


def test_successful_conditional_close_queues_even_when_preview_flag_is_true() -> None:
    result = classify_memory_review_close(
        _manager(_task()),
        _event(preview=True),
        _input(preview=True),
    )

    assert result is not None


@pytest.mark.parametrize("category", ["research", "planning", "manual"])
def test_completed_non_repository_leaf_queues_without_commits(category: str) -> None:
    task = _task(category=category, commits=None)
    event = _event()
    event["tool_output"]["commit_shas"] = []

    assert classify_memory_review_close(_manager(task), event, _input()) is not None


@pytest.mark.parametrize(
    ("task", "event", "tool_input", "has_children"),
    [
        (_task(), _event(closed=False), _input(), False),
        (
            _task(commits=None),
            {**_event(), "tool_output": {**_event()["tool_output"], "commit_shas": []}},
            _input(),
            False,
        ),
        (_task(task_type="epic"), _event(), _input(), False),
        (_task(), _event(), _input(), True),
        (_task(closed_reason="duplicate"), _event(), _input(reason="duplicate"), False),
        (
            _task(closed_reason="already_implemented"),
            _event(),
            _input(reason="already_implemented"),
            False,
        ),
        (_task(closed_reason="wont_fix"), _event(), _input(reason="wont_fix"), False),
        (_task(closed_reason="obsolete"), _event(), _input(reason="obsolete"), False),
        (_task(closed_reason="out_of_repo"), _event(), _input(reason="out_of_repo"), False),
    ],
)
def test_non_work_and_structural_closures_skip(
    task: SimpleNamespace,
    event: dict[str, Any],
    tool_input: dict[str, Any],
    has_children: bool,
) -> None:
    assert (
        classify_memory_review_close(_manager(task, has_children=has_children), event, tool_input)
        is None
    )


def test_duplicate_delivery_and_successful_review_record_deduplicate() -> None:
    manager = _manager(_task())
    first = queue_memory_review_close(manager, _event(), _input(), {})
    duplicate = queue_memory_review_close(
        manager,
        _event(),
        _input(),
        {"_memory_pending_task_reviews": first},
    )
    reviewed = queue_memory_review_close(
        manager,
        _event(),
        _input(),
        {
            "_memory_task_review_records": [
                {"closure_id": first[0]["closure_id"], "candidate_ids": []}
            ]
        },
    )

    assert duplicate == first
    assert reviewed == []


def test_uncategorized_completed_leaf_queues_without_commits() -> None:
    task = _task(category=None, commits=None)
    event = _event()
    event["tool_output"]["commit_shas"] = []

    assert classify_memory_review_close(_manager(task), event, _input()) is not None


def test_payload_without_task_id_skips_even_when_input_names_task() -> None:
    manager = _manager(_task())
    event = _event()
    del event["tool_output"]["task_id"]

    assert classify_memory_review_close(manager, event, _input()) is None
    manager.get_task.assert_not_called()


def test_delivered_flag_drops_consumed_closures_before_queueing() -> None:
    manager = _manager(_task())
    delivered = {
        "closure_id": "old:closed",
        "task_id": "old",
        "task_ref": "#1",
        "changes_summary": "Earlier closure.",
    }

    requeued = queue_memory_review_close(
        manager,
        _event(),
        _input(),
        {"_memory_pending_task_reviews": [delivered], "_memory_review_stop_delivered": True},
    )
    undelivered = queue_memory_review_close(
        manager,
        _event(),
        _input(),
        {"_memory_pending_task_reviews": [delivered], "_memory_review_stop_delivered": False},
    )
    unqueued = queue_memory_review_close(
        _manager(_task(task_type="epic")),
        _event(),
        _input(),
        {"_memory_pending_task_reviews": [delivered], "_memory_review_stop_delivered": True},
    )

    assert [item["task_ref"] for item in requeued] == ["#42"]
    assert [item["task_ref"] for item in undelivered] == ["#1", "#42"]
    assert unqueued == []
