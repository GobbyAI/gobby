"""Focused tests for task GitHub MCP tools."""

from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.task_github import create_github_registry

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_import_github_issues_fails_before_fetch_for_unresolvable_parent() -> None:
    """A bad shared parent must not silently re-root imported issues."""
    task_manager = MagicMock()
    ctx = MagicMock(task_manager=task_manager)

    with (
        patch(
            "gobby.mcp_proxy.tools.tasks.resolve_task_id_for_mcp",
            side_effect=ValueError("Task #99999 not found"),
        ) as mock_resolve,
        patch(
            "gobby.mcp_proxy.tools.task_github.get_project_context",
            return_value={"id": "project-id"},
        ),
        patch("gobby.mcp_proxy.tools.task_github._fetch_issues_via_gh") as mock_fetch,
    ):
        registry = create_github_registry(ctx)
        result = await registry.call(
            "import_github_issues",
            {"repo": "owner/repo", "parent_task_id": "#99999"},
        )

    assert result == {
        "success": False,
        "error": "Could not resolve parent task '#99999': Task #99999 not found",
    }
    assert mock_resolve.call_count == 1
    assert mock_fetch.call_count == 0
    mock_resolve.assert_called_once_with(task_manager, "#99999", "project-id")
    mock_fetch.assert_not_called()
    task_manager.create_task.assert_not_called()
    task_manager.update_task.assert_not_called()


@pytest.mark.asyncio
async def test_import_github_issues_resolves_parent_once_and_creates_nested_task() -> None:
    """A valid shared parent is resolved once and applied during task creation."""
    task_manager = MagicMock()
    created_task = MagicMock()
    created_task.to_brief.return_value = {"id": "created-task"}
    task_manager.create_task.return_value = created_task
    ctx = MagicMock(task_manager=task_manager)
    issue = {
        "number": 42,
        "title": "Imported issue",
        "body": "Issue body",
        "labels": [{"name": "bug"}],
    }

    with (
        patch(
            "gobby.mcp_proxy.tools.tasks.resolve_task_id_for_mcp",
            return_value="parent-uuid",
        ) as mock_resolve,
        patch(
            "gobby.mcp_proxy.tools.task_github.get_project_context",
            return_value={"id": "project-id"},
        ),
        patch("gobby.mcp_proxy.tools.task_github._fetch_issues_via_gh", return_value=[issue]),
        patch("gobby.mcp_proxy.tools.task_github._find_task_by_github_issue", return_value=None),
    ):
        registry = create_github_registry(ctx)
        result = await registry.call(
            "import_github_issues",
            {"repo": "owner/repo", "parent_task_id": "#123"},
        )

    assert result == {
        "success": True,
        "imported_count": 1,
        "updated_count": 0,
        "tasks": [{"id": "created-task"}],
    }
    assert mock_resolve.call_count == 1
    assert task_manager.create_task.call_count == 1
    mock_resolve.assert_called_once_with(task_manager, "#123", "project-id")
    task_manager.create_task.assert_called_once_with(
        project_id="project-id",
        title="Imported issue",
        description="Issue body",
        parent_task_id="parent-uuid",
        github_issue_number=42,
        github_repo="owner/repo",
        labels=["bug"],
    )
    task_manager.update_task.assert_not_called()


@pytest.mark.asyncio
async def test_import_github_issues_applies_parent_to_existing_task() -> None:
    """A deduplicated issue is moved under the explicitly requested parent."""
    task_manager = MagicMock()
    existing_task = MagicMock(id="existing-task")
    updated_task = MagicMock()
    updated_task.to_brief.return_value = {"id": "existing-task"}
    task_manager.get_task.return_value = updated_task
    ctx = MagicMock(task_manager=task_manager)
    issue = {"number": 42, "title": "Updated issue", "body": "New body", "labels": []}

    with (
        patch(
            "gobby.mcp_proxy.tools.tasks.resolve_task_id_for_mcp",
            return_value="parent-uuid",
        ) as mock_resolve,
        patch(
            "gobby.mcp_proxy.tools.task_github.get_project_context",
            return_value={"id": "project-id"},
        ),
        patch("gobby.mcp_proxy.tools.task_github._fetch_issues_via_gh", return_value=[issue]),
        patch(
            "gobby.mcp_proxy.tools.task_github._find_task_by_github_issue",
            return_value=existing_task,
        ),
    ):
        registry = create_github_registry(ctx)
        result = await registry.call(
            "import_github_issues",
            {"repo": "owner/repo", "parent_task_id": "#123"},
        )

    assert result["success"] is True
    assert result["updated_count"] == 1
    assert result["tasks"] == [{"id": "existing-task"}]
    mock_resolve.assert_called_once_with(task_manager, "#123", "project-id")
    task_manager.update_task.assert_called_once_with(
        "existing-task",
        title="Updated issue",
        description="New body",
        labels=None,
        parent_task_id="parent-uuid",
    )
    task_manager.create_task.assert_not_called()
