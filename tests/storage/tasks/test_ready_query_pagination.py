"""Regression coverage for uncapped ready-task query pagination."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from gobby.storage.tasks._queries import list_ready_tasks


def test_ready_query_can_page_beyond_internal_thousand_row_boundary() -> None:
    rows = [{"id": f"task-{index}"} for index in range(1001)]
    db = MagicMock()

    def fetchall(query: str, params: tuple[object, ...]):
        if "LIMIT %s" in query:
            return rows[: int(params[-1])]
        return rows

    db.fetchall.side_effect = fetchall

    def task_from_row(row):
        return SimpleNamespace(id=row["id"])

    with (
        patch("gobby.storage.tasks._queries.Task.from_row", side_effect=task_from_row) as from_row,
        patch("gobby.storage.tasks._queries.hydrate_task_stage_state") as hydrate_stages,
        patch("gobby.storage.tasks._queries.hydrate_task_blocking_state") as hydrate_blocking,
        patch(
            "gobby.storage.tasks._queries.order_tasks_hierarchically", side_effect=lambda x: x
        ) as order_tasks,
    ):
        result = list_ready_tasks(db, project_id="project", limit=1, offset=1000)

    assert [task.id for task in result] == ["task-1000"]
    query, params = db.fetchall.call_args.args
    assert "LIMIT %s" not in query
    assert params == ("project",)
    assert from_row.call_count == 1001
    hydrate_stages.assert_called_once()
    hydrate_blocking.assert_called_once()
    order_tasks.assert_called_once()
