"""FalkorDB graph search helpers for memory search."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from gobby.memory.services._search_constants import (
    _GRAPH_EXPANSION_ENTITY_SEED_LIMIT,
    _GRAPH_RELATED_EXPANSION_TIMEOUT_SECONDS,
    _GRAPH_TRAVERSAL_CONFIDENCE_FACTOR,
)

if TYPE_CHECKING:
    from gobby.memory.services.knowledge_graph import KnowledgeGraphService

logger = logging.getLogger(__name__)


async def search_graph_scored(
    *,
    kg_service: KnowledgeGraphService,
    query_embedding: list[float],
    limit: int = 10,
    min_score: float = 0.5,
    project_id: str | None = None,
) -> list[tuple[str, float]]:
    """Search FalkorDB graph for memory IDs, each scored by entity-match confidence."""
    entity_results = await kg_service.search_entities_by_vector(
        query_embedding=query_embedding,
        limit=limit,
        min_score=min_score,
        project_id=project_id,
    )

    if not entity_results:
        return []

    confidence: dict[str, float] = {}
    direct_memory_ids: list[str] = []
    entity_keys: list[str] = []
    seen_entity_keys: set[str] = set()
    seed_max_score = 0.0
    for result in entity_results:
        entity_key = result.get("entity_key")
        entity_score = float(result.get("score") or 0.0)
        if (
            entity_key
            and entity_key not in seen_entity_keys
            and len(entity_keys) < _GRAPH_EXPANSION_ENTITY_SEED_LIMIT
        ):
            seen_entity_keys.add(entity_key)
            entity_keys.append(entity_key)
            seed_max_score = max(seed_max_score, entity_score)
        for memory_id in result.get("memory_ids", []):
            if memory_id not in direct_memory_ids:
                direct_memory_ids.append(memory_id)
            if entity_score > confidence.get(memory_id, 0.0):
                confidence[memory_id] = entity_score

    traversed_memory_ids: list[str] = []
    if entity_keys:
        try:
            traversed_memory_ids = await asyncio.wait_for(
                kg_service.find_related_memory_ids(
                    entity_keys=entity_keys,
                    max_hops=1,
                    limit=limit,
                    project_id=project_id,
                ),
                timeout=_GRAPH_RELATED_EXPANSION_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "Graph related-memory expansion timed out after %.1fs; returning direct graph hits",
                _GRAPH_RELATED_EXPANSION_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning("Graph related-memory expansion failed: %s", exc)

    traversed_confidence = seed_max_score * _GRAPH_TRAVERSAL_CONFIDENCE_FACTOR
    seen = set(direct_memory_ids)
    merged = list(direct_memory_ids)
    for memory_id in traversed_memory_ids:
        if memory_id not in seen:
            seen.add(memory_id)
            merged.append(memory_id)
        if traversed_confidence > confidence.get(memory_id, 0.0):
            confidence[memory_id] = traversed_confidence

    return [(memory_id, confidence.get(memory_id, 0.0)) for memory_id in merged[:limit]]


async def search_graph_for_memories(
    *,
    kg_service: KnowledgeGraphService,
    query_embedding: list[float],
    limit: int = 10,
    min_score: float = 0.5,
    project_id: str | None = None,
) -> list[str]:
    """Search FalkorDB graph for ranked memory IDs via entity vector similarity."""
    scored = await search_graph_scored(
        kg_service=kg_service,
        query_embedding=query_embedding,
        limit=limit,
        min_score=min_score,
        project_id=project_id,
    )
    return [memory_id for memory_id, _ in scored]
