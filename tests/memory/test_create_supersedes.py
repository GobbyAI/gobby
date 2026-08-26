from __future__ import annotations

import asyncio
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from psycopg.errors import RaiseException

from gobby.config.persistence import MemoryConfig
from gobby.memory.backends.null import NullBackend
from gobby.memory.backends.storage_adapter import StorageAdapter
from gobby.memory.dream.apply import apply_dream_plan, revert_dream_run
from gobby.memory.dream.candidates import memory_to_candidate
from gobby.memory.dream.models import DreamAction, RelatedMemoryEvidence
from gobby.memory.dream.related import RelatedEvidenceChannelError
from gobby.memory.dream.storage import MemoryDreamStore
from gobby.memory.manager import MemoryManager
from gobby.memory.protocol import MemoryBackendProtocol
from gobby.memory.services import lifecycle as lifecycle_module
from gobby.memory.write_result import MemoryWriteResult
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories import LocalMemoryManager
from gobby.storage.memories_crud import MAX_SUPERSEDES_IDS
from gobby.storage.memories_models import PERSONAL_PROJECT_ID
from gobby.storage.memories_scope import MemoryScope
from gobby.storage.projects import LocalProjectManager

pytestmark = pytest.mark.integration


async def _drain_background_tasks(manager: MemoryManager) -> None:
    while manager._background_tasks:
        await asyncio.gather(*tuple(manager._background_tasks))


@pytest.mark.asyncio
async def test_auto_mark_due(temp_db, monkeypatch) -> None:
    """A created memory wakes only fused older survivors after create completes."""
    manager = MemoryManager(db=temp_db, config=MemoryConfig(enabled=True))
    older = manager.storage.create_memory(
        content="The daemon uses the original reconciliation policy.",
        project_id=PERSONAL_PROJECT_ID,
    )
    untouched = manager.storage.create_memory(
        content="A separate operational note.",
        project_id=PERSONAL_PROJECT_ID,
    )
    stamp = datetime.now(UTC)
    manager.storage.mark_dreamed(older.id, when=stamp)
    manager.storage.mark_dreamed(untouched.id, when=stamp)

    async def related(candidates, **kwargs):
        assert kwargs["temporal_direction"] == "older"
        assert kwargs["anchor_at"] == candidates[0].created_at
        evidence = RelatedMemoryEvidence(
            id=older.id,
            memory_type="fact",
            created_at=older.created_at,
            newer_by_days=0.0,
            content=older.content,
            matched_via="keyword+vector",
        )
        return [replace(candidates[0], related=(evidence,))]

    monkeypatch.setattr(lifecycle_module, "gather_related_evidence", related)

    created = await manager.create_memory(
        "The daemon uses the replacement reconciliation policy.",
        project_id=PERSONAL_PROJECT_ID,
    )
    await _drain_background_tasks(manager)

    assert manager.storage.get_memory(created.id).last_dreamed_at is None
    assert manager.storage.get_memory(older.id).last_dreamed_at is None
    assert manager.storage.get_memory(older.id).dream_due_version == 1
    assert manager.storage.get_memory(untouched.id).last_dreamed_at is not None

    disabled = MemoryManager(
        db=temp_db,
        config=MemoryConfig(dream={"write_supersession_mark_due_enabled": False}),
    )
    called = False

    async def should_not_run(*args, **kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(lifecycle_module, "gather_related_evidence", should_not_run)
    await disabled.create_memory("Disabled mark due", project_id=PERSONAL_PROJECT_ID)
    assert called is False


@pytest.mark.asyncio
async def test_background_mark_due_exhaustion_emits_terminal_warning(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = MemoryManager(db=temp_db, config=MemoryConfig(enabled=True))
    memory = manager.storage.create_memory(
        content="background retry exhaustion",
        project_id=PERSONAL_PROJECT_ID,
    )

    async def fail_related(*_args: object, **_kwargs: object) -> list[object]:
        raise RelatedEvidenceChannelError(
            "vector",
            attempts=3,
            detail="EmbeddingGenerationLeaseLost: serving fenced",
        )

    monkeypatch.setattr(lifecycle_module, "gather_related_evidence", fail_related)
    with caplog.at_level(logging.WARNING, logger=lifecycle_module.__name__):
        task = manager.schedule_write_mark_due(memory, "created")
        assert task is not None
        await task

    records = [
        record
        for record in caplog.records
        if "Background related-memory mark-due failed" in record.message
    ]
    assert len(records) == 1
    assert "channel=vector" in records[0].message
    assert "attempts=3" in records[0].message
    assert "EmbeddingGenerationLeaseLost: serving fenced" in records[0].message
    assert records[0].levelno == logging.WARNING
    assert records[0].exc_info is None
    assert "mark-due skipped" not in caplog.text


@pytest.mark.slow
@pytest.mark.asyncio
async def test_mark_due_lost_wakeup_guard(temp_db) -> None:
    """A mark-due after selection fences the entire dream mutation."""
    manager = MemoryManager(db=temp_db, config=MemoryConfig(enabled=True))
    memory = manager.storage.create_memory(
        content="Selected content",
        project_id=PERSONAL_PROJECT_ID,
    )
    candidate = memory_to_candidate(memory, datetime.now(UTC))
    store = MemoryDreamStore(temp_db)
    run_id = store.create_run(project_id=PERSONAL_PROJECT_ID, dry_run=False, options={})

    assert manager.mark_memories_due([memory.id], expected_project_id=PERSONAL_PROJECT_ID) == 1
    summary = await apply_dream_plan(
        memory_manager=manager,
        store=store,
        run_id=run_id,
        actions=[
            DreamAction(
                action="refresh",
                memory_id=memory.id,
                content="Stale planner rewrite",
                confidence=1.0,
            )
        ],
        candidates=[candidate],
        dry_run=False,
        reconcile_after_apply=False,
    )

    current = manager.storage.get_memory(memory.id)
    assert summary["mutations"] == 0
    assert current.content == "Selected content"
    assert current.last_dreamed_at is None
    assert current.dream_due_version == 1
    assert store.list_snapshots(run_id) == []


@pytest.mark.slow
@pytest.mark.asyncio
async def test_atomic_apply_listener_boundary(temp_db) -> None:
    """A fenced apply notifies once after commit and never on mismatch."""
    manager = MemoryManager(db=temp_db, config=MemoryConfig(enabled=True))
    memory = manager.storage.create_memory(
        content="Listener content",
        project_id=PERSONAL_PROJECT_ID,
    )
    candidate = memory_to_candidate(memory, datetime.now(UTC))
    store = MemoryDreamStore(temp_db)
    run_id = store.create_run(project_id=PERSONAL_PROJECT_ID, dry_run=False, options={})
    notifications: list[str] = []
    manager.storage.add_change_listener(lambda: notifications.append("changed"))

    await apply_dream_plan(
        memory_manager=manager,
        store=store,
        run_id=run_id,
        actions=[
            DreamAction(
                action="refresh",
                memory_id=memory.id,
                content="Listener refreshed",
                confidence=1.0,
            )
        ],
        candidates=[candidate],
        dry_run=False,
        reconcile_after_apply=False,
    )

    assert notifications == ["changed"]
    assert len(store.list_snapshots(run_id)) == 1

    manager.mark_memories_due([memory.id], expected_project_id=PERSONAL_PROJECT_ID)
    notifications.clear()
    await apply_dream_plan(
        memory_manager=manager,
        store=store,
        run_id=run_id,
        actions=[DreamAction(action="keep", memory_id=memory.id, confidence=1.0)],
        candidates=[candidate],
        dry_run=False,
        reconcile_after_apply=False,
    )

    assert notifications == []
    assert len(store.list_snapshots(run_id)) == 1


@pytest.mark.asyncio
async def test_dream_revert_preserves_due_generation(temp_db) -> None:
    """Revert never lowers a post-apply wakeup and leaves the row due."""
    manager = MemoryManager(db=temp_db, config=MemoryConfig(enabled=True))
    memory = manager.storage.create_memory(
        content="Before refresh",
        project_id=PERSONAL_PROJECT_ID,
    )
    store = MemoryDreamStore(temp_db)
    run_id = store.create_run(project_id=PERSONAL_PROJECT_ID, dry_run=False, options={})
    await apply_dream_plan(
        memory_manager=manager,
        store=store,
        run_id=run_id,
        actions=[
            DreamAction(
                action="refresh",
                memory_id=memory.id,
                content="After refresh",
                confidence=1.0,
            )
        ],
        candidates=[memory_to_candidate(memory, datetime.now(UTC))],
        dry_run=False,
        reconcile_after_apply=False,
    )
    manager.mark_memories_due([memory.id], expected_project_id=PERSONAL_PROJECT_ID)

    result = await revert_dream_run(
        store=store,
        run_id=run_id,
        memory_manager=None,
        reconcile_after_revert=False,
    )

    restored = manager.storage.get_memory(memory.id)
    assert result["success"] is True
    assert restored.content == "Before refresh"
    assert restored.last_dreamed_at is None
    assert restored.dream_due_version == 2


@pytest.mark.asyncio
async def test_dream_revert_action_owned_restore_set(temp_db) -> None:
    """Refresh-owned tag edits conflict, while promote preserves unowned tag edits."""
    manager = MemoryManager(db=temp_db, config=MemoryConfig(enabled=True))
    store = MemoryDreamStore(temp_db)
    refreshed = manager.storage.create_memory(
        content="Refresh before",
        project_id=PERSONAL_PROJECT_ID,
        tags=["before"],
    )
    refresh_run = store.create_run(
        project_id=PERSONAL_PROJECT_ID,
        dry_run=False,
        options={},
    )
    await apply_dream_plan(
        memory_manager=manager,
        store=store,
        run_id=refresh_run,
        actions=[
            DreamAction(
                action="refresh",
                memory_id=refreshed.id,
                content="Refresh after",
                tags=["dream"],
                confidence=1.0,
            )
        ],
        candidates=[memory_to_candidate(refreshed, datetime.now(UTC))],
        dry_run=False,
        reconcile_after_apply=False,
    )
    await manager.update_memory(refreshed.id, tags=["manual"])

    refresh_revert = await revert_dream_run(
        store=store,
        run_id=refresh_run,
        memory_manager=manager,
        reconcile_after_revert=False,
    )

    current_refresh = manager.storage.get_memory(refreshed.id)
    assert [item["memory_id"] for item in refresh_revert["conflicts"]] == [refreshed.id]
    assert current_refresh.content == "Refresh after"
    assert current_refresh.tags == ["manual"]

    promoted = manager.storage.create_memory(
        content="Promote before",
        project_id=PERSONAL_PROJECT_ID,
        tags=["before"],
    )
    promote_run = store.create_run(
        project_id=PERSONAL_PROJECT_ID,
        dry_run=False,
        options={},
    )
    await apply_dream_plan(
        memory_manager=manager,
        store=store,
        run_id=promote_run,
        actions=[DreamAction(action="promote", memory_id=promoted.id, confidence=1.0)],
        candidates=[memory_to_candidate(promoted, datetime.now(UTC))],
        dry_run=False,
        reconcile_after_apply=False,
    )
    await manager.update_memory(promoted.id, tags=["manual"])

    promote_revert = await revert_dream_run(
        store=store,
        run_id=promote_run,
        memory_manager=manager,
        reconcile_after_revert=False,
    )

    current_promote = manager.storage.get_memory(promoted.id)
    assert promote_revert["conflicts"] == []
    assert current_promote.is_global is False
    assert current_promote.tags == ["manual"]


@pytest.mark.asyncio
async def test_expected_error_cursor_fence(temp_db, monkeypatch) -> None:
    """The expected-error cursor fallback uses the original selection fence."""
    manager = MemoryManager(db=temp_db, config=MemoryConfig(enabled=True))
    memory = manager.storage.create_memory(
        content="Expected error source",
        project_id=PERSONAL_PROJECT_ID,
    )
    candidate = memory_to_candidate(memory, datetime.now(UTC))
    store = MemoryDreamStore(temp_db)
    run_id = store.create_run(project_id=PERSONAL_PROJECT_ID, dry_run=False, options={})
    original_apply = store.apply_candidate_action
    first = True

    def fail_after_wakeup(**kwargs):
        nonlocal first
        if first:
            first = False
            manager.mark_memories_due([memory.id], expected_project_id=PERSONAL_PROJECT_ID)
            raise ValueError("forced expected action error")
        return original_apply(**kwargs)

    monkeypatch.setattr(store, "apply_candidate_action", fail_after_wakeup)
    result = await apply_dream_plan(
        memory_manager=manager,
        store=store,
        run_id=run_id,
        actions=[
            DreamAction(
                action="refresh",
                memory_id=memory.id,
                content="Must not land",
                confidence=1.0,
            )
        ],
        candidates=[candidate],
        dry_run=False,
        reconcile_after_apply=False,
    )

    current = manager.storage.get_memory(memory.id)
    assert result["errors"] == 1
    assert current.content == "Expected error source"
    assert current.last_dreamed_at is None
    assert current.dream_due_version == 1
    assert store.list_snapshots(run_id) == []


@pytest.mark.asyncio
async def test_dream_revert_failure_converges(temp_db, monkeypatch) -> None:
    """A post-commit secondary failure leaves durable intent for repair."""
    vector_store = MagicMock()
    vector_store.upsert = AsyncMock()
    vector_store.delete = AsyncMock()
    vector_store.search = AsyncMock(return_value=[])
    vector_store.is_remote.return_value = True
    manager = MemoryManager(
        db=temp_db,
        config=MemoryConfig(enabled=True),
        vector_store=vector_store,
        embed_fn=AsyncMock(return_value=[0.2, 0.4]),
    )
    memory = manager.storage.create_memory(
        content="Failure before",
        project_id=PERSONAL_PROJECT_ID,
    )
    store = MemoryDreamStore(temp_db)
    run_id = store.create_run(project_id=PERSONAL_PROJECT_ID, dry_run=False, options={})
    await apply_dream_plan(
        memory_manager=manager,
        store=store,
        run_id=run_id,
        actions=[
            DreamAction(
                action="refresh",
                memory_id=memory.id,
                content="Failure after",
                confidence=1.0,
            )
        ],
        candidates=[memory_to_candidate(memory, datetime.now(UTC))],
        dry_run=False,
        reconcile_after_apply=False,
    )
    original_restore = manager.restore_memory_indices
    monkeypatch.setattr(
        manager,
        "restore_memory_indices",
        AsyncMock(side_effect=OSError("secondary unavailable")),
    )

    result = await revert_dream_run(
        store=store,
        run_id=run_id,
        memory_manager=manager,
        reconcile_after_revert=False,
    )

    assert result["success"] is True
    assert result["secondary_sync_failures"][0]["memory_id"] == memory.id
    assert manager.storage.get_memory(memory.id).vector_needs_reindex is True
    monkeypatch.setattr(manager, "restore_memory_indices", original_restore)
    assert await manager.reconcile_memory_indices(memory.id) is True
    assert manager.storage.get_memory(memory.id).vector_needs_reindex is False


@pytest.mark.asyncio
async def test_mark_due_eligibility_boundary(temp_db, monkeypatch) -> None:
    """Only fused survivors returned by evidence ranking are marked due."""
    manager = MemoryManager(db=temp_db, config=MemoryConfig(enabled=True))
    keyword_survivor = manager.storage.create_memory(
        content="Bread top filling bread bottom",
        project_id=PERSONAL_PROJECT_ID,
    )
    vector_survivor = manager.storage.create_memory(
        content="Semantically equivalent daemon policy",
        project_id=PERSONAL_PROJECT_ID,
    )
    common_word = manager.storage.create_memory(
        content="The system is available",
        project_id=PERSONAL_PROJECT_ID,
    )
    outside_top_k = manager.storage.create_memory(
        content="A lower-ranked policy note",
        project_id=PERSONAL_PROJECT_ID,
    )
    stamp = datetime.now(UTC)
    for memory in [keyword_survivor, vector_survivor, common_word, outside_top_k]:
        manager.storage.mark_dreamed(memory.id, when=stamp)

    async def fused(candidates, **_kwargs):
        related = tuple(
            RelatedMemoryEvidence(
                id=memory.id,
                memory_type="fact",
                created_at=memory.created_at,
                newer_by_days=0.0,
                content=memory.content,
                matched_via=matched_via,
            )
            for memory, matched_via in [
                (keyword_survivor, "keyword"),
                (vector_survivor, "vector"),
            ]
        )
        return [replace(candidates[0], related=related)]

    monkeypatch.setattr(lifecycle_module, "gather_related_evidence", fused)
    await manager.create_memory(
        "Bread surrounds filling under the current daemon policy",
        project_id=PERSONAL_PROJECT_ID,
    )
    await _drain_background_tasks(manager)

    assert manager.storage.get_memory(keyword_survivor.id).last_dreamed_at is None
    assert manager.storage.get_memory(vector_survivor.id).last_dreamed_at is None
    assert manager.storage.get_memory(common_word.id).last_dreamed_at is not None
    assert manager.storage.get_memory(outside_top_k.id).last_dreamed_at is not None


@pytest.mark.asyncio
async def test_mark_due_write_scope(temp_db, monkeypatch) -> None:
    """The update fence rejects targets rescoped or hidden after retrieval."""
    project_a = str(uuid.uuid4())
    project_b = str(uuid.uuid4())
    for project_id in [project_a, project_b]:
        temp_db.execute(
            "INSERT INTO projects (id, name) VALUES (%s, %s)",
            (project_id, f"Project {project_id}"),
        )
    manager = MemoryManager(db=temp_db, config=MemoryConfig(enabled=True))
    eligible = manager.storage.create_memory(content="eligible target", project_id=project_a)
    rescoped = manager.storage.create_memory(content="rescoped target", project_id=project_a)
    hidden = manager.storage.create_memory(content="hidden target", project_id=project_a)
    other = manager.storage.create_memory(content="other target", project_id=project_b)
    global_memory = manager.storage.create_memory(
        content="global target",
        project_id=PERSONAL_PROJECT_ID,
        is_global=True,
    )
    stamp = datetime.now(UTC)
    for memory in [eligible, rescoped, hidden, other, global_memory]:
        manager.storage.mark_dreamed(memory.id, when=stamp)

    async def interleaved(candidates, **kwargs):
        assert kwargs["scope"].project_id == project_a
        assert kwargs["scope"].kind == "project_only"
        temp_db.execute(
            "UPDATE memories SET project_id = %s WHERE id = %s",
            (project_b, rescoped.id),
        )
        manager.storage.mark_dreamed(hidden.id, hidden_as="review", when=stamp)
        evidence = tuple(
            RelatedMemoryEvidence(
                id=memory.id,
                memory_type="fact",
                created_at=memory.created_at,
                newer_by_days=0.0,
                content=memory.content,
                matched_via="keyword+vector",
            )
            for memory in [eligible, rescoped, hidden, other, global_memory]
        )
        return [replace(candidates[0], related=evidence)]

    monkeypatch.setattr(lifecycle_module, "gather_related_evidence", interleaved)
    await manager.create_memory("new scoped knowledge", project_id=project_a)
    await _drain_background_tasks(manager)

    assert manager.storage.get_memory(eligible.id).last_dreamed_at is None
    for memory in [rescoped, hidden, other, global_memory]:
        row = temp_db.fetchone(
            "SELECT last_dreamed_at FROM memories WHERE id = %s",
            (memory.id,),
        )
        assert row is not None
        assert row["last_dreamed_at"] is not None


@pytest.mark.asyncio
async def test_mark_due_dedup_noop(temp_db, monkeypatch) -> None:
    """A content-deduped active write never schedules another wakeup."""
    manager = MemoryManager(db=temp_db, config=MemoryConfig(enabled=True))
    calls = 0

    async def no_matches(candidates, **_kwargs):
        nonlocal calls
        calls += 1
        return candidates

    monkeypatch.setattr(lifecycle_module, "gather_related_evidence", no_matches)
    first = await manager.create_memory("Deduped knowledge", project_id=PERSONAL_PROJECT_ID)
    await _drain_background_tasks(manager)
    calls = 0

    duplicate = await manager.create_memory(
        "Deduped knowledge",
        project_id=PERSONAL_PROJECT_ID,
    )

    assert duplicate.id == first.id
    assert calls == 0
    assert manager._background_tasks == set()


@pytest.mark.asyncio
async def test_mark_due_reactivation_anchor(temp_db, monkeypatch) -> None:
    """Reactivation anchors older-evidence lookup at the reactivation timestamp."""
    manager = MemoryManager(db=temp_db, config=MemoryConfig(enabled=True))
    old_time = datetime(2025, 1, 1, tzinfo=UTC)
    middle_time = datetime(2025, 6, 1, tzinfo=UTC)
    hidden = manager.storage.create_memory(
        content="Reactivated knowledge",
        project_id=PERSONAL_PROJECT_ID,
        created_at=old_time,
        updated_at=old_time,
    )
    manager.storage.mark_dreamed(hidden.id, hidden_as="review", when=middle_time)
    intervening = manager.storage.create_memory(
        content="Intervening related knowledge",
        project_id=PERSONAL_PROJECT_ID,
        created_at=middle_time,
        updated_at=middle_time,
    )
    manager.storage.mark_dreamed(intervening.id, when=middle_time)

    async def related(candidates, **kwargs):
        assert kwargs["anchor_at"] == candidates[0].updated_at
        assert kwargs["anchor_at"] > intervening.created_at
        evidence = RelatedMemoryEvidence(
            id=intervening.id,
            memory_type="fact",
            created_at=intervening.created_at,
            newer_by_days=0.0,
            content=intervening.content,
            matched_via="vector",
        )
        return [replace(candidates[0], related=(evidence,))]

    monkeypatch.setattr(lifecycle_module, "gather_related_evidence", related)
    await manager.create_memory("Reactivated knowledge", project_id=PERSONAL_PROJECT_ID)
    await _drain_background_tasks(manager)

    assert manager.storage.get_memory(intervening.id).last_dreamed_at is None


@pytest.mark.slow
@pytest.mark.asyncio
async def test_mark_due_burst_bounded(temp_db, monkeypatch) -> None:
    """Queued writes construct at most two related-evidence sessions at once."""
    active = 0
    maximum = 0
    constructed = 0
    closed = 0
    release = asyncio.Event()
    saturated = asyncio.Event()

    class TrackingSession:
        def __init__(self) -> None:
            nonlocal active, maximum, constructed
            active += 1
            constructed += 1
            maximum = max(maximum, active)
            if active == lifecycle_module.WRITE_MARK_DUE_MAX_CONCURRENCY:
                saturated.set()

        async def aclose(self) -> None:
            nonlocal active, closed
            active -= 1
            closed += 1

    async def blocked(candidates, **_kwargs):
        await release.wait()
        return candidates

    monkeypatch.setattr(lifecycle_module, "RelatedEvidenceSession", TrackingSession)
    monkeypatch.setattr(lifecycle_module, "gather_related_evidence", blocked)
    manager = MemoryManager(db=temp_db, config=MemoryConfig(enabled=True))
    memories = [
        manager.storage.create_memory(
            content=f"burst {index}",
            project_id=PERSONAL_PROJECT_ID,
        )
        for index in range(5)
    ]
    tasks = [manager.schedule_write_mark_due(memory, "created") for memory in memories]
    assert all(task is not None for task in tasks)
    await asyncio.wait_for(saturated.wait(), timeout=1.0)

    assert constructed == lifecycle_module.WRITE_MARK_DUE_MAX_CONCURRENCY
    assert maximum == lifecycle_module.WRITE_MARK_DUE_MAX_CONCURRENCY
    release.set()
    await asyncio.gather(*(task for task in tasks if task is not None))

    assert constructed == len(memories)
    assert closed == len(memories)
    assert active == 0


@pytest.mark.slow
@pytest.mark.asyncio
async def test_manager_close_drains_mark_due(temp_db, monkeypatch) -> None:
    """Manager close cancels active work while queued tasks build no sessions."""
    active = 0
    constructed = 0
    closed = 0
    saturated = asyncio.Event()
    blocker = asyncio.Event()

    class TrackingSession:
        def __init__(self) -> None:
            nonlocal active, constructed
            active += 1
            constructed += 1
            if active == lifecycle_module.WRITE_MARK_DUE_MAX_CONCURRENCY:
                saturated.set()

        async def aclose(self) -> None:
            nonlocal active, closed
            active -= 1
            closed += 1

    async def blocked(candidates, **_kwargs):
        await blocker.wait()
        return candidates

    monkeypatch.setattr(lifecycle_module, "RelatedEvidenceSession", TrackingSession)
    monkeypatch.setattr(lifecycle_module, "gather_related_evidence", blocked)
    manager = MemoryManager(db=temp_db, config=MemoryConfig(enabled=True))
    for index in range(5):
        memory = manager.storage.create_memory(
            content=f"shutdown {index}",
            project_id=PERSONAL_PROJECT_ID,
        )
        manager.schedule_write_mark_due(memory, "created")
    await asyncio.wait_for(saturated.wait(), timeout=1.0)

    await manager.close()

    assert constructed == lifecycle_module.WRITE_MARK_DUE_MAX_CONCURRENCY
    assert closed == constructed
    assert active == 0
    assert manager._background_tasks == set()


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

    try:
        with pytest.raises(RaiseException, match="injected supersession failure"):
            manager.create_memory(
                "Rolled back replacement",
                PERSONAL_PROJECT_ID,
                supersedes=[first.id, second.id],
            )
    finally:
        temp_db.execute("DROP TRIGGER IF EXISTS fail_supersession_test_trigger ON memories")
        temp_db.execute("DROP FUNCTION IF EXISTS fail_supersession_test()")

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


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
@pytest.mark.asyncio
async def test_supersedes_purge_does_not_fence_restore(temp_db) -> None:
    """A paused secondary purge never blocks restore_memory.

    #20331 moved supersession-purge fencing from the memories row lock to
    ``pg_advisory_lock``: the visibility ``FOR UPDATE`` slice commits before
    secondary I/O starts, so ``restore_memory`` proceeds while the purge is
    still deleting artifacts, and the restored row's ``vector_needs_reindex``
    marker hands the (possibly purged) secondary stores to reconciliation.
    """
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

    restored = await asyncio.wait_for(
        asyncio.to_thread(manager.storage.restore_memory, old.id),
        timeout=2,
    )
    assert restored

    allow_purge.set()
    await replacing
    row = manager.storage.get_memory(old.id)
    assert row.deleted_at is None
    assert row.vector_needs_reindex is True


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
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
