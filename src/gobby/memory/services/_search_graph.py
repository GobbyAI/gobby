"""FalkorDB graph search helpers for memory search."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from gobby.memory.services._search_constants import (
    _GRAPH_EXPANSION_ENTITY_SEED_LIMIT,
    _GRAPH_TRAVERSAL_CONFIDENCE_FACTOR,
)

if TYPE_CHECKING:
    from gobby.memory.services.knowledge_graph import KnowledgeGraphService

logger = logging.getLogger(__name__)


@dataclass
class GraphScoredResult:
    """Graph search hits plus edge-component breakdown for traversal entrants.

    ``component_map`` (contract §3.2) covers only memory ids that entered the
    result set via weighted graph traversal; direct entity-match hits carry no
    components.
    """

    scored: list[tuple[str, float]] = field(default_factory=list)
    component_map: dict[str, dict[str, float | None]] = field(default_factory=dict)


async def search_graph_scored(
    *,
    kg_service: KnowledgeGraphService,
    query_embedding: list[float],
    related_expansion_timeout_seconds: float,
    limit: int = 10,
    min_score: float = 0.5,
    project_id: str | None = None,
    include_global: bool = True,
) -> GraphScoredResult:
    """Search FalkorDB graph for memory IDs, each scored by entity-match confidence."""
    entity_results = await kg_service.search_entities_by_vector(
        query_embedding=query_embedding,
        limit=limit,
        min_score=min_score,
        project_id=project_id,
        include_global=include_global,
    )

    if not entity_results:
        return GraphScoredResult()

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
    traversal_component_map: dict[str, dict[str, float | None]] = {}
    if entity_keys:
        try:
            traversal = await kg_service.find_related_memory_ids(
                entity_keys=entity_keys,
                max_hops=1,
                limit=limit,
                project_id=project_id,
                include_global=include_global,
                timeout_seconds=related_expansion_timeout_seconds,
            )
            traversed_memory_ids = traversal.memory_ids
            traversal_component_map = traversal.component_map
        except Exception as exc:
            logger.warning("Graph related-memory expansion failed: %s", exc)

    traversed_confidence = seed_max_score * _GRAPH_TRAVERSAL_CONFIDENCE_FACTOR
    seen = set(direct_memory_ids)
    merged = list(direct_memory_ids)
    component_map: dict[str, dict[str, float | None]] = {}
    for memory_id in traversed_memory_ids:
        if memory_id not in seen:
            seen.add(memory_id)
            merged.append(memory_id)
            components = traversal_component_map.get(memory_id)
            if components:
                component_map[memory_id] = components
        if traversed_confidence > confidence.get(memory_id, 0.0):
            confidence[memory_id] = traversed_confidence

    returned = merged[:limit]
    returned_set = set(returned)
    component_map = {
        memory_id: comps for memory_id, comps in component_map.items() if memory_id in returned_set
    }
    return GraphScoredResult(
        scored=[(memory_id, confidence.get(memory_id, 0.0)) for memory_id in returned],
        component_map=component_map,
    )


async def search_graph_for_memories(
    *,
    kg_service: KnowledgeGraphService,
    query_embedding: list[float],
    related_expansion_timeout_seconds: float,
    limit: int = 10,
    min_score: float = 0.5,
    project_id: str | None = None,
) -> list[str]:
    """Search FalkorDB graph for ranked memory IDs via entity vector similarity."""
    result = await search_graph_scored(
        kg_service=kg_service,
        query_embedding=query_embedding,
        related_expansion_timeout_seconds=related_expansion_timeout_seconds,
        limit=limit,
        min_score=min_score,
        project_id=project_id,
    )
    return [memory_id for memory_id, _ in result.scored]
