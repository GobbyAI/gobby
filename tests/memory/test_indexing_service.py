from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.memory.services.indexing import REINDEX_PAGE_SIZE, IndexingService
from gobby.projects.fenced_vector_store import ProjectFencedVectorStore
from gobby.projects.write_fence import ProjectWriteFence
from gobby.storage.memories import Memory
from gobby.storage.memories_scope import ALL_MEMORIES, MemoryScope, memory_matches_scope
from tests.projects.fence_helpers import wait_for_exclusive_claim

pytestmark = pytest.mark.unit


def _memory(memory_id: str, content: str, project_id: str = "project-1") -> Memory:
    return Memory(
        id=memory_id,
        memory_type="fact",
        content=content,
        created_at="2026-05-31T00:00:00+00:00",
        updated_at="2026-05-31T00:00:00+00:00",
        project_id=project_id,
    )


class _MemoryStorage:
    def __init__(self, memories: list[Memory]) -> None:
        self.memories = memories
        self.stale_ids = {memory.id for memory in memories if memory.vector_needs_reindex}
        self.reindexed_content: dict[str, str] = {}
        self.list_calls: list[tuple[MemoryScope, int | None, int]] = []
        self.db = MagicMock()
        self.db.execute.return_value.rowcount = 0

    def list_memories(
        self,
        scope: MemoryScope = ALL_MEMORIES,
        memory_type: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        tags_all: list[str] | None = None,
        tags_any: list[str] | None = None,
        tags_none: list[str] | None = None,
    ) -> list[Memory]:
        self.list_calls.append((scope, limit, offset))
        memories = [
            memory
            for memory in self.memories
            if memory_matches_scope(memory.project_id, memory.is_global, scope)
            and (memory_type is None or memory.memory_type == memory_type)
        ]
        end = None if limit is None else offset + limit
        return memories[offset:end]

    def list_live_ids(self, *, limit: int | None = None, offset: int = 0) -> list[str]:
        ids = [memory.id for memory in self.memories if memory.deleted_at is None]
        end = None if limit is None else offset + limit
        return ids[offset:end]

    def get_memories(self, memory_ids: list[str], **_kwargs: Any) -> list[Memory]:
        by_id = {memory.id: memory for memory in self.memories}
        return [by_id[memory_id] for memory_id in memory_ids if memory_id in by_id]

    def delete_project_crossrefs(self, project_id: str) -> int:
        return 0

    def list_vector_reindex_ids(self) -> list[str]:
        return sorted(self.stale_ids)

    def mark_vectors_reindexed(self, indexed_content: dict[str, str]) -> int:
        self.reindexed_content.update(indexed_content)
        cleared = self.stale_ids & indexed_content.keys()
        self.stale_ids -= cleared
        return len(cleared)

    def mark_vector_reindex_needed(self, memory_id: str) -> None:
        self.stale_ids.add(memory_id)

    def mark_vector_snapshot_reindexed(
        self,
        memory_id: str,
        content: str,
        project_id: str,
        is_global: bool,
    ) -> bool:
        matching = next(
            (
                memory
                for memory in self.memories
                if memory.id == memory_id
                and memory.content == content
                and memory.project_id == project_id
                and memory.is_global == is_global
                and memory.deleted_at is None
            ),
            None,
        )
        if matching is None:
            return False
        self.reindexed_content[memory_id] = content
        self.stale_ids.discard(memory_id)
        return True

    def reconcile_vector_snapshot_page(
        self,
        snapshots: list[tuple[str, str, str, bool]],
        reindex_ids: list[str],
    ) -> set[str]:
        cleared: set[str] = set()
        for memory_id, content, project_id, is_global in snapshots:
            if self.mark_vector_snapshot_reindexed(
                memory_id,
                content,
                project_id,
                is_global,
            ):
                cleared.add(memory_id)
            else:
                self.stale_ids.add(memory_id)
        self.stale_ids.update(reindex_ids)
        return cleared


class _VectorStore:
    def __init__(self) -> None:
        self.ids: list[str] = []
        self.events: list[str] = []
        self.rebuild = AsyncMock(side_effect=self._rebuild)
        self.scroll_ids = AsyncMock(side_effect=self._scroll_ids)
        self.delete = AsyncMock()
        self.delete_many = AsyncMock(side_effect=self._delete_many)
        self.batch_upsert = AsyncMock(side_effect=self._batch_upsert)
        self.delete_collection = AsyncMock()

    @property
    def collection_name(self) -> str:
        return "memories"

    async def _rebuild(
        self,
        memory_dicts: list[dict[str, Any]],
        _embed_fn: Callable[[str], Awaitable[list[float]]],
    ) -> None:
        self.ids = [str(memory["id"]) for memory in memory_dicts]

    async def _scroll_ids(
        self,
        batch_size: int = 1000,
        filters: dict[str, str] | None = None,
    ) -> list[str]:
        del batch_size, filters
        self.events.append("scroll")
        return list(self.ids)

    async def _batch_upsert(
        self,
        batch: list[tuple[str, list[float], dict[str, Any]]],
        collection_name: str | None = None,
    ) -> None:
        del collection_name
        self.events.append("upsert")
        self.ids.extend(memory_id for memory_id, _embedding, _payload in batch)

    async def _delete_many(
        self,
        memory_ids: list[str],
        collection_name: str | None = None,
    ) -> None:
        del collection_name
        self.events.append("delete")
        self.ids = [memory_id for memory_id in self.ids if memory_id not in memory_ids]


class _SlowVectorStore(_VectorStore):
    def __init__(self) -> None:
        super().__init__()
        self.rebuild_started = asyncio.Event()
        self.complete_rebuild = asyncio.Event()
        self.rebuild = AsyncMock(side_effect=self._slow_rebuild)

    async def _slow_rebuild(
        self,
        memory_dicts: list[dict[str, Any]],
        embed_fn: Callable[[str], Awaitable[list[float]]],
    ) -> None:
        self.rebuild_started.set()
        await self.complete_rebuild.wait()
        await self._rebuild(memory_dicts, embed_fn)


async def _embed_fn(_content: str) -> list[float]:
    return [0.1, 0.2]


def _service(
    storage: _MemoryStorage,
    vector_store: _VectorStore,
    run_db: Callable[..., Awaitable[Any]] | None = None,
    embed_fn: Callable[[str], Awaitable[list[float]]] = _embed_fn,
    cleanup_rowless: Callable[[str], Awaitable[None]] | None = None,
) -> IndexingService:
    return IndexingService(
        storage=storage,
        vector_store=vector_store,
        embed_fn=embed_fn,
        kg_service=None,
        crossref_service=MagicMock(),
        kg_rebuilder=AsyncMock(return_value={}),
        cleanup_rowless=cleanup_rowless,
        run_db=run_db,
    )


@pytest.mark.asyncio
async def test_global_reindex_routes_rowless_cleanup_through_recreate_fence() -> None:
    storage = _MemoryStorage([_memory("mem-1", "alpha")])
    vector_store = _VectorStore()
    cleanup_rowless = AsyncMock()
    service = _service(storage, vector_store, cleanup_rowless=cleanup_rowless)
    original_fetch = service.fetch_all_memories
    snapshot_count = 0

    async def fetch() -> list[Memory]:
        nonlocal snapshot_count
        snapshot_count += 1
        if snapshot_count == 2:
            storage.memories.clear()
        return await original_fetch()

    service.fetch_all_memories = fetch  # type: ignore[method-assign]

    result = await service.reindex_embeddings()

    assert result["success"] is True
    cleanup_rowless.assert_awaited_once_with("mem-1")
    vector_store.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_backfills_missing_vectors_and_deletes_orphans() -> None:
    """Storage-minus-Qdrant rows are embedded while Qdrant orphans still disappear."""
    storage = _MemoryStorage([_memory("present", "Present"), _memory("missing", "Missing")])
    vector_store = _VectorStore()
    vector_store.ids = ["present", "orphan"]
    embed_fn = AsyncMock(return_value=[0.4, 0.5])

    report = await _service(storage, vector_store, embed_fn=embed_fn).reconcile_stores()

    assert report["qdrant"] == {
        "orphans_found": 1,
        "orphans_deleted": 1,
        "missing_found": 1,
        "missing_embedded": 1,
        "stale_found": 0,
        "stale_reindexed": 0,
        "errors": 0,
        "total": 2,
    }
    assert set(vector_store.ids) == {"present", "missing"}
    embed_fn.assert_awaited_once_with("Missing")
    assert vector_store.batch_upsert.await_args.args[0] == [
        (
            "missing",
            [0.4, 0.5],
            {
                "content": "Missing",
                "project_id": "project-1",
                "is_global": False,
                "memory_type": "fact",
            },
        )
    ]


@pytest.mark.asyncio
async def test_reconcile_deletes_tombstone_vector_and_keeps_live_vector() -> None:
    soft_deleted = _memory("soft-deleted", "Deleted")
    soft_deleted.deleted_at = soft_deleted.updated_at
    storage = _MemoryStorage([_memory("live", "Live"), soft_deleted])
    vector_store = _VectorStore()
    vector_store.ids = ["live", "soft-deleted"]

    report = await _service(storage, vector_store).reconcile_stores()

    assert report["qdrant"]["orphans_found"] == 1
    assert report["qdrant"]["orphans_deleted"] == 1
    assert vector_store.ids == ["live"]


@pytest.mark.asyncio
async def test_reconcile_replaces_stale_vector_and_clears_current_marker() -> None:
    stale = _memory("stale", "Current content")
    stale.vector_needs_reindex = True
    storage = _MemoryStorage([stale])
    vector_store = _VectorStore()
    vector_store.ids = ["stale"]
    embed_fn = AsyncMock(return_value=[0.7, 0.8])

    report = await _service(storage, vector_store, embed_fn=embed_fn).reconcile_stores()

    assert report["qdrant"]["missing_found"] == 0
    assert report["qdrant"]["stale_found"] == 1
    assert report["qdrant"]["stale_reindexed"] == 1
    assert report["qdrant"]["errors"] == 0
    embed_fn.assert_awaited_once_with("Current content")
    assert storage.reindexed_content == {"stale": "Current content"}
    assert storage.stale_ids == set()


@pytest.mark.asyncio
async def test_reconcile_reports_only_content_qualified_stale_clears() -> None:
    stale = _memory("stale", "Current content")
    stale.vector_needs_reindex = True
    storage = _MemoryStorage([stale])
    storage.mark_vectors_reindexed = MagicMock(return_value=0)
    vector_store = _VectorStore()
    vector_store.ids = ["stale"]

    report = await _service(storage, vector_store).reconcile_stores()

    assert report["qdrant"]["stale_found"] == 1
    assert report["qdrant"]["stale_reindexed"] == 0
    storage.mark_vectors_reindexed.assert_called_once_with({"stale": "Current content"})


@pytest.mark.asyncio
async def test_reconcile_dry_run_reports_missing_and_orphans_without_mutating() -> None:
    storage = _MemoryStorage([_memory("missing", "Missing")])
    vector_store = _VectorStore()
    vector_store.ids = ["orphan"]
    embed_fn = AsyncMock(return_value=[0.4, 0.5])

    report = await _service(storage, vector_store, embed_fn=embed_fn).reconcile_stores(dry_run=True)

    assert report["qdrant"]["orphans_found"] == 1
    assert report["qdrant"]["orphans_deleted"] == 0
    assert report["qdrant"]["missing_found"] == 1
    assert report["qdrant"]["missing_embedded"] == 0
    assert vector_store.ids == ["orphan"]
    embed_fn.assert_not_awaited()
    vector_store.delete_many.assert_not_awaited()
    vector_store.batch_upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_reports_one_embedding_failure_and_continues() -> None:
    storage = _MemoryStorage([_memory("bad", "Bad"), _memory("good", "Good")])
    vector_store = _VectorStore()

    async def embed_fn(content: str) -> list[float]:
        if content == "Bad":
            raise RuntimeError("embedding unavailable")
        return [0.4, 0.5]

    report = await _service(storage, vector_store, embed_fn=embed_fn).reconcile_stores()

    assert report["qdrant"]["missing_found"] == 2
    assert report["qdrant"]["missing_embedded"] == 1
    assert report["qdrant"]["errors"] == 1
    assert report["qdrant"]["reindex_failures"] == [
        {"memory_id": "bad", "error": "embedding unavailable"}
    ]
    assert vector_store.ids == ["good"]


@pytest.mark.asyncio
async def test_reconcile_reports_vector_upsert_failure_without_crashing() -> None:
    storage = _MemoryStorage([_memory("missing", "Missing")])
    vector_store = _VectorStore()
    vector_store.batch_upsert.side_effect = RuntimeError("qdrant unavailable")

    report = await _service(storage, vector_store).reconcile_stores()

    assert report["qdrant"]["missing_found"] == 1
    assert report["qdrant"]["missing_embedded"] == 0
    assert report["qdrant"]["errors"] == 1
    assert report["qdrant"]["reindex_failures"] == [
        {"memory_id": "missing", "error": "vector upsert failed: qdrant unavailable"}
    ]
    assert vector_store.ids == []


@pytest.mark.asyncio
async def test_project_reindex_logs_cumulative_batch_progress(
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage = _MemoryStorage([_memory(f"mem-{index}", f"content {index}") for index in range(501)])
    vector_store = _VectorStore()
    service = _service(storage, vector_store)

    with caplog.at_level(logging.INFO, logger="gobby.memory.services.indexing"):
        result = await service.reindex_embeddings(project_id="project-1")

    assert result["success"] is True
    assert result["embeddings_generated"] == 501
    assert vector_store.batch_upsert.await_count == 2
    assert [len(call.args[0]) for call in vector_store.batch_upsert.await_args_list] == [500, 1]
    assert [
        record.message
        for record in caplog.records
        if record.name == "gobby.memory.services.indexing"
        and record.message.startswith("Reindex progress:")
    ] == [
        "Reindex progress: 500/501 vectors",
        "Reindex progress: 501/501 vectors",
    ]


@pytest.mark.asyncio
async def test_project_reindex_pages_all_memories_then_deletes_only_stale_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("gobby.memory.services.indexing.REINDEX_PAGE_SIZE", 2)
    storage = _MemoryStorage([_memory(f"mem-{index}", f"content {index}") for index in range(5)])
    vector_store = _VectorStore()
    vector_store.ids = ["mem-0", "stale-project-vector"]
    service = _service(storage, vector_store)

    result = await service.reindex_embeddings(project_id="project-1")

    assert result["success"] is True
    assert result["embeddings_generated"] == 5
    assert storage.list_calls == [
        (MemoryScope.owner("project-1"), 2, 0),
        (MemoryScope.owner("project-1"), 2, 2),
        (MemoryScope.owner("project-1"), 2, 4),
    ]
    vector_store.scroll_ids.assert_awaited_once_with(filters={"project_id": "project-1"})
    assert [len(call.args[0]) for call in vector_store.batch_upsert.await_args_list] == [2, 2, 1]
    vector_store.delete_many.assert_awaited_once_with(["stale-project-vector"])
    assert vector_store.events == ["scroll", "upsert", "upsert", "upsert", "delete"]


@pytest.mark.asyncio
async def test_project_reindex_embed_failure_never_deletes_existing_vectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("gobby.memory.services.indexing.REINDEX_PAGE_SIZE", 1)
    storage = _MemoryStorage([_memory("mem-1", "first"), _memory("mem-2", "fail")])
    vector_store = _VectorStore()
    vector_store.ids = ["mem-1", "mem-2", "stale-project-vector"]

    async def embed_fn(content: str) -> list[float]:
        if content == "fail":
            raise RuntimeError("embedding failed")
        return [0.1, 0.2]

    service = _service(storage, vector_store, embed_fn=embed_fn)

    result = await service.reindex_embeddings(project_id="project-1")

    assert result == {
        "success": False,
        "total_memories": 2,
        "error": "embedding failed",
    }
    assert vector_store.batch_upsert.await_count == 1
    vector_store.delete_many.assert_not_awaited()
    vector_store.delete.assert_not_awaited()
    assert vector_store.events == ["scroll", "upsert"]


@pytest.mark.asyncio
async def test_global_reindex_skips_unchanged_memory_snapshot() -> None:
    storage = _MemoryStorage(
        [
            _memory("mem-1", "alpha"),
            _memory("mem-2", "beta"),
        ]
    )
    vector_store = _VectorStore()
    service = _service(storage, vector_store)

    first = await service.reindex_embeddings()
    second = await service.reindex_embeddings()

    assert first["success"] is True
    assert first["embeddings_generated"] == 2
    assert second["success"] is True
    assert second["embeddings_generated"] == 0
    assert second["skipped"] is True
    assert vector_store.rebuild.await_count == 1
    assert vector_store.scroll_ids.await_count == 1


@pytest.mark.asyncio
async def test_global_reindex_acquires_admission_before_source_snapshot() -> None:
    events: list[str] = []

    class FencedVectorStore(_VectorStore):
        @asynccontextmanager
        async def global_write_context(self) -> AsyncIterator[None]:
            events.append("admission:enter")
            try:
                yield
            finally:
                events.append("admission:exit")

    storage = _MemoryStorage([_memory("mem-1", "alpha")])
    vector_store = FencedVectorStore()

    async def rebuild(
        memory_dicts: list[dict[str, Any]],
        embed_fn: Callable[[str], Awaitable[list[float]]],
    ) -> None:
        events.append("rebuild")
        await vector_store._rebuild(memory_dicts, embed_fn)

    vector_store.rebuild = AsyncMock(side_effect=rebuild)
    service = _service(storage, vector_store)
    original_fetch = service.fetch_all_memories

    async def fetch() -> list[Memory]:
        events.append("snapshot")
        return await original_fetch()

    service.fetch_all_memories = fetch  # type: ignore[method-assign]

    result = await service.reindex_embeddings()

    assert result["success"] is True
    assert events == [
        "admission:enter",
        "snapshot",
        "rebuild",
        "snapshot",
        "admission:exit",
    ]


@pytest.mark.asyncio
async def test_global_reindex_does_not_skip_durable_stale_marker() -> None:
    storage = _MemoryStorage([_memory("mem-1", "alpha")])
    vector_store = _VectorStore()
    service = _service(storage, vector_store)

    await service.reindex_embeddings()
    storage.stale_ids.add("mem-1")
    second = await service.reindex_embeddings()

    assert second["success"] is True
    assert second["embeddings_generated"] == 1
    assert second["skipped"] is False
    assert vector_store.rebuild.await_count == 2
    assert storage.stale_ids == set()


@pytest.mark.asyncio
async def test_global_reindex_pages_every_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gobby.memory.services.indexing.REINDEX_PAGE_SIZE", 2)
    storage = _MemoryStorage([_memory(f"mem-{index}", str(index)) for index in range(5)])
    vector_store = _VectorStore()

    result = await _service(storage, vector_store).reindex_embeddings()

    assert result["success"] is True
    assert result["embeddings_generated"] == 5
    assert vector_store.ids == [f"mem-{index}" for index in range(5)]
    assert storage.list_calls == [
        (ALL_MEMORIES, 2, 0),
        (ALL_MEMORIES, 2, 2),
        (ALL_MEMORIES, 2, 4),
        (ALL_MEMORIES, 2, 0),
        (ALL_MEMORIES, 2, 2),
        (ALL_MEMORIES, 2, 4),
    ]


@pytest.mark.asyncio
async def test_global_index_rebuild_pages_every_crossref_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("gobby.memory.services.indexing.REINDEX_PAGE_SIZE", 2)
    storage = _MemoryStorage([_memory(f"mem-{index}", str(index)) for index in range(5)])
    service = _service(storage, _VectorStore())
    service._crossref_service.rebuild_for_memory = AsyncMock(return_value=1)

    report = await service.rebuild_indices()

    assert report["crossrefs"] == {"memories_processed": 5, "crossrefs_created": 5}
    assert service._crossref_service.rebuild_for_memory.await_count == 5
    assert storage.list_calls == [
        (ALL_MEMORIES, 2, 0),
        (ALL_MEMORIES, 2, 2),
        (ALL_MEMORIES, 2, 4),
        (ALL_MEMORIES, 2, 0),
        (ALL_MEMORIES, 2, 2),
        (ALL_MEMORIES, 2, 4),
        (ALL_MEMORIES, 2, 0),
        (ALL_MEMORIES, 2, 2),
        (ALL_MEMORIES, 2, 4),
    ]


@pytest.mark.asyncio
async def test_global_reindex_reads_storage_through_run_storage() -> None:
    storage = _MemoryStorage([_memory("mem-1", "alpha")])
    vector_store = _VectorStore()
    run_db_calls: list[tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any]]] = []

    async def run_db(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        run_db_calls.append((func, args, kwargs))
        return func(*args, **kwargs)

    service = _service(storage, vector_store, run_db=run_db)

    result = await service.reindex_embeddings()

    assert result["success"] is True
    func, args, kwargs = run_db_calls[0]
    assert getattr(func, "__self__", None) is storage
    assert getattr(func, "__func__", None) is _MemoryStorage.list_memories
    assert args == ()
    assert kwargs == {"scope": ALL_MEMORIES, "limit": REINDEX_PAGE_SIZE, "offset": 0}


@pytest.mark.asyncio
async def test_concurrent_global_reindex_awaits_running_rebuild(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _MemoryStorage(
        [
            _memory("mem-1", "alpha"),
            _memory("mem-2", "beta"),
        ]
    )
    vector_store = _SlowVectorStore()
    service = _service(storage, vector_store)
    original_shield = asyncio.shield
    shield_calls = 0
    second_call_waiting = asyncio.Event()

    def shield_spy(awaitable: Any) -> Any:
        nonlocal shield_calls
        shield_calls += 1
        if shield_calls == 2:
            second_call_waiting.set()
        return original_shield(awaitable)

    monkeypatch.setattr(asyncio, "shield", shield_spy)

    with caplog.at_level(logging.INFO, logger="gobby.memory.services.indexing"):
        first_task = asyncio.create_task(service.reindex_embeddings())
        await asyncio.wait_for(vector_store.rebuild_started.wait(), timeout=1.0)
        second_task = asyncio.create_task(service.reindex_embeddings())
        await asyncio.wait_for(second_call_waiting.wait(), timeout=1.0)
        vector_store.complete_rebuild.set()
        first, second = await asyncio.wait_for(
            asyncio.gather(first_task, second_task),
            timeout=1.0,
        )

    assert first["success"] is True
    assert first["embeddings_generated"] == 2
    assert first["skipped"] is False
    assert second["success"] is True
    assert second["embeddings_generated"] == 2
    assert second["skipped"] is False
    assert "skip_reason" not in second
    assert shield_calls == 2
    assert vector_store.rebuild.await_count == 1
    assert "Skipping global embedding reindex" not in caplog.text


@pytest.mark.asyncio
async def test_global_reindex_rebuilds_when_memory_content_changes() -> None:
    storage = _MemoryStorage([_memory("mem-1", "alpha")])
    vector_store = _VectorStore()
    service = _service(storage, vector_store)

    await service.reindex_embeddings()
    storage.memories = [_memory("mem-1", "changed")]
    second = await service.reindex_embeddings()

    assert second["success"] is True
    assert second["embeddings_generated"] == 1
    assert second["skipped"] is False
    assert vector_store.rebuild.await_count == 2


@pytest.mark.asyncio
async def test_global_reindex_uses_identity_fast_path_before_vector_scroll() -> None:
    storage = _MemoryStorage([_memory("mem-1", "alpha")])
    vector_store = _VectorStore()
    service = _service(storage, vector_store)

    await service.reindex_embeddings()
    storage.memories.append(_memory("mem-2", "beta"))
    second = await service.reindex_embeddings()

    assert second["success"] is True
    assert second["embeddings_generated"] == 2
    assert second["skipped"] is False
    assert vector_store.rebuild.await_count == 2
    assert vector_store.scroll_ids.await_count == 0


@pytest.mark.asyncio
async def test_global_reindex_rebuilds_after_dedupe_window() -> None:
    storage = _MemoryStorage([_memory("mem-1", "alpha")])
    vector_store = _VectorStore()
    service = _service(storage, vector_store)

    await service.reindex_embeddings()
    service._last_global_reindex_completed_at = 0.0
    second = await service.reindex_embeddings()

    assert second["success"] is True
    assert second["embeddings_generated"] == 1
    assert second["skipped"] is False
    assert vector_store.rebuild.await_count == 2


@pytest.mark.asyncio
async def test_global_reindex_resets_fingerprint_when_vector_scroll_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage = _MemoryStorage([_memory("mem-1", "alpha")])
    vector_store = _VectorStore()
    service = _service(storage, vector_store)

    await service.reindex_embeddings()
    vector_store.scroll_ids.side_effect = RuntimeError("qdrant unavailable")

    with caplog.at_level(logging.WARNING, logger="gobby.memory.services.indexing"):
        second = await service.reindex_embeddings()

    assert second["success"] is False
    assert "qdrant unavailable" in second["error"]
    assert vector_store.rebuild.await_count == 1
    assert service._last_global_reindex_fingerprint is None
    assert service._last_global_reindex_identity_fingerprint is None
    assert "Could not verify vector store IDs before skipping reindex" in caplog.text


@pytest.mark.asyncio
async def test_global_reindex_rebuilds_when_vector_ids_do_not_match_snapshot() -> None:
    storage = _MemoryStorage([_memory("mem-1", "alpha")])
    vector_store = _VectorStore()
    service = _service(storage, vector_store)

    await service.reindex_embeddings()
    vector_store.ids = []
    second = await service.reindex_embeddings()

    assert second["success"] is True
    assert second["embeddings_generated"] == 1
    assert second["skipped"] is False
    assert vector_store.rebuild.await_count == 2


@pytest.mark.asyncio
async def test_clear_indices_uses_vector_store_collection_name_method() -> None:
    storage = _MemoryStorage([])
    vector_store = _VectorStore()
    service = _service(storage, vector_store)

    result = await service.clear_indices()

    vector_store.delete_collection.assert_awaited_once_with("memories")
    assert result["vectors_cleared"] is True


@pytest.mark.asyncio
async def test_project_reindex_holds_writer_admission_across_embedding_and_batch() -> None:
    project = MagicMock(deleted_at=None)
    fence = ProjectWriteFence(lambda _project_id: project)
    inner = _VectorStore()
    vector_store = ProjectFencedVectorStore(inner, fence)  # type: ignore[arg-type]
    embed_started = asyncio.Event()
    release_embed = asyncio.Event()

    async def embed(_content: str) -> list[float]:
        embed_started.set()
        await release_embed.wait()
        return [0.1]

    service = _service(_MemoryStorage([_memory("mem-1", "alpha")]), vector_store, embed_fn=embed)  # type: ignore[arg-type]
    reindex_task = asyncio.create_task(service.reindex_embeddings("project-1"))
    await embed_started.wait()
    project.deleted_at = object()
    exclusive_entered = asyncio.Event()

    async def purge() -> None:
        async with fence.exclusive("project-1", timeout=1.0):
            exclusive_entered.set()

    purge_task = asyncio.create_task(purge())
    await wait_for_exclusive_claim(fence, "project-1")
    assert not exclusive_entered.is_set()

    release_embed.set()
    result = await reindex_task
    await purge_task
    assert result["success"] is True
    assert inner.ids == ["mem-1"]
    assert exclusive_entered.is_set()


@pytest.mark.asyncio
async def test_reconcile_backfill_holds_global_admission_across_embedding_and_batch() -> None:
    project = MagicMock(deleted_at=None)
    fence = ProjectWriteFence(lambda _project_id: project)
    inner = _VectorStore()
    vector_store = ProjectFencedVectorStore(inner, fence)  # type: ignore[arg-type]
    embed_started = asyncio.Event()
    release_embed = asyncio.Event()

    async def embed(_content: str) -> list[float]:
        embed_started.set()
        await release_embed.wait()
        return [0.1]

    service = _service(_MemoryStorage([_memory("missing", "alpha")]), vector_store, embed_fn=embed)  # type: ignore[arg-type]
    reconcile_task = asyncio.create_task(service.reconcile_stores())
    await embed_started.wait()
    project.deleted_at = object()
    exclusive_entered = asyncio.Event()

    async def purge() -> None:
        async with fence.exclusive("project-1", timeout=1.0):
            exclusive_entered.set()

    purge_task = asyncio.create_task(purge())
    await wait_for_exclusive_claim(fence, "project-1")
    assert not exclusive_entered.is_set()

    release_embed.set()
    result = await reconcile_task
    await purge_task
    assert result["qdrant"]["missing_embedded"] == 1
    assert inner.ids == ["missing"]
    assert exclusive_entered.is_set()


@pytest.mark.asyncio
async def test_reconcile_backfill_embeds_content_with_rationale() -> None:
    """The backfilled vector is computed from content plus rationale (#21010)."""
    missing = _memory("missing", "Missing")
    missing.rationale = "Needed when a future session re-derives the DSN."
    storage = _MemoryStorage([missing])
    vector_store = _VectorStore()
    embed_fn = AsyncMock(return_value=[0.4, 0.5])

    await _service(storage, vector_store, embed_fn=embed_fn).reconcile_stores()

    embed_fn.assert_awaited_once_with(
        "Missing\n\nWhy: Needed when a future session re-derives the DSN."
    )
    # The payload and the reindex marker still carry the bare content.
    upsert = vector_store.batch_upsert.await_args
    assert upsert is not None
    payload = upsert.args[0][0][2]
    assert payload["content"] == "Missing"
    assert "rationale" not in payload


@pytest.mark.asyncio
async def test_project_reindex_embeds_content_with_rationale_and_keeps_payload_bare() -> None:
    memory = _memory("m1", "Body")
    memory.rationale = "Why."
    storage = _MemoryStorage([memory])
    vector_store = _VectorStore()
    embed_fn = AsyncMock(return_value=[0.1, 0.2])
    service = _service(storage, vector_store, embed_fn=embed_fn)

    report = await service.reindex_embeddings(project_id="project-1")

    assert report["success"] is True
    embed_fn.assert_awaited_once_with("Body\n\nWhy: Why.")
    upsert = vector_store.batch_upsert.await_args
    assert upsert is not None
    batch = upsert.args[0]
    assert batch[0][2] == {
        "content": "Body",
        "project_id": "project-1",
        "is_global": False,
        "memory_type": "fact",
    }
