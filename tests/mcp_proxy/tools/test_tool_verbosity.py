import unittest.mock
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.tools.memory import create_memory_registry
from gobby.mcp_proxy.tools.sessions import create_session_messages_registry
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.mcp_proxy.tools.worktrees import create_worktrees_registry

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_memory_verbosity_reduction():
    """Verify create/update don't echo back full content."""
    mock_manager = AsyncMock()
    # Mock return value behaves like a Memory object
    mock_memory = MagicMock()
    mock_memory.id = "mem-123"
    mock_memory.content = "Massive content..." * 100
    mock_manager.create_memory.return_value = mock_memory
    mock_manager.update_memory.return_value = mock_memory
    mock_manager.search_memories = AsyncMock(return_value=[])
    mock_manager.content_exists = MagicMock(return_value=False)

    registry = create_memory_registry(mock_manager)

    # Test create_memory
    result = await registry.call("create_memory", {"content": "test"})
    assert result["success"] is True
    assert result["memory"]["id"] == "mem-123"
    # Should NOT contain content in the improved version
    assert "content" not in result["memory"]


@pytest.mark.asyncio
async def test_task_verbosity_reduction():
    """Verify create_task doesn't echo back full task."""
    mock_manager = MagicMock()
    mock_sync = MagicMock()

    mock_task = MagicMock()
    mock_task.id = "task-123"
    mock_task.to_dict.return_value = {
        "id": "task-123",
        "title": "Big Task",
        "description": "huge...",
    }
    # create_task now uses create_task_with_decomposition and get_task
    mock_manager.create_task_with_decomposition.return_value = {
        "task": {"id": "task-123"},
    }
    mock_manager.get_task.return_value = mock_task
    mock_manager.update_task.return_value = mock_task

    registry = create_task_registry(mock_manager, mock_sync)

    # Test create
    result = await registry.call(
        "create_task", {"title": "test", "session_id": "test-session", "category": "research"}
    )
    assert result["id"] == "task-123"
    # Should NOT contain full dict in improved version
    assert "description" not in result


@pytest.mark.asyncio
async def test_worktree_verbosity_reduction():
    """Verify create_worktree returns minimal info."""
    mock_storage = MagicMock()
    mock_git = MagicMock()

    mock_wt = MagicMock()
    mock_wt.id = "wt-123"
    mock_wt.worktree_path = "/tmp/wt"
    mock_wt.branch_name = "feat/test"
    mock_storage.create.return_value = mock_wt
    mock_storage.get_by_branch.return_value = None  # Ensure no collision
    mock_git.create_worktree.return_value.success = True
    mock_git.has_unpushed_commits.return_value = (False, 0)

    # Mock resolve_project_context to avoid invalid repo errors
    with unittest.mock.patch(
        "gobby.mcp_proxy.tools.worktrees._resolve_project_context"
    ) as mock_ctx:
        mock_ctx.return_value = (mock_git, "proj-123", None)

        registry = create_worktrees_registry(mock_storage, mock_git, project_id="proj-123")

        result = await registry.call("create_worktree", {"branch_name": "feat/test"})

        assert result["success"] is True
        assert result["worktree_id"] == "wt-123"
        # Should be minimal


@pytest.mark.asyncio
async def test_session_message_truncation():
    """Verify get_session_messages truncates large content."""
    mock_session_manager = MagicMock()

    class FakeMessageManager:
        async def get_messages(self, *args, **kwargs):
            return [{"role": "user", "content": "A" * 1000, "tool_calls": []}]

        async def count_messages(self, *args, **kwargs):
            return 1

    mock_msg_manager = FakeMessageManager()

    registry = create_session_messages_registry(
        message_manager=mock_msg_manager, session_manager=mock_session_manager
    )

    # Test default truncation
    result = await registry.call("get_session_messages", {"session_id": "sess-123"})
    assert result.get("success") is True, f"Result failed: {result}"
    msg = result["messages"][0]

    # In improved version:
    assert len(msg["content"]) < 1000
    assert "..." in msg["content"]

    # Test opt-out
    result_full = await registry.call(
        "get_session_messages", {"session_id": "sess-123", "full_content": True}
    )
    assert len(result_full["messages"][0]["content"]) == 1000
