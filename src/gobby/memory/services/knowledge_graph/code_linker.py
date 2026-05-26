"""Qdrant-to-code-symbol linking for memory graph entities."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from gobby.memory.falkor_client import FalkorConnectionError
from gobby.memory.vectorstore import is_recoverable_vector_store_error

from .models import _GraphEntity

if TYPE_CHECKING:
    from gobby.memory.falkor_client import FalkorClient
    from gobby.memory.vectorstore import VectorStore

logger = logging.getLogger(__name__)


def _is_expected_search_failure(error: Exception) -> bool:
    if is_recoverable_vector_store_error(error):
        return True
    message = str(error).lower()
    return "collection" in message and (
        "not found" in message or "does not exist" in message or "doesn't exist" in message
    )


class KnowledgeGraphCodeLinker:
    """Writes RELATES_TO_CODE edges for entity/code-symbol matches."""

    def __init__(
        self,
        falkor_client: FalkorClient,
        vector_store: VectorStore | None,
        *,
        code_link_min_score: float,
        code_symbol_collection_prefix: str,
        search_limit: int = 3,
    ) -> None:
        self._falkor = falkor_client
        self._vector_store = vector_store
        self._code_link_min_score = code_link_min_score
        self._code_symbol_collection_prefix = code_symbol_collection_prefix
        self.search_limit = search_limit

    async def link_entities_to_code(
        self,
        entities: list[_GraphEntity],
        entity_embeddings: dict[str, list[float]],
        project_id: str,
    ) -> None:
        """Cross-link entities to code symbols via RELATES_TO_CODE edges.

        Searches the code symbol Qdrant collection for each entity embedding
        and writes edges to FalkorDB for matches above the similarity threshold.
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
                    limit=self.search_limit,
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
                if _is_expected_search_failure(e):
                    logger.warning(
                        "Code symbol search unavailable for entity %s in collection %s: %s",
                        entity.name,
                        collection,
                        e,
                    )
                else:
                    logger.error(
                        "Unexpected code symbol search failure",
                        extra={
                            "entity_key": entity.entity_key,
                            "entity_name": entity.name,
                            "collection": collection,
                            "project_id": project_id,
                        },
                        exc_info=True,
                    )
                continue

        if not links:
            return

        try:
            await self._falkor.query(
                "UNWIND $links AS link "
                "MATCH (e:_Entity {entity_key: link.entity_key}) "
                "MATCH (c:CodeSymbol {id: link.symbol_id, project: $project_id}) "
                "MERGE (e)-[r:RELATES_TO_CODE]->(c) "
                "SET r.score = link.score, r.updated_at = timestamp()",
                {"links": links, "project_id": project_id},
            )
            logger.debug(
                "Wrote %d RELATES_TO_CODE edges for project %s",
                len(links),
                project_id,
            )
        except FalkorConnectionError as e:
            logger.warning("FalkorDB unreachable during RELATES_TO_CODE write: %s", e)
        except Exception as e:
            logger.warning("Failed to write RELATES_TO_CODE edges: %s", e)
