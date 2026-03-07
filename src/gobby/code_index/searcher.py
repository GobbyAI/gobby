"""Hybrid search for code symbols.

Combines SQLite name search, Qdrant semantic search, and Neo4j graph boost
using Reciprocal Rank Fusion (RRF) for unified ranking.

Degrades gracefully: SQLite-only if Qdrant/Neo4j unavailable.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from gobby.code_index.graph import CodeGraph
from gobby.code_index.models import Symbol
from gobby.code_index.storage import CodeIndexStorage
from gobby.config.code_index import CodeIndexConfig

logger = logging.getLogger(__name__)

# RRF constant (same as memory/manager.py)
RRF_K = 60


def _rrf_score(rank: int) -> float:
    """Reciprocal Rank Fusion score."""
    return 1.0 / (RRF_K + rank)


class CodeSearcher:
    """Hybrid search across code symbols."""

    def __init__(
        self,
        storage: CodeIndexStorage,
        vector_store: Any | None = None,
        embed_fn: Callable[..., Any] | None = None,
        graph: CodeGraph | None = None,
        config: CodeIndexConfig | None = None,
    ) -> None:
        self._storage = storage
        self._vector_store = vector_store
        self._embed_fn = embed_fn
        self._graph = graph
        self._config = config

    async def search(
        self,
        query: str,
        project_id: str,
        kind: str | None = None,
        file_path: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Hybrid search combining name, semantic, and graph sources.

        Returns list of dicts with symbol data + score + source info.
        """
        # Source 1: SQLite name search (always available)
        name_results = self._storage.search_symbols_by_name(
            query=query,
            project_id=project_id,
            kind=kind,
            file_path=file_path,
            limit=limit * 2,
        )

        # Build score map: symbol_id -> {source: rank}
        score_map: dict[str, dict[str, int]] = {}
        symbol_cache: dict[str, Symbol] = {}

        for rank, sym in enumerate(name_results):
            score_map[sym.id] = {"name": rank}
            symbol_cache[sym.id] = sym

        # Source 2: Qdrant semantic search (if available)
        if self._vector_store is not None and self._embed_fn is not None:
            try:
                semantic_results = await self._semantic_search(query, project_id, limit * 2)
                for rank, (sym_id, _score) in enumerate(semantic_results):
                    if sym_id not in score_map:
                        score_map[sym_id] = {}
                    score_map[sym_id]["semantic"] = rank
            except Exception as e:
                logger.debug(f"Semantic search failed (degrading gracefully): {e}")

        # Source 3: Neo4j graph boost (if available)
        if self._graph is not None and self._graph.available:
            try:
                graph_ids = await self._graph_boost(query, project_id)
                for rank, sym_id in enumerate(graph_ids):
                    if sym_id not in score_map:
                        score_map[sym_id] = {}
                    score_map[sym_id]["graph"] = rank
            except Exception as e:
                logger.debug(f"Graph boost failed (degrading gracefully): {e}")

        # RRF merge
        final_scores: list[tuple[str, float]] = []
        for sym_id, sources in score_map.items():
            score = sum(_rrf_score(rank) for rank in sources.values())
            final_scores.append((sym_id, score))

        # Sort by score descending
        final_scores.sort(key=lambda x: x[1], reverse=True)

        # Build result list
        results: list[dict[str, Any]] = []
        for sym_id, score in final_scores[:limit]:
            matched = symbol_cache.get(sym_id)
            if matched is None:
                matched = self._storage.get_symbol(sym_id)
            if matched is None:
                continue

            result = matched.to_dict()
            result["_score"] = round(score, 4)
            result["_sources"] = list(score_map.get(sym_id, {}).keys())
            results.append(result)

        return results

    async def _semantic_search(
        self, query: str, project_id: str, limit: int
    ) -> list[tuple[str, float]]:
        """Run Qdrant semantic search. Returns [(symbol_id, score)]."""
        if self._embed_fn is None or self._vector_store is None:
            return []

        embedding = await self._embed_fn(query)
        if embedding is None:
            return []

        collection = (
            f"{self._config.qdrant_collection_prefix}{project_id}"
            if self._config
            else f"code_symbols_{project_id}"
        )

        hits = await self._vector_store.search(
            collection_name=collection,
            query_vector=embedding,
            limit=limit,
        )

        return [(hit.id, hit.score) for hit in hits]

    async def _graph_boost(self, query: str, project_id: str) -> list[str]:
        """Get symbol IDs related to query via graph. Returns IDs for boosting."""
        if self._graph is None:
            return []

        # Use the query as a symbol name to find callers/usages
        callers = await self._graph.find_callers(query, project_id, limit=10)
        usages = await self._graph.find_usages(query, project_id, limit=10)

        ids: list[str] = []
        seen: set[str] = set()
        for record in callers + usages:
            sid = record.get("caller_id") or record.get("source_id", "")
            if sid and sid not in seen:
                ids.append(sid)
                seen.add(sid)

        return ids

    def search_text(
        self,
        query: str,
        project_id: str,
        file_path: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Full-text search across symbol names and signatures (SQLite only)."""
        results = self._storage.search_symbols_by_name(
            query=query,
            project_id=project_id,
            file_path=file_path,
            limit=limit,
        )
        return [sym.to_dict() for sym in results]
