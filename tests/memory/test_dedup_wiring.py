"""Tests for DedupService wiring into MemoryManager."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.memory.manager import MemoryManager

pytestmark = pytest.mark.unit


def _make_config(**overrides):
    """Create a mock MemoryConfig."""
    config = MagicMock()
    config.enabled = True
    config.neo4j_url = None
    config.auto_crossref = False
    for k, v in overrides.items():
        setattr(config, k, v)
    return config


def _make_manager(
    has_llm: bool = False,
    has_vector_store: bool = False,
    has_embed_fn: bool = False,
):
    """Create a MemoryManager with optional mocked dependencies."""
    db = MagicMock()
    config = _make_config()
    llm_service = MagicMock() if has_llm else None

    if has_llm:
        provider = MagicMock()
        provider.generate_json = AsyncMock(return_value={"facts": []})
        llm_service.get_default_provider.return_value = provider

    vector_store = MagicMock() if has_vector_store else None
    if vector_store:
        vector_store.search = AsyncMock(return_value=[])
        vector_store.upsert = AsyncMock()
        vector_store.delete = AsyncMock()

    embed_fn = AsyncMock(return_value=[0.1] * 1536) if has_embed_fn else None

    manager = MemoryManager(
        db=db,
        config=config,
        llm_service=llm_service,
        vector_store=vector_store,
        embed_fn=embed_fn,
    )

    return manager


class TestDedupServiceInitialization:
    """Tests for DedupService initialization in MemoryManager."""

    def test_dedup_service_created_when_llm_available(self) -> None:
        """DedupService is initialized when LLM, VectorStore, and embed_fn are all available."""
        manager = _make_manager(has_llm=True, has_vector_store=True, has_embed_fn=True)
        assert manager._dedup_service is not None

    def test_no_dedup_service_without_llm(self) -> None:
        """DedupService is None when no LLM service."""
        manager = _make_manager(has_llm=False, has_vector_store=True, has_embed_fn=True)
        assert manager._dedup_service is None

    def test_no_dedup_service_without_vector_store(self) -> None:
        """DedupService is None when no VectorStore."""
        manager = _make_manager(has_llm=True, has_vector_store=False, has_embed_fn=True)
        assert manager._dedup_service is None

    def test_no_dedup_service_without_embed_fn(self) -> None:
        """DedupService is None when no embed_fn."""
        manager = _make_manager(has_llm=True, has_vector_store=True, has_embed_fn=False)
        assert manager._dedup_service is None


class TestBackgroundDedupTask:
    """Tests for fire-and-forget dedup task in create_memory."""

    @pytest.mark.asyncio
    async def test_create_memory_fires_background_dedup(self) -> None:
        """create_memory fires a background dedup task when DedupService is available."""
        manager = _make_manager(has_llm=True, has_vector_store=True, has_embed_fn=True)

        # Mock backend to return a record
        mock_record = MagicMock()
        mock_record.id = "mem-1"
        mock_record.content = "Test content"
        mock_record.memory_type = "fact"
        mock_record.created_at = None
        mock_record.updated_at = None
        mock_record.project_id = None
        mock_record.source_type = "user"
        mock_record.source_session_id = None

        mock_record.access_count = 0
        mock_record.last_accessed_at = None
        mock_record.tags = []
        manager._backend.content_exists = AsyncMock(return_value=False)
        manager._backend.create = AsyncMock(return_value=mock_record)

        # Mock dedup service
        manager._dedup_service.process = AsyncMock()

        memory = await manager.create_memory(content="Test content")

        assert memory.id == "mem-1"

        # Wait for background tasks to complete
        if manager._background_tasks:
            await asyncio.wait(manager._background_tasks, timeout=1.0)

        # Verify dedup was fired
        manager._dedup_service.process.assert_called_once()

    @pytest.mark.asyncio
    async def test_background_task_tracked_and_cleaned(self) -> None:
        """Background dedup tasks are tracked in _background_tasks and auto-cleaned."""
        manager = _make_manager(has_llm=True, has_vector_store=True, has_embed_fn=True)

        mock_record = MagicMock()
        mock_record.id = "mem-2"
        mock_record.content = "Content"
        mock_record.memory_type = "fact"
        mock_record.created_at = None
        mock_record.updated_at = None
        mock_record.project_id = None
        mock_record.source_type = "user"
        mock_record.source_session_id = None

        mock_record.access_count = 0
        mock_record.last_accessed_at = None
        mock_record.tags = []
        manager._backend.content_exists = AsyncMock(return_value=False)
        manager._backend.create = AsyncMock(return_value=mock_record)
        manager._dedup_service.process = AsyncMock()

        await manager.create_memory(content="Content")

        # Task should be tracked initially
        assert len(manager._background_tasks) >= 1

        # Wait for completion and cleanup
        if manager._background_tasks:
            await asyncio.wait(manager._background_tasks, timeout=1.0)

        # Task should be cleaned up after completion
        assert len(manager._background_tasks) == 0

    @pytest.mark.asyncio
    async def test_no_dedup_task_without_service(self) -> None:
        """No background task fires when DedupService is not available."""
        manager = _make_manager(has_llm=False, has_vector_store=False)

        mock_record = MagicMock()
        mock_record.id = "mem-3"
        mock_record.content = "Content"
        mock_record.memory_type = "fact"
        mock_record.created_at = None
        mock_record.updated_at = None
        mock_record.project_id = None
        mock_record.source_type = "user"
        mock_record.source_session_id = None

        mock_record.access_count = 0
        mock_record.last_accessed_at = None
        mock_record.tags = []
        manager._backend.content_exists = AsyncMock(return_value=False)
        manager._backend.create = AsyncMock(return_value=mock_record)

        await manager.create_memory(content="Content")

        # No background tasks should be created
        assert len(manager._background_tasks) == 0

    @pytest.mark.asyncio
    async def test_dedup_failure_doesnt_fail_caller(self) -> None:
        """Dedup failure is logged but doesn't affect the create_memory return."""
        manager = _make_manager(has_llm=True, has_vector_store=True, has_embed_fn=True)

        mock_record = MagicMock()
        mock_record.id = "mem-4"
        mock_record.content = "Content"
        mock_record.memory_type = "fact"
        mock_record.created_at = None
        mock_record.updated_at = None
        mock_record.project_id = None
        mock_record.source_type = "user"
        mock_record.source_session_id = None

        mock_record.access_count = 0
        mock_record.last_accessed_at = None
        mock_record.tags = []
        manager._backend.content_exists = AsyncMock(return_value=False)
        manager._backend.create = AsyncMock(return_value=mock_record)

        # Make dedup fail
        manager._dedup_service.process = AsyncMock(side_effect=Exception("LLM crash"))

        # create_memory should still succeed
        memory = await manager.create_memory(content="Content")
        assert memory.id == "mem-4"

        # Wait for background task to complete (with error)
        await asyncio.sleep(0.1)

        # Task should still be cleaned up
        assert len(manager._background_tasks) == 0

    @pytest.mark.asyncio
    async def test_create_memory_returns_immediately(self) -> None:
        """create_memory returns the memory immediately, before dedup completes."""
        manager = _make_manager(has_llm=True, has_vector_store=True, has_embed_fn=True)

        mock_record = MagicMock()
        mock_record.id = "mem-5"
        mock_record.content = "Immediate"
        mock_record.memory_type = "fact"
        mock_record.created_at = None
        mock_record.updated_at = None
        mock_record.project_id = None
        mock_record.source_type = "user"
        mock_record.source_session_id = None

        mock_record.access_count = 0
        mock_record.last_accessed_at = None
        mock_record.tags = []
        manager._backend.content_exists = AsyncMock(return_value=False)
        manager._backend.create = AsyncMock(return_value=mock_record)

        # Make dedup slow
        async def slow_dedup(*args, **kwargs):
            await asyncio.sleep(5)

        manager._dedup_service.process = AsyncMock(side_effect=slow_dedup)

        # Should return immediately, not wait for dedup
        memory = await asyncio.wait_for(
            manager.create_memory(content="Immediate"),
            timeout=1.0,
        )
        assert memory.id == "mem-5"
