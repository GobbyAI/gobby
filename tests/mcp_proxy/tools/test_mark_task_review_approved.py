"""Tests for mark_task_review_approved MCP tool and review_approved status.

Tests status transitions, validation, and blocked status in update_task.
"""

from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from gobby.storage.tasks import Task
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_task_manager():
    """Create a mock task manager."""
    manager = MagicMock()
    manager.db = MagicMock()
    return manager


@pytest.fixture
def mock_sync_manager():
    """Create a mock sync manager."""
    return MagicMock()


@pytest.fixture
def sample_task_needs_review():
    """Create a task in needs_review status."""
    return Task(
        id="550e8400-e29b-41d4-a716-446655440000",
        project_id="proj-1",
        title="Test Task",
        status="needs_review",
        priority=2,
        task_type="task",
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        description="Test description",
        seq_num=42,
    )


@pytest.fixture
def sample_task_in_progress():
    """Create a task in in_progress status."""
    return Task(
        id="550e8400-e29b-41d4-a716-446655440000",
        project_id="proj-1",
        title="Test Task",
        status="in_progress",
        priority=2,
        task_type="task",
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        description="Original description",
        seq_num=42,
    )


@pytest.fixture
def sample_task_open():
    """Create a task in open status."""
    return Task(
        id="550e8400-e29b-41d4-a716-446655440000",
        project_id="proj-1",
        title="Test Task",
        status="open",
        priority=2,
        task_type="task",
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )


@pytest.fixture
def lifecycle_registry(mock_task_manager, mock_sync_manager):
    """Create a lifecycle registry with mark_task_review_approved tool."""
    from gobby.mcp_proxy.tools.tasks._context import RegistryContext
    from gobby.mcp_proxy.tools.tasks._lifecycle import create_lifecycle_registry

    ctx = RegistryContext(
        task_manager=mock_task_manager,
        sync_manager=mock_sync_manager,
    )
    return create_lifecycle_registry(ctx)


class TestMarkTaskReviewApproved:
    """Tests for mark_task_review_approved lifecycle tool."""

    @pytest.fixture(autouse=True)
    def _set_session_context(self):
        with session_context_for_test("session-abc"):
            yield

    def test_approve_needs_review_task(
        self, lifecycle_registry, mock_task_manager, sample_task_needs_review
    ) -> None:
        """Test approving a task in needs_review status."""
        mock_task_manager.get_task.return_value = sample_task_needs_review
        approved_task = replace(sample_task_needs_review, status="review_approved")
        mock_task_manager.mark_task_review_approved.return_value = approved_task

        tool_func = lifecycle_registry._tools["mark_task_review_approved"].func
        result = tool_func(
            task_id="#42",
        )

        assert "error" not in result
        mock_task_manager.mark_task_review_approved.assert_called_once_with(
            "#42",
            approval_notes=None,
        )

    def test_approve_in_progress_task(
        self, lifecycle_registry, mock_task_manager, sample_task_in_progress
    ) -> None:
        """Test approving a task in in_progress status (also valid)."""
        mock_task_manager.get_task.return_value = sample_task_in_progress
        mock_task_manager.mark_task_review_approved.return_value = sample_task_in_progress

        tool_func = lifecycle_registry._tools["mark_task_review_approved"].func
        result = tool_func(
            task_id="#42",
        )

        assert "error" not in result

    def test_approve_rejects_open_task(
        self, lifecycle_registry, mock_task_manager, sample_task_open
    ) -> None:
        """Test that approving an open task is rejected."""
        mock_task_manager.get_task.return_value = sample_task_open

        tool_func = lifecycle_registry._tools["mark_task_review_approved"].func
        result = tool_func(
            task_id="#42",
        )

        assert "error" in result
        assert "Cannot approve" in result["error"]
        mock_task_manager.mark_task_review_approved.assert_not_called()

    def test_approve_rejects_closed_task(self, lifecycle_registry, mock_task_manager) -> None:
        """Test that approving a closed task is rejected."""
        closed_task = Task(
            id="550e8400-e29b-41d4-a716-446655440000",
            project_id="proj-1",
            title="Test Task",
            status="closed",
            priority=2,
            task_type="task",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
        )
        mock_task_manager.get_task.return_value = closed_task

        tool_func = lifecycle_registry._tools["mark_task_review_approved"].func
        result = tool_func(
            task_id="#42",
        )

        assert "error" in result
        assert "Cannot approve" in result["error"]

    def test_approve_with_notes(
        self, lifecycle_registry, mock_task_manager, sample_task_needs_review
    ) -> None:
        """Test approving with approval notes appends to description."""
        mock_task_manager.get_task.return_value = sample_task_needs_review
        approved_task = replace(sample_task_needs_review, status="review_approved")
        mock_task_manager.mark_task_review_approved.return_value = approved_task

        tool_func = lifecycle_registry._tools["mark_task_review_approved"].func
        result = tool_func(
            task_id="#42",
            approval_notes="Looks good, all tests pass.",
        )

        assert "error" not in result
        mock_task_manager.mark_task_review_approved.assert_called_once_with(
            "#42",
            approval_notes="Looks good, all tests pass.",
        )

    def test_approve_task_not_found(self, lifecycle_registry, mock_task_manager) -> None:
        """Test approving a task that doesn't exist."""
        from unittest.mock import patch

        from gobby.storage.tasks import TaskNotFoundError

        with patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_status.resolve_task_id_for_mcp",
            side_effect=TaskNotFoundError("Task #999 not found"),
        ):
            tool_func = lifecycle_registry._tools["mark_task_review_approved"].func
            result = tool_func(
                task_id="#999",
            )

        assert "error" in result


class TestApproveSignoffSummary:
    """signoff_summary writes the adversary_verdict session variable."""

    @pytest.fixture(autouse=True)
    def _set_session_context(self):
        with session_context_for_test("session-abc"):
            yield

    @pytest.fixture
    def registry_with_ctx(self, mock_task_manager, mock_sync_manager):
        from gobby.mcp_proxy.tools.tasks._context import RegistryContext
        from gobby.mcp_proxy.tools.tasks._lifecycle import create_lifecycle_registry

        ctx = RegistryContext(
            task_manager=mock_task_manager,
            sync_manager=mock_sync_manager,
        )
        ctx.resolve_session_id = MagicMock(return_value="resolved-session-abc")
        ctx.session_task_manager = MagicMock()
        ctx.session_var_manager = MagicMock()
        ctx.session_manager = MagicMock()
        ctx.get_project_repo_path = MagicMock(return_value=None)
        return create_lifecycle_registry(ctx), ctx

    def test_explicit_signoff_summary_is_persisted(
        self, registry_with_ctx, mock_task_manager, sample_task_needs_review
    ) -> None:
        registry, ctx = registry_with_ctx
        mock_task_manager.get_task.return_value = sample_task_needs_review
        mock_task_manager.mark_task_review_approved.return_value = replace(
            sample_task_needs_review, status="review_approved"
        )

        tool_func = registry._tools["mark_task_review_approved"].func
        result = tool_func(
            task_id="#42",
            signoff_summary="APPROVED: round 13, no blocking findings",
        )

        assert "error" not in result
        ctx.session_var_manager.set_variable.assert_called_once_with(
            "resolved-session-abc",
            "adversary_verdict",
            "APPROVED: round 13, no blocking findings",
        )

    def test_omitted_signoff_summary_synthesizes_stock_template(
        self, registry_with_ctx, mock_task_manager, sample_task_needs_review
    ) -> None:
        registry, ctx = registry_with_ctx
        mock_task_manager.get_task.return_value = sample_task_needs_review
        mock_task_manager.mark_task_review_approved.return_value = replace(
            sample_task_needs_review, status="review_approved"
        )

        tool_func = registry._tools["mark_task_review_approved"].func
        result = tool_func(task_id="#42")

        assert "error" not in result
        ctx.session_var_manager.set_variable.assert_called_once_with(
            "resolved-session-abc",
            "adversary_verdict",
            "Approved #42",
        )


class TestReviewApprovedStatusInModel:
    """Tests for 'review_approved' in Task status Literal."""

    def test_review_approved_is_valid_status(self) -> None:
        """Test that 'review_approved' is a valid task status."""
        task = Task(
            id="test-id",
            project_id="proj-1",
            title="Test",
            status="review_approved",
            priority=2,
            task_type="task",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
        )
        assert task.status == "review_approved"

    def test_all_valid_statuses(self) -> None:
        """Test that all expected statuses are valid in the Literal type."""
        import typing

        # Get the Literal type from the Task dataclass annotation
        hints = typing.get_type_hints(Task)
        status_type = hints["status"]
        # Extract literal values
        valid_statuses = typing.get_args(status_type)
        assert "review_approved" in valid_statuses
        assert "open" in valid_statuses
        assert "closed" in valid_statuses
        assert "needs_review" in valid_statuses
        assert "in_progress" in valid_statuses
