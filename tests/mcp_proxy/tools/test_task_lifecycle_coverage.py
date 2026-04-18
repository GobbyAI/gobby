"""Tests for tasks/_lifecycle.py — targeting uncovered lines."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.mcp_proxy.tools.tasks._lifecycle import _is_uuid
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.sync.tasks import TaskSyncManager
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    *,
    id: str = "550e8400-e29b-41d4-a716-446655440000",
    project_id: str = "proj-1",
    title: str = "Test Task",
    status: str = "open",
    priority: int = 2,
    task_type: str = "task",
    assignee: str | None = None,
    labels: list[str] | None = None,
    validation_criteria: str | None = None,
    commits: list[str] | None = None,
    seq_num: int | None = 42,
    description: str | None = "Test desc",
) -> Task:
    return Task(
        id=id,
        project_id=project_id,
        title=title,
        status=status,
        priority=priority,
        task_type=task_type,
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        assignee=assignee,
        labels=labels or [],
        validation_criteria=validation_criteria,
        commits=commits,
        seq_num=seq_num,
        description=description,
    )


@pytest.fixture
def mock_task_manager() -> MagicMock:
    mgr = MagicMock(spec=LocalTaskManager)
    mgr.db = MagicMock()
    return mgr


@pytest.fixture
def mock_sync_manager() -> MagicMock:
    return MagicMock(spec=TaskSyncManager)


def _create_registry(task_manager: MagicMock, sync_manager: MagicMock) -> Any:
    """Create registry with patches for context managers."""
    with (
        patch("gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"),
        patch("gobby.mcp_proxy.tools.tasks._context.LocalSessionManager") as MockSM,
    ):
        mock_sm = MagicMock()
        mock_sm.resolve_session_reference.return_value = "resolved-session"
        MockSM.return_value = mock_sm
        return create_task_registry(task_manager, sync_manager)


# ---------------------------------------------------------------------------
# close_task tests
# ---------------------------------------------------------------------------


class TestCloseTask:
    """Tests for the close_task lifecycle tool."""

    @pytest.mark.asyncio
    async def test_close_task_get_returns_none(self, mock_task_manager, mock_sync_manager):
        """Returns error when get_task returns None after resolve."""
        mock_task_manager.get_task.return_value = None
        registry = _create_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call(
            "close_task",
            {"task_id": "550e8400-e29b-41d4-a716-446655440000", "changes_summary": "done"},
        )
        assert "error" in result
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_close_epic_all_children_closed_no_commit_needed(
        self, mock_task_manager, mock_sync_manager
    ):
        """Closing a parent task (epic) with all children closed succeeds without commits."""
        parent = _make_task(task_type="epic", commits=None)
        child = _make_task(
            id="child-0000-0000-0000-000000000001",
            title="Child Task",
            status="closed",
            seq_num=43,
        )
        mock_task_manager.get_task.return_value = parent
        # First list_tasks call (limit=1) returns a child -> is a parent
        # Second list_tasks call (limit=1000) returns all children (all closed)
        mock_task_manager.list_tasks.return_value = [child]
        mock_task_manager.close_task.return_value = parent

        registry = _create_registry(mock_task_manager, mock_sync_manager)

        with patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_close.validate_commit_requirements"
        ) as mock_vcr:
            result = await registry.call(
                "close_task",
                {"task_id": parent.id, "changes_summary": "All subtasks completed"},
            )
            # commit check should NOT have been called
            mock_vcr.assert_not_called()

        assert "error" not in result
        assert result.get("success", True) is not False
        mock_task_manager.close_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_epic_open_children_blocked(self, mock_task_manager, mock_sync_manager):
        """Closing a parent task with open children is blocked."""
        parent = _make_task(task_type="epic", commits=None)
        open_child = _make_task(
            id="child-0000-0000-0000-000000000002",
            title="Open Child",
            status="in_progress",
            seq_num=44,
        )
        mock_task_manager.get_task.return_value = parent
        mock_task_manager.list_tasks.return_value = [open_child]

        registry = _create_registry(mock_task_manager, mock_sync_manager)
        result = await registry.call(
            "close_task",
            {"task_id": parent.id, "changes_summary": "Trying to close"},
        )

        assert result["success"] is False
        assert result["error"] == "validation_failed"
        assert "open" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_close_epic_no_children_no_commit_succeeds(
        self, mock_task_manager, mock_sync_manager
    ):
        """Closing an epic with no children succeeds without commits or changes_summary."""
        epic = _make_task(task_type="epic", commits=None)
        mock_task_manager.get_task.return_value = epic
        mock_task_manager.list_tasks.return_value = []  # no children
        mock_task_manager.close_task.return_value = epic

        registry = _create_registry(mock_task_manager, mock_sync_manager)

        with patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_close.validate_commit_requirements"
        ) as mock_vcr:
            result = await registry.call(
                "close_task",
                {"task_id": epic.id},
            )
            # commit check should NOT have been called — epics skip leaf validation
            mock_vcr.assert_not_called()

        assert "error" not in result
        assert result.get("success", True) is not False
        mock_task_manager.close_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_commit_requirements_fail(self, mock_task_manager, mock_sync_manager):
        """Returns error when commit requirements fail."""
        task = _make_task()
        mock_task_manager.get_task.return_value = task
        mock_task_manager.list_tasks.return_value = []  # leaf task (no children)

        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._lifecycle_close.validate_commit_requirements"
            ) as mock_vcr,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"),
            patch("gobby.mcp_proxy.tools.tasks._context.LocalSessionManager") as MockSM,
        ):
            mock_sm = MagicMock()
            MockSM.return_value = mock_sm

            mock_vcr.return_value = MagicMock(
                can_close=False,
                error_type="missing_commits",
                message="no commits linked",
            )

            registry = create_task_registry(mock_task_manager, mock_sync_manager)
            result = await registry.call(
                "close_task",
                {"task_id": task.id, "changes_summary": "done"},
            )
        assert result["success"] is False
        assert result["error"] == "missing_commits"

    @pytest.mark.asyncio
    async def test_close_task_invalid_commit_sha_returns_error(
        self, mock_task_manager, mock_sync_manager
    ):
        """Returns error when commit_sha cannot be resolved (nonexistent or non-commit)."""
        task = _make_task()
        mock_task_manager.get_task.return_value = task
        mock_task_manager.link_commit.side_effect = ValueError(
            "Invalid or unresolved commit SHA: deadbeef"
        )

        registry = _create_registry(mock_task_manager, mock_sync_manager)
        result = await registry.call(
            "close_task",
            {"task_id": task.id, "changes_summary": "done", "commit_sha": "deadbeef"},
        )

        assert "error" in result
        assert "Invalid or unresolved" in result["error"]

    @pytest.mark.asyncio
    async def test_close_task_passes_cwd_to_link_commit(self, mock_task_manager, mock_sync_manager):
        """Verifies link_commit receives the project repo_path as cwd."""
        task = _make_task(commits=["abc1234"])
        mock_task_manager.get_task.return_value = task
        mock_task_manager.link_commit.return_value = task
        mock_task_manager.list_tasks.return_value = []
        mock_task_manager.close_task.return_value = task

        registry = _create_registry(mock_task_manager, mock_sync_manager)

        with patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_close.validate_commit_requirements"
        ) as mock_vcr:
            mock_vcr.return_value = MagicMock(can_close=True)
            await registry.call(
                "close_task",
                {"task_id": task.id, "changes_summary": "done", "commit_sha": "abc1234"},
            )

        # link_commit should have been called with cwd keyword arg
        call_kwargs = mock_task_manager.link_commit.call_args
        assert call_kwargs is not None
        assert "cwd" in call_kwargs.kwargs


# ---------------------------------------------------------------------------
# validate_commit_requirements stale SHA tests
# ---------------------------------------------------------------------------


class TestValidateCommitRequirementsStale:
    """Tests for stale SHA detection in validate_commit_requirements."""

    def test_stale_commits_detected(self) -> None:
        """Returns stale_commits error when linked SHAs don't exist in repo."""
        from gobby.mcp_proxy.tools.tasks._lifecycle_validation import (
            validate_commit_requirements,
        )

        task = _make_task(commits=["abc1234", "def5678"])

        with patch("gobby.utils.git.normalize_commit_sha") as mock_norm:
            # First SHA resolves, second doesn't
            mock_norm.side_effect = ["abc1234", None]
            result = validate_commit_requirements(task, reason="completed", repo_path="/repo")

        assert not result.can_close
        assert result.error_type == "stale_commits"
        assert result.extra is not None
        assert "def5678" in result.extra["stale_shas"]

    def test_all_commits_valid(self) -> None:
        """Passes when all linked SHAs exist in repo."""
        from gobby.mcp_proxy.tools.tasks._lifecycle_validation import (
            validate_commit_requirements,
        )

        task = _make_task(commits=["abc1234"])

        with patch("gobby.utils.git.normalize_commit_sha") as mock_norm:
            mock_norm.return_value = "abc1234"
            result = validate_commit_requirements(task, reason="completed", repo_path="/repo")

        assert result.can_close

    def test_skips_verification_without_repo_path(self) -> None:
        """Degrades gracefully when no repo_path is available."""
        from gobby.mcp_proxy.tools.tasks._lifecycle_validation import (
            validate_commit_requirements,
        )

        task = _make_task(commits=["abc1234"])
        result = validate_commit_requirements(task, reason="completed", repo_path=None)

        assert result.can_close


# ---------------------------------------------------------------------------
# reopen_task tests
# ---------------------------------------------------------------------------


class TestReopenTask:
    """Tests for reopen_task tool."""

    @pytest.mark.asyncio
    async def test_reopen_success(self, mock_task_manager, mock_sync_manager):
        """Reopen resolves task and calls reopen."""
        mock_task_manager.get_task.return_value = _make_task(status="in_progress")
        registry = _create_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call(
            "reopen_task",
            {"task_id": "550e8400-e29b-41d4-a716-446655440000"},
        )
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_reopen_clears_claimed_tasks_variable(self, mock_task_manager, mock_sync_manager):
        """Reopen removes task from claimed_tasks session variable for prior assignee."""
        task_id = "550e8400-e29b-41d4-a716-446655440000"
        session_id = "session-abc"
        mock_task_manager.get_task.return_value = _make_task(
            status="in_progress", assignee=session_id
        )

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.SessionTaskManager") as MockSTM,
            patch("gobby.mcp_proxy.tools.tasks._context.LocalSessionManager") as MockSM,
            patch("gobby.workflows.task_claim_state.remove_claimed_task") as mock_remove,
        ):
            mock_sm = MagicMock()
            mock_sm.resolve_session_reference.return_value = "resolved-session"
            MockSM.return_value = mock_sm

            mock_stm = MagicMock()
            MockSTM.return_value = mock_stm

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            # Mock session_var_manager on the context
            mock_svm = MagicMock()
            mock_svm.get_variables.return_value = {
                "task_claimed": True,
                "claimed_tasks": {task_id: "#42"},
            }
            mock_remove.return_value = {"task_claimed": False, "claimed_tasks": {}}

            # Patch session_var_manager on the registry context
            with patch(
                "gobby.mcp_proxy.tools.tasks._context.SessionVariableManager",
                return_value=mock_svm,
            ):
                registry = create_task_registry(mock_task_manager, mock_sync_manager)
                result = await registry.call("reopen_task", {"task_id": task_id})

            assert "error" not in result
            mock_remove.assert_called_once_with(mock_svm.get_variables.return_value, task_id)

    @pytest.mark.asyncio
    async def test_reopen_value_error(self, mock_task_manager, mock_sync_manager):
        """Returns error when reopen raises ValueError."""
        mock_task_manager.get_task.return_value = _make_task(status="in_progress")
        mock_task_manager.reopen_task.side_effect = ValueError("cannot reopen")
        registry = _create_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call(
            "reopen_task",
            {"task_id": "550e8400-e29b-41d4-a716-446655440000"},
        )
        assert "error" in result
        assert "cannot reopen" in result["error"]


# ---------------------------------------------------------------------------
# delete_task tests
# ---------------------------------------------------------------------------


class TestDeleteTask:
    """Tests for delete_task tool."""

    @pytest.mark.asyncio
    async def test_delete_success(self, mock_task_manager, mock_sync_manager):
        """Delete resolves task and deletes."""
        task = _make_task()
        mock_task_manager.get_task.return_value = task
        mock_task_manager.delete_task.return_value = True
        registry = _create_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call("delete_task", {"task_id": task.id})
        assert "error" not in result
        assert result["ref"] == "#42"

    @pytest.mark.asyncio
    async def test_delete_not_found(self, mock_task_manager, mock_sync_manager):
        """Returns error when task not found."""
        mock_task_manager.get_task.return_value = None
        registry = _create_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call(
            "delete_task", {"task_id": "550e8400-e29b-41d4-a716-446655440000"}
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_delete_has_dependents_error(self, mock_task_manager, mock_sync_manager):
        """Returns specific error when task has dependent task(s)."""
        task = _make_task()
        mock_task_manager.get_task.return_value = task
        from gobby.storage.tasks._models import TaskHasDependentsError

        mock_task_manager.delete_task.side_effect = TaskHasDependentsError(
            "Cannot delete: has dependent task(s)"
        )
        registry = _create_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call("delete_task", {"task_id": task.id, "cascade": False})
        assert result["error"] == "has_dependents"
        assert "suggestion" in result

    @pytest.mark.asyncio
    async def test_delete_has_children_error(self, mock_task_manager, mock_sync_manager):
        """Returns specific error when task has children."""
        task = _make_task()
        mock_task_manager.get_task.return_value = task
        from gobby.storage.tasks._models import TaskHasChildrenError

        mock_task_manager.delete_task.side_effect = TaskHasChildrenError(
            "Cannot delete: has children"
        )
        registry = _create_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call("delete_task", {"task_id": task.id, "cascade": False})
        assert result["error"] == "has_children"

    @pytest.mark.asyncio
    async def test_delete_returns_false(self, mock_task_manager, mock_sync_manager):
        """Returns error when delete returns False."""
        task = _make_task()
        mock_task_manager.get_task.return_value = task
        mock_task_manager.delete_task.return_value = False
        registry = _create_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call("delete_task", {"task_id": task.id})
        assert "error" in result
        assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# add_label / remove_label tests
# ---------------------------------------------------------------------------


class TestLabels:
    """Tests for add_label and remove_label tools."""

    @pytest.mark.asyncio
    async def test_add_label_success(self, mock_task_manager, mock_sync_manager):
        task = _make_task(labels=["existing"])
        mock_task_manager.add_label.return_value = task
        registry = _create_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call("add_label", {"task_id": task.id, "label": "new"})
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_add_label_not_found(self, mock_task_manager, mock_sync_manager):
        mock_task_manager.add_label.return_value = None
        registry = _create_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call(
            "add_label",
            {"task_id": "550e8400-e29b-41d4-a716-446655440000", "label": "x"},
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_remove_label_success(self, mock_task_manager, mock_sync_manager):
        task = _make_task(labels=[])
        mock_task_manager.remove_label.return_value = task
        registry = _create_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call("remove_label", {"task_id": task.id, "label": "old"})
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_remove_label_not_found(self, mock_task_manager, mock_sync_manager):
        mock_task_manager.remove_label.return_value = None
        registry = _create_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call(
            "remove_label",
            {"task_id": "550e8400-e29b-41d4-a716-446655440000", "label": "x"},
        )
        assert "error" in result


# ---------------------------------------------------------------------------
# escalate_task tests
# ---------------------------------------------------------------------------


class TestEscalateTask:
    """Tests for escalate_task tool."""

    @pytest.fixture(autouse=True)
    def _set_session_context(self):
        with session_context_for_test("my-session"):
            yield

    @pytest.mark.asyncio
    async def test_escalate_success(self, mock_task_manager, mock_sync_manager):
        task = _make_task(status="in_progress")
        mock_task_manager.get_task.return_value = task
        registry = _create_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call(
            "escalate_task",
            {"task_id": task.id, "reason": "blocked by external dep"},
        )
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_escalate_already_escalated(self, mock_task_manager, mock_sync_manager):
        task = _make_task(status="escalated")
        mock_task_manager.get_task.return_value = task
        registry = _create_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call(
            "escalate_task",
            {"task_id": task.id, "reason": "still blocked"},
        )
        assert "error" in result
        assert "escalated" in result["error"]

    @pytest.mark.asyncio
    async def test_escalate_closed_task(self, mock_task_manager, mock_sync_manager):
        task = _make_task(status="closed")
        mock_task_manager.get_task.return_value = task
        registry = _create_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call(
            "escalate_task",
            {"task_id": task.id, "reason": "oops"},
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_escalate_with_session_id(self, mock_task_manager, mock_sync_manager):
        task = _make_task(status="in_progress")
        mock_task_manager.get_task.return_value = task
        registry = _create_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call(
            "escalate_task",
            {
                "task_id": task.id,
                "reason": "blocked",
            },
        )
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_escalate_clears_claimed_tasks_variable(
        self, mock_task_manager, mock_sync_manager
    ):
        """Escalation removes the task from the prior owner's claimed_tasks."""
        task_id = "550e8400-e29b-41d4-a716-446655440000"
        session_id = "session-abc"
        task = _make_task(id=task_id, status="in_progress", assignee=session_id)
        mock_task_manager.get_task.return_value = task
        mock_task_manager.escalate_task.return_value = task

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as MockSVM,
            patch("gobby.workflows.task_claim_state.remove_claimed_task") as mock_remove,
        ):
            mock_svm = MagicMock()
            mock_svm.get_variables.return_value = {
                "task_claimed": True,
                "claimed_tasks": {task_id: "#42"},
            }
            MockSVM.return_value = mock_svm
            mock_remove.return_value = {"task_claimed": False, "claimed_tasks": {}}

            registry = _create_registry(mock_task_manager, mock_sync_manager)
            result = await registry.call(
                "escalate_task",
                {"task_id": task_id, "reason": "blocked"},
            )

        assert "error" not in result
        mock_remove.assert_called_once_with(mock_svm.get_variables.return_value, task_id)
        mock_svm.merge_variables.assert_called_once_with(
            session_id,
            {"task_claimed": False, "claimed_tasks": {}},
        )


# ---------------------------------------------------------------------------
# mark_task_review_approved tests
# ---------------------------------------------------------------------------


class TestMarkTaskReviewApproved:
    """Tests for mark_task_review_approved tool."""

    @pytest.fixture(autouse=True)
    def _set_session_context(self):
        with session_context_for_test("sess-1"):
            yield

    @pytest.mark.asyncio
    async def test_approve_needs_review(self, mock_task_manager, mock_sync_manager):
        task = _make_task(status="needs_review")
        mock_task_manager.get_task.return_value = task
        mock_task_manager.update_task.return_value = task
        registry = _create_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call(
            "mark_task_review_approved",
            {"task_id": task.id},
        )
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_approve_wrong_status(self, mock_task_manager, mock_sync_manager):
        task = _make_task(status="closed")
        mock_task_manager.get_task.return_value = task
        registry = _create_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call(
            "mark_task_review_approved",
            {"task_id": task.id},
        )
        assert "error" in result
        assert "closed" in result["error"]

    @pytest.mark.asyncio
    async def test_approve_with_notes(self, mock_task_manager, mock_sync_manager):
        task = _make_task(status="needs_review", description="Original desc")
        mock_task_manager.get_task.return_value = task
        mock_task_manager.mark_task_review_approved.return_value = task
        registry = _create_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call(
            "mark_task_review_approved",
            {
                "task_id": task.id,
                "approval_notes": "Looks good",
            },
        )
        assert "error" not in result
        mock_task_manager.mark_task_review_approved.assert_called_once_with(
            task.id,
            approval_notes="Looks good",
        )

    @pytest.mark.asyncio
    async def test_approve_update_fails(self, mock_task_manager, mock_sync_manager):
        task = _make_task(status="needs_review")
        mock_task_manager.get_task.return_value = task
        mock_task_manager.mark_task_review_approved.return_value = None
        registry = _create_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call(
            "mark_task_review_approved",
            {"task_id": task.id},
        )
        assert "error" in result
        assert "Failed to approve" in result["error"]

    @pytest.mark.asyncio
    async def test_approve_clears_claimed_tasks_variable(
        self, mock_task_manager, mock_sync_manager
    ):
        """Review approval removes the task from the prior owner's claimed_tasks."""
        task_id = "550e8400-e29b-41d4-a716-446655440000"
        session_id = "session-abc"
        task = _make_task(id=task_id, status="needs_review", assignee=session_id)
        mock_task_manager.get_task.return_value = task
        mock_task_manager.mark_task_review_approved.return_value = task

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as MockSVM,
            patch("gobby.workflows.task_claim_state.remove_claimed_task") as mock_remove,
        ):
            mock_svm = MagicMock()
            mock_svm.get_variables.return_value = {
                "task_claimed": True,
                "claimed_tasks": {task_id: "#42"},
            }
            MockSVM.return_value = mock_svm
            mock_remove.return_value = {"task_claimed": False, "claimed_tasks": {}}

            registry = _create_registry(mock_task_manager, mock_sync_manager)
            result = await registry.call(
                "mark_task_review_approved",
                {"task_id": task_id},
            )

        assert "error" not in result
        mock_remove.assert_called_once_with(mock_svm.get_variables.return_value, task_id)
        mock_svm.merge_variables.assert_called_once_with(
            session_id,
            {"task_claimed": False, "claimed_tasks": {}},
        )


# ---------------------------------------------------------------------------
# mark_task_needs_review tests
# ---------------------------------------------------------------------------


class TestMarkTaskNeedsReview:
    """Tests for mark_task_needs_review tool."""

    @pytest.fixture(autouse=True)
    def _set_session_context(self):
        with session_context_for_test("sess-1"):
            yield

    @pytest.mark.asyncio
    async def test_mark_needs_review_success(self, mock_task_manager, mock_sync_manager):
        task = _make_task(status="in_progress")
        mock_task_manager.get_task.return_value = task
        mock_task_manager.mark_task_needs_review.return_value = task
        registry = _create_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call(
            "mark_task_needs_review",
            {"task_id": task.id},
        )
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_mark_needs_review_with_notes(self, mock_task_manager, mock_sync_manager):
        task = _make_task(status="in_progress", description="Original")
        mock_task_manager.get_task.return_value = task
        mock_task_manager.mark_task_needs_review.return_value = task
        registry = _create_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call(
            "mark_task_needs_review",
            {
                "task_id": task.id,
                "review_notes": "Please check the output",
            },
        )
        assert "error" not in result
        mock_task_manager.mark_task_needs_review.assert_called_once_with(
            task.id,
            review_notes="Please check the output",
        )

    @pytest.mark.asyncio
    async def test_mark_needs_review_update_fails(self, mock_task_manager, mock_sync_manager):
        task = _make_task(status="in_progress")
        mock_task_manager.get_task.return_value = task
        mock_task_manager.mark_task_needs_review.return_value = None
        registry = _create_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call(
            "mark_task_needs_review",
            {"task_id": task.id},
        )
        assert "error" in result
        assert "Failed to mark" in result["error"]

    @pytest.mark.asyncio
    async def test_mark_needs_review_not_found(self, mock_task_manager, mock_sync_manager):
        mock_task_manager.get_task.return_value = None
        registry = _create_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call(
            "mark_task_needs_review",
            {"task_id": "550e8400-e29b-41d4-a716-446655440000"},
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_mark_needs_review_clears_claimed_tasks_variable(
        self, mock_task_manager, mock_sync_manager
    ):
        """Needs-review transition removes the task from the prior owner's claimed_tasks."""
        task_id = "550e8400-e29b-41d4-a716-446655440000"
        session_id = "session-abc"
        task = _make_task(id=task_id, status="in_progress", assignee=session_id)
        mock_task_manager.get_task.return_value = task
        mock_task_manager.mark_task_needs_review.return_value = task

        with (
            patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as MockSVM,
            patch("gobby.workflows.task_claim_state.remove_claimed_task") as mock_remove,
        ):
            mock_svm = MagicMock()
            mock_svm.get_variables.return_value = {
                "task_claimed": True,
                "claimed_tasks": {task_id: "#42"},
            }
            MockSVM.return_value = mock_svm
            mock_remove.return_value = {"task_claimed": False, "claimed_tasks": {}}

            registry = _create_registry(mock_task_manager, mock_sync_manager)
            result = await registry.call(
                "mark_task_needs_review",
                {"task_id": task_id},
            )

        assert "error" not in result
        mock_remove.assert_called_once_with(mock_svm.get_variables.return_value, task_id)
        mock_svm.merge_variables.assert_called_once_with(
            session_id,
            {"task_claimed": False, "claimed_tasks": {}},
        )


# ---------------------------------------------------------------------------
# _is_uuid tests
# ---------------------------------------------------------------------------


class TestIsUuid:
    """Tests for the _is_uuid helper."""

    def test_valid_uuid(self):
        assert _is_uuid("550e8400-e29b-41d4-a716-446655440000") is True

    def test_invalid_uuid(self):
        assert _is_uuid("#123") is False

    def test_none_value(self):
        assert _is_uuid(None) is False
