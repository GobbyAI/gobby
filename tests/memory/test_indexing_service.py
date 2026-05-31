from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.memory.services.indexing import IndexingService
from gobby.storage.memories import Memory


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
    collection_name = "memories"

    def __init__(self) -> None:
        self.ids: list[str] = []
        self.rebuild = AsyncMock(side_effect=self._rebuild)
        self.scroll_ids = AsyncMock(side_effect=self._scroll_ids)
        self.delete = AsyncMock()
        self.batch_upsert = AsyncMock()
        self.delete_collection = AsyncMock()

    async def _rebuild(
        self,
        memory_dicts: list[dict[str, Any]],
        _embed_fn: Callable[[str], Awaitable[list[float]]],
    ) -> None:
        self.ids = [str(memory["id"]) for memory in memory_dicts]

    async def _scroll_ids(self) -> list[str]:
        return list(self.ids)


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
