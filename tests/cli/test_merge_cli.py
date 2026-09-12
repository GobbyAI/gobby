"""Tests for merge CLI commands (TDD red phase).

Tests for CLI merge commands:
- gobby merge start <source-branch> [--strategy=auto|ai-only|human]
- gobby merge status [--verbose]
- gobby merge resolve <file> [--strategy=ai|human]
- gobby merge apply [--force]
- gobby merge abort
"""

import importlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner


@pytest.mark.parametrize("abort_exit", [0, 1])
@pytest.mark.parametrize("json_format", [True, False], ids=["json", "text"])
def test_abort_preserves_resolution_until_git_abort_succeeds(
    abort_exit: int, json_format: bool
) -> None:
    from gobby.cli import cli

    events: list[str] = []
    resolution = SimpleNamespace(id="resolution-id", worktree_id="worktree-id", status="pending")
    manager = MagicMock()
    manager.get_active_resolution.return_value = resolution
    manager.get_resolution.return_value = resolution

    def delete_resolution(resolution_id: str) -> bool:
        assert resolution_id == resolution.id
        events.append("delete-record")
        return True

    def run_git(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        events.append(" ".join(command))
        is_abort = command == ["merge", "--abort"]
        return subprocess.CompletedProcess(
            command, abort_exit if is_abort else 0, "", "abort refused"
        )

    manager.delete_resolution.side_effect = delete_resolution
    git = MagicMock()
    git.run_git_command = AsyncMock(side_effect=run_git)
    with (
        patch("gobby.cli.merge.get_project_context", return_value={"id": "project-id"}),
        patch("gobby.cli.merge.get_worktree_context", return_value={"id": "worktree-id"}),
        patch("gobby.cli.merge.get_merge_manager", return_value=manager),
        patch("gobby.cli.merge.get_merge_resolver"),
        patch("gobby.cli.merge.get_git_manager", return_value=git),
        patch("gobby.cli.merge.worktree_manager_context") as worktrees,
    ):
        worktrees.return_value.__enter__.return_value.get.return_value = SimpleNamespace(
            worktree_path="/fixture/worktree"
        )
        result = CliRunner().invoke(cli, ["merge", "abort", *(["--json"] if json_format else [])])

    assert "merge --abort" in events, result.output
    assert result.exit_code == abort_exit, result.output
    if abort_exit:
        manager.delete_resolution.assert_not_called()
        assert "abort refused" in result.output
    else:
        assert events.index("merge --abort") < events.index("delete-record")
        manager.delete_resolution.assert_called_once_with(resolution.id)
    if json_format:
        assert json.loads(result.output)["success"] is (abort_exit == 0)


pytestmark = pytest.mark.unit


@pytest.mark.parametrize("strategy", ["ai", "human"])
@pytest.mark.parametrize("json_format", [True, False], ids=["json", "text"])
def test_resolve_output_preserves_pending_human_status(strategy: str, json_format: bool) -> None:
    from gobby.cli import cli

    payload = {"id": "conflict-id", "status": "resolved" if strategy == "ai" else "pending"}
    conflict = MagicMock()
    conflict.id = "conflict-id"
    conflict.to_dict.return_value = payload
    manager = MagicMock()
    manager.get_conflict_by_path.return_value = conflict
    manager.get_conflict.return_value = conflict
    with (
        patch("gobby.cli.merge.get_project_context", return_value={"id": "project-id"}),
        patch("gobby.cli.merge.get_merge_manager", return_value=manager),
        patch("gobby.cli.merge._resolve_conflict_with_ai", new_callable=AsyncMock) as resolve,
    ):
        resolve.return_value = {"success": True}
        result = CliRunner().invoke(
            cli,
            [
                "merge",
                "resolve",
                "src/example.py",
                "--strategy",
                strategy,
                *(["--json"] if json_format else []),
            ],
        )

    assert result.exit_code == 0, result.output
    if json_format:
        assert json.loads(result.output) == payload
    elif strategy == "human":
        assert "Resolved:" not in result.output
        assert "human" in result.output.lower()
    else:
        assert "Resolved: src/example.py" in result.output
    if strategy == "human":
        resolve.assert_not_awaited()


# ==============================================================================
# Fixtures
# ==============================================================================


@pytest.fixture
def runner() -> CliRunner:
    """Create a CLI test runner."""
    return CliRunner()


@pytest.fixture
def mock_resolution() -> MagicMock:
    """Create a mock merge resolution."""
    resolution = MagicMock()
    resolution.id = "mr-abc123"
    resolution.worktree_id = "wt-xyz"
    resolution.source_branch = "feature/test"
    resolution.target_branch = "main"
    resolution.status = "pending"
    resolution.tier_used = None
    resolution.created_at = "2024-01-01T00:00:00Z"
    resolution.updated_at = "2024-01-01T00:00:00Z"
    resolution.to_dict.return_value = {
        "id": "mr-abc123",
        "worktree_id": "wt-xyz",
        "source_branch": "feature/test",
        "target_branch": "main",
        "status": "pending",
        "tier_used": None,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }
    return resolution


@pytest.fixture
def mock_conflict() -> MagicMock:
    """Create a mock merge conflict."""
    conflict = MagicMock()
    conflict.id = "mc-conflict1"
    conflict.resolution_id = "mr-abc123"
    conflict.file_path = "src/test.py"
    conflict.status = "pending"
    conflict.ours_content = "our version"
    conflict.theirs_content = "their version"
    conflict.resolved_content = None
    conflict.to_dict.return_value = {
        "id": "mc-conflict1",
        "resolution_id": "mr-abc123",
        "file_path": "src/test.py",
        "status": "pending",
        "ours_content": "our version",
        "theirs_content": "their version",
        "resolved_content": None,
    }
    return conflict


# ==============================================================================
# Import Tests
# ==============================================================================


class TestMergeCliImports:
    """Test that merge CLI module can be imported."""

    def test_import_merge_cli_module(self) -> None:
        """Can import merge CLI module."""
        module = importlib.import_module("gobby.cli.merge")

        assert module.merge.name == "merge"

    def test_import_merge_commands(self) -> None:
        """Can import merge command group."""
        from gobby.cli.merge import merge

        assert merge is not None


# ==============================================================================
# merge start Command Tests
# ==============================================================================


class TestMergeStartCommand:
    """Tests for 'gobby merge start' command."""

    @patch("gobby.cli.merge.get_worktree_context")
    @patch("gobby.cli.merge.get_merge_manager")
    @patch("gobby.cli.merge.get_merge_resolver")
    @patch("gobby.cli.merge.get_project_context")
    def test_merge_start_basic(
        self,
        mock_project_ctx: MagicMock,
        mock_get_resolver: MagicMock,
        mock_get_manager: MagicMock,
        mock_worktree_ctx: MagicMock,
        runner: CliRunner,
        mock_resolution: MagicMock,
    ) -> None:
        """Test basic merge start command."""
        from gobby.cli import cli

        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_worktree_ctx.return_value = None  # Not in a worktree
        mock_manager = MagicMock()
        mock_manager.get_or_create_resolution.return_value = (mock_resolution, True)
        mock_get_manager.return_value = mock_manager

        mock_resolver = MagicMock()
        mock_get_resolver.return_value = mock_resolver

        result = runner.invoke(cli, ["merge", "start", "feature/test"])

        assert result.exit_code == 0
        assert "mr-abc123" in result.output or "feature/test" in result.output

    @patch("gobby.cli.merge.get_worktree_context")
    @patch("gobby.cli.merge.get_merge_manager")
    @patch("gobby.cli.merge.get_merge_resolver")
    @patch("gobby.cli.merge.get_project_context")
    def test_merge_start_with_strategy(
        self,
        mock_project_ctx: MagicMock,
        mock_get_resolver: MagicMock,
        mock_get_manager: MagicMock,
        mock_worktree_ctx: MagicMock,
        runner: CliRunner,
        mock_resolution: MagicMock,
    ) -> None:
        """Test merge start with --strategy option."""
        from gobby.cli import cli

        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_worktree_ctx.return_value = None  # Not in a worktree
        mock_manager = MagicMock()
        mock_manager.get_or_create_resolution.return_value = (mock_resolution, True)
        mock_get_manager.return_value = mock_manager

        mock_resolver = MagicMock()
        mock_get_resolver.return_value = mock_resolver

        result = runner.invoke(cli, ["merge", "start", "feature/test", "--strategy", "ai-only"])

        assert result.exit_code == 0
        assert result.exception is None

    @patch("gobby.cli.merge.get_project_context")
    def test_merge_start_no_project(
        self,
        mock_project_ctx: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test merge start fails without project context."""
        from gobby.cli import cli

        mock_project_ctx.return_value = None

        result = runner.invoke(cli, ["merge", "start", "feature/test"])

        assert result.exit_code != 0 or "error" in result.output.lower()

    def test_merge_start_requires_branch(self, runner: CliRunner) -> None:
        """Test merge start requires source branch argument."""
        from gobby.cli import cli

        result = runner.invoke(cli, ["merge", "start"])

        # Should fail without branch argument
        assert result.exit_code != 0

    @patch("gobby.cli.merge.get_worktree_context")
    @patch("gobby.cli.merge.get_merge_manager")
    @patch("gobby.cli.merge.get_merge_resolver")
    @patch("gobby.cli.merge.get_project_context")
    def test_merge_start_reuses_existing_resolution(
        self,
        mock_project_ctx: MagicMock,
        mock_get_resolver: MagicMock,
        mock_get_manager: MagicMock,
        mock_worktree_ctx: MagicMock,
        runner: CliRunner,
        mock_resolution: MagicMock,
    ) -> None:
        """Test merge start reuses an existing compatible resolution."""
        from gobby.cli import cli

        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_worktree_ctx.return_value = None
        mock_manager = MagicMock()
        mock_manager.get_or_create_resolution.return_value = (mock_resolution, False)
        mock_manager.create_resolution.side_effect = AssertionError("should not insert")
        mock_get_manager.return_value = mock_manager
        mock_get_resolver.return_value = MagicMock()

        result = runner.invoke(cli, ["merge", "start", "feature/test"])

        assert result.exit_code == 0
        assert "mr-abc123" in result.output
        mock_manager.get_or_create_resolution.assert_called_once()
        mock_manager.create_resolution.assert_not_called()


# ==============================================================================
# merge status Command Tests
# ==============================================================================


class TestMergeStatusCommand:
    """Tests for 'gobby merge status' command."""

    @patch("gobby.cli.merge.get_worktree_context")
    @patch("gobby.cli.merge.get_merge_manager")
    @patch("gobby.cli.merge.get_project_context")
    def test_merge_status_basic(
        self,
        mock_project_ctx: MagicMock,
        mock_get_manager: MagicMock,
        mock_worktree_ctx: MagicMock,
        runner: CliRunner,
        mock_resolution: MagicMock,
    ) -> None:
        """Test basic merge status command."""
        from gobby.cli import cli

        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_worktree_ctx.return_value = None  # Not in a worktree
        mock_manager = MagicMock()
        mock_manager.list_resolutions.return_value = [mock_resolution]
        mock_manager.list_conflicts.return_value = []
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["merge", "status"])

        assert result.exit_code == 0
        assert result.exception is None
        assert "merge" in result.output.lower()

    @patch("gobby.cli.merge.get_worktree_context")
    @patch("gobby.cli.merge.get_merge_manager")
    @patch("gobby.cli.merge.get_project_context")
    def test_merge_status_verbose(
        self,
        mock_project_ctx: MagicMock,
        mock_get_manager: MagicMock,
        mock_worktree_ctx: MagicMock,
        runner: CliRunner,
        mock_resolution: MagicMock,
        mock_conflict: MagicMock,
    ) -> None:
        """Test merge status with --verbose option."""
        from gobby.cli import cli

        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_worktree_ctx.return_value = None  # Not in a worktree
        mock_manager = MagicMock()
        mock_manager.list_resolutions.return_value = [mock_resolution]
        mock_manager.list_conflicts.return_value = [mock_conflict]
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["merge", "status", "--verbose"])

        assert result.exit_code == 0
        # Verbose should show conflict details
        assert "src/test.py" in result.output or "conflict" in result.output.lower()

    @patch("gobby.cli.merge.get_worktree_context")
    @patch("gobby.cli.merge.get_merge_manager")
    @patch("gobby.cli.merge.get_project_context")
    def test_merge_status_no_active_merges(
        self,
        mock_project_ctx: MagicMock,
        mock_get_manager: MagicMock,
        mock_worktree_ctx: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test merge status when no active merges."""
        from gobby.cli import cli

        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_worktree_ctx.return_value = None  # Not in a worktree
        mock_manager = MagicMock()
        mock_manager.list_resolutions.return_value = []
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["merge", "status"])

        assert result.exit_code == 0
        assert result.exception is None
        assert "no" in result.output.lower() and "merge" in result.output.lower()


# ==============================================================================
# merge resolve Command Tests
# ==============================================================================


class TestMergeResolveCommand:
    """Tests for 'gobby merge resolve' command."""

    @patch("gobby.cli.merge.conflict_hunks_for_ai", new_callable=AsyncMock)
    @patch("gobby.cli.merge.get_worktree_manager")
    @patch("gobby.cli.merge.get_merge_manager")
    @patch("gobby.cli.merge.get_merge_resolver")
    @patch("gobby.cli.merge.get_project_context")
    def test_merge_resolve_basic(
        self,
        mock_project_ctx: MagicMock,
        mock_get_resolver: MagicMock,
        mock_get_manager: MagicMock,
        mock_get_worktree_manager: MagicMock,
        mock_conflict_hunks: AsyncMock,
        runner: CliRunner,
        mock_conflict: MagicMock,
        mock_resolution: MagicMock,
        tmp_path: Path,
    ) -> None:
        """AI resolve writes content and marks the conflict resolved after success."""
        from gobby.cli import cli

        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_manager = MagicMock()
        mock_manager.get_conflict_by_path.return_value = mock_conflict
        mock_manager.get_resolution.return_value = mock_resolution
        mock_manager.update_conflict.return_value = mock_conflict
        mock_get_manager.return_value = mock_manager

        worktree = MagicMock()
        worktree.worktree_path = str(tmp_path)
        mock_worktree_manager = MagicMock()
        mock_worktree_manager.get.return_value = worktree
        mock_get_worktree_manager.return_value = mock_worktree_manager
        mock_conflict_hunks.return_value = [MagicMock()]

        mock_resolver = MagicMock()
        mock_resolver.resolve_file = AsyncMock(
            return_value=SimpleNamespace(
                success=True,
                needs_human_review=False,
                failure_reason=None,
                resolved_content_by_file={"src/test.py": "merged version\n"},
                tier=SimpleNamespace(value="conflict_only_ai"),
            )
        )
        mock_get_resolver.return_value = mock_resolver

        result = runner.invoke(cli, ["merge", "resolve", "src/test.py"])

        assert result.exit_code == 0
        assert result.exception is None
        assert (tmp_path / "src" / "test.py").read_text() == "merged version\n"
        mock_resolver.resolve_file.assert_awaited_once()
        mock_manager.update_conflict.assert_called_once_with(
            conflict_id="mc-conflict1",
            status="resolved",
            resolved_content="merged version\n",
        )

    @patch("gobby.cli.merge.conflict_hunks_for_ai", new_callable=AsyncMock)
    @patch("gobby.cli.merge.get_worktree_manager")
    @patch("gobby.cli.merge.get_merge_manager")
    @patch("gobby.cli.merge.get_merge_resolver")
    @patch("gobby.cli.merge.get_project_context")
    def test_merge_resolve_ai_failure_does_not_mark_resolved(
        self,
        mock_project_ctx: MagicMock,
        mock_get_resolver: MagicMock,
        mock_get_manager: MagicMock,
        mock_get_worktree_manager: MagicMock,
        mock_conflict_hunks: AsyncMock,
        runner: CliRunner,
        mock_conflict: MagicMock,
        mock_resolution: MagicMock,
        tmp_path: Path,
    ) -> None:
        """AI resolve failure exits without a DB status flip."""
        from gobby.cli import cli

        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_manager = MagicMock()
        mock_manager.get_conflict_by_path.return_value = mock_conflict
        mock_manager.get_resolution.return_value = mock_resolution
        mock_get_manager.return_value = mock_manager

        worktree = MagicMock()
        worktree.worktree_path = str(tmp_path)
        mock_worktree_manager = MagicMock()
        mock_worktree_manager.get.return_value = worktree
        mock_get_worktree_manager.return_value = mock_worktree_manager
        mock_conflict_hunks.return_value = [MagicMock()]

        mock_resolver = MagicMock()
        mock_resolver.resolve_file = AsyncMock(
            return_value=SimpleNamespace(
                success=False,
                needs_human_review=True,
                failure_reason="model failed",
                resolved_content_by_file={},
                tier=SimpleNamespace(value="human_review"),
            )
        )
        mock_get_resolver.return_value = mock_resolver

        result = runner.invoke(cli, ["merge", "resolve", "src/test.py"])

        assert result.exit_code == 1
        assert "AI resolution failed" in result.output
        mock_manager.update_conflict.assert_not_called()
        assert not (tmp_path / "src" / "test.py").exists()

    @patch("gobby.cli.merge.get_merge_manager")
    @patch("gobby.cli.merge.get_project_context")
    def test_merge_resolve_with_strategy(
        self,
        mock_project_ctx: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_conflict: MagicMock,
    ) -> None:
        """Test merge resolve with --strategy option."""
        from gobby.cli import cli

        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_manager = MagicMock()
        mock_manager.get_conflict_by_path.return_value = mock_conflict
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["merge", "resolve", "src/test.py", "--strategy", "human"])

        assert result.exit_code == 0

    @patch("gobby.cli.merge.get_merge_manager")
    @patch("gobby.cli.merge.get_project_context")
    def test_merge_resolve_file_not_found(
        self,
        mock_project_ctx: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test merge resolve when file not found."""
        from gobby.cli import cli

        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_manager = MagicMock()
        mock_manager.get_conflict_by_path.return_value = None
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["merge", "resolve", "nonexistent.py"])

        assert result.exit_code != 0 or "not found" in result.output.lower()

    def test_merge_resolve_requires_file(self, runner: CliRunner) -> None:
        """Test merge resolve requires file argument."""
        from gobby.cli import cli

        result = runner.invoke(cli, ["merge", "resolve"])

        # Should fail without file argument
        assert result.exit_code != 0


# ==============================================================================
# merge apply Command Tests
# ==============================================================================


class TestMergeApplyCommand:
    """Tests for 'gobby merge apply' command."""

    @patch("gobby.cli.merge._run_resolution_tool", new_callable=AsyncMock)
    @patch("gobby.cli.merge.get_worktree_context")
    @patch("gobby.cli.merge.get_merge_manager")
    @patch("gobby.cli.merge.get_project_context")
    def test_merge_apply_basic(
        self,
        mock_project_ctx: MagicMock,
        mock_get_manager: MagicMock,
        mock_worktree_ctx: MagicMock,
        mock_run_resolution_tool: AsyncMock,
        runner: CliRunner,
        mock_resolution: MagicMock,
    ) -> None:
        """Apply delegates to real merge machinery instead of flipping DB status."""
        from gobby.cli import cli

        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_worktree_ctx.return_value = None
        mock_resolution.status = "pending"
        mock_manager = MagicMock()
        mock_manager.get_active_resolution.return_value = mock_resolution
        mock_manager.list_conflicts.return_value = []  # All resolved
        mock_get_manager.return_value = mock_manager
        mock_run_resolution_tool.return_value = {
            "success": True,
            "files_merged": ["src/test.py"],
            "commit_sha": "merged-sha",
        }

        result = runner.invoke(cli, ["merge", "apply"])

        assert result.exit_code == 0
        mock_run_resolution_tool.assert_awaited_once_with(mock_manager, "mr-abc123", "merge_apply")
        mock_manager.update_resolution.assert_not_called()
        assert "commit: merged-sha" in result.output

    @patch("gobby.cli.merge._run_resolution_tool", new_callable=AsyncMock)
    @patch("gobby.cli.merge.get_worktree_context")
    @patch("gobby.cli.merge.get_merge_manager")
    @patch("gobby.cli.merge.get_project_context")
    def test_merge_apply_with_force(
        self,
        mock_project_ctx: MagicMock,
        mock_get_manager: MagicMock,
        mock_worktree_ctx: MagicMock,
        mock_run_resolution_tool: AsyncMock,
        runner: CliRunner,
        mock_resolution: MagicMock,
    ) -> None:
        """Test merge apply with --force option."""
        from gobby.cli import cli

        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_worktree_ctx.return_value = None
        mock_manager = MagicMock()
        mock_manager.get_active_resolution.return_value = mock_resolution
        mock_manager.list_conflicts.return_value = []
        mock_get_manager.return_value = mock_manager
        mock_run_resolution_tool.return_value = {
            "success": True,
            "files_merged": [],
            "commit_sha": "merged-sha",
        }

        result = runner.invoke(cli, ["merge", "apply", "--force"])

        assert result.exit_code == 0
        mock_run_resolution_tool.assert_awaited_once_with(mock_manager, "mr-abc123", "merge_apply")
        mock_manager.update_resolution.assert_not_called()
        assert "Applied merge: mr-abc123" in result.output
        assert "commit: merged-sha" in result.output

    @patch("gobby.cli.merge._run_resolution_tool", new_callable=AsyncMock)
    @patch("gobby.cli.merge.get_worktree_context")
    @patch("gobby.cli.merge.get_merge_manager")
    @patch("gobby.cli.merge.get_project_context")
    def test_merge_apply_failure_does_not_report_success(
        self,
        mock_project_ctx: MagicMock,
        mock_get_manager: MagicMock,
        mock_worktree_ctx: MagicMock,
        mock_run_resolution_tool: AsyncMock,
        runner: CliRunner,
        mock_resolution: MagicMock,
    ) -> None:
        """Apply reports merge-tool failure and avoids success messaging."""
        from gobby.cli import cli

        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_worktree_ctx.return_value = None
        mock_manager = MagicMock()
        mock_manager.get_active_resolution.return_value = mock_resolution
        mock_manager.list_conflicts.return_value = []
        mock_get_manager.return_value = mock_manager
        mock_run_resolution_tool.return_value = {
            "success": False,
            "error": "git commit failed",
        }

        result = runner.invoke(cli, ["merge", "apply"])

        assert result.exit_code == 1
        assert "git commit failed" in result.output
        assert "Applied merge" not in result.output
        mock_manager.update_resolution.assert_not_called()

    @patch("gobby.cli.merge._run_resolution_tool", new_callable=AsyncMock)
    @patch("gobby.cli.merge.get_worktree_context")
    @patch("gobby.cli.merge.get_merge_manager")
    @patch("gobby.cli.merge.get_project_context")
    def test_merge_apply_does_not_use_global_active_resolution(
        self,
        mock_project_ctx: MagicMock,
        mock_get_manager: MagicMock,
        mock_worktree_ctx: MagicMock,
        mock_run_resolution_tool: AsyncMock,
        runner: CliRunner,
    ) -> None:
        """Apply ignores a newer pending merge from another worktree."""
        from gobby.cli import cli

        foreign_resolution = MagicMock()
        foreign_resolution.id = "mr-foreign"
        foreign_resolution.status = "pending"

        def active_resolution(*, worktree_id: str | None = None) -> MagicMock | None:
            return None if worktree_id == "wt-current" else foreign_resolution

        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_worktree_ctx.return_value = {"id": "wt-current", "branch_name": "feature/current"}
        mock_manager = MagicMock()
        mock_manager.get_active_resolution.side_effect = active_resolution
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["merge", "apply"])

        assert result.exit_code == 1
        assert "No active merge operation found" in result.output
        mock_manager.get_active_resolution.assert_called_once_with(worktree_id="wt-current")
        mock_run_resolution_tool.assert_not_awaited()
        mock_manager.list_conflicts.assert_not_called()

    @patch("gobby.cli.merge.get_merge_manager")
    @patch("gobby.cli.merge.get_worktree_context")
    @patch("gobby.cli.merge.get_project_context")
    def test_merge_apply_with_pending_conflicts(
        self,
        mock_project_ctx: MagicMock,
        mock_worktree_ctx: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_resolution: MagicMock,
        mock_conflict: MagicMock,
    ) -> None:
        """Test merge apply fails with pending conflicts."""
        from gobby.cli import cli

        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_worktree_ctx.return_value = None
        mock_manager = MagicMock()
        mock_manager.get_active_resolution.return_value = mock_resolution
        mock_conflict.status = "pending"
        mock_manager.list_conflicts.return_value = [mock_conflict]
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["merge", "apply"])

        # Should fail or warn about pending conflicts
        assert result.exit_code != 0 or "pending" in result.output.lower()
        assert "pending conflict" in result.output.lower()
        mock_manager.get_active_resolution.assert_called_once_with(worktree_id=None)
        mock_manager.list_conflicts.assert_called_once_with(resolution_id="mr-abc123")

    @patch("gobby.cli.merge.get_merge_manager")
    @patch("gobby.cli.merge.get_worktree_context")
    @patch("gobby.cli.merge.get_project_context")
    def test_merge_apply_no_active_merge(
        self,
        mock_project_ctx: MagicMock,
        mock_worktree_ctx: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test merge apply when no active merge."""
        from gobby.cli import cli

        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_worktree_ctx.return_value = None
        mock_manager = MagicMock()
        mock_manager.get_active_resolution.return_value = None
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["merge", "apply"])

        assert result.exit_code != 0 or "no" in result.output.lower()
        assert "No active merge operation found" in result.output
        mock_manager.get_active_resolution.assert_called_once_with(worktree_id=None)
        mock_manager.list_conflicts.assert_not_called()


# ==============================================================================
# merge abort Command Tests
# ==============================================================================


class TestMergeAbortCommand:
    """Tests for 'gobby merge abort' command."""

    @patch("gobby.cli.merge.get_merge_manager")
    @patch("gobby.cli.merge.get_worktree_context")
    @patch("gobby.cli.merge.get_project_context")
    def test_merge_abort_basic(
        self,
        mock_project_ctx: MagicMock,
        mock_worktree_ctx: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_resolution: MagicMock,
    ) -> None:
        """Test basic merge abort command."""
        from gobby.cli import cli

        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_worktree_ctx.return_value = None
        mock_manager = MagicMock()
        mock_manager.get_active_resolution.return_value = mock_resolution
        mock_manager.delete_resolution.return_value = True
        mock_get_manager.return_value = mock_manager

        with patch("gobby.cli.merge._run_resolution_tool", new_callable=AsyncMock) as abort:
            abort.return_value = {"success": True, "resolution_id": mock_resolution.id}
            result = runner.invoke(cli, ["merge", "abort"])

        assert result.exit_code == 0
        assert "abort" in result.output.lower()
        abort.assert_awaited_once_with(mock_manager, mock_resolution.id, "merge_abort")
        mock_manager.delete_resolution.assert_not_called()

    @patch("gobby.cli.merge.get_merge_manager")
    @patch("gobby.cli.merge.get_worktree_context")
    @patch("gobby.cli.merge.get_project_context")
    def test_merge_abort_no_active_merge(
        self,
        mock_project_ctx: MagicMock,
        mock_worktree_ctx: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test merge abort when no active merge."""
        from gobby.cli import cli

        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_worktree_ctx.return_value = None
        mock_manager = MagicMock()
        mock_manager.get_active_resolution.return_value = None
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["merge", "abort"])

        assert result.exit_code != 0 or "no" in result.output.lower()
        assert "No active merge operation to abort" in result.output
        mock_manager.get_active_resolution.assert_called_once_with(worktree_id=None)
        mock_manager.delete_resolution.assert_not_called()

    @patch("gobby.cli.merge.get_merge_manager")
    @patch("gobby.cli.merge.get_worktree_context")
    @patch("gobby.cli.merge.get_project_context")
    def test_merge_abort_does_not_use_global_active_resolution(
        self,
        mock_project_ctx: MagicMock,
        mock_worktree_ctx: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Abort ignores a newer pending merge from another worktree."""
        from gobby.cli import cli

        foreign_resolution = MagicMock()
        foreign_resolution.id = "mr-foreign"
        foreign_resolution.status = "pending"

        def active_resolution(*, worktree_id: str | None = None) -> MagicMock | None:
            return None if worktree_id == "wt-current" else foreign_resolution

        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_worktree_ctx.return_value = {"id": "wt-current", "branch_name": "feature/current"}
        mock_manager = MagicMock()
        mock_manager.get_active_resolution.side_effect = active_resolution
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["merge", "abort"])

        assert result.exit_code == 1
        assert "No active merge operation to abort" in result.output
        mock_manager.get_active_resolution.assert_called_once_with(worktree_id="wt-current")
        mock_manager.delete_resolution.assert_not_called()

    @patch("gobby.cli.merge.get_merge_manager")
    @patch("gobby.cli.merge.get_worktree_context")
    @patch("gobby.cli.merge.get_project_context")
    def test_merge_abort_already_resolved(
        self,
        mock_project_ctx: MagicMock,
        mock_worktree_ctx: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_resolution: MagicMock,
    ) -> None:
        """Test merge abort when already resolved."""
        from gobby.cli import cli

        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_worktree_ctx.return_value = None
        mock_resolution.status = "resolved"
        mock_manager = MagicMock()
        mock_manager.get_active_resolution.return_value = mock_resolution
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["merge", "abort"])

        # Should fail for already resolved merge
        assert result.exit_code != 0 or "resolved" in result.output.lower()
        assert "Cannot abort an already resolved merge" in result.output
        mock_manager.get_active_resolution.assert_called_once_with(worktree_id=None)
        mock_manager.delete_resolution.assert_not_called()


# ==============================================================================
# Output Formatting Tests
# ==============================================================================


class TestMergeOutputFormatting:
    """Tests for merge command output formatting."""

    @patch("gobby.cli.merge.get_worktree_context")
    @patch("gobby.cli.merge.get_merge_manager")
    @patch("gobby.cli.merge.get_project_context")
    def test_status_output_format(
        self,
        mock_project_ctx: MagicMock,
        mock_get_manager: MagicMock,
        mock_worktree_ctx: MagicMock,
        runner: CliRunner,
        mock_resolution: MagicMock,
        mock_conflict: MagicMock,
    ) -> None:
        """Test status command outputs formatted merge info."""
        from gobby.cli import cli

        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_worktree_ctx.return_value = None  # Not in a worktree
        mock_manager = MagicMock()
        mock_manager.list_resolutions.return_value = [mock_resolution]
        mock_manager.list_conflicts.return_value = [mock_conflict]
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["merge", "status"])

        # Check output contains expected fields
        assert result.exit_code == 0
        assert result.exception is None
        # Output should contain branch info or status


# ==============================================================================
# Error Message Tests
# ==============================================================================


class TestMergeErrorMessages:
    """Tests for merge command error messages."""

    @patch("gobby.cli.merge.get_project_context")
    def test_no_project_error_message(
        self,
        mock_project_ctx: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test error message when no project context."""
        from gobby.cli import cli

        mock_project_ctx.return_value = None

        result = runner.invoke(cli, ["merge", "status"])

        # Should show meaningful error
        assert result.exit_code != 0 or "project" in result.output.lower()

    @patch("gobby.cli.merge.get_merge_manager")
    @patch("gobby.cli.merge.get_project_context")
    def test_conflict_resolution_error_message(
        self,
        mock_project_ctx: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test error message when conflict resolution fails."""
        from gobby.cli import cli

        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_manager = MagicMock()
        mock_manager.get_conflict_by_path.side_effect = Exception("Resolution failed")
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["merge", "resolve", "src/test.py"])

        # Should show error message
        assert result.exit_code != 0 or "error" in result.output.lower()


# ==============================================================================
# Worktree Context Integration Tests
# ==============================================================================


class TestMergeWorktreeIntegration:
    """Tests for merge commands integration with worktree context."""

    @patch("gobby.cli.merge.get_merge_manager")
    @patch("gobby.cli.merge.get_worktree_context")
    @patch("gobby.cli.merge.get_project_context")
    def test_merge_start_uses_worktree_context(
        self,
        mock_project_ctx: MagicMock,
        mock_worktree_ctx: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_resolution: MagicMock,
    ) -> None:
        """Test merge start uses current worktree context."""
        from gobby.cli import cli

        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_worktree_ctx.return_value = {"id": "wt-xyz", "branch_name": "main"}
        mock_manager = MagicMock()
        mock_manager.get_or_create_resolution.return_value = (mock_resolution, True)
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["merge", "start", "feature/test"])

        # Should use worktree context for the merge
        assert result.exit_code == 0
        assert result.exception is None

    @patch("gobby.cli.merge.get_merge_manager")
    @patch("gobby.cli.merge.get_worktree_context")
    @patch("gobby.cli.merge.get_project_context")
    def test_merge_in_worktree_directory(
        self,
        mock_project_ctx: MagicMock,
        mock_worktree_ctx: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_resolution: MagicMock,
    ) -> None:
        """Test merge operations work in worktree directory."""
        from gobby.cli import cli

        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_worktree_ctx.return_value = {
            "id": "wt-xyz",
            "branch_name": "feature/work",
            "worktree_path": "/tmp/gobby-worktrees/feature-work",
        }
        mock_manager = MagicMock()
        mock_manager.list_resolutions.return_value = [mock_resolution]
        mock_manager.list_conflicts.return_value = []
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["merge", "status"])

        assert result.exit_code == 0
        assert result.exception is None
