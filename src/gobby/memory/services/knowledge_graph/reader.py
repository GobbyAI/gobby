"""Read-side knowledge graph queries."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from gobby.memory.falkor_client import FalkorConnectionError

if TYPE_CHECKING:
    from collections.abc import Callable

    from gobby.memory.falkor_client import FalkorClient

logger = logging.getLogger(__name__)

_DIRECT_MEMORY_LINK_FACTOR = 4
_RELATED_ENTITY_SEED_LIMIT = 8
_RELATED_ENTITY_NEIGHBOR_LIMIT = 8
_RELATED_ENTITY_LIMIT_FACTOR = 4
_STRUCTURAL_RELATIONSHIP_TYPES = ("MENTIONED_IN", "RELATES_TO_CODE")
_TRAVERSAL_TIMEOUT_THRESHOLD = 3
_TRAVERSAL_TIMEOUT_COOLDOWN_SECONDS = 60.0
_TRAVERSAL_WARNING_INTERVAL_SECONDS = 60.0


class KnowledgeGraphReader:
    """Owns graph read, vector search, and fallback search operations."""

    def __init__(
        self,
        falkor_client: FalkorClient,
        embed_fn: Callable[..., Any] | None,
        *,
        embedding_dim: int,
    ) -> None:
        self._falkor = falkor_client
        self._embed_fn = embed_fn
        self._embedding_dim = embedding_dim
        self._vector_index_ensured = False
        self._traversal_timeout_count = 0
        self._traversal_disabled_until = 0.0
        self._last_traversal_warning_at = float("-inf")
        self._suppressed_traversal_warnings = 0

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
            await self._falkor.ensure_vector_index(dimension=self._embedding_dim)
            self._vector_index_ensured = True
        except FalkorConnectionError:
            logger.debug("FalkorDB unreachable, skipping vector index creation")
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
            entity_rows = await self._falkor.vector_search(
                query_embedding=query_embedding,
                limit=limit,
                min_score=min_score,
                project_id=project_id,
            )

            if not entity_rows:
                return []

            entity_keys = [r.get("entity_key", "") for r in entity_rows if r.get("entity_key")]
            memory_map: dict[str, list[str]] = {key: [] for key in entity_keys}
            memory_link_limit = max(limit, 0) * _DIRECT_MEMORY_LINK_FACTOR

            if entity_keys and memory_link_limit > 0:
                try:
                    mem_rows = await self._falkor.query(
                        "UNWIND $entity_keys AS entity_key "
                        "MATCH (e:_Entity {entity_key: entity_key})-[:MENTIONED_IN]->(m:Memory) "
                        "WHERE (e.project_id = $project_id "
                        "OR ($project_id IS NULL AND e.project_id IS NULL)) "
                        "AND (m.project_id = $project_id "
                        "OR ($project_id IS NULL AND m.project_id IS NULL)) "
                        "RETURN entity_key, m.memory_id AS memory_id "
                        "ORDER BY m.updated_at DESC LIMIT $memory_link_limit",
                        {
                            "entity_keys": entity_keys,
                            "project_id": project_id,
                            "memory_link_limit": memory_link_limit,
                        },
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

        except FalkorConnectionError as e:
            logger.warning(f"FalkorDB unreachable during entity vector search: {e}")
            return []
        except Exception as e:
            logger.warning(f"Entity vector search failed: {e}")
            return []

    def _related_traversal_is_disabled(self) -> bool:
        return time.monotonic() < self._traversal_disabled_until

    @staticmethod
    def _is_query_timeout_error(error: BaseException) -> bool:
        message = str(error).lower()
        response_body = getattr(error, "response_body", None)
        if response_body is not None:
            message = f"{message} {response_body!s}".lower()
        return "query timed out" in message

    def _record_traversal_timeout(self, error: BaseException) -> None:
        now = time.monotonic()
        self._traversal_timeout_count += 1
        if self._traversal_timeout_count >= _TRAVERSAL_TIMEOUT_THRESHOLD:
            self._traversal_disabled_until = now + _TRAVERSAL_TIMEOUT_COOLDOWN_SECONDS
        self._warn_traversal_timeout(error, now)

    def _record_traversal_success(self) -> None:
        self._traversal_timeout_count = 0
        self._traversal_disabled_until = 0.0

    def _warn_traversal_timeout(self, error: BaseException, now: float) -> None:
        if now - self._last_traversal_warning_at < _TRAVERSAL_WARNING_INTERVAL_SECONDS:
            self._suppressed_traversal_warnings += 1
            return

        suppressed = self._suppressed_traversal_warnings
        self._suppressed_traversal_warnings = 0
        self._last_traversal_warning_at = now
        logger.warning(
            "FalkorDB graph traversal query timed out; consecutive_timeouts=%d "
            "cooldown_seconds=%.0f suppressed_warnings=%d error=%s",
            self._traversal_timeout_count,
            _TRAVERSAL_TIMEOUT_COOLDOWN_SECONDS,
            suppressed,
            error,
        )

    async def _find_related_entity_keys(
        self,
        seed_keys: list[str],
        max_hops: int,
        limit: int,
        project_id: str | None,
    ) -> list[str]:
        related_keys: list[str] = []
        seen = set(seed_keys)
        frontier = list(seed_keys)
        max_related_entities = max(
            _RELATED_ENTITY_NEIGHBOR_LIMIT,
            limit * _RELATED_ENTITY_LIMIT_FACTOR,
        )

        for _ in range(max_hops):
            next_frontier: list[str] = []
            for source_key in frontier:
                if len(related_keys) >= max_related_entities:
                    break
                rows = await self._falkor.query(
                    "MATCH (start:_Entity {entity_key: $entity_key})-[r]-(neighbor:_Entity) "
                    "WHERE (start.project_id = $project_id "
                    "OR ($project_id IS NULL AND start.project_id IS NULL)) "
                    "AND (neighbor.project_id = $project_id "
                    "OR ($project_id IS NULL AND neighbor.project_id IS NULL)) "
                    "AND NOT (type(r) IN $excluded_relationship_types) "
                    "RETURN neighbor.entity_key AS related_entity_key "
                    "ORDER BY related_entity_key LIMIT $neighbor_limit",
                    {
                        "entity_key": source_key,
                        "project_id": project_id,
                        "neighbor_limit": _RELATED_ENTITY_NEIGHBOR_LIMIT,
                        "excluded_relationship_types": list(_STRUCTURAL_RELATIONSHIP_TYPES),
                    },
                )
                for row in rows[:_RELATED_ENTITY_NEIGHBOR_LIMIT]:
                    related_key = row.get("related_entity_key")
                    if not related_key or related_key in seen:
                        continue
                    seen.add(related_key)
                    related_keys.append(related_key)
                    next_frontier.append(related_key)
                    if len(related_keys) >= max_related_entities:
                        break
            if not next_frontier or len(related_keys) >= max_related_entities:
                break
            frontier = next_frontier

        return related_keys

    async def find_related_memory_ids(
        self,
        entity_keys: list[str],
        max_hops: int = 2,
        limit: int = 20,
        project_id: str | None = None,
    ) -> list[str]:
        """Traverse from entities through relationships to find related memory IDs."""
        if not entity_keys or limit <= 0:
            return []
        if self._related_traversal_is_disabled():
            return []

        max_hops = max(1, min(max_hops, 3))
        seed_keys = list(dict.fromkeys(entity_keys))[:_RELATED_ENTITY_SEED_LIMIT]
        if not seed_keys:
            return []

        try:
            related_entity_keys = await self._find_related_entity_keys(
                seed_keys,
                max_hops,
                limit,
                project_id,
            )
            if not related_entity_keys:
                self._record_traversal_success()
                return []

            rows = await self._falkor.query(
                "UNWIND $entity_keys AS entity_key "
                "MATCH (e:_Entity {entity_key: entity_key})"
                "-[:MENTIONED_IN]->(m:Memory) "
                "WHERE (e.project_id = $project_id "
                "OR ($project_id IS NULL AND e.project_id IS NULL)) "
                "AND (m.project_id = $project_id "
                "OR ($project_id IS NULL AND m.project_id IS NULL)) "
                "RETURN DISTINCT m.memory_id AS memory_id, m.updated_at AS updated_at "
                "ORDER BY updated_at DESC LIMIT $limit",
                {"entity_keys": related_entity_keys, "limit": limit, "project_id": project_id},
            )
            self._record_traversal_success()
            return [r["memory_id"] for r in rows if r.get("memory_id")]
        except FalkorConnectionError as e:
            if self._is_query_timeout_error(e):
                self._record_traversal_timeout(e)
                return []
            self._record_traversal_success()
            logger.warning(f"FalkorDB unreachable during graph traversal: {e}")
            return []
        except Exception as e:
            if self._is_query_timeout_error(e):
                self._record_traversal_timeout(e)
                return []
            self._record_traversal_success()
            logger.warning(f"Graph traversal failed: {e}")
            return []

    async def get_entity_graph(
        self,
        limit: int = 500,
        project_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Get the entity graph for visualization."""
        try:
            return await self._falkor.get_entity_graph(limit=limit, project_id=project_id)
        except FalkorConnectionError as e:
            logger.warning(f"FalkorDB unreachable: {e}")
            return None
        except Exception as e:
            logger.warning(f"FalkorDB query failed: {e}")
            return None

    async def get_entity_neighbors(
        self,
        entity_key: str,
        project_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Get neighbors for a single entity."""
        try:
            return await self._falkor.get_entity_neighbors(entity_key, project_id=project_id)
        except FalkorConnectionError as e:
            logger.warning(f"FalkorDB unreachable: {e}")
            return None
        except Exception as e:
            logger.warning(f"FalkorDB query failed: {e}")
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
            rows = await self._falkor.query(
                "MATCH (n:_Entity) WHERE toLower(n.name) CONTAINS toLower($query) "
                "RETURN n.entity_key AS entity_key, n.name AS name, "
                "n.entity_type AS entity_type, n.project_id AS project_id, "
                "labels(n) AS labels, properties(n) AS props "
                "LIMIT $limit",
                {"query": query, "limit": limit},
            )
            return rows
        except FalkorConnectionError as e:
            logger.warning(f"FalkorDB unreachable: {e}")
            return []
        except Exception as e:
            logger.warning(f"Graph search failed: {e}")
            return []
