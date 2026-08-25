"""Keyword search helpers for memory search."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast

from gobby.storage.memories import LocalMemoryManager, Memory

RunStorage = Callable[..., Awaitable[Any]]


class KeywordSearch(Protocol):
    def __call__(
        self,
        query: str,
        limit: int,
        project_id: str | None,
        /,
        *,
        include_global: bool = True,
    ) -> list[tuple[str, float]]: ...


async def keyword_ranked(
    *,
    run_storage: RunStorage,
    keyword_search: KeywordSearch,
    query: str,
    limit: int,
    project_id: str | None,
    include_global: bool = True,
) -> list[str]:
    """Run keyword search and return ranked memory IDs for RRF merge."""
    results = cast(
        list[tuple[str, float]],
        await run_storage(
            keyword_search,
            query,
            limit,
            project_id,
            include_global=include_global,
        ),
    )
    return [memory_id for memory_id, _ in results]


async def keyword_fallback(
    *,
    run_storage: RunStorage,
    storage: LocalMemoryManager,
    keyword_search: KeywordSearch,
    query: str,
    limit: int,
    project_id: str | None,
    memory_type: str | None,
    tags_all: list[str] | None,
    tags_any: list[str] | None,
    tags_none: list[str] | None,
    include_global: bool = True,
) -> list[Memory]:
    """Keyword search fallback when vector search is unavailable.

    Returned memories carry their normalized BM25 score as ``ranking_score``
    and no ``similarity``: with no vector store nothing can put them on the
    cosine axis, so recall's null-similarity backstop judges them (#20873).
    """
    keyword_results = cast(
        list[tuple[str, float]],
        await run_storage(
            keyword_search,
            query,
            limit * 2,
            project_id,
            include_global=include_global,
        ),
    )
    if not keyword_results:
        return []

    memories: list[Memory] = []
    for memory_id, score in keyword_results:
        try:
            mem = cast(Memory, await run_storage(storage.get_memory, memory_id))
        except ValueError:
            continue
        if memory_type and mem.memory_type != memory_type:
            continue
        if tags_all and not all(tag in (mem.tags or []) for tag in tags_all):
            continue
        if tags_any and not any(tag in (mem.tags or []) for tag in tags_any):
            continue
        if tags_none and any(tag in (mem.tags or []) for tag in tags_none):
            continue
        # The keyword score is a max-normalized BM25 rank statistic -- the top
        # hit is exactly 1.0 whatever its relevance -- not a cosine, so it must
        # not travel as `similarity`: recall's selection gate judges that axis
        # against a cosine floor, and a fabricated 1.0 injected the top hit
        # unconditionally every turn (#20874). With no vector store there is no
        # stored vector to score, which is exactly the case #20873 narrowed the
        # null-similarity backstop to, so these hits carry no similarity and
        # their rank travels as `ranking_score` instead.
        mem.ranking_score = score
        mem.search_via = "keyword"
        memories.append(mem)
        if len(memories) >= limit:
            break
    return memories
