"""Tests for mark_task_review_rejected MCP tool."""

from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

from gobby.storage.tasks import Task
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_task_manager():
    manager = MagicMock()
    manager.db = MagicMock()
    return manager


@pytest.fixture
def mock_sync_manager():
    return MagicMock()


@pytest.fixture
def sample_task_needs_review():
    return Task(
        id="550e8400-e29b-41d4-a716-446655440000",
        project_id="proj-1",
        title="Planning Task",
        status="needs_review",
        priority=2,
        task_type="task",
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        description="Existing description",
        labels=["planning-round:0"],
        seq_num=42,
    )


@pytest.fixture
def sample_task_open():
    return Task(
        id="550e8400-e29b-41d4-a716-446655440000",
        project_id="proj-1",
        title="Planning Task",
        status="open",
        priority=2,
        task_type="task",
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )


@pytest.fixture
def sample_task_in_progress():
    return Task(
        id="550e8400-e29b-41d4-a716-446655440000",
        project_id="proj-1",
        title="Planning Task",
        status="in_progress",
        priority=2,
        task_type="task",
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        description="Existing description",
        labels=["planning-round:0"],
        seq_num=42,
        claimed_by_session_id="session-abc",
        assignee="session-abc",
    )


@pytest.fixture
def lifecycle_registry(mock_task_manager, mock_sync_manager):
    from gobby.mcp_proxy.tools.tasks._context import RegistryContext
    from gobby.mcp_proxy.tools.tasks._lifecycle import create_lifecycle_registry

    ctx = RegistryContext(
        task_manager=mock_task_manager,
        sync_manager=mock_sync_manager,
    )
    ctx.resolve_session_id = MagicMock(return_value="resolved-session-abc")
    ctx.session_task_manager = MagicMock()
    ctx.session_var_manager = MagicMock()
    return create_lifecycle_registry(ctx), ctx


class TestMarkTaskReviewRejected:
    @pytest.fixture(autouse=True)
    def _set_session_context(self):
        with session_context_for_test("session-abc"):
            yield

    @pytest.mark.asyncio
    async def test_reject_needs_review_task(
        self, lifecycle_registry, mock_task_manager, sample_task_needs_review
    ) -> None:
        registry, ctx = lifecycle_registry
        mock_task_manager.get_task.side_effect = (
            lambda task_id: sample_task_needs_review
            if task_id == sample_task_needs_review.id
            else None
        )
        rejected_task = replace(
            sample_task_needs_review,
            status="open",
            labels=["planning-round:1"],
        )
        mock_task_manager.mark_task_review_rejected.return_value = rejected_task

        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._lifecycle_status.resolve_task_id_for_mcp",
                return_value=sample_task_needs_review.id,
            ),
            patch("gobby.mcp_proxy.tools.tasks._lifecycle_status.notify_parent_on_status_change"),
        ):
            result = await registry.call(
                "mark_task_review_rejected",
                {
                    "task_id": "#42",
                    "rejection_notes": "Need better sequencing",
                    "round_number": 1,
                },
            )

        assert "error" not in result
        mock_task_manager.mark_task_review_rejected.assert_called_once_with(
            sample_task_needs_review.id,
            rejection_notes="Need better sequencing",
            round_number=1,
        )
        ctx.session_task_manager.link_task.assert_called_once_with(
            "resolved-session-abc",
            sample_task_needs_review.id,
            "review_rejected",
        )

    @pytest.mark.asyncio
    async def test_reject_in_progress_task(
        self, lifecycle_registry, mock_task_manager, sample_task_in_progress
    ) -> None:
        registry, _ = lifecycle_registry
        mock_task_manager.get_task.side_effect = (
            lambda task_id: sample_task_in_progress
            if task_id == sample_task_in_progress.id
            else None
        )
        rejected_task = replace(
            sample_task_in_progress,
            status="open",
            labels=["planning-round:1"],
            claimed_by_session_id=None,
            assignee=None,
        )
        mock_task_manager.mark_task_review_rejected.return_value = rejected_task

        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._lifecycle_status.resolve_task_id_for_mcp",
                return_value=sample_task_in_progress.id,
            ),
            patch("gobby.mcp_proxy.tools.tasks._lifecycle_status.notify_parent_on_status_change"),
            patch(
                "gobby.mcp_proxy.tools.tasks._lifecycle_status._clear_prior_claim_session_variables"
            ),
        ):
            result = await registry.call(
                "mark_task_review_rejected",
                {
                    "task_id": "#42",
                    "rejection_notes": "Need better sequencing",
                    "round_number": 1,
                },
            )

        assert "error" not in result
        mock_task_manager.mark_task_review_rejected.assert_called_once_with(
            sample_task_in_progress.id,
            rejection_notes="Need better sequencing",
            round_number=1,
        )

    @pytest.mark.asyncio
    async def test_reject_rejects_open_task(
        self, lifecycle_registry, mock_task_manager, sample_task_open
    ) -> None:
        registry, _ = lifecycle_registry
        mock_task_manager.get_task.side_effect = (
            lambda task_id: sample_task_open if task_id == sample_task_open.id else None
        )

        with patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_status.resolve_task_id_for_mcp",
            return_value=sample_task_open.id,
        ):
            result = await registry.call("mark_task_review_rejected", {"task_id": "#42"})

        assert "error" in result
        assert "Cannot reject review" in result["error"]
        mock_task_manager.mark_task_review_rejected.assert_not_called()

    @pytest.mark.asyncio
    async def test_reject_writes_explicit_signoff_summary_to_session_var(
        self, lifecycle_registry, mock_task_manager, sample_task_needs_review
    ) -> None:
        registry, ctx = lifecycle_registry
        mock_task_manager.get_task.side_effect = (
            lambda task_id: sample_task_needs_review
            if task_id == sample_task_needs_review.id
            else None
        )
        mock_task_manager.mark_task_review_rejected.return_value = replace(
            sample_task_needs_review,
            status="open",
            labels=["planning-round:7"],
        )
        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._lifecycle_status.resolve_task_id_for_mcp",
                return_value=sample_task_needs_review.id,
            ),
            patch("gobby.mcp_proxy.tools.tasks._lifecycle_status.notify_parent_on_status_change"),
        ):
            result = await registry.call(
                "mark_task_review_rejected",
                {
                    "task_id": "#42",
                    "rejection_notes": "stuff",
                    "round_number": 7,
                    "signoff_summary": "REJECTED: round 7, 2 blocking (alpha, beta)",
                },
            )
        assert "error" not in result
        ctx.session_var_manager.set_variable.assert_called_once_with(
            "resolved-session-abc",
            "adversary_verdict",
            "REJECTED: round 7, 2 blocking (alpha, beta)",
        )

    @pytest.mark.asyncio
    async def test_reject_synthesizes_stock_signoff_when_omitted(
        self, lifecycle_registry, mock_task_manager, sample_task_needs_review
    ) -> None:
        registry, ctx = lifecycle_registry
        mock_task_manager.get_task.side_effect = (
            lambda task_id: sample_task_needs_review
            if task_id == sample_task_needs_review.id
            else None
        )
        mock_task_manager.mark_task_review_rejected.return_value = replace(
            sample_task_needs_review,
            status="open",
            labels=["planning-round:3"],
        )
        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._lifecycle_status.resolve_task_id_for_mcp",
                return_value=sample_task_needs_review.id,
            ),
            patch("gobby.mcp_proxy.tools.tasks._lifecycle_status.notify_parent_on_status_change"),
        ):
            result = await registry.call(
                "mark_task_review_rejected",
                {"task_id": "#42", "rejection_notes": "stuff", "round_number": 3},
            )
        assert "error" not in result
        ctx.session_var_manager.set_variable.assert_called_once_with(
            "resolved-session-abc",
            "adversary_verdict",
            "Rejected #42 round 3",
        )
