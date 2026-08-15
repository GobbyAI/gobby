"""Read-side knowledge graph queries."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypedDict

from gobby.memory.falkor_client import FalkorConnectionError
from gobby.memory.scoring import temporal_decay

from .clustering import EntityVector
from .writer import COOCCUR_ALPHA, COOCCUR_SUPPORT_CAP

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from gobby.memory.falkor_client import FalkorClient

    # Given candidate memory IDs (and an optional project scope), return previews
    # ({id: ActiveMemoryPreview}) for the subset that are currently active (not
    # soft-hidden) in the memory store -- the source of truth for visibility. The
    # graph retains soft-hidden Memory nodes until purge, so entity reads must
    # consult this to drop entities/relationships backed only by hidden rows; the
    # preview content feeds the entity-card enrichment on graph reads.
    ActiveMemoryLookup = Callable[
        [Sequence[str], str | None], Awaitable[dict[str, "ActiveMemoryPreview"]]
    ]


class ActiveMemoryPreview(TypedDict):
    """Content preview for one active memory backing a graph entity."""

    content: str
    updated_at: datetime | None


logger = logging.getLogger(__name__)

_DIRECT_MEMORY_LINK_FACTOR = 4
_RELATED_ENTITY_SEED_LIMIT = 8
_RELATED_ENTITY_NEIGHBOR_LIMIT = 8
_RELATED_ENTITY_LIMIT_FACTOR = 4
_STRUCTURAL_RELATIONSHIP_TYPES = ("MENTIONED_IN", "RELATES_TO_CODE")
_TRAVERSAL_TIMEOUT_THRESHOLD = 3
_TRAVERSAL_TIMEOUT_COOLDOWN_SECONDS = 60.0
_TRAVERSAL_WARNING_INTERVAL_SECONDS = 60.0
_CLUSTER_ENTITY_QUERY_LIMIT = 256


_PREVIEW_SNIPPET_CHARS = 200


def _latest_preview_snippet(previews: list[ActiveMemoryPreview]) -> str | None:
    """Whitespace-collapsed snippet of the most recently updated preview."""
    if not previews:
        return None

    def _recency(preview: ActiveMemoryPreview) -> datetime:
        ts = preview["updated_at"]
        if ts is None:
            return datetime.min.replace(tzinfo=UTC)
        return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)

    latest = max(previews, key=_recency)
    collapsed = " ".join(latest["content"].split())
    if not collapsed:
        return None
    if len(collapsed) <= _PREVIEW_SNIPPET_CHARS:
        return collapsed
    return collapsed[: _PREVIEW_SNIPPET_CHARS - 1].rstrip() + "…"


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


def _components_have_signal(components: dict[str, float | None]) -> bool:
    """True when an edge-component breakdown carries any non-default term."""
    if components.get("edge_cosine") is not None:
        return True
    if components.get("edge_support_norm") is not None:
        return True
    if components.get("edge_weight_blend") is not None:
        return True
    decay = components.get("edge_decay_factor")
    return decay is not None and decay != 1.0


@dataclass
class RelatedMemoryTraversal:
    """Result of entity-graph traversal to related memories.

    ``component_map`` carries the #17096 edge-weight component breakdown
    (contract §3.2) for each memory admitted via weighted traversal, keyed by
    memory_id. Memories reached only through cluster expansion have no entry.
    """

    memory_ids: list[str] = field(default_factory=list)
    component_map: dict[str, dict[str, float | None]] = field(default_factory=dict)


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
        cooccur_alpha: float | None = None,
        cooccur_support_cap: int | None = None,
        active_memory_lookup: ActiveMemoryLookup | None = None,
    ) -> None:
        self._falkor = falkor_client
        self._embed_fn = embed_fn
        self._embedding_dim = embedding_dim
        self._graph_edge_decay = graph_edge_decay
        self._edge_half_life_days = edge_half_life_days
        self._cluster_recall_expansion = cluster_recall_expansion
        self._cluster_expansion_per_entity = max(cluster_expansion_per_entity, 0)
        # None means "read the writer module globals at call time" (static floor
        # plus benchmark monkeypatch surface); a value is a #17200 fitted
        # override and must match what the writer blends with.
        self._cooccur_alpha = cooccur_alpha
        self._cooccur_support_cap = cooccur_support_cap
        self._active_memory_lookup = active_memory_lookup
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
            logger.warning("Failed to ensure vector index: %s", e)

    async def search_entities_by_vector(
        self,
        query_embedding: list[float],
        limit: int = 10,
        min_score: float = 0.5,
        project_id: str | None = None,
        include_global: bool = True,
    ) -> list[dict[str, Any]]:
        """Search entities by vector similarity and return with linked memory IDs."""
        await self.ensure_vector_index()

        try:
            entity_rows = await self._falkor.vector_search(
                query_embedding=query_embedding,
                limit=limit,
                min_score=min_score,
                project_id=project_id,
                include_global=include_global,
            )
            allowed_project_ids: set[str | None] = set()
            if project_id is not None:
                allowed_project_ids.add(project_id)
            if include_global:
                allowed_project_ids.add(None)
            entity_rows = [
                row for row in entity_rows if row.get("project_id") in allowed_project_ids
            ]

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
                        "WHERE ((e.project_id = $project_id AND e.is_global = false) "
                        "OR ($include_global AND e.is_global = true)) "
                        "AND ((m.project_id = $project_id AND m.is_global = false) "
                        "OR ($include_global AND m.is_global = true)) "
                        "RETURN entity_key, m.memory_id AS memory_id "
                        "ORDER BY m.updated_at DESC LIMIT $memory_link_limit",
                        {
                            "entity_keys": entity_keys,
                            "project_id": project_id,
                            "include_global": include_global,
                            "memory_link_limit": memory_link_limit,
                        },
                    )
                    for r in mem_rows:
                        key = r.get("entity_key", "")
                        mid = r.get("memory_id")
                        if key in memory_map and mid:
                            memory_map[key].append(mid)
                except Exception as e:
                    logger.debug("Failed to batch-fetch memory links: %s", e)

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
            logger.warning("FalkorDB unreachable during entity vector search: %s", e)
            return []
        except Exception as e:
            logger.warning("Entity vector search failed: %s", e)
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

    def _record_traversal_timeout(
        self,
        error: BaseException,
        timeout_seconds: float | None,
    ) -> None:
        now = time.monotonic()
        self._traversal_timeout_count += 1
        if self._traversal_timeout_count >= _TRAVERSAL_TIMEOUT_THRESHOLD:
            self._traversal_disabled_until = now + _TRAVERSAL_TIMEOUT_COOLDOWN_SECONDS
        self._warn_traversal_timeout(error, now, timeout_seconds)

    def _record_traversal_success(self) -> None:
        self._traversal_timeout_count = 0
        self._traversal_disabled_until = 0.0

    def _warn_traversal_timeout(
        self,
        error: BaseException,
        now: float,
        timeout_seconds: float | None,
    ) -> None:
        if now - self._last_traversal_warning_at < _TRAVERSAL_WARNING_INTERVAL_SECONDS:
            self._suppressed_traversal_warnings += 1
            return

        suppressed = self._suppressed_traversal_warnings
        self._suppressed_traversal_warnings = 0
        self._last_traversal_warning_at = now
        logger.warning(
            "FalkorDB graph traversal timed out; effective_timeout_seconds=%s "
            "consecutive_timeouts=%d cooldown_seconds=%.0f suppressed_warnings=%d error=%s",
            timeout_seconds,
            self._traversal_timeout_count,
            _TRAVERSAL_TIMEOUT_COOLDOWN_SECONDS,
            suppressed,
            error,
            extra={
                "effective_timeout_seconds": timeout_seconds,
                "consecutive_timeouts": self._traversal_timeout_count,
                "cooldown_seconds": _TRAVERSAL_TIMEOUT_COOLDOWN_SECONDS,
                "suppressed_warnings": suppressed,
            },
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

    def _edge_components(
        self,
        raw_weight: Any,
        edge_support: Any,
        updated_at: Any,
    ) -> dict[str, float | None]:
        """Decompose one traversal edge into the #17096 formula terms (§3.2).

        ``edge_cosine`` is recovered algebraically from the stored blend —
        ``weight = alpha * cos01 + (1 - alpha) * support_norm`` — because
        CO_OCCURS edges persist ``weight`` and ``support`` but not the raw
        cosine. Unweighted edges (no ``r.weight``/``r.support``) yield None
        components; ``edge_decay_factor`` is 1.0 when edge decay is off.
        """
        alpha = self._cooccur_alpha if self._cooccur_alpha is not None else COOCCUR_ALPHA
        cap = (
            self._cooccur_support_cap
            if self._cooccur_support_cap is not None
            else COOCCUR_SUPPORT_CAP
        )
        blend: float | None
        try:
            blend = float(raw_weight) if raw_weight is not None else None
        except (TypeError, ValueError):
            blend = None
        support_norm: float | None = None
        try:
            if edge_support is not None and cap > 0:
                support_norm = min(int(edge_support), cap) / cap
        except (TypeError, ValueError):
            support_norm = None
        cosine: float | None = None
        if blend is not None and support_norm is not None and alpha > 0:
            cosine = (blend - (1.0 - alpha) * support_norm) / alpha
            cosine = min(max(cosine, 0.0), 1.0)
        decay = 1.0
        if self._graph_edge_decay:
            iso = _edge_timestamp_to_iso(updated_at)
            if iso is not None:
                decay = temporal_decay(iso, self._edge_half_life_days)
        return {
            "edge_cosine": cosine,
            "edge_support_norm": support_norm,
            "edge_weight_blend": blend,
            "edge_decay_factor": decay,
        }

    async def _find_related_entity_keys(
        self,
        seed_keys: list[str],
        max_hops: int,
        limit: int,
        project_id: str | None,
        include_global: bool,
    ) -> tuple[list[str], dict[str, dict[str, float | None]], dict[str, float]]:
        """Return admitted entity keys, their admitting-edge components, and path scores."""
        related_keys: list[str] = []
        components_by_key: dict[str, dict[str, float | None]] = {}
        admission_score_by_key: dict[str, float] = {}
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
            hop_components: dict[str, dict[str, float | None]] = {}
            if not frontier:
                break
            rows = await self._falkor.query(
                "UNWIND $source_keys AS source_key "
                "MATCH (start:_Entity {entity_key: source_key})-[r]-(neighbor:_Entity) "
                "WHERE ((start.project_id = $project_id AND start.is_global = false) "
                "OR ($include_global AND start.is_global = true)) "
                "AND ((neighbor.project_id = $project_id AND neighbor.is_global = false) "
                "OR ($include_global AND neighbor.is_global = true)) "
                "AND NOT (type(r) IN $excluded_relationship_types) "
                "RETURN source_key AS source_key, "
                "neighbor.entity_key AS related_entity_key, "
                "coalesce(r.weight, 1.0) AS edge_weight, r.weight AS raw_weight, "
                "r.support AS edge_support, r.updated_at AS updated_at",
                {
                    "source_keys": [source_key for source_key, _score in frontier],
                    "project_id": project_id,
                    "include_global": include_global,
                    "excluded_relationship_types": list(_STRUCTURAL_RELATIONSHIP_TYPES),
                },
            )
            rows_by_source: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                source_key = row.get("source_key")
                if not source_key:
                    continue
                rows_by_source.setdefault(str(source_key), []).append(row)
            for source_key, source_score in frontier:
                # Score each candidate, keep the strongest edge per neighbor, then cap
                # to the top _RELATED_ENTITY_NEIGHBOR_LIMIT for this source.
                best_by_key: dict[str, float] = {}
                best_row_by_key: dict[str, dict[str, Any]] = {}
                for row in rows_by_source.get(source_key, []):
                    related_key = row.get("related_entity_key")
                    if not related_key or related_key in seen:
                        continue
                    edge_score = self._edge_score(row.get("edge_weight"), row.get("updated_at"))
                    if related_key not in best_by_key or edge_score > best_by_key[related_key]:
                        best_by_key[related_key] = edge_score
                        best_row_by_key[related_key] = row
                scored = sorted(best_by_key.items(), key=lambda item: (-item[1], item[0]))
                for related_key, edge_score in scored[:_RELATED_ENTITY_NEIGHBOR_LIMIT]:
                    path_score = source_score * edge_score
                    if related_key not in hop_best or path_score > hop_best[related_key]:
                        hop_best[related_key] = path_score
                        row = best_row_by_key[related_key]
                        hop_components[related_key] = self._edge_components(
                            row.get("raw_weight"),
                            row.get("edge_support"),
                            row.get("updated_at"),
                        )
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
                components_by_key[related_key] = hop_components.get(related_key, {})
                admission_score_by_key[related_key] = path_score
                next_frontier.append((related_key, path_score))
                if len(related_keys) >= max_related_entities:
                    break
            if not next_frontier or len(related_keys) >= max_related_entities:
                break
            frontier = next_frontier

        return related_keys, components_by_key, admission_score_by_key

    async def fetch_project_entity_vectors(
        self,
        project_id: str | None,
    ) -> list[EntityVector]:
        """Fetch project-scoped entity embeddings for offline clustering."""
        rows = await self._falkor.query(
            "MATCH (e:_Entity) "
            "WHERE (e.project_id = $project_id AND e.is_global = false) "
            "OR e.is_global = true "
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
        include_global: bool,
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
                "WHERE ((source.project_id = $project_id AND source.is_global = false) "
                "OR ($include_global AND source.is_global = true)) "
                "AND source.cluster_id IS NOT NULL AND source.cluster_id >= 0 "
                "MATCH (candidate:_Entity {cluster_id: source.cluster_id}) "
                "WHERE ((candidate.project_id = $project_id AND candidate.is_global = false) "
                "OR ($include_global AND candidate.is_global = true)) "
                "AND candidate.cluster_id IS NOT NULL AND candidate.cluster_id >= 0 "
                "AND NOT (candidate.entity_key IN $excluded_keys) "
                "RETURN DISTINCT candidate.entity_key AS entity_key, "
                "candidate.cluster_id AS cluster_id "
                "ORDER BY cluster_id ASC, entity_key ASC LIMIT $limit",
                {
                    "source_keys": expansion_sources,
                    "excluded_keys": excluded_keys,
                    "project_id": project_id,
                    "include_global": include_global,
                    "limit": expansion_limit,
                },
            )
        except Exception as e:
            if self._is_query_timeout_error(e):
                raise
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
        include_global: bool = True,
        timeout_seconds: float | None = None,
    ) -> RelatedMemoryTraversal:
        """Traverse from entities through relationships to find related memory IDs."""
        if not entity_keys or limit <= 0:
            return RelatedMemoryTraversal()
        if self._related_traversal_is_disabled():
            return RelatedMemoryTraversal()

        max_hops = max(1, min(max_hops, 3))
        seed_keys = list(dict.fromkeys(entity_keys))[:_RELATED_ENTITY_SEED_LIMIT]
        if not seed_keys:
            return RelatedMemoryTraversal()

        try:
            async with asyncio.timeout(timeout_seconds):
                (
                    related_entity_keys,
                    components_by_entity,
                    admission_score_by_entity,
                ) = await self._find_related_entity_keys(
                    seed_keys,
                    max_hops,
                    limit,
                    project_id,
                    include_global,
                )
                cluster_entity_keys = await self._find_cluster_entity_keys(
                    seed_keys,
                    seed_keys,
                    related_entity_keys,
                    project_id,
                    include_global,
                )
                memory_entity_keys = list(
                    dict.fromkeys([*related_entity_keys, *cluster_entity_keys])
                )
                if not memory_entity_keys:
                    self._record_traversal_success()
                    return RelatedMemoryTraversal()

                rows = await self._falkor.query(
                    "UNWIND $entity_keys AS entity_key "
                    "MATCH (e:_Entity {entity_key: entity_key})"
                    "-[:MENTIONED_IN]->(m:Memory) "
                    "WHERE ((e.project_id = $project_id AND e.is_global = false) "
                    "OR ($include_global AND e.is_global = true)) "
                    "AND ((m.project_id = $project_id AND m.is_global = false) "
                    "OR ($include_global AND m.is_global = true)) "
                    "RETURN DISTINCT m.memory_id AS memory_id, m.updated_at AS updated_at "
                    "ORDER BY updated_at DESC LIMIT $limit",
                    {
                        "entity_keys": memory_entity_keys,
                        "limit": limit,
                        "project_id": project_id,
                        "include_global": include_global,
                    },
                )
                memory_ids = [r["memory_id"] for r in rows if r.get("memory_id")]
                component_map = await self._attribute_edge_components(
                    memory_ids=memory_ids,
                    components_by_entity=components_by_entity,
                    admission_score_by_entity=admission_score_by_entity,
                    project_id=project_id,
                    include_global=include_global,
                )
                self._record_traversal_success()
                return RelatedMemoryTraversal(memory_ids=memory_ids, component_map=component_map)
        except TimeoutError as e:
            self._record_traversal_timeout(e, timeout_seconds)
            return RelatedMemoryTraversal()
        except FalkorConnectionError as e:
            if self._is_query_timeout_error(e):
                self._record_traversal_timeout(e, timeout_seconds)
                return RelatedMemoryTraversal()
            self._record_traversal_success()
            logger.warning("FalkorDB unreachable during graph traversal: %s", e)
            return RelatedMemoryTraversal()
        except Exception as e:
            if self._is_query_timeout_error(e):
                self._record_traversal_timeout(e, timeout_seconds)
                return RelatedMemoryTraversal()
            self._record_traversal_success()
            logger.warning("Graph traversal failed: %s", e)
            return RelatedMemoryTraversal()

    async def _attribute_edge_components(
        self,
        *,
        memory_ids: list[str],
        components_by_entity: dict[str, dict[str, float | None]],
        admission_score_by_entity: dict[str, float],
        project_id: str | None,
        include_global: bool,
    ) -> dict[str, dict[str, float | None]]:
        """Map admitting-edge components (contract §3.2) onto traversed memories.

        A memory mentioned by several traversal-admitted entities takes the
        components of the entity with the strongest accumulated path score.
        Entities admitted only through unweighted edges (all component terms
        None, no decay) are skipped — their hits stay componentless, so the
        attribution query never runs in the unweighted regime.
        """
        entity_keys = [
            key
            for key, comps in components_by_entity.items()
            if comps and _components_have_signal(comps)
        ]
        if not memory_ids or not entity_keys:
            return {}

        rows = await self._falkor.query(
            "UNWIND $entity_keys AS entity_key "
            "MATCH (e:_Entity {entity_key: entity_key})-[:MENTIONED_IN]->(m:Memory) "
            "WHERE m.memory_id IN $memory_ids "
            "AND ((e.project_id = $project_id AND e.is_global = false) "
            "OR ($include_global AND e.is_global = true)) "
            "RETURN e.entity_key AS entity_key, m.memory_id AS memory_id",
            {
                "entity_keys": entity_keys,
                "memory_ids": memory_ids,
                "project_id": project_id,
                "include_global": include_global,
            },
        )
        component_map: dict[str, dict[str, float | None]] = {}
        best_score: dict[str, float] = {}
        for row in rows:
            entity_key = row.get("entity_key")
            memory_id = row.get("memory_id")
            if not entity_key or not memory_id:
                continue
            components = components_by_entity.get(entity_key)
            if not components:
                continue
            score = admission_score_by_entity.get(entity_key, 0.0)
            if memory_id not in best_score or score > best_score[memory_id]:
                best_score[memory_id] = score
                component_map[memory_id] = components
        return component_map

    async def get_entity_graph(
        self,
        limit: int = 500,
        relationship_limit: int = 2000,
        project_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Get the entity graph for visualization, hiding soft-deleted-only artifacts."""
        try:
            graph = await self._falkor.get_entity_graph(
                limit=limit,
                relationship_limit=relationship_limit,
                project_id=project_id,
            )
        except FalkorConnectionError as e:
            logger.warning("FalkorDB unreachable: %s", e)
            return None
        except Exception as e:
            logger.warning("FalkorDB query failed: %s", e)
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
            logger.warning("FalkorDB unreachable: %s", e)
            return None
        except Exception as e:
            logger.warning("FalkorDB query failed: %s", e)
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
        touching a dropped entity drops with it. Without an injected store lookup (e.g.
        in tests) the raw graph is returned unchanged.

        Each surviving entity is enriched with ``memory_count`` (active backing
        memories) and ``memory_preview`` (a snippet of the most recently updated
        one) so graph UIs can show human-readable cards instead of raw properties.
        """
        if graph is None or self._active_memory_lookup is None:
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
        try:
            previews = await self._active_memory_lookup(all_memory_ids, project_id)
        except Exception:
            logger.warning("Active memory lookup failed for entity graph", exc_info=True)
            return graph

        visible_keys = {
            key
            for key, memory_ids in backing.items()
            if any(memory_id in previews for memory_id in memory_ids)
        }
        filtered_entities = []
        for entity in entities:
            key = str(entity.get("entity_key") or "")
            if key not in visible_keys:
                continue
            active_backing = [mid for mid in backing.get(key, []) if mid in previews]
            filtered_entities.append(
                {
                    **entity,
                    "memory_count": len(active_backing),
                    "memory_preview": _latest_preview_snippet(
                        [previews[mid] for mid in active_backing]
                    ),
                }
            )
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
        include_global: bool = True,
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
                "WHERE ((e.project_id = $project_id AND e.is_global = false) "
                "OR ($include_global AND e.is_global = true)) "
                "AND ((m.project_id = $project_id AND m.is_global = false) "
                "OR ($include_global AND m.is_global = true)) "
                "RETURN e.entity_key AS entity_key, "
                "collect(DISTINCT m.memory_id) AS memory_ids",
                {
                    "entity_keys": entity_keys,
                    "project_id": project_id,
                    "include_global": include_global,
                },
            )
        except FalkorConnectionError as e:
            logger.warning("FalkorDB unreachable resolving entity backing memories: %s", e)
            return None
        except Exception as e:
            logger.warning("Failed to resolve entity backing memories: %s", e)
            return None

        backing: dict[str, list[str]] = {}
        for row in rows:
            key = row.get("entity_key")
            if not key:
                continue
            backing[str(key)] = [str(mid) for mid in (row.get("memory_ids") or []) if mid]
        return backing

    async def search_graph(
        self,
        query: str,
        limit: int = 10,
        project_id: str | None = None,
        include_global: bool = True,
    ) -> list[dict[str, Any]]:
        """Search the knowledge graph, using vector search before substring fallback."""
        if self._embed_fn is not None:
            try:
                embedding = await self._embed_fn(query, is_query=True)
                results = await self.search_entities_by_vector(
                    query_embedding=embedding,
                    limit=limit,
                    min_score=0.3,
                    project_id=project_id,
                    include_global=include_global,
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
                logger.debug("Vector graph search failed, falling back to substring: %s", e)

        try:
            rows = await self._falkor.query(
                "MATCH (n:_Entity) WHERE toLower(n.name) CONTAINS toLower($query) "
                "AND ((n.project_id = $project_id AND n.is_global = false) "
                "OR ($include_global AND n.is_global = true)) "
                "RETURN n.entity_key AS entity_key, n.name AS name, "
                "n.entity_type AS entity_type, n.project_id AS project_id, "
                "labels(n) AS labels, properties(n) AS props "
                "LIMIT $limit",
                {
                    "query": query,
                    "limit": limit,
                    "project_id": project_id,
                    "include_global": include_global,
                },
            )
            allowed_project_ids: set[str | None] = set()
            if project_id is not None:
                allowed_project_ids.add(project_id)
            if include_global:
                allowed_project_ids.add(None)
            return [row for row in rows if row.get("project_id") in allowed_project_ids]
        except FalkorConnectionError as e:
            logger.warning("FalkorDB unreachable: %s", e)
            return []
        except Exception as e:
            logger.warning("Graph search failed: %s", e)
            return []
