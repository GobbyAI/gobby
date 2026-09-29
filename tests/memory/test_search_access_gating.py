"""Search-time stats: which callers count a hit as surfaced, and how recency ranks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from gobby.config.persistence import MemoryConfig
from gobby.memory.services.search import SearchService
from gobby.storage.memories import Memory
from gobby.storage.memories_models import MemoryType

pytestmark = pytest.mark.unit

_PROBE_CALLERS = (
    "memory.search",
    "mcp_proxy.memory.create_memory.similar_existing",
    "review_learning.related_lessons",
)


def _memory(
    memory_id: str,
    *,
    updated_at: datetime,
    last_accessed_at: datetime | None = None,
    last_surfaced_at: datetime | None = None,
) -> Memory:
    return Memory(
        id=memory_id,
        memory_type=MemoryType.FACT,
        content=memory_id,
        created_at=updated_at,
        updated_at=updated_at,
        source_type="agent",
        tags=[],
        last_accessed_at=last_accessed_at,
        last_surfaced_at=last_surfaced_at,
    )


class _RecordingStorage:
    def __init__(self, memories: list[Memory]) -> None:
        self._memories = {memory.id: memory for memory in memories}
        self.surfaced: list[str] = []
        self.accessed: list[str] = []

    def list_memories(self, **_kwargs: Any) -> list[Memory]:
        return list(self._memories.values())

    def get_memories(self, memory_ids: list[str], scope: Any = None) -> list[Memory]:
        return [self._memories[memory_id] for memory_id in memory_ids]

    def get_memory(self, memory_id: str, scope: Any = None) -> Memory:
        return self._memories[memory_id]

    def update_surfaced_stats(self, memory_id: str, surfaced_at: datetime) -> None:
        self.surfaced.append(memory_id)

    def update_access_stats(self, memory_id: str, accessed_at: datetime) -> None:
        self.accessed.append(memory_id)


def _service(storage: _RecordingStorage) -> SearchService:
    async def _embed(text: str, is_query: bool = False) -> list[float]:
        return [1.0, 0.0]

    return SearchService(
        storage=cast(Any, storage),
        vector_store=None,
        embed_fn=_embed,
        kg_service=None,
        keyword_search=lambda query, limit, project_id, *, include_global=True: [],
        config=MemoryConfig(access_debounce_seconds=60),
        falkordb_graph_search=False,
        falkordb_graph_min_score=0.0,
        rrf_k=60,
        falkordb_rrf_k=60,
        vector_store_failure_logger=lambda message, error: None,
        run_db=None,
    )


def _surfacing_pair() -> list[Memory]:
    now = datetime.now(UTC)
    return [
        _memory("never-surfaced", updated_at=now),
        _memory(
            "just-surfaced",
            updated_at=now,
            last_surfaced_at=now - timedelta(seconds=5),
        ),
    ]


@pytest.mark.asyncio
async def test_surfaced_callers_increment_surfaced_stats() -> None:
    from gobby.memory.services.search import SURFACED_CALLERS

    assert SURFACED_CALLERS == frozenset(
        {
            "memory.surface",
            "mcp_proxy.memory.search_memories",
            "mcp_proxy.memory.review_task_memories",
            "http.memory.search",
            "cli.memory.recall",
        }
    )
    for caller in sorted(SURFACED_CALLERS):
        storage = _RecordingStorage(_surfacing_pair())

        await _service(storage).search(limit=10, caller=caller)

        # The hit surfaced five seconds ago is inside the 60-second debounce.
        assert storage.surfaced == ["never-surfaced"], caller
        assert storage.accessed == [], caller


@pytest.mark.asyncio
async def test_probe_callers_do_not_increment() -> None:
    for caller in _PROBE_CALLERS:
        storage = _RecordingStorage(_surfacing_pair())

        await _service(storage).search(limit=10, caller=caller)

        assert (storage.surfaced, storage.accessed) == ([], []), caller

    storage = _RecordingStorage(_surfacing_pair())
    await _service(storage).search(limit=10)
    assert (storage.surfaced, storage.accessed) == ([], [])


def test_fetched_older_memory_outranks_unfetched_newer() -> None:
    now = datetime.now(UTC)
    storage = _RecordingStorage(
        [
            _memory("newer-unfetched", updated_at=now - timedelta(days=10)),
            _memory(
                "older-fetched",
                updated_at=now - timedelta(days=60),
                last_accessed_at=now - timedelta(hours=1),
            ),
        ]
    )

    results = _service(storage)._build_results(
        merged_ids=["newer-unfetched", "older-fetched"],
        ranking_score_map={"newer-unfetched": 1.0, "older-fetched": 1.0},
        qdrant_score_map={"newer-unfetched": 0.5, "older-fetched": 0.5},
        qdrant_set={"newer-unfetched", "older-fetched"},
        keyword_set=set(),
        graph_set=None,
        rrf_applied=False,
        project_id=None,
        memory_type=None,
        tags_all=None,
        tags_any=None,
        tags_none=None,
        half_life=30.0,
        effective_min_score=0.0,
        limit=2,
    )

    assert [mem.id for mem in results] == ["older-fetched", "newer-unfetched"]
