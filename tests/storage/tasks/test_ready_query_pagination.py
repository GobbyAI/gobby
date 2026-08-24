"""Ready/blocked listings order over a projection and hydrate only the page.

``list_ready_tasks`` backs ``suggest_next_task`` and the dispatcher;
``list_blocked_tasks`` is its dual. Both used to ``SELECT t.*`` every matching
row, convert and hydrate all of them, and only then slice the page -- the
whole-set shape #20840 removed from the main listing (measured there on a
14,904-task project: 331.8 ms row fetch + 89.5 ms Task.from_row + 145.1 ms
hydration against 48.9 ms for the four-column projection). They now order over
the key projection and fetch, convert, and hydrate only the page, from one
snapshot (#20878).
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._queries import list_blocked_tasks, list_ready_tasks

KEY_COLUMNS = "SELECT t.id, t.parent_task_id, t.priority, t.created_at"
KEY_ORDER = "ORDER BY t.priority ASC, t.created_at ASC, t.id ASC"
SNAPSHOT_SQL = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"

LISTINGS: list[Callable[..., list[Any]]] = [list_ready_tasks, list_blocked_tasks]


def test_list_tasks_orders_hierarchy_before_applying_pagination(temp_db, sample_project) -> None:
    task_manager = LocalTaskManager(temp_db)
    project_id = sample_project["id"]
    parent = task_manager.create_task(
        project_id=project_id,
        title="Parent",
        priority=4,
        validation_criteria="Test task completion is observable.",
    )
    child = task_manager.create_task(
        project_id=project_id,
        title="Child",
        priority=0,
        parent_task_id=parent.id,
        validation_criteria="Test task completion is observable.",
    )

    first_page = task_manager.list_tasks(project_id=project_id, limit=1, offset=0)
    second_page = task_manager.list_tasks(project_id=project_id, limit=1, offset=1)

    assert [task.id for task in first_page] == [parent.id]
    assert [task.id for task in second_page] == [child.id]


def _at(day: int) -> datetime:
    return datetime(2026, 1, day, tzinfo=UTC)


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


def _sort_key(row: dict[str, Any]) -> tuple[int, datetime, str]:
    return (row["priority"], row["created_at"], row["id"])


def _fake_db(
    keys: list[dict[str, Any]],
    edges: list[dict[str, Any]] | None = None,
) -> MagicMock:
    """A db answering the three queries a projected listing issues.

    The projection fetch honours ``ORDER BY`` the way PostgreSQL would, so a
    missing ORDER BY is visible as heap order coming back. The projection and
    the edge fetch both mention ``task_dependencies`` (the readiness CTE and
    the blocked filter embed it), so the projection is recognized by its
    column list first. ``db.snapshot_depth`` records how many reads arrived
    inside an open transaction.
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
        if query.startswith("SELECT task_id, depends_on FROM task_dependencies"):
            requested = set(params[0])
            return [edge for edge in edges or [] if edge["task_id"] in requested]
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


def _page(listing: Callable[..., list[Any]], db: MagicMock, **kwargs: Any) -> list[Any]:
    """Run one listing with row conversion and hydration stubbed."""
    with (
        patch(
            "gobby.storage.tasks._queries.Task.from_row",
            side_effect=lambda row: SimpleNamespace(id=row["id"]),
        ),
        patch("gobby.storage.tasks._queries.hydrate_task_stage_state"),
        patch("gobby.storage.tasks._queries.hydrate_task_blocking_state"),
    ):
        return listing(db, project_id="project", **kwargs)


@pytest.mark.parametrize("listing", LISTINGS)
def test_listing_hydrates_only_the_page_not_the_matching_set(
    listing: Callable[..., list[Any]],
) -> None:
    keys = [_row(f"task-{index:04d}") for index in range(5000)]
    db = _fake_db(keys)

    with (
        patch(
            "gobby.storage.tasks._queries.Task.from_row",
            side_effect=lambda row: SimpleNamespace(id=row["id"]),
        ) as from_row,
        patch("gobby.storage.tasks._queries.hydrate_task_stage_state") as hydrate_stages,
        patch("gobby.storage.tasks._queries.hydrate_task_blocking_state") as hydrate_blocking,
    ):
        result = listing(db, project_id="project", limit=20)

    assert len(result) == 20
    assert from_row.call_count == 20, "once per returned task, not once per matching row"
    assert hydrate_stages.call_args.args[1] == result
    assert hydrate_blocking.call_args.args[1] == result

    issued = [call.args[0] for call in db.fetchall.call_args_list]
    assert not any("SELECT t.* FROM tasks" in query for query in issued), (
        "the whole-set SELECT t.* fetch is the defect"
    )
    assert not any(
        "SELECT * FROM tasks" in query and "id = ANY" not in query for query in issued
    ), "the full-row fetch must be restricted to the page"


@pytest.mark.parametrize("listing", LISTINGS)
def test_listing_can_page_beyond_internal_thousand_row_boundary(
    listing: Callable[..., list[Any]],
) -> None:
    """The projection carries no SQL LIMIT, so no internal cap eats the offset."""
    keys = [_row(f"task-{index:04d}") for index in range(1001)]
    db = _fake_db(keys)

    result = _page(listing, db, limit=1, offset=1000)

    assert [task.id for task in result] == ["task-1000"]
    projection = next(
        call.args[0] for call in db.fetchall.call_args_list if KEY_COLUMNS in call.args[0]
    )
    assert "LIMIT %s" not in projection


@pytest.mark.parametrize("listing", LISTINGS)
def test_listing_keeps_parents_before_children_and_sibling_order(
    listing: Callable[..., list[Any]],
) -> None:
    keys = [
        _row("root-late", priority=1, created_day=1),
        _row("root-first", priority=0, created_day=2),
        _row("child-b", parent="root-late", priority=0, created_day=3),
        _row("child-a", parent="root-late", priority=0, created_day=5),
    ]

    result = _page(listing, _fake_db(keys), limit=10)

    assert [task.id for task in result] == ["root-first", "root-late", "child-b", "child-a"]


def test_blocked_page_keeps_topological_sibling_order() -> None:
    """A blocked sibling chain still lists the blocker first.

    Two blocked siblings can block one another (each with its own external
    blocker keeping it in the set), so the edge fetch that feeds the sibling
    topsort is load-bearing here even though it substitutes ids in hand.
    """
    keys = [
        _row("blocked", priority=0, created_day=1),
        _row("blocker", priority=0, created_day=9),
    ]
    edges = [{"task_id": "blocked", "depends_on": "blocker"}]

    result = _page(list_blocked_tasks, _fake_db(keys, edges), limit=10)

    assert [task.id for task in result] == ["blocker", "blocked"]


@pytest.mark.parametrize("listing", LISTINGS)
def test_listing_is_read_from_one_snapshot(listing: Callable[..., list[Any]]) -> None:
    """Projection, edges, page fetch, and hydration see one view of the tasks.

    Independent transactions let a delete land between the projection and the
    page fetch, silently shortening the page (#20870 F2); the ready and
    blocked listings share the hierarchy page's snapshot discipline.
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
        listing(db, project_id="project", limit=3)

    assert db.reads_outside_snapshot == []
    assert depths == [1, 1], "both hydration passes belong to the page's snapshot"
    assert db.transaction.call_count == 1, "one snapshot, not one per read"
    statements = [call.args[0] for call in db.txn.execute.call_args_list]
    assert statements[:1] == [SNAPSHOT_SQL], "the snapshot must be declared before any read"
