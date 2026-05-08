"""Regression tests for MCP task listing current-stage filters."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.tasks import LocalTaskManager
from tests.storage.tasks._stage_test_helpers import set_stage_state

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_mcp_list_tasks_current_stage_state_excludes_stale_closed_task(
    temp_db, sample_project
) -> None:
    """gobby-tasks:list_tasks should not return closed tasks by stale stage rows."""
    task_manager = LocalTaskManager(temp_db)
    registry = create_task_registry(task_manager, MagicMock())
    project_id = sample_project["id"]

    open_task = task_manager.create_task(project_id, "Open review")
    set_stage_state(temp_db, open_task.id, "development", "needs_review")
    closed_task = task_manager.create_task(project_id, "Closed stale review")
    set_stage_state(temp_db, closed_task.id, "development", "needs_review")
    temp_db.execute(
        """
        UPDATE tasks
           SET closed_at = ?,
               closed_reason = ?
         WHERE id = ?
        """,
        ("2026-05-06T00:00:00+00:00", "closed-with-stale-stage", closed_task.id),
    )

    with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
        mock_ctx.return_value = {"id": project_id}
        result = await registry.call(
            "list_tasks",
            {"current_stage_state": "needs_review", "limit": 100},
        )

    result_ids = {task["id"] for task in result["tasks"]}
    assert open_task.id in result_ids
    assert closed_task.id not in result_ids
