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

import contextlib
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.storage.tasks._queries import list_tasks

pytestmark = pytest.mark.unit

KEY_COLUMNS = "SELECT id, parent_task_id, priority, created_at"
KEY_ORDER = "ORDER BY priority ASC, created_at ASC, id ASC"
SNAPSHOT_SQL = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"


def _at(day: int) -> datetime:
    return datetime(2026, 1, day, tzinfo=UTC)


def _fake_db(
    keys: list[dict[str, Any]],
    edges: list[dict[str, Any]] | None = None,
) -> MagicMock:
    """A db answering the three queries the hierarchy page issues.

    The projection fetch honours ``ORDER BY`` the way PostgreSQL would, so a
    caller that hands ``keys`` in heap order gets heap order back unless the
    query asks for a sort. That is what makes the shuffle tests below able to
    see a missing ORDER BY at all (#20870).

    ``db.snapshot_depth`` records how many reads arrived inside an open
    transaction, so a test can assert the page was assembled from one.
    """
    db = MagicMock()
    by_id = {row["id"]: row for row in keys}
    db.snapshot_depth = 0
    db.reads_outside_snapshot = []

    def fetchall(query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        if db.snapshot_depth == 0:
            db.reads_outside_snapshot.append(query)
        if KEY_COLUMNS in query:
            return sorted(keys, key=_sort_key) if KEY_ORDER in query else list(keys)
        if "task_dependencies" in query:
            return edges or []
        if "id = ANY" in query:
            requested = set(params[0])
            return [by_id[i] for i in by_id if i in requested]
        raise AssertionError(f"unexpected query: {query}")

    @contextlib.contextmanager
    def transaction() -> Iterator[MagicMock]:
        db.snapshot_depth += 1
        try:
            yield db.txn
        finally:
            db.snapshot_depth -= 1

    db.fetchall.side_effect = fetchall
    db.transaction.side_effect = transaction
    return db


def _sort_key(row: dict[str, Any]) -> tuple[int, datetime, str]:
    return (row["priority"], row["created_at"], row["id"])


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


def _page(db: MagicMock, **kwargs: Any) -> list[Any]:
    """Run one hierarchy listing with row conversion and hydration stubbed."""
    with (
        patch(
            "gobby.storage.tasks._queries.Task.from_row",
            side_effect=lambda row: SimpleNamespace(id=row["id"]),
        ),
        patch("gobby.storage.tasks._queries.hydrate_task_stage_state"),
        patch("gobby.storage.tasks._queries.hydrate_task_blocking_state"),
    ):
        return list_tasks(db, project_id="project", **kwargs)


def test_a_tie_group_is_ordered_in_sql_not_by_heap_order() -> None:
    """Ties are the common case, so the tie-break cannot be Postgres heap order.

    ``priority`` defaults to 2 and ``created_at`` is the transaction timestamp,
    so every sibling an expansion applies in one transaction carries a
    byte-identical sort key. ``order_task_keys`` breaks those ties by input
    order, which makes the projection's own ORDER BY the only thing standing
    between a page and a non-HOT update reshuffling it (#20870).
    """
    siblings = [_row(f"task-{index:04d}") for index in range(12)]

    forward = _page(_fake_db(list(siblings)), limit=12)
    reversed_heap = _page(_fake_db(list(reversed(siblings))), limit=12)

    assert [task.id for task in forward] == [f"task-{index:04d}" for index in range(12)]
    assert [task.id for task in reversed_heap] == [task.id for task in forward]


def test_paging_a_tie_group_returns_each_task_once_across_reshuffles() -> None:
    """A row reshuffled between page 1 and page 2 must not vanish or double."""
    siblings = [_row(f"task-{index:04d}") for index in range(10)]
    rotated = siblings[7:] + siblings[:7]

    first = _page(_fake_db(list(siblings)), limit=5, offset=0)
    second = _page(_fake_db(rotated), limit=5, offset=5)

    paged = [task.id for task in first] + [task.id for task in second]
    assert len(paged) == len(set(paged)), "a reshuffled tie group must not duplicate a task"
    assert set(paged) == {row["id"] for row in siblings}


def test_the_page_is_read_from_one_snapshot() -> None:
    """Projection, edges, and hydration must agree on one view of the tasks.

    Three independent transactions let a delete land between the projection and
    the hydration, which silently shortens the page (#20870 F2).
    """
    keys = [_row(f"task-{index:04d}") for index in range(6)]
    db = _fake_db(keys, [{"task_id": "task-0003", "depends_on": "task-0004"}])
    depths: list[int] = []

    with (
        patch(
            "gobby.storage.tasks._queries.Task.from_row",
            side_effect=lambda row: SimpleNamespace(id=row["id"]),
        ),
        patch(
            "gobby.storage.tasks._queries.hydrate_task_stage_state",
            side_effect=lambda *_: depths.append(db.snapshot_depth),
        ),
        patch(
            "gobby.storage.tasks._queries.hydrate_task_blocking_state",
            side_effect=lambda *_: depths.append(db.snapshot_depth),
        ),
    ):
        list_tasks(db, project_id="project", limit=3)

    assert db.reads_outside_snapshot == []
    assert depths == [1, 1], "both hydration passes belong to the page's snapshot"
    assert db.transaction.call_count == 1, "one snapshot, not one per read"
    statements = [call.args[0] for call in db.txn.execute.call_args_list]
    assert statements[:1] == [SNAPSHOT_SQL], "the snapshot must be declared before any read"


def test_the_page_inherits_an_open_snapshot_instead_of_opening_its_own() -> None:
    """A caller already holding a snapshot owns the isolation level.

    ``SET TRANSACTION ISOLATION LEVEL`` is only legal as a transaction's first
    statement, so a nested listing has to read the caller's view rather than
    redeclare one.
    """
    db = _fake_db([_row(f"task-{index:04d}") for index in range(4)])

    with patch(
        "gobby.storage.tasks._queries.ambient_transaction",
        return_value=db.txn,
    ):
        result = _page(db, limit=4)

    assert len(result) == 4
    assert db.transaction.call_count == 0, "the caller's transaction is the snapshot"
    assert db.txn.execute.call_args_list == [], "the caller's isolation level stands"


def test_an_unrecognized_sort_by_is_a_value_error() -> None:
    """The route maps ValueError to 400; a KeyError reaches the client as a 500."""
    db = _fake_db([_row("task-0000")])

    with pytest.raises(ValueError, match="sort_by"):
        list_tasks(db, project_id="project", sort_by="nonsense")
