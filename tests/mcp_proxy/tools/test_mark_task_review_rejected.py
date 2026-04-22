"""Tests for mark_task_review_rejected MCP tool."""

from dataclasses import replace
from unittest.mock import MagicMock

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
    return create_lifecycle_registry(ctx)


class TestMarkTaskReviewRejected:
    @pytest.fixture(autouse=True)
    def _set_session_context(self):
        with session_context_for_test("session-abc"):
            yield

    def test_reject_needs_review_task(
        self, lifecycle_registry, mock_task_manager, sample_task_needs_review
    ) -> None:
        mock_task_manager.get_task.return_value = sample_task_needs_review
        rejected_task = replace(
            sample_task_needs_review,
            status="open",
            labels=["planning-round:1"],
        )
        mock_task_manager.mark_task_review_rejected.return_value = rejected_task

        tool_func = lifecycle_registry._tools["mark_task_review_rejected"].func
        result = tool_func(
            task_id="#42",
            rejection_notes="Need better sequencing",
            round=1,
        )

        assert "error" not in result
        mock_task_manager.mark_task_review_rejected.assert_called_once_with(
            "#42",
            rejection_notes="Need better sequencing",
            round=1,
        )

    def test_reject_in_progress_task(
        self, lifecycle_registry, mock_task_manager, sample_task_in_progress
    ) -> None:
        mock_task_manager.get_task.return_value = sample_task_in_progress
        rejected_task = replace(
            sample_task_in_progress,
            status="open",
            labels=["planning-round:1"],
            claimed_by_session_id=None,
            assignee=None,
        )
        mock_task_manager.mark_task_review_rejected.return_value = rejected_task

        tool_func = lifecycle_registry._tools["mark_task_review_rejected"].func
        result = tool_func(
            task_id="#42",
            rejection_notes="Need better sequencing",
            round=1,
        )

        assert "error" not in result
        mock_task_manager.mark_task_review_rejected.assert_called_once_with(
            "#42",
            rejection_notes="Need better sequencing",
            round=1,
        )

    def test_reject_rejects_open_task(
        self, lifecycle_registry, mock_task_manager, sample_task_open
    ) -> None:
        mock_task_manager.get_task.return_value = sample_task_open

        tool_func = lifecycle_registry._tools["mark_task_review_rejected"].func
        result = tool_func(task_id="#42")

        assert "error" in result
        assert "Cannot reject review" in result["error"]
        mock_task_manager.mark_task_review_rejected.assert_not_called()
