"""FalkorDB write helpers for the memory knowledge graph."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from gobby.memory.falkor_client import FalkorConnectionError
from gobby.memory.identity import entity_key
from gobby.search.similarity import cosine_similarity as _cosine_similarity

from .models import Relationship, _GraphEntity

if TYPE_CHECKING:
    from gobby.memory.falkor_client import FalkorClient

logger = logging.getLogger(__name__)

# Co-occurrence edge-weighting constants. The FORM of the weight blend is fixed;
# these coefficients are the frozen winners of the offline recall sweep in
# tests/memory/test_recall_benchmark.py and remain the static rollback floor.
# The benchmark sweeps them by monkeypatching these module globals, so the
# writer/reader read them at call time unless an explicit fitted override was
# injected at construction (#17200 gate-shipped constants only).
COOCCUR_ALPHA: float = 0.5
COOCCUR_SUPPORT_CAP: int = 5
# Bounded fanout: only the top-N salient entities of a memory form co-occurrence
# pairs (N=8 -> <=28 pairs), keeping pairwise cost bounded as the graph densifies.
COOCCUR_MAX_ENTITIES: int = 8
CLUSTER_WRITE_BATCH_SIZE: int = 500


def cooccurrence_weight(cosine: float, support: int, *, alpha: float, cap: int) -> float:
    """Blend entity cosine similarity with saturating co-occurrence support.

    ``cos01 = max(cosine, 0.0)`` -- negative similarity must never act as a positive
    edge weight. Support enters through a saturating normalizer (diminishing returns
    on repeated co-mention, the same rationale as BM25 term-frequency saturation),
    and the blend is convex-linear, so the result is bounded to ``[0.0, 1.0]``.
    """
    cos01 = max(cosine, 0.0)
    safe_cap = cap if cap > 0 else 1
    norm_support = min(support, safe_cap) / safe_cap
    return alpha * cos01 + (1.0 - alpha) * norm_support


def _project_scope(var: str) -> str:
    """Cypher predicate scoping ``var`` by explicit ownership and visibility."""
    return (
        f"(($is_global AND {var}.is_global = true) OR "
        f"(NOT $is_global AND {var}.project_id = $project_id AND {var}.is_global = false))"
    )


class KnowledgeGraphWriter:
    """Owns FalkorDB schema and write mechanics for graph projection."""

    def __init__(
        self,
        falkor_client: FalkorClient,
        *,
        cooccur_alpha: float | None = None,
        cooccur_support_cap: int | None = None,
    ) -> None:
        self._falkor = falkor_client
        self._graph_schema_ensured = False
        self._graph_schema_lock = asyncio.Lock()
        # None means "read the module globals at call time" (static floor plus
        # benchmark monkeypatch surface); a value is a #17200 fitted override.
        self._cooccur_alpha = cooccur_alpha
        self._cooccur_support_cap = cooccur_support_cap

    @property
    def graph_schema_ensured(self) -> bool:
        return self._graph_schema_ensured

    @graph_schema_ensured.setter
    def graph_schema_ensured(self, value: bool) -> None:
        self._graph_schema_ensured = value

    @property
    def graph_schema_lock(self) -> asyncio.Lock:
        return self._graph_schema_lock

    async def ensure_graph_schema(self) -> None:
        """Lazily ensure the memory knowledge-graph schema exists."""
        if self._graph_schema_ensured:
            return
        async with self._graph_schema_lock:
            if self._graph_schema_ensured:
                return
            try:
                await self._falkor.ensure_memory_graph_schema()
                self._graph_schema_ensured = True
            except FalkorConnectionError:
                logger.debug("FalkorDB unreachable during knowledge-graph schema creation")
                raise
            except Exception as e:
                logger.warning("Failed to ensure knowledge-graph schema: %s", e)
                raise

    async def merge_entity(self, entity: _GraphEntity) -> None:
        """Merge a normalized entity node."""
        await self._falkor.merge_node(
            entity_key=entity.entity_key,
            name=entity.name,
            project_id=entity.project_id,
            labels=[entity.entity_type.capitalize()],
            properties={
                "entity_type": entity.entity_type,
                "project_id": entity.project_id,
                "is_global": entity.is_global,
            },
        )

    async def merge_relationship(
        self,
        relationship: Relationship,
        properties: dict[str, Any] | None = None,
    ) -> None:
        """Merge an entity relationship, optionally carrying edge properties.

        When ``properties`` includes a ``weight`` key the underlying client also
        manages a reinforcement ``count`` and ``updated_at`` timestamp; otherwise
        the call is byte-for-byte the prior unweighted behavior.
        """
        await self._falkor.merge_relationship(
            source_key=relationship.source,
            target_key=relationship.target,
            rel_type=relationship.relationship,
            properties=properties,
        )

    async def set_entity_vector(self, entity_key: str, embedding: list[float]) -> None:
        """Set an entity node embedding vector."""
        await self._falkor.set_node_vector(
            entity_key=entity_key,
            embedding=embedding,
        )

    async def write_entity_clusters(
        self,
        cluster_ids_by_entity_key: Mapping[str, int | None],
        project_id: str | None,
    ) -> dict[str, int]:
        """Persist entity cluster labels and remove stale labels from noise rows."""
        set_rows = [
            {"entity_key": entity_key, "cluster_id": cluster_id}
            for entity_key, cluster_id in cluster_ids_by_entity_key.items()
            if cluster_id is not None
        ]
        clear_keys = [
            entity_key
            for entity_key, cluster_id in cluster_ids_by_entity_key.items()
            if cluster_id is None
        ]

        proj_e = _project_scope("e")
        for index in range(0, len(set_rows), CLUSTER_WRITE_BATCH_SIZE):
            set_batch = set_rows[index : index + CLUSTER_WRITE_BATCH_SIZE]
            await self._falkor.query(
                "UNWIND $rows AS row "
                "MATCH (e:_Entity {entity_key: row.entity_key}) "
                f"WHERE {proj_e} "
                "SET e.cluster_id = row.cluster_id",
                {"rows": set_batch, "project_id": project_id},
            )
        for index in range(0, len(clear_keys), CLUSTER_WRITE_BATCH_SIZE):
            clear_batch = clear_keys[index : index + CLUSTER_WRITE_BATCH_SIZE]
            await self._falkor.query(
                "UNWIND $entity_keys AS entity_key "
                "MATCH (e:_Entity {entity_key: entity_key}) "
                f"WHERE {proj_e} "
                "REMOVE e.cluster_id",
                {"entity_keys": clear_batch, "project_id": project_id},
            )
        return {"clustered": len(set_rows), "noise": len(clear_keys)}

    async def fetch_existing_relations(self, entity_keys: list[str]) -> list[dict[str, str]]:
        """Fetch existing relationships involving the given entities."""
        if not entity_keys:
            return []

        outbound_rows = await self._falkor.query(
            "UNWIND $keys AS key "
            "MATCH (a:_Entity {entity_key: key}) "
            "MATCH (a)-[r]->(b:_Entity) "
            "WHERE type(r) <> 'CO_OCCURS' "
            "RETURN a.name AS source, type(r) AS rel_type, b.name AS target",
            {"keys": entity_keys},
        )
        inbound_rows = await self._falkor.query(
            "UNWIND $keys AS key "
            "MATCH (b:_Entity {entity_key: key}) "
            "MATCH (a:_Entity)-[r]->(b) "
            "WHERE type(r) <> 'CO_OCCURS' "
            "RETURN a.name AS source, type(r) AS rel_type, b.name AS target",
            {"keys": entity_keys},
        )

        relations: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for rows in (outbound_rows, inbound_rows):
            for row in rows:
                triple = (row["source"], row["rel_type"], row["target"])
                if triple in seen:
                    continue
                seen.add(triple)
                relations.append(
                    {
                        "source": row["source"],
                        "relationship": row["rel_type"],
                        "destination": row["target"],
                    }
                )
        return relations

    async def delete_relations(
        self,
        relations: list[dict[str, Any]],
        project_id: str,
        is_global: bool,
    ) -> list[dict[str, Any]]:
        """Delete selected relationships from FalkorDB and return failed entries."""
        failures: list[dict[str, Any]] = []
        for rel in relations:
            source = rel.get("source", "")
            relationship = rel.get("relationship", "")
            destination = rel.get("destination", "")
            if not (source and relationship and destination):
                logger.warning("Skipping malformed relation delete request: %s", rel)
                failures.append(
                    {"relation": rel, "error": "missing source/relationship/destination"}
                )
                continue
            try:
                await self._falkor.query(
                    "MATCH (a:_Entity {entity_key: $source_key})-[r]->"
                    "(b:_Entity {entity_key: $target_key}) "
                    "WHERE type(r) = $rel_type DELETE r",
                    {
                        "source_key": entity_key(project_id, source, is_global=is_global),
                        "target_key": entity_key(project_id, destination, is_global=is_global),
                        "rel_type": relationship,
                    },
                )
            except FalkorConnectionError as e:
                logger.warning("FalkorDB unreachable during relation delete: %s", e)
                failures.append({"relation": rel, "error": str(e)})
            except Exception as e:
                logger.warning("Failed to delete relation %s: %s", rel, e)
                failures.append({"relation": rel, "error": str(e)})
        return failures

    async def link_entities_to_memory(
        self,
        entities: list[_GraphEntity],
        memory_id: str,
        project_id: str,
        is_global: bool,
    ) -> None:
        """Create Memory node and MENTIONED_IN relationships from entities."""
        await self._falkor.query(
            "MERGE (m:Memory {memory_id: $memory_id}) "
            "ON CREATE SET m.created_at = timestamp() "
            "SET m.project_id = $project_id, m.is_global = $is_global, "
            "m.updated_at = timestamp()",
            {"memory_id": memory_id, "project_id": project_id, "is_global": is_global},
        )
        entity_keys = [entity.entity_key for entity in entities]
        if not entity_keys:
            return
        await self._falkor.query(
            "UNWIND $entity_keys AS entity_key "
            "MATCH (e:_Entity {entity_key: entity_key}), "
            "(m:Memory {memory_id: $memory_id}) "
            "MERGE (e)-[:MENTIONED_IN]->(m)",
            {"entity_keys": entity_keys, "memory_id": memory_id},
        )

    async def merge_cooccurrence_edges(
        self,
        pairs: list[tuple[str, str]],
        project_id: str,
        is_global: bool,
        embeddings: dict[str, list[float]],
        *,
        weighted: bool = True,
    ) -> None:
        """Materialize derived ``CO_OCCURS`` support edges over canonical ``a<b`` pairs.

        Support is the distinct shared-memory count read (idempotently) from the live
        ``MENTIONED_IN`` bipartite structure, so it self-corrects when memories are
        removed instead of inflating a counter. When ``weighted`` is true the edge also
        carries a weight blending entity cosine similarity with the saturating support
        normalizer; when false the edge is densification-only (no ``weight`` property,
        so traversal coalesces to a neutral ``1.0``). Zero-support pairs delete any
        stale edge (using the same project-scoped entity match as the write path) so the
        traversable layer never accumulates noise.
        """
        if not pairs:
            return

        proj_a = _project_scope("a")
        proj_b = _project_scope("b")
        proj_m = _project_scope("m")

        pair_params = [{"a": a, "b": b} for a, b in pairs]
        support_rows = await self._falkor.query(
            "UNWIND $pairs AS p "
            "MATCH (a:_Entity {entity_key: p.a}), (b:_Entity {entity_key: p.b}) "
            f"WHERE {proj_a} AND {proj_b} "
            "OPTIONAL MATCH (a)-[:MENTIONED_IN]->(m:Memory)<-[:MENTIONED_IN]-(b) "
            "RETURN p.a AS a, p.b AS b, "
            f"count(DISTINCT CASE WHEN m IS NOT NULL AND {proj_m} THEN m END) AS support",
            {"pairs": pair_params, "project_id": project_id, "is_global": is_global},
        )

        alpha = self._cooccur_alpha if self._cooccur_alpha is not None else COOCCUR_ALPHA
        cap = (
            self._cooccur_support_cap
            if self._cooccur_support_cap is not None
            else COOCCUR_SUPPORT_CAP
        )
        write_rows: list[dict[str, Any]] = []
        delete_rows: list[dict[str, str]] = []
        for row in support_rows:
            a = row.get("a")
            b = row.get("b")
            if not a or not b:
                continue
            support = int(row.get("support") or 0)
            if support <= 0:
                delete_rows.append({"a": a, "b": b})
                continue
            new_row: dict[str, Any] = {"a": a, "b": b, "support": support}
            if weighted:
                emb_a = embeddings.get(a)
                emb_b = embeddings.get(b)
                cosine = _cosine_similarity(emb_a, emb_b) if emb_a and emb_b else 0.0
                new_row["weight"] = cooccurrence_weight(cosine, support, alpha=alpha, cap=cap)
            write_rows.append(new_row)

        if write_rows:
            weight_clause = ", r.weight = p.weight" if weighted else ""
            await self._falkor.query(
                "UNWIND $rows AS p "
                "MATCH (a:_Entity {entity_key: p.a}), (b:_Entity {entity_key: p.b}) "
                f"WHERE {proj_a} AND {proj_b} "
                "MERGE (a)-[r:CO_OCCURS]->(b) "
                f"SET r.support = p.support{weight_clause}, r.updated_at = timestamp()",
                {"rows": write_rows, "project_id": project_id, "is_global": is_global},
            )
        if delete_rows:
            await self._falkor.query(
                "UNWIND $rows AS p "
                "MATCH (a:_Entity {entity_key: p.a})-[r:CO_OCCURS]->"
                "(b:_Entity {entity_key: p.b}) "
                f"WHERE {proj_a} AND {proj_b} "
                "DELETE r",
                {"rows": delete_rows, "project_id": project_id, "is_global": is_global},
            )
