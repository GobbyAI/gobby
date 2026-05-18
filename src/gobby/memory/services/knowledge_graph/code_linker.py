"""Qdrant-to-code-symbol linking for memory graph entities."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from gobby.memory.neo4j_client import Neo4jConnectionError

from .models import _GraphEntity

if TYPE_CHECKING:
    from gobby.memory.neo4j_client import Neo4jClient
    from gobby.memory.vectorstore import VectorStore

logger = logging.getLogger(__name__)


class KnowledgeGraphCodeLinker:
    """Writes RELATES_TO_CODE edges for entity/code-symbol matches."""

    def __init__(
        self,
        neo4j_client: Neo4jClient,
        vector_store: VectorStore | None,
        *,
        code_link_min_score: float,
        code_symbol_collection_prefix: str,
    ) -> None:
        self._neo4j = neo4j_client
        self._vector_store = vector_store
        self._code_link_min_score = code_link_min_score
        self._code_symbol_collection_prefix = code_symbol_collection_prefix

    async def link_entities_to_code(
        self,
        entities: list[_GraphEntity],
        entity_embeddings: dict[str, list[float]],
        project_id: str,
    ) -> None:
        """Cross-link entities to code symbols via RELATES_TO_CODE edges.

        Searches the code symbol Qdrant collection for each entity embedding
        and writes edges to Neo4j for matches above the similarity threshold.
        Gracefully no-ops if the collection doesn't exist.
        """
        vector_store = self._vector_store
        if vector_store is None:
            return
        collection = f"{self._code_symbol_collection_prefix}{project_id}"
        links: list[dict[str, Any]] = []
        for entity in entities:
            embedding = entity_embeddings.get(entity.entity_key)
            if not embedding:
                continue
            try:
                results = await vector_store.search(
                    query_embedding=embedding,
                    collection_name=collection,
                    limit=3,
                )
                for symbol_id, score in results:
                    if score >= self._code_link_min_score:
                        links.append(
                            {
                                "entity_key": entity.entity_key,
                                "symbol_id": symbol_id,
                                "score": score,
                            }
                        )
            except Exception as e:
                logger.debug(
                    "Code symbol search failed for entity %s: %s",
                    entity.name,
                    e,
                )
                continue

        if not links:
            return

        try:
            await self._neo4j.query(
                "UNWIND $links AS link "
                "MATCH (e:_Entity {entity_key: link.entity_key}) "
                "MATCH (c:CodeSymbol {id: link.symbol_id, project: $project_id}) "
                "MERGE (e)-[r:RELATES_TO_CODE]->(c) "
                "SET r.score = link.score, r.updated_at = datetime()",
                {"links": links, "project_id": project_id},
            )
            logger.debug(
                "Wrote %d RELATES_TO_CODE edges for project %s",
                len(links),
                project_id,
            )
        except Neo4jConnectionError as e:
            logger.warning("Neo4j unreachable during RELATES_TO_CODE write: %s", e)
        except Exception as e:
            logger.warning("Failed to write RELATES_TO_CODE edges: %s", e)
