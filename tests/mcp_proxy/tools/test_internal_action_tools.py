"""Tests for internal action MCP tools.

Verifies that workflow action functions are exposed as MCP tools:
- gobby-memory: backup_memories, restore_memories
- gobby-tasks: backup_tasks, restore_tasks
- gobby-sessions: set_handoff, get_handoff, capture_baseline_dirty_files
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.sync.tasks import TaskBackupError
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit


# ─── Shared mock fixtures ───


@pytest.fixture
def mock_memory_manager():
    manager = MagicMock()
    manager.config = MagicMock()
    manager.config.enabled = True
    manager.db = MagicMock()
    return manager


@pytest.fixture
def mock_memory_backup_manager():
    manager = MagicMock()
    manager.restore = AsyncMock(return_value=5)
    manager.backup = AsyncMock(return_value=3)
    return manager


@pytest.fixture
def mock_session_manager():
    manager = MagicMock()
    session = MagicMock()
    session.project_id = "11111111-1111-4111-8111-111111110123"
    session.transcript_path = "/tmp/test.jsonl"
    session.handoff_markdown = None
    manager.get = MagicMock(return_value=session)
    return manager


@pytest.fixture
def mock_llm_service():
    return MagicMock()


@pytest.fixture
def mock_transcript_processor():
    return MagicMock()


@pytest.fixture
def mock_task_manager():
    manager = MagicMock()
    manager.db = MagicMock()
    return manager


# ─── Registry fixtures ───


@pytest.fixture
def memory_registry(
    mock_memory_manager,
    mock_memory_backup_manager,
    mock_session_manager,
    mock_llm_service,
):
    from gobby.mcp_proxy.tools.memory import create_memory_registry

    return create_memory_registry(
        memory_manager_resolver=lambda: mock_memory_manager,
        llm_service_resolver=lambda: mock_llm_service,
        memory_backup_manager_resolver=lambda: mock_memory_backup_manager,
        session_manager=mock_session_manager,
    )


@pytest.fixture
def task_backup_registry(mock_task_manager):
    from gobby.mcp_proxy.tools.tasks._backup import create_backup_registry

    ctx = MagicMock()
    ctx.task_manager = mock_task_manager
    ctx.get_current_project_id.return_value = "11111111-1111-4111-8111-111111110123"
    return create_backup_registry(ctx)


@pytest.fixture
def session_registry(
    mock_session_manager,
    mock_llm_service,
    mock_transcript_processor,
):
    from gobby.mcp_proxy.tools.sessions import create_session_messages_registry

    return create_session_messages_registry(
        session_manager=mock_session_manager,
        llm_service_resolver=lambda: mock_llm_service,
        transcript_processor=mock_transcript_processor,
    )


# ═══════════════════════════════════════════════════════════════════════
# gobby-memory: restore_memories
# ═══════════════════════════════════════════════════════════════════════


class TestMemoryRestore:
    def test_tool_registered(self, memory_registry) -> None:
        assert "restore_memories" in memory_registry._tools

    @pytest.mark.asyncio
    async def test_calls_backup_manager(self, memory_registry, mock_memory_backup_manager) -> None:
        result = await memory_registry.call("restore_memories", {})
        assert result["success"] is True
        assert result["restored"] == 5
        mock_memory_backup_manager.restore.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_error_when_no_backup_manager(self, mock_memory_manager) -> None:
        from gobby.mcp_proxy.tools.memory import create_memory_registry

        registry = create_memory_registry(
            lambda: mock_memory_manager, memory_backup_manager_resolver=None
        )
        result = await registry.call("restore_memories", {})
        assert result["success"] is False
        assert "error" in result


# ═══════════════════════════════════════════════════════════════════════
# gobby-memory: backup_memories
# ═══════════════════════════════════════════════════════════════════════


class TestMemoryBackup:
    def test_tool_registered(self, memory_registry) -> None:
        assert "backup_memories" in memory_registry._tools

    @pytest.mark.asyncio
    async def test_calls_backup_manager(self, memory_registry, mock_memory_backup_manager) -> None:
        result = await memory_registry.call("backup_memories", {})
        assert result["success"] is True
        assert result["backed_up"] == 3
        mock_memory_backup_manager.backup.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════
# gobby-tasks: backup_tasks / restore_tasks
# ═══════════════════════════════════════════════════════════════════════


class TestTaskBackupRestore:
    @pytest.mark.asyncio
    @patch("gobby.mcp_proxy.tools.tasks._backup.TaskBackupManager")
    async def test_backup_and_restore_are_registered_and_callable(
        self, manager_cls: MagicMock, task_backup_registry
    ) -> None:
        manager_cls.return_value.backup.return_value = 4
        manager_cls.return_value.restore.return_value = 2

        backup_result = await task_backup_registry.call("backup_tasks", {})
        restore_result = await task_backup_registry.call("restore_tasks", {})

        assert backup_result == {"success": True, "backed_up": 4}
        assert restore_result == {"success": True, "restored": 2}

    @pytest.mark.asyncio
    @pytest.mark.unit
    @patch("gobby.mcp_proxy.tools.tasks._backup.TaskBackupManager")
    async def test_backup_returns_structured_failure(
        self,
        manager_cls: MagicMock,
        task_backup_registry: InternalToolRegistry,
    ) -> None:
        manager_cls.return_value.backup.side_effect = TaskBackupError("backup unavailable")

        result = await task_backup_registry.call("backup_tasks", {})

        assert result == {"success": False, "error": "backup unavailable"}


# ═══════════════════════════════════════════════════════════════════════
# gobby-sessions: set_handoff (replaced generate_handoff + extract_handoff_context)
# ═══════════════════════════════════════════════════════════════════════


class TestSessionSetHandoffContext:
    """Verify set_handoff is registered on gobby-sessions and callable."""

    def test_tool_registered(self, session_registry) -> None:
        assert "set_handoff" in session_registry._tools

    @pytest.mark.asyncio
    async def test_agent_authored_path(self, session_registry) -> None:
        with session_context_for_test("sess-1"):
            result = await session_registry.call(
                "set_handoff",
                {"content": "## Test handoff"},
            )
        assert result["success"] is True
        assert result["mode"] == "agent_authored"


# ═══════════════════════════════════════════════════════════════════════
# gobby-sessions: capture_baseline_dirty_files
# ═══════════════════════════════════════════════════════════════════════


class TestSessionCaptureBaselineDirtyFiles:
    """Verify capture_baseline_dirty_files is registered on gobby-sessions."""

    def test_tool_registered(self, session_registry) -> None:
        assert "capture_baseline_dirty_files" in session_registry._tools

    @pytest.mark.asyncio
    async def test_returns_dirty_files(self, session_registry) -> None:
        with patch(
            "gobby.mcp_proxy.tools.sessions._actions.get_dirty_files",
        ) as mock_fn:
            mock_fn.return_value = {"file1.py", "file2.py"}
            result = await session_registry.call(
                "capture_baseline_dirty_files",
                {"project_path": "/tmp/project"},
            )
            assert result["success"] is True
            assert result["file_count"] == 2
            mock_fn.assert_called_once_with("/tmp/project")
