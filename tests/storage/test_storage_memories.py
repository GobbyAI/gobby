import uuid

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.memories import LocalMemoryManager
from gobby.storage.migrations import run_migrations

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path):
    database = LocalDatabase(tmp_path / "gobby-hub.db")
    run_migrations(database)
    yield database
    database.close()


@pytest.fixture
def memory_manager(db):
    return LocalMemoryManager(db)


def test_create_memory(memory_manager) -> None:
    memory = memory_manager.create_memory(
        content="Test memory",
        memory_type="fact",
        tags=["test"],
    )
    # Memory IDs are UUID5 (deterministic from content)
    uuid.UUID(memory.id)  # validates format
    assert memory.content == "Test memory"
    assert memory.memory_type == "fact"
    assert memory.tags == ["test"]


def test_get_memory(memory_manager) -> None:
    created = memory_manager.create_memory(content="Test get")
    retrieved = memory_manager.get_memory(created.id)
    assert retrieved == created


def test_update_memory(memory_manager) -> None:
    created = memory_manager.create_memory(content="Original")
    updated = memory_manager.update_memory(
        created.id,
        content="Updated",
    )
    assert updated.content == "Updated"
    assert updated.updated_at >= created.updated_at


def test_delete_memory(memory_manager) -> None:
    created = memory_manager.create_memory(content="To delete")
    assert memory_manager.delete_memory(created.id)
    with pytest.raises(ValueError, match="not found"):
        memory_manager.get_memory(created.id)


def test_list_memories(memory_manager, db) -> None:
    # Seed projects for foreign keys
    db.execute("INSERT INTO projects (id, name) VALUES ('p1', 'Project 1')")
    db.execute("INSERT INTO projects (id, name) VALUES ('p2', 'Project 2')")

    memory_manager.create_memory(content="Global", project_id=None)
    memory_manager.create_memory(content="Project A", project_id="p1")
    memory_manager.create_memory(content="Project B", project_id="p2")

    # List all global + project A
    # Wait, implementation logic: (project_id = ? OR project_id IS NULL)

    # 1. List with project_id="p1"
    memories = memory_manager.list_memories(project_id="p1")
    contents = {m.content for m in memories}
    assert "Global" in contents
    assert "Project A" in contents
    assert "Project B" not in contents

    # 2. List global only (if no project_id passed? Filter logic is currently conditional)
    # If project_id is None, query doesn't filter by project_id, so returns all?
    # Logic in manager: "if project_id: ...". So if None, returns all.
    all_memories = memory_manager.list_memories()
    assert len(all_memories) == 3


def test_search_memories(memory_manager) -> None:
    memory_manager.create_memory(content="The quick brown fox")
    memory_manager.create_memory(content="The lazy dog")

    results = memory_manager.search_memories(query_text="fox")
    assert len(results) == 1
    assert results[0].content == "The quick brown fox"

    results = memory_manager.search_memories(query_text="The")
    assert len(results) == 2


def test_memory_to_dict(memory_manager) -> None:
    """Test Memory.to_dict() method."""
    memory = memory_manager.create_memory(
        content="Test to_dict",
        memory_type="preference",
        tags=["tag1", "tag2"],
    )

    d = memory.to_dict()
    assert d["id"] == memory.id
    assert d["content"] == "Test to_dict"
    assert d["memory_type"] == "preference"
    assert d["tags"] == ["tag1", "tag2"]
    assert d["access_count"] == 0
    assert d["last_accessed_at"] is None
    assert "created_at" in d
    assert "updated_at" in d


def test_add_change_listener(memory_manager) -> None:
    """Test adding a change listener and verifying it's called."""
    call_count = [0]

    def listener():
        call_count[0] += 1

    memory_manager.add_change_listener(listener)

    # Listener should be called on create
    memory_manager.create_memory(content="Listener test")
    assert call_count[0] == 1

    # Listener should be called on update
    memories = memory_manager.list_memories()
    memory_manager.update_memory(memories[0].id, content="Updated content")
    assert call_count[0] == 2

    # Listener should be called on delete
    memory_manager.delete_memory(memories[0].id)
    assert call_count[0] == 3


def test_change_listener_error_handling(memory_manager) -> None:
    """Test that listener errors are caught and don't break operations."""
    call_count = [0]

    def failing_listener():
        call_count[0] += 1
        raise ValueError("Listener error")

    def normal_listener():
        call_count[0] += 10

    memory_manager.add_change_listener(failing_listener)
    memory_manager.add_change_listener(normal_listener)

    # Should not raise despite failing listener, and should still call other listeners
    memory = memory_manager.create_memory(content="Test error handling")
    assert call_count[0] == 11  # 1 from failing + 10 from normal
    assert memory.content == "Test error handling"


def test_create_memory_returns_existing(memory_manager) -> None:
    """Test that creating a memory with same content/project returns existing."""
    memory1 = memory_manager.create_memory(content="Duplicate test", project_id=None)
    memory2 = memory_manager.create_memory(content="Duplicate test", project_id=None)

    assert memory1.id == memory2.id
    assert memory1.content == memory2.content


def test_create_memory_dedup_across_projects(memory_manager, db) -> None:
    """Test that same content with different project_ids returns same memory (global dedup)."""
    db.execute("INSERT INTO projects (id, name) VALUES ('proj1', 'Project 1')")
    db.execute("INSERT INTO projects (id, name) VALUES ('proj2', 'Project 2')")

    memory1 = memory_manager.create_memory(content="Global dedup test", project_id="proj1")
    memory2 = memory_manager.create_memory(content="Global dedup test", project_id="proj2")
    memory3 = memory_manager.create_memory(content="Global dedup test", project_id=None)

    # All should return the same memory ID (global deduplication)
    assert memory1.id == memory2.id == memory3.id
    # First project_id should be preserved
    assert memory1.project_id == "proj1"


def test_memory_exists(memory_manager) -> None:
    """Test memory_exists method."""
    memory = memory_manager.create_memory(content="Exists test")
    assert memory_manager.memory_exists(memory.id) is True
    assert memory_manager.memory_exists("mm-nonexistent") is False


def test_content_exists_with_project(memory_manager, db) -> None:
    """Test content_exists method with project_id."""
    db.execute("INSERT INTO projects (id, name) VALUES ('proj1', 'Project 1')")

    memory_manager.create_memory(content="Project content", project_id="proj1")

    # Same content with same project should exist
    assert memory_manager.content_exists("Project content", project_id="proj1") is True

    # Same content with different project should ALSO exist (global deduplication)
    # This prevents duplicates when same content is stored with different project_ids
    assert memory_manager.content_exists("Project content", project_id="other-proj") is True

    # Different content should not exist
    assert memory_manager.content_exists("Other content", project_id="proj1") is False


def test_content_exists_without_project(memory_manager) -> None:
    """Test content_exists method without project_id."""
    memory_manager.create_memory(content="Global content", project_id=None)

    # Same content without project should exist
    assert memory_manager.content_exists("Global content", project_id=None) is True

    # Different content should not exist
    assert memory_manager.content_exists("Different content", project_id=None) is False


def test_update_memory_individual_fields(memory_manager) -> None:
    """Test updating individual fields in update_memory."""
    memory = memory_manager.create_memory(
        content="Original content",
        tags=["original"],
    )

    # Update only content
    updated = memory_manager.update_memory(memory.id, content="New content")
    assert updated.content == "New content"
    assert updated.tags == ["original"]

    # Update only tags
    updated = memory_manager.update_memory(memory.id, tags=["new", "tags"])
    assert updated.tags == ["new", "tags"]


def test_update_memory_no_changes(memory_manager) -> None:
    """Test update_memory with no changes returns existing memory."""
    memory = memory_manager.create_memory(content="No change test")
    updated = memory_manager.update_memory(memory.id)
    assert updated.id == memory.id
    assert updated.content == memory.content


def test_update_memory_not_found(memory_manager) -> None:
    """Test update_memory raises error for non-existent memory."""
    with pytest.raises(ValueError, match="Memory mm-nonexistent not found"):
        memory_manager.update_memory("mm-nonexistent", content="Update")


def test_delete_memory_not_found(memory_manager) -> None:
    """Test delete_memory returns False for non-existent memory."""
    result = memory_manager.delete_memory("mm-nonexistent")
    assert result is False


def test_list_memories_by_type(memory_manager) -> None:
    """Test filtering memories by memory_type."""
    memory_manager.create_memory(content="Fact memory", memory_type="fact")
    memory_manager.create_memory(content="Preference memory", memory_type="preference")
    memory_manager.create_memory(content="Pattern memory", memory_type="pattern")

    facts = memory_manager.list_memories(memory_type="fact")
    assert len(facts) == 1
    assert facts[0].memory_type == "fact"

    preferences = memory_manager.list_memories(memory_type="preference")
    assert len(preferences) == 1
    assert preferences[0].memory_type == "preference"


def test_list_memories_offset(memory_manager) -> None:
    """Test list_memories with offset pagination."""
    for i in range(5):
        memory_manager.create_memory(content=f"Memory {i}")

    # Get all memories
    all_memories = memory_manager.list_memories(limit=10)
    assert len(all_memories) == 5

    # Get with offset
    offset_memories = memory_manager.list_memories(limit=2, offset=2)
    assert len(offset_memories) == 2


def test_update_access_stats(memory_manager) -> None:
    """Test update_access_stats method."""
    memory = memory_manager.create_memory(content="Access test")
    assert memory.access_count == 0
    assert memory.last_accessed_at is None

    # Update access stats
    from datetime import UTC, datetime

    access_time = datetime.now(UTC).isoformat()
    memory_manager.update_access_stats(memory.id, access_time)

    # Retrieve and verify
    updated = memory_manager.get_memory(memory.id)
    assert updated.access_count == 1
    assert updated.last_accessed_at == access_time

    # Update again
    access_time2 = datetime.now(UTC).isoformat()
    memory_manager.update_access_stats(memory.id, access_time2)

    updated2 = memory_manager.get_memory(memory.id)
    assert updated2.access_count == 2
    assert updated2.last_accessed_at == access_time2


def test_search_memories_with_project(memory_manager, db) -> None:
    """Test search_memories with project_id filter."""
    db.execute("INSERT INTO projects (id, name) VALUES ('proj-search', 'Search Project')")

    memory_manager.create_memory(content="Project-specific fox", project_id="proj-search")
    memory_manager.create_memory(content="Global fox", project_id=None)

    # Search with project filter should find both project-specific and global
    results = memory_manager.search_memories(query_text="fox", project_id="proj-search")
    assert len(results) == 2


def test_search_memories_limit(memory_manager) -> None:
    """Test search_memories respects limit parameter."""
    for i in range(10):
        memory_manager.create_memory(content=f"Searchable item {i}")

    results = memory_manager.search_memories(query_text="Searchable", limit=3)
    assert len(results) == 3


def test_search_memories_escapes_wildcards(memory_manager) -> None:
    """Test that search properly escapes SQL LIKE wildcards."""
    memory_manager.create_memory(content="100% complete")
    memory_manager.create_memory(content="user_name is set")
    memory_manager.create_memory(content="path\\to\\file")

    # Search for % character
    results = memory_manager.search_memories(query_text="100%")
    assert len(results) == 1
    assert results[0].content == "100% complete"

    # Search for _ character
    results = memory_manager.search_memories(query_text="user_name")
    assert len(results) == 1
    assert results[0].content == "user_name is set"

    # Search for backslash
    results = memory_manager.search_memories(query_text="path\\to")
    assert len(results) == 1


def test_get_memory_not_found(memory_manager) -> None:
    """Test get_memory raises ValueError for non-existent memory."""
    with pytest.raises(ValueError, match="Memory mm-nonexistent not found"):
        memory_manager.get_memory("mm-nonexistent")


def test_memory_from_row_with_null_tags(memory_manager) -> None:
    """Test Memory.from_row handles null tags correctly."""
    # Create a memory without tags
    memory = memory_manager.create_memory(content="No tags", tags=None)
    assert memory.tags == []


def test_create_memory_with_all_fields(memory_manager, db) -> None:
    """Test creating a memory with all optional fields set."""
    db.execute("INSERT INTO projects (id, name) VALUES ('proj-full', 'Full Project')")
    # Insert a valid session to satisfy foreign key constraint
    db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, created_at) "
        "VALUES ('sess-123', 'ext-123', 'machine-1', 'claude', 'proj-full', datetime('now'))"
    )

    memory = memory_manager.create_memory(
        content="Full memory",
        memory_type="context",
        project_id="proj-full",
        source_type="agent",
        source_session_id="sess-123",
        tags=["tag1", "tag2", "tag3"],
    )

    assert memory.content == "Full memory"
    assert memory.memory_type == "context"
    assert memory.project_id == "proj-full"
    assert memory.source_type == "agent"
    assert memory.source_session_id == "sess-123"
    assert memory.tags == ["tag1", "tag2", "tag3"]


def test_list_memories_combined_filters(memory_manager, db) -> None:
    """Test list_memories with multiple filters combined."""
    db.execute("INSERT INTO projects (id, name) VALUES ('proj-combo', 'Combo Project')")

    memory_manager.create_memory(
        content="Fact one",
        memory_type="fact",
        project_id="proj-combo",
    )
    memory_manager.create_memory(
        content="Fact two",
        memory_type="fact",
        project_id="proj-combo",
    )
    memory_manager.create_memory(
        content="Preference one",
        memory_type="preference",
        project_id="proj-combo",
    )

    # Filter by project and type
    results = memory_manager.list_memories(project_id="proj-combo", memory_type="fact")
    assert len(results) == 2
    assert all(r.memory_type == "fact" for r in results)
