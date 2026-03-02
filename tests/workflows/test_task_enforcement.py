"""Tests for task enforcement actions."""

import subprocess
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from gobby.workflows.definitions import WorkflowState
from gobby.workflows.enforcement import (
    capture_baseline_dirty_files,
    require_commit_before_stop,
    require_task_complete,
    require_task_review_or_close_before_stop,
    validate_session_task_scope,
)
from gobby.workflows.git_utils import get_dirty_files

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_task_manager():
    """Create mock task manager."""
    return MagicMock()


@pytest.fixture
def workflow_state():
    """Create a workflow state with empty variables."""
    return WorkflowState(
        session_id="test-session",
        workflow_name="test-workflow",
        step="test-step",
        step_entered_at=datetime.now(UTC),
        variables={},
    )


# =============================================================================
# Tests for _get_dirty_files helper
# =============================================================================


class TestGetDirtyFiles:
    """Tests for get_dirty_files helper function."""

    def test_parses_git_status_output(self, monkeypatch) -> None:
        """Parse git status --porcelain output correctly."""
        import gobby.workflows.git_utils as git_utils

        mock_result = MagicMock(
            returncode=0,
            stdout=" M src/file.py\n?? new_file.py\nA  staged.py",
            stderr="",
        )
        monkeypatch.setattr(git_utils.subprocess, "run", lambda *a, **k: mock_result)

        result = get_dirty_files("/test/path")
        assert result == {"src/file.py", "new_file.py", "staged.py"}

    def test_excludes_gobby_directory(self, monkeypatch) -> None:
        """Files in .gobby/ are excluded from dirty files."""
        import gobby.workflows.git_utils as git_utils

        mock_result = MagicMock(
            returncode=0,
            stdout=" M src/file.py\n M .gobby/tasks.jsonl\n?? .gobby/new.json",
            stderr="",
        )
        monkeypatch.setattr(git_utils.subprocess, "run", lambda *a, **k: mock_result)

        result = get_dirty_files("/test/path")

        assert result == {"src/file.py"}
        assert ".gobby/tasks.jsonl" not in result
        assert ".gobby/new.json" not in result

    def test_handles_renames(self, monkeypatch) -> None:
        """Parse rename format correctly (old -> new)."""
        import gobby.workflows.git_utils as git_utils

        mock_result = MagicMock(
            returncode=0,
            stdout="R  old_name.py -> new_name.py",
            stderr="",
        )
        monkeypatch.setattr(git_utils.subprocess, "run", lambda *a, **k: mock_result)

        result = get_dirty_files("/test/path")
        # Should capture the old name (source of rename)
        assert result == {"old_name.py"}

    def test_returns_empty_set_on_no_changes(self, monkeypatch) -> None:
        """Empty output returns empty set."""
        import gobby.workflows.git_utils as git_utils

        mock_result = MagicMock(returncode=0, stdout="", stderr="")
        monkeypatch.setattr(git_utils.subprocess, "run", lambda *a, **k: mock_result)

        result = get_dirty_files("/test/path")
        assert result == set()

    def test_returns_empty_set_on_git_failure(self, monkeypatch) -> None:
        """Git failure returns empty set."""
        import gobby.workflows.git_utils as git_utils

        mock_result = MagicMock(returncode=128, stdout="", stderr="fatal: not a git repository")
        monkeypatch.setattr(git_utils.subprocess, "run", lambda *a, **k: mock_result)

        result = get_dirty_files("/test/path")
        assert result == set()

    def test_returns_empty_set_on_timeout(self, monkeypatch) -> None:
        """Timeout returns empty set."""
        import gobby.workflows.git_utils as git_utils

        def raise_timeout(*a, **k):
            raise subprocess.TimeoutExpired(cmd="git", timeout=10)

        monkeypatch.setattr(git_utils.subprocess, "run", raise_timeout)

        result = get_dirty_files("/test/path")
        assert result == set()

    def test_returns_empty_set_on_file_not_found(self, monkeypatch) -> None:
        """Git not found returns empty set."""
        import gobby.workflows.git_utils as git_utils

        def raise_fnf(*a, **k):
            raise FileNotFoundError("git not found")

        monkeypatch.setattr(git_utils.subprocess, "run", raise_fnf)

        result = get_dirty_files("/test/path")
        assert result == set()

    def test_returns_empty_set_on_generic_error(self, monkeypatch) -> None:
        """Generic error returns empty set."""
        import gobby.workflows.git_utils as git_utils

        def raise_oserror(*a, **k):
            raise OSError("Unexpected error")

        monkeypatch.setattr(git_utils.subprocess, "run", raise_oserror)

        result = get_dirty_files("/test/path")
        assert result == set()


# =============================================================================
# Tests for capture_baseline_dirty_files
# =============================================================================


class TestCaptureBaselineDirtyFiles:
    """Tests for capture_baseline_dirty_files action."""

    async def test_no_workflow_state_returns_none(self):
        """When no workflow_state, return None."""
        result = await capture_baseline_dirty_files(
            workflow_state=None,
            project_path="/test/path",
        )
        assert result is None

    async def test_captures_dirty_files_as_baseline(self, workflow_state):
        """Captures current dirty files and stores in workflow_state."""
        with patch("gobby.workflows.enforcement.commit_policy.get_dirty_files") as mock_get_dirty:
            mock_get_dirty.return_value = {"src/file1.py", "src/file2.py"}

            result = await capture_baseline_dirty_files(
                workflow_state=workflow_state,
                project_path="/test/path",
            )

        assert result is not None
        assert result["baseline_captured"] is True
        assert result["file_count"] == 2
        assert set(result["files"]) == {"src/file1.py", "src/file2.py"}

        # Check stored in workflow_state (as list, not set)
        baseline = workflow_state.variables["baseline_dirty_files"]
        assert isinstance(baseline, list)
        assert set(baseline) == {"src/file1.py", "src/file2.py"}

    async def test_captures_empty_baseline(self, workflow_state):
        """Captures empty baseline when no dirty files."""
        with patch("gobby.workflows.enforcement.commit_policy.get_dirty_files") as mock_get_dirty:
            mock_get_dirty.return_value = set()

            result = await capture_baseline_dirty_files(
                workflow_state=workflow_state,
                project_path="/test/path",
            )

        assert result is not None
        assert result["baseline_captured"] is True
        assert result["file_count"] == 0
        assert result["files"] == []
        assert workflow_state.variables["baseline_dirty_files"] == []


# =============================================================================
# Tests for require_commit_before_stop
# =============================================================================


class TestRequireCommitBeforeStop:
    """Tests for require_commit_before_stop action."""

    async def test_no_workflow_state_allows(self):
        """When no workflow_state, allow stop."""
        result = await require_commit_before_stop(
            workflow_state=None,
            project_path="/test/path",
            task_manager=MagicMock(),
        )
        assert result is None

    async def test_no_claimed_task_allows(self, workflow_state):
        """When no claimed_task_id, allow stop."""
        result = await require_commit_before_stop(
            workflow_state=workflow_state,
            project_path="/test/path",
            task_manager=MagicMock(),
        )
        assert result is None

    async def test_task_no_longer_in_progress_clears_state(self, workflow_state, mock_task_manager):
        """When task status changed, clear workflow state and allow."""
        workflow_state.variables["claimed_task_id"] = "gt-abc123"
        workflow_state.variables["task_claimed"] = True

        # Task exists but is now closed
        mock_task = MagicMock()
        mock_task.status = "closed"
        mock_task_manager.get_task.return_value = mock_task

        result = await require_commit_before_stop(
            workflow_state=workflow_state,
            project_path="/test/path",
            task_manager=mock_task_manager,
        )

        assert result is None
        # State should be cleared
        assert workflow_state.variables["claimed_task_id"] is None
        assert workflow_state.variables["task_claimed"] is False

    async def test_task_not_found_clears_state(self, workflow_state, mock_task_manager):
        """When task no longer exists, clear workflow state and allow."""
        workflow_state.variables["claimed_task_id"] = "gt-deleted"
        workflow_state.variables["task_claimed"] = True

        mock_task_manager.get_task.return_value = None

        result = await require_commit_before_stop(
            workflow_state=workflow_state,
            project_path="/test/path",
            task_manager=mock_task_manager,
        )

        assert result is None
        assert workflow_state.variables["claimed_task_id"] is None
        assert workflow_state.variables["task_claimed"] is False

    async def test_no_uncommitted_changes_allows(self, workflow_state, mock_task_manager):
        """When git status shows no changes, allow stop."""
        workflow_state.variables["claimed_task_id"] = "gt-abc123"

        mock_task = MagicMock()
        mock_task.status = "in_progress"
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.workflows.enforcement.commit_policy.get_dirty_files") as mock_get_dirty:
            mock_get_dirty.return_value = set()

            result = await require_commit_before_stop(
                workflow_state=workflow_state,
                project_path="/test/path",
                task_manager=mock_task_manager,
            )

        assert result is None

    async def test_uncommitted_changes_blocks(self, workflow_state, mock_task_manager):
        """When git status shows NEW changes (not in baseline), block stop."""
        workflow_state.variables["claimed_task_id"] = "gt-abc123"
        workflow_state.variables["baseline_dirty_files"] = []  # No baseline files

        mock_task = MagicMock()
        mock_task.status = "in_progress"
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.workflows.enforcement.commit_policy.get_dirty_files") as mock_get_dirty:
            mock_get_dirty.return_value = {"src/file.py", "new_file.py"}

            result = await require_commit_before_stop(
                workflow_state=workflow_state,
                project_path="/test/path",
                task_manager=mock_task_manager,
            )

        assert result is not None
        assert result["decision"] == "block"
        assert "gt-abc123" in result["reason"]
        assert "uncommitted" in result["reason"]
        assert "close_task" in result["reason"]

    async def test_preexisting_dirty_files_allows(self, workflow_state, mock_task_manager):
        """Pre-existing dirty files (in baseline) are ignored."""
        workflow_state.variables["claimed_task_id"] = "gt-abc123"
        # Baseline captured at session start
        workflow_state.variables["baseline_dirty_files"] = [
            "src/preexisting.py",
            "config.yaml",
        ]

        mock_task = MagicMock()
        mock_task.status = "in_progress"
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.workflows.enforcement.commit_policy.get_dirty_files") as mock_get_dirty:
            # Same files as baseline - no NEW changes
            mock_get_dirty.return_value = {"src/preexisting.py", "config.yaml"}

            result = await require_commit_before_stop(
                workflow_state=workflow_state,
                project_path="/test/path",
                task_manager=mock_task_manager,
            )

        assert result is None

    async def test_new_changes_with_baseline_blocks(self, workflow_state, mock_task_manager):
        """New changes beyond baseline are blocked."""
        workflow_state.variables["claimed_task_id"] = "gt-abc123"
        # Some files were already dirty at session start
        workflow_state.variables["baseline_dirty_files"] = ["src/preexisting.py"]

        mock_task = MagicMock()
        mock_task.status = "in_progress"
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.workflows.enforcement.commit_policy.get_dirty_files") as mock_get_dirty:
            # New files added during session
            mock_get_dirty.return_value = {
                "src/preexisting.py",  # In baseline
                "src/new_file.py",  # NEW - should trigger block
            }

            result = await require_commit_before_stop(
                workflow_state=workflow_state,
                project_path="/test/path",
                task_manager=mock_task_manager,
            )

        assert result is not None
        assert result["decision"] == "block"
        # Should only mention the NEW file
        assert "src/new_file.py" in result["reason"]

    async def test_no_baseline_treats_all_as_new(self, workflow_state, mock_task_manager):
        """When no baseline, all dirty files are treated as new."""
        workflow_state.variables["claimed_task_id"] = "gt-abc123"
        # No baseline captured (e.g., workflow activated after session started)

        mock_task = MagicMock()
        mock_task.status = "in_progress"
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.workflows.enforcement.commit_policy.get_dirty_files") as mock_get_dirty:
            mock_get_dirty.return_value = {"src/file.py"}

            result = await require_commit_before_stop(
                workflow_state=workflow_state,
                project_path="/test/path",
                task_manager=mock_task_manager,
            )

        assert result is not None
        assert result["decision"] == "block"

    async def test_block_reason_lists_new_dirty_files(self, workflow_state, mock_task_manager):
        """Block reason includes list of new dirty files."""
        workflow_state.variables["claimed_task_id"] = "gt-abc123"
        workflow_state.variables["baseline_dirty_files"] = []

        mock_task = MagicMock()
        mock_task.status = "in_progress"
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.workflows.enforcement.commit_policy.get_dirty_files") as mock_get_dirty:
            mock_get_dirty.return_value = {"src/a.py", "src/b.py", "src/c.py"}

            result = await require_commit_before_stop(
                workflow_state=workflow_state,
                project_path="/test/path",
                task_manager=mock_task_manager,
            )

        assert result is not None
        assert "3 uncommitted" in result["reason"]
        assert "src/a.py" in result["reason"]
        assert "src/b.py" in result["reason"]
        assert "src/c.py" in result["reason"]

    async def test_block_reason_truncates_long_file_list(self, workflow_state, mock_task_manager):
        """Block reason truncates file list if more than 10 files."""
        workflow_state.variables["claimed_task_id"] = "gt-abc123"
        workflow_state.variables["baseline_dirty_files"] = []

        mock_task = MagicMock()
        mock_task.status = "in_progress"
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.workflows.enforcement.commit_policy.get_dirty_files") as mock_get_dirty:
            # 15 files
            mock_get_dirty.return_value = {f"src/file{i}.py" for i in range(15)}

            result = await require_commit_before_stop(
                workflow_state=workflow_state,
                project_path="/test/path",
                task_manager=mock_task_manager,
            )

        assert result is not None
        assert "15 uncommitted" in result["reason"]
        assert "and 5 more files" in result["reason"]

    async def test_max_block_count_allows(self, workflow_state, mock_task_manager):
        """After 3 blocks, allow to prevent infinite loop."""
        workflow_state.variables["claimed_task_id"] = "gt-abc123"
        workflow_state.variables["_commit_block_count"] = 3  # Already at max

        mock_task = MagicMock()
        mock_task.status = "in_progress"
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.workflows.enforcement.commit_policy.get_dirty_files") as mock_get_dirty:
            mock_get_dirty.return_value = {"src/file.py"}

            result = await require_commit_before_stop(
                workflow_state=workflow_state,
                project_path="/test/path",
                task_manager=mock_task_manager,
            )

        assert result is None

    async def test_block_count_increments(self, workflow_state, mock_task_manager):
        """Block count increments on each block."""
        workflow_state.variables["claimed_task_id"] = "gt-abc123"
        workflow_state.variables["baseline_dirty_files"] = []

        mock_task = MagicMock()
        mock_task.status = "in_progress"
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.workflows.enforcement.commit_policy.get_dirty_files") as mock_get_dirty:
            mock_get_dirty.return_value = {"src/file.py"}

            # First block
            result = await require_commit_before_stop(
                workflow_state=workflow_state,
                project_path="/test/path",
                task_manager=mock_task_manager,
            )

            assert result is not None
            assert workflow_state.variables["_commit_block_count"] == 1

            # Second block
            result = await require_commit_before_stop(
                workflow_state=workflow_state,
                project_path="/test/path",
                task_manager=mock_task_manager,
            )

            assert result is not None
            assert workflow_state.variables["_commit_block_count"] == 2

    async def test_no_task_manager_skips_status_check(self, workflow_state):
        """When no task_manager, skip task status check but still check git."""
        workflow_state.variables["claimed_task_id"] = "gt-abc123"
        workflow_state.variables["baseline_dirty_files"] = []

        with patch("gobby.workflows.enforcement.commit_policy.get_dirty_files") as mock_get_dirty:
            mock_get_dirty.return_value = {"src/file.py"}

            result = await require_commit_before_stop(
                workflow_state=workflow_state,
                project_path="/test/path",
                task_manager=None,  # No task manager
            )

        # Should still block because git shows changes
        assert result is not None
        assert result["decision"] == "block"

    async def test_block_reason_includes_instructions(self, workflow_state, mock_task_manager):
        """Block reason includes commit and close instructions."""
        workflow_state.variables["claimed_task_id"] = "gt-xyz789"
        workflow_state.variables["baseline_dirty_files"] = []

        mock_task = MagicMock()
        mock_task.status = "in_progress"
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.workflows.enforcement.commit_policy.get_dirty_files") as mock_get_dirty:
            mock_get_dirty.return_value = {"file.txt"}

            result = await require_commit_before_stop(
                workflow_state=workflow_state,
                project_path="/test/path",
                task_manager=mock_task_manager,
            )

        assert result is not None
        assert "[gt-xyz789]" in result["reason"]
        assert 'close_task(task_id="gt-xyz789"' in result["reason"]


# =============================================================================
# Tests for require_task_review_or_close_before_stop
# =============================================================================


class TestRequireTaskReviewOrCloseBeforeStop:
    """Tests for require_task_review_or_close_before_stop action."""

    async def test_no_workflow_state_allows(self):
        """When no workflow_state, allow stop."""
        result = await require_task_review_or_close_before_stop(
            workflow_state=None,
            task_manager=MagicMock(),
        )
        assert result is None

    async def test_no_claimed_task_allows(self, workflow_state):
        """When no claimed_task_id, allow stop."""
        result = await require_task_review_or_close_before_stop(
            workflow_state=workflow_state,
            task_manager=MagicMock(),
        )
        assert result is None

    async def test_no_task_manager_allows(self, workflow_state):
        """When no task_manager, allow stop."""
        workflow_state.variables["claimed_task_id"] = "gt-abc123"

        result = await require_task_review_or_close_before_stop(
            workflow_state=workflow_state,
            task_manager=None,
        )
        assert result is None

    async def test_task_not_found_clears_state(self, workflow_state, mock_task_manager):
        """When task no longer exists, clear workflow state and allow."""
        workflow_state.variables["claimed_task_id"] = "gt-deleted"
        workflow_state.variables["task_claimed"] = True

        mock_task_manager.get_task.return_value = None

        result = await require_task_review_or_close_before_stop(
            workflow_state=workflow_state,
            task_manager=mock_task_manager,
        )

        assert result is None
        assert workflow_state.variables["claimed_task_id"] is None
        assert workflow_state.variables["task_claimed"] is False

    async def test_task_closed_clears_state(self, workflow_state, mock_task_manager):
        """When task is closed, clear workflow state and allow."""
        workflow_state.variables["claimed_task_id"] = "gt-abc123"
        workflow_state.variables["task_claimed"] = True

        mock_task = MagicMock()
        mock_task.status = "closed"
        mock_task_manager.get_task.return_value = mock_task

        result = await require_task_review_or_close_before_stop(
            workflow_state=workflow_state,
            task_manager=mock_task_manager,
        )

        assert result is None
        assert workflow_state.variables["claimed_task_id"] is None
        assert workflow_state.variables["task_claimed"] is False

    async def test_task_in_review_clears_state(self, workflow_state, mock_task_manager):
        """When task is in review, clear workflow state and allow."""
        workflow_state.variables["claimed_task_id"] = "gt-abc123"
        workflow_state.variables["task_claimed"] = True

        mock_task = MagicMock()
        mock_task.status = "review"
        mock_task_manager.get_task.return_value = mock_task

        result = await require_task_review_or_close_before_stop(
            workflow_state=workflow_state,
            task_manager=mock_task_manager,
        )

        assert result is None
        assert workflow_state.variables["claimed_task_id"] is None
        assert workflow_state.variables["task_claimed"] is False

    async def test_task_in_progress_blocks(self, workflow_state, mock_task_manager):
        """When task is in_progress, block stop."""
        task_id = "01234567-89ab-cdef-0123-456789abcdef"
        workflow_state.variables["claimed_task_id"] = task_id

        mock_task = MagicMock()
        mock_task.id = task_id
        mock_task.seq_num = 42
        mock_task.status = "in_progress"
        mock_task_manager.get_task.return_value = mock_task

        result = await require_task_review_or_close_before_stop(
            workflow_state=workflow_state,
            task_manager=mock_task_manager,
        )

        assert result is not None
        assert result["decision"] == "block"
        assert "#42" in result["reason"]
        assert "still in_progress" in result["reason"]
        assert "close_task()" in result["reason"]
        assert result["task_id"] == task_id
        assert result["task_status"] == "in_progress"

    async def test_exception_allows(self, workflow_state, mock_task_manager):
        """When exception occurs, allow stop to avoid blocking."""
        workflow_state.variables["claimed_task_id"] = "gt-abc123"

        mock_task_manager.get_task.side_effect = Exception("Database error")

        result = await require_task_review_or_close_before_stop(
            workflow_state=workflow_state,
            task_manager=mock_task_manager,
        )

        assert result is None

    async def test_accepts_extra_kwargs(self, workflow_state, mock_task_manager):
        """Function accepts extra kwargs for compatibility."""
        workflow_state.variables["claimed_task_id"] = "gt-abc123"

        mock_task = MagicMock()
        mock_task.status = "closed"
        mock_task_manager.get_task.return_value = mock_task

        # Should not raise even with extra kwargs
        result = await require_task_review_or_close_before_stop(
            workflow_state=workflow_state,
            task_manager=mock_task_manager,
            project_path="/some/path",  # Extra kwarg for compatibility
            extra_arg="ignored",
        )

        assert result is None

    # -------------------------------------------------------------------------
    # Tests for session_task (scoping variable, NOT ownership)
    # session_task should never block stop — only claimed_task_id does.
    # -------------------------------------------------------------------------

    async def test_session_task_string_does_not_block(self, workflow_state, mock_task_manager):
        """session_task is a scoping variable, not ownership — should not block stop."""
        task_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        workflow_state.variables["session_task"] = task_id
        # No claimed_task_id set

        result = await require_task_review_or_close_before_stop(
            workflow_state=workflow_state,
            task_manager=mock_task_manager,
        )

        # session_task alone should never block
        assert result is None

    async def test_session_task_list_does_not_block(self, workflow_state, mock_task_manager):
        """session_task list should not block stop — only claimed_task_id does."""
        task1_id = "11111111-1111-1111-1111-111111111111"
        task2_id = "22222222-2222-2222-2222-222222222222"
        workflow_state.variables["session_task"] = [task1_id, task2_id]
        # No claimed_task_id set

        result = await require_task_review_or_close_before_stop(
            workflow_state=workflow_state,
            task_manager=mock_task_manager,
        )

        # session_task alone should never block
        assert result is None

    async def test_session_task_allows_when_all_closed(self, workflow_state, mock_task_manager):
        """When session_task is set but all tasks are closed, allow stop."""
        workflow_state.variables["session_task"] = "gt-closed-task"
        # No claimed_task_id set

        mock_task = MagicMock()
        mock_task.id = "gt-closed-task"
        mock_task.status = "closed"
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.list_tasks.return_value = []

        result = await require_task_review_or_close_before_stop(
            workflow_state=workflow_state,
            task_manager=mock_task_manager,
        )

        # Should allow since no in_progress task found
        assert result is None

    async def test_session_task_subtask_in_progress_does_not_block(
        self, workflow_state, mock_task_manager
    ):
        """session_task with in_progress subtask should not block — only claimed_task_id does."""
        parent_id = "33333333-3333-3333-3333-333333333333"
        workflow_state.variables["session_task"] = parent_id
        # No claimed_task_id set

        result = await require_task_review_or_close_before_stop(
            workflow_state=workflow_state,
            task_manager=mock_task_manager,
        )

        # session_task alone should never block
        assert result is None

    async def test_session_task_wildcard_allows(self, workflow_state, mock_task_manager):
        """When session_task='*', don't check tasks (wildcard means all)."""
        workflow_state.variables["session_task"] = "*"
        # No claimed_task_id set

        result = await require_task_review_or_close_before_stop(
            workflow_state=workflow_state,
            task_manager=mock_task_manager,
        )

        # Wildcard means we don't check specific tasks
        assert result is None
        mock_task_manager.get_task.assert_not_called()

    async def test_session_task_no_task_manager_allows(self, workflow_state):
        """When session_task is set but no task_manager, allow stop."""
        workflow_state.variables["session_task"] = "gt-task"
        # No claimed_task_id set

        result = await require_task_review_or_close_before_stop(
            workflow_state=workflow_state,
            task_manager=None,
        )

        assert result is None

    async def test_claimed_task_takes_precedence_over_session_task(
        self, workflow_state, mock_task_manager
    ):
        """claimed_task_id is checked first, session_task is fallback."""
        claimed_id = "55555555-5555-5555-5555-555555555555"
        session_id = "66666666-6666-6666-6666-666666666666"
        workflow_state.variables["claimed_task_id"] = claimed_id
        workflow_state.variables["session_task"] = session_id

        mock_claimed_task = MagicMock()
        mock_claimed_task.id = claimed_id
        mock_claimed_task.seq_num = 99
        mock_claimed_task.status = "in_progress"

        # Only return the claimed task
        mock_task_manager.get_task.return_value = mock_claimed_task

        result = await require_task_review_or_close_before_stop(
            workflow_state=workflow_state,
            task_manager=mock_task_manager,
        )

        assert result is not None
        assert result["decision"] == "block"
        # Should block on claimed_task, not session_task
        assert "#99" in result["reason"]


# =============================================================================
# Tests for require_task_complete
# =============================================================================


class TestRequireTaskComplete:
    """Tests for require_task_complete action."""

    async def test_no_task_ids_allows(self):
        """When no task_ids specified, allow stop."""
        result = await require_task_complete(
            task_manager=MagicMock(),
            session_id="test-session",
            task_ids=None,
        )
        assert result is None

    async def test_empty_task_ids_allows(self):
        """When empty task_ids list, allow stop."""
        result = await require_task_complete(
            task_manager=MagicMock(),
            session_id="test-session",
            task_ids=[],
        )
        assert result is None

    async def test_no_task_manager_allows(self):
        """When no task_manager available, allow stop."""
        result = await require_task_complete(
            task_manager=None,
            session_id="test-session",
            task_ids=["gt-abc123"],
        )
        assert result is None

    async def test_max_block_count_allows(self, workflow_state):
        """After 5 blocks, allow to prevent infinite loop."""
        workflow_state.variables["_task_block_count"] = 5

        result = await require_task_complete(
            task_manager=MagicMock(),
            session_id="test-session",
            task_ids=["gt-abc123"],
            workflow_state=workflow_state,
        )

        assert result is None

    async def test_task_not_found_skipped(self, mock_task_manager):
        """When task not found, skip it and continue."""
        mock_task_manager.get_task.return_value = None

        result = await require_task_complete(
            task_manager=mock_task_manager,
            session_id="test-session",
            task_ids=["gt-nonexistent"],
        )

        assert result is None

    async def test_closed_task_skipped(self, mock_task_manager):
        """When task is closed, skip it."""
        mock_task = MagicMock()
        mock_task.status = "closed"
        mock_task_manager.get_task.return_value = mock_task

        result = await require_task_complete(
            task_manager=mock_task_manager,
            session_id="test-session",
            task_ids=["gt-closed"],
        )

        assert result is None

    async def test_all_tasks_closed_allows(self, mock_task_manager):
        """When all specified tasks are closed, allow stop."""
        mock_task1 = MagicMock()
        mock_task1.status = "closed"
        mock_task2 = MagicMock()
        mock_task2.status = "closed"

        mock_task_manager.get_task.side_effect = [mock_task1, mock_task2]

        result = await require_task_complete(
            task_manager=mock_task_manager,
            session_id="test-session",
            task_ids=["gt-task1", "gt-task2"],
        )

        assert result is None

    async def test_leaf_task_not_closed_blocks(self, mock_task_manager):
        """Task with no subtasks but not closed should block with close reminder."""
        mock_task = MagicMock()
        mock_task.id = "gt-leaf"
        mock_task.title = "Leaf Task"
        mock_task.status = "in_progress"

        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.list_tasks.return_value = []  # No subtasks

        result = await require_task_complete(
            task_manager=mock_task_manager,
            session_id="test-session",
            task_ids=["gt-leaf"],
        )

        assert result is not None
        assert result["decision"] == "block"
        assert "ready to close" in result["reason"]
        assert "close_task" in result["reason"]
        assert "gt-leaf" in result["reason"]

    async def test_incomplete_subtasks_no_claimed_task_blocks(
        self, mock_task_manager, workflow_state
    ):
        """Incomplete subtasks with no claimed task should suggest next task."""
        mock_parent = MagicMock()
        mock_parent.id = "gt-parent"
        mock_parent.title = "Parent Feature"
        mock_parent.status = "open"

        mock_subtask1 = MagicMock()
        mock_subtask1.id = "gt-sub1"
        mock_subtask1.status = "open"

        mock_subtask2 = MagicMock()
        mock_subtask2.id = "gt-sub2"
        mock_subtask2.status = "open"

        mock_task_manager.get_task.return_value = mock_parent
        mock_task_manager.list_tasks.return_value = [mock_subtask1, mock_subtask2]

        result = await require_task_complete(
            task_manager=mock_task_manager,
            session_id="test-session",
            task_ids=["gt-parent"],
            workflow_state=workflow_state,
        )

        assert result is not None
        assert result["decision"] == "block"
        assert "2 incomplete subtask(s)" in result["reason"]
        assert "suggest_next_task()" in result["reason"]

    async def test_incomplete_subtasks_with_claimed_task_under_parent(
        self, mock_task_manager, workflow_state
    ):
        """Claimed task under parent should remind to finish it."""
        workflow_state.variables["task_claimed"] = True
        workflow_state.variables["claimed_task_id"] = "gt-sub1"

        mock_parent = MagicMock()
        mock_parent.id = "gt-parent"
        mock_parent.title = "Parent Feature"
        mock_parent.status = "open"

        mock_subtask1 = MagicMock()
        mock_subtask1.id = "gt-sub1"
        mock_subtask1.status = "in_progress"

        mock_subtask2 = MagicMock()
        mock_subtask2.id = "gt-sub2"
        mock_subtask2.status = "open"

        # Return correct task based on task_id (needed for claimed_task_id resolution)
        def get_task_side_effect(task_id):
            if task_id == "gt-sub1":
                return mock_subtask1
            elif task_id == "gt-parent":
                return mock_parent
            return None

        mock_task_manager.get_task.side_effect = get_task_side_effect
        mock_task_manager.list_tasks.return_value = [mock_subtask1, mock_subtask2]

        result = await require_task_complete(
            task_manager=mock_task_manager,
            session_id="test-session",
            task_ids=["gt-parent"],
            workflow_state=workflow_state,
        )

        assert result is not None
        assert result["decision"] == "block"
        assert "current task is not yet complete" in result["reason"]
        assert 'close_task(task_id="gt-sub1")' in result["reason"]

    async def test_incomplete_subtasks_with_claimed_task_not_under_parent(
        self, mock_task_manager, workflow_state
    ):
        """Claimed task not under parent should redirect to parent work."""
        workflow_state.variables["task_claimed"] = True
        workflow_state.variables["claimed_task_id"] = "gt-other"  # Different task

        mock_parent = MagicMock()
        mock_parent.id = "gt-parent"
        mock_parent.title = "Parent Feature"
        mock_parent.status = "open"

        mock_subtask1 = MagicMock()
        mock_subtask1.id = "gt-sub1"
        mock_subtask1.status = "open"

        mock_task_manager.get_task.return_value = mock_parent
        mock_task_manager.list_tasks.return_value = [mock_subtask1]

        result = await require_task_complete(
            task_manager=mock_task_manager,
            session_id="test-session",
            task_ids=["gt-parent"],
            workflow_state=workflow_state,
        )

        assert result is not None
        assert result["decision"] == "block"
        assert "1 incomplete subtask(s)" in result["reason"]
        assert "suggest_next_task()" in result["reason"]

    async def test_multiple_tasks_shows_count(self, mock_task_manager):
        """Multiple incomplete tasks shows remaining count."""
        mock_task1 = MagicMock()
        mock_task1.id = "gt-task1"
        mock_task1.title = "Task 1"
        mock_task1.status = "open"

        mock_task2 = MagicMock()
        mock_task2.id = "gt-task2"
        mock_task2.title = "Task 2"
        mock_task2.status = "open"

        mock_task_manager.get_task.side_effect = [mock_task1, mock_task2]
        mock_task_manager.list_tasks.return_value = []  # No subtasks

        result = await require_task_complete(
            task_manager=mock_task_manager,
            session_id="test-session",
            task_ids=["gt-task1", "gt-task2"],
        )

        assert result is not None
        assert result["decision"] == "block"
        assert "2 tasks remaining in total" in result["reason"]

    async def test_block_count_increments(self, mock_task_manager, workflow_state):
        """Block count increments on each block."""
        mock_task = MagicMock()
        mock_task.id = "gt-task"
        mock_task.title = "Test Task"
        mock_task.status = "open"

        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.list_tasks.return_value = []

        # First block
        await require_task_complete(
            task_manager=mock_task_manager,
            session_id="test-session",
            task_ids=["gt-task"],
            workflow_state=workflow_state,
        )

        assert workflow_state.variables["_task_block_count"] == 1

        # Second block
        await require_task_complete(
            task_manager=mock_task_manager,
            session_id="test-session",
            task_ids=["gt-task"],
            workflow_state=workflow_state,
        )

        assert workflow_state.variables["_task_block_count"] == 2

    async def test_error_handling_allows(self, mock_task_manager):
        """On exception, allow stop to avoid blocking legitimate work."""

        mock_task_manager.get_task.side_effect = Exception("Database error")

        result = await require_task_complete(
            task_manager=mock_task_manager,
            session_id="test-session",
            task_ids=["gt-abc123"],
        )

        assert result is None

    async def test_all_subtasks_closed_parent_not_closed_allows(self, mock_task_manager):
        """All subtasks closed allows stop (parent completion tracked elsewhere).

        The require_task_complete function only blocks when there are incomplete
        subtasks OR no subtasks. If all subtasks are complete, the parent is
        considered complete for enforcement purposes.
        """
        mock_parent = MagicMock()
        mock_parent.id = "gt-parent"
        mock_parent.title = "Parent Feature"
        mock_parent.status = "in_progress"  # Not closed, but subtasks are

        mock_subtask1 = MagicMock()
        mock_subtask1.id = "gt-sub1"
        mock_subtask1.status = "closed"

        mock_subtask2 = MagicMock()
        mock_subtask2.id = "gt-sub2"
        mock_subtask2.status = "closed"

        mock_task_manager.get_task.return_value = mock_parent
        mock_task_manager.list_tasks.return_value = [mock_subtask1, mock_subtask2]

        result = await require_task_complete(
            task_manager=mock_task_manager,
            session_id="test-session",
            task_ids=["gt-parent"],
        )

        # All subtasks closed = parent considered complete for enforcement
        assert result is None

    async def test_mixed_complete_incomplete_tasks(self, mock_task_manager):
        """Mix of complete and incomplete tasks blocks on first incomplete."""
        mock_task1 = MagicMock()
        mock_task1.id = "gt-task1"
        mock_task1.status = "closed"

        mock_task2 = MagicMock()
        mock_task2.id = "gt-task2"
        mock_task2.title = "Incomplete Task"
        mock_task2.status = "open"

        mock_task_manager.get_task.side_effect = [mock_task1, mock_task2]
        mock_task_manager.list_tasks.return_value = []

        result = await require_task_complete(
            task_manager=mock_task_manager,
            session_id="test-session",
            task_ids=["gt-task1", "gt-task2"],
        )

        assert result is not None
        assert result["decision"] == "block"
        assert "Incomplete Task" in result["reason"]

    async def test_without_workflow_state_no_block_count(self, mock_task_manager):
        """Without workflow state, block count is not tracked."""
        mock_task = MagicMock()
        mock_task.id = "gt-task"
        mock_task.title = "Test Task"
        mock_task.status = "open"

        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.list_tasks.return_value = []

        # Multiple blocks without workflow state
        for _ in range(10):
            result = await require_task_complete(
                task_manager=mock_task_manager,
                session_id="test-session",
                task_ids=["gt-task"],
                workflow_state=None,
            )
            # Should still block (no max count check without state)
            assert result is not None


# =============================================================================
# Tests for validate_session_task_scope
# =============================================================================


class TestValidateSessionTaskScope:
    """Tests for validate_session_task_scope action."""

    @pytest.fixture
    def workflow_state_with_session_task(self):
        """Create a workflow state with session_task set."""
        return WorkflowState(
            session_id="test-session",
            workflow_name="test-workflow",
            step="test-step",
            step_entered_at=datetime.now(UTC),
            variables={"session_task": "epic-1"},
        )

    async def test_no_session_task_allows_all(self, mock_task_manager, workflow_state):
        """When no session_task is set, any task can be claimed."""
        event_data = {
            "tool_name": "update_task",
            "tool_input": {"arguments": {"task_id": "any-task", "status": "in_progress"}},
        }

        result = await validate_session_task_scope(
            task_manager=mock_task_manager,
            workflow_state=workflow_state,
            event_data=event_data,
        )

        assert result is None  # Allowed

    async def test_descendant_task_allowed(
        self, mock_task_manager, workflow_state_with_session_task
    ):
        """Task that is descendant of session_task is allowed."""
        event_data = {
            "tool_name": "update_task",
            "tool_input": {"arguments": {"task_id": "child-1", "status": "in_progress"}},
        }

        # Mock is_descendant_of to return True
        with patch("gobby.workflows.enforcement.task_policy.is_descendant_of") as mock_descendant:
            mock_descendant.return_value = True

            result = await validate_session_task_scope(
                task_manager=mock_task_manager,
                workflow_state=workflow_state_with_session_task,
                event_data=event_data,
            )

        assert result is None  # Allowed
        mock_descendant.assert_called_once_with(mock_task_manager, "child-1", "epic-1")

    async def test_non_descendant_task_blocked(
        self, mock_task_manager, workflow_state_with_session_task
    ):
        """Task outside session_task hierarchy is blocked."""
        event_data = {
            "tool_name": "update_task",
            "tool_input": {"arguments": {"task_id": "other-task", "status": "in_progress"}},
        }

        # Mock is_descendant_of to return False
        with patch("gobby.workflows.enforcement.task_policy.is_descendant_of") as mock_descendant:
            mock_descendant.return_value = False

            # Mock get_task for error message
            mock_session_task = MagicMock()
            mock_session_task.title = "My Epic"
            mock_task_manager.get_task.return_value = mock_session_task

            result = await validate_session_task_scope(
                task_manager=mock_task_manager,
                workflow_state=workflow_state_with_session_task,
                event_data=event_data,
            )

        assert result is not None
        assert result["decision"] == "block"
        assert "not within the session_task scope" in result["reason"]
        assert "epic-1" in result["reason"]
        assert "suggest_next_task" in result["reason"]

    async def test_non_update_task_tool_allowed(
        self, mock_task_manager, workflow_state_with_session_task
    ):
        """Non-update_task tool calls are not affected."""
        event_data = {
            "tool_name": "create_task",
            "tool_input": {"arguments": {"title": "New task"}},
        }

        result = await validate_session_task_scope(
            task_manager=mock_task_manager,
            workflow_state=workflow_state_with_session_task,
            event_data=event_data,
        )

        assert result is None  # Allowed - not an update_task call

    async def test_non_in_progress_status_allowed(
        self, mock_task_manager, workflow_state_with_session_task
    ):
        """Setting status to something other than in_progress is allowed."""
        event_data = {
            "tool_name": "update_task",
            "tool_input": {"arguments": {"task_id": "any-task", "status": "blocked"}},
        }

        result = await validate_session_task_scope(
            task_manager=mock_task_manager,
            workflow_state=workflow_state_with_session_task,
            event_data=event_data,
        )

        assert result is None  # Allowed - not claiming (in_progress)

    async def test_no_workflow_state_allows(self, mock_task_manager):
        """When no workflow state, scope check is skipped."""
        event_data = {
            "tool_name": "update_task",
            "tool_input": {"arguments": {"task_id": "any-task", "status": "in_progress"}},
        }

        result = await validate_session_task_scope(
            task_manager=mock_task_manager,
            workflow_state=None,
            event_data=event_data,
        )

        assert result is None  # Allowed - no workflow state to check

    async def test_no_task_manager_allows(self, workflow_state_with_session_task):
        """When no task manager, scope check is skipped."""
        event_data = {
            "tool_name": "update_task",
            "tool_input": {"arguments": {"task_id": "any-task", "status": "in_progress"}},
        }

        result = await validate_session_task_scope(
            task_manager=None,
            workflow_state=workflow_state_with_session_task,
            event_data=event_data,
        )

        assert result is None  # Allowed - no task manager to check

    async def test_wildcard_allows_all(self, mock_task_manager):
        """When session_task='*', all tasks are allowed."""
        workflow_state = WorkflowState(
            session_id="test-session",
            workflow_name="test-workflow",
            step="test-step",
            step_entered_at=datetime.now(UTC),
            variables={"session_task": "*"},
        )
        event_data = {
            "tool_name": "update_task",
            "tool_input": {"arguments": {"task_id": "any-task", "status": "in_progress"}},
        }

        result = await validate_session_task_scope(
            task_manager=mock_task_manager,
            workflow_state=workflow_state,
            event_data=event_data,
        )

        assert result is None  # Allowed - wildcard means all tasks

    async def test_array_allows_descendant_of_any(self, mock_task_manager):
        """When session_task is array, task must be descendant of ANY."""
        workflow_state = WorkflowState(
            session_id="test-session",
            workflow_name="test-workflow",
            step="test-step",
            step_entered_at=datetime.now(UTC),
            variables={"session_task": ["epic-1", "epic-2"]},
        )
        event_data = {
            "tool_name": "update_task",
            "tool_input": {"arguments": {"task_id": "child-of-epic-2", "status": "in_progress"}},
        }

        # Mock is_descendant_of: False for epic-1, True for epic-2
        with patch("gobby.workflows.enforcement.task_policy.is_descendant_of") as mock_descendant:
            mock_descendant.side_effect = [False, True]  # Not under epic-1, but under epic-2

            result = await validate_session_task_scope(
                task_manager=mock_task_manager,
                workflow_state=workflow_state,
                event_data=event_data,
            )

        assert result is None  # Allowed - descendant of epic-2
        assert mock_descendant.call_count == 2

    async def test_array_blocks_if_not_descendant_of_any(self, mock_task_manager):
        """When session_task is array, blocks if not descendant of any."""
        workflow_state = WorkflowState(
            session_id="test-session",
            workflow_name="test-workflow",
            step="test-step",
            step_entered_at=datetime.now(UTC),
            variables={"session_task": ["epic-1", "epic-2"]},
        )
        event_data = {
            "tool_name": "update_task",
            "tool_input": {"arguments": {"task_id": "unrelated-task", "status": "in_progress"}},
        }

        with patch("gobby.workflows.enforcement.task_policy.is_descendant_of") as mock_descendant:
            mock_descendant.return_value = False  # Not under any

            result = await validate_session_task_scope(
                task_manager=mock_task_manager,
                workflow_state=workflow_state,
                event_data=event_data,
            )

        assert result is not None
        assert result["decision"] == "block"
        assert "epic-1" in result["reason"]
        assert "epic-2" in result["reason"]

    async def test_empty_array_allows_all(self, mock_task_manager):
        """Empty session_task array means no scope restriction."""
        workflow_state = WorkflowState(
            session_id="test-session",
            workflow_name="test-workflow",
            step="test-step",
            step_entered_at=datetime.now(UTC),
            variables={"session_task": []},
        )
        event_data = {
            "tool_name": "update_task",
            "tool_input": {"arguments": {"task_id": "any-task", "status": "in_progress"}},
        }

        result = await validate_session_task_scope(
            task_manager=mock_task_manager,
            workflow_state=workflow_state,
            event_data=event_data,
        )

        assert result is None  # Allowed - empty list means no restriction

    async def test_no_event_data_allows(self, mock_task_manager, workflow_state_with_session_task):
        """When no event_data provided, allow."""
        result = await validate_session_task_scope(
            task_manager=mock_task_manager,
            workflow_state=workflow_state_with_session_task,
            event_data=None,
        )

        assert result is None

    async def test_no_status_in_arguments_allows(
        self, mock_task_manager, workflow_state_with_session_task
    ):
        """When no status in arguments, allow (not claiming)."""
        event_data = {
            "tool_name": "update_task",
            "tool_input": {"arguments": {"task_id": "any-task", "title": "New Title"}},
        }

        result = await validate_session_task_scope(
            task_manager=mock_task_manager,
            workflow_state=workflow_state_with_session_task,
            event_data=event_data,
        )

        assert result is None

    async def test_no_task_id_in_arguments_allows(
        self, mock_task_manager, workflow_state_with_session_task
    ):
        """When no task_id in arguments, allow."""
        event_data = {
            "tool_name": "update_task",
            "tool_input": {"arguments": {"status": "in_progress"}},  # Missing task_id
        }

        result = await validate_session_task_scope(
            task_manager=mock_task_manager,
            workflow_state=workflow_state_with_session_task,
            event_data=event_data,
        )

        assert result is None

    async def test_invalid_session_task_type_allows(self, mock_task_manager):
        """When session_task is invalid type (not str/list), allow with warning."""
        workflow_state = WorkflowState(
            session_id="test-session",
            workflow_name="test-workflow",
            step="test-step",
            step_entered_at=datetime.now(UTC),
            variables={"session_task": 12345},  # Invalid type
        )
        event_data = {
            "tool_name": "update_task",
            "tool_input": {"arguments": {"task_id": "any-task", "status": "in_progress"}},
        }

        result = await validate_session_task_scope(
            task_manager=mock_task_manager,
            workflow_state=workflow_state,
            event_data=event_data,
        )

        assert result is None

    async def test_empty_arguments_allows(
        self, mock_task_manager, workflow_state_with_session_task
    ):
        """When arguments is empty or None, allow."""
        event_data = {
            "tool_name": "update_task",
            "tool_input": {"arguments": None},
        }

        result = await validate_session_task_scope(
            task_manager=mock_task_manager,
            workflow_state=workflow_state_with_session_task,
            event_data=event_data,
        )

        assert result is None

    async def test_missing_tool_input_allows(
        self, mock_task_manager, workflow_state_with_session_task
    ):
        """When tool_input is missing, allow."""
        event_data = {
            "tool_name": "update_task",
        }

        result = await validate_session_task_scope(
            task_manager=mock_task_manager,
            workflow_state=workflow_state_with_session_task,
            event_data=event_data,
        )

        assert result is None

    async def test_blocked_message_includes_single_task_title(self, mock_task_manager):
        """When single session_task, error message includes task title."""
        workflow_state = WorkflowState(
            session_id="test-session",
            workflow_name="test-workflow",
            step="test-step",
            step_entered_at=datetime.now(UTC),
            variables={"session_task": "epic-main"},
        )
        event_data = {
            "tool_name": "update_task",
            "tool_input": {"arguments": {"task_id": "wrong-task", "status": "in_progress"}},
        }

        with patch("gobby.workflows.enforcement.task_policy.is_descendant_of") as mock_descendant:
            mock_descendant.return_value = False

            mock_session_task = MagicMock()
            mock_session_task.title = "Main Epic Feature"
            mock_task_manager.get_task.return_value = mock_session_task

            result = await validate_session_task_scope(
                task_manager=mock_task_manager,
                workflow_state=workflow_state,
                event_data=event_data,
            )

        assert result is not None
        assert "Main Epic Feature" in result["reason"]
        assert "epic-main" in result["reason"]
        assert 'suggest_next_task(parent_id="epic-main")' in result["reason"]

    async def test_blocked_message_for_array_session_task(self, mock_task_manager):
        """When multiple session_tasks, error message lists all IDs."""
        workflow_state = WorkflowState(
            session_id="test-session",
            workflow_name="test-workflow",
            step="test-step",
            step_entered_at=datetime.now(UTC),
            variables={"session_task": ["epic-1", "epic-2", "epic-3"]},
        )
        event_data = {
            "tool_name": "update_task",
            "tool_input": {"arguments": {"task_id": "wrong-task", "status": "in_progress"}},
        }

        with patch("gobby.workflows.enforcement.task_policy.is_descendant_of") as mock_descendant:
            mock_descendant.return_value = False

            result = await validate_session_task_scope(
                task_manager=mock_task_manager,
                workflow_state=workflow_state,
                event_data=event_data,
            )

        assert result is not None
        assert "epic-1, epic-2, epic-3" in result["reason"]
        assert "one of the scoped parent IDs" in result["reason"]

    async def test_session_task_not_found_still_shows_id(self, mock_task_manager):
        """When session_task doesn't exist, still show its ID in error."""
        workflow_state = WorkflowState(
            session_id="test-session",
            workflow_name="test-workflow",
            step="test-step",
            step_entered_at=datetime.now(UTC),
            variables={"session_task": "gt-deleted"},
        )
        event_data = {
            "tool_name": "update_task",
            "tool_input": {"arguments": {"task_id": "wrong-task", "status": "in_progress"}},
        }

        with patch("gobby.workflows.enforcement.task_policy.is_descendant_of") as mock_descendant:
            mock_descendant.return_value = False

            mock_task_manager.get_task.return_value = None  # Task not found

            result = await validate_session_task_scope(
                task_manager=mock_task_manager,
                workflow_state=workflow_state,
                event_data=event_data,
            )

        assert result is not None
        assert "gt-deleted" in result["reason"]


# =============================================================================
# Additional edge case tests for coverage
# =============================================================================


class TestRequireTaskCompleteEdgeCases:
    """Edge case tests for require_task_complete."""

    async def test_fallback_block_for_edge_case(self, mock_task_manager, workflow_state):
        """Test the fallback block case for incomplete subtasks.

        This covers line 288-289 which is a defensive fallback.
        The code path happens when has_claimed_task is False and incomplete exists.
        """
        # This is actually covered by test_incomplete_subtasks_no_claimed_task_blocks
        # The fallback at lines 288-289 is truly unreachable in current code
        # since all conditions are exhaustively handled above it.
        # Leaving this test as documentation.
        pass

    async def test_parent_with_some_closed_subtasks(self, mock_task_manager):
        """Parent with mix of closed and open subtasks blocks."""
        mock_parent = MagicMock()
        mock_parent.id = "gt-parent"
        mock_parent.title = "Parent Task"
        mock_parent.status = "open"

        mock_closed_subtask = MagicMock()
        mock_closed_subtask.id = "gt-sub1"
        mock_closed_subtask.status = "closed"

        mock_open_subtask = MagicMock()
        mock_open_subtask.id = "gt-sub2"
        mock_open_subtask.status = "open"

        mock_task_manager.get_task.return_value = mock_parent
        mock_task_manager.list_tasks.return_value = [mock_closed_subtask, mock_open_subtask]

        result = await require_task_complete(
            task_manager=mock_task_manager,
            session_id="test-session",
            task_ids=["gt-parent"],
        )

        assert result is not None
        assert result["decision"] == "block"
        assert "1 incomplete subtask(s)" in result["reason"]


class TestValidateSessionTaskScopeEdgeCases:
    """Edge case tests for validate_session_task_scope."""

    async def test_string_session_task_as_single_item_list(self, mock_task_manager):
        """String session_task gets normalized to single-item list internally."""
        workflow_state = WorkflowState(
            session_id="test-session",
            workflow_name="test-workflow",
            step="test-step",
            step_entered_at=datetime.now(UTC),
            variables={"session_task": "single-epic"},
        )
        event_data = {
            "tool_name": "update_task",
            "tool_input": {"arguments": {"task_id": "child-task", "status": "in_progress"}},
        }

        with patch("gobby.workflows.enforcement.task_policy.is_descendant_of") as mock_descendant:
            mock_descendant.return_value = True

            result = await validate_session_task_scope(
                task_manager=mock_task_manager,
                workflow_state=workflow_state,
                event_data=event_data,
            )

        assert result is None
        mock_descendant.assert_called_once_with(mock_task_manager, "child-task", "single-epic")


# =============================================================================
# Tests for configurable messages (messages: dict support)
# =============================================================================


class TestResolveMessage:
    """Tests for _resolve_message helper in commit_policy and task_policy."""

    def test_returns_default_when_messages_none(self):
        from gobby.workflows.enforcement.commit_policy import _resolve_message

        result = _resolve_message(None, "key", "default text")
        assert result == "default text"

    def test_returns_default_when_key_missing(self):
        from gobby.workflows.enforcement.commit_policy import _resolve_message

        result = _resolve_message({"other_key": "val"}, "key", "default text")
        assert result == "default text"

    def test_returns_custom_message_when_key_present(self):
        from gobby.workflows.enforcement.commit_policy import _resolve_message

        result = _resolve_message({"key": "custom text"}, "key", "default text")
        assert result == "custom text"

    def test_renders_jinja2_template(self):
        from gobby.workflows.enforcement.commit_policy import _resolve_message

        result = _resolve_message(
            {"key": "Task {{ task_ref }} is blocked."},
            "key",
            "default",
            {"task_ref": "#42"},
        )
        assert result == "Task #42 is blocked."

    def test_no_rendering_without_context(self):
        from gobby.workflows.enforcement.commit_policy import _resolve_message

        result = _resolve_message(
            {"key": "Has {{ braces }} but no context"},
            "key",
            "default",
        )
        assert result == "Has {{ braces }} but no context"

    def test_task_policy_resolve_message_works(self):
        """Verify the _resolve_message in task_policy works identically."""
        from gobby.workflows.enforcement.task_policy import _resolve_message

        result = _resolve_message(
            {"task_ready_to_close": "Close {{ task_title }} now."},
            "task_ready_to_close",
            "default",
            {"task_title": "My Task"},
        )
        assert result == "Close My Task now."


class TestCustomMessagesInPolicies:
    """Tests that custom messages from YAML flow through to block reasons."""

    @pytest.mark.asyncio
    async def test_review_or_close_uses_custom_message(self, workflow_state, mock_task_manager):
        """Custom task_still_in_progress message replaces default."""
        task_id = "01234567-89ab-cdef-0123-456789abcdef"
        workflow_state.variables["claimed_task_id"] = task_id

        mock_task = MagicMock()
        mock_task.id = task_id
        mock_task.seq_num = 42
        mock_task.status = "in_progress"
        mock_task_manager.get_task.return_value = mock_task

        custom_messages = {"task_still_in_progress": "Custom: {{ task_ref }} needs closing."}

        result = await require_task_review_or_close_before_stop(
            workflow_state=workflow_state,
            task_manager=mock_task_manager,
            messages=custom_messages,
        )

        assert result is not None
        assert result["decision"] == "block"
        assert result["reason"] == "Custom: #42 needs closing."

    @pytest.mark.asyncio
    async def test_review_or_close_uses_default_when_no_messages(
        self, workflow_state, mock_task_manager
    ):
        """Without messages param, default hardcoded message is used."""
        task_id = "01234567-89ab-cdef-0123-456789abcdef"
        workflow_state.variables["claimed_task_id"] = task_id

        mock_task = MagicMock()
        mock_task.id = task_id
        mock_task.seq_num = 42
        mock_task.status = "in_progress"
        mock_task_manager.get_task.return_value = mock_task

        result = await require_task_review_or_close_before_stop(
            workflow_state=workflow_state,
            task_manager=mock_task_manager,
        )

        assert result is not None
        assert "still in_progress" in result["reason"]
        assert "close_task()" in result["reason"]

    @pytest.mark.asyncio
    @patch("gobby.workflows.enforcement.commit_policy.get_dirty_files")
    async def test_commit_before_stop_uses_custom_message(
        self, mock_dirty, workflow_state, mock_task_manager
    ):
        """Custom uncommitted_changes message replaces default."""
        workflow_state.variables["claimed_task_id"] = "task-123"
        workflow_state.variables["baseline_dirty_files"] = []

        mock_task = MagicMock()
        mock_task.status = "in_progress"
        mock_task_manager.get_task.return_value = mock_task

        mock_dirty.return_value = {"src/new.py"}

        custom_messages = {
            "uncommitted_changes": "Custom: {{ claimed_task_id }} has {{ new_dirty_count }} dirty files."
        }

        result = await require_commit_before_stop(
            workflow_state=workflow_state,
            project_path="/some/path",
            task_manager=mock_task_manager,
            messages=custom_messages,
        )

        assert result is not None
        assert result["decision"] == "block"
        assert result["reason"] == "Custom: task-123 has 1 dirty files."

    @pytest.mark.asyncio
    async def test_require_task_complete_uses_custom_message(
        self, workflow_state, mock_task_manager
    ):
        """Custom task_ready_to_close message replaces default."""
        parent_task = MagicMock()
        parent_task.id = "parent-uuid"
        parent_task.title = "Feature X"
        parent_task.status = "open"

        mock_task_manager.get_task.return_value = parent_task
        mock_task_manager.list_tasks.return_value = []  # no subtasks

        custom_messages = {"task_ready_to_close": "Please close {{ task_title }}."}

        result = await require_task_complete(
            task_manager=mock_task_manager,
            session_id="test-session",
            task_ids=["parent-uuid"],
            workflow_state=workflow_state,
            messages=custom_messages,
        )

        assert result is not None
        assert result["reason"] == "Please close Feature X."


# =============================================================================
# Tests for _load_agent_prompt (agent event handler prompt extraction)
# =============================================================================


class TestLoadAgentPrompt:
    """Tests for _load_agent_prompt helper in _agent.py."""

    def test_returns_fallback_when_file_missing(self, tmp_path, monkeypatch):
        """When prompt file doesn't exist, return fallback."""
        from gobby.hooks.event_handlers._agent import _load_agent_prompt

        monkeypatch.setattr(
            "gobby.hooks.event_handlers._agent.get_bundled_prompts_path",
            lambda: tmp_path,
            raising=False,
        )
        # Patch at the import location inside the function
        import gobby.prompts.sync as sync_mod

        monkeypatch.setattr(sync_mod, "get_bundled_prompts_path", lambda: tmp_path)

        result = _load_agent_prompt("nonexistent", fallback="fallback text")
        assert result == "fallback text"

    def test_loads_and_strips_frontmatter(self, tmp_path, monkeypatch):
        """Loads file, strips YAML frontmatter, returns content."""
        from gobby.hooks.event_handlers._agent import _load_agent_prompt

        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        (agent_dir / "test-prompt.md").write_text("---\nname: test\n---\nHello world")

        import gobby.prompts.sync as sync_mod

        monkeypatch.setattr(sync_mod, "get_bundled_prompts_path", lambda: tmp_path)

        result = _load_agent_prompt("test-prompt", fallback="fallback")
        assert result == "Hello world"

    def test_renders_jinja2_template(self, tmp_path, monkeypatch):
        """Renders Jinja2 variables when context is provided."""
        from gobby.hooks.event_handlers._agent import _load_agent_prompt

        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        (agent_dir / "skill-hint.md").write_text(
            '---\nname: test\n---\nSkill: `get_skill(name="{{ skill_name }}")`'
        )

        import gobby.prompts.sync as sync_mod

        monkeypatch.setattr(sync_mod, "get_bundled_prompts_path", lambda: tmp_path)

        result = _load_agent_prompt(
            "skill-hint",
            {"skill_name": "commit"},
            "fallback",
        )
        assert result == 'Skill: `get_skill(name="commit")`'

    def test_renders_jinja2_conditionals(self, tmp_path, monkeypatch):
        """Renders Jinja2 if/for blocks for skill-not-found template."""
        from gobby.hooks.event_handlers._agent import _load_agent_prompt

        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        (agent_dir / "skill-not-found.md").write_text(
            "---\nname: test\n---\n"
            "Skill '{{ skill_name }}' not found.\n"
            "{% if close_matches %}\nDid you mean:\n"
            "{% for m in close_matches %}\n  - `/gobby:{{ m }}`\n{% endfor %}\n"
            "{% endif %}"
        )

        import gobby.prompts.sync as sync_mod

        monkeypatch.setattr(sync_mod, "get_bundled_prompts_path", lambda: tmp_path)

        result = _load_agent_prompt(
            "skill-not-found",
            {"skill_name": "comm", "close_matches": ["commit", "compact"]},
            "fallback",
        )
        assert "Skill 'comm' not found." in result
        assert "`/gobby:commit`" in result
        assert "`/gobby:compact`" in result
