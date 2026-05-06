"""Focused coverage tests for task MCP tools."""

from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.plans.bootstrap_ledger import BootstrapLedgerMismatchError
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit


class TestCloseTaskTool:
    """Tests for close_task MCP tool."""

    @pytest.fixture(autouse=True)
    def _set_session_context(self):
        with session_context_for_test("test-session"):
            yield

    @pytest.mark.asyncio
    async def test_close_task_not_found(self, mock_task_manager, mock_sync_manager):
        """Test close_task returns error when task not found."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task_manager.get_task.return_value = None

        result = await registry.call(
            "close_task", {"task_id": "00000000-0000-0000-0000-000000000000"}
        )

        assert "error" in result
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_close_task_no_commits_error(self, mock_task_manager, mock_sync_manager):
        """Test close_task requires commits to be linked."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440000"
        mock_task.commits = None
        mock_task.project_id = "proj-1"
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.list_tasks.return_value = []  # leaf task (no children)

        with patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager:
            mock_proj_instance = MagicMock()
            mock_proj_instance.get.return_value = None
            MockProjManager.return_value = mock_proj_instance

            result = await registry.call(
                "close_task",
                {
                    "task_id": "550e8400-e29b-41d4-a716-446655440000",
                    "changes_summary": "test changes",
                },
            )

            assert "error" in result
            assert result["error"] == "no_commits_linked"

    @pytest.mark.asyncio
    async def test_close_task_with_skip_reason_skips_commit_check(
        self, mock_task_manager, mock_sync_manager
    ):
        """Test close_task with skip reason bypasses commit check."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440000"
        mock_task.commits = None
        mock_task.project_id = "proj-1"
        mock_task.requires_user_review = False  # Avoid review routing
        mock_task.to_brief.return_value = {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "status": "closed",
        }
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.close_task.return_value = mock_task
        mock_task_manager.list_tasks.return_value = []  # No children

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
            patch("gobby.utils.git.run_git_command") as mock_git,
            patch(
                "gobby.utils.git.normalize_commit_sha",
                side_effect=lambda sha, cwd=None: sha,
            ),
        ):
            mock_proj_instance = MagicMock()
            mock_proj_instance.get.return_value = None
            MockProjManager.return_value = mock_proj_instance
            mock_git.return_value = "abc123"

            result = await registry.call(
                "close_task",
                {
                    "task_id": "550e8400-e29b-41d4-a716-446655440000",
                    "reason": "duplicate",
                    "changes_summary": "test changes",
                },
            )

            assert "error" not in result
            mock_task_manager.close_task.assert_called_once()
            assert mock_task_manager.close_task.call_count == 1
            assert mock_task_manager.close_task.call_args is not None

    @pytest.mark.asyncio
    async def test_close_task_parent_with_open_children(self, mock_task_manager, mock_sync_manager):
        """Test close_task fails for parent with open children."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440020"
        mock_task.commits = ["abc123"]
        mock_task.project_id = "proj-1"
        mock_task.validation_criteria = None
        mock_task_manager.get_task.return_value = mock_task

        # Create open child tasks
        child1 = MagicMock()
        child1.id = "550e8400-e29b-41d4-a716-446655440021"
        child1.title = "Open Child 1"
        child1.status = "open"

        child2 = MagicMock()
        child2.id = "550e8400-e29b-41d4-a716-446655440022"
        child2.title = "Open Child 2"
        child2.status = "in_progress"

        mock_task_manager.list_tasks.return_value = [child1, child2]

        with patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager:
            mock_proj_instance = MagicMock()
            mock_proj_instance.get.return_value = None
            MockProjManager.return_value = mock_proj_instance

            result = await registry.call(
                "close_task", {"task_id": "550e8400-e29b-41d4-a716-446655440020"}
            )

            assert "error" in result
            assert result["error"] == "validation_failed"
            assert "open_children" in result

    @pytest.mark.asyncio
    async def test_close_task_success_with_commits(self, mock_task_manager, mock_sync_manager):
        """Test close_task succeeds when commits are linked."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440000"
        mock_task.commits = ["abc123"]
        mock_task.project_id = "proj-1"
        mock_task.validation_criteria = None
        mock_task.requires_user_review = False  # Explicitly set to avoid review routing
        mock_task.to_brief.return_value = {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "status": "closed",
        }
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.close_task.return_value = mock_task
        mock_task_manager.list_tasks.return_value = []  # No children

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
            patch("gobby.utils.git.run_git_command") as mock_git,
            patch(
                "gobby.utils.git.normalize_commit_sha",
                side_effect=lambda sha, cwd=None: sha,
            ),
        ):
            mock_proj_instance = MagicMock()
            mock_proj_instance.get.return_value = None
            MockProjManager.return_value = mock_proj_instance
            mock_git.return_value = "abc123"

            result = await registry.call(
                "close_task",
                {
                    "task_id": "550e8400-e29b-41d4-a716-446655440000",
                    "changes_summary": "test changes",
                },
            )

            assert result == {"success": True}
            assert mock_task_manager.close_task.call_count == 1

    @pytest.mark.asyncio
    async def test_close_task_surfaces_bootstrap_ledger_mismatch(
        self, mock_task_manager, mock_sync_manager
    ):
        """Test close_task returns structured bootstrap ledger mismatch errors."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440000"
        mock_task.commits = None
        mock_task.project_id = "proj-1"
        mock_task.task_type = "epic"
        mock_task.seq_num = 13175
        mock_task.requires_user_review = False
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.list_tasks.return_value = []
        mock_task_manager.close_task.side_effect = BootstrapLedgerMismatchError(
            ["A8:A8.7 expected leaves ['x'], manifest has []"],
            plan_id="task-13175-plan-coverage-contract",
        )

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
            patch("gobby.utils.git.run_git_command", return_value="abc123"),
        ):
            mock_proj_instance = MagicMock()
            mock_proj_instance.get.return_value = None
            MockProjManager.return_value = mock_proj_instance

            result = await registry.call(
                "close_task",
                {"task_id": "550e8400-e29b-41d4-a716-446655440000"},
            )

        assert result["success"] is False
        assert result["error"] == "bootstrap_ledger_mismatch"
        assert result["plan_id"] == "task-13175-plan-coverage-contract"
        assert result["mismatches"] == ["A8:A8.7 expected leaves ['x'], manifest has []"]

    @pytest.mark.asyncio
    async def test_close_task_with_commit_sha_links_first(
        self, mock_task_manager, mock_sync_manager
    ):
        """Test close_task with commit_sha links the commit first."""
        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440000"
        mock_task.commits = ["abc123"]
        mock_task.project_id = "proj-1"
        mock_task.validation_criteria = None
        mock_task.to_brief.return_value = {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "status": "closed",
        }
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.link_commit.return_value = mock_task
        mock_task_manager.close_task.return_value = mock_task
        mock_task_manager.list_tasks.return_value = []

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
            patch("gobby.utils.git.run_git_command") as mock_git,
            patch(
                "gobby.utils.git.normalize_commit_sha",
                side_effect=lambda sha, cwd=None: sha,
            ),
        ):
            expected_repo_path = "/fake/repo/path"
            mock_proj_instance = MagicMock()
            mock_project = MagicMock()
            mock_project.repo_path = expected_repo_path
            mock_proj_instance.get.return_value = mock_project
            MockProjManager.return_value = mock_proj_instance
            mock_git.return_value = "abc123"

            # Build the registry inside the patch so RegistryContext picks up
            # the mocked LocalProjectManager.
            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            await registry.call(
                "close_task",
                {
                    "task_id": "550e8400-e29b-41d4-a716-446655440000",
                    "commit_sha": "new-commit",
                    "changes_summary": "Closed with the explicitly linked commit.",
                },
            )

            # link_commit is called with cwd kwarg resolved from the project repo
            call_args = mock_task_manager.link_commit.call_args
            assert call_args[0] == ("550e8400-e29b-41d4-a716-446655440000", "new-commit")
            assert call_args.kwargs["cwd"] == expected_repo_path

            close_call = mock_task_manager.close_task.call_args
            assert close_call[0] == ("550e8400-e29b-41d4-a716-446655440000",)
            assert close_call.kwargs["closed_commit_sha"] == "new-commit"

    @pytest.mark.asyncio
    async def test_close_task_with_skip_validation(self, mock_task_manager, mock_sync_manager):
        """Test close_task with skip_validation bypasses LLM validation."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440000"
        mock_task.commits = ["abc123"]
        mock_task.project_id = "proj-1"
        mock_task.validation_criteria = "Must pass tests"
        mock_task.to_brief.return_value = {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "status": "closed",
        }
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.close_task.return_value = mock_task
        mock_task_manager.list_tasks.return_value = []

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
            patch("gobby.utils.git.run_git_command") as mock_git,
            patch(
                "gobby.utils.git.normalize_commit_sha",
                side_effect=lambda sha, cwd=None: sha,
            ),
        ):
            mock_proj_instance = MagicMock()
            mock_proj_instance.get.return_value = None
            MockProjManager.return_value = mock_proj_instance
            mock_git.return_value = "abc123"

            result = await registry.call(
                "close_task",
                {
                    "task_id": "550e8400-e29b-41d4-a716-446655440000",
                    "skip_validation": True,
                    "override_justification": "Manually verified",
                    "changes_summary": "test changes",
                },
            )

            # When override_justification is provided, task escalates for human review.
            # The validation_override_reason is now persisted in the same write as
            # the escalation (no separate update_task call).
            assert result.get("routed_to_escalation") is True
            mock_task_manager.escalate_task.assert_called_once_with(
                "550e8400-e29b-41d4-a716-446655440000",
                reason="Validation override requested: Manually verified",
                validation_override_reason="Manually verified",
            )
            assert not any(
                call.kwargs.get("validation_override_reason") is not None
                for call in mock_task_manager.update_task.call_args_list
            )

    @pytest.mark.asyncio
    async def test_close_task_fails_when_commit_sha_cannot_be_resolved(
        self, mock_task_manager, mock_sync_manager
    ):
        """Test close_task surfaces git SHA resolution failures on commit-backed closes."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440000"
        mock_task.commits = ["abc123"]
        mock_task.project_id = "proj-1"
        mock_task.validation_criteria = None
        mock_task.requires_user_review = False
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.list_tasks.return_value = []

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
            patch("gobby.utils.git.run_git_command", return_value=None),
            patch("gobby.utils.git.normalize_commit_sha", side_effect=lambda sha, cwd=None: sha),
        ):
            mock_proj_instance = MagicMock()
            mock_proj_instance.get.return_value = None
            MockProjManager.return_value = mock_proj_instance

            result = await registry.call(
                "close_task",
                {
                    "task_id": "550e8400-e29b-41d4-a716-446655440000",
                    "changes_summary": "test changes",
                },
            )

        assert result["success"] is False
        assert result["error"] == "Could not resolve commit SHA for close - git rev-parse failed"

    @pytest.mark.asyncio
    async def test_close_task_out_of_repo_blocked_when_session_had_edits(
        self, mock_task_manager, mock_sync_manager
    ):
        """Test out_of_repo reason still enforces commit check when session had edits."""
        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440000"
        mock_task.commits = None
        mock_task.project_id = "proj-1"
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.list_tasks.return_value = []  # leaf task (no children)

        # Mock session with had_edits=True
        mock_session = MagicMock()
        mock_session.had_edits = True

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
            patch("gobby.utils.git.run_git_command") as mock_git,
            patch(
                "gobby.utils.git.normalize_commit_sha",
                side_effect=lambda sha, cwd=None: sha,
            ),
            patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as MockSessionManager,
        ):
            mock_proj_instance = MagicMock()
            mock_proj_instance.get.return_value = None
            MockProjManager.return_value = mock_proj_instance
            mock_git.return_value = "abc123"

            mock_session_instance = MagicMock()
            mock_session_instance.get.return_value = mock_session
            MockSessionManager.return_value = mock_session_instance
            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            result = await registry.call(
                "close_task",
                {
                    "task_id": "550e8400-e29b-41d4-a716-446655440000",
                    "reason": "out_of_repo",
                    "changes_summary": "test changes",
                },
            )

            assert result.get("error") == "missing_commits_for_edits"
            mock_task_manager.close_task.assert_not_called()
            assert mock_task_manager.close_task.call_count == 0
            assert not mock_task_manager.close_task.called

    @pytest.mark.asyncio
    async def test_close_task_out_of_repo_succeeds_without_session_edits(
        self, mock_task_manager, mock_sync_manager
    ):
        """Test out_of_repo reason succeeds when the session had no in-repo edits."""
        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440000"
        mock_task.commits = None
        mock_task.project_id = "proj-1"
        mock_task.validation_criteria = None
        mock_task.requires_user_review = False
        mock_task.to_brief.return_value = {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "status": "closed",
        }
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.close_task.return_value = mock_task
        mock_task_manager.list_tasks.return_value = []

        mock_session = MagicMock()
        mock_session.had_edits = False

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
            patch("gobby.utils.git.run_git_command") as mock_git,
            patch(
                "gobby.utils.git.normalize_commit_sha",
                side_effect=lambda sha, cwd=None: sha,
            ),
            patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as MockSessionManager,
        ):
            mock_proj_instance = MagicMock()
            mock_proj_instance.get.return_value = None
            MockProjManager.return_value = mock_proj_instance
            mock_git.return_value = "abc123"

            mock_session_instance = MagicMock()
            mock_session_instance.get.return_value = mock_session
            MockSessionManager.return_value = mock_session_instance
            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            result = await registry.call(
                "close_task",
                {
                    "task_id": "550e8400-e29b-41d4-a716-446655440000",
                    "reason": "out_of_repo",
                    "changes_summary": "test changes",
                },
            )

            assert result == {"success": True}
            mock_task_manager.close_task.assert_called_once()
            assert mock_task_manager.close_task.call_count == 1
            assert mock_task_manager.close_task.call_args is not None

    @pytest.mark.asyncio
    async def test_close_task_clears_task_claimed_variables(
        self, mock_task_manager, mock_sync_manager
    ):
        """close_task must remove task from claimed_tasks session variables.

        Regression test for #9064: after successful close, the workflow state
        variables were not cleared due to scoping issues, causing stale
        'Task (unknown) is still in_progress' blocks after compaction.
        """
        task_uuid = "550e8400-e29b-41d4-a716-446655440000"

        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"
            ) as MockSessionTaskManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as MockSessionManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as MockSVManager,
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
            patch("gobby.utils.git.run_git_command") as mock_git,
            patch(
                "gobby.utils.git.normalize_commit_sha",
                side_effect=lambda sha, cwd=None: sha,
            ),
        ):
            mock_st_instance = MagicMock()
            MockSessionTaskManager.return_value = mock_st_instance

            mock_session_manager = MagicMock()
            mock_session_manager.resolve_session_reference.return_value = "test-session"
            mock_session_manager.get.return_value = None
            MockSessionManager.return_value = mock_session_manager

            # Session variables with only this task claimed
            mock_sv_manager = MagicMock()
            mock_sv_manager.get_variables.return_value = {
                "task_claimed": True,
                "claimed_tasks": {task_uuid: "#42"},
            }
            MockSVManager.return_value = mock_sv_manager

            mock_proj_instance = MagicMock()
            mock_proj_instance.get.return_value = None
            MockProjManager.return_value = mock_proj_instance
            mock_git.return_value = "abc123"

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            mock_task = MagicMock()
            mock_task.id = task_uuid
            mock_task.commits = ["abc123"]
            mock_task.project_id = "proj-1"
            mock_task.validation_criteria = None
            mock_task.requires_user_review = False
            mock_task.to_brief.return_value = {"id": task_uuid, "status": "closed"}
            mock_task_manager.get_task.return_value = mock_task
            mock_task_manager.close_task.return_value = mock_task
            mock_task_manager.list_tasks.return_value = []

            result = await registry.call(
                "close_task",
                {
                    "task_id": task_uuid,
                    "changes_summary": "test changes",
                },
            )

            assert "error" not in result
            # Variables must show task removed from claimed_tasks
            mock_sv_manager.merge_variables.assert_called_once_with(
                "test-session",
                {
                    "task_claimed": False,
                    "claimed_tasks": {},
                },
            )
            assert mock_sv_manager.merge_variables.call_count == 1
            assert mock_sv_manager.merge_variables.call_args is not None


# =============================================================================
# reopen_task Tool Tests
# =============================================================================


class TestReopenTaskTool:
    """Tests for reopen_task MCP tool."""

    @pytest.mark.asyncio
    async def test_reopen_task_success(self, mock_task_manager, mock_sync_manager):
        """Test reopen_task successfully reopens a closed task."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        reopened_task = MagicMock()
        mock_task_manager.reopen_task.return_value = reopened_task

        result = await registry.call(
            "reopen_task", {"task_id": "550e8400-e29b-41d4-a716-446655440000"}
        )

        mock_task_manager.reopen_task.assert_called_with(
            "550e8400-e29b-41d4-a716-446655440000", reason=None
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_reopen_task_with_reason(self, mock_task_manager, mock_sync_manager):
        """Test reopen_task with a reason."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        reopened_task = MagicMock()
        reopened_task.to_dict.return_value = {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "status": "open",
        }
        mock_task_manager.reopen_task.return_value = reopened_task

        await registry.call(
            "reopen_task",
            {"task_id": "550e8400-e29b-41d4-a716-446655440000", "reason": "Needs more work"},
        )

        mock_task_manager.reopen_task.assert_called_with(
            "550e8400-e29b-41d4-a716-446655440000", reason="Needs more work"
        )
        assert mock_task_manager.reopen_task.call_count >= 1
        assert mock_task_manager.reopen_task.call_args is not None

    @pytest.mark.asyncio
    async def test_reopen_task_error(self, mock_task_manager, mock_sync_manager):
        """Test reopen_task returns error on failure."""
        registry = create_task_registry(mock_task_manager, mock_sync_manager)

        mock_task_manager.reopen_task.side_effect = ValueError("Task not found")

        result = await registry.call(
            "reopen_task", {"task_id": "00000000-0000-0000-0000-000000000000"}
        )

        assert "error" in result

    @pytest.mark.asyncio
    async def test_reopen_task_leaves_worktree_status_unchanged(
        self, mock_task_manager, mock_sync_manager
    ):
        """Test reopen_task does not mutate associated worktrees."""
        with patch(
            "gobby.mcp_proxy.tools.tasks._context.LocalWorktreeManager"
        ) as MockWorktreeManager:
            mock_wt_instance = MagicMock()
            mock_worktree = MagicMock()
            mock_worktree.id = "wt-123"
            mock_worktree.status = "merged"
            mock_wt_instance.get_by_task.return_value = mock_worktree
            MockWorktreeManager.return_value = mock_wt_instance

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            reopened_task = MagicMock()
            reopened_task.to_dict.return_value = {
                "id": "550e8400-e29b-41d4-a716-446655440000",
                "status": "open",
            }
            mock_task_manager.reopen_task.return_value = reopened_task

            await registry.call("reopen_task", {"task_id": "550e8400-e29b-41d4-a716-446655440000"})

            mock_wt_instance.get_by_task.assert_not_called()
            assert mock_wt_instance.get_by_task.call_count == 0
            assert not mock_wt_instance.get_by_task.called
            mock_wt_instance.update.assert_not_called()
            assert mock_wt_instance.update.call_count == 0
            assert not mock_wt_instance.update.called


# =============================================================================
# delete_task Tool Tests
# =============================================================================


class TestSessionVariableMirroring:
    """Tests that task lifecycle operations mirror state to session_variables.

    The rule engine evaluates session_variables (higher precedence) over
    workflow_states. If task tools only write to workflow_states, the
    require-task-close rule never blocks the stop hook.
    """

    @pytest.fixture(autouse=True)
    def _set_session_context(self):
        with session_context_for_test("test-session"):
            yield

    @pytest.mark.asyncio
    async def test_claim_task_mirrors_to_session_variables(
        self, mock_task_manager, mock_sync_manager
    ):
        """claim_task must mirror task_claimed=True to session_variables."""
        task_uuid = "550e8400-e29b-41d4-a716-446655440099"

        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"
            ) as MockSessionTaskManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as MockSessionManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as MockSVManager,
        ):
            mock_st_instance = MagicMock()
            MockSessionTaskManager.return_value = mock_st_instance

            mock_session_manager = MagicMock()
            mock_session_manager.resolve_session_reference.return_value = "test-session"
            mock_session_manager.get.return_value = MagicMock(project_id="proj-1")
            MockSessionManager.return_value = mock_session_manager

            mock_sv_manager = MagicMock()
            MockSVManager.return_value = mock_sv_manager

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            mock_task = MagicMock()
            mock_task.id = task_uuid
            mock_task.seq_num = 200
            mock_task.project_id = "proj-1"
            mock_task.status = "open"
            mock_task.assignee = None
            mock_task_manager.get_task.return_value = mock_task
            mock_task_manager.update_task.return_value = mock_task

            result = await registry.call(
                "claim_task",
                {"task_id": task_uuid},
            )

            assert "error" not in result
            # session_variables must be written with task in claimed_tasks dict
            mock_sv_manager.merge_variables.assert_called_once()
            call_args = mock_sv_manager.merge_variables.call_args
            merged = call_args[0][1]
            assert merged["task_claimed"] is True
            assert task_uuid in merged["claimed_tasks"]
            assert merged["claimed_tasks"][task_uuid] == "#200"

    @pytest.mark.asyncio
    async def test_close_task_mirrors_clear_to_session_variables(
        self, mock_task_manager, mock_sync_manager
    ):
        """close_task must mirror task_claimed=False to session_variables."""
        task_uuid = "550e8400-e29b-41d4-a716-446655440099"

        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"
            ) as MockSessionTaskManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as MockSessionManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as MockSVManager,
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
            patch("gobby.utils.git.run_git_command") as mock_git,
            patch(
                "gobby.utils.git.normalize_commit_sha",
                side_effect=lambda sha, cwd=None: sha,
            ),
        ):
            mock_st_instance = MagicMock()
            MockSessionTaskManager.return_value = mock_st_instance

            mock_session_manager = MagicMock()
            mock_session_manager.resolve_session_reference.return_value = "test-session"
            mock_session_manager.get.return_value = None
            MockSessionManager.return_value = mock_session_manager

            mock_sv_manager = MagicMock()
            mock_sv_manager.get_variables.return_value = {
                "task_claimed": True,
                "claimed_tasks": {task_uuid: "#200"},
            }
            MockSVManager.return_value = mock_sv_manager

            mock_proj_instance = MagicMock()
            mock_proj_instance.get.return_value = None
            MockProjManager.return_value = mock_proj_instance
            mock_git.return_value = "abc123"

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            mock_task = MagicMock()
            mock_task.id = task_uuid
            mock_task.commits = ["abc123"]
            mock_task.project_id = "proj-1"
            mock_task.validation_criteria = None
            mock_task.requires_user_review = False
            mock_task_manager.get_task.return_value = mock_task
            mock_task_manager.close_task.return_value = mock_task
            mock_task_manager.list_tasks.return_value = []

            result = await registry.call(
                "close_task",
                {"task_id": task_uuid, "changes_summary": "done"},
            )

            assert "error" not in result
            # session_variables must show task removed
            mock_sv_manager.merge_variables.assert_called_once_with(
                "test-session",
                {
                    "task_claimed": False,
                    "claimed_tasks": {},
                },
            )
            assert mock_sv_manager.merge_variables.call_count == 1
            assert mock_sv_manager.merge_variables.call_args is not None

    @pytest.mark.asyncio
    async def test_create_task_with_claim_mirrors_to_session_variables(
        self, mock_task_manager, mock_sync_manager
    ):
        """create_task(claim=True) must mirror task_claimed=True to session_variables."""
        task_uuid = "550e8400-e29b-41d4-a716-446655440099"

        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"
            ) as MockSessionTaskManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as MockSessionManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as MockSVManager,
        ):
            mock_st_instance = MagicMock()
            MockSessionTaskManager.return_value = mock_st_instance

            mock_session_manager = MagicMock()
            mock_session_manager.resolve_session_reference.return_value = "test-session"
            mock_session_manager.get.return_value = MagicMock(project_id="proj-1")
            MockSessionManager.return_value = mock_session_manager

            mock_sv_manager = MagicMock()
            MockSVManager.return_value = mock_sv_manager

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            mock_task = MagicMock()
            mock_task.id = task_uuid
            mock_task.seq_num = 300
            mock_task.status = "in_progress"
            mock_task.assignee = "test-session"
            mock_task_manager.create_task_with_decomposition.return_value = {
                "task": {"id": task_uuid},
            }
            mock_task_manager.get_task.return_value = mock_task
            mock_task_manager.claim_task.return_value = mock_task

            with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
                mock_ctx.return_value = {"id": "proj-1"}

                result = await registry.call(
                    "create_task",
                    {
                        "title": "New Task",
                        "category": "research",
                        "claim": True,
                    },
                )

                assert result["id"] == task_uuid
                # session_variables must be written with task in claimed_tasks dict
                mock_sv_manager.merge_variables.assert_called_once()
                call_args = mock_sv_manager.merge_variables.call_args
                merged = call_args[0][1]
                assert merged["task_claimed"] is True
                assert task_uuid in merged["claimed_tasks"]
                assert merged["claimed_tasks"][task_uuid] == "#300"
