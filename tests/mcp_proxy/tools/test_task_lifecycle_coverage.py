"""Tests for tasks/_lifecycle.py — targeting uncovered lines."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.mcp_proxy.tools.tasks._lifecycle import _is_uuid
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.sync.tasks import TaskSyncManager

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
    requires_user_review: bool = False,
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
        requires_user_review=requires_user_review,
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
            "gobby.mcp_proxy.tools.tasks._lifecycle.validate_commit_requirements"
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
    async def test_close_epic_open_children_blocked(
        self, mock_task_manager, mock_sync_manager
    ):
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
    async def test_close_commit_requirements_fail(self, mock_task_manager, mock_sync_manager):
        """Returns error when commit requirements fail."""
        task = _make_task()
        mock_task_manager.get_task.return_value = task
        mock_task_manager.list_tasks.return_value = []  # leaf task (no children)

        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._lifecycle.validate_commit_requirements"
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
        mock_task_manager.delete_task.side_effect = ValueError(
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
        mock_task_manager.delete_task.side_effect = ValueError("Cannot delete: has children")
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
                "session_id": "my-session",
            },
        )
        assert "error" not in result


# ---------------------------------------------------------------------------
# mark_task_review_approved tests
# ---------------------------------------------------------------------------


class TestMarkTaskReviewApproved:
    """Tests for mark_task_review_approved tool."""

    @pytest.mark.asyncio
    async def test_approve_needs_review(self, mock_task_manager, mock_sync_manager):
        task = _make_task(status="needs_review")
        mock_task_manager.get_task.return_value = task
        mock_task_manager.update_task.return_value = task
        registry = _create_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call(
            "mark_task_review_approved",
            {"task_id": task.id, "session_id": "sess-1"},
        )
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_approve_wrong_status(self, mock_task_manager, mock_sync_manager):
        task = _make_task(status="closed")
        mock_task_manager.get_task.return_value = task
        registry = _create_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call(
            "mark_task_review_approved",
            {"task_id": task.id, "session_id": "sess-1"},
        )
        assert "error" in result
        assert "closed" in result["error"]

    @pytest.mark.asyncio
    async def test_approve_with_notes(self, mock_task_manager, mock_sync_manager):
        task = _make_task(status="needs_review", description="Original desc")
        mock_task_manager.get_task.return_value = task
        mock_task_manager.update_task.return_value = task
        registry = _create_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call(
            "mark_task_review_approved",
            {
                "task_id": task.id,
                "session_id": "sess-1",
                "approval_notes": "Looks good",
            },
        )
        assert "error" not in result
        # Verify description was updated with approval notes
        call_kwargs = mock_task_manager.update_task.call_args
        assert "Approval Notes" in call_kwargs.kwargs.get("description", "")

    @pytest.mark.asyncio
    async def test_approve_update_fails(self, mock_task_manager, mock_sync_manager):
        task = _make_task(status="needs_review")
        mock_task_manager.get_task.return_value = task
        mock_task_manager.update_task.return_value = None
        registry = _create_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call(
            "mark_task_review_approved",
            {"task_id": task.id, "session_id": "sess-1"},
        )
        assert "error" in result
        assert "Failed to approve" in result["error"]


# ---------------------------------------------------------------------------
# mark_task_needs_review tests
# ---------------------------------------------------------------------------


class TestMarkTaskNeedsReview:
    """Tests for mark_task_needs_review tool."""

    @pytest.mark.asyncio
    async def test_mark_needs_review_success(self, mock_task_manager, mock_sync_manager):
        task = _make_task(status="in_progress")
        mock_task_manager.get_task.return_value = task
        mock_task_manager.update_task.return_value = task
        registry = _create_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call(
            "mark_task_needs_review",
            {"task_id": task.id, "session_id": "sess-1"},
        )
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_mark_needs_review_with_notes(self, mock_task_manager, mock_sync_manager):
        task = _make_task(status="in_progress", description="Original")
        mock_task_manager.get_task.return_value = task
        mock_task_manager.update_task.return_value = task
        registry = _create_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call(
            "mark_task_needs_review",
            {
                "task_id": task.id,
                "session_id": "sess-1",
                "review_notes": "Please check the output",
            },
        )
        assert "error" not in result
        call_kwargs = mock_task_manager.update_task.call_args
        assert "Review Notes" in call_kwargs.kwargs.get("description", "")

    @pytest.mark.asyncio
    async def test_mark_needs_review_update_fails(self, mock_task_manager, mock_sync_manager):
        task = _make_task(status="in_progress")
        mock_task_manager.get_task.return_value = task
        mock_task_manager.update_task.return_value = None
        registry = _create_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call(
            "mark_task_needs_review",
            {"task_id": task.id, "session_id": "sess-1"},
        )
        assert "error" in result
        assert "Failed to mark" in result["error"]

    @pytest.mark.asyncio
    async def test_mark_needs_review_not_found(self, mock_task_manager, mock_sync_manager):
        mock_task_manager.get_task.return_value = None
        registry = _create_registry(mock_task_manager, mock_sync_manager)

        result = await registry.call(
            "mark_task_needs_review",
            {"task_id": "550e8400-e29b-41d4-a716-446655440000", "session_id": "s"},
        )
        assert "error" in result


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
