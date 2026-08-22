"""MCP task lifecycle regressions for task/worktree decoupling."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit

TEST_REPO_PATH = str(Path(__file__).resolve().parents[3])


@pytest.fixture
def mock_task_manager() -> MagicMock:
    manager = MagicMock(spec=LocalTaskManager)
    manager.db = MagicMock()
    # Close-review and backoff lookups read as "no row" instead of MagicMock rows.
    _conn = MagicMock()
    _conn.execute.return_value.fetchone.return_value = None
    _conn.execute.return_value.rowcount = 0
    manager.db.transaction.return_value.__enter__.return_value = _conn
    manager.db.transaction.return_value.__exit__.return_value = False
    return manager


@pytest.fixture(autouse=True)
def _set_session_context() -> Iterator[None]:
    with session_context_for_test("test-session"):
        yield


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["completed", "duplicate"])
async def test_close_task_does_not_mutate_worktree_status(
    mock_task_manager: MagicMock,
    reason: str,
) -> None:
    with (
        patch("gobby.mcp_proxy.tools.tasks._context.LocalWorktreeManager") as MockWorktreeManager,
        patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
        patch("gobby.utils.git.run_git_command") as mock_git,
        patch(
            "gobby.utils.git.normalize_commit_sha",
            side_effect=lambda sha, cwd=None: sha,
        ),
        patch("gobby.hooks.event_handlers._plan.on_epic_terminal"),
    ):
        mock_wt_instance = MagicMock()
        MockWorktreeManager.return_value = mock_wt_instance
        mock_proj_instance = MagicMock()
        mock_proj_instance.get.return_value = MagicMock(repo_path=TEST_REPO_PATH)
        MockProjManager.return_value = mock_proj_instance
        mock_git.return_value = "abc123"

        registry: InternalToolRegistry = create_task_registry(mock_task_manager)

        now = datetime.now(UTC)
        mock_task = Task(
            id="550e8400-e29b-41d4-a716-446655440000",
            project_id="11111111-1111-4111-8111-111111110001",
            title="Worktree lifecycle",
            priority=2,
            task_type="epic",
            created_at=now,
            updated_at=now,
            commits=["abc123"],
            seq_num=123,
        )
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.close_task.return_value = mock_task
        mock_task_manager.list_tasks.return_value = []

        result = await registry.call(
            "close_task",
            {
                "task_id": "550e8400-e29b-41d4-a716-446655440000",
                "reason": reason,
                "changes_summary": "test changes",
            },
        )

    assert result == {
        "success": True,
        "preview": False,
        "can_close": True,
        "closed": True,
        "task_id": mock_task.id,
        "commit_shas": ["abc123"],
    }
    mock_task_manager.close_task.assert_called_once()
    assert mock_task_manager.close_task.call_args.args == (mock_task.id,)
    assert mock_task_manager.close_task.call_args.kwargs["reason"] == reason
    mock_wt_instance.get_by_task.assert_not_called()
    mock_wt_instance.mark_merged.assert_not_called()
    mock_wt_instance.mark_abandoned.assert_not_called()
