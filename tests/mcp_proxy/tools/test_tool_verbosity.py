import unittest.mock
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.tools.memory import create_memory_registry
from gobby.mcp_proxy.tools.sessions import create_session_messages_registry
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.mcp_proxy.tools.worktrees import create_worktrees_registry
from gobby.sessions.transcript_reader import TranscriptReader
from gobby.sessions.transcript_render_models import RenderedMessage
from gobby.sessions.transcript_window import WindowResult
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_memory_verbosity_reduction() -> None:
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

    registry = create_memory_registry(lambda: mock_manager)

    # Test create_memory
    result = await registry.call(
        "create_memory",
        {
            "content": "test",
            "rationale": "Future sessions need this fact to avoid re-deriving it.",
        },
    )
    assert result["success"] is True, result
    assert result["memory"]["id"] == "mem-123"
    # Should NOT contain content in the improved version
    assert "content" not in result["memory"]


@pytest.mark.asyncio
async def test_task_verbosity_reduction(
    temp_db: HubDatabase,
    canonical_task_session: Session,
) -> None:
    """Verify create_task doesn't echo back full task."""
    mock_manager = MagicMock()
    mock_manager.db = temp_db
    mock_sync = MagicMock()

    mock_task = MagicMock()
    mock_task.id = "task-123"
    mock_task.seq_num = 123
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

    with session_context_for_test(canonical_task_session.id):
        result = await registry.call(
            "create_task",
            {
                "title": "test",
                "category": "research",
                "validation_criteria": "Test task completion is observable.",
            },
        )
    assert result["id"] == "task-123"
    assert "description" not in result


@pytest.mark.asyncio
async def test_worktree_verbosity_reduction() -> None:
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
        mock_ctx.return_value = (mock_git, "11111111-1111-4111-8111-111111110123", None)

        registry = create_worktrees_registry(
            mock_storage, mock_git, project_id="11111111-1111-4111-8111-111111110123"
        )

        result = await registry.call("create_worktree", {"branch_name": "feat/test"})

        assert result["success"] is True
        assert result["worktree_id"] == "wt-123"
        # Should be minimal


@pytest.mark.asyncio
async def test_session_message_truncation() -> None:
    """Verify get_session_messages returns complete content (#20401 removed slicing)."""
    mock_session_manager = MagicMock()
    mock_reader = MagicMock(spec=TranscriptReader)
    mock_reader.get_rendered_window = AsyncMock(
        return_value=WindowResult(
            groups=[
                RenderedMessage(
                    id="message-1",
                    role="user",
                    content="A" * 1000,
                    timestamp=datetime(2026, 7, 17, tzinfo=UTC),
                )
            ],
            returned_count=1,
            total_groups=1,
            parsed_message_count=1,
        )
    )
    mock_reader.count_messages = AsyncMock(return_value=1)

    registry = create_session_messages_registry(
        transcript_reader=mock_reader, session_manager=mock_session_manager
    )

    result = await registry.call("get_session_messages", {"session_id": "sess-123"})
    assert result.get("success") is True, f"Result failed: {result}"
    msg = result["messages"][0]

    assert len(msg["content"]) == 1000
    assert result["truncated"] is False

    # The legacy opt-in flag is accepted and still returns complete content.
    result_full = await registry.call(
        "get_session_messages", {"session_id": "sess-123", "full_content": False}
    )
    assert len(result_full["messages"][0]["content"]) == 1000
