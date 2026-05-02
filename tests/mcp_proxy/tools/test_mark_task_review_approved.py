"""Tests for approve_review MCP tool and review_approved status.

Tests status transitions, validation, and blocked status in update_task.
"""

from unittest.mock import ANY, MagicMock

import pytest

from gobby.storage.tasks import Task
from gobby.storage.tasks._stage_states import StageState
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_task_manager():
    """Create a mock task manager."""
    manager = MagicMock()
    manager.db = MagicMock()
    manager.stage_states = MagicMock()
    manager.stage_states.get.return_value = _stage_state(
        "550e8400-e29b-41d4-a716-446655440000",
        state="review_approved",
    )
    return manager


def _stage_state(
    task_id: str,
    *,
    stage_name: str = "planning",
    state: str = "needs_review",
) -> StageState:
    return StageState(
        task_id=task_id,
        stage_name=stage_name,
        position=0,
        state=state,  # type: ignore[arg-type]
        review_policy="required",
        reviewer_agent="plan-adversary",
        entered_at=None,
        entered_by_session_id=None,
        completed_at=None,
        completed_by_session_id=None,
        completed_commit_sha=None,
        work_attempt_count=1,
        review_round_count=0,
        max_work_attempts=None,
        max_review_rounds=None,
        artifact_refs=None,
        notes=None,
        updated_at="2024-01-01T00:00:00Z",
    )


def _task(
    *,
    status: str,
    description: str | None = None,
    seq_num: int | None = None,
) -> Task:
    stage_state = {
        "open": "ready",
        "closed": "done",
    }.get(status, status)
    return Task(
        id="550e8400-e29b-41d4-a716-446655440000",
        project_id="proj-1",
        title="Test Task",
        priority=2,
        task_type="task",
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        description=description,
        seq_num=seq_num,
        closed_at="2024-01-02T00:00:00Z" if status == "closed" else None,
        stages=(
            {
                "stage_name": "planning",
                "position": 0,
                "state": stage_state,
            },
        ),
    )


@pytest.fixture
def mock_sync_manager():
    """Create a mock sync manager."""
    return MagicMock()


@pytest.fixture
def sample_task_needs_review():
    """Create a task in needs_review status."""
    return _task(status="needs_review", description="Test description", seq_num=42)


@pytest.fixture
def sample_task_in_progress():
    """Create a task in in_progress status."""
    return _task(status="in_progress", description="Original description", seq_num=42)


@pytest.fixture
def sample_task_open():
    """Create a task in open status."""
    return _task(status="open")


@pytest.fixture
def stage_ops_registry(mock_task_manager, mock_sync_manager):
    """Create a stage ops registry with approve_review tool."""
    from gobby.mcp_proxy.tools.tasks._context import RegistryContext
    from gobby.mcp_proxy.tools.tasks._stage_ops import create_stage_ops_registry

    ctx = RegistryContext(
        task_manager=mock_task_manager,
        sync_manager=mock_sync_manager,
    )
    ctx.resolve_session_id = MagicMock(return_value="resolved-session-abc")
    ctx.session_manager = MagicMock()
    ctx.session_manager.get.return_value = None
    ctx.session_task_manager = MagicMock()
    ctx.session_var_manager = MagicMock()
    ctx.get_project_repo_path = MagicMock(return_value=None)
    return create_stage_ops_registry(ctx)


class TestMarkTaskReviewApproved:
    """Tests for approve_review lifecycle tool."""

    @pytest.fixture(autouse=True)
    def _set_session_context(self):
        with session_context_for_test("session-abc"):
            yield

    def test_approve_needs_review_task(
        self, stage_ops_registry, mock_task_manager, sample_task_needs_review
    ) -> None:
        """Test approving a task in needs_review status."""
        mock_task_manager.get_task.return_value = sample_task_needs_review
        approved_task = _task(
            status="review_approved",
            description=sample_task_needs_review.description,
            seq_num=sample_task_needs_review.seq_num,
        )
        mock_task_manager.approve_review.return_value = approved_task

        tool_func = stage_ops_registry._tools["approve_review"].func
        result = tool_func(
            task_id=sample_task_needs_review.id,
            stage_name="planning",
        )

        assert "error" not in result
        mock_task_manager.approve_review.assert_called_once_with(
            sample_task_needs_review.id,
            "planning",
            approval_notes=None,
            by_session_id=ANY,
        )

    def test_approve_in_progress_task(
        self, stage_ops_registry, mock_task_manager, sample_task_in_progress
    ) -> None:
        """Test approving a task in in_progress status (also valid)."""
        mock_task_manager.get_task.return_value = sample_task_in_progress
        mock_task_manager.approve_review.return_value = sample_task_in_progress

        tool_func = stage_ops_registry._tools["approve_review"].func
        result = tool_func(
            task_id=sample_task_in_progress.id,
            stage_name="planning",
        )

        assert "error" not in result

    def test_approve_rejects_open_task(
        self, stage_ops_registry, mock_task_manager, sample_task_open
    ) -> None:
        """Open legacy status is delegated to stage-state validation."""
        mock_task_manager.get_task.return_value = sample_task_open
        mock_task_manager.approve_review.side_effect = ValueError(
            "Illegal stage transition"
        )

        tool_func = stage_ops_registry._tools["approve_review"].func
        result = tool_func(
            task_id=sample_task_open.id,
            stage_name="planning",
        )

        assert "error" in result
        assert "Illegal stage transition" in result["error"]
        mock_task_manager.approve_review.assert_called_once()

    def test_approve_rejects_closed_task(self, stage_ops_registry, mock_task_manager) -> None:
        """Closed legacy status is delegated to stage-state validation."""
        closed_task = _task(status="closed")
        mock_task_manager.get_task.return_value = closed_task
        mock_task_manager.approve_review.side_effect = ValueError(
            "No current stage"
        )

        tool_func = stage_ops_registry._tools["approve_review"].func
        result = tool_func(
            task_id=closed_task.id,
            stage_name="planning",
        )

        assert "error" in result
        assert "No current stage" in result["error"]

    def test_approve_with_notes(
        self, stage_ops_registry, mock_task_manager, sample_task_needs_review
    ) -> None:
        """Test approving with approval notes appends to description."""
        mock_task_manager.get_task.return_value = sample_task_needs_review
        approved_task = _task(
            status="review_approved",
            description=sample_task_needs_review.description,
            seq_num=sample_task_needs_review.seq_num,
        )
        mock_task_manager.approve_review.return_value = approved_task

        tool_func = stage_ops_registry._tools["approve_review"].func
        result = tool_func(
            task_id=sample_task_needs_review.id,
            stage_name="planning",
            approval_notes="Looks good, all tests pass.",
        )

        assert "error" not in result
        mock_task_manager.approve_review.assert_called_once_with(
            sample_task_needs_review.id,
            "planning",
            approval_notes="Looks good, all tests pass.",
            by_session_id=ANY,
        )

    def test_approve_task_not_found(self, stage_ops_registry, mock_task_manager) -> None:
        """Test approving a task that doesn't exist."""
        from unittest.mock import patch

        from gobby.storage.tasks import TaskNotFoundError

        with patch(
            "gobby.mcp_proxy.tools.tasks._stage_review.resolve_task_id_for_mcp",
            side_effect=TaskNotFoundError("Task #999 not found"),
        ):
            tool_func = stage_ops_registry._tools["approve_review"].func
            result = tool_func(
                task_id="#999",
                stage_name="planning",
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
        from gobby.mcp_proxy.tools.tasks._stage_ops import create_stage_ops_registry

        ctx = RegistryContext(
            task_manager=mock_task_manager,
            sync_manager=mock_sync_manager,
        )
        ctx.resolve_session_id = MagicMock(return_value="resolved-session-abc")
        ctx.session_task_manager = MagicMock()
        ctx.session_var_manager = MagicMock()
        ctx.session_manager = MagicMock()
        ctx.session_manager.get.return_value = None
        ctx.get_project_repo_path = MagicMock(return_value=None)
        return create_stage_ops_registry(ctx), ctx

    def test_explicit_signoff_summary_is_persisted(
        self, registry_with_ctx, mock_task_manager, sample_task_needs_review
    ) -> None:
        registry, ctx = registry_with_ctx
        mock_task_manager.get_task.return_value = sample_task_needs_review
        mock_task_manager.approve_review.return_value = _task(
            status="review_approved",
            description=sample_task_needs_review.description,
            seq_num=sample_task_needs_review.seq_num,
        )

        tool_func = registry._tools["approve_review"].func
        result = tool_func(
            task_id=sample_task_needs_review.id,
            stage_name="planning",
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
        mock_task_manager.approve_review.return_value = _task(
            status="review_approved",
            description=sample_task_needs_review.description,
            seq_num=sample_task_needs_review.seq_num,
        )

        tool_func = registry._tools["approve_review"].func
        result = tool_func(task_id=sample_task_needs_review.id, stage_name="planning")

        assert "error" not in result
        ctx.session_var_manager.set_variable.assert_called_once_with(
            "resolved-session-abc",
            "adversary_verdict",
            "Approved #42",
        )
