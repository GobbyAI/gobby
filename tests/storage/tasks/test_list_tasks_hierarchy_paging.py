"""Hierarchy paging orders over a projection and hydrates only the page.

The hierarchy sort is global -- a page of 20 cannot be known without ordering
the whole filtered set -- but ordering needs four columns and the local
blocking edges, not whole task rows. Measured on the live project (14,904
tasks): SELECT * of every row cost 331.8 ms plus 89.5 ms of Task.from_row and
145.1 ms hydrating stage and blocking state for rows that were then discarded,
against 48.9 ms for the four-column projection and 15.3 ms for the edges
(#20840).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.storage.tasks._queries import list_tasks

pytestmark = pytest.mark.unit

KEY_COLUMNS = "SELECT id, parent_task_id, priority, created_at"


def _at(day: int) -> datetime:
    return datetime(2026, 1, day, tzinfo=UTC)


def _fake_db(
    keys: list[dict[str, Any]],
    edges: list[dict[str, Any]] | None = None,
) -> MagicMock:
    """A db answering the three queries the hierarchy page issues."""
    db = MagicMock()
    by_id = {row["id"]: row for row in keys}

    def fetchall(query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        if KEY_COLUMNS in query:
            return keys
        if "task_dependencies" in query:
            return edges or []
        if "id = ANY" in query:
            requested = set(params[0])
            return [by_id[i] for i in by_id if i in requested]
        raise AssertionError(f"unexpected query: {query}")

    db.fetchall.side_effect = fetchall
    return db


def _row(
    task_id: str,
    *,
    parent: str | None = None,
    priority: int = 2,
    created_day: int = 1,
) -> dict[str, Any]:
    return {
        "id": task_id,
        "parent_task_id": parent,
        "priority": priority,
        "created_at": _at(created_day),
    }


def test_hierarchy_page_hydrates_only_the_page_not_the_project() -> None:
    keys = [_row(f"task-{index:04d}", created_day=1) for index in range(5000)]
    db = _fake_db(keys)

    with (
        patch(
            "gobby.storage.tasks._queries.Task.from_row",
            side_effect=lambda row: SimpleNamespace(id=row["id"]),
        ) as from_row,
        patch("gobby.storage.tasks._queries.hydrate_task_stage_state") as hydrate_stages,
        patch("gobby.storage.tasks._queries.hydrate_task_blocking_state") as hydrate_blocking,
    ):
        result = list_tasks(db, project_id="project", limit=20)

    assert len(result) == 20
    assert from_row.call_count == 20, "whole-project row conversion is the defect"
    assert hydrate_stages.call_args.args[1] == result
    assert hydrate_blocking.call_args.args[1] == result

    issued = [call.args[0] for call in db.fetchall.call_args_list]
    assert any(KEY_COLUMNS in query for query in issued)
    assert not any(
        "SELECT * FROM tasks" in query and "id = ANY" not in query for query in issued
    ), "the full-row fetch must be restricted to the page"


def test_hierarchy_page_keeps_parents_before_children_and_sibling_order() -> None:
    keys = [
        _row("root-late", priority=1, created_day=1),
        _row("root-first", priority=0, created_day=2),
        _row("child-b", parent="root-late", priority=0, created_day=3),
        _row("child-a", parent="root-late", priority=0, created_day=5),
    ]
    db = _fake_db(keys)

    with (
        patch(
            "gobby.storage.tasks._queries.Task.from_row",
            side_effect=lambda row: SimpleNamespace(id=row["id"]),
        ),
        patch("gobby.storage.tasks._queries.hydrate_task_stage_state"),
        patch("gobby.storage.tasks._queries.hydrate_task_blocking_state"),
    ):
        result = list_tasks(db, project_id="project", limit=10)

    assert [task.id for task in result] == [
        "root-first",
        "root-late",
        "child-b",
        "child-a",
    ]


def test_hierarchy_page_keeps_topological_sibling_order() -> None:
    """A blocker sorting after the task it blocks still comes first.

    494 of this project's 4,179 sibling dependency edges invert the
    (priority, created_at) key, so this refinement is not decorative -- it is
    what puts dependency-ordered plan leaves in the order they must be done.
    """
    keys = [
        _row("blocked", priority=0, created_day=1),
        _row("blocker", priority=0, created_day=9),
    ]
    edges = [{"task_id": "blocked", "depends_on": "blocker"}]
    db = _fake_db(keys, edges)

    with (
        patch(
            "gobby.storage.tasks._queries.Task.from_row",
            side_effect=lambda row: SimpleNamespace(id=row["id"]),
        ),
        patch("gobby.storage.tasks._queries.hydrate_task_stage_state"),
        patch("gobby.storage.tasks._queries.hydrate_task_blocking_state"),
    ):
        result = list_tasks(db, project_id="project", limit=10)

    assert [task.id for task in result] == ["blocker", "blocked"]


def test_hierarchy_page_applies_offset_across_the_global_order() -> None:
    keys = [_row(f"task-{index:04d}", priority=index) for index in range(50)]
    db = _fake_db(keys)

    with (
        patch(
            "gobby.storage.tasks._queries.Task.from_row",
            side_effect=lambda row: SimpleNamespace(id=row["id"]),
        ),
        patch("gobby.storage.tasks._queries.hydrate_task_stage_state"),
        patch("gobby.storage.tasks._queries.hydrate_task_blocking_state"),
    ):
        result = list_tasks(db, project_id="project", limit=5, offset=10)

    assert [task.id for task in result] == [f"task-{index:04d}" for index in range(10, 15)]
