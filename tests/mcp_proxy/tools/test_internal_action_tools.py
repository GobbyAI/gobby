"""Tests for internal action MCP tools.

Verifies that workflow action functions are exposed as MCP tools:
- gobby-memory: sync_import, sync_export
- gobby-tasks: sync_import, sync_export
- gobby-sessions: set_handoff_context, get_handoff_context, capture_baseline_dirty_files
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
def mock_memory_sync_manager():
    manager = MagicMock()
    manager.import_from_files = AsyncMock(return_value=5)
    manager.export_to_files = AsyncMock(return_value=3)
    return manager


@pytest.fixture
def mock_session_manager():
    manager = MagicMock()
    session = MagicMock()
    session.project_id = "proj-123"
    session.transcript_path = "/tmp/test.jsonl"
    session.digest_markdown = None
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


@pytest.fixture
def mock_task_sync_manager():
    manager = MagicMock()
    manager.import_from_jsonl = MagicMock()
    manager.export_to_jsonl = MagicMock()
    return manager


# ─── Registry fixtures ───


@pytest.fixture
def memory_registry(
    mock_memory_manager,
    mock_memory_sync_manager,
    mock_session_manager,
    mock_llm_service,
):
    from gobby.mcp_proxy.tools.memory import create_memory_registry

    return create_memory_registry(
        memory_manager=mock_memory_manager,
        llm_service=mock_llm_service,
        memory_sync_manager=mock_memory_sync_manager,
        session_manager=mock_session_manager,
    )


@pytest.fixture
def task_sync_registry(
    mock_task_manager,
    mock_task_sync_manager,
    mock_session_manager,
):
    from gobby.mcp_proxy.tools.task_sync import create_commit_registry

    return create_commit_registry(
        sync_manager=mock_task_sync_manager,
        task_manager=mock_task_manager,
        session_manager=mock_session_manager,
    )


@pytest.fixture
def session_registry(
    mock_session_manager,
    mock_llm_service,
    mock_transcript_processor,
):
    from gobby.mcp_proxy.tools.sessions import create_session_messages_registry

    return create_session_messages_registry(
        session_manager=mock_session_manager,
        llm_service=mock_llm_service,
        transcript_processor=mock_transcript_processor,
    )


# ═══════════════════════════════════════════════════════════════════════
# gobby-memory: sync_import
# ═══════════════════════════════════════════════════════════════════════


class TestMemorySyncImport:
    """Verify sync_import is registered on gobby-memory and callable."""

    def test_tool_registered(self, memory_registry) -> None:
        assert "sync_import" in memory_registry._tools

    @pytest.mark.asyncio
    async def test_calls_sync_manager(self, memory_registry, mock_memory_sync_manager) -> None:
        result = await memory_registry.call("sync_import", {})
        assert result["success"] is True
        assert result["imported"] == 5
        mock_memory_sync_manager.import_from_files.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_error_when_no_sync_manager(self, mock_memory_manager) -> None:
        from gobby.mcp_proxy.tools.memory import create_memory_registry

        registry = create_memory_registry(mock_memory_manager, memory_sync_manager=None)
        result = await registry.call("sync_import", {})
        assert result["success"] is False
        assert "error" in result


# ═══════════════════════════════════════════════════════════════════════
# gobby-memory: sync_export
# ═══════════════════════════════════════════════════════════════════════


class TestMemorySyncExport:
    """Verify sync_export is registered on gobby-memory and callable."""

    def test_tool_registered(self, memory_registry) -> None:
        assert "sync_export" in memory_registry._tools

    @pytest.mark.asyncio
    async def test_calls_sync_manager(self, memory_registry, mock_memory_sync_manager) -> None:
        result = await memory_registry.call("sync_export", {})
        assert result["success"] is True
        assert result["exported"] == 3
        mock_memory_sync_manager.export_to_files.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════
# gobby-tasks: sync_import
# ═══════════════════════════════════════════════════════════════════════


# NOTE: TestTaskSyncImport and TestTaskSyncExport removed — sync tools are now CLI-only.


# ═══════════════════════════════════════════════════════════════════════
# gobby-sessions: set_handoff_context (replaced generate_handoff + extract_handoff_context)
# ═══════════════════════════════════════════════════════════════════════


class TestSessionSetHandoffContext:
    """Verify set_handoff_context is registered on gobby-sessions and callable."""

    def test_tool_registered(self, session_registry) -> None:
        assert "set_handoff_context" in session_registry._tools

    @pytest.mark.asyncio
    async def test_agent_authored_path(self, session_registry) -> None:
        with session_context_for_test("sess-1"):
            result = await session_registry.call(
                "set_handoff_context",
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
