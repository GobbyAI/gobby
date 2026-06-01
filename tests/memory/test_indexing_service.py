from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.memory.services.indexing import IndexingService
from gobby.storage.memories import Memory

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
        self.db = MagicMock()
        self.db.execute.return_value.rowcount = 0

    def list_memories(
        self,
        project_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Memory]:
        memories = [
            memory
            for memory in self.memories
            if project_id is None or memory.project_id == project_id
        ]
        end = None if limit is None else offset + limit
        return memories[offset:end]


class _VectorStore:
    def __init__(self) -> None:
        self.ids: list[str] = []
        self.rebuild = AsyncMock(side_effect=self._rebuild)
        self.scroll_ids = AsyncMock(side_effect=self._scroll_ids)
        self.delete = AsyncMock()
        self.batch_upsert = AsyncMock()
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

    async def _scroll_ids(self) -> list[str]:
        return list(self.ids)


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


def _service(storage: _MemoryStorage, vector_store: _VectorStore) -> IndexingService:
    return IndexingService(
        storage=storage,  # type: ignore[arg-type]
        vector_store=vector_store,  # type: ignore[arg-type]
        embed_fn=_embed_fn,
        kg_service=None,
        crossref_service=MagicMock(),
        kg_rebuilder=AsyncMock(return_value={}),
    )


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
