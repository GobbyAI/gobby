"""Structured error code coverage for task MCP lifecycle tools."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.mcp_proxy.tools.tasks._errors import TaskToolErrorCode
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.sync.tasks import TaskSyncManager
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit


def _make_task(
    *,
    task_id: str = "550e8400-e29b-41d4-a716-446655440000",
    status: str = "open",
    assignee: str | None = None,
) -> Task:
    return Task(
        id=task_id,
        project_id="proj-1",
        title="Test Task",
        status=status,
        priority=2,
        task_type="task",
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        assignee=assignee,
        seq_num=42,
    )


@pytest.fixture
def mock_task_manager() -> MagicMock:
    manager = MagicMock(spec=LocalTaskManager)
    manager.db = MagicMock()
    return manager


@pytest.fixture
def mock_sync_manager() -> MagicMock:
    return MagicMock(spec=TaskSyncManager)


def _create_registry(task_manager: MagicMock, sync_manager: MagicMock) -> Any:
    with (
        patch("gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"),
        patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as mock_session_manager_cls,
    ):
        session_manager = MagicMock()
        session_manager.resolve_session_reference.return_value = "session-abc"
        session_manager.get.return_value = MagicMock(project_id="proj-1")
        mock_session_manager_cls.return_value = session_manager
        return create_task_registry(task_manager, sync_manager)


@pytest.mark.asyncio
async def test_claim_task_closed_task_returns_task_closed(
    mock_task_manager: MagicMock,
    mock_sync_manager: MagicMock,
) -> None:
    task = _make_task(status="closed")
    mock_task_manager.get_task.return_value = task
    registry = _create_registry(mock_task_manager, mock_sync_manager)

    with session_context_for_test("session-abc"):
        result = await registry.call("claim_task", {"task_id": task.id})

    assert result["success"] is False
    assert result["status"] == "error"
    assert result["error_code"] == TaskToolErrorCode.TASK_CLOSED.value
    assert result["error"] == f"Cannot claim task {task.id}: task is closed"
    mock_task_manager.claim_task.assert_not_called()


@pytest.mark.asyncio
async def test_claim_task_conflict_returns_claim_conflict(
    mock_task_manager: MagicMock,
    mock_sync_manager: MagicMock,
) -> None:
    task = _make_task(status="in_progress", assignee="other-session")
    mock_task_manager.get_task.return_value = task
    registry = _create_registry(mock_task_manager, mock_sync_manager)

    with session_context_for_test("session-abc"):
        result = await registry.call("claim_task", {"task_id": task.id})

    assert result["success"] is False
    assert result["status"] == "error"
    assert result["error_code"] == TaskToolErrorCode.TASK_CLAIM_CONFLICT.value
    assert result["error"] == "Task already claimed by another session"
    assert result["claimed_by"] == "other-session"
    mock_task_manager.claim_task.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "extra_args", "error_fragment"),
    [
        ("escalate_task", {"reason": "blocked"}, "Cannot escalate task with status 'closed'."),
        ("mark_task_review_approved", {}, "Cannot approve task with status 'closed'."),
        ("mark_task_review_rejected", {}, "Cannot reject review for task with status 'closed'."),
        ("mark_task_needs_review", {}, "Cannot mark task with status 'closed' as needs_review."),
        ("de_escalate_task", {"reason": "resolved"}, "current status: closed"),
    ],
)
async def test_lifecycle_closed_task_returns_task_closed(
    mock_task_manager: MagicMock,
    mock_sync_manager: MagicMock,
    tool_name: str,
    extra_args: dict[str, Any],
    error_fragment: str,
) -> None:
    task = _make_task(status="closed")
    mock_task_manager.get_task.return_value = task
    registry = _create_registry(mock_task_manager, mock_sync_manager)

    with session_context_for_test("session-abc"):
        result = await registry.call(tool_name, {"task_id": task.id, **extra_args})

    assert result["success"] is False
    assert result["status"] == "error"
    assert result["error_code"] == TaskToolErrorCode.TASK_CLOSED.value
    assert error_fragment in result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "status", "extra_args"),
    [
        ("escalate_task", "escalated", {"reason": "blocked"}),
        ("mark_task_review_approved", "open", {}),
        ("mark_task_review_rejected", "open", {}),
        ("mark_task_needs_review", "escalated", {}),
        ("de_escalate_task", "open", {"reason": "resolved"}),
    ],
)
async def test_lifecycle_invalid_status_returns_task_invalid_status(
    mock_task_manager: MagicMock,
    mock_sync_manager: MagicMock,
    tool_name: str,
    status: str,
    extra_args: dict[str, Any],
) -> None:
    task = _make_task(status=status)
    mock_task_manager.get_task.return_value = task
    registry = _create_registry(mock_task_manager, mock_sync_manager)

    with session_context_for_test("session-abc"):
        result = await registry.call(tool_name, {"task_id": task.id, **extra_args})

    assert result["success"] is False
    assert result["status"] == "error"
    assert result["error_code"] == TaskToolErrorCode.TASK_INVALID_STATUS.value


@pytest.mark.asyncio
async def test_lifecycle_value_error_with_unrelated_status_word_is_generic(
    mock_task_manager: MagicMock,
    mock_sync_manager: MagicMock,
) -> None:
    task = _make_task(status="open")
    message = "Cannot update username status cache"
    mock_task_manager.get_task.return_value = task
    mock_task_manager.escalate_task.side_effect = ValueError(message)
    registry = _create_registry(mock_task_manager, mock_sync_manager)

    with session_context_for_test("session-abc"):
        result = await registry.call(
            "escalate_task",
            {"task_id": task.id, "reason": "blocked"},
        )

    assert result == {"error": message}
