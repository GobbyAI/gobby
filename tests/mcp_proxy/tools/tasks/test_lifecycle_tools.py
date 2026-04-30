"""Phase 2 tests for merge lifecycle MCP tools."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle import create_lifecycle_registry

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_task_manager() -> MagicMock:
    manager = MagicMock()
    manager.db = MagicMock()
    manager.get_task.return_value = MagicMock(id="task-1", status="open", lifecycle="pr")
    return manager


@pytest.fixture
def lifecycle_registry(mock_task_manager: MagicMock):
    return create_lifecycle_registry(
        RegistryContext(task_manager=mock_task_manager, sync_manager=MagicMock())
    )


def test_mark_task_pr_opened_tool_from_open(lifecycle_registry, mock_task_manager) -> None:
    tool = lifecycle_registry._tools["mark_task_pr_opened"].func
    result = tool(task_id="#1", pr_url="https://example.test/pr/1")

    assert "error" not in result
    mock_task_manager.mark_task_pr_opened.assert_called_once_with(
        "#1",
        pr_url="https://example.test/pr/1",
    )


def test_mark_task_pr_opened_tool_recovers_from_attended_pr_creation_escalation(
    lifecycle_registry,
    mock_task_manager,
) -> None:
    mock_task_manager.get_task.return_value = MagicMock(
        id="task-1",
        status="escalated",
        lifecycle="pr",
        is_escalated=True,
    )
    tool = lifecycle_registry._tools["mark_task_pr_opened"].func
    result = tool(task_id="#1", pr_url="https://example.test/pr/1")

    assert "error" not in result
    mock_task_manager.mark_task_pr_opened.assert_called_once_with(
        "#1",
        pr_url="https://example.test/pr/1",
    )


def test_mark_task_merged_tool_covers_worktree_clone_and_skipped_pr_paths(
    lifecycle_registry,
    mock_task_manager,
) -> None:
    tool = lifecycle_registry._tools["mark_task_merged"].func

    assert "error" not in tool(task_id="#1", pr_url="https://example.test/pr/1")
    assert "error" not in tool(task_id="#1", merge_sha="abc123")
    assert "error" not in tool(task_id="#1")

    assert mock_task_manager.mark_task_merged.call_count == 3


def test_mark_task_merge_failed_tool_routes_per_attended_mode(
    lifecycle_registry,
    mock_task_manager,
) -> None:
    tool = lifecycle_registry._tools["mark_task_merge_failed"].func
    result = tool(task_id="#1", reason="conflict")

    assert "error" not in result
    mock_task_manager.mark_task_merge_failed.assert_called_once_with("#1", reason="conflict")
