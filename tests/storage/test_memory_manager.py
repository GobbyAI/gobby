import uuid

import pytest

from gobby.config.persistence import MemoryConfig
from gobby.memory.manager import MemoryManager
from gobby.storage.database import LocalDatabase
from tests.fixtures.migrations import run_migrations

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path):
    database = LocalDatabase(tmp_path / "gobby-hub.db")
    run_migrations(database)
    yield database
    database.close()


@pytest.fixture
def memory_manager(db):
    config = MemoryConfig()
    return MemoryManager(db, config)


@pytest.mark.asyncio
async def test_create_memory(memory_manager):
    memory = await memory_manager.create_memory(
        content="Test remember",
        memory_type="fact",
        tags=["test"],
    )
    uuid.UUID(memory.id)  # validates UUID format
    assert memory.content == "Test remember"


@pytest.mark.asyncio
async def test_search_memories_no_query(memory_manager):
    await memory_manager.create_memory("Memory one")
    await memory_manager.create_memory("Memory two")

    # No query returns all
    memories = await memory_manager.search_memories()
    assert len(memories) >= 1


@pytest.mark.asyncio
async def test_search_memories_with_query(memory_manager):
    """Without VectorStore, keyword search is used."""
    await memory_manager.create_memory("The quick brown fox")
    await memory_manager.create_memory("The lazy dog")

    # Without VectorStore, the hub handles keyword search.
    memories = await memory_manager.search_memories(query="fox")
    assert len(memories) == 1
    assert "fox" in memories[0].content


@pytest.mark.asyncio
async def test_delete_memory(memory_manager):
    memory = await memory_manager.create_memory("To forget")
    assert await memory_manager.delete_memory(memory.id)
    assert await memory_manager.search_memories(query="To forget") == []


@pytest.mark.asyncio
async def test_content_exists(memory_manager):
    """Test duplicate content detection."""
    await memory_manager.create_memory("Unique content")

    assert memory_manager.content_exists("Unique content")
    assert not memory_manager.content_exists("Different content")


@pytest.mark.asyncio
async def test_get_memory(memory_manager):
    """Test getting a specific memory by ID."""
    memory = await memory_manager.create_memory("Test memory")

    retrieved = memory_manager.get_memory(memory.id)
    assert retrieved is not None
    assert retrieved.content == "Test memory"

    # Non-existent memory
    assert memory_manager.get_memory("nonexistent-id") is None


@pytest.mark.asyncio
async def test_update_memory(memory_manager):
    """Test updating memory fields."""
    memory = await memory_manager.create_memory("Original", tags=["old"])

    updated = await memory_manager.update_memory(
        memory.id,
        content="Updated",
        tags=["new", "updated"],
    )

    assert updated.content == "Updated"
    assert updated.tags == ["new", "updated"]


@pytest.mark.asyncio
async def test_list_memories(memory_manager):
    """Test listing memories with filters."""
    await memory_manager.create_memory("Fact 1", memory_type="fact")
    await memory_manager.create_memory("Pref 1", memory_type="preference")
    await memory_manager.create_memory("Fact 2", memory_type="fact")

    # Filter by type
    facts = memory_manager.list_memories(memory_type="fact")
    assert len(facts) == 2
    assert all(m.memory_type == "fact" for m in facts)


@pytest.mark.asyncio
async def test_get_stats(memory_manager):
    """Test memory statistics."""
    await memory_manager.create_memory("Fact", memory_type="fact")
    await memory_manager.create_memory("Preference", memory_type="preference")

    stats = memory_manager.get_stats()

    assert stats["total_count"] == 2
    assert stats["by_type"]["fact"] == 1
    assert stats["by_type"]["preference"] == 1


@pytest.mark.asyncio
async def test_access_tracking_increments_count(memory_manager):
    """Test that search_memories increments access_count."""
    memory = await memory_manager.create_memory("Track my access")

    # Initial access_count should be 0
    assert memory.access_count == 0

    # search_memories should update access stats
    await memory_manager.search_memories(query="Track")

    # Fetch fresh from storage
    updated = memory_manager.get_memory(memory.id)
    assert updated.access_count == 1
    assert updated.last_accessed_at is not None


@pytest.mark.asyncio
async def test_access_tracking_updates_timestamp(memory_manager):
    """Test that search_memories updates last_accessed_at."""
    memory = await memory_manager.create_memory("Timestamp test")

    # Initial last_accessed_at should be None
    assert memory.last_accessed_at is None

    # search_memories triggers access update
    await memory_manager.search_memories(query="Timestamp")

    updated = memory_manager.get_memory(memory.id)
    assert updated.last_accessed_at is not None


@pytest.mark.asyncio
async def test_access_tracking_debounce(db):
    """Test that rapid accesses are debounced."""

    # Use very short debounce for testing
    config = MemoryConfig(access_debounce_seconds=3600)  # 1 hour debounce
    manager = MemoryManager(db, config)

    memory = await manager.create_memory("Debounce test")

    # First search - should update
    await manager.search_memories(query="Debounce")
    first_access = manager.get_memory(memory.id)
    assert first_access.access_count == 1

    # Second search immediately - should be debounced
    await manager.search_memories(query="Debounce")
    second_access = manager.get_memory(memory.id)
    assert second_access.access_count == 1  # Still 1, debounced


@pytest.mark.asyncio
async def test_access_tracking_independent_memories(memory_manager):
    """Test that access stats are tracked independently per memory.

    Without VectorStore, search returns all memories so both get accessed.
    We test independence by accessing via get_memory instead.
    """
    memory1 = await memory_manager.create_memory("Alpha memory")
    memory2 = await memory_manager.create_memory("Beta memory")

    # Access only the first memory directly
    memory_manager._update_access_stats([memory_manager.get_memory(memory1.id)])

    updated1 = memory_manager.get_memory(memory1.id)
    updated2 = memory_manager.get_memory(memory2.id)

    assert updated1.access_count == 1
    assert updated2.access_count == 0  # Not accessed
