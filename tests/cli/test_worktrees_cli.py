from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.worktrees import worktrees
from gobby.storage.worktrees import Worktree

pytestmark = pytest.mark.unit

# Mock worktree data
MOCK_WORKTREE = Worktree(
    id="wt-123",
    branch_name="feature/test",
    worktree_path="/tmp/wt-123",
    base_branch="main",
    status="active",
    created_at="2023-01-01T00:00:00Z",
    updated_at="2023-01-01T00:00:00Z",
    project_id="proj-123",
    agent_session_id=None,
    task_id=None,
    merged_at=None,
)


@pytest.fixture
def mock_worktree_manager():
    with patch("gobby.cli.worktrees.get_worktree_manager") as mock:
        yield mock.return_value


@pytest.fixture
def mock_httpx():
    with patch("gobby.cli.worktrees.httpx.post") as mock:
        yield mock


def test_list_worktrees_empty(mock_worktree_manager) -> None:
    """Test 'worktrees list' with no worktrees."""
    mock_worktree_manager.list_worktrees.return_value = []

    runner = CliRunner()
    result = runner.invoke(worktrees, ["list"])

    assert result.exit_code == 0
    assert "No worktrees found" in result.output


def test_list_worktrees_populated(mock_worktree_manager) -> None:
    """Test 'worktrees list' with active worktrees."""
    mock_worktree_manager.list_worktrees.return_value = [MOCK_WORKTREE]

    runner = CliRunner()
    result = runner.invoke(worktrees, ["list"])

    assert result.exit_code == 0
    assert "wt-123" in result.output
    assert "feature/test" in result.output
    assert "active" in result.output


def test_create_worktree_success(mock_httpx) -> None:
    """Test 'worktrees create' success via Daemon API."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "success": True,
        "worktree_id": "wt-new",
        "branch_name": "feature/new",
        "worktree_path": "/tmp/new",
    }
    mock_httpx.return_value = mock_response

    runner = CliRunner()
    result = runner.invoke(worktrees, ["create", "feature/new"])

    assert result.exit_code == 0
    assert "Created worktree: wt-new" in result.output
    mock_httpx.assert_called_once()


def test_create_worktree_failure(mock_httpx) -> None:
    """Test 'worktrees create' failure."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"success": False, "error": "Branch exists"}
    mock_httpx.return_value = mock_response

    runner = CliRunner()
    result = runner.invoke(worktrees, ["create", "feature/fail"])

    assert result.exit_code == 0
    assert "Failed to create worktree: Branch exists" in result.output


@patch("gobby.cli.worktrees.get_daemon_url", return_value="http://localhost:9876")
def test_delete_worktree_success(mock_url, mock_worktree_manager, mock_httpx) -> None:
    """Test 'worktrees delete' success via Daemon API."""
    mock_worktree_manager.list_worktrees.return_value = [MOCK_WORKTREE]
    mock_worktree_manager.get.return_value = MOCK_WORKTREE

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"success": True, "result": {"success": True}}
    mock_httpx.return_value = mock_response

    runner = CliRunner()
    # Mock resolve logic if needed, but resolve_worktree_id uses manager.list_worktrees
    # Default mock implementation should work if we mock manager methods correctly.

    result = runner.invoke(worktrees, ["delete", "wt-123", "--yes"])

    assert result.exit_code == 0
    assert "Deleted worktree: wt-123" in result.output
    mock_httpx.assert_called_once_with(
        "http://localhost:9876/api/mcp/gobby-worktrees/tools/delete_worktree",
        json={"worktree_id": "wt-123", "force": False},
        timeout=30.0,
    )


def test_show_worktree(mock_worktree_manager) -> None:
    """Test 'worktrees show'."""
    mock_worktree_manager.list_worktrees.return_value = [MOCK_WORKTREE]
    mock_worktree_manager.get.return_value = MOCK_WORKTREE

    runner = CliRunner()
    result = runner.invoke(worktrees, ["show", "wt-123"])

    assert result.exit_code == 0
    assert "Worktree: wt-123" in result.output
    assert "Branch: feature/test" in result.output
