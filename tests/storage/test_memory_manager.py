import uuid

import pytest

from gobby.config.persistence import MemoryConfig
from gobby.memory.manager import MemoryManager
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


@pytest.fixture
def db(temp_db: HubDatabase):
    database = temp_db
    yield database


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
    assert memory_manager.get_memory(str(uuid.uuid4())) is None


@pytest.mark.asyncio
async def test_update_memory(memory_manager):
    """Test updating memory fields."""
    memory = await memory_manager.create_memory("Original", tags=["old"])

    updated = await memory_manager.update_memory(
        memory.id,
        tags=["new", "updated"],
    )

    assert updated.content == "Original"
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

    stats = await memory_manager.get_stats()

    assert stats["total_count"] == 2
    assert stats["by_type"]["fact"] == 1
    assert stats["by_type"]["preference"] == 1


@pytest.mark.asyncio
async def test_surfacing_search_increments_surfaced_count(memory_manager):
    """A surfacing search counts hits as surfaced and leaves access_count alone."""
    memory = await memory_manager.create_memory("Track my surfacing")
    assert (memory.surfaced_count, memory.access_count) == (0, 0)

    await memory_manager.search_memories(query="Track", caller="memory.surface")

    updated = memory_manager.get_memory(memory.id)
    assert (updated.surfaced_count, updated.access_count) == (1, 0)
    assert updated.last_surfaced_at is not None
    assert updated.last_accessed_at is None


@pytest.mark.asyncio
async def test_default_search_is_a_probe(memory_manager):
    """The `memory.search` default caller increments neither counter."""
    memory = await memory_manager.create_memory("Timestamp test")

    await memory_manager.search_memories(query="Timestamp")

    updated = memory_manager.get_memory(memory.id)
    assert (updated.surfaced_count, updated.access_count) == (0, 0)
    assert updated.last_surfaced_at is None


@pytest.mark.asyncio
async def test_surfaced_tracking_debounce(db):
    """Rapid surfacing searches are debounced by access_debounce_seconds."""
    config = MemoryConfig(access_debounce_seconds=3600)
    manager = MemoryManager(db, config)

    memory = await manager.create_memory("Debounce test")

    await manager.search_memories(query="Debounce", caller="memory.surface")
    first = manager.get_memory(memory.id)
    assert first.surfaced_count == 1

    await manager.search_memories(query="Debounce", caller="memory.surface")
    second = manager.get_memory(memory.id)
    assert second.surfaced_count == 1


@pytest.mark.asyncio
async def test_access_tracking_independent_memories(memory_manager):
    """Test that access stats are tracked independently per memory.

    Without VectorStore, search returns all memories so both get accessed.
    We test independence by accessing via get_memory instead.
    """
    memory1 = await memory_manager.create_memory("Alpha memory")
    memory2 = await memory_manager.create_memory("Beta memory")

    # Access only the first memory directly
    await memory_manager.record_memory_access(memory1.id)

    updated1 = memory_manager.get_memory(memory1.id)
    updated2 = memory_manager.get_memory(memory2.id)

    assert updated1.access_count == 1
    assert updated2.access_count == 0  # Not accessed
