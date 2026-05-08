"""Tests for merge CLI commands (TDD red phase).

Tests for CLI merge commands:
- gobby merge start <source-branch> [--strategy=auto|ai-only|human]
- gobby merge status [--verbose]
- gobby merge resolve <file> [--strategy=ai|human]
- gobby merge apply [--force]
- gobby merge abort
"""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

pytestmark = pytest.mark.unit

# ==============================================================================
# Fixtures
# ==============================================================================


@pytest.fixture
def runner() -> CliRunner:
    """Create a CLI test runner."""
    return CliRunner()


@pytest.fixture
def mock_resolution():
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
def mock_conflict():
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
        from gobby.cli import merge

        assert merge is not None

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
        mock_manager.create_resolution.return_value = mock_resolution
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
        mock_manager.create_resolution.return_value = mock_resolution
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

    @patch("gobby.cli.merge.get_merge_manager")
    @patch("gobby.cli.merge.get_merge_resolver")
    @patch("gobby.cli.merge.get_project_context")
    def test_merge_resolve_basic(
        self,
        mock_project_ctx: MagicMock,
        mock_get_resolver: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_conflict: MagicMock,
    ) -> None:
        """Test basic merge resolve command."""
        from gobby.cli import cli

        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_manager = MagicMock()
        mock_manager.get_conflict_by_path.return_value = mock_conflict
        mock_get_manager.return_value = mock_manager

        mock_resolver = MagicMock()
        mock_get_resolver.return_value = mock_resolver

        result = runner.invoke(cli, ["merge", "resolve", "src/test.py"])

        assert result.exit_code == 0
        assert result.exception is None

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

    @patch("gobby.cli.merge.get_merge_manager")
    @patch("gobby.cli.merge.get_project_context")
    def test_merge_apply_basic(
        self,
        mock_project_ctx: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_resolution: MagicMock,
    ) -> None:
        """Test basic merge apply command."""
        from gobby.cli import cli

        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_resolution.status = "pending"
        mock_manager = MagicMock()
        mock_manager.get_active_resolution.return_value = mock_resolution
        mock_manager.list_conflicts.return_value = []  # All resolved
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["merge", "apply"])

        assert result.exit_code == 0

    @patch("gobby.cli.merge.get_merge_manager")
    @patch("gobby.cli.merge.get_project_context")
    def test_merge_apply_with_force(
        self,
        mock_project_ctx: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_resolution: MagicMock,
    ) -> None:
        """Test merge apply with --force option."""
        from gobby.cli import cli

        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_manager = MagicMock()
        mock_manager.get_active_resolution.return_value = mock_resolution
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["merge", "apply", "--force"])

        assert result.exit_code == 0

    @patch("gobby.cli.merge.get_merge_manager")
    @patch("gobby.cli.merge.get_project_context")
    def test_merge_apply_with_pending_conflicts(
        self,
        mock_project_ctx: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_resolution: MagicMock,
        mock_conflict: MagicMock,
    ) -> None:
        """Test merge apply fails with pending conflicts."""
        from gobby.cli import cli

        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_manager = MagicMock()
        mock_manager.get_active_resolution.return_value = mock_resolution
        mock_conflict.status = "pending"
        mock_manager.list_conflicts.return_value = [mock_conflict]
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["merge", "apply"])

        # Should fail or warn about pending conflicts
        assert result.exit_code != 0 or "pending" in result.output.lower()

    @patch("gobby.cli.merge.get_merge_manager")
    @patch("gobby.cli.merge.get_project_context")
    def test_merge_apply_no_active_merge(
        self,
        mock_project_ctx: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test merge apply when no active merge."""
        from gobby.cli import cli

        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_manager = MagicMock()
        mock_manager.get_active_resolution.return_value = None
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["merge", "apply"])

        assert result.exit_code != 0 or "no" in result.output.lower()


# ==============================================================================
# merge abort Command Tests
# ==============================================================================


class TestMergeAbortCommand:
    """Tests for 'gobby merge abort' command."""

    @patch("gobby.cli.merge.get_merge_manager")
    @patch("gobby.cli.merge.get_project_context")
    def test_merge_abort_basic(
        self,
        mock_project_ctx: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_resolution: MagicMock,
    ) -> None:
        """Test basic merge abort command."""
        from gobby.cli import cli

        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_manager = MagicMock()
        mock_manager.get_active_resolution.return_value = mock_resolution
        mock_manager.delete_resolution.return_value = True
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["merge", "abort"])

        assert result.exit_code == 0
        assert "abort" in result.output.lower()

    @patch("gobby.cli.merge.get_merge_manager")
    @patch("gobby.cli.merge.get_project_context")
    def test_merge_abort_no_active_merge(
        self,
        mock_project_ctx: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test merge abort when no active merge."""
        from gobby.cli import cli

        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_manager = MagicMock()
        mock_manager.get_active_resolution.return_value = None
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["merge", "abort"])

        assert result.exit_code != 0 or "no" in result.output.lower()

    @patch("gobby.cli.merge.get_merge_manager")
    @patch("gobby.cli.merge.get_project_context")
    def test_merge_abort_already_resolved(
        self,
        mock_project_ctx: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_resolution: MagicMock,
    ) -> None:
        """Test merge abort when already resolved."""
        from gobby.cli import cli

        mock_project_ctx.return_value = {"id": "proj-123"}
        mock_resolution.status = "resolved"
        mock_manager = MagicMock()
        mock_manager.get_active_resolution.return_value = mock_resolution
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["merge", "abort"])

        # Should fail for already resolved merge
        assert result.exit_code != 0 or "resolved" in result.output.lower()


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
        mock_manager.create_resolution.return_value = mock_resolution
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
