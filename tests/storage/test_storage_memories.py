import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories import LocalMemoryManager
from gobby.storage.memories_crud import DuplicateMemoryContentError
from gobby.storage.memories_models import Memory, MemoryType, visibility_predicate
from gobby.storage.memories_scope import ALL_MEMORIES, GLOBAL_MEMORIES, MemoryScope
from gobby.storage.projects import PERSONAL_PROJECT_ID

pytestmark = pytest.mark.unit

# projects.id, sessions.id, and memories.id/project_id/source_session_id are
# native uuid columns; synthetic ids must be valid UUID strings.
PROJECT_1 = "11111111-1111-1111-1111-111111111111"
PROJECT_2 = "22222222-2222-2222-2222-222222222222"
SESSION_1 = "33333333-3333-3333-3333-333333333333"
UNKNOWN_MEMORY_ID = "99999999-9999-9999-9999-999999999999"
UNKNOWN_PROJECT_ID = "88888888-8888-8888-8888-888888888888"
TASK_1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"


@pytest.fixture
def db(temp_db: HubDatabase):
    database = temp_db
    yield database


@pytest.fixture
def memory_manager(db):
    class TestMemoryManager(LocalMemoryManager):
        def create_memory(
            self,
            content: str,
            project_id: str = PERSONAL_PROJECT_ID,
            **kwargs,
        ) -> Memory:
            return super().create_memory(content, project_id, **kwargs)

    return TestMemoryManager(db)


def test_storage_create_requires_concrete_owner(db) -> None:
    manager = LocalMemoryManager(db)
    with pytest.raises(TypeError):
        manager.create_memory(content="owner required")  # type: ignore[call-arg]


def _insert_session(db: HubDatabase, session_id: str, project_id: str) -> None:
    db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, created_at) "
        "VALUES (%s, %s, '21000000-0000-4000-8000-000000000001', 'claude', %s, CURRENT_TIMESTAMP)",
        (session_id, f"ext-{session_id}", project_id),
    )


def _insert_task(db: HubDatabase, task_id: str, project_id: str = PERSONAL_PROJECT_ID) -> None:
    db.execute(
        "INSERT INTO tasks "
        "(id, title, project_id, task_type, priority, validation_criteria, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (
            task_id,
            f"Task {task_id}",
            project_id,
            "task",
            2,
            "Storage fixture task; behavior asserted by the test.",
        ),
    )


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


def test_create_memory_rejects_noncanonical_type(memory_manager) -> None:
    with pytest.raises(ValueError, match="Invalid memory_type 'debugging_pattern'"):
        memory_manager.create_memory(content="Bad type", memory_type="debugging_pattern")


def test_memory_model_coerces_canonical_string_to_enum(memory_manager) -> None:
    memory = memory_manager.create_memory(content="Canonical type", memory_type="fact")

    assert memory.memory_type is MemoryType.FACT


def test_get_memory(memory_manager) -> None:
    created = memory_manager.create_memory(content="Test get")
    retrieved = memory_manager.get_memory(created.id)
    assert retrieved == created


def test_update_memory(memory_manager) -> None:
    created = memory_manager.create_memory(content="Original")
    updated = memory_manager.update_memory(
        created.id,
        content="  Updated  ",
    )
    assert updated.id == created.id
    assert updated.content == "Updated"
    assert memory_manager.get_memory(created.id).content == "Updated"


def test_update_memory_type_marks_vector_payload_stale(memory_manager) -> None:
    memory = memory_manager.create_memory(content="Type update")

    updated = memory_manager.update_memory(memory.id, memory_type="pattern")

    assert updated.memory_type is MemoryType.PATTERN
    assert updated.vector_needs_reindex is True


def test_update_memory_with_same_type_keeps_vector_payload_fresh(memory_manager) -> None:
    memory = memory_manager.create_memory(content="Same type update")
    memory_manager.mark_vectors_reindexed({memory.id: memory.content})

    updated = memory_manager.update_memory(memory.id, memory_type="fact")

    assert updated.memory_type is MemoryType.FACT
    assert updated.vector_needs_reindex is False


def test_update_content_and_type_marks_vector_payload_stale_once(memory_manager) -> None:
    memory = memory_manager.create_memory(content="Combined type update")

    updated = memory_manager.update_memory(
        memory.id,
        content="Combined type and content update",
        memory_type="context",
    )

    assert updated.content == "Combined type and content update"
    assert updated.memory_type is MemoryType.CONTEXT
    assert updated.vector_needs_reindex is True


def test_update_memory_rejects_noncanonical_type(memory_manager) -> None:
    memory = memory_manager.create_memory(content="Type update rejection")

    with pytest.raises(ValueError, match="Invalid memory_type 'debugging_pattern'"):
        memory_manager.update_memory(memory.id, memory_type="debugging_pattern")


def test_content_update_tracks_and_clears_stale_vector_state(memory_manager) -> None:
    memory = memory_manager.create_memory("Original vector content")

    updated = memory_manager.update_memory(memory.id, content="Current vector content")

    assert updated.vector_needs_reindex is True
    assert memory_manager.list_vector_reindex_ids() == [memory.id]
    assert memory_manager.mark_vectors_reindexed({memory.id: "obsolete content"}) == 0
    assert memory_manager.get_memory(memory.id).vector_needs_reindex is True
    assert memory_manager.mark_vectors_reindexed({memory.id: updated.content}) == 1
    assert memory_manager.get_memory(memory.id).vector_needs_reindex is False
    assert memory_manager.mark_vectors_reindexed({memory.id: "obsolete content"}) == 0
    assert memory_manager.get_memory(memory.id).vector_needs_reindex is True


def test_promote_memory_does_not_bump_updated_at_or_change_owner(memory_manager, db) -> None:
    db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_1, "Project 1"))
    created = memory_manager.create_memory(content="Universal", project_id=PROJECT_1)

    promoted = memory_manager.set_memory_global(created.id, True)

    assert promoted.project_id == PROJECT_1
    assert promoted.is_global is True
    assert promoted.updated_at == created.updated_at


def test_delete_memory(memory_manager) -> None:
    created = memory_manager.create_memory(content="To delete")
    assert memory_manager.delete_memory(created.id)
    with pytest.raises(ValueError, match="not found"):
        memory_manager.get_memory(created.id)


def test_delete_memory_scoped_rejects_other_project(memory_manager, db) -> None:
    project_a = "11111111-1111-4111-8111-111111111111"
    project_b = "22222222-2222-4222-8222-222222222222"
    db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (project_a, "Project A"))
    db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (project_b, "Project B"))
    created = memory_manager.create_memory(content="Project B memory", project_id=project_b)

    assert memory_manager.delete_memory_scoped(created.id, project_a) is False
    assert memory_manager.get_memory(created.id).project_id == project_b


def test_update_memory_scoped_rejects_other_project(memory_manager, db) -> None:
    project_a = "11111111-1111-4111-8111-111111111111"
    project_b = "22222222-2222-4222-8222-222222222222"
    db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (project_a, "Project A"))
    db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (project_b, "Project B"))
    created = memory_manager.create_memory(content="Project B memory", project_id=project_b)

    with pytest.raises(ValueError, match="not found"):
        memory_manager.update_memory_scoped(
            created.id,
            project_a,
            content="Cross-project rewrite",
        )

    assert memory_manager.get_memory(created.id).content == "Project B memory"


def test_list_memories(memory_manager, db) -> None:
    # Seed projects for foreign keys
    db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_1, "Project 1"))
    db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_2, "Project 2"))

    memory_manager.create_memory(content="Global", project_id=PERSONAL_PROJECT_ID, is_global=True)
    memory_manager.create_memory(content="Project A", project_id=PROJECT_1)
    memory_manager.create_memory(content="Project B", project_id=PROJECT_2)

    # Project-visible scope includes the project's own rows plus global rows.
    memories = memory_manager.list_memories(scope=MemoryScope.project_visible(PROJECT_1))
    contents = {m.content for m in memories}
    assert "Global" in contents
    assert "Project A" in contents
    assert "Project B" not in contents

    # The explicit all scope is the default.
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

    # Listener should be called on mutable metadata update
    memories = memory_manager.list_memories()
    memory_manager.update_memory(memories[0].id, tags=["updated"])
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
    memory1 = memory_manager.create_memory(
        content="Duplicate test", project_id=PERSONAL_PROJECT_ID, is_global=True
    )
    memory2 = memory_manager.create_memory(
        content="Duplicate test", project_id=PERSONAL_PROJECT_ID, is_global=True
    )

    assert memory1.id == memory2.id
    assert memory1.content == memory2.content


def test_create_memory_persists_normalized_content(memory_manager) -> None:
    """create_memory stores stripped content consistently with deterministic IDs."""
    memory = memory_manager.create_memory(content="  Normalized content  ")

    assert memory.content == "Normalized content"


def test_update_memory_content_preserves_id(memory_manager) -> None:
    """update_memory revises content without changing the memory entity ID."""
    memory = memory_manager.create_memory(content="Before")

    updated = memory_manager.update_memory(memory.id, content="  After  ")

    assert updated.id == memory.id
    assert updated.content == "After"
    assert memory_manager.get_memory_by_content("After", ALL_MEMORIES).id == memory.id


def test_create_memory_old_content_after_revision_gets_new_id(memory_manager) -> None:
    memory = memory_manager.create_memory(content="Reusable content")
    memory_manager.update_memory(memory.id, content="Revised content")

    recreated = memory_manager.create_memory(content="Reusable content")

    assert recreated.id != memory.id
    assert recreated.content == "Reusable content"
    assert memory_manager.get_memory(memory.id).content == "Revised content"


def test_update_memory_empty_content_fails(memory_manager) -> None:
    memory = memory_manager.create_memory(content="Before")

    with pytest.raises(ValueError, match="Memory content cannot be empty"):
        memory_manager.update_memory(memory.id, content="   ")

    assert memory_manager.get_memory(memory.id).content == "Before"


def test_update_memory_duplicate_content_same_scope_fails(memory_manager) -> None:
    memory = memory_manager.create_memory(content="Before")
    memory_manager.create_memory(content="Existing")

    with pytest.raises(DuplicateMemoryContentError, match="already exists"):
        memory_manager.update_memory(memory.id, content="Existing")

    assert memory_manager.get_memory(memory.id).content == "Before"


def test_update_memory_duplicate_hidden_content_same_scope_fails(memory_manager, db) -> None:
    memory = memory_manager.create_memory(content="Before")
    hidden = memory_manager.create_memory(content="Hidden duplicate")
    _hide(db, hidden.id)

    with pytest.raises(ValueError, match="already exists"):
        memory_manager.update_memory(memory.id, content="Hidden duplicate")

    assert memory_manager.get_memory(memory.id).content == "Before"


def test_update_memory_allows_duplicate_content_in_different_project(memory_manager, db) -> None:
    db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_1, "Project 1"))
    db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_2, "Project 2"))
    memory = memory_manager.create_memory(content="Before", project_id=PROJECT_1)
    other = memory_manager.create_memory(content="Shared content", project_id=PROJECT_2)

    updated = memory_manager.update_memory(memory.id, content="Shared content")

    assert updated.id == memory.id
    assert updated.project_id == PROJECT_1
    assert other.project_id == PROJECT_2


def test_create_memory_dedup_scopes_to_project(memory_manager, db) -> None:
    """Same content can be stored independently per project scope."""
    db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_1, "Project 1"))
    db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_2, "Project 2"))

    memory1 = memory_manager.create_memory(content="Scoped dedup test", project_id=PROJECT_1)
    memory2 = memory_manager.create_memory(content="Scoped dedup test", project_id=PROJECT_2)
    memory3 = memory_manager.create_memory(
        content="Scoped dedup test", project_id=PERSONAL_PROJECT_ID, is_global=True
    )
    memory4 = memory_manager.create_memory(content="Scoped dedup test", project_id=PROJECT_1)

    assert memory1.id != memory2.id
    assert memory1.id != memory3.id
    assert memory2.id != memory3.id
    assert memory4.id == memory1.id
    assert memory1.project_id == PROJECT_1
    assert memory2.project_id == PROJECT_2
    assert memory3.project_id == PERSONAL_PROJECT_ID
    assert memory3.is_global is True
    assert (
        memory_manager.get_memory_by_content(
            "Scoped dedup test", MemoryScope.project_visible(PROJECT_1)
        ).id
        == memory1.id
    )
    assert (
        memory_manager.get_memory_by_content(
            "Scoped dedup test", MemoryScope.project_visible(PROJECT_2)
        ).id
        == memory2.id
    )


def test_create_project_memory_dedups_against_visible_global(memory_manager, db) -> None:
    """A visible global memory wins over creating a project-local duplicate."""
    db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_1, "Project 1"))
    global_memory = memory_manager.create_memory(
        content="Visible global duplicate",
        project_id=PERSONAL_PROJECT_ID,
        is_global=True,
    )

    project_result = memory_manager.create_memory(
        content="Visible global duplicate",
        project_id=PROJECT_1,
    )

    assert project_result.id == global_memory.id
    assert project_result.project_id == PERSONAL_PROJECT_ID
    assert project_result.is_global is True
    row = db.fetchone("SELECT COUNT(*) AS cnt FROM memories WHERE project_id = %s", (PROJECT_1,))
    assert row is not None
    assert row["cnt"] == 0


def test_source_session_proximity_dedup_scopes_to_project(memory_manager, db) -> None:
    """Same-session proximity dedup does not cross project boundaries."""
    db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_1, "Project 1"))
    db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_2, "Project 2"))
    _insert_session(db, SESSION_1, PROJECT_1)

    first = memory_manager.create_memory(
        content="Same-session scoped memory",
        project_id=PROJECT_1,
        source_session_id=SESSION_1,
    )
    second = memory_manager.create_memory(
        content="Same-session scoped memory",
        project_id=PROJECT_2,
        source_session_id=SESSION_1,
    )

    assert second.id != first.id
    assert second.project_id == PROJECT_2


def test_source_session_proximity_dedup_ignores_deleted(memory_manager, db) -> None:
    """Deleted memories do not satisfy source-session proximity dedup."""
    db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_1, "Session Project"))
    _insert_session(db, SESSION_1, PROJECT_1)

    created = memory_manager.create_memory(
        content="Deleted same-session memory",
        source_session_id=SESSION_1,
    )
    assert memory_manager.delete_memory(created.id)

    revived = memory_manager.create_memory(
        content="Deleted same-session memory",
        source_session_id=SESSION_1,
    )
    row = db.fetchone("SELECT deleted_at FROM memories WHERE id = %s", (created.id,))

    assert revived.id == created.id
    assert row is not None
    assert row["deleted_at"] is None


def test_memory_exists(memory_manager) -> None:
    """Test memory_exists method."""
    memory = memory_manager.create_memory(content="Exists test")
    assert memory_manager.memory_exists(memory.id) is True
    assert memory_manager.memory_exists(UNKNOWN_MEMORY_ID) is False


def test_mark_pending_graphs_scopes_to_project(memory_manager, db) -> None:
    """Project-scoped resets should only affect memories in that project."""
    db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_1, "Project 1"))
    db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_2, "Project 2"))

    mem_proj1 = memory_manager.create_memory(content="Project 1 memory", project_id=PROJECT_1)
    mem_proj2 = memory_manager.create_memory(content="Project 2 memory", project_id=PROJECT_2)
    mem_global = memory_manager.create_memory(
        content="Global memory", project_id=PERSONAL_PROJECT_ID, is_global=True
    )

    updated = memory_manager.mark_pending_graphs(MemoryScope.project_only(PROJECT_1))

    assert updated == 1
    rows = db.fetchall(
        "SELECT id, graph_processed FROM memories WHERE id IN (%s, %s, %s)",
        (mem_proj1.id, mem_proj2.id, mem_global.id),
    )
    by_id = {row["id"]: row["graph_processed"] for row in rows}
    assert by_id[mem_proj1.id] == 0
    assert by_id[mem_proj2.id] == 1
    assert by_id[mem_global.id] == 1


def test_mark_pending_graphs_without_project_resets_all(memory_manager, db) -> None:
    """Global resets should mark every memory as pending KG processing."""
    db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_1, "Project 1"))
    db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_2, "Project 2"))

    mem_proj1 = memory_manager.create_memory(content="Reset all project 1", project_id=PROJECT_1)
    mem_proj2 = memory_manager.create_memory(content="Reset all project 2", project_id=PROJECT_2)
    mem_global = memory_manager.create_memory(
        content="Reset all global", project_id=PERSONAL_PROJECT_ID, is_global=True
    )

    updated = memory_manager.mark_pending_graphs()

    assert updated == 3
    rows = db.fetchall(
        "SELECT id, graph_processed FROM memories WHERE id IN (%s, %s, %s)",
        (mem_proj1.id, mem_proj2.id, mem_global.id),
    )
    assert {row["graph_processed"] for row in rows} == {0}


def test_deterministic_graph_failures_become_terminal_at_cap(memory_manager, db) -> None:
    """A deterministic poison memory leaves the pending queue at the configured cap."""
    memory = memory_manager.create_memory(content="Deterministic poison")
    memory_manager.mark_pending_graph(memory.id)

    assert (
        memory_manager.record_graph_failure(memory.id, deterministic=True, max_attempts=3)
        == "pending"
    )
    assert (
        memory_manager.record_graph_failure(memory.id, deterministic=True, max_attempts=3)
        == "pending"
    )
    assert (
        memory_manager.record_graph_failure(memory.id, deterministic=True, max_attempts=3)
        == "failed"
    )

    row = db.fetchone(
        "SELECT graph_processed, graph_attempts, graph_status FROM memories WHERE id = %s",
        (memory.id,),
    )
    assert row is not None
    assert row["graph_processed"] is True
    assert row["graph_attempts"] == 3
    assert row["graph_status"] == "failed"
    assert memory_manager.get_pending_graph_memories() == []


def test_terminal_poison_memory_does_not_starve_newer_pending_memory(memory_manager, db) -> None:
    """After terminal failure, the queue advances beyond its former oldest row."""
    poison = memory_manager.create_memory(content="Old poison")
    newer = memory_manager.create_memory(content="New valid memory")
    db.execute(
        "UPDATE memories SET created_at = created_at - INTERVAL '1 hour' WHERE id = %s",
        (poison.id,),
    )
    memory_manager.mark_pending_graph(poison.id)
    memory_manager.mark_pending_graph(newer.id)

    memory_manager.record_graph_failure(poison.id, deterministic=True, max_attempts=1)

    pending = memory_manager.get_pending_graph_memories(limit=1)
    assert [memory.id for memory in pending] == [newer.id]


def test_transient_graph_failure_stays_pending_without_consuming_attempt(
    memory_manager, db
) -> None:
    """Retryable, partial, and unexpected failures retain their queue position and budget."""
    memory = memory_manager.create_memory(content="Transient graph outage")
    memory_manager.mark_pending_graph(memory.id)

    assert (
        memory_manager.record_graph_failure(memory.id, deterministic=False, max_attempts=3)
        == "pending"
    )

    row = db.fetchone(
        "SELECT graph_processed, graph_attempts, graph_status FROM memories WHERE id = %s",
        (memory.id,),
    )
    assert row is not None
    assert row["graph_processed"] is False
    assert row["graph_attempts"] == 0
    assert row["graph_status"] == "pending"
    assert [item.id for item in memory_manager.get_pending_graph_memories()] == [memory.id]


def test_mark_graph_processed_resets_retry_state(memory_manager, db) -> None:
    """Success and no-entity outcomes complete the queue row and clear old attempts."""
    memory = memory_manager.create_memory(content="Eventually succeeds")
    memory_manager.mark_pending_graph(memory.id)
    memory_manager.record_graph_failure(memory.id, deterministic=True, max_attempts=3)

    memory_manager.mark_graph_processed(memory.id)

    row = db.fetchone(
        "SELECT graph_processed, graph_attempts, graph_status FROM memories WHERE id = %s",
        (memory.id,),
    )
    assert row is not None
    assert row["graph_processed"] is True
    assert row["graph_attempts"] == 0
    assert row["graph_status"] == "completed"


def test_list_live_ids_applies_offset_without_limit(
    memory_manager: LocalMemoryManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetchall = MagicMock(return_value=[{"id": "mem-3"}])
    monkeypatch.setattr(memory_manager.db, "fetchall", fetchall)

    assert memory_manager.list_live_ids(offset=2) == ["mem-3"]
    fetchall.assert_called_once_with(
        "SELECT id FROM memories WHERE deleted_at IS NULL ORDER BY id OFFSET %s",
        (2,),
    )


def test_list_live_ids_excludes_soft_deleted_memories(
    memory_manager: LocalMemoryManager,
    db: HubDatabase,
) -> None:
    live = memory_manager.create_memory(content="Live", project_id=PERSONAL_PROJECT_ID)
    deleted = memory_manager.create_memory(content="Soft deleted", project_id=PERSONAL_PROJECT_ID)
    db.execute("UPDATE memories SET deleted_at = NOW() WHERE id = %s", (deleted.id,))

    assert memory_manager.list_live_ids() == [live.id]


def test_content_exists_with_project(memory_manager, db) -> None:
    """Test content_exists method with project_id."""
    db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_1, "Project 1"))

    memory_manager.create_memory(content="Project content", project_id=PROJECT_1)

    # Same content with same project should exist
    assert (
        memory_manager.content_exists("Project content", MemoryScope.project_visible(PROJECT_1))
        is True
    )

    # Same content with different project should not exist in that project scope
    assert (
        memory_manager.content_exists(
            "Project content", MemoryScope.project_visible(UNKNOWN_PROJECT_ID)
        )
        is False
    )

    # Different content should not exist
    assert (
        memory_manager.content_exists("Other content", MemoryScope.project_visible(PROJECT_1))
        is False
    )


def test_content_lookup_uses_project_plus_global_visibility(memory_manager, db) -> None:
    """Existence and retrieval use identical project-plus-global precedence."""
    db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_1, "Project 1"))
    db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_2, "Project 2"))
    global_memory = memory_manager.create_memory(
        content="Shared visible", project_id=PERSONAL_PROJECT_ID, is_global=True
    )
    project_memory = memory_manager.create_memory(content="Project only", project_id=PROJECT_1)

    project_2_scope = MemoryScope.project_visible(PROJECT_2)
    project_1_scope = MemoryScope.project_visible(PROJECT_1)
    assert memory_manager.content_exists("Shared visible", project_2_scope) is True
    assert (
        memory_manager.get_memory_by_content("Shared visible", project_2_scope).id
        == global_memory.id
    )
    assert memory_manager.content_exists("Project only", project_2_scope) is False
    assert memory_manager.get_memory_by_content("Project only", project_2_scope) is None
    assert memory_manager.content_exists("Project only", project_1_scope) is True
    assert (
        memory_manager.get_memory_by_content("Project only", project_1_scope).id
        == project_memory.id
    )


def test_global_content_lookup_does_not_match_project_memory(memory_manager, db) -> None:
    """Creating or querying global scope does not absorb a project-local memory."""
    db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_1, "Project 1"))
    project_memory = memory_manager.create_memory(content="Scope direction", project_id=PROJECT_1)

    assert memory_manager.content_exists("Scope direction", GLOBAL_MEMORIES) is False
    assert memory_manager.get_memory_by_content("Scope direction", GLOBAL_MEMORIES) is None

    global_memory = memory_manager.create_memory(
        content="Scope direction", project_id=PERSONAL_PROJECT_ID, is_global=True
    )
    assert global_memory.id != project_memory.id
    assert global_memory.project_id == PERSONAL_PROJECT_ID
    assert global_memory.is_global is True


def test_content_exists_without_project(memory_manager) -> None:
    """Test content_exists method without project_id."""
    memory_manager.create_memory(
        content="Global content", project_id=PERSONAL_PROJECT_ID, is_global=True
    )

    # Same content without project should exist
    assert memory_manager.content_exists("Global content", GLOBAL_MEMORIES) is True

    # Different content should not exist
    assert memory_manager.content_exists("Different content", GLOBAL_MEMORIES) is False


def test_update_memory_individual_fields(memory_manager) -> None:
    """Test updating individual fields in update_memory."""
    memory = memory_manager.create_memory(
        content="Original content",
        tags=["original"],
    )

    updated_content = memory_manager.update_memory(memory.id, content="New content")
    assert updated_content.content == "New content"

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
    with pytest.raises(ValueError, match=f"Memory {UNKNOWN_MEMORY_ID} not found"):
        memory_manager.update_memory(UNKNOWN_MEMORY_ID, tags=["updated"])


def test_delete_memory_not_found(memory_manager) -> None:
    """Test delete_memory returns False for non-existent memory."""
    result = memory_manager.delete_memory(UNKNOWN_MEMORY_ID)
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


def test_list_and_count_reject_noncanonical_type(memory_manager) -> None:
    for invalid_type in ("debugging_pattern", ""):
        with pytest.raises(ValueError, match="Invalid memory_type"):
            memory_manager.list_memories(memory_type=invalid_type)
        with pytest.raises(ValueError, match="Invalid memory_type"):
            memory_manager.count_memories(memory_type=invalid_type)


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
    assert updated.last_accessed_at == datetime.fromisoformat(access_time)

    # Update again
    access_time2 = datetime.now(UTC).isoformat()
    memory_manager.update_access_stats(memory.id, access_time2)

    updated2 = memory_manager.get_memory(memory.id)
    assert updated2.access_count == 2
    assert updated2.last_accessed_at == datetime.fromisoformat(access_time2)


def test_search_memories_with_project(memory_manager, db) -> None:
    """Test search_memories with project_id filter."""
    db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_1, "Search Project"))

    memory_manager.create_memory(content="Project-specific fox", project_id=PROJECT_1)
    memory_manager.create_memory(
        content="Global fox", project_id=PERSONAL_PROJECT_ID, is_global=True
    )

    # Search with project filter should find both project-specific and global
    results = memory_manager.search_memories(
        query_text="fox", scope=MemoryScope.project_visible(PROJECT_1)
    )
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
    with pytest.raises(ValueError, match=f"Memory {UNKNOWN_MEMORY_ID} not found"):
        memory_manager.get_memory(UNKNOWN_MEMORY_ID)


def test_memory_from_row_with_null_tags(memory_manager) -> None:
    """Test Memory.from_row handles null tags correctly."""
    # Create a memory without tags
    memory = memory_manager.create_memory(content="No tags", tags=None)
    assert memory.tags == []


def test_memory_from_row_accepts_jsonb_tags_list() -> None:
    """Test Memory.from_row accepts tags already decoded from JSONB."""
    memory = Memory.from_row(
        {
            "id": "mem-1",
            "memory_type": "fact",
            "content": "Tagged memory",
            "created_at": "2026-06-20T00:00:00+00:00",
            "updated_at": "2026-06-20T00:00:00+00:00",
            "project_id": "proj-1",
            "source_type": "agent",
            "source_session_id": None,
            "access_count": 0,
            "last_accessed_at": None,
            "tags": ["alpha", "beta"],
        }
    )

    assert memory.tags == ["alpha", "beta"]


def test_create_memory_with_all_fields(memory_manager, db) -> None:
    """Test creating a memory with all optional fields set."""
    db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_1, "Full Project"))
    # Insert a valid session to satisfy foreign key constraint
    _insert_session(db, SESSION_1, PROJECT_1)

    memory = memory_manager.create_memory(
        content="Full memory",
        memory_type="context",
        project_id=PROJECT_1,
        source_type="agent",
        source_session_id=SESSION_1,
        tags=["tag1", "tag2", "tag3"],
    )

    assert memory.content == "Full memory"
    assert memory.memory_type == "context"
    assert memory.project_id == PROJECT_1
    assert memory.source_type == "agent"
    assert memory.source_session_id == SESSION_1
    assert memory.tags == ["tag1", "tag2", "tag3"]


def test_list_memories_combined_filters(memory_manager, db) -> None:
    """Test list_memories with multiple filters combined."""
    db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_1, "Combo Project"))

    memory_manager.create_memory(
        content="Fact one",
        memory_type="fact",
        project_id=PROJECT_1,
    )
    memory_manager.create_memory(
        content="Fact two",
        memory_type="fact",
        project_id=PROJECT_1,
    )
    memory_manager.create_memory(
        content="Preference one",
        memory_type="preference",
        project_id=PROJECT_1,
    )

    # Filter by project and type
    results = memory_manager.list_memories(
        scope=MemoryScope.project_visible(PROJECT_1), memory_type="fact"
    )
    assert len(results) == 2
    assert all(r.memory_type == "fact" for r in results)


def test_list_memories_tag_filter_fetches_additional_pages(memory_manager, db) -> None:
    matching = [
        memory_manager.create_memory(content=f"tag pagination keep {index}", tags=["keep"])
        for index in range(2)
    ]
    skipped = [
        memory_manager.create_memory(content=f"tag pagination skip {index}", tags=["skip"])
        for index in range(55)
    ]

    base = datetime(2026, 1, 1, tzinfo=UTC)
    for index, memory in enumerate(matching):
        db.execute(
            "UPDATE memories SET updated_at = %s WHERE id = %s",
            ((base + timedelta(minutes=index)).isoformat(), memory.id),
        )
    for index, memory in enumerate(skipped):
        db.execute(
            "UPDATE memories SET updated_at = %s WHERE id = %s",
            ((base + timedelta(hours=1, minutes=index)).isoformat(), memory.id),
        )

    results = memory_manager.list_memories(tags_any=["keep"], limit=2)

    assert {memory.content for memory in results} == {
        "tag pagination keep 0",
        "tag pagination keep 1",
    }


def test_visibility_predicate_rejects_unknown_column() -> None:
    with pytest.raises(ValueError, match="Invalid visibility column"):
        visibility_predicate("active", column="deleted_at OR 1=1")


# --- Dream soft-delete visibility (migration 289) -------------------------


def _hide(db, memory_id: str, action: str = "review") -> None:
    """Soft-hide a memory the way dream's mark_dreamed will (direct SQL)."""
    db.execute(
        "UPDATE memories SET deleted_at = NOW(), dream_action = %s WHERE id = %s",
        (action, memory_id),
    )


def test_memory_roundtrips_dream_soft_delete_fields(memory_manager, db) -> None:
    created = memory_manager.create_memory(content="round-trip me")
    _hide(db, created.id, "delete")

    hidden = memory_manager.get_memory(created.id, visibility="all")
    assert hidden.deleted_at is not None
    assert hidden.dream_action == "delete"

    data = hidden.to_dict()
    assert data["dream_action"] == "delete"
    assert "deleted_at" in data
    assert "last_dreamed_at" in data


def test_get_memory_visibility_filters(memory_manager, db) -> None:
    created = memory_manager.create_memory(content="hide visibility get")
    _hide(db, created.id)

    # active (default) hides it
    with pytest.raises(ValueError, match="not found"):
        memory_manager.get_memory(created.id)
    # hidden / all surface it
    assert memory_manager.get_memory(created.id, visibility="hidden").id == created.id
    assert memory_manager.get_memory(created.id, visibility="all").id == created.id


def test_get_memories_visibility(memory_manager, db) -> None:
    visible = memory_manager.create_memory(content="batch visible")
    hidden = memory_manager.create_memory(content="batch hidden")
    _hide(db, hidden.id)

    active = memory_manager.get_memories([visible.id, hidden.id])
    assert {m.id for m in active} == {visible.id}
    both = memory_manager.get_memories([visible.id, hidden.id], visibility="all")
    assert {m.id for m in both} == {visible.id, hidden.id}


def test_list_count_search_visibility(memory_manager, db) -> None:
    visible = memory_manager.create_memory(content="visible alpha")
    hidden = memory_manager.create_memory(content="hidden alpha")
    _hide(db, hidden.id)

    active_ids = {m.id for m in memory_manager.list_memories()}
    assert visible.id in active_ids
    assert hidden.id not in active_ids
    assert {m.id for m in memory_manager.list_memories(visibility="hidden")} == {hidden.id}
    assert {visible.id, hidden.id} <= {m.id for m in memory_manager.list_memories(visibility="all")}

    assert memory_manager.count_memories(visibility="active") == 1
    assert memory_manager.count_memories(visibility="hidden") == 1
    assert memory_manager.count_memories(visibility="all") == 2

    assert {m.id for m in memory_manager.search_memories(query_text="alpha")} == {visible.id}
    all_search = memory_manager.search_memories(query_text="alpha", visibility="all")
    assert {m.id for m in all_search} == {visible.id, hidden.id}


def test_count_memories_filters_by_memory_type(memory_manager) -> None:
    memory_manager.create_memory(content="fact row", memory_type="fact")
    memory_manager.create_memory(content="preference row", memory_type="preference")

    assert memory_manager.count_memories(memory_type="fact") == 1
    assert memory_manager.count_memories(memory_type="preference") == 1
    assert memory_manager.count_memories(memory_type="pattern") == 0


def test_visibility_predicate_rejects_unknown() -> None:
    from gobby.storage.memories import visibility_predicate

    assert visibility_predicate("active") == "deleted_at IS NULL"
    assert visibility_predicate("hidden") == "deleted_at IS NOT NULL"
    assert visibility_predicate("all") == ""
    with pytest.raises(ValueError, match="Invalid visibility"):
        visibility_predicate("bogus")  # type: ignore[arg-type]


def test_restore_memory_row_tolerates_pre_289_snapshot(memory_manager, db) -> None:
    from gobby.memory.dream.storage import MemoryDreamStore

    store = MemoryDreamStore(db)
    created = memory_manager.create_memory(content="legacy snapshot")
    snapshot = store.get_memory_row(created.id)
    assert snapshot is not None

    # Simulate a snapshot captured before migration 289 added the columns.
    for column in ("deleted_at", "dream_action", "last_dreamed_at"):
        snapshot.pop(column, None)
    db.execute("DELETE FROM memories WHERE id = %s", (created.id,))

    store.restore_memory_row(snapshot)  # must not raise on the missing columns

    restored = memory_manager.get_memory(created.id)
    assert restored.content == "legacy snapshot"
    assert restored.deleted_at is None
    assert restored.dream_action is None
    assert restored.last_dreamed_at is None


def test_mark_dreamed_stamps_without_hiding_or_bumping_updated_at(memory_manager, db) -> None:
    created = memory_manager.create_memory(content="keep me current")
    before = db.fetchone("SELECT updated_at FROM memories WHERE id = %s", (created.id,))

    memory_manager.mark_dreamed(created.id, when=datetime.now(UTC).isoformat())

    row = db.fetchone(
        "SELECT updated_at, last_dreamed_at, deleted_at, dream_action FROM memories WHERE id = %s",
        (created.id,),
    )
    # GC bookkeeping must never bump recency / temporal decay.
    assert row["updated_at"] == before["updated_at"]
    assert row["last_dreamed_at"] is not None
    assert row["deleted_at"] is None
    assert row["dream_action"] is None
    # Stays visible to agents (the "keep" case).
    assert memory_manager.get_memory(created.id).id == created.id


def test_mark_dreamed_soft_hides_without_bumping_updated_at(memory_manager, db) -> None:
    created = memory_manager.create_memory(content="hide me")
    before = db.fetchone("SELECT updated_at FROM memories WHERE id = %s", (created.id,))

    memory_manager.mark_dreamed(created.id, hidden_as="delete")

    row = db.fetchone(
        "SELECT updated_at, last_dreamed_at, deleted_at, dream_action FROM memories WHERE id = %s",
        (created.id,),
    )
    assert row["updated_at"] == before["updated_at"]
    assert row["deleted_at"] is not None
    assert row["dream_action"] == "delete"
    assert row["last_dreamed_at"] is not None
    # Hidden from agent-facing reads.
    with pytest.raises(ValueError, match="not found"):
        memory_manager.get_memory(created.id)


def test_mark_dreamed_missing_raises(memory_manager) -> None:
    with pytest.raises(ValueError, match="not found"):
        memory_manager.mark_dreamed("00000000-0000-0000-0000-000000000000")


def test_restore_memory_reactivates_hidden_row(memory_manager) -> None:
    created = memory_manager.create_memory(content="bring me back")
    memory_manager.mark_dreamed(created.id, hidden_as="review")
    with pytest.raises(ValueError, match="not found"):
        memory_manager.get_memory(created.id)

    memory_manager.restore_memory(created.id)

    restored = memory_manager.get_memory(created.id)
    assert restored.id == created.id
    assert restored.deleted_at is None
    assert restored.dream_action is None
    # Stamped so the next sweep's cooldown leaves it alone for a while.
    assert restored.last_dreamed_at is not None


def test_restore_memory_missing_raises(memory_manager) -> None:
    with pytest.raises(ValueError, match="not found"):
        memory_manager.restore_memory("00000000-0000-0000-0000-000000000000")


def test_purge_dream_hidden_removes_only_aged_rows_of_action(memory_manager) -> None:
    old = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    recent = datetime.now(UTC).isoformat()

    aged_delete = memory_manager.create_memory(content="aged delete")
    memory_manager.mark_dreamed(aged_delete.id, hidden_as="delete", when=old)
    recent_delete = memory_manager.create_memory(content="recent delete")
    memory_manager.mark_dreamed(recent_delete.id, hidden_as="delete", when=recent)
    aged_review = memory_manager.create_memory(content="aged review")
    memory_manager.mark_dreamed(aged_review.id, hidden_as="review", when=old)

    purged = memory_manager.purge_dream_hidden("delete", older_than_days=30)

    assert purged == [aged_delete.id]
    assert not memory_manager.memory_exists(aged_delete.id)  # physically gone
    assert memory_manager.memory_exists(recent_delete.id)  # within grace
    assert memory_manager.memory_exists(aged_review.id)  # different action


def test_prune_runs_drops_aged_runs_and_cascades_snapshots(db) -> None:
    from gobby.memory.dream.storage import MemoryDreamStore

    store = MemoryDreamStore(db)
    # project_id is left NULL — memory_dream_runs.project_id carries an FK to projects.
    aged = store.create_run(project_id=None, dry_run=False, options={})
    store.update_run(aged, status="completed")
    fresh = store.create_run(project_id=None, dry_run=False, options={})
    aged_memory_id = str(uuid.uuid4())
    fresh_memory_id = str(uuid.uuid4())
    aged_snapshot = store.insert_snapshot(
        run_id=aged,
        memory_id=aged_memory_id,
        action="delete",
        before_data={"id": aged_memory_id},
    )
    store.complete_snapshot(aged_snapshot, after_data=None)
    fresh_snapshot = store.insert_snapshot(
        run_id=fresh,
        memory_id=fresh_memory_id,
        action="review",
        before_data={"id": fresh_memory_id},
    )
    store.complete_snapshot(fresh_snapshot, after_data=None)

    # Backdate the aged run beyond the retention window.
    old_stamp = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    store.update_run(aged, created_at=old_stamp)

    removed = store.prune_runs(older_than_days=30)

    assert removed == 1
    assert store.get_run(aged) is None
    assert store.get_run(fresh) is not None
    # The pruned run's snapshot cascades out; the fresh run keeps its history.
    assert store.list_snapshots(aged) == []
    assert len(store.list_snapshots(fresh)) == 1


def test_prune_runs_removes_forfeited_runs_and_preserves_active_runs(db: HubDatabase) -> None:
    from gobby.memory.dream.storage import MemoryDreamStore

    store = MemoryDreamStore(db)
    forfeited = store.create_run(project_id=None, dry_run=False, options={})
    store.update_run(forfeited, status="completed")
    active = store.create_run(project_id=None, dry_run=False, options={})
    store.update_run(forfeited, status="revert_forfeited")
    store.update_run(active, status="running")
    old_stamp = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    store.update_run(forfeited, created_at=old_stamp)
    store.update_run(active, created_at=old_stamp)

    removed = store.prune_runs(older_than_days=30)

    assert removed == 1
    assert store.get_run(forfeited) is None
    assert store.get_run(active) is not None


def test_list_dream_candidates_active_only_and_cooldown(memory_manager) -> None:
    cutoff = (datetime.now(UTC) - timedelta(hours=20)).isoformat()
    before_cutoff = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    after_cutoff = datetime.now(UTC).isoformat()

    never = memory_manager.create_memory(content="never dreamed")
    stale = memory_manager.create_memory(content="dreamed long ago")
    memory_manager.mark_dreamed(stale.id, when=before_cutoff)
    fresh = memory_manager.create_memory(content="dreamed just now")
    memory_manager.mark_dreamed(fresh.id, when=after_cutoff)
    hidden = memory_manager.create_memory(content="hidden one")
    memory_manager.mark_dreamed(hidden.id, hidden_as="delete", when=before_cutoff)

    page = memory_manager.list_dream_candidates(limit=50, redream_cutoff=cutoff, scope=ALL_MEMORIES)
    ids = [m.id for m in page]

    assert never.id in ids
    assert stale.id in ids
    assert fresh.id not in ids  # dreamed within the cooldown window
    assert hidden.id not in ids  # soft-hidden rows are never candidates
    # Never-dreamed sorts ahead of previously-dreamed (NULLS FIRST).
    assert ids.index(never.id) < ids.index(stale.id)


def test_list_dream_candidates_excludes_review_lesson_patterns(memory_manager) -> None:
    cutoff = datetime.now(UTC).isoformat()
    protected = memory_manager.create_memory(
        content="Repeated review finding",
        memory_type="pattern",
        tags=["review-lesson", "pattern:sql-placeholders", "occurrence:review-1"],
    )
    ordinary = memory_manager.create_memory(content="ordinary candidate")

    page = memory_manager.list_dream_candidates(limit=50, redream_cutoff=cutoff, scope=ALL_MEMORIES)
    ids = {memory.id for memory in page}

    assert protected.id not in ids
    assert ordinary.id in ids


def test_list_dream_candidates_limit(memory_manager) -> None:
    cutoff = datetime.now(UTC).isoformat()
    for i in range(3):
        memory_manager.create_memory(content=f"candidate {i}")
    page = memory_manager.list_dream_candidates(limit=2, redream_cutoff=cutoff, scope=ALL_MEMORIES)
    assert len(page) == 2


def test_list_dream_candidates_project_and_global_scope(memory_manager, db) -> None:
    db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_1, "Project 1"))
    db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_2, "Project 2"))
    cutoff = datetime.now(UTC).isoformat()

    glob = memory_manager.create_memory(
        content="global fact", project_id=PERSONAL_PROJECT_ID, is_global=True
    )
    proj1 = memory_manager.create_memory(content="project one fact", project_id=PROJECT_1)
    proj2 = memory_manager.create_memory(content="project two fact", project_id=PROJECT_2)

    def ids(scope: MemoryScope = ALL_MEMORIES) -> set[str]:
        page = memory_manager.list_dream_candidates(limit=50, redream_cutoff=cutoff, scope=scope)
        return {m.id for m in page}

    # Project-visible means owner rows plus globals.
    assert ids(MemoryScope.project_visible(PROJECT_1)) == {glob.id, proj1.id}
    # Project only.
    assert ids(MemoryScope.project_only(PROJECT_1)) == {proj1.id}
    # Global-only returns only explicitly global rows.
    assert ids(GLOBAL_MEMORIES) == {glob.id}
    # The all scope sweeps every row.
    assert {glob.id, proj1.id, proj2.id} <= ids()


def test_list_dream_scopes_returns_due_distinct_scopes(memory_manager, db) -> None:
    project_a = str(uuid.uuid4())
    project_b = str(uuid.uuid4())
    project_fresh = str(uuid.uuid4())
    project_hidden = str(uuid.uuid4())
    for project_id in [project_a, project_b, project_fresh, project_hidden]:
        db.execute(
            "INSERT INTO projects (id, name) VALUES (%s, %s)",
            (project_id, f"Project {project_id}"),
        )
    cutoff = (datetime.now(UTC) - timedelta(hours=20)).isoformat()
    before_cutoff = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    after_cutoff = datetime.now(UTC).isoformat()

    memory_manager.create_memory(content="never dreamed project b", project_id=project_b)
    stale = memory_manager.create_memory(content="stale project a", project_id=project_a)
    memory_manager.mark_dreamed(stale.id, when=before_cutoff)
    fresh = memory_manager.create_memory(content="fresh project", project_id=project_fresh)
    memory_manager.mark_dreamed(fresh.id, when=after_cutoff)
    hidden = memory_manager.create_memory(content="hidden project", project_id=project_hidden)
    memory_manager.mark_dreamed(hidden.id, hidden_as="delete", when=before_cutoff)
    memory_manager.create_memory(
        content="global due", project_id=PERSONAL_PROJECT_ID, is_global=True
    )

    assert memory_manager.list_dream_scopes(redream_cutoff=cutoff) == [
        MemoryScope.project_only(project_id) for project_id in sorted([project_a, project_b])
    ] + [GLOBAL_MEMORIES]


def test_list_dream_scopes_far_future_cutoff_returns_all_with_memories(memory_manager, db) -> None:
    """Far-future cutoff enumerates every project with live memories.

    Codewiki freshness registration uses this idiom: a cutoff past every
    possible ``last_dreamed_at`` makes each live memory "due", so the result is
    exactly the set of project-only scopes (plus the global scope) the per-project
    dream sweep will judge. Soft-deleted-only projects stay excluded.
    """
    project_dreamed = str(uuid.uuid4())
    project_hidden = str(uuid.uuid4())
    for project_id in [project_dreamed, project_hidden]:
        db.execute(
            "INSERT INTO projects (id, name) VALUES (%s, %s)",
            (project_id, f"Project {project_id}"),
        )
    far_future = "9999-12-31T23:59:59+00:00"
    just_now = datetime.now(UTC).isoformat()

    fresh = memory_manager.create_memory(content="freshly dreamed", project_id=project_dreamed)
    memory_manager.mark_dreamed(fresh.id, when=just_now)
    hidden = memory_manager.create_memory(content="hidden only", project_id=project_hidden)
    memory_manager.mark_dreamed(hidden.id, hidden_as="delete", when=just_now)
    memory_manager.create_memory(
        content="global memory", project_id=PERSONAL_PROJECT_ID, is_global=True
    )

    result = memory_manager.list_dream_scopes(redream_cutoff=far_future)

    # Freshly-dreamed project is included (cooldown neutralized) alongside the
    # explicit global scope; the soft-deleted-only project is excluded.
    assert MemoryScope.project_only(project_dreamed) in result
    assert GLOBAL_MEMORIES in result
    assert MemoryScope.project_only(project_hidden) not in result


def test_mark_project_memories_due_resets_only_live_project_rows(memory_manager, db) -> None:
    """The truth-change force-due touches only a project's stamped, live rows.

    Never-dreamed rows are already due, soft-hidden rows must stay hidden, and
    other projects' cooldowns must not bleed — the trigger is per-project.
    """
    project_a = str(uuid.uuid4())
    project_b = str(uuid.uuid4())
    for project_id in [project_a, project_b]:
        db.execute(
            "INSERT INTO projects (id, name) VALUES (%s, %s)",
            (project_id, f"Project {project_id}"),
        )
    when = datetime.now(UTC).isoformat()

    dreamed = memory_manager.create_memory(content="a dreamed", project_id=project_a)
    memory_manager.mark_dreamed(dreamed.id, when=when)
    never = memory_manager.create_memory(content="a never dreamed", project_id=project_a)
    hidden = memory_manager.create_memory(content="a hidden", project_id=project_a)
    memory_manager.mark_dreamed(hidden.id, hidden_as="delete", when=when)
    other = memory_manager.create_memory(content="b dreamed", project_id=project_b)
    memory_manager.mark_dreamed(other.id, when=when)
    global_memory = memory_manager.create_memory(
        content="global dreamed", project_id=PERSONAL_PROJECT_ID, is_global=True
    )
    memory_manager.mark_dreamed(global_memory.id, when=when)

    affected = memory_manager.mark_project_memories_due(project_a)

    # Only the stamped, live, project-a row is reset.
    assert affected == 1
    assert memory_manager.get_memory(dreamed.id).last_dreamed_at is None
    assert memory_manager.get_memory(never.id).last_dreamed_at is None  # already NULL
    # The other project's cooldown is untouched (no cross-project bleed).
    assert memory_manager.get_memory(other.id).last_dreamed_at is not None
    # Global rows are reset by platform-truth changes, not project-truth changes.
    assert memory_manager.get_memory(global_memory.id).last_dreamed_at is not None
    # The soft-hidden row stays hidden regardless.
    with pytest.raises(ValueError, match=f"Memory {hidden.id} not found"):
        memory_manager.get_memory(hidden.id)

    global_affected = memory_manager.mark_global_memories_due()

    assert global_affected == 1
    assert memory_manager.get_memory(global_memory.id).last_dreamed_at is None


def test_list_dream_candidates_memory_type_scope(memory_manager) -> None:
    cutoff = datetime.now(UTC).isoformat()
    fact = memory_manager.create_memory(content="a fact", memory_type="fact")
    pref = memory_manager.create_memory(content="a preference", memory_type="preference")

    page = memory_manager.list_dream_candidates(
        limit=50,
        redream_cutoff=cutoff,
        scope=ALL_MEMORIES,
        memory_type="preference",
    )
    ids = {m.id for m in page}
    assert pref.id in ids
    assert fact.id not in ids


def test_create_memory_restores_hidden_duplicate(
    memory_manager: LocalMemoryManager,
) -> None:
    created = memory_manager.create_memory(
        content="reactivate via recreate",
        project_id=PERSONAL_PROJECT_ID,
    )
    memory_manager.mark_dreamed(created.id, hidden_as="delete")
    with pytest.raises(ValueError, match="not found"):
        memory_manager.get_memory(created.id)

    # Re-creating identical content collides on the deterministic uuid5 id and
    # must reactivate the hidden row instead of returning an invisible memory.
    recreated = memory_manager.create_memory(
        content="  reactivate via recreate  ",
        project_id=PERSONAL_PROJECT_ID,
    )
    assert recreated.id == created.id
    assert recreated.deleted_at is None
    assert recreated.dream_action is None
    assert memory_manager.get_memory(created.id).id == created.id


def test_create_memory_persists_rationale_and_provenance(
    memory_manager: LocalMemoryManager, db: HubDatabase
) -> None:
    _insert_task(db, TASK_1)
    created = memory_manager.create_memory(
        content="Fresh rationale memory",
        project_id=PERSONAL_PROJECT_ID,
        rationale="Need this for later recall",
        source_task_id=TASK_1,
        created_by_agent="backend-developer",
    )
    loaded = memory_manager.get_memory(created.id)
    assert loaded.rationale == "Need this for later recall"
    assert loaded.source_task_id == TASK_1
    assert loaded.created_by_agent == "backend-developer"
    dumped = loaded.to_dict()
    assert dumped["rationale"] == "Need this for later recall"
    assert dumped["source_task_id"] == TASK_1
    assert dumped["created_by_agent"] == "backend-developer"

    rehydrated = Memory.from_row(
        {
            "id": created.id,
            "memory_type": "fact",
            "content": "Fresh rationale memory",
            "created_at": loaded.created_at,
            "updated_at": loaded.updated_at,
            "project_id": loaded.project_id,
            "source_type": "agent",
            "source_session_id": None,
            "access_count": 0,
            "last_accessed_at": None,
            "tags": None,
            "rationale": "Need this for later recall",
            "source_task_id": uuid.UUID(TASK_1),
            "created_by_agent": "backend-developer",
        }
    )
    assert rehydrated.rationale == "Need this for later recall"
    assert rehydrated.source_task_id == TASK_1
    assert rehydrated.created_by_agent == "backend-developer"

    restore_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1"
    restored = memory_manager.create_memory(
        content="Restore metadata memory",
        project_id=PERSONAL_PROJECT_ID,
        rationale="Restored claim",
        source_task_id=TASK_1,
        created_by_agent="codex",
        memory_id=restore_id,
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
        updated_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    assert restored.id == restore_id
    assert restored.rationale == "Restored claim"
    assert restored.source_task_id == TASK_1
    assert restored.created_by_agent == "codex"

    winner = memory_manager.create_memory(
        content="Restore metadata memory updated",
        project_id=PERSONAL_PROJECT_ID,
        rationale="Winning claim",
        source_task_id=TASK_1,
        created_by_agent="grok",
        memory_id=restore_id,
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
        updated_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    assert winner.id == restore_id
    assert winner.content == "Restore metadata memory updated"
    assert winner.rationale == "Winning claim"
    assert winner.source_task_id == TASK_1
    assert winner.created_by_agent == "grok"


def test_duplicate_create_preserves_original_rationale(
    memory_manager: LocalMemoryManager, db: HubDatabase
) -> None:
    _insert_task(db, TASK_1)
    _insert_session(db, SESSION_1, PERSONAL_PROJECT_ID)

    first = memory_manager.create_memory(
        content="Duplicate rationale content",
        project_id=PERSONAL_PROJECT_ID,
        is_global=True,
        rationale="original",
        source_task_id=TASK_1,
        created_by_agent="first-agent",
    )
    second = memory_manager.create_memory(
        content="Duplicate rationale content",
        project_id=PERSONAL_PROJECT_ID,
        is_global=True,
        rationale="overwrite attempt",
        created_by_agent="second-agent",
    )
    assert second.id == first.id
    assert second.rationale == "original"
    assert second.source_task_id == TASK_1
    assert second.created_by_agent == "first-agent"

    prox = memory_manager.create_memory(
        content="Proximity rationale content",
        project_id=PERSONAL_PROJECT_ID,
        source_session_id=SESSION_1,
        rationale="prox-original",
        source_task_id=TASK_1,
        created_by_agent="prox-agent",
    )
    # Content-duplicate SQL matches stored content exactly; proximity compares
    # the stripped stored value, so a trailing-space row isolates that branch.
    db.execute("UPDATE memories SET content = %s WHERE id = %s", (prox.content + "  ", prox.id))
    prox2 = memory_manager.create_memory(
        content="Proximity rationale content",
        project_id=PERSONAL_PROJECT_ID,
        source_session_id=SESSION_1,
        rationale="prox-overwrite",
        created_by_agent="other-agent",
    )
    assert prox2.id == prox.id
    assert prox2.rationale == "prox-original"
    assert prox2.source_task_id == TASK_1
    assert prox2.created_by_agent == "prox-agent"


def test_restore_memory_row_round_trips_rationale_and_provenance(
    memory_manager: LocalMemoryManager, db: HubDatabase
) -> None:
    from gobby.memory.dream.storage import MemoryDreamStore

    _insert_task(db, TASK_1)
    store = MemoryDreamStore(db)
    created = memory_manager.create_memory(
        content="Dream journal rationale",
        project_id=PERSONAL_PROJECT_ID,
        rationale="snapshot claim",
        source_task_id=TASK_1,
        created_by_agent="dream-agent",
    )
    snapshot = store.get_memory_row(created.id)
    assert snapshot is not None
    assert snapshot["rationale"] == "snapshot claim"
    assert str(snapshot["source_task_id"]) == TASK_1
    assert snapshot["created_by_agent"] == "dream-agent"

    db.execute("DELETE FROM memories WHERE id = %s", (created.id,))
    store.restore_memory_row(snapshot)

    restored = memory_manager.get_memory(created.id)
    assert restored.rationale == "snapshot claim"
    assert restored.source_task_id == TASK_1
    assert restored.created_by_agent == "dream-agent"
