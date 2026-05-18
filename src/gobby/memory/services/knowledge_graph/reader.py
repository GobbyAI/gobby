"""Read-side knowledge graph queries."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from gobby.memory.neo4j_client import Neo4jConnectionError

if TYPE_CHECKING:
    from collections.abc import Callable

    from gobby.memory.neo4j_client import Neo4jClient

logger = logging.getLogger(__name__)


class KnowledgeGraphReader:
    """Owns graph read, vector search, and fallback search operations."""

    def __init__(
        self,
        neo4j_client: Neo4jClient,
        embed_fn: Callable[..., Any],
        *,
        embedding_dim: int,
    ) -> None:
        self._neo4j = neo4j_client
        self._embed_fn = embed_fn
        self._embedding_dim = embedding_dim
        self._vector_index_ensured = False

    @property
    def vector_index_ensured(self) -> bool:
        return self._vector_index_ensured

    @vector_index_ensured.setter
    def vector_index_ensured(self, value: bool) -> None:
        self._vector_index_ensured = value

    async def ensure_vector_index(self) -> None:
        """Lazily ensure the entity vector index exists."""
        if self._vector_index_ensured:
            return
        try:
            await self._neo4j.ensure_vector_index(dimensions=self._embedding_dim)
            self._vector_index_ensured = True
        except Neo4jConnectionError:
            logger.debug("Neo4j unreachable, skipping vector index creation")
        except Exception as e:
            logger.warning(f"Failed to ensure vector index: {e}")

    async def search_entities_by_vector(
        self,
        query_embedding: list[float],
        limit: int = 10,
        min_score: float = 0.5,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search entities by vector similarity and return with linked memory IDs."""
        await self.ensure_vector_index()

        try:
            entity_rows = await self._neo4j.vector_search(
                query_embedding=query_embedding,
                limit=limit,
                min_score=min_score,
                project_id=project_id,
            )

            if not entity_rows:
                return []

            entity_keys = [r.get("entity_key", "") for r in entity_rows if r.get("entity_key")]
            memory_map: dict[str, list[str]] = {key: [] for key in entity_keys}

            if entity_keys:
                try:
                    mem_rows = await self._neo4j.query(
                        "UNWIND $entity_keys AS entity_key "
                        "MATCH (e:_Entity {entity_key: entity_key})-[:MENTIONED_IN]->(m:Memory) "
                        "WHERE m.project_id = $project_id "
                        "OR ($project_id IS NULL AND m.project_id IS NULL) "
                        "RETURN entity_key, m.memory_id AS memory_id",
                        {"entity_keys": entity_keys, "project_id": project_id},
                    )
                    for r in mem_rows:
                        key = r.get("entity_key", "")
                        mid = r.get("memory_id")
                        if key in memory_map and mid:
                            memory_map[key].append(mid)
                except Exception as e:
                    logger.debug(f"Failed to batch-fetch memory links: {e}")

            results = []
            for row in entity_rows:
                key = row.get("entity_key", "")
                name = row.get("name", "")
                if not key or not name:
                    continue
                results.append(
                    {
                        "entity_key": key,
                        "name": name,
                        "entity_type": row.get("entity_type") or "entity",
                        "project_id": row.get("project_id"),
                        "labels": row.get("labels", []),
                        "score": row.get("score", 0.0),
                        "memory_ids": memory_map.get(key, []),
                    }
                )

            return results

        except Neo4jConnectionError as e:
            logger.warning(f"Neo4j unreachable during entity vector search: {e}")
            return []
        except Exception as e:
            logger.warning(f"Entity vector search failed: {e}")
            return []

    async def find_related_memory_ids(
        self,
        entity_keys: list[str],
        max_hops: int = 2,
        limit: int = 20,
        project_id: str | None = None,
    ) -> list[str]:
        """Traverse from entities through relationships to find related memory IDs."""
        if not entity_keys:
            return []

        max_hops = max(1, min(max_hops, 3))

        try:
            rows = await self._neo4j.query(
                "UNWIND $entity_keys AS entity_key "
                f"MATCH (start:_Entity {{entity_key: entity_key}})-[*1..{max_hops}]-(related:_Entity)"
                "-[:MENTIONED_IN]->(m:Memory) "
                "WHERE m.project_id = $project_id "
                "OR ($project_id IS NULL AND m.project_id IS NULL) "
                "RETURN DISTINCT m.memory_id AS memory_id LIMIT $limit",
                {"entity_keys": entity_keys, "limit": limit, "project_id": project_id},
            )
            return [r["memory_id"] for r in rows if r.get("memory_id")]
        except Neo4jConnectionError as e:
            logger.warning(f"Neo4j unreachable during graph traversal: {e}")
            return []
        except Exception as e:
            logger.warning(f"Graph traversal failed: {e}")
            return []

    async def get_entity_graph(
        self,
        limit: int = 500,
        project_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Get the entity graph for visualization."""
        try:
            return await self._neo4j.get_entity_graph(limit=limit, project_id=project_id)
        except Neo4jConnectionError as e:
            logger.warning(f"Neo4j unreachable: {e}")
            return None
        except Exception as e:
            logger.warning(f"Neo4j query failed: {e}")
            return None

    async def get_entity_neighbors(
        self,
        entity_key: str,
        project_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Get neighbors for a single entity."""
        try:
            return await self._neo4j.get_entity_neighbors(entity_key, project_id=project_id)
        except Neo4jConnectionError as e:
            logger.warning(f"Neo4j unreachable: {e}")
            return None
        except Exception as e:
            logger.warning(f"Neo4j query failed: {e}")
            return None

    async def search_graph(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search the knowledge graph, using vector search before substring fallback."""
        if self._embed_fn is not None:
            try:
                embedding = await self._embed_fn(query, is_query=True)
                results = await self.search_entities_by_vector(
                    query_embedding=embedding,
                    limit=limit,
                    min_score=0.3,
                )
                if results:
                    return [
                        {
                            "entity_key": r["entity_key"],
                            "name": r["name"],
                            "entity_type": r.get("entity_type") or "entity",
                            "project_id": r.get("project_id"),
                            "labels": r["labels"],
                            "score": r["score"],
                        }
                        for r in results
                    ]
            except Exception as e:
                logger.debug(f"Vector graph search failed, falling back to substring: {e}")

        try:
            rows = await self._neo4j.query(
                "MATCH (n:_Entity) WHERE toLower(n.name) CONTAINS toLower($query) "
                "RETURN n.entity_key AS entity_key, n.name AS name, "
                "n.entity_type AS entity_type, n.project_id AS project_id, "
                "labels(n) AS labels, properties(n) AS props "
                "LIMIT $limit",
                {"query": query, "limit": limit},
            )
            return rows
        except Neo4jConnectionError as e:
            logger.warning(f"Neo4j unreachable: {e}")
            return []
        except Exception as e:
            logger.warning(f"Graph search failed: {e}")
            return []
