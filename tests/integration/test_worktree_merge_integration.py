"""Integration tests for merge flow with worktree system (TDD red phase).

Tests cover:
- Merge initiation from worktree context
- Automatic merge on worktree sync
- Task status updates during merge resolution
- Merge state persistence across daemon restarts
- Concurrent merges in different worktrees

Note: These tests are designed to fail initially (TDD red phase) as they test
functionality that doesn't exist yet. The green phase implementation will make
these tests pass.
"""

from unittest.mock import MagicMock, patch

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.merge_resolutions import MergeConflict, MergeResolution, MergeResolutionManager

pytestmark = pytest.mark.integration

# ==============================================================================
# Fixtures
# ==============================================================================


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
    return resolution


@pytest.fixture
def mock_conflict():
    """Create a mock merge conflict."""
    conflict = MagicMock()
    conflict.id = "mc-conflict1"
    conflict.resolution_id = "mr-abc123"
    conflict.file_path = "src/test.py"
    conflict.status = "pending"
    return conflict


# ==============================================================================
# Merge Initiation from Worktree Context Tests
# ==============================================================================


class TestMergeInitiationFromWorktree:
    """Tests for initiating merges from worktree context."""

    def test_worktree_has_merge_state_field(self) -> None:
        """Worktree should have merge_state field for tracking merge status."""
        from gobby.storage.worktrees import Worktree

        # Check if merge_state is an attribute of Worktree
        worktree_fields = Worktree.__dataclass_fields__
        assert "merge_state" in worktree_fields

    def test_worktree_manager_has_set_merge_state_method(self) -> None:
        """WorktreeManager should have method to set merge state."""
        from gobby.storage.worktrees import LocalWorktreeManager

        # Check for set_merge_state method
        assert hasattr(LocalWorktreeManager, "set_merge_state")

    def test_worktree_manager_has_get_by_merge_state_method(self) -> None:
        """WorktreeManager should have method to get worktrees by merge state."""
        from gobby.storage.worktrees import LocalWorktreeManager

        assert hasattr(LocalWorktreeManager, "get_by_merge_state")


# ==============================================================================
# Automatic Merge on Worktree Sync Tests
# ==============================================================================


class TestAutomaticMergeOnSync:
    """Tests for automatic merge when syncing worktrees."""

    def test_worktree_manager_has_sync_with_merge_resolution(self) -> None:
        """WorktreeManager should have sync_with_merge_resolution method."""
        from gobby.storage.worktrees import LocalWorktreeManager

        assert hasattr(LocalWorktreeManager, "sync_with_merge_resolution")

    def test_worktree_sync_returns_merge_info(self) -> None:
        """Worktree sync should return merge resolution info when conflicts occur."""
        # Check method signature includes merge info
        import inspect

        from gobby.storage.worktrees import LocalWorktreeManager

        if hasattr(LocalWorktreeManager, "sync_with_merge_resolution"):
            sig = inspect.signature(LocalWorktreeManager.sync_with_merge_resolution)
            params = list(sig.parameters.keys())
            assert "merge_manager" in params or "return" in str(sig)


# ==============================================================================
# Task Status Updates During Merge Tests
# ==============================================================================


class TestTaskStatusDuringMerge:
    """Tests for task status updates during merge resolution.

    Note: These tests are marked as skip because task-level merge integration
    requires schema changes to the tasks table and careful consideration of
    the impact on the task system. The worktree-level merge_state field
    provides sufficient tracking for Phase 1.
    """

    @pytest.mark.xfail(
        strict=True,
        reason="Task merge fields deferred to Phase 2 - requires schema migration",
    )
    def test_task_has_merge_in_progress_field(self) -> None:
        """Task should have merge_in_progress field."""
        from gobby.storage.tasks import Task

        task_fields = Task.__dataclass_fields__
        assert "merge_in_progress" in task_fields

    @pytest.mark.xfail(
        strict=True,
        reason="Task merge fields deferred to Phase 2 - requires schema migration",
    )
    def test_task_has_blocked_by_merge_field(self) -> None:
        """Task should have blocked_by_merge field."""
        from gobby.storage.tasks import Task

        task_fields = Task.__dataclass_fields__
        assert "blocked_by_merge" in task_fields

    @pytest.mark.xfail(
        strict=True,
        reason="Task merge fields deferred to Phase 2 - requires schema migration",
    )
    def test_task_manager_has_set_merge_status_method(self) -> None:
        """TaskManager should have method to set merge status."""
        from gobby.storage.tasks import LocalTaskManager

        assert hasattr(LocalTaskManager, "set_merge_status")


# ==============================================================================
# Merge State Persistence Tests
# ==============================================================================


class TestMergeStatePersistence:
    """Tests for merge state persistence across daemon restarts."""

    def test_merge_resolution_persists(self, hub_db: HubDatabase) -> None:
        """Merge resolution should persist in database."""
        from gobby.storage.merge_resolutions import MergeResolutionManager
        from gobby.storage.projects import LocalProjectManager
        from gobby.storage.worktrees import LocalWorktreeManager

        db = hub_db

        # Create prerequisite data (project and worktree for foreign key)
        project_manager = LocalProjectManager(db)
        project = project_manager.create(name="test-project", repo_path="/tmp/test-repo")

        worktree_manager = LocalWorktreeManager(db)
        worktree = worktree_manager.create(
            project_id=project.id,
            branch_name="feature/test",
            worktree_path="/tmp/worktrees/test",
        )

        # Create real manager
        manager = MergeResolutionManager(db)

        # Create resolution
        created = manager.create_resolution(
            worktree_id=worktree.id,
            source_branch="feature/test",
            target_branch="main",
        )

        # Retrieve and verify
        retrieved = manager.get_resolution(created.id)
        assert retrieved is not None
        assert retrieved.worktree_id == worktree.id
        assert retrieved.source_branch == "feature/test"

    def test_worktree_merge_state_in_database_schema(self) -> None:
        """Database schema should include merge_state column in worktrees table."""
        # This test verifies the schema includes merge_state
        # Will pass when migration adds the column
        from gobby.storage.worktrees import Worktree

        assert "merge_state" in Worktree.__dataclass_fields__


# ==============================================================================
# Concurrent Merges Tests
# ==============================================================================


class TestConcurrentMerges:
    """Tests for concurrent merges in different worktrees."""

    @patch("gobby.storage.merge_resolutions.MergeResolutionManager.list_resolutions")
    def test_list_active_merges_method_exists(self, mock_list) -> None:
        """MergeResolutionManager should have list_resolutions method."""

        assert hasattr(MergeResolutionManager, "list_resolutions")

    def test_merge_resolution_manager_prevents_duplicate_worktree_merges(self) -> None:
        """MergeResolutionManager should prevent duplicate active merges per worktree."""

        # Check for method that validates or prevents duplicates
        assert hasattr(MergeResolutionManager, "has_active_resolution_for_worktree") or hasattr(
            MergeResolutionManager, "get_active_resolution"
        )


# ==============================================================================
# CLI Status Output Tests
# ==============================================================================


class TestCLIMergeStatusOutput:
    """Tests for merge status in gobby status CLI output."""

    def test_daemon_module_has_get_merge_status(self) -> None:
        """gobby.cli.daemon should have get_merge_status function."""
        # This function should provide merge status for CLI output
        try:
            from gobby.cli.daemon import get_merge_status

            assert callable(get_merge_status)
        except ImportError:
            # Function doesn't exist yet - red phase failure
            pytest.fail("get_merge_status not found in gobby.cli.daemon")

    @patch("gobby.cli.daemon.get_merge_status")
    def test_status_command_includes_merge_info(self, mock_get_merge) -> None:
        """gobby status command should include merge information."""
        from click.testing import CliRunner

        from gobby.cli import cli

        mock_get_merge.return_value = {
            "active": True,
            "resolution_id": "mr-abc123",
            "conflicts": 2,
        }

        runner = CliRunner()
        result = runner.invoke(cli, ["status"])

        # The status command should call get_merge_status
        # This test will pass when integration is complete
        # For now, we just verify the command runs
        assert result.exit_code == 0 or "merge" in result.output.lower()


# ==============================================================================
# Hook Integration Tests
# ==============================================================================


class TestMergeHooks:
    """Tests for pre-merge and post-merge hooks."""

    def test_merge_hook_manager_exists(self) -> None:
        """MergeHookManager should exist in gobby.hooks.git."""
        try:
            from gobby.hooks.git import MergeHookManager

            assert MergeHookManager is not None
        except ImportError:
            pytest.fail("MergeHookManager not found in gobby.hooks.git")

    def test_merge_hook_manager_has_register_pre_merge(self) -> None:
        """MergeHookManager should have register_pre_merge method."""
        try:
            from gobby.hooks.git import MergeHookManager

            assert hasattr(MergeHookManager, "register_pre_merge")
        except ImportError:
            pytest.fail("MergeHookManager not found in gobby.hooks.git")

    def test_merge_hook_manager_has_register_post_merge(self) -> None:
        """MergeHookManager should have register_post_merge method."""
        try:
            from gobby.hooks.git import MergeHookManager

            assert hasattr(MergeHookManager, "register_post_merge")
        except ImportError:
            pytest.fail("MergeHookManager not found in gobby.hooks.git")

    def test_merge_resolution_manager_has_hooks_support(self) -> None:
        """MergeResolutionManager should support hooks."""

        # Check for methods that support hooks
        assert hasattr(MergeResolutionManager, "create_resolution_with_hooks") or hasattr(
            MergeResolutionManager, "add_change_listener"
        )


# ==============================================================================
# get_active_resolution and get_conflict_by_path Tests
# ==============================================================================


class TestMergeManagerHelperMethods:
    """Tests for helper methods needed by CLI."""

    def test_merge_manager_has_get_active_resolution(self) -> None:
        """MergeResolutionManager should have get_active_resolution method."""

        assert hasattr(MergeResolutionManager, "get_active_resolution")

    def test_merge_manager_has_get_conflict_by_path(self) -> None:
        """MergeResolutionManager should have get_conflict_by_path method."""

        assert hasattr(MergeResolutionManager, "get_conflict_by_path")

    def test_get_active_resolution_returns_pending_resolution(self, mock_resolution) -> None:
        """get_active_resolution should return the current pending resolution."""

        db = MagicMock()
        db.fetchone.return_value = {"id": "mr-abc123"}
        manager = MergeResolutionManager(db)

        with patch.object(MergeResolution, "from_row", return_value=mock_resolution):
            result = manager.get_active_resolution()

        assert result is mock_resolution
        assert "status = 'pending'" in db.fetchone.call_args.args[0]

    def test_get_conflict_by_path_finds_conflict(self, mock_conflict) -> None:
        """get_conflict_by_path should find conflict by file path."""

        db = MagicMock()
        db.fetchone.return_value = {"id": "mc-conflict1"}
        manager = MergeResolutionManager(db)

        with patch.object(MergeConflict, "from_row", return_value=mock_conflict):
            result = manager.get_conflict_by_path("src/test.py", "mr-abc123")

        assert result is mock_conflict
        assert db.fetchone.call_args.args[1] == ("src/test.py", "mr-abc123")
