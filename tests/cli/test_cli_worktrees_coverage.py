from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.worktrees import worktrees
from gobby.storage.worktrees import Worktree

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_worktree_manager():
    with patch("gobby.cli.worktrees.get_worktree_manager") as mock:
        yield mock.return_value


@pytest.fixture
def mock_task_manager():
    with patch("gobby.cli.worktrees.get_task_manager") as mock:
        yield mock.return_value


@pytest.fixture
def mock_resolve_worktree_id():
    with patch("gobby.cli.worktrees.resolve_worktree_id") as mock:
        yield mock


@pytest.fixture
def mock_httpx():
    with patch("httpx.post") as mock:
        yield mock


@pytest.fixture
def runner():
    return CliRunner()


def test_create_worktree(runner, mock_httpx) -> None:
    """Test create worktree command."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "success": True,
        "worktree_id": "wt-123",
        "worktree_path": "/tmp/wt-123",
        "branch_name": "feature/test",
    }
    mock_httpx.return_value = mock_response

    with patch("os.getcwd", return_value="/app"):
        result = runner.invoke(worktrees, ["create", "feature/test"])

    assert result.exit_code == 0
    assert "Created worktree: wt-123" in result.output


def test_create_worktree_with_task(runner, mock_httpx, mock_task_manager) -> None:
    """Test create worktree with task link."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"success": True}
    mock_httpx.return_value = mock_response

    # Mock task resolution
    mock_task = MagicMock()
    mock_task.id = "task-uuid"

    with (
        patch("gobby.cli.worktrees.resolve_task_id", return_value=mock_task),
        patch("os.getcwd", return_value="/app"),
    ):
        result = runner.invoke(worktrees, ["create", "feature/test", "--task", "#1"])

    assert result.exit_code == 0

    # Verify task ID sent in request
    call_args = mock_httpx.call_args[1]["json"]
    assert call_args["task_id"] == "task-uuid"


def test_list_worktrees(runner, mock_worktree_manager) -> None:
    """Test list worktrees command."""
    mock_worktree_manager.list_worktrees.return_value = [
        Worktree(
            id="wt-123",
            branch_name="feat/1",
            status="active",
            worktree_path="/tmp/1",
            project_id="proj-1",
            task_id="task-1",
            base_branch="main",
            agent_session_id=None,
            created_at="",
            updated_at="",
            merged_at=None,
        ),
        Worktree(
            id="wt-456",
            branch_name="feat/2",
            status="stale",
            worktree_path="/tmp/2",
            project_id="proj-1",
            task_id="task-2",
            base_branch="main",
            agent_session_id=None,
            created_at="",
            updated_at="",
            merged_at=None,
        ),
    ]

    result = runner.invoke(worktrees, ["list"])

    assert result.exit_code == 0
    assert "wt-123" in result.output
    assert "feat/1" in result.output
    assert "active" in result.output


def test_show_worktree(runner, mock_worktree_manager, mock_resolve_worktree_id) -> None:
    """Test show worktree command."""
    mock_resolve_worktree_id.return_value = "wt-123"
    worktree = Worktree(
        id="wt-123",
        branch_name="feat/1",
        status="active",
        worktree_path="/tmp/1",
        base_branch="main",
        project_id="proj-1",
        task_id="task-1",
        agent_session_id=None,
        created_at="",
        updated_at="",
        merged_at=None,
    )
    mock_worktree_manager.get.return_value = worktree

    result = runner.invoke(worktrees, ["show", "wt-123"])

    assert result.exit_code == 0
    assert "Worktree: wt-123" in result.output
    assert "Branch: feat/1" in result.output


def test_delete_worktree(
    runner, mock_httpx, mock_resolve_worktree_id, mock_worktree_manager
) -> None:
    """Test delete worktree command."""
    mock_resolve_worktree_id.return_value = "wt-123"

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"success": True}
    mock_httpx.return_value = mock_response

    result = runner.invoke(worktrees, ["delete", "wt-123", "--yes"])

    assert result.exit_code == 0
    assert "Deleted worktree: wt-123" in result.output


def test_claim_worktree(runner, mock_worktree_manager, mock_resolve_worktree_id) -> None:
    """Test claim worktree command."""
    mock_resolve_worktree_id.return_value = "wt-123"
    mock_worktree_manager.claim.return_value = True

    with patch("gobby.cli.worktrees.resolve_session_id", return_value="sess-1"):
        result = runner.invoke(worktrees, ["claim", "wt-123", "sess-1"])

    assert result.exit_code == 0
    assert "Claimed worktree wt-123" in result.output


def test_sync_worktree(runner, mock_httpx, mock_resolve_worktree_id, mock_worktree_manager) -> None:
    """Test sync worktree command."""
    mock_resolve_worktree_id.return_value = "wt-123"

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"success": True, "commits_behind": 2}
    mock_httpx.return_value = mock_response

    result = runner.invoke(worktrees, ["sync", "wt-123"])

    assert result.exit_code == 0
    assert "Synced worktree wt-123" in result.output
    assert "Commits merged: 2" in result.output


def test_cleanup_worktrees(runner, mock_httpx) -> None:
    """Test cleanup worktrees command."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"success": True, "count": 2}
    mock_httpx.return_value = mock_response

    result = runner.invoke(worktrees, ["cleanup", "--yes"])

    assert result.exit_code == 0
    assert "Cleaned up 2 stale worktree(s)" in result.output
