"""Unit tests for hierarchical task ordering (`order_tasks_hierarchically`).

Includes the #19878 regression: sibling-group sorting must stay near-linear.
The previous implementation re-sorted the whole ready queue for every popped
node (O(n^2 log n)); on multi-thousand-task projects that pinned a daemon
thread in pure-Python CPU long enough to starve the event loop.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from gobby.storage.tasks import order_tasks_hierarchically
from gobby.storage.tasks._models import Task

pytestmark = pytest.mark.unit

_BASE_CREATED = datetime(2026, 1, 1, tzinfo=UTC)


def _task(
    task_id: str,
    *,
    priority: int = 2,
    created_offset: int = 0,
    parent_task_id: str | None = None,
    blocked_by: set[str] | None = None,
) -> Task:
    created = _BASE_CREATED + timedelta(seconds=created_offset)
    task = Task(
        id=task_id,
        project_id="project",
        title=task_id,
        priority=priority,
        task_type="task",
        created_at=created,
        updated_at=created,
        parent_task_id=parent_task_id,
    )
    if blocked_by:
        task.blocked_by = blocked_by
    return task


def test_priority_then_created_at_orders_roots() -> None:
    low = _task("low", priority=4, created_offset=0)
    early = _task("early", priority=1, created_offset=1)
    late = _task("late", priority=1, created_offset=2)

    result = order_tasks_hierarchically([low, late, early])

    assert [task.id for task in result] == ["early", "late", "low"]


def test_local_dependencies_order_siblings_topologically() -> None:
    # "blocker" has the worst priority but blocks "blocked", which has the
    # best priority — topology must win over priority.
    blocker = _task("blocker", priority=4, created_offset=0)
    blocked = _task("blocked", priority=0, created_offset=1, blocked_by={"blocker"})
    bystander = _task("bystander", priority=2, created_offset=2)

    result = order_tasks_hierarchically([blocked, bystander, blocker])

    order = [task.id for task in result]
    assert order.index("blocker") < order.index("blocked")
    assert order == ["bystander", "blocker", "blocked"]


def test_parents_precede_children() -> None:
    parent = _task("parent", priority=3, created_offset=0)
    child = _task("child", priority=0, created_offset=1, parent_task_id="parent")
    other_root = _task("other-root", priority=0, created_offset=2)

    result = order_tasks_hierarchically([child, other_root, parent])

    assert [task.id for task in result] == ["other-root", "parent", "child"]


def test_dependency_cycle_falls_back_to_priority_order() -> None:
    first = _task("first", priority=1, created_offset=0, blocked_by={"second"})
    second = _task("second", priority=2, created_offset=1, blocked_by={"first"})
    free = _task("free", priority=3, created_offset=2)

    result = order_tasks_hierarchically([second, free, first])

    assert [task.id for task in result] == ["free", "first", "second"]


def test_equal_sort_keys_preserve_input_order() -> None:
    tasks = [_task(f"tie-{index}", priority=2, created_offset=0) for index in range(6)]

    result = order_tasks_hierarchically(list(tasks))

    assert [task.id for task in result] == [task.id for task in tasks]


def test_large_flat_sibling_group_sorts_quickly() -> None:
    """#19878 regression: 10k parentless siblings must sort in near-linear time.

    The heap-based implementation finishes in well under 100ms; the previous
    per-pop full re-sort took tens of seconds at this size. The 2s bound gives
    slow CI two orders of magnitude of headroom while still failing hard on
    any quadratic regression.
    """
    tasks = [
        _task(f"task-{index}", priority=index % 5, created_offset=index) for index in range(10_000)
    ]

    started = time.monotonic()
    result = order_tasks_hierarchically(tasks)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"hierarchical ordering took {elapsed:.2f}s for 10k siblings"
    expected = sorted(tasks, key=lambda task: (task.priority, task.created_at))
    assert [task.id for task in result] == [task.id for task in expected]
