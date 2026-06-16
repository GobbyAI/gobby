"""Read-side knowledge graph queries."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.memory.falkor_client import FalkorConnectionError
from gobby.memory.scoring import temporal_decay

from .clustering import EntityVector

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from gobby.memory.falkor_client import FalkorClient

    # Given candidate memory IDs (and an optional project scope), return the subset that
    # are currently active (not soft-hidden) in the memory store -- the source of truth
    # for visibility. The graph retains soft-hidden Memory nodes until purge, so entity
    # reads must consult this to drop entities/relationships backed only by hidden rows.
    ActiveMemoryFilter = Callable[[Sequence[str], str | None], Awaitable[set[str]]]

logger = logging.getLogger(__name__)

_DIRECT_MEMORY_LINK_FACTOR = 4
_RELATED_ENTITY_SEED_LIMIT = 8
_RELATED_ENTITY_NEIGHBOR_LIMIT = 8
_RELATED_ENTITY_LIMIT_FACTOR = 4
# Generous per-source candidate cap pulled from FalkorDB before Python-side
# weight/decay scoring; the top _RELATED_ENTITY_NEIGHBOR_LIMIT survive per source.
_RELATED_ENTITY_QUERY_LIMIT = 64
_STRUCTURAL_RELATIONSHIP_TYPES = ("MENTIONED_IN", "RELATES_TO_CODE")
_TRAVERSAL_TIMEOUT_THRESHOLD = 3
_TRAVERSAL_TIMEOUT_COOLDOWN_SECONDS = 60.0
_TRAVERSAL_WARNING_INTERVAL_SECONDS = 60.0
_CLUSTER_ENTITY_QUERY_LIMIT = 256


def _edge_timestamp_to_iso(value: Any) -> str | None:
    """Convert a FalkorDB ``timestamp()`` (epoch ms) edge value to an ISO string.

    Edge ``updated_at`` is written via Cypher ``timestamp()`` (milliseconds since
    epoch). ``temporal_decay`` expects an ISO-8601 string, so numeric values are
    converted; strings pass through; anything else yields ``None`` (no decay).
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000.0, tz=UTC).isoformat()
        except (ValueError, OverflowError, OSError):
            return None
    return None


class KnowledgeGraphReader:
    """Owns graph read, vector search, and fallback search operations."""

    def __init__(
        self,
        falkor_client: FalkorClient,
        embed_fn: Callable[..., Any] | None,
        *,
        embedding_dim: int,
        graph_edge_decay: bool = False,
        edge_half_life_days: float = 30.0,
        cluster_recall_expansion: bool = False,
        cluster_expansion_per_entity: int = 3,
        active_memory_filter: ActiveMemoryFilter | None = None,
    ) -> None:
        self._falkor = falkor_client
        self._embed_fn = embed_fn
        self._embedding_dim = embedding_dim
        self._graph_edge_decay = graph_edge_decay
        self._edge_half_life_days = edge_half_life_days
        self._cluster_recall_expansion = cluster_recall_expansion
        self._cluster_expansion_per_entity = max(cluster_expansion_per_entity, 0)
        self._active_memory_filter = active_memory_filter
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
                        "OR e.project_id IS NULL) "
                        "AND (m.project_id = $project_id "
                        "OR m.project_id IS NULL) "
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

    def _edge_score(self, edge_weight: Any, updated_at: Any) -> float:
        """Combine an edge weight with optional recency decay for candidate ranking.

        Decay acts at candidate selection (which neighbors survive the cap), not
        final ranking. ``edge_weight`` falls back to a neutral ``1.0`` so unweighted
        and ``coalesce(r.weight, 1.0)`` edges degrade to the prior behavior.
        """
        try:
            weight = float(edge_weight) if edge_weight is not None else 1.0
        except (TypeError, ValueError):
            weight = 1.0
        if not self._graph_edge_decay:
            return weight
        iso = _edge_timestamp_to_iso(updated_at)
        if iso is None:
            return weight
        return weight * temporal_decay(iso, self._edge_half_life_days)

    async def _find_related_entity_keys(
        self,
        seed_keys: list[str],
        max_hops: int,
        limit: int,
        project_id: str | None,
    ) -> list[str]:
        related_keys: list[str] = []
        seen = set(seed_keys)
        # Frontier carries the accumulated path score so multi-hop ranking is global
        # rather than only per-source.
        frontier: list[tuple[str, float]] = [(key, 1.0) for key in seed_keys]
        max_related_entities = max(
            _RELATED_ENTITY_NEIGHBOR_LIMIT,
            limit * _RELATED_ENTITY_LIMIT_FACTOR,
        )

        for _ in range(max_hops):
            hop_best: dict[str, float] = {}
            for source_key, source_score in frontier:
                rows = await self._falkor.query(
                    "MATCH (start:_Entity {entity_key: $entity_key})-[r]-(neighbor:_Entity) "
                    "WHERE (start.project_id = $project_id "
                    "OR start.project_id IS NULL) "
                    "AND (neighbor.project_id = $project_id "
                    "OR neighbor.project_id IS NULL) "
                    "AND NOT (type(r) IN $excluded_relationship_types) "
                    "RETURN neighbor.entity_key AS related_entity_key, "
                    "coalesce(r.weight, 1.0) AS edge_weight, r.updated_at AS updated_at "
                    "ORDER BY coalesce(r.weight, 1.0) DESC "
                    "LIMIT $neighbor_limit",
                    {
                        "entity_key": source_key,
                        "project_id": project_id,
                        "neighbor_limit": _RELATED_ENTITY_QUERY_LIMIT,
                        "excluded_relationship_types": list(_STRUCTURAL_RELATIONSHIP_TYPES),
                    },
                )
                # Score each candidate, keep the strongest edge per neighbor, then cap
                # to the top _RELATED_ENTITY_NEIGHBOR_LIMIT for this source.
                best_by_key: dict[str, float] = {}
                for row in rows:
                    related_key = row.get("related_entity_key")
                    if not related_key or related_key in seen:
                        continue
                    edge_score = self._edge_score(row.get("edge_weight"), row.get("updated_at"))
                    if related_key not in best_by_key or edge_score > best_by_key[related_key]:
                        best_by_key[related_key] = edge_score
                scored = sorted(best_by_key.items(), key=lambda item: (-item[1], item[0]))
                for related_key, edge_score in scored[:_RELATED_ENTITY_NEIGHBOR_LIMIT]:
                    path_score = source_score * edge_score
                    if related_key not in hop_best or path_score > hop_best[related_key]:
                        hop_best[related_key] = path_score
            if not hop_best:
                break
            # Globally order this hop's fresh neighbors by accumulated path score.
            ordered = sorted(hop_best.items(), key=lambda item: (-item[1], item[0]))
            next_frontier: list[tuple[str, float]] = []
            for related_key, path_score in ordered:
                if related_key in seen:
                    continue
                seen.add(related_key)
                related_keys.append(related_key)
                next_frontier.append((related_key, path_score))
                if len(related_keys) >= max_related_entities:
                    break
            if not next_frontier or len(related_keys) >= max_related_entities:
                break
            frontier = next_frontier

        return related_keys

    async def fetch_project_entity_vectors(
        self,
        project_id: str | None,
    ) -> list[EntityVector]:
        """Fetch project-scoped entity embeddings for offline clustering."""
        rows = await self._falkor.query(
            "MATCH (e:_Entity) "
            "WHERE (e.project_id = $project_id "
            "OR e.project_id IS NULL) "
            "RETURN e.entity_key AS entity_key, e.name AS name, e.embedding AS embedding "
            "ORDER BY e.entity_key",
            {"project_id": project_id},
        )
        return [
            EntityVector(
                entity_key=str(row.get("entity_key") or ""),
                name=str(row.get("name") or ""),
                embedding=row.get("embedding"),
            )
            for row in rows
            if row.get("entity_key")
        ]

    async def _find_cluster_entity_keys(
        self,
        source_keys: list[str],
        seed_keys: list[str],
        related_entity_keys: list[str],
        project_id: str | None,
    ) -> list[str]:
        if not self._cluster_recall_expansion or self._cluster_expansion_per_entity <= 0:
            return []
        expansion_sources = list(dict.fromkeys([*source_keys, *related_entity_keys]))
        if not expansion_sources:
            return []
        expansion_limit = min(
            len(expansion_sources) * self._cluster_expansion_per_entity,
            _CLUSTER_ENTITY_QUERY_LIMIT,
        )
        if expansion_limit <= 0:
            return []

        excluded_keys = list(dict.fromkeys([*seed_keys, *related_entity_keys]))
        try:
            rows = await self._falkor.query(
                "UNWIND $source_keys AS source_key "
                "MATCH (source:_Entity {entity_key: source_key}) "
                "WHERE (source.project_id = $project_id "
                "OR source.project_id IS NULL) "
                "AND source.cluster_id IS NOT NULL AND source.cluster_id >= 0 "
                "MATCH (candidate:_Entity {cluster_id: source.cluster_id}) "
                "WHERE (candidate.project_id = $project_id "
                "OR candidate.project_id IS NULL) "
                "AND candidate.cluster_id IS NOT NULL AND candidate.cluster_id >= 0 "
                "AND NOT (candidate.entity_key IN $excluded_keys) "
                "RETURN DISTINCT candidate.entity_key AS entity_key, "
                "candidate.cluster_id AS cluster_id "
                "ORDER BY cluster_id ASC, entity_key ASC LIMIT $limit",
                {
                    "source_keys": expansion_sources,
                    "excluded_keys": excluded_keys,
                    "project_id": project_id,
                    "limit": expansion_limit,
                },
            )
        except Exception as e:
            if self._is_query_timeout_error(e):
                self._record_traversal_timeout(e)
            else:
                logger.debug("Cluster recall expansion failed: %s", e)
            return []

        cluster_keys: list[str] = []
        seen = set(excluded_keys)
        for row in rows:
            entity_key = row.get("entity_key")
            cluster_id = row.get("cluster_id")
            if not entity_key or entity_key in seen or cluster_id is None:
                continue
            try:
                if int(cluster_id) < 0:
                    continue
            except (TypeError, ValueError):
                continue
            seen.add(entity_key)
            cluster_keys.append(entity_key)
        return cluster_keys

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
            cluster_entity_keys = await self._find_cluster_entity_keys(
                seed_keys,
                seed_keys,
                related_entity_keys,
                project_id,
            )
            memory_entity_keys = list(dict.fromkeys([*related_entity_keys, *cluster_entity_keys]))
            if not memory_entity_keys:
                self._record_traversal_success()
                return []

            rows = await self._falkor.query(
                "UNWIND $entity_keys AS entity_key "
                "MATCH (e:_Entity {entity_key: entity_key})"
                "-[:MENTIONED_IN]->(m:Memory) "
                "WHERE (e.project_id = $project_id "
                "OR e.project_id IS NULL) "
                "AND (m.project_id = $project_id "
                "OR m.project_id IS NULL) "
                "RETURN DISTINCT m.memory_id AS memory_id, m.updated_at AS updated_at "
                "ORDER BY updated_at DESC LIMIT $limit",
                {"entity_keys": memory_entity_keys, "limit": limit, "project_id": project_id},
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
        """Get the entity graph for visualization, hiding soft-deleted-only artifacts."""
        try:
            graph = await self._falkor.get_entity_graph(limit=limit, project_id=project_id)
        except FalkorConnectionError as e:
            logger.warning(f"FalkorDB unreachable: {e}")
            return None
        except Exception as e:
            logger.warning(f"FalkorDB query failed: {e}")
            return None
        return await self._filter_graph_by_active_memories(graph, project_id)

    async def get_entity_neighbors(
        self,
        entity_key: str,
        project_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Get neighbors for a single entity, hiding soft-deleted-only artifacts."""
        try:
            graph = await self._falkor.get_entity_neighbors(entity_key, project_id=project_id)
        except FalkorConnectionError as e:
            logger.warning(f"FalkorDB unreachable: {e}")
            return None
        except Exception as e:
            logger.warning(f"FalkorDB query failed: {e}")
            return None
        return await self._filter_graph_by_active_memories(graph, project_id)

    async def _filter_graph_by_active_memories(
        self,
        graph: dict[str, Any] | None,
        project_id: str | None,
    ) -> dict[str, Any] | None:
        """Drop entities/relationships not backed by at least one active memory.

        The graph keeps soft-hidden ``Memory`` nodes until purge, so an entity-graph
        read must consult the memory store (the visibility source of truth). An entity
        mentioned by both hidden and active memories stays visible; an entity backed
        only by hidden (or no longer existing) memories is dropped, and any relationship
        touching a dropped entity drops with it. Without an injected store filter (e.g.
        in tests) the raw graph is returned unchanged.
        """
        if graph is None or self._active_memory_filter is None:
            return graph
        entities = graph.get("entities") or []
        relationships = graph.get("relationships") or []
        if not entities:
            return graph

        entity_keys = [str(e["entity_key"]) for e in entities if e.get("entity_key")]
        backing = await self._entity_backing_memories(entity_keys, project_id)
        if backing is None:
            # Backing lookup failed (e.g. FalkorDB transient error); fail open rather
            # than blank the entire visualization on a non-visibility fault.
            return graph
        all_memory_ids = sorted({mid for ids in backing.values() for mid in ids})
        active_ids = await self._active_memory_filter(all_memory_ids, project_id)

        visible_keys = {
            key
            for key, memory_ids in backing.items()
            if any(memory_id in active_ids for memory_id in memory_ids)
        }
        filtered_entities = [e for e in entities if str(e.get("entity_key") or "") in visible_keys]
        filtered_relationships = [
            r
            for r in relationships
            if str(r.get("source_key") or "") in visible_keys
            and str(r.get("target_key") or "") in visible_keys
        ]
        return {"entities": filtered_entities, "relationships": filtered_relationships}

    async def _entity_backing_memories(
        self,
        entity_keys: list[str],
        project_id: str | None,
    ) -> dict[str, list[str]] | None:
        """Map each entity key to the memory IDs that mention it via ``MENTIONED_IN``.

        Returns ``None`` when the graph lookup fails so callers can fail open instead
        of treating a transient error as "no entity has active backing".
        """
        if not entity_keys:
            return {}
        try:
            rows = await self._falkor.query(
                "UNWIND $entity_keys AS entity_key "
                "MATCH (e:_Entity {entity_key: entity_key})-[:MENTIONED_IN]->(m:Memory) "
                "WHERE (e.project_id = $project_id "
                "OR e.project_id IS NULL) "
                "AND (m.project_id = $project_id "
                "OR m.project_id IS NULL) "
                "RETURN e.entity_key AS entity_key, "
                "collect(DISTINCT m.memory_id) AS memory_ids",
                {"entity_keys": entity_keys, "project_id": project_id},
            )
        except FalkorConnectionError as e:
            logger.warning(f"FalkorDB unreachable resolving entity backing memories: {e}")
            return None
        except Exception as e:
            logger.warning(f"Failed to resolve entity backing memories: {e}")
            return None

        backing: dict[str, list[str]] = {}
        for row in rows:
            key = row.get("entity_key")
            if not key:
                continue
            backing[str(key)] = [str(mid) for mid in (row.get("memory_ids") or []) if mid]
        return backing

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
