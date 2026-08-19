"""Tests for memory backend factory, NullBackend, and StorageAdapter.

Tests the pluggable backend system:
- get_backend() factory function (null, unknown types)
- NullBackend CRUD operations (no persistence)
- StorageAdapter for hub storage (CRUD, search, list)
- StorageAdapter media attachment support
- Module exports
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from gobby.memory.backends import get_backend
from gobby.memory.backends.storage_adapter import StorageAdapter
from gobby.memory.protocol import MemoryBackendProtocol, MemoryCapability
from gobby.storage.memories import LocalMemoryManager

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


def _make_adapter(hub_db: HubDatabase) -> StorageAdapter:
    """Create a StorageAdapter wrapping a fresh LocalMemoryManager."""
    return StorageAdapter(LocalMemoryManager(hub_db))


# =============================================================================
# Test: get_backend Factory Function
# =============================================================================


class TestGetBackend:
    """Tests for the get_backend factory function."""

    def test_get_backend_exists(self) -> None:
        """Test that get_backend function is importable."""
        assert callable(get_backend)

    def test_get_backend_unknown_type_raises(self) -> None:
        """Test that unknown backend type raises ValueError."""
        with pytest.raises(ValueError, match="Unknown backend type"):
            get_backend("unknown_backend_type")

    def test_get_backend_null_type(self) -> None:
        """Test that 'null' backend type returns a valid backend."""
        backend = get_backend("null")
        assert isinstance(backend, MemoryBackendProtocol)

    def test_get_backend_postgres_raises(self) -> None:
        """Test that 'postgres' backend type is no longer supported via factory."""
        with pytest.raises(ValueError, match="Unknown backend type"):
            get_backend("postgres")


# =============================================================================
# Test: NullBackend
# =============================================================================


class TestNullBackend:
    """Tests for the NullBackend implementation (for testing purposes)."""

    def test_null_backend_capabilities(self) -> None:
        """Test that NullBackend declares its capabilities."""
        backend = get_backend("null")
        caps = backend.capabilities()
        assert isinstance(caps, set)
        # NullBackend should support basic operations
        assert MemoryCapability.CREATE in caps
        assert MemoryCapability.READ in caps

    @pytest.mark.asyncio
    async def test_null_backend_create(self):
        """Test that NullBackend.create() works."""
        backend = get_backend("null")
        result = await backend.create("test memory content")
        assert result.outcome == "created"
        record = result.memory
        assert record.content == "test memory content"
        assert record.id is not None

    @pytest.mark.asyncio
    async def test_null_backend_create_rejects_noncanonical_memory_type(self) -> None:
        backend = get_backend("null")

        with pytest.raises(ValueError, match="Invalid memory_type 'debugging_pattern'"):
            await backend.create("test memory content", memory_type="debugging_pattern")

    @pytest.mark.asyncio
    async def test_null_backend_get_returns_none(self):
        """Test that NullBackend.get() returns None (no persistence)."""
        backend = get_backend("null")
        result = await backend.get("nonexistent-id")
        assert result is None

    @pytest.mark.asyncio
    async def test_null_backend_search_returns_empty(self):
        """Test that NullBackend.search() returns empty list."""
        from gobby.memory.protocol import MemoryQuery

        backend = get_backend("null")
        query = MemoryQuery(text="test")
        results = await backend.search(query)
        assert results == []

    @pytest.mark.asyncio
    async def test_null_backend_delete_returns_false(self):
        """Test that NullBackend.delete() returns False (nothing to delete)."""
        backend = get_backend("null")
        result = await backend.delete("nonexistent-id")
        assert result is False

    @pytest.mark.asyncio
    async def test_null_backend_list_returns_empty(self):
        """Test that NullBackend.list_memories() returns empty list."""
        backend = get_backend("null")
        results = await backend.list_memories()
        assert results == []


# =============================================================================
# Test: StorageAdapter
# =============================================================================


class TestStorageAdapter:
    """Tests for the StorageAdapter (hub storage via MemoryBackendProtocol)."""

    def test_storage_adapter_capabilities(self, hub_db: HubDatabase) -> None:
        """Test that StorageAdapter declares full capabilities."""
        backend = _make_adapter(hub_db)
        caps = backend.capabilities()
        assert isinstance(caps, set)
        assert MemoryCapability.CREATE in caps
        assert MemoryCapability.READ in caps
        assert MemoryCapability.UPDATE in caps
        assert MemoryCapability.DELETE in caps
        assert MemoryCapability.SEARCH_TEXT in caps
        assert MemoryCapability.TAGS in caps
        assert "media" not in {cap.value for cap in caps}

    @pytest.mark.asyncio
    async def test_storage_adapter_create_and_get(self, hub_db: HubDatabase):
        """Test StorageAdapter create and get operations."""
        backend = _make_adapter(hub_db)

        # Create a memory
        result = await backend.create(
            content="Test memory for StorageAdapter",
            memory_type="fact",
            tags=["test", "adapter"],
        )
        assert result.outcome == "created"
        record = result.memory
        assert record.id is not None
        assert record.content == "Test memory for StorageAdapter"

        # Get the memory back
        retrieved = await backend.get(record.id)
        assert retrieved is not None
        assert retrieved.id == record.id
        assert retrieved.content == record.content

    @pytest.mark.asyncio
    async def test_storage_adapter_update(self, hub_db: HubDatabase):
        """Test StorageAdapter update operation."""
        backend = _make_adapter(hub_db)

        # Create a memory
        record = (await backend.create(content="Original content", tags=["old"])).memory

        # Update it
        updated = await backend.update(
            record.id,
            tags=["new"],
        )
        assert updated.content == "Original content"
        assert updated.tags == ["new"]

    @pytest.mark.asyncio
    async def test_storage_adapter_delete(self, hub_db: HubDatabase):
        """Test StorageAdapter delete operation."""
        backend = _make_adapter(hub_db)

        # Create a memory
        record = (await backend.create(content="To be deleted")).memory

        # Delete it
        result = await backend.delete(record.id)
        assert result is True

        # Verify it's gone
        retrieved = await backend.get(record.id)
        assert retrieved is None

    @pytest.mark.asyncio
    async def test_storage_adapter_search(self, hub_db: HubDatabase):
        """Test StorageAdapter search operation."""
        from gobby.memory.protocol import MemoryQuery

        backend = _make_adapter(hub_db)

        # Create some memories
        await backend.create(content="Python programming language")
        await backend.create(content="JavaScript for web development")
        await backend.create(content="Python web frameworks like Flask")

        # Search for Python
        query = MemoryQuery(text="Python")
        results = await backend.search(query)

        assert len(results) >= 2
        assert all("Python" in r.content for r in results)

    @pytest.mark.asyncio
    async def test_storage_adapter_list_memories(self, hub_db: HubDatabase):
        """Test StorageAdapter list_memories operation."""
        backend = _make_adapter(hub_db)

        # Create some memories
        await backend.create(content="Memory 1", memory_type="fact")
        await backend.create(content="Memory 2", memory_type="preference")
        await backend.create(content="Memory 3", memory_type="fact")

        # List all
        all_results = await backend.list_memories()
        assert len(all_results) >= 3

        # List by type
        facts = await backend.list_memories(memory_type="fact")
        assert all(r.memory_type == "fact" for r in facts)

    @pytest.mark.asyncio
    async def test_storage_adapter_list_with_limit(self, hub_db: HubDatabase):
        """Test StorageAdapter list_memories with limit."""
        backend = _make_adapter(hub_db)

        # Create several memories
        for i in range(5):
            await backend.create(content=f"Memory {i}")

        # List with limit
        results = await backend.list_memories(limit=3)
        assert len(results) == 3


# =============================================================================
# Test: Module Exports
# =============================================================================


class TestModuleExports:
    """Tests for module exports."""

    def test_get_backend_exported(self) -> None:
        """Test that get_backend is exported from backends module."""
        from gobby.memory import backends

        assert hasattr(backends, "get_backend")
        assert callable(backends.get_backend)

    def test_backend_classes_not_directly_exported(self) -> None:
        """Test that backend implementations are not directly exported.

        Users should use get_backend() factory, not import classes directly.
        """
        from gobby.memory import backends

        # NullBackend should not be in __all__
        # (implementation detail, not public API)
        if hasattr(backends, "__all__"):
            assert "NullBackend" not in backends.__all__


@pytest.mark.asyncio
async def test_create_memory_round_trips_rationale_and_provenance(hub_db: HubDatabase) -> None:
    from gobby.config.persistence import MemoryConfig
    from gobby.memory.manager import MemoryManager
    from gobby.memory.services.repository import MemoryRepository
    from gobby.storage.projects import PERSONAL_PROJECT_ID

    task_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
    hub_db.execute(
        "INSERT INTO tasks "
        "(id, title, project_id, task_type, priority, validation_criteria, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (
            task_id,
            "Provenance task",
            PERSONAL_PROJECT_ID,
            "task",
            2,
            "Facade provenance fixture.",
        ),
    )
    manager = MemoryManager(
        db=hub_db,
        config=MemoryConfig(enabled=True, backend="local", access_debounce_seconds=0),
    )
    memory = await manager.create_memory(
        content="Facade rationale hop",
        rationale="because the hop must echo",
        source_task_id=task_id,
        created_by_agent="backend-developer",
    )
    assert memory.rationale == "because the hop must echo"
    assert memory.source_task_id == task_id
    assert memory.created_by_agent == "backend-developer"

    null_backend = get_backend("null")
    null_result = await null_backend.create(
        content="Null rationale hop",
        rationale="null echo",
        source_task_id=task_id,
        created_by_agent="null-agent",
    )
    assert null_result.memory.rationale == "null echo"
    assert null_result.memory.source_task_id == task_id
    assert null_result.memory.created_by_agent == "null-agent"
    echoed = MemoryRepository.record_to_memory(null_result.memory)
    assert echoed.rationale == "null echo"
    assert echoed.source_task_id == task_id
    assert echoed.created_by_agent == "null-agent"


# =============================================================================
# Test: StorageAdapter Media Support
# =============================================================================
