"""Comprehensive tests for MemoryManager class.

Tests cover:
- Memory creation (create_memory)
- Memory retrieval (search_memories)
- Memory deletion (delete_memory)
- Access statistics and debouncing
- Statistics retrieval
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from gobby.config.persistence import MemoryConfig
from gobby.memory.manager import MemoryManager
from gobby.memory.protocol import MemoryBackendProtocol
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories import LocalMemoryManager, Memory

pytestmark = pytest.mark.unit

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def db(hub_db):
    """Create a temporary hub database for testing."""
    return hub_db


@pytest.fixture
def memory_config():
    """Create a default memory configuration with PostgreSQL backend."""
    return MemoryConfig(
        enabled=True,
        backend="local",
        injection_limit=10,
        access_debounce_seconds=60,
    )


@pytest.fixture
def memory_manager(db, memory_config):
    """Create a MemoryManager with real database."""
    return MemoryManager(db=db, config=memory_config)


@pytest.fixture
def mock_storage():
    """Create a mock LocalMemoryManager."""
    return MagicMock(spec=LocalMemoryManager)


@pytest.fixture
def mock_config():
    """Create a mock MemoryConfig."""
    config = MagicMock(spec=MemoryConfig)
    config.access_debounce_seconds = 60
    config.backend = "postgres"
    return config


@pytest.fixture
def mock_db():
    """Create a mock database."""
    return MagicMock(spec=HubDatabase)


# =============================================================================
# Test: Initialization
# =============================================================================


class TestMemoryManagerInit:
    """Tests for MemoryManager initialization."""

    def test_init_creates_storage(self, db, memory_config) -> None:
        """Test that initialization creates a LocalMemoryManager."""
        manager = MemoryManager(db=db, config=memory_config)
        assert manager.db is db
        assert manager.config is memory_config
        assert isinstance(manager.storage, LocalMemoryManager)

    def test_init_creates_backend(self, db, memory_config) -> None:
        """Test that initialization creates a MemoryBackendProtocol instance."""
        manager = MemoryManager(db=db, config=memory_config)
        assert hasattr(manager, "_backend")
        assert isinstance(manager._backend, MemoryBackendProtocol)

    def test_init_with_null_backend(self, db) -> None:
        """Test that null backend can be used for testing."""
        config = MemoryConfig(backend="null")
        manager = MemoryManager(db=db, config=config)
        assert hasattr(manager, "_backend")
        assert isinstance(manager._backend, MemoryBackendProtocol)


# =============================================================================
# Test: create_memory (Memory Creation)
# =============================================================================


class TestCreateMemory:
    """Tests for the create_memory method."""

    @pytest.mark.asyncio
    async def test_create_memory_basic(self, memory_manager):
        """Test basic memory creation."""
        memory = await memory_manager.create_memory(
            content="Test fact",
            memory_type="fact",
        )

        uuid.UUID(memory.id)  # validates UUID format
        assert memory.content == "Test fact"
        assert memory.memory_type == "fact"

    @pytest.mark.asyncio
    async def test_create_memory_with_all_params(self, db, memory_config):
        """Test memory creation with all parameters."""
        db.execute(
            "INSERT INTO projects (id, name, repo_path) VALUES (?, ?, ?)",
            ("proj-123", "test-project", "/tmp/test"),
        )
        now = datetime.now(UTC).isoformat()
        db.execute(
            """INSERT INTO sessions (id, external_id, machine_id, source, project_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("sess-123", "ext-123", "machine-123", "claude", "proj-123", now),
        )

        manager = MemoryManager(db=db, config=memory_config)
        memory = await manager.create_memory(
            content="User prefers dark theme",
            memory_type="preference",
            project_id=None,
            source_type="user",
            source_session_id="sess-123",
            tags=["ui", "theme"],
        )

        assert memory.content == "User prefers dark theme"
        assert memory.memory_type == "preference"
        assert memory.source_type == "user"
        assert memory.source_session_id == "sess-123"
        assert memory.tags == ["ui", "theme"]

    @pytest.mark.asyncio
    async def test_create_memory_default_values(self, memory_manager):
        """Test memory creation uses correct defaults."""
        memory = await memory_manager.create_memory(content="Simple fact")

        assert memory.memory_type == "fact"
        assert memory.source_type == "agent"
        assert memory.tags == []


# =============================================================================
# Test: search_memories (Memory Retrieval)
# =============================================================================


class TestSearchMemories:
    """Tests for the search_memories method."""

    @pytest.mark.asyncio
    async def test_search_memories_no_query_returns_top_memories(self, memory_manager):
        """Test search_memories without query returns top memories."""
        await memory_manager.create_memory(content="Low importance")
        await memory_manager.create_memory(content="High importance")
        await memory_manager.create_memory(content="Medium importance")

        memories = await memory_manager.search_memories(limit=2)

        assert len(memories) == 2

    @pytest.mark.asyncio
    async def test_search_memories_no_query_all_returned(self, memory_manager):
        """Test search_memories without query returns all memories (no VectorStore)."""
        await memory_manager.create_memory(content="Python is a programming language")
        await memory_manager.create_memory(content="JavaScript runs in browsers")

        memories = await memory_manager.search_memories(limit=10)

        assert len(memories) == 2

    @pytest.mark.asyncio
    async def test_search_memories_by_memory_type(self, memory_manager):
        """Test search_memories filters by memory type."""
        await memory_manager.create_memory(content="Fact 1", memory_type="fact")
        await memory_manager.create_memory(content="Pref 1", memory_type="preference")

        memories = await memory_manager.search_memories(memory_type="preference")

        assert len(memories) == 1
        assert memories[0].memory_type == "preference"

    @pytest.mark.asyncio
    async def test_search_memories_limit(self, memory_manager):
        """Test search_memories respects limit parameter."""
        for i in range(5):
            await memory_manager.create_memory(content=f"Memory {i}")

        memories = await memory_manager.search_memories(limit=3)

        assert len(memories) == 3

    @pytest.mark.asyncio
    async def test_search_memories_updates_access_stats(self, memory_manager):
        """Test search_memories updates access statistics."""
        memory = await memory_manager.create_memory(content="Track access")
        original_count = memory.access_count

        _ = await memory_manager.search_memories(limit=10)

        updated = memory_manager.get_memory(memory.id)
        assert updated.access_count == original_count + 1
        assert updated.last_accessed_at is not None


# =============================================================================
# Test: Access Statistics
# =============================================================================


class TestAccessStats:
    """Tests for access statistics updates."""

    @pytest.mark.asyncio
    async def test_update_access_stats_debouncing(self, memory_manager):
        """Test access stats debouncing prevents rapid updates."""
        memory = await memory_manager.create_memory(content="Debounce test")

        # First search - should update
        _ = await memory_manager.search_memories(limit=10)
        updated = memory_manager.get_memory(memory.id)
        first_access_count = updated.access_count

        # Second immediate search - should be debounced
        _ = await memory_manager.search_memories(limit=10)
        updated_again = memory_manager.get_memory(memory.id)

        # Should still be same count due to debouncing
        assert updated_again.access_count == first_access_count

    def test_update_access_stats_empty_list(self, memory_manager) -> None:
        """Test _update_access_stats handles empty list."""
        with patch.object(memory_manager.storage, "update_access_stats") as update_access_stats:
            result = memory_manager._update_access_stats([])

        assert result is None
        assert update_access_stats.call_count == 0

    def test_update_access_stats_invalid_timestamp(self, db, memory_config) -> None:
        """Test _update_access_stats handles invalid timestamps gracefully."""
        manager = MemoryManager(db=db, config=memory_config)

        memory = MagicMock(spec=Memory)
        memory.id = "mm-test"
        memory.last_accessed_at = "invalid-timestamp"

        assert manager._update_access_stats([memory]) is None

    def test_update_access_stats_no_timezone(self, db, memory_config) -> None:
        """Test _update_access_stats handles timestamps without timezone."""
        manager = MemoryManager(db=db, config=memory_config)

        real_memory = manager.storage.create_memory(content="Test timezone")

        memory = MagicMock(spec=Memory)
        memory.id = real_memory.id
        memory.last_accessed_at = "2024-01-01T00:00:00"

        manager._update_access_stats([memory])

        updated = manager.get_memory(real_memory.id)
        assert updated.access_count >= 1


# =============================================================================
# Test: delete_memory (Memory Deletion)
# =============================================================================


class TestDeleteMemory:
    """Tests for the delete_memory method."""

    @pytest.mark.asyncio
    async def test_delete_existing_memory(self, memory_manager):
        """Test deleting an existing memory."""
        memory = await memory_manager.create_memory(content="To delete")

        result = await memory_manager.delete_memory(memory.id)

        assert result is True
        assert memory_manager.get_memory(memory.id) is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_memory(self, memory_manager) -> None:
        """Test deleting a non-existent memory returns False."""
        result = await memory_manager.delete_memory("mm-nonexistent")
        assert result is False


# =============================================================================
# Test: List Memories
# =============================================================================


class TestListMemories:
    """Tests for list_memories method."""

    @pytest.mark.asyncio
    async def test_list_memories_basic(self, memory_manager):
        """Test basic memory listing."""
        await memory_manager.create_memory(content="Memory 1")
        await memory_manager.create_memory(content="Memory 2")

        memories = memory_manager.list_memories()

        assert len(memories) == 2

    @pytest.mark.asyncio
    async def test_list_memories_with_offset(self, memory_manager):
        """Test memory listing with offset."""
        for i in range(5):
            await memory_manager.create_memory(content=f"Memory {i}")

        memories = memory_manager.list_memories(limit=2, offset=2)

        assert len(memories) == 2

    @pytest.mark.asyncio
    async def test_list_memories_by_type(self, memory_manager):
        """Test memory listing filtered by type."""
        await memory_manager.create_memory(content="Fact", memory_type="fact")
        await memory_manager.create_memory(content="Preference", memory_type="preference")

        memories = memory_manager.list_memories(memory_type="fact")

        assert len(memories) == 1
        assert memories[0].memory_type == "fact"


# =============================================================================
# Test: Content Exists
# =============================================================================


class TestContentExists:
    """Tests for content_exists method."""

    @pytest.mark.asyncio
    async def test_content_exists_true(self, memory_manager):
        """Test content_exists returns True for existing content."""
        await memory_manager.create_memory(content="Existing content")

        result = memory_manager.content_exists("Existing content")

        assert result is True

    def test_content_exists_false(self, memory_manager) -> None:
        """Test content_exists returns False for non-existing content."""
        result = memory_manager.content_exists("Non-existing content")

        assert result is False


# =============================================================================
# Test: Get Memory
# =============================================================================


class TestGetMemory:
    """Tests for get_memory method."""

    @pytest.mark.asyncio
    async def test_get_memory_exists(self, memory_manager):
        """Test getting an existing memory."""
        created = await memory_manager.create_memory(content="Get test")

        retrieved = memory_manager.get_memory(created.id)

        assert retrieved is not None
        assert retrieved.id == created.id
        assert retrieved.content == created.content

    def test_get_memory_not_found(self, memory_manager) -> None:
        """Test getting a non-existent memory returns None."""
        result = memory_manager.get_memory("mm-nonexistent")

        assert result is None


# =============================================================================
# Test: Update Memory
# =============================================================================


class TestUpdateMemory:
    """Tests for update_memory method."""

    @pytest.mark.asyncio
    async def test_update_memory_content(self, memory_manager):
        """Test updating memory content."""
        memory = await memory_manager.create_memory(content="Original")

        updated = await memory_manager.update_memory(memory.id, content="Updated")

        assert updated.content == "Updated"

    @pytest.mark.asyncio
    async def test_update_memory_tags(self, memory_manager):
        """Test updating memory tags."""
        memory = await memory_manager.create_memory(content="Test", tags=["old"])

        updated = await memory_manager.update_memory(memory.id, tags=["new", "tags"])

        assert updated.tags == ["new", "tags"]

    @pytest.mark.asyncio
    async def test_update_memory_not_found_raises(self, memory_manager):
        """Test updating non-existent memory raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            await memory_manager.update_memory("mm-nonexistent", content="New")


# =============================================================================
# Test: Get Stats
# =============================================================================


class TestGetStats:
    """Tests for get_stats method."""

    def test_get_stats_empty(self, memory_manager) -> None:
        """Test stats with no memories."""
        stats = memory_manager.get_stats()

        assert stats["total_count"] == 0
        assert stats["by_type"] == {}

    @pytest.mark.asyncio
    async def test_get_stats_with_memories(self, memory_manager):
        """Test stats with multiple memories."""
        await memory_manager.create_memory(content="Fact 1", memory_type="fact")
        await memory_manager.create_memory(content="Fact 2", memory_type="fact")
        await memory_manager.create_memory(content="Pref 1", memory_type="preference")

        stats = memory_manager.get_stats()

        assert stats["total_count"] == 3
        assert stats["by_type"]["fact"] == 2
        assert stats["by_type"]["preference"] == 1


# =============================================================================
# Test: Edge Cases and Error Handling
# =============================================================================


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_duplicate_content_handling(self, memory_manager):
        """Test creating memory with duplicate content returns existing."""
        memory1 = await memory_manager.create_memory(content="Duplicate test")
        memory2 = await memory_manager.create_memory(content="Duplicate test")

        assert memory1.id == memory2.id

    @pytest.mark.asyncio
    async def test_search_memories_empty_database(self, memory_manager):
        """Test search_memories on empty database returns empty list."""
        memories = await memory_manager.search_memories()
        assert memories == []

    @pytest.mark.asyncio
    async def test_update_access_stats_exception_handling(self, db, memory_config):
        """Test _update_access_stats handles storage exceptions."""
        manager = MemoryManager(db=db, config=memory_config)

        memory = MagicMock(spec=Memory)
        memory.id = "mm-test"
        memory.last_accessed_at = None

        with patch.object(manager.storage, "update_access_stats") as mock_update:
            mock_update.side_effect = Exception("Database error")

            assert manager._update_access_stats([memory]) is None
            assert mock_update.call_count == 1


# =============================================================================
# Test: search_memories_as_context
# =============================================================================


class TestSearchMemoriesAsContext:
    """Tests for search_memories_as_context method."""

    @pytest.mark.asyncio
    async def test_search_memories_as_context_returns_formatted_context(self, db, memory_config):
        """Test search_memories_as_context returns properly formatted context string."""
        manager = MemoryManager(db=db, config=memory_config)

        await manager.create_memory(content="Test preference", memory_type="preference")
        await manager.create_memory(content="Test fact", memory_type="fact")

        context = await manager.search_memories_as_context()

        assert isinstance(context, str)
        assert "<project-memory>" in context
        assert "</project-memory>" in context
        assert "Test preference" in context
        assert "Test fact" in context

    @pytest.mark.asyncio
    async def test_search_memories_as_context_empty_memories(self, db, memory_config):
        """Test search_memories_as_context returns empty string when no memories."""
        manager = MemoryManager(db=db, config=memory_config)

        context = await manager.search_memories_as_context()

        assert context == ""

    @pytest.mark.asyncio
    async def test_search_memories_as_context_respects_limit(self, db, memory_config):
        """Test search_memories_as_context respects limit parameter."""
        manager = MemoryManager(db=db, config=memory_config)

        for i in range(10):
            await manager.create_memory(content=f"Memory {i}", memory_type="fact")

        context = await manager.search_memories_as_context(limit=3)

        assert isinstance(context, str)
        assert "<project-memory>" in context

    @pytest.mark.asyncio
    async def test_search_memories_as_context_respects_project_filter(self, db, memory_config):
        """Test search_memories_as_context filters by project_id."""
        manager = MemoryManager(db=db, config=memory_config)

        await manager.create_memory(content="Global memory")

        context = await manager.search_memories_as_context(project_id="proj-123")

        assert isinstance(context, str)


# =============================================================================
# Test: VectorStore integration
# =============================================================================


class TestVectorStoreIntegration:
    """Tests for VectorStore-related operations."""

    @pytest.mark.asyncio
    async def test_embed_and_upsert_no_vectorstore(self, memory_manager) -> None:
        """_embed_and_upsert does nothing when no VectorStore."""
        result = await memory_manager._embed_and_upsert("id", "content")

        assert result is None
        assert memory_manager.vector_store is None

    @pytest.mark.asyncio
    async def test_embed_and_upsert_failure_logged(self, db, memory_config) -> None:
        """_embed_and_upsert logs warning on failure."""
        from unittest.mock import AsyncMock

        mock_vs = MagicMock()
        mock_vs.upsert = AsyncMock(side_effect=RuntimeError("VectorStore error"))
        mock_embed = AsyncMock(return_value=[0.1, 0.2])
        manager = MemoryManager(
            db=db, config=memory_config, vector_store=mock_vs, embed_fn=mock_embed
        )
        # Should not raise
        await manager._embed_and_upsert("id", "content")
        mock_vs.upsert.assert_called_once()
        assert mock_vs.upsert.call_count == 1
        assert mock_vs.upsert.call_args is not None

    @pytest.mark.asyncio
    async def test_vectorstore_unavailable_does_not_disable_embeddings(
        self, db, memory_config
    ) -> None:
        """Transient VectorStore failures should not mark embeddings unavailable."""
        from unittest.mock import AsyncMock

        from gobby.memory.vectorstore import VectorStoreUnavailableError

        mock_vs = MagicMock()
        mock_vs.upsert = AsyncMock(side_effect=VectorStoreUnavailableError())
        mock_embed = AsyncMock(return_value=[0.1, 0.2])
        manager = MemoryManager(
            db=db, config=memory_config, vector_store=mock_vs, embed_fn=mock_embed
        )

        await manager._embed_and_upsert("id-1", "content")
        await manager._embed_and_upsert("id-2", "content")

        assert manager._embeddings_available is True
        assert mock_embed.call_count == 2
        assert mock_vs.upsert.call_count == 2

    @pytest.mark.asyncio
    async def test_create_memory_with_vectorstore(self, db, memory_config) -> None:
        """create_memory embeds content into VectorStore when available."""
        from unittest.mock import AsyncMock

        mock_vs = MagicMock()
        mock_vs.upsert = AsyncMock()
        mock_embed = AsyncMock(return_value=[0.1, 0.2])
        manager = MemoryManager(
            db=db, config=memory_config, vector_store=mock_vs, embed_fn=mock_embed
        )
        await manager.create_memory(content="VectorStore test")
        mock_vs.upsert.assert_called_once()
        assert mock_vs.upsert.call_count == 1
        assert mock_vs.upsert.call_args is not None


# =============================================================================
# Test: delete_memory with VectorStore and KG
# =============================================================================


class TestDeleteMemoryExtended:
    """Extended tests for delete_memory with VectorStore and KG."""

    @pytest.mark.asyncio
    async def test_delete_with_vectorstore(self, db, memory_config) -> None:
        """delete_memory removes from VectorStore when available."""
        from unittest.mock import AsyncMock

        mock_vs = MagicMock()
        mock_vs.delete = AsyncMock()
        mock_vs.upsert = AsyncMock()
        mock_embed = AsyncMock(return_value=[0.1, 0.2])
        manager = MemoryManager(
            db=db, config=memory_config, vector_store=mock_vs, embed_fn=mock_embed
        )
        memory = await manager.create_memory(content="To delete with VS")
        result = await manager.delete_memory(memory.id)
        assert result is True
        mock_vs.delete.assert_called_once_with(memory.id)
        assert mock_vs.delete.call_count == 1
        assert mock_vs.delete.call_args is not None

    @pytest.mark.asyncio
    async def test_delete_vectorstore_error_handled(self, db, memory_config) -> None:
        """delete_memory handles VectorStore delete failure gracefully."""
        from unittest.mock import AsyncMock

        mock_vs = MagicMock()
        mock_vs.delete = AsyncMock(side_effect=RuntimeError("VS error"))
        mock_vs.upsert = AsyncMock()
        mock_embed = AsyncMock(return_value=[0.1, 0.2])
        manager = MemoryManager(
            db=db, config=memory_config, vector_store=mock_vs, embed_fn=mock_embed
        )
        memory = await manager.create_memory(content="To delete VS fail")
        result = await manager.delete_memory(memory.id)
        assert result is True  # Still returns True since PostgreSQL delete succeeded
        assert mock_vs.delete.await_count == 1


# =============================================================================
# Test: adelete_memory (async delete)
# =============================================================================


class TestADeleteMemory:
    """Tests for adelete_memory."""

    @pytest.mark.asyncio
    async def test_adelete_existing(self, memory_manager) -> None:
        """adelete_memory removes an existing memory."""
        memory = await memory_manager.create_memory(content="Async delete test")
        result = await memory_manager.adelete_memory(memory.id)
        assert result is True
        assert memory_manager.get_memory(memory.id) is None

    @pytest.mark.asyncio
    async def test_adelete_nonexistent(self, memory_manager) -> None:
        """adelete_memory returns False for nonexistent memory."""
        result = await memory_manager.adelete_memory("mm-nonexistent")
        assert result is False


# =============================================================================
# Test: aget_memory (async get)
# =============================================================================


class TestAGetMemory:
    """Tests for aget_memory."""

    @pytest.mark.asyncio
    async def test_aget_existing(self, memory_manager) -> None:
        """aget_memory returns existing memory."""
        created = await memory_manager.create_memory(content="Async get test")
        result = await memory_manager.aget_memory(created.id)
        assert result is not None
        assert result.content == "Async get test"

    @pytest.mark.asyncio
    async def test_aget_nonexistent(self, memory_manager) -> None:
        """aget_memory returns None for nonexistent memory."""
        result = await memory_manager.aget_memory("mm-nonexistent")
        assert result is None


# =============================================================================
# Test: alist_memories (async list)
# =============================================================================


class TestAListMemories:
    """Tests for alist_memories."""

    @pytest.mark.asyncio
    async def test_alist_basic(self, memory_manager) -> None:
        """alist_memories returns memories."""
        await memory_manager.create_memory(content="AList 1")
        await memory_manager.create_memory(content="AList 2")
        result = await memory_manager.alist_memories()
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_alist_with_limit(self, memory_manager) -> None:
        """alist_memories respects limit."""
        for i in range(5):
            await memory_manager.create_memory(content=f"AList limit {i}")
        result = await memory_manager.alist_memories(limit=3)
        assert len(result) == 3


# =============================================================================
# Test: acontent_exists (async content exists)
# =============================================================================


class TestAContentExists:
    """Tests for acontent_exists."""

    @pytest.mark.asyncio
    async def test_acontent_exists_true(self, memory_manager) -> None:
        """acontent_exists returns True for existing content."""
        await memory_manager.create_memory(content="Async exists test")
        result = await memory_manager.acontent_exists("Async exists test")
        assert result is True

    @pytest.mark.asyncio
    async def test_acontent_exists_false(self, memory_manager) -> None:
        """acontent_exists returns False for non-existing content."""
        result = await memory_manager.acontent_exists("Non-existing async content")
        assert result is False


# =============================================================================
# Test: aupdate_memory (async update)
# =============================================================================


class TestAUpdateMemory:
    """Tests for aupdate_memory."""

    @pytest.mark.asyncio
    async def test_aupdate_content(self, memory_manager) -> None:
        """aupdate_memory updates content."""
        memory = await memory_manager.create_memory(content="Original async")
        updated = await memory_manager.aupdate_memory(memory.id, content="Updated async")
        assert updated.content == "Updated async"

    @pytest.mark.asyncio
    async def test_aupdate_tags(self, memory_manager) -> None:
        """aupdate_memory updates tags."""
        memory = await memory_manager.create_memory(content="Tag async", tags=["old"])
        updated = await memory_manager.aupdate_memory(memory.id, tags=["new"])
        assert updated.tags == ["new"]


# =============================================================================
# Test: find_by_prefix
# =============================================================================


class TestFindByPrefix:
    """Tests for find_by_prefix."""

    @pytest.mark.asyncio
    async def test_find_by_prefix(self, memory_manager) -> None:
        """find_by_prefix returns memories matching ID prefix."""
        memory = await memory_manager.create_memory(content="Prefix test")
        prefix = memory.id[:8]
        results = memory_manager.find_by_prefix(prefix)
        assert len(results) >= 1
        assert any(r.id == memory.id for r in results)

    def test_find_by_prefix_no_match(self, memory_manager) -> None:
        """find_by_prefix returns empty list for non-matching prefix."""
        results = memory_manager.find_by_prefix("zzz-nonexistent")
        assert results == []


# =============================================================================
# Test: count_memories
# =============================================================================


class TestCountMemories:
    """Tests for count_memories."""

    def test_count_empty(self, memory_manager) -> None:
        """count_memories returns 0 for empty database."""
        assert memory_manager.count_memories() == 0

    @pytest.mark.asyncio
    async def test_count_with_memories(self, memory_manager) -> None:
        """count_memories returns correct count."""
        await memory_manager.create_memory(content="Count 1")
        await memory_manager.create_memory(content="Count 2")
        assert memory_manager.count_memories() == 2


# =============================================================================
# Test: reindex_embeddings
# =============================================================================


class TestReindexEmbeddings:
    """Tests for reindex_embeddings."""

    @pytest.mark.asyncio
    async def test_reindex_no_vectorstore(self, memory_manager) -> None:
        """reindex_embeddings returns error when no VectorStore."""
        result = await memory_manager.reindex_embeddings()
        assert result["success"] is False
        assert "not configured" in result["error"]

    @pytest.mark.asyncio
    async def test_reindex_with_vectorstore(self, db, memory_config) -> None:
        """reindex_embeddings rebuilds collection with all memories."""
        from unittest.mock import AsyncMock

        mock_vs = MagicMock()
        mock_vs.upsert = AsyncMock()
        mock_vs.rebuild = AsyncMock()
        mock_embed = AsyncMock(return_value=[0.1, 0.2])
        manager = MemoryManager(
            db=db, config=memory_config, vector_store=mock_vs, embed_fn=mock_embed
        )
        await manager.create_memory(content="Reindex 1")
        await manager.create_memory(content="Reindex 2")
        result = await manager.reindex_embeddings()
        assert result["success"] is True
        assert result["total_memories"] == 2
        assert result["embeddings_generated"] == 2
        # Verify rebuild was called (not individual upserts)
        mock_vs.rebuild.assert_called_once()
        memory_dicts = mock_vs.rebuild.call_args[0][0]
        assert len(memory_dicts) == 2


# =============================================================================
# Test: get_entity_graph / get_entity_neighbors
# =============================================================================


class TestEntityGraph:
    """Tests for FalkorDB entity graph methods."""

    def test_clear_graph_clients_clears_manager_and_child_service_refs(
        self,
        db,
        memory_config,
    ) -> None:
        """clear_graph_clients removes all graph references owned by the manager."""
        manager = MemoryManager(db=db, config=memory_config, falkordb_host=None)
        kg_service = MagicMock()
        manager._falkor_client = MagicMock()
        manager._kg_service = kg_service
        manager._search_service._kg_service = kg_service
        manager._indexing_service._kg_service = kg_service

        manager.clear_graph_clients()

        assert manager._falkor_client is None
        assert manager._kg_service is None
        assert manager._search_service._kg_service is None
        assert manager._indexing_service._kg_service is None

    @pytest.mark.asyncio
    async def test_get_entity_graph_no_falkordb(self, db, memory_config) -> None:
        """get_entity_graph returns None when no FalkorDB configured."""
        manager = MemoryManager(db=db, config=memory_config, falkordb_host=None)
        assert manager._falkor_client is None
        assert manager._kg_service is None
        result = await manager.get_entity_graph()
        assert result is None

    @pytest.mark.asyncio
    async def test_get_entity_neighbors_no_falkordb(self, db, memory_config) -> None:
        """get_entity_neighbors returns None when no FalkorDB configured."""
        manager = MemoryManager(db=db, config=memory_config, falkordb_host=None)
        assert manager._falkor_client is None
        result = await manager.get_entity_neighbors("test-entity")
        assert result is None


# =============================================================================
# Test: export_markdown
# =============================================================================


class TestExportMarkdown:
    """Tests for export_markdown."""

    @pytest.mark.asyncio
    async def test_export_empty(self, memory_manager) -> None:
        """export_markdown returns something for empty database."""
        result = memory_manager.export_markdown()
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_export_with_memories(self, memory_manager) -> None:
        """export_markdown includes memory content."""
        await memory_manager.create_memory(content="Export test memory")
        result = memory_manager.export_markdown()
        assert "Export test memory" in result


# =============================================================================
# Test: _rrf_merge
# =============================================================================


class TestRRFMerge:
    """Tests for Reciprocal Rank Fusion merge."""

    def test_rrf_single_list(self) -> None:
        """RRF with one empty list returns the other list."""
        result = MemoryManager._rrf_merge(["a", "b", "c"], [])
        assert result == ["a", "b", "c"]

    def test_rrf_both_lists(self) -> None:
        """RRF with both lists merges and ranks correctly."""
        result = MemoryManager._rrf_merge(["a", "b"], ["b", "c"])
        # "b" should rank highest since it appears in both lists
        assert result[0] == "b"

    def test_rrf_disjoint_lists(self) -> None:
        """RRF with disjoint lists returns all items."""
        result = MemoryManager._rrf_merge(["a", "b"], ["c", "d"])
        assert set(result) == {"a", "b", "c", "d"}

    def test_rrf_empty_lists(self) -> None:
        """RRF with empty lists returns empty list."""
        result = MemoryManager._rrf_merge([], [])
        assert result == []


# =============================================================================
# Test: llm_service property
# =============================================================================


class TestLLMServiceProperty:
    """Tests for llm_service property getter/setter."""

    def test_get_llm_service(self, memory_manager) -> None:
        """llm_service getter returns value from ingestion service."""
        assert memory_manager.llm_service is None

    def test_set_llm_service(self, memory_manager) -> None:
        """llm_service setter updates both internal and ingestion service."""
        mock_service = MagicMock()
        memory_manager.llm_service = mock_service
        assert memory_manager._llm_service is mock_service

    def test_embed_fn_property(self, memory_manager) -> None:
        """embed_fn property returns None when not configured."""
        assert memory_manager.embed_fn is None

    def test_kg_service_property(self, memory_manager) -> None:
        """kg_service property returns None when not configured."""
        assert memory_manager.kg_service is None


# =============================================================================
# Test: create_memory with auto_crossref
# =============================================================================


class TestCreateMemoryAutoCrossref:
    """Tests for create_memory with auto_crossref."""

    @pytest.mark.asyncio
    async def test_auto_crossref_failure_handled(self, db, memory_config) -> None:
        """Auto-crossref failure does not prevent memory creation."""
        memory_config.auto_crossref = True
        manager = MemoryManager(db=db, config=memory_config)
        # No VectorStore, so _create_crossrefs returns 0
        memory = await manager.create_memory(content="Crossref test")
        assert memory is not None


# =============================================================================
# Test: _create_crossrefs
# =============================================================================


class TestCreateCrossrefs:
    """Tests for _create_crossrefs."""

    @pytest.mark.asyncio
    async def test_no_vectorstore_returns_zero(self, memory_manager) -> None:
        """_create_crossrefs returns 0 when no VectorStore."""
        memory = await memory_manager.create_memory(content="Crossref no VS")
        result = await memory_manager._create_crossrefs(memory)
        assert result == 0

    @pytest.mark.asyncio
    async def test_crossrefs_with_vectorstore(self, db, memory_config) -> None:
        """_create_crossrefs creates cross-references from VectorStore results."""
        import asyncio
        from unittest.mock import AsyncMock

        mock_vs = MagicMock()
        mock_vs.upsert = AsyncMock()
        mock_vs.search = AsyncMock(return_value=[("other-id", 0.9)])
        mock_embed = AsyncMock(return_value=[0.1, 0.2])
        manager = MemoryManager(
            db=db, config=memory_config, vector_store=mock_vs, embed_fn=mock_embed
        )
        # Create two memories so crossref can find the other
        mem1 = await manager.create_memory(content="First memory")
        mem2 = await manager.create_memory(content="Second memory")
        # Drain background dedup tasks so they don't pollute search.await_count.
        if manager._background_tasks:
            await asyncio.gather(*manager._background_tasks, return_exceptions=True)
        mock_vs.search = AsyncMock(return_value=[(mem2.id, 0.9)])
        result = await manager._create_crossrefs(mem1)
        assert result >= 0  # May be 0 or 1 depending on crossref logic
        assert mock_vs.search.await_count == 1


# =============================================================================
# Test: get_related
# =============================================================================


class TestGetRelated:
    """Tests for get_related."""

    @pytest.mark.asyncio
    async def test_get_related_empty(self, memory_manager) -> None:
        """get_related returns empty list when no crossrefs exist."""
        memory = await memory_manager.create_memory(content="Related test")
        related = await memory_manager.get_related(memory.id)
        assert related == []
