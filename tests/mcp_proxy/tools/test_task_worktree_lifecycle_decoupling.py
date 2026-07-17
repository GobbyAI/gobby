"""MCP task lifecycle regressions for task/worktree decoupling."""

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.tasks import LocalTaskManager
from gobby.sync.tasks import TaskSyncManager
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit

TEST_REPO_PATH = str(Path(__file__).resolve().parents[3])


@pytest.fixture
def mock_task_manager() -> MagicMock:
    manager = MagicMock(spec=LocalTaskManager)
    manager.db = MagicMock()
    return manager


@pytest.fixture
def mock_sync_manager() -> MagicMock:
    return MagicMock(spec=TaskSyncManager)


@pytest.fixture(autouse=True)
def _set_session_context() -> Iterator[None]:
    with session_context_for_test("test-session"):
        yield


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["completed", "duplicate"])
async def test_close_task_does_not_mutate_worktree_status(
    mock_task_manager: MagicMock,
    mock_sync_manager: MagicMock,
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
    ):
        mock_wt_instance = MagicMock()
        MockWorktreeManager.return_value = mock_wt_instance
        mock_proj_instance = MagicMock()
        mock_proj_instance.get.return_value = MagicMock(repo_path=TEST_REPO_PATH)
        MockProjManager.return_value = mock_proj_instance
        mock_git.return_value = "abc123"

        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440000"
        mock_task.commits = ["abc123"]
        mock_task.project_id = "11111111-1111-4111-8111-111111110001"
        mock_task.validation_criteria = None
        mock_task.requires_user_review = False
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

    assert result == {"success": True}
    mock_task_manager.close_task.assert_called_once()
    assert mock_task_manager.close_task.call_args.args == (mock_task.id,)
    assert mock_task_manager.close_task.call_args.kwargs["reason"] == reason
    mock_wt_instance.get_by_task.assert_not_called()
    mock_wt_instance.mark_merged.assert_not_called()
    mock_wt_instance.mark_abandoned.assert_not_called()
