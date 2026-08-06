"""Tests for gobby.cli.clones module.

Tests for Clone CLI commands:
- create
- list
- spawn
- sync
- merge
- delete
"""

import importlib
import json
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from gobby.storage.clones import Clone
from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError


def test_full_uuid_foreign_clone_reports_ownership_mismatch() -> None:
    from gobby.cli.clones import resolve_clone_id

    manager = MagicMock()
    manager.get.side_effect = MachineOwnershipMismatchError(
        resource_kind="clone",
        resource_id="10000000-0000-4000-8000-000000000002",
        owner_machine_id="20000000-0000-4000-8000-000000000002",
        current_machine_id="20000000-0000-4000-8000-000000000001",
    )

    with pytest.raises(click.ClickException) as exc_info:
        resolve_clone_id(manager, "10000000-0000-4000-8000-000000000002")

    assert "machine_ownership_mismatch" in str(exc_info.value)
    manager.list_clones.assert_not_called()


pytestmark = pytest.mark.cli

# Mock clone data
MOCK_CLONE = Clone(
    id="clone-123",
    project_id="proj-1",
    branch_name="feature/test",
    clone_path="/tmp/clones/test",
    base_branch="main",
    task_id=None,
    agent_session_id=None,
    status="active",
    remote_url="https://github.com/user/repo.git",
    last_sync_at=None,
    cleanup_after=None,
    created_at="2024-01-01T00:00:00Z",
    updated_at="2024-01-01T00:00:00Z",
)


@pytest.fixture
def mock_clone_manager():
    with patch("gobby.cli.clones.get_clone_manager") as mock:
        yield mock.return_value


@pytest.fixture
def mock_httpx():
    with patch("gobby.cli.clones.httpx.post") as mock:
        yield mock


class TestClonesListCommand:
    """Tests for 'clones list' command."""

    def test_list_clones_empty(self, mock_clone_manager) -> None:
        """Test 'clones list' with no clones."""
        from gobby.cli.clones import clones

        mock_clone_manager.list_clones.return_value = []

        runner = CliRunner()
        result = runner.invoke(clones, ["list"])

        assert result.exit_code == 0
        assert "No clones found" in result.output

    def test_list_clones_populated(self, mock_clone_manager) -> None:
        """Test 'clones list' with clones present."""
        from gobby.cli.clones import clones

        mock_clone_manager.list_clones.return_value = [MOCK_CLONE]

        runner = CliRunner()
        result = runner.invoke(clones, ["list"])

        assert result.exit_code == 0
        assert "clone-123" in result.output
        assert "feature/test" in result.output
        assert "active" in result.output

    def test_list_clones_json_format(self, mock_clone_manager) -> None:
        """Test 'clones list --json'."""
        from gobby.cli.clones import clones

        mock_clone_manager.list_clones.return_value = [MOCK_CLONE]

        runner = CliRunner()
        result = runner.invoke(clones, ["list", "--json"])

        assert result.exit_code == 0
        assert '"id": "clone-123"' in result.output


class TestClonesCreateCommand:
    """Tests for 'clones create' command."""

    def test_create_clone_success(self, mock_httpx) -> None:
        """Test 'clones create' success."""
        from gobby.cli.clones import clones

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "success": True,
            "clone": {"id": "clone-new", "branch_name": "feature/new"},
        }
        mock_httpx.return_value = mock_response

        runner = CliRunner()
        result = runner.invoke(clones, ["create", "feature/new", "/tmp/clone-new"])

        assert result.exit_code == 0
        assert "clone-new" in result.output
        mock_httpx.assert_called_once()
        assert "headers" in mock_httpx.call_args.kwargs

    def test_create_clone_failure(self, mock_httpx) -> None:
        """Test 'clones create' failure."""
        from gobby.cli.clones import clones

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "success": False,
            "error": "Clone failed",
        }
        mock_httpx.return_value = mock_response

        runner = CliRunner()
        result = runner.invoke(clones, ["create", "feature/fail", "/tmp/fail"])

        assert result.exit_code == 1
        assert "Failed" in result.output or "Clone failed" in result.output


class TestClonesSpawnCommand:
    """Tests for 'clones spawn' command."""

    def test_spawn_agent_success(self, mock_clone_manager, mock_httpx) -> None:
        """Test 'clones spawn' success."""
        from gobby.cli.clones import clones

        mock_clone_manager.list_clones.return_value = [MOCK_CLONE]
        mock_clone_manager.get.return_value = MOCK_CLONE

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "success": True,
            "session_id": "session-456",
            "clone_id": "clone-123",
        }
        mock_httpx.return_value = mock_response

        runner = CliRunner()
        result = runner.invoke(
            clones,
            ["spawn", "clone-123", "Work on feature", "--parent-session-id", "parent-123"],
        )

        assert result.exit_code == 0
        assert "session-456" in result.output or "Spawned" in result.output
        mock_httpx.assert_called_once()
        assert "headers" in mock_httpx.call_args.kwargs

    def test_spawn_agent_clone_not_found(self, mock_clone_manager) -> None:
        """Test 'clones spawn' with non-existent clone."""
        from gobby.cli.clones import clones

        mock_clone_manager.list_clones.return_value = []
        mock_clone_manager.get.return_value = None

        runner = CliRunner()
        result = runner.invoke(
            clones,
            ["spawn", "nonexistent", "Work on feature", "--parent-session-id", "parent-123"],
        )

        assert "not found" in result.output.lower() or result.exit_code != 0


class TestClonesSyncCommand:
    """Tests for 'clones sync' command."""

    def test_sync_clone_success(self, mock_clone_manager, mock_httpx) -> None:
        """Test 'clones sync' success."""
        from gobby.cli.clones import clones

        mock_clone_manager.list_clones.return_value = [MOCK_CLONE]
        mock_clone_manager.get.return_value = MOCK_CLONE

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True}
        mock_httpx.return_value = mock_response

        runner = CliRunner()
        result = runner.invoke(clones, ["sync", "clone-123"])

        assert result.exit_code == 0
        mock_httpx.assert_called_once()
        assert "headers" in mock_httpx.call_args.kwargs


class TestClonesMergeCommand:
    """Tests for 'clones merge' command."""

    def test_merge_clone_success(self, mock_clone_manager, mock_httpx) -> None:
        """Test 'clones merge' success."""
        from gobby.cli.clones import clones

        mock_clone_manager.list_clones.return_value = [MOCK_CLONE]
        mock_clone_manager.get.return_value = MOCK_CLONE

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "success": True,
            "message": "Merged successfully",
        }
        mock_httpx.return_value = mock_response

        runner = CliRunner()
        result = runner.invoke(clones, ["merge", "clone-123"])

        assert result.exit_code == 0
        assert "Merged" in result.output or "success" in result.output.lower()
        assert "headers" in mock_httpx.call_args.kwargs

    def test_merge_clone_conflicts(self, mock_clone_manager, mock_httpx) -> None:
        """Test 'clones merge' with conflicts."""
        from gobby.cli.clones import clones

        mock_clone_manager.list_clones.return_value = [MOCK_CLONE]
        mock_clone_manager.get.return_value = MOCK_CLONE

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "success": False,
            "has_conflicts": True,
            "conflicted_files": ["src/foo.py", "src/bar.py"],
        }
        mock_httpx.return_value = mock_response

        runner = CliRunner()
        result = runner.invoke(clones, ["merge", "clone-123"])

        assert result.exit_code == 1
        assert "conflict" in result.output.lower() or "src/foo.py" in result.output


class TestClonesDeleteCommand:
    """Tests for 'clones delete' command."""

    def test_delete_clone_success(self, mock_clone_manager, mock_httpx) -> None:
        """Test 'clones delete' success."""
        from gobby.cli.clones import clones

        mock_clone_manager.list_clones.return_value = [MOCK_CLONE]
        mock_clone_manager.get.return_value = MOCK_CLONE

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True}
        mock_httpx.return_value = mock_response

        runner = CliRunner()
        result = runner.invoke(clones, ["delete", "clone-123", "--yes"])

        assert result.exit_code == 0
        assert "Deleted" in result.output or "success" in result.output.lower()
        assert "headers" in mock_httpx.call_args.kwargs

    def test_delete_clone_force_json_requires_yes(self, mock_clone_manager, mock_httpx) -> None:
        """Test 'clones delete --force --json' without --yes does not delete."""
        from gobby.cli.clones import clones

        mock_clone_manager.list_clones.return_value = [MOCK_CLONE]
        mock_clone_manager.get.return_value = MOCK_CLONE

        runner = CliRunner()
        result = runner.invoke(clones, ["delete", "clone-123", "--force", "--json"])

        assert result.exit_code == 1
        assert json.loads(result.output) == {
            "success": False,
            "error": "Force deleting a clone requires --yes",
        }
        mock_httpx.assert_not_called()

    def test_delete_clone_not_found(self, mock_clone_manager) -> None:
        """Test 'clones delete' with non-existent clone."""
        from gobby.cli.clones import clones

        mock_clone_manager.list_clones.return_value = []
        mock_clone_manager.get.return_value = None

        runner = CliRunner()
        result = runner.invoke(clones, ["delete", "nonexistent", "--yes"])

        assert result.exit_code == 1
        assert "not found" in result.output.lower() or result.exit_code != 0


class TestResolveCloneId:
    def test_resolve_clone_id_scopes_prefix_lookup_to_current_project(
        self,
        mock_clone_manager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clones_module = importlib.import_module("gobby.cli.clones")

        mock_clone_manager.get.return_value = None
        mock_clone_manager.list_clones.return_value = [MOCK_CLONE]
        monkeypatch.setattr(
            clones_module, "resolve_project_ref", lambda *_args, **_kwargs: "proj-1"
        )

        assert clones_module.resolve_clone_id(mock_clone_manager, "clone") == "clone-123"
        mock_clone_manager.list_clones.assert_called_once_with(project_id="proj-1")
