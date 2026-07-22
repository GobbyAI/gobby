from __future__ import annotations

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.config.persistence import MemoryConfig
from gobby.memory.backends.null import NullBackend
from gobby.memory.backends.storage_adapter import StorageAdapter
from gobby.memory.manager import MemoryManager
from gobby.memory.protocol import MemoryBackendProtocol
from gobby.memory.services import lifecycle as lifecycle_module
from gobby.memory.write_result import MemoryWriteResult
from gobby.storage.memories import LocalMemoryManager
from gobby.storage.memories_crud import MAX_SUPERSEDES_IDS
from gobby.storage.memories_models import PERSONAL_PROJECT_ID
from gobby.storage.memories_scope import MemoryScope
from gobby.storage.projects import LocalProjectManager


def test_supersedes_soft_hides_and_tags(temp_db) -> None:
    manager = LocalMemoryManager(temp_db)
    old_memory = manager.create_memory(
        content="The daemon uses the legacy memory path.",
        project_id=PERSONAL_PROJECT_ID,
    )

    new_memory = manager.create_memory(
        content="The daemon uses the reconciled memory path.",
        project_id=PERSONAL_PROJECT_ID,
        tags=["decision"],
        supersedes=[old_memory.id],
    )

    hidden = manager.get_memory(old_memory.id, visibility="all")
    assert hidden.deleted_at is not None
    assert new_memory.tags == ["decision", f"supersedes:{old_memory.id}"]


@pytest.mark.parametrize("invalid_case", ["missing", "cross_scope", "hidden"])
def test_supersedes_validation_rejects(temp_db, invalid_case: str) -> None:
    manager = LocalMemoryManager(temp_db)
    target_id = str(uuid.uuid4())
    if invalid_case == "cross_scope":
        other_project = str(uuid.uuid4())
        temp_db.execute(
            "INSERT INTO projects (id, name) VALUES (%s, %s)",
            (other_project, "Other project"),
        )
        target_id = manager.create_memory("Other scope", other_project).id
    elif invalid_case == "hidden":
        target_id = manager.create_memory("Already hidden", PERSONAL_PROJECT_ID).id
        manager.mark_dreamed(target_id, hidden_as="delete")

    before = manager.count_memories(visibility="all")
    with pytest.raises(ValueError):
        manager.create_memory(
            "Rejected replacement",
            PERSONAL_PROJECT_ID,
            supersedes=[target_id],
        )
    assert manager.count_memories(visibility="all") == before


def test_supersedes_rollback_on_failure(temp_db) -> None:
    manager = LocalMemoryManager(temp_db)
    first = manager.create_memory("First rollback target", PERSONAL_PROJECT_ID)
    second = manager.create_memory("Second rollback target", PERSONAL_PROJECT_ID)
    temp_db.execute(
        """
        CREATE FUNCTION fail_supersession_test() RETURNS trigger AS $$
        BEGIN
            IF NEW.content = 'Second rollback target' AND NEW.deleted_at IS NOT NULL THEN
                RAISE EXCEPTION 'injected supersession failure';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
    )
    temp_db.execute(
        """
        CREATE TRIGGER fail_supersession_test_trigger
        BEFORE UPDATE ON memories
        FOR EACH ROW EXECUTE FUNCTION fail_supersession_test()
        """
    )

    with pytest.raises(Exception, match="injected supersession failure"):
        manager.create_memory(
            "Rolled back replacement",
            PERSONAL_PROJECT_ID,
            supersedes=[first.id, second.id],
        )

    assert manager.get_memory(first.id).deleted_at is None
    assert manager.get_memory(second.id).deleted_at is None
    assert (
        temp_db.fetchone(
            "SELECT id FROM memories WHERE content = %s",
            ("Rolled back replacement",),
        )
        is None
    )


def test_supersedes_idempotent_retry(temp_db) -> None:
    manager = LocalMemoryManager(temp_db)
    first = manager.create_memory("First old fact", PERSONAL_PROJECT_ID)
    replacement = manager.create_memory(
        "Durable replacement",
        PERSONAL_PROJECT_ID,
        tags=["caller-a"],
        supersedes=[first.id],
    )

    retried = manager.create_memory(
        "Durable replacement",
        PERSONAL_PROJECT_ID,
        tags=["caller-b"],
        supersedes=[first.id],
    )
    assert retried.id == replacement.id
    assert retried.tags == ["caller-a", f"supersedes:{first.id}", "caller-b"]

    second = manager.create_memory("Second old fact", PERSONAL_PROJECT_ID)
    partial = manager.create_memory(
        "Durable replacement",
        PERSONAL_PROJECT_ID,
        tags=["caller-c"],
        supersedes=[first.id, second.id],
    )
    assert partial.tags == [
        "caller-a",
        f"supersedes:{first.id}",
        "caller-b",
        "caller-c",
        f"supersedes:{second.id}",
    ]
    assert manager.get_memory(second.id, visibility="all").deleted_at is not None


def test_supersedes_mixed_targets(temp_db) -> None:
    manager = LocalMemoryManager(temp_db)
    satisfied = manager.create_memory("Satisfied target", PERSONAL_PROJECT_ID)
    active = manager.create_memory("Active target", PERSONAL_PROJECT_ID)
    replacement = manager.create_memory(
        "Mixed replacement",
        PERSONAL_PROJECT_ID,
        supersedes=[satisfied.id],
    )

    mixed = manager.create_memory(
        replacement.content,
        PERSONAL_PROJECT_ID,
        supersedes=[satisfied.id, active.id],
    )
    assert mixed.id == replacement.id
    assert manager.get_memory(active.id, visibility="all").deleted_at is not None

    unrelated_hidden = manager.create_memory("Unrelated hidden", PERSONAL_PROJECT_ID)
    manager.mark_dreamed(unrelated_hidden.id, hidden_as="delete")
    with pytest.raises(ValueError, match="without matching provenance"):
        manager.create_memory(
            replacement.content,
            PERSONAL_PROJECT_ID,
            supersedes=[satisfied.id, unrelated_hidden.id],
        )


def test_supersedes_inverse_role_concurrency(temp_db) -> None:
    manager = LocalMemoryManager(temp_db)
    memory_x = manager.create_memory("Concurrent X", PERSONAL_PROJECT_ID)
    memory_y = manager.create_memory("Concurrent Y", PERSONAL_PROJECT_ID)

    with ThreadPoolExecutor(max_workers=2) as executor:
        future_a = executor.submit(
            manager.create_memory,
            memory_x.content,
            PERSONAL_PROJECT_ID,
            tags=["caller-a"],
            supersedes=[memory_y.id],
        )
        future_b = executor.submit(
            manager.create_memory,
            memory_y.content,
            PERSONAL_PROJECT_ID,
            tags=["caller-b"],
            supersedes=[memory_x.id],
        )
        future_a.result(timeout=5)
        future_b.result(timeout=5)

    final_x = manager.get_memory(memory_x.id, visibility="all")
    final_y = manager.get_memory(memory_y.id, visibility="all")
    assert "caller-a" in (final_x.tags or [])
    assert f"supersedes:{memory_y.id}" in (final_x.tags or [])
    assert "caller-b" in (final_y.tags or [])
    assert f"supersedes:{memory_x.id}" in (final_y.tags or [])

    same_target = manager.create_memory("Same target", PERSONAL_PROJECT_ID)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            manager.create_memory,
            "Same result",
            PERSONAL_PROJECT_ID,
            tags=["first"],
            supersedes=[same_target.id],
        )
        second = executor.submit(
            manager.create_memory,
            "Same result",
            PERSONAL_PROJECT_ID,
            tags=["second"],
            supersedes=[same_target.id],
        )
        first.result(timeout=5)
        second.result(timeout=5)
    same_result = manager.get_memory_by_content(
        "Same result",
        MemoryScope.project_visible(PERSONAL_PROJECT_ID),
    )
    assert same_result is not None
    assert {"first", "second"} <= set(same_result.tags or [])


def test_write_outcome_types(temp_db) -> None:
    manager = LocalMemoryManager(temp_db)
    created = manager.create_memory_with_outcome("Outcome memory", PERSONAL_PROJECT_ID)
    assert isinstance(created, MemoryWriteResult)
    assert created.outcome == "created"

    deduped = manager.create_memory_with_outcome("Outcome memory", PERSONAL_PROJECT_ID)
    assert deduped.outcome == "deduped"
    assert deduped.memory.id == created.memory.id

    manager.mark_dreamed(created.memory.id, hidden_as="delete")
    reactivated = manager.create_memory_with_outcome("Outcome memory", PERSONAL_PROJECT_ID)
    assert reactivated.outcome == "reactivated"
    assert reactivated.memory.deleted_at is None


def test_supersedes_cardinality_bounds(temp_db) -> None:
    manager = LocalMemoryManager(temp_db)
    with pytest.raises(ValueError, match="Invalid supersedes"):
        manager.create_memory("Malformed", PERSONAL_PROJECT_ID, supersedes=["bad-id"])
    with pytest.raises(ValueError, match="at most"):
        manager.create_memory(
            "Over cap",
            PERSONAL_PROJECT_ID,
            supersedes=[str(uuid.uuid4()) for _ in range(MAX_SUPERSEDES_IDS + 1)],
        )

    target = manager.create_memory("Repeated target", PERSONAL_PROJECT_ID)
    replacement = manager.create_memory(
        "Repeated ids collapse",
        PERSONAL_PROJECT_ID,
        supersedes=[target.id] * (MAX_SUPERSEDES_IDS + 1),
    )
    assert replacement.tags == [f"supersedes:{target.id}"]


def test_supersedes_listener_boundary(temp_db) -> None:
    manager = LocalMemoryManager(temp_db)
    first = manager.create_memory("Listener target", PERSONAL_PROJECT_ID)
    notifications: list[bool] = []
    manager.add_change_listener(lambda: notifications.append(True))

    manager.create_memory(
        "Listener replacement",
        PERSONAL_PROJECT_ID,
        supersedes=[first.id],
    )
    assert notifications == [True]

    manager.create_memory(
        "Listener replacement",
        PERSONAL_PROJECT_ID,
        supersedes=[first.id],
    )
    assert notifications == [True]


@pytest.mark.asyncio
async def test_backend_protocol_supersedes(temp_db) -> None:
    manager = LocalMemoryManager(temp_db)
    target = manager.create_memory("Backend target", PERSONAL_PROJECT_ID)
    adapter = StorageAdapter(manager)
    result = await adapter.create(
        "Backend replacement",
        supersedes=[target.id],
    )
    assert isinstance(adapter, MemoryBackendProtocol)
    assert result.outcome == "created"
    assert result.memory.tags == [f"supersedes:{target.id}"]
    assert manager.get_memory(target.id, visibility="all").deleted_at is not None

    null_backend = NullBackend()
    null_result = await null_backend.create(
        "Null replacement",
        supersedes=[target.id],
    )
    assert isinstance(null_backend, MemoryBackendProtocol)
    assert null_result.memory.tags == [f"supersedes:{target.id}"]


@pytest.mark.asyncio
async def test_hidden_row_secondary_write_fence(temp_db) -> None:
    vector_store = MagicMock()
    vector_store.upsert = AsyncMock()
    vector_store.search = AsyncMock(return_value=[])
    embed = AsyncMock(return_value=[0.1, 0.2])
    manager = MemoryManager(
        db=temp_db,
        config=MemoryConfig(enabled=True),
        vector_store=vector_store,
        embed_fn=embed,
    )
    hidden = manager.storage.create_memory("stale source", PERSONAL_PROJECT_ID)
    manager.storage.create_memory(
        "replacement",
        PERSONAL_PROJECT_ID,
        supersedes=[hidden.id],
    )

    indexed = await manager.restore_memory_indices(
        hidden.id,
        hidden.content,
        hidden.project_id,
        hidden.is_global,
        hidden.memory_type.value,
    )

    assert indexed is False
    assert manager.storage.get_memory(hidden.id, visibility="all").deleted_at is not None
    vector_store.upsert.assert_not_awaited()
    vector_store.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_supersedes_purges_secondary_indices(temp_db) -> None:
    vector_store = MagicMock()
    vector_store.upsert = AsyncMock()
    vector_store.delete = AsyncMock()
    manager = MemoryManager(
        db=temp_db,
        config=MemoryConfig(enabled=True),
        vector_store=vector_store,
        embed_fn=AsyncMock(return_value=[0.1, 0.2]),
    )
    old = await manager.create_memory(
        "old indexed memory",
        project_id=PERSONAL_PROJECT_ID,
    )
    vector_store.delete.reset_mock()

    replacement = await manager.create_memory(
        "new indexed memory",
        project_id=PERSONAL_PROJECT_ID,
        supersedes=[old.id],
    )

    assert manager.storage.get_memory(old.id, visibility="all").deleted_at is not None
    assert manager._lifecycle_service._vector_store is vector_store
    vector_store.delete.assert_awaited_once_with(old.id)
    assert replacement.id != old.id


@pytest.mark.asyncio
async def test_restore_after_supersede_rebuilds(temp_db) -> None:
    vector_store = MagicMock()
    vector_store.upsert = AsyncMock()
    vector_store.delete = AsyncMock()
    manager = MemoryManager(
        db=temp_db,
        config=MemoryConfig(enabled=True),
        vector_store=vector_store,
        embed_fn=AsyncMock(return_value=[0.1, 0.2]),
    )
    old = await manager.create_memory(
        "restore this memory",
        project_id=PERSONAL_PROJECT_ID,
    )
    await manager.create_memory(
        "temporary replacement",
        project_id=PERSONAL_PROJECT_ID,
        supersedes=[old.id],
    )
    assert manager.storage.restore_memory(old.id)
    restored = manager.storage.get_memory(old.id)
    vector_store.upsert.reset_mock()

    assert await manager.restore_memory_indices(
        restored.id,
        restored.content,
        restored.project_id,
        restored.is_global,
        restored.memory_type.value,
    )

    vector_store.upsert.assert_awaited_once()
    repaired = manager.storage.get_memory(restored.id)
    assert repaired.vector_needs_reindex is False
    assert repaired.graph_status == "pending"


@pytest.mark.asyncio
async def test_supersedes_row_lock_fencing(temp_db) -> None:
    purge_started = asyncio.Event()
    allow_purge = asyncio.Event()
    vector_store = MagicMock()
    vector_store.upsert = AsyncMock()

    async def pause_purge(_memory_id: str) -> None:
        purge_started.set()
        await allow_purge.wait()

    vector_store.delete = AsyncMock(side_effect=pause_purge)
    manager = MemoryManager(
        db=temp_db,
        config=MemoryConfig(enabled=True),
        vector_store=vector_store,
        embed_fn=AsyncMock(return_value=[0.1, 0.2]),
    )
    old = await manager.create_memory("row lock target", project_id=PERSONAL_PROJECT_ID)

    replacing = asyncio.create_task(
        manager.create_memory(
            "row lock replacement",
            project_id=PERSONAL_PROJECT_ID,
            supersedes=[old.id],
        )
    )
    await asyncio.wait_for(purge_started.wait(), timeout=2)
    restoring = asyncio.create_task(asyncio.to_thread(manager.storage.restore_memory, old.id))
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(restoring), timeout=0.05)

    allow_purge.set()
    await asyncio.wait_for(replacing, timeout=2)
    assert await asyncio.wait_for(restoring, timeout=2)
    assert manager.storage.get_memory(old.id).deleted_at is None


@pytest.mark.asyncio
async def test_supersedes_cleanup_budget_releases_row_lock(temp_db, monkeypatch) -> None:
    monkeypatch.setattr(lifecycle_module, "SUPERSESSION_CLEANUP_BUDGET_SECONDS", 1.1)
    vector_store = MagicMock()
    vector_store.upsert = AsyncMock()
    never_finishes = asyncio.Event()

    async def block_delete(_memory_id: str) -> None:
        await never_finishes.wait()

    vector_store.delete = AsyncMock(side_effect=block_delete)
    manager = MemoryManager(
        db=temp_db,
        config=MemoryConfig(enabled=True),
        vector_store=vector_store,
        embed_fn=AsyncMock(return_value=[0.1, 0.2]),
    )
    old = await manager.create_memory("budget target", project_id=PERSONAL_PROJECT_ID)

    await asyncio.wait_for(
        manager.create_memory(
            "budget replacement",
            project_id=PERSONAL_PROJECT_ID,
            supersedes=[old.id],
        ),
        timeout=2,
    )

    assert await asyncio.wait_for(
        asyncio.to_thread(manager.storage.restore_memory, old.id),
        timeout=1,
    )
    assert manager.storage.get_memory(old.id).deleted_at is None


@pytest.mark.asyncio
async def test_crossref_counterpart_hidden_skip(temp_db) -> None:
    vector_store = MagicMock()
    vector_store.upsert = AsyncMock()
    embed = AsyncMock(return_value=[1.0, 0.0])
    manager = MemoryManager(
        db=temp_db,
        config=MemoryConfig(enabled=True, auto_crossref=True),
        vector_store=vector_store,
        embed_fn=embed,
    )
    source = manager.storage.create_memory("source", PERSONAL_PROJECT_ID)
    counterpart = manager.storage.create_memory("counterpart", PERSONAL_PROJECT_ID)
    manager.storage.mark_vectors_reindexed({counterpart.id: counterpart.content})

    async def hide_counterpart(*_args, **_kwargs):
        manager.storage.mark_dreamed(counterpart.id, hidden_as="delete")
        return [(counterpart.id, 0.99)]

    vector_store.search = AsyncMock(side_effect=hide_counterpart)

    assert await manager.restore_memory_indices(
        source.id,
        source.content,
        source.project_id,
        source.is_global,
        source.memory_type.value,
    )
    row = temp_db.fetchone(
        """
        SELECT 1 FROM memory_crossrefs
        WHERE source_id = %s AND target_id = %s
        """,
        (source.id, counterpart.id),
    )
    assert row is None


@pytest.mark.asyncio
async def test_hard_delete_recreate_race_converges(temp_db) -> None:
    delete_started = asyncio.Event()
    allow_delete = asyncio.Event()
    vector_store = MagicMock()

    async def pause_delete(_memory_id: str) -> None:
        delete_started.set()
        await allow_delete.wait()

    vector_store.delete = AsyncMock(side_effect=pause_delete)
    manager = MemoryManager(
        db=temp_db,
        config=MemoryConfig(enabled=True),
        vector_store=vector_store,
    )
    original = manager.storage.create_memory("recreate me", PERSONAL_PROJECT_ID)

    deleting = asyncio.create_task(manager.delete_memory(original.id))
    await delete_started.wait()
    recreated = manager.storage.create_memory("recreate me", PERSONAL_PROJECT_ID)
    assert recreated.id == original.id
    allow_delete.set()
    assert await deleting

    repaired = manager.storage.get_memory(recreated.id)
    assert repaired.vector_needs_reindex is True
    assert repaired.graph_status == "pending"


@pytest.mark.asyncio
async def test_intent_repair_without_qdrant(temp_db) -> None:
    manager = MemoryManager(db=temp_db, config=MemoryConfig(enabled=True))
    memory = manager.storage.create_memory("repair without vectors", PERSONAL_PROJECT_ID)

    report = await manager.reconcile_stores()

    assert report["projection_intents"] == {
        "found": 1,
        "attempted": 1,
        "converged": 0,
        "remaining": 1,
        "errors": 0,
    }
    repaired = manager.storage.get_memory(memory.id)
    assert repaired.vector_needs_reindex is True
    assert repaired.graph_status == "pending"


@pytest.mark.asyncio
async def test_rescope_replaces_scope_projections(temp_db) -> None:
    vector_store = MagicMock()
    vector_store.upsert = AsyncMock()
    vector_store.search = AsyncMock(return_value=[])
    manager = MemoryManager(
        db=temp_db,
        config=MemoryConfig(enabled=True, auto_crossref=True),
        vector_store=vector_store,
        embed_fn=AsyncMock(return_value=[0.2, 0.4]),
    )
    memory = await manager.create_memory(
        "rescope projection",
        project_id=PERSONAL_PROJECT_ID,
    )
    target = LocalProjectManager(temp_db).create("rescope-target")
    kg_service = MagicMock()
    kg_service.remove_memory_from_graph = AsyncMock()
    manager._kg_service = kg_service
    vector_store.upsert.reset_mock()

    moved = await manager.move_memory(memory.id, target.id)

    assert moved.project_id == target.id
    vector_store.upsert.assert_awaited_once_with(
        memory.id,
        [0.2, 0.4],
        {
            "project_id": target.id,
            "is_global": False,
            "memory_type": "fact",
        },
    )
    kg_service.remove_memory_from_graph.assert_awaited_once_with(
        memory.id,
        project_id=PERSONAL_PROJECT_ID,
        is_global=False,
    )
    assert manager.storage.get_memory(memory.id).vector_needs_reindex is False


@pytest.mark.asyncio
async def test_create_reconciliation_fenced(temp_db) -> None:
    embed_started = asyncio.Event()
    allow_embed = asyncio.Event()
    embed_calls = 0
    vector_store = MagicMock()
    vector_store.upsert = AsyncMock()
    vector_store.delete = AsyncMock()
    vector_store.search = AsyncMock(return_value=[])

    async def embed(_content: str) -> list[float]:
        nonlocal embed_calls
        embed_calls += 1
        if embed_calls == 1:
            embed_started.set()
            await allow_embed.wait()
        return [0.3, 0.6]

    async def run_db(func: Any, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(func, *args, **kwargs)

    manager = MemoryManager(
        db=temp_db,
        config=MemoryConfig(enabled=True, auto_crossref=True),
        vector_store=vector_store,
        embed_fn=embed,
        run_db=run_db,
    )
    creating = asyncio.create_task(
        manager.create_memory("create fence source", project_id=PERSONAL_PROJECT_ID)
    )
    await embed_started.wait()
    source = manager.storage.list_memories(
        scope=MemoryScope.owner(PERSONAL_PROJECT_ID),
        visibility="all",
    )[0]
    superseding = asyncio.create_task(
        manager.create_memory(
            "create fence replacement",
            project_id=PERSONAL_PROJECT_ID,
            supersedes=[source.id],
        )
    )
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(superseding), timeout=0.05)
    allow_embed.set()

    created = await creating
    await superseding
    assert created.id == source.id
    assert manager.storage.get_memory(created.id, visibility="all").deleted_at is not None
    assert vector_store.upsert.await_count == 2
    vector_store.delete.assert_awaited_once_with(created.id)


@pytest.mark.asyncio
async def test_bulk_rebuilds_respect_fence(temp_db) -> None:
    vector_store = MagicMock()
    vector_store.upsert = AsyncMock()
    manager = MemoryManager(
        db=temp_db,
        config=MemoryConfig(enabled=True, auto_crossref=True),
        vector_store=vector_store,
        embed_fn=AsyncMock(return_value=[1.0, 0.0]),
    )
    source = manager.storage.create_memory("bulk source", PERSONAL_PROJECT_ID)
    counterpart = manager.storage.create_memory("bulk counterpart", PERSONAL_PROJECT_ID)
    manager.storage.mark_vectors_reindexed({counterpart.id: counterpart.content})

    async def update_source(*_args, **_kwargs):
        manager.storage.update_memory(source.id, content="newer bulk source")
        return [(counterpart.id, 0.99)]

    vector_store.search = AsyncMock(side_effect=update_source)

    assert await manager.rebuild_crossrefs_for_memory(source) == 0
    assert (
        temp_db.fetchone(
            "SELECT 1 FROM memory_crossrefs WHERE source_id = %s AND target_id = %s",
            (source.id, counterpart.id),
        )
        is None
    )


@pytest.mark.asyncio
async def test_hard_delete_cleanup_removes_artifacts(temp_db) -> None:
    vector_store = MagicMock()
    vector_store.delete = AsyncMock()
    manager = MemoryManager(
        db=temp_db,
        config=MemoryConfig(enabled=True),
        vector_store=vector_store,
    )
    kg_service = MagicMock()
    kg_service.remove_memory_from_graph = AsyncMock()
    manager._kg_service = kg_service
    memory = manager.storage.create_memory("hard delete artifacts", PERSONAL_PROJECT_ID)

    assert await manager.delete_memory(memory.id)
    assert manager.storage.memory_exists(memory.id) is False
    with pytest.raises(ValueError, match=r"Memory .* not found"):
        manager.storage.get_memory(memory.id, visibility="all")

    vector_store.delete.assert_awaited_once_with(memory.id)
    kg_service.remove_memory_from_graph.assert_awaited_once_with(
        memory.id,
        project_id=PERSONAL_PROJECT_ID,
        is_global=False,
    )


@pytest.mark.asyncio
async def test_crossref_counterpart_rescope_race(temp_db) -> None:
    vector_store = MagicMock()
    vector_store.upsert = AsyncMock()
    manager = MemoryManager(
        db=temp_db,
        config=MemoryConfig(enabled=True, auto_crossref=True),
        vector_store=vector_store,
        embed_fn=AsyncMock(return_value=[1.0, 0.0]),
    )
    source = manager.storage.create_memory("rescope source", PERSONAL_PROJECT_ID)
    counterpart = manager.storage.create_memory("rescope counterpart", PERSONAL_PROJECT_ID)
    target = LocalProjectManager(temp_db).create("counterpart-target")
    manager.storage.mark_vectors_reindexed({counterpart.id: counterpart.content})

    async def rescope_counterpart(*_args, **_kwargs):
        manager.storage.move_memory(counterpart.id, target.id)
        return [(counterpart.id, 0.99)]

    vector_store.search = AsyncMock(side_effect=rescope_counterpart)

    assert await manager.restore_memory_indices(
        source.id,
        source.content,
        source.project_id,
        source.is_global,
        source.memory_type.value,
    )
    assert (
        temp_db.fetchone(
            "SELECT 1 FROM memory_crossrefs WHERE source_id = %s AND target_id = %s",
            (source.id, counterpart.id),
        )
        is None
    )
