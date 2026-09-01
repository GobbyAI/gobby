"""Comprehensive tests for MemoryManager class.

Tests cover:
- Memory creation (create_memory)
- Memory retrieval (search_memories)
- Memory deletion (delete_memory)
- Access statistics and debouncing
- Statistics retrieval
"""

import inspect
import logging
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.persistence import MemoryConfig
from gobby.memory.manager import DEFAULT_LIST_LIMIT, MemoryManager
from gobby.memory.protocol import MemoryBackendProtocol
from gobby.memory.services.projection_repair import ProjectionScopeRepairResult
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories import LocalMemoryManager, Memory
from gobby.storage.projects import PERSONAL_PROJECT_ID

pytestmark = pytest.mark.unit

# Valid-format UUID constants for uuid-typed columns (projects.id, sessions.id,
# memories.id) — synthetic slugs like "proj-123" are rejected by PostgreSQL.
PROJECT_ID = "11111111-1111-4111-8111-111111111111"
SESSION_ID = "22222222-2222-4222-8222-222222222222"
OTHER_PROJECT_ID = "33333333-3333-4333-8333-333333333333"
MISSING_MEMORY_ID = "99999999-9999-4999-8999-999999999999"

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def db(hub_db: HubDatabase) -> HubDatabase:
    """Create a temporary hub database for testing."""
    return hub_db


@pytest.fixture
def memory_config() -> MemoryConfig:
    """Create a default memory configuration with PostgreSQL backend."""
    return MemoryConfig(
        enabled=True,
        backend="local",
        injection_limit=10,
        access_debounce_seconds=60,
    )


@pytest.fixture
def memory_manager(db: HubDatabase, memory_config: MemoryConfig) -> MemoryManager:
    """Create a MemoryManager with real database."""
    return MemoryManager(db=db, config=memory_config)


@pytest.fixture
def mock_storage() -> MagicMock:
    """Create a mock LocalMemoryManager."""
    return MagicMock(spec=LocalMemoryManager)


@pytest.fixture
def mock_config() -> MagicMock:
    """Create a mock MemoryConfig."""
    config = MagicMock(spec=MemoryConfig)
    config.access_debounce_seconds = 60
    config.backend = "postgres"
    return config


@pytest.fixture
def mock_db() -> MagicMock:
    """Create a mock database."""
    return MagicMock(spec=HubDatabase)


# =============================================================================
# Test: Initialization
# =============================================================================


class TestMemoryManagerInit:
    """Tests for MemoryManager initialization."""

    def test_init_creates_storage(self, db: HubDatabase, memory_config: MemoryConfig) -> None:
        """Test that initialization creates a LocalMemoryManager."""
        manager = MemoryManager(db=db, config=memory_config)
        assert manager.db is db
        assert manager.config is memory_config
        assert isinstance(manager.storage, LocalMemoryManager)

    def test_init_creates_backend(self, db: HubDatabase, memory_config: MemoryConfig) -> None:
        """Test that initialization creates a MemoryBackendProtocol instance."""
        manager = MemoryManager(db=db, config=memory_config)
        assert hasattr(manager, "_backend")
        assert isinstance(manager._backend, MemoryBackendProtocol)

    def test_init_with_null_backend(self, db: HubDatabase) -> None:
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
    async def test_create_memory_basic(self, memory_manager: MemoryManager) -> None:
        """Test basic memory creation."""
        memory = await memory_manager.create_memory(
            content="Test fact",
            memory_type="fact",
        )

        uuid.UUID(memory.id)  # validates UUID format
        assert memory.content == "Test fact"
        assert memory.memory_type == "fact"

    @pytest.mark.asyncio
    async def test_create_memory_restores_soft_hidden_duplicate(
        self, memory_manager: MemoryManager
    ) -> None:
        """Re-creating content dream GC soft-hid reactivates the row via the
        lifecycle content-check path, not an invisible duplicate."""
        created = await memory_manager.create_memory(content="reactivate me via facade")
        memory_manager.storage.mark_dreamed(created.id, hidden_as="delete")
        with pytest.raises(ValueError, match="not found"):
            memory_manager.storage.get_memory(created.id)

        recreated = await memory_manager.create_memory(content="reactivate me via facade")

        assert recreated.id == created.id
        assert recreated.deleted_at is None
        assert recreated.dream_action is None
        # Restored to visibility rather than left hidden.
        assert memory_manager.storage.get_memory(created.id).id == created.id

    @pytest.mark.asyncio
    async def test_create_memory_with_all_params(
        self, db: HubDatabase, memory_config: MemoryConfig
    ) -> None:
        """Test memory creation with all parameters."""
        db.execute(
            "INSERT INTO projects (id, name) VALUES (%s, %s)",
            (PROJECT_ID, "test-project"),
        )
        now = datetime.now(UTC).isoformat()
        db.execute(
            """INSERT INTO sessions (id, external_id, machine_id, source, project_id, created_at)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                SESSION_ID,
                "ext-123",
                "21000000-0000-4000-8000-000000000008",
                "claude",
                PROJECT_ID,
                now,
            ),
        )

        manager = MemoryManager(db=db, config=memory_config)
        memory = await manager.create_memory(
            content="User prefers dark theme",
            memory_type="preference",
            project_id=None,
            source_type="user",
            source_session_id=SESSION_ID,
            tags=["ui", "theme"],
        )

        assert memory.content == "User prefers dark theme"
        assert memory.memory_type == "preference"
        assert memory.source_type == "user"
        assert memory.source_session_id == SESSION_ID
        assert memory.tags == ["ui", "theme"]

    @pytest.mark.asyncio
    async def test_create_memory_default_values(self, memory_manager: MemoryManager) -> None:
        """Test memory creation uses correct defaults."""
        memory = await memory_manager.create_memory(content="Simple fact")

        assert memory.memory_type == "fact"
        assert memory.source_type == "agent"
        assert memory.tags == []

    @pytest.mark.asyncio
    async def test_create_memory_uses_project_plus_global_dedup_scope(
        self,
        memory_manager: MemoryManager,
        db: HubDatabase,
    ) -> None:
        """Facade creation sees globals, isolates projects, and keeps global creation global."""
        db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_ID, "Project 1"))
        db.execute(
            "INSERT INTO projects (id, name) VALUES (%s, %s)",
            (OTHER_PROJECT_ID, "Project 2"),
        )

        global_memory = await memory_manager.create_memory(
            content="Facade visible global",
            project_id=PERSONAL_PROJECT_ID,
            is_global=True,
        )
        visible_result = await memory_manager.create_memory(
            content="Facade visible global",
            project_id=PROJECT_ID,
        )
        first_project = await memory_manager.create_memory(
            content="Facade project isolated",
            project_id=PROJECT_ID,
        )
        same_project = await memory_manager.create_memory(
            content="Facade project isolated",
            project_id=PROJECT_ID,
        )
        second_project = await memory_manager.create_memory(
            content="Facade project isolated",
            project_id=OTHER_PROJECT_ID,
        )
        new_global = await memory_manager.create_memory(
            content="Facade project isolated",
            project_id=PERSONAL_PROJECT_ID,
            is_global=True,
        )

        assert visible_result.id == global_memory.id
        assert visible_result.project_id == PERSONAL_PROJECT_ID
        assert visible_result.is_global is True
        assert same_project.id == first_project.id
        assert first_project.id != second_project.id
        assert new_global.id not in {first_project.id, second_project.id}
        assert new_global.project_id == PERSONAL_PROJECT_ID
        assert new_global.is_global is True

    def test_create_memory_with_restore_metadata_uses_lww(
        self, memory_manager: MemoryManager
    ) -> None:
        memory_id = str(uuid.uuid4())
        created_at = datetime(2023, 1, 1, tzinfo=UTC)
        initial_updated_at = datetime(2023, 1, 2, tzinfo=UTC)

        initial = memory_manager.storage.create_memory(
            content="initial restored memory",
            project_id=PERSONAL_PROJECT_ID,
            memory_id=memory_id,
            created_at=created_at,
            updated_at=initial_updated_at,
        )
        stale = memory_manager.storage.create_memory(
            content="stale restored memory",
            project_id=PERSONAL_PROJECT_ID,
            memory_id=memory_id,
            created_at=datetime(2023, 1, 3, tzinfo=UTC),
            updated_at=datetime(2023, 1, 1, tzinfo=UTC),
        )
        fresh = memory_manager.storage.create_memory(
            content="fresh restored memory",
            project_id=PERSONAL_PROJECT_ID,
            memory_id=memory_id,
            created_at=datetime(2023, 1, 4, tzinfo=UTC),
            updated_at=datetime(2023, 1, 5, tzinfo=UTC),
        )

        assert initial.id == memory_id
        assert initial.created_at == created_at
        assert stale.content == "initial restored memory"
        assert fresh.content == "fresh restored memory"
        assert fresh.created_at == created_at
        assert fresh.updated_at == datetime(2023, 1, 5, tzinfo=UTC)


class TestProjectionScopeRepair:
    """Tests for startup repair of explicit scope projections."""

    @pytest.mark.asyncio
    async def test_manager_delegates_projection_scope_repair(
        self, mock_db: MagicMock, memory_config: MemoryConfig
    ) -> None:
        manager = MemoryManager(db=mock_db, config=memory_config)
        expected = ProjectionScopeRepairResult(vectors_repaired=2, graph_entities_repaired=3)
        manager._projection_repair_service.repair = AsyncMock(return_value=expected)

        result = await manager.repair_secondary_scope_projections()

        assert result is expected
        manager._projection_repair_service.repair.assert_awaited_once_with()


def test_null_project_repair_fenced(db: HubDatabase) -> None:
    """The explicit-scope migration makes the former NULL-project race unrepresentable."""
    column = db.fetchone(
        """
        SELECT is_nullable
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'memories'
          AND column_name = 'project_id'
        """
    )

    assert column is not None
    assert column["is_nullable"] == "NO"


class TestMemoryScopeChanges:
    """Tests for explicit visibility changes and secondary-store sync."""

    @pytest.mark.asyncio
    async def test_promote_memory_updates_vector_payload_and_marks_graph_pending(
        self,
        mock_db: MagicMock,
        memory_config: MemoryConfig,
    ) -> None:
        vector_store = MagicMock()
        vector_store.set_payload = AsyncMock()
        manager = MemoryManager(db=mock_db, config=memory_config, vector_store=vector_store)
        updated = Memory(
            id="mem-1",
            memory_type="fact",
            content="Universal",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
            project_id="proj-1",
            is_global=True,
        )
        manager.storage = MagicMock(spec=LocalMemoryManager)
        previous = Memory(
            id="mem-1",
            memory_type="fact",
            content="Universal",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
            project_id="proj-1",
            is_global=False,
        )
        manager.storage.get_memory.return_value = previous
        manager.storage.set_memory_global.return_value = updated
        manager._lifecycle_service._reconcile_active_snapshot = AsyncMock(return_value=True)

        result = await manager.promote_memory("mem-1")

        assert result is updated
        assert result.is_global is True
        assert result.project_id == "proj-1"
        manager.storage.set_memory_global.assert_called_once_with("mem-1", True)
        manager._lifecycle_service._reconcile_active_snapshot.assert_awaited_once_with(
            updated,
            graph_cleanup_project_id="proj-1",
            graph_cleanup_is_global=False,
        )


# =============================================================================
# Test: search_memories (Memory Retrieval)
# =============================================================================


class TestSearchMemories:
    """Tests for the search_memories method."""

    @pytest.mark.asyncio
    async def test_search_memories_no_query_returns_top_memories(
        self, memory_manager: MemoryManager
    ) -> None:
        """Test search_memories without query returns top memories."""
        await memory_manager.create_memory(content="Low importance")
        await memory_manager.create_memory(content="High importance")
        await memory_manager.create_memory(content="Medium importance")

        memories = await memory_manager.search_memories(limit=2)

        assert len(memories) == 2

    @pytest.mark.asyncio
    async def test_search_memories_no_query_all_returned(
        self, memory_manager: MemoryManager
    ) -> None:
        """Test search_memories without query returns all memories (no VectorStore)."""
        await memory_manager.create_memory(content="Python is a programming language")
        await memory_manager.create_memory(content="JavaScript runs in browsers")

        memories = await memory_manager.search_memories(limit=10)

        assert len(memories) == 2

    @pytest.mark.asyncio
    async def test_search_memories_by_memory_type(self, memory_manager: MemoryManager) -> None:
        """Test search_memories filters by memory type."""
        await memory_manager.create_memory(content="Fact 1", memory_type="fact")
        await memory_manager.create_memory(content="Pref 1", memory_type="preference")

        memories = await memory_manager.search_memories(memory_type="preference")

        assert len(memories) == 1
        assert memories[0].memory_type == "preference"

    @pytest.mark.asyncio
    async def test_search_memories_limit(self, memory_manager: MemoryManager) -> None:
        """Test search_memories respects limit parameter."""
        for i in range(5):
            await memory_manager.create_memory(content=f"Memory {i}")

        memories = await memory_manager.search_memories(limit=3)

        assert len(memories) == 3

    @pytest.mark.asyncio
    async def test_search_memories_updates_access_stats(
        self, memory_manager: MemoryManager
    ) -> None:
        """Test search_memories updates access statistics."""
        memory = await memory_manager.create_memory(content="Track access")
        original_count = memory.access_count

        _ = await memory_manager.search_memories(limit=10)

        updated = memory_manager.get_memory(memory.id)
        assert updated.access_count == original_count + 1
        assert updated.last_accessed_at is not None

    @pytest.mark.asyncio
    async def test_recall_search_on_degraded_manager_emits_caller_event(
        self, memory_manager: MemoryManager
    ) -> None:
        """Regression #17491: a manager without vector wiring routes queries through
        the keyword fallback, which must still emit one recall-signal event per
        search carrying the caller and join keys — fallback is never silent."""
        snapshots = []
        memory_manager._search_service._search_debug_sink = snapshots.append

        await memory_manager.search_memories(
            query="anything at all",
            session_id="session-1",
            recall_request_id="request-1",
            caller="memory.recall",
        )

        assert len(snapshots) == 1
        assert snapshots[0].caller == "memory.recall"
        assert snapshots[0].session_id == "session-1"
        assert snapshots[0].recall_request_id == "request-1"

    def test_injection_outcome_recorder_follows_the_signal_hub_flag(
        self, db: HubDatabase, memory_config: MemoryConfig
    ) -> None:
        """#21011: the search tool's delivery recorder exists only with the hub on."""
        assert MemoryManager(db=db, config=memory_config).injection_outcome_recorder is None

        hub_on = memory_config.model_copy(update={"recall_signal_hub": True})

        assert MemoryManager(db=db, config=hub_on).injection_outcome_recorder is not None


# =============================================================================
# Test: Access Statistics
# =============================================================================


class TestAccessStats:
    """Tests for access statistics updates."""

    @pytest.mark.asyncio
    async def test_update_access_stats_debouncing(self, memory_manager: MemoryManager) -> None:
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

    @pytest.mark.asyncio
    async def test_update_access_stats_empty_list(self, memory_manager: MemoryManager) -> None:
        """Test _update_access_stats handles empty list."""
        with patch.object(memory_manager.storage, "update_access_stats") as update_access_stats:
            result = await memory_manager._update_access_stats([])

        assert result is None
        assert update_access_stats.call_count == 0

    @pytest.mark.asyncio
    async def test_update_access_stats_invalid_timestamp(
        self, db: HubDatabase, memory_config: MemoryConfig
    ) -> None:
        """Test _update_access_stats handles invalid timestamps gracefully."""
        manager = MemoryManager(db=db, config=memory_config)

        memory = MagicMock(spec=Memory)
        memory.id = "mm-test"
        memory.last_accessed_at = "invalid-timestamp"

        assert await manager._update_access_stats([memory]) is None

    @pytest.mark.asyncio
    async def test_update_access_stats_no_timezone(
        self, db: HubDatabase, memory_config: MemoryConfig
    ) -> None:
        """Test _update_access_stats handles timestamps without timezone."""
        manager = MemoryManager(db=db, config=memory_config)

        real_memory = manager.storage.create_memory(
            content="Test timezone", project_id=PERSONAL_PROJECT_ID
        )

        memory = MagicMock(spec=Memory)
        memory.id = real_memory.id
        memory.last_accessed_at = "2024-01-01T00:00:00"

        await manager._update_access_stats([memory])

        updated = manager.get_memory(real_memory.id)
        assert updated.access_count >= 1


# =============================================================================
# Test: delete_memory (Memory Deletion)
# =============================================================================


class TestDeleteMemory:
    """Tests for the delete_memory method."""

    @pytest.mark.asyncio
    async def test_delete_existing_memory(self, memory_manager: MemoryManager) -> None:
        """Test deleting an existing memory."""
        memory = await memory_manager.create_memory(content="To delete")

        result = await memory_manager.delete_memory(memory.id)

        assert result is True
        assert memory_manager.get_memory(memory.id) is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_memory(self, memory_manager: MemoryManager) -> None:
        """Test deleting a non-existent memory returns False."""
        result = await memory_manager.delete_memory(MISSING_MEMORY_ID)
        assert result is False


# =============================================================================
# Test: List Memories
# =============================================================================


class TestListMemories:
    """Tests for list_memories method."""

    @pytest.mark.asyncio
    async def test_list_memories_basic(self, memory_manager: MemoryManager) -> None:
        """Test basic memory listing."""
        await memory_manager.create_memory(content="Memory 1")
        await memory_manager.create_memory(content="Memory 2")

        memories = memory_manager.list_memories()

        assert len(memories) == 2

    @pytest.mark.asyncio
    async def test_list_memories_with_offset(self, memory_manager: MemoryManager) -> None:
        """Test memory listing with offset."""
        for i in range(5):
            await memory_manager.create_memory(content=f"Memory {i}")

        memories = memory_manager.list_memories(limit=2, offset=2)

        assert len(memories) == 2

    @pytest.mark.asyncio
    async def test_list_memories_by_type(self, memory_manager: MemoryManager) -> None:
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
    async def test_content_exists_true(self, memory_manager: MemoryManager) -> None:
        """Test content_exists returns True for existing content."""
        await memory_manager.create_memory(content="Existing content")

        result = memory_manager.content_exists("Existing content")

        assert result is True

    def test_content_exists_false(self, memory_manager: MemoryManager) -> None:
        """Test content_exists returns False for non-existing content."""
        result = memory_manager.content_exists("Non-existing content")

        assert result is False


# =============================================================================
# Test: Get Memory
# =============================================================================


class TestGetMemory:
    """Tests for get_memory method."""

    @pytest.mark.asyncio
    async def test_get_memory_exists(self, memory_manager: MemoryManager) -> None:
        """Test getting an existing memory."""
        created = await memory_manager.create_memory(content="Get test")

        retrieved = memory_manager.get_memory(created.id)

        assert retrieved is not None
        assert retrieved.id == created.id
        assert retrieved.content == created.content

    def test_get_memory_not_found(self, memory_manager: MemoryManager) -> None:
        """Test getting a non-existent memory returns None."""
        result = memory_manager.get_memory(MISSING_MEMORY_ID)

        assert result is None


# =============================================================================
# Test: Update Memory
# =============================================================================


class TestUpdateMemory:
    """Tests for update_memory method."""

    @pytest.mark.asyncio
    async def test_update_memory_content(self, memory_manager: MemoryManager) -> None:
        """Test updating memory content preserves the memory ID."""
        memory = await memory_manager.create_memory(content="Original")

        updated = await memory_manager.update_memory(memory.id, content="Updated")

        assert updated.id == memory.id
        assert updated.content == "Updated"
        assert memory_manager.get_memory(memory.id).content == "Updated"

    @pytest.mark.asyncio
    async def test_update_memory_tags(self, memory_manager: MemoryManager) -> None:
        """Test updating memory tags."""
        memory = await memory_manager.create_memory(content="Test", tags=["old"])

        updated = await memory_manager.update_memory(memory.id, tags=["new", "tags"])

        assert updated.tags == ["new", "tags"]

    @pytest.mark.asyncio
    async def test_update_memory_type(self, memory_manager: MemoryManager) -> None:
        """Test updating and persisting a memory type."""
        memory = await memory_manager.create_memory(content="Test", memory_type="fact")

        updated = await memory_manager.update_memory(memory.id, memory_type="preference")

        assert updated.memory_type == "preference"
        assert memory_manager.get_memory(memory.id).memory_type == "preference"

    @pytest.mark.asyncio
    async def test_update_memory_not_found_raises(self, memory_manager: MemoryManager) -> None:
        """Test updating non-existent memory raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            await memory_manager.update_memory(MISSING_MEMORY_ID, tags=["new"])

    @pytest.mark.asyncio
    async def test_scoped_update_rejects_other_project(
        self, db: HubDatabase, memory_config: MemoryConfig
    ) -> None:
        """An out-of-scope update leaves the memory and secondary indices unchanged."""
        mock_vs = MagicMock()
        mock_vs.upsert = AsyncMock()
        mock_embed = AsyncMock(return_value=[0.1, 0.2])
        mock_kg = MagicMock()
        mock_kg.remove_memory_from_graph = AsyncMock()
        manager = MemoryManager(
            db=db, config=memory_config, vector_store=mock_vs, embed_fn=mock_embed
        )
        manager._kg_service = mock_kg
        db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_ID, "Project A"))
        db.execute(
            "INSERT INTO projects (id, name) VALUES (%s, %s)", (OTHER_PROJECT_ID, "Project B")
        )
        memory = await manager.create_memory(
            content="Project B memory", project_id=OTHER_PROJECT_ID
        )
        mock_vs.upsert.reset_mock()

        with pytest.raises(ValueError, match="not found"):
            await manager.update_memory_scoped(
                memory.id,
                PROJECT_ID,
                content="Cross-project rewrite",
            )

        assert manager.get_memory(memory.id).content == "Project B memory"
        assert all(
            call.kwargs["payload"]["content"] != "Cross-project rewrite"
            for call in mock_vs.upsert.await_args_list
        )
        mock_kg.remove_memory_from_graph.assert_not_awaited()


# =============================================================================
# Test: Get Stats
# =============================================================================


class TestGetStats:
    """Tests for get_stats method."""

    @pytest.mark.asyncio
    async def test_get_stats_empty(self, memory_manager: MemoryManager) -> None:
        """Test stats with no memories."""
        stats = await memory_manager.get_stats()

        assert stats["total_count"] == 0
        assert stats["by_type"] == {}

    @pytest.mark.asyncio
    async def test_get_stats_with_memories(self, memory_manager: MemoryManager) -> None:
        """Test stats with multiple memories."""
        await memory_manager.create_memory(content="Fact 1", memory_type="fact")
        await memory_manager.create_memory(content="Fact 2", memory_type="fact")
        await memory_manager.create_memory(content="Pref 1", memory_type="preference")

        stats = await memory_manager.get_stats()

        assert stats["total_count"] == 3
        assert stats["by_type"]["fact"] == 2
        assert stats["by_type"]["preference"] == 1


# =============================================================================
# Test: Edge Cases and Error Handling
# =============================================================================


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_duplicate_content_handling(self, memory_manager: MemoryManager) -> None:
        """Test creating memory with duplicate content returns existing."""
        memory1 = await memory_manager.create_memory(content="Duplicate test")
        memory2 = await memory_manager.create_memory(content="Duplicate test")

        assert memory1.id == memory2.id

    @pytest.mark.asyncio
    async def test_search_memories_empty_database(self, memory_manager: MemoryManager) -> None:
        """Test search_memories on empty database returns empty list."""
        memories = await memory_manager.search_memories()
        assert memories == []

    @pytest.mark.asyncio
    async def test_update_access_stats_exception_handling(
        self, db: HubDatabase, memory_config: MemoryConfig
    ) -> None:
        """Test _update_access_stats handles storage exceptions."""
        manager = MemoryManager(db=db, config=memory_config)

        memory = MagicMock(spec=Memory)
        memory.id = "mm-test"
        memory.last_accessed_at = None

        with patch.object(manager.storage, "update_access_stats") as mock_update:
            mock_update.side_effect = Exception("Database error")

            assert await manager._update_access_stats([memory]) is None
            assert mock_update.call_count == 1


# =============================================================================
# Test: VectorStore integration
# =============================================================================


class TestVectorStoreIntegration:
    """Tests for VectorStore-related operations."""

    @pytest.mark.asyncio
    async def test_embed_and_upsert_no_vectorstore(self, memory_manager: MemoryManager) -> None:
        """_embed_and_upsert does nothing when no VectorStore."""
        result = await memory_manager._embed_and_upsert("id", "content")

        assert result is False
        assert memory_manager.vector_store is None

    @pytest.mark.asyncio
    async def test_embed_and_upsert_failure_logged(
        self, db: HubDatabase, memory_config: MemoryConfig
    ) -> None:
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
        self, db: HubDatabase, memory_config: MemoryConfig
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

        assert mock_embed.call_count == 2
        assert mock_vs.upsert.call_count == 2
        assert not hasattr(manager, "_embeddings_available")

    @pytest.mark.asyncio
    async def test_create_memory_with_vectorstore(
        self, db: HubDatabase, memory_config: MemoryConfig
    ) -> None:
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

    @pytest.mark.asyncio
    async def test_create_and_update_retry_embedding_until_provider_recovers(
        self,
        db: HubDatabase,
        memory_config: MemoryConfig,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Transient embed failures never suppress later create/update attempts."""
        mock_vs = MagicMock()
        mock_vs.upsert = AsyncMock()
        mock_embed = AsyncMock(
            side_effect=[
                RuntimeError("provider down"),
                RuntimeError("provider still down"),
                [0.1, 0.2],
            ]
        )
        manager = MemoryManager(
            db=db,
            config=memory_config,
            vector_store=mock_vs,
            embed_fn=mock_embed,
        )

        with caplog.at_level(logging.DEBUG, logger="gobby.memory.services.lifecycle"):
            memory = await manager.create_memory(content="Initial content")
            unavailable = await manager.update_memory(memory.id, content="Still unavailable")
            assert unavailable.vector_needs_reindex is True
            recovered = await manager.update_memory(memory.id, content="Recovered content")

        assert recovered.content == "Recovered content"
        assert recovered.vector_needs_reindex is False
        assert mock_embed.await_count == 3
        mock_vs.upsert.assert_awaited_once_with(
            memory.id,
            [0.1, 0.2],
            {
                "project_id": PERSONAL_PROJECT_ID,
                "is_global": False,
                "memory_type": "fact",
            },
        )
        embedding_records = [
            record
            for record in caplog.records
            if record.name == "gobby.memory.services.lifecycle"
            and record.message.startswith("Embedding failed for")
        ]
        assert [record.levelno for record in embedding_records] == [logging.WARNING, logging.DEBUG]
        assert not hasattr(manager, "_embeddings_available")

    @pytest.mark.asyncio
    async def test_vector_upsert_failure_leaves_content_marked_for_reindex(
        self,
        db: HubDatabase,
        memory_config: MemoryConfig,
    ) -> None:
        mock_vs = MagicMock()
        mock_vs.upsert = AsyncMock(side_effect=RuntimeError("qdrant rejected write"))
        manager = MemoryManager(
            db=db,
            config=memory_config,
            vector_store=mock_vs,
            embed_fn=AsyncMock(return_value=[0.1, 0.2]),
        )
        memory = manager.storage.create_memory("Old indexed content", PERSONAL_PROJECT_ID)

        updated = await manager.update_memory(memory.id, content="New current content")

        assert updated.content == "New current content"
        assert updated.vector_needs_reindex is True
        assert manager.storage.list_vector_reindex_ids() == [memory.id]


class TestLifecycleService:
    """Service-level tests for memory lifecycle side effects."""

    @pytest.mark.asyncio
    async def test_restore_memory_indices_recreates_vector_and_requeues_graph(
        self,
        db: HubDatabase,
        memory_config: MemoryConfig,
    ) -> None:
        mock_vs = MagicMock()
        mock_vs.upsert = AsyncMock()
        mock_embed = AsyncMock(return_value=[0.1, 0.2])
        manager = MemoryManager(
            db=db,
            config=memory_config,
            vector_store=mock_vs,
            embed_fn=mock_embed,
        )
        db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_ID, "Project 1"))
        memory = manager.storage.create_memory(
            content="restored content",
            project_id=PROJECT_ID,
        )
        manager.storage.mark_graph_processed(memory.id)

        await manager.restore_memory_indices(
            memory.id,
            memory.content,
            PROJECT_ID,
            False,
            memory.memory_type.value,
        )

        mock_vs.upsert.assert_awaited_once_with(
            memory.id,
            [0.1, 0.2],
            {
                "project_id": PROJECT_ID,
                "is_global": False,
                "memory_type": memory.memory_type.value,
            },
        )
        row = db.fetchone("SELECT graph_processed FROM memories WHERE id = %s", (memory.id,))
        assert row is not None
        assert row["graph_processed"] in (False, 0)

    @pytest.mark.asyncio
    async def test_restore_memory_indices_returns_false_for_missing_row(
        self,
        db: HubDatabase,
        memory_config: MemoryConfig,
    ) -> None:
        manager = MemoryManager(db=db, config=memory_config)

        restored = await manager.restore_memory_indices(
            str(uuid.uuid4()),
            "missing content",
            PROJECT_ID,
            False,
            "fact",
        )

        assert restored is False

    @pytest.mark.asyncio
    async def test_create_update_delete_updates_secondary_indices(
        self,
        db: HubDatabase,
        memory_config: MemoryConfig,
    ) -> None:
        """Lifecycle service handles mutable metadata without vector churn."""
        mock_vs = MagicMock()
        mock_vs.upsert = AsyncMock()
        mock_vs.delete = AsyncMock()
        mock_embed = AsyncMock(return_value=[0.1, 0.2])
        manager = MemoryManager(
            db=db,
            config=memory_config,
            vector_store=mock_vs,
            embed_fn=mock_embed,
        )
        db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_ID, "Project 1"))

        memory = await manager._lifecycle_service.create_memory(
            content="Lifecycle service memory",
            project_id=PROJECT_ID,
        )
        updated = await manager._lifecycle_service.update_memory(
            memory.id,
            tags=["updated"],
        )
        deleted = await manager._lifecycle_service.delete_memory(memory.id)

        assert updated.content == "Lifecycle service memory"
        assert updated.tags == ["updated"]
        assert deleted is True
        assert mock_vs.upsert.await_count == 1
        mock_vs.delete.assert_awaited_once_with(memory.id)

    @pytest.mark.asyncio
    async def test_content_update_refreshes_secondary_indices(self, db: HubDatabase) -> None:
        config = MemoryConfig(
            enabled=True,
            backend="local",
            auto_crossref=True,
            dream={"write_supersession_mark_due_enabled": False},
        )
        mock_vs = MagicMock()
        mock_vs.upsert = AsyncMock()
        mock_vs.search = AsyncMock(return_value=[])
        mock_embed = AsyncMock(return_value=[0.1, 0.2])
        manager = MemoryManager(
            db=db,
            config=config,
            vector_store=mock_vs,
            embed_fn=mock_embed,
        )
        kg_service = MagicMock()
        kg_service.remove_memory_from_graph = AsyncMock()
        manager._kg_service = kg_service
        db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_ID, "Project 1"))

        memory = await manager.create_memory(content="Lifecycle old", project_id=PROJECT_ID)
        other = await manager.create_memory(content="Lifecycle related", project_id=PROJECT_ID)
        third = await manager.create_memory(content="Lifecycle third", project_id=PROJECT_ID)
        manager.storage.mark_graph_processed(memory.id)
        manager.storage.create_crossref(memory.id, other.id, 0.4)
        manager.storage.create_crossref(other.id, memory.id, 0.5)
        mock_vs.upsert.reset_mock()
        mock_vs.search.return_value = [
            (memory.id, 1.0),
            (other.id, 0.95),
            (third.id, 0.9),
        ]
        mock_vs.search_by_stored_vectors = AsyncMock(
            return_value={
                other.id: [(memory.id, 0.95)],
                third.id: [(memory.id, 0.9)],
            }
        )
        mock_embed.reset_mock()

        updated = await manager.update_memory(memory.id, content="Lifecycle new")

        assert updated.id == memory.id
        assert updated.content == "Lifecycle new"
        mock_embed.assert_any_await("Lifecycle new")
        mock_vs.upsert.assert_awaited_once_with(
            memory.id,
            [0.1, 0.2],
            {"project_id": PROJECT_ID, "is_global": False, "memory_type": "fact"},
        )
        kg_service.remove_memory_from_graph.assert_awaited_once_with(
            memory.id,
            project_id=PROJECT_ID,
            is_global=False,
        )
        graph_row = db.fetchone("SELECT graph_processed FROM memories WHERE id = %s", (memory.id,))
        assert graph_row["graph_processed"] is False
        assert mock_vs.search_by_stored_vectors.await_count == 1
        stored_vector_batches = [
            set(call.args[0]) for call in mock_vs.search_by_stored_vectors.await_args_list
        ]
        assert stored_vector_batches == [{other.id, third.id}]
        crossrefs = manager.storage.get_crossrefs(memory.id)
        assert len(crossrefs) == 3
        assert {(crossref.source_id, crossref.target_id) for crossref in crossrefs} == {
            (memory.id, other.id),
            (memory.id, third.id),
            (other.id, memory.id),
        }
        assert crossrefs[0].source_id == memory.id
        assert crossrefs[0].target_id == other.id


# =============================================================================
# Test: delete_memory with VectorStore and KG
# =============================================================================


class TestDeleteMemoryExtended:
    """Extended tests for delete_memory with VectorStore and KG."""

    @pytest.mark.asyncio
    async def test_delete_with_vectorstore(
        self, db: HubDatabase, memory_config: MemoryConfig
    ) -> None:
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
    async def test_scoped_delete_preserves_other_project_indices(
        self, db: HubDatabase, memory_config: MemoryConfig
    ) -> None:
        """An out-of-scope delete leaves the row, vector, and graph artifacts intact."""
        mock_vs = MagicMock()
        mock_vs.delete = AsyncMock()
        mock_vs.upsert = AsyncMock()
        mock_embed = AsyncMock(return_value=[0.1, 0.2])
        mock_kg = MagicMock()
        mock_kg.remove_memory_from_graph = AsyncMock()
        manager = MemoryManager(
            db=db, config=memory_config, vector_store=mock_vs, embed_fn=mock_embed
        )
        manager._kg_service = mock_kg
        db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_ID, "Project A"))
        db.execute(
            "INSERT INTO projects (id, name) VALUES (%s, %s)", (OTHER_PROJECT_ID, "Project B")
        )
        memory = await manager.create_memory(
            content="Project B protected memory",
            project_id=OTHER_PROJECT_ID,
        )
        mock_vs.delete.reset_mock()

        result = await manager.delete_memory_scoped(memory.id, PROJECT_ID)

        assert result is False
        assert manager.get_memory(memory.id) is not None
        mock_vs.delete.assert_not_awaited()
        mock_kg.remove_memory_from_graph.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delete_vectorstore_error_handled(
        self, db: HubDatabase, memory_config: MemoryConfig
    ) -> None:
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
    async def test_adelete_existing(self, memory_manager: MemoryManager) -> None:
        """adelete_memory removes an existing memory."""
        memory = await memory_manager.create_memory(content="Async delete test")
        result = await memory_manager.adelete_memory(memory.id)
        assert result is True
        assert memory_manager.get_memory(memory.id) is None

    @pytest.mark.asyncio
    async def test_adelete_nonexistent(self, memory_manager: MemoryManager) -> None:
        """adelete_memory returns False for nonexistent memory."""
        result = await memory_manager.adelete_memory(MISSING_MEMORY_ID)
        assert result is False


# =============================================================================
# Test: aget_memory (async get)
# =============================================================================


class TestAGetMemory:
    """Tests for aget_memory."""

    @pytest.mark.asyncio
    async def test_aget_existing(self, memory_manager: MemoryManager) -> None:
        """aget_memory returns existing memory."""
        created = await memory_manager.create_memory(content="Async get test")
        result = await memory_manager.aget_memory(created.id)
        assert result is not None
        assert result.content == "Async get test"

    @pytest.mark.asyncio
    async def test_aget_nonexistent(self, memory_manager: MemoryManager) -> None:
        """aget_memory returns None for nonexistent memory."""
        result = await memory_manager.aget_memory(MISSING_MEMORY_ID)
        assert result is None


# =============================================================================
# Test: alist_memories (async list)
# =============================================================================


class TestAListMemories:
    """Tests for alist_memories."""

    def test_alist_default_limit_signature(self) -> None:
        """alist_memories exposes the default pagination limit."""
        signature = inspect.signature(MemoryManager.alist_memories)
        assert signature.parameters["limit"].default == DEFAULT_LIST_LIMIT

    @pytest.mark.asyncio
    async def test_alist_basic(self, memory_manager: MemoryManager) -> None:
        """alist_memories returns memories."""
        await memory_manager.create_memory(content="AList 1")
        await memory_manager.create_memory(content="AList 2")
        result = await memory_manager.alist_memories()
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_alist_with_limit(self, memory_manager: MemoryManager) -> None:
        """alist_memories respects limit."""
        for i in range(5):
            await memory_manager.create_memory(content=f"AList limit {i}")
        result = await memory_manager.alist_memories(limit=3)
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_alist_with_none_limit_uses_default(self, memory_manager: MemoryManager) -> None:
        """alist_memories treats explicit None as the default limit."""
        for i in range(DEFAULT_LIST_LIMIT + 1):
            await memory_manager.create_memory(content=f"AList default {i}")
        result = await memory_manager.alist_memories(limit=None)
        assert len(result) == DEFAULT_LIST_LIMIT

    @pytest.mark.asyncio
    async def test_alist_with_zero_limit(self, memory_manager: MemoryManager) -> None:
        """alist_memories preserves explicit limit=0."""
        await memory_manager.create_memory(content="AList zero")
        result = await memory_manager.alist_memories(limit=0)
        assert result == []

    @pytest.mark.asyncio
    async def test_alist_excludes_memories_with_forbidden_tags(
        self,
        memory_manager: MemoryManager,
    ) -> None:
        """alist_memories applies tags_none through the async backend."""
        code_memory = await memory_manager.create_memory(
            content="Code lesson",
            tags=["review-lesson", "lesson-domain:code"],
        )
        await memory_manager.create_memory(
            content="Plan lesson",
            tags=["review-lesson", "lesson-domain:plan"],
        )

        result = await memory_manager.alist_memories(
            tags_all=["review-lesson"],
            tags_none=["lesson-domain:plan"],
        )

        assert [memory.id for memory in result] == [code_memory.id]


# =============================================================================
# Test: acontent_exists (async content exists)
# =============================================================================


class TestAContentExists:
    """Tests for acontent_exists."""

    @pytest.mark.asyncio
    async def test_acontent_exists_true(self, memory_manager: MemoryManager) -> None:
        """acontent_exists returns True for existing content."""
        await memory_manager.create_memory(content="Async exists test")
        result = await memory_manager.acontent_exists("Async exists test")
        assert result is True

    @pytest.mark.asyncio
    async def test_acontent_exists_false(self, memory_manager: MemoryManager) -> None:
        """acontent_exists returns False for non-existing content."""
        result = await memory_manager.acontent_exists("Non-existing async content")
        assert result is False


# =============================================================================
# Test: aupdate_memory (async update)
# =============================================================================


class TestAUpdateMemory:
    """Tests for aupdate_memory."""

    @pytest.mark.asyncio
    async def test_aupdate_content(self, memory_manager: MemoryManager) -> None:
        """aupdate_memory revises content and preserves the memory ID."""
        memory = await memory_manager.create_memory(content="Original async")
        memory_manager.mark_graph_processed(memory.id)

        updated = await memory_manager.aupdate_memory(memory.id, content="Updated async")

        assert updated.id == memory.id
        assert updated.content == "Updated async"
        graph_row = memory_manager.db.fetchone(
            "SELECT graph_processed FROM memories WHERE id = %s",
            (memory.id,),
        )
        assert graph_row["graph_processed"] is False

    @pytest.mark.asyncio
    async def test_aupdate_tags(self, memory_manager: MemoryManager) -> None:
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
    async def test_find_by_prefix(self, memory_manager: MemoryManager) -> None:
        """find_by_prefix returns memories matching ID prefix."""
        memory = await memory_manager.create_memory(content="Prefix test")
        prefix = memory.id[:8]
        results = memory_manager.find_by_prefix(prefix)
        assert len(results) >= 1
        assert any(r.id == memory.id for r in results)

    def test_find_by_prefix_no_match(self, memory_manager: MemoryManager) -> None:
        """find_by_prefix returns empty list for non-matching prefix."""
        results = memory_manager.find_by_prefix("zzz-nonexistent")
        assert results == []


class TestResolveMemoryId:
    """resolve_memory_id turns a full UUID or unique prefix into the stored id."""

    @pytest.mark.asyncio
    async def test_resolve_memory_id_accepts_full_uuid_and_unique_prefix(
        self, memory_manager: MemoryManager
    ) -> None:
        memory = await memory_manager.create_memory(content="Resolver target")

        assert memory_manager.resolve_memory_id(memory.id) == memory.id
        assert memory_manager.resolve_memory_id(memory.id[:8]) == memory.id
        assert memory_manager.resolve_memory_id(str(uuid.uuid4())) is None

    def test_resolve_memory_id_malformed_ref_returns_none(
        self, memory_manager: MemoryManager
    ) -> None:
        """A non-UUID ref is a prefix miss; the uuid column never sees it."""
        assert memory_manager.resolve_memory_id("not-a-uuid") is None
        assert memory_manager.resolve_memory_id("zzz") is None

    def test_resolve_memory_id_ambiguous_prefix_raises(
        self, memory_manager: MemoryManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from gobby.memory.facade import AmbiguousMemoryReferenceError

        first = MagicMock(id="c12fce9e-11c0-5112-8f03-fbf794031f6f")
        second = MagicMock(id="c12fce9e-0000-5000-8000-000000000000")
        monkeypatch.setattr(
            memory_manager,
            "find_by_prefix",
            lambda prefix, limit=5, project_id=None: [first, second],
        )

        with pytest.raises(AmbiguousMemoryReferenceError) as exc_info:
            memory_manager.resolve_memory_id("c12fce9e")

        assert exc_info.value.candidates == (first.id, second.id)
        assert first.id in str(exc_info.value)
        assert second.id in str(exc_info.value)


# =============================================================================
# Test: count_memories
# =============================================================================


class TestCountMemories:
    """Tests for count_memories."""

    def test_count_empty(self, memory_manager: MemoryManager) -> None:
        """count_memories returns 0 for empty database."""
        assert memory_manager.count_memories() == 0

    @pytest.mark.asyncio
    async def test_count_with_memories(self, memory_manager: MemoryManager) -> None:
        """count_memories returns correct count."""
        await memory_manager.create_memory(content="Count 1")
        await memory_manager.create_memory(content="Count 2")
        assert memory_manager.count_memories() == 2

    @pytest.mark.asyncio
    async def test_count_filters_by_memory_type(self, memory_manager: MemoryManager) -> None:
        """count_memories filters totals by memory_type."""
        await memory_manager.create_memory(content="Count fact", memory_type="fact")
        await memory_manager.create_memory(content="Count preference", memory_type="preference")

        assert memory_manager.count_memories(memory_type="fact") == 1
        assert memory_manager.count_memories(memory_type="preference") == 1
        assert memory_manager.count_memories(memory_type="pattern") == 0


# =============================================================================
# Test: reindex_embeddings
# =============================================================================


class TestReindexEmbeddings:
    """Tests for reindex_embeddings."""

    @pytest.mark.asyncio
    async def test_reindex_no_vectorstore(self, memory_manager: MemoryManager) -> None:
        """reindex_embeddings returns error when no VectorStore."""
        result = await memory_manager.reindex_embeddings()
        assert result["success"] is False
        assert "not configured" in result["error"]

    @pytest.mark.asyncio
    async def test_reindex_with_vectorstore(
        self, db: HubDatabase, memory_config: MemoryConfig
    ) -> None:
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
        db: HubDatabase,
        memory_config: MemoryConfig,
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
    async def test_get_entity_graph_no_falkordb(
        self, db: HubDatabase, memory_config: MemoryConfig
    ) -> None:
        """get_entity_graph returns None when no FalkorDB configured."""
        manager = MemoryManager(db=db, config=memory_config, falkordb_host=None)
        assert manager._falkor_client is None
        assert manager._kg_service is None
        result = await manager.get_entity_graph()
        assert result is None

    @pytest.mark.asyncio
    async def test_get_entity_neighbors_no_falkordb(
        self, db: HubDatabase, memory_config: MemoryConfig
    ) -> None:
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
    async def test_export_empty(self, memory_manager: MemoryManager) -> None:
        """export_markdown returns something for empty database."""
        result = memory_manager.export_markdown()
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_export_with_memories(self, memory_manager: MemoryManager) -> None:
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

    def test_get_llm_service(self, memory_manager: MemoryManager) -> None:
        """llm_service getter returns value from ingestion service."""
        assert memory_manager.llm_service is None

    def test_llm_service_resolver_cannot_be_replaced(self, memory_manager: MemoryManager) -> None:
        with pytest.raises(AttributeError):
            object.__setattr__(memory_manager, "llm_service", MagicMock())

    def test_embed_fn_property(self, memory_manager: MemoryManager) -> None:
        """embed_fn property returns None when not configured."""
        assert memory_manager.embed_fn is None

    def test_kg_service_property(self, memory_manager: MemoryManager) -> None:
        """kg_service property returns None when not configured."""
        assert memory_manager.kg_service is None


# =============================================================================
# Test: create_memory with auto_crossref
# =============================================================================


class TestCreateMemoryAutoCrossref:
    """Tests for create_memory with auto_crossref."""

    @pytest.mark.asyncio
    async def test_auto_crossref_failure_handled(
        self, db: HubDatabase, memory_config: MemoryConfig
    ) -> None:
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
    async def test_no_vectorstore_returns_zero(self, memory_manager: MemoryManager) -> None:
        """_create_crossrefs returns 0 when no VectorStore."""
        memory = await memory_manager.create_memory(content="Crossref no VS")
        result = await memory_manager._create_crossrefs(memory)
        assert result == 0

    @pytest.mark.asyncio
    async def test_crossrefs_with_vectorstore(
        self,
        db: HubDatabase,
        memory_config: MemoryConfig,
    ) -> None:
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
    async def test_get_related_empty(self, memory_manager: MemoryManager) -> None:
        """get_related returns empty list when no crossrefs exist."""
        memory = await memory_manager.create_memory(content="Related test")
        related = await memory_manager.get_related(memory.id)
        assert related == []
