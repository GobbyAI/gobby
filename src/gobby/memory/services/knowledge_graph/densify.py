"""Non-LLM CO_OCCURS densification over the existing knowledge graph.

``CO_OCCURS`` is derived data: canonical ``a<b`` entity pairs from the
``MENTIONED_IN`` bipartite structure, support = distinct shared-memory count,
weight = ``cooccurrence_weight`` over stored ``_Entity.embedding`` vectors.
Graphs built before ``materialize_cooccurrence`` was enabled have none of these
edges, and the write path only materializes them per memory at write time. This
module retrofits the whole graph in bounded batches without any LLM extraction.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from gobby.storage.projects import GLOBAL_PROJECT_ID

from .writer import COOCCUR_MAX_ENTITIES, KnowledgeGraphWriter, _project_scope

if TYPE_CHECKING:
    from gobby.memory.falkor_client import FalkorClient

logger = logging.getLogger(__name__)

# Pairs per merge_cooccurrence_edges call. Each batch is one bounded support
# query + one bounded write, with an await between batches, so the pass never
# issues a single monster query that could stall past the HTTP timeout.
DENSIFY_BATCH_SIZE: int = 200


@dataclass(frozen=True)
class CooccurrenceDensifyResult:
    """Counters describing one densification pass."""

    project_id: str | None
    weighted: bool
    memories_scanned: int
    entities_with_embedding: int
    pairs_total: int
    pairs_skipped_no_embedding: int
    pairs_merged: int
    batches: int
    edges_before: int
    edges_after: int


def _coerce_vector(embedding: Any) -> list[float] | None:
    """Coerce a stored graph vector value to ``list[float]`` (None if invalid)."""
    if isinstance(embedding, (str, bytes)) or not isinstance(embedding, Sequence):
        return None
    values: list[float] = []
    for item in embedding:
        try:
            values.append(float(item))
        except (TypeError, ValueError):
            return None
    return values or None


async def _count_cooccurs_edges(falkor: FalkorClient, project_id: str | None) -> int:
    proj_a = _project_scope("a")
    proj_b = _project_scope("b")
    rows = await falkor.query(
        "MATCH (a:_Entity)-[r:CO_OCCURS]->(b:_Entity) "
        f"WHERE {proj_a} AND {proj_b} "
        "RETURN count(r) AS edges",
        {
            "project_id": GLOBAL_PROJECT_ID if project_id is None else project_id,
            "is_global": project_id is None,
        },
    )
    return int(rows[0].get("edges") or 0) if rows else 0


async def _fetch_entity_embeddings(
    falkor: FalkorClient, project_id: str | None
) -> dict[str, list[float]]:
    proj_e = _project_scope("e")
    rows = await falkor.query(
        "MATCH (e:_Entity) "
        f"WHERE {proj_e} AND e.embedding IS NOT NULL "
        "RETURN e.entity_key AS entity_key, e.embedding AS embedding",
        {
            "project_id": GLOBAL_PROJECT_ID if project_id is None else project_id,
            "is_global": project_id is None,
        },
    )
    embeddings: dict[str, list[float]] = {}
    for row in rows:
        key = row.get("entity_key")
        vector = _coerce_vector(row.get("embedding"))
        if key and vector:
            embeddings[str(key)] = vector
    return embeddings


async def densify_cooccurrence(
    falkor: FalkorClient,
    writer: KnowledgeGraphWriter,
    project_id: str | None,
    *,
    weighted: bool,
    batch_size: int = DENSIFY_BATCH_SIZE,
    max_entities_per_memory: int = COOCCUR_MAX_ENTITIES,
) -> CooccurrenceDensifyResult:
    """Materialize derived ``CO_OCCURS`` edges over the existing graph.

    Pair derivation mirrors the write path (``COOCCUR_MAX_ENTITIES`` salient
    entities per memory, canonical ``a<b``) with one documented difference: the
    extractor's salience order is not persisted in the graph, so the retrofit
    caps on sorted entity keys instead — deterministic across reruns. Support,
    weight, idempotency, and zero-support cleanup all come from delegating each
    batch to ``KnowledgeGraphWriter.merge_cooccurrence_edges``, so densified
    edges cannot drift from write-path semantics. In weighted mode, pairs where
    either entity lacks a stored embedding are skipped (and counted); a rerun
    after an embedding backfill picks them up.
    """
    edges_before = await _count_cooccurs_edges(falkor, project_id)

    proj_e = _project_scope("e")
    proj_m = _project_scope("m")
    rows = await falkor.query(
        "MATCH (e:_Entity)-[:MENTIONED_IN]->(m:Memory) "
        f"WHERE {proj_e} AND {proj_m} "
        "RETURN id(m) AS memory_id, collect(DISTINCT e.entity_key) AS keys",
        {
            "project_id": GLOBAL_PROJECT_ID if project_id is None else project_id,
            "is_global": project_id is None,
        },
    )

    pairs: set[tuple[str, str]] = set()
    for row in rows:
        keys = sorted({str(k) for k in (row.get("keys") or []) if k})
        pairs.update(itertools.combinations(keys[:max_entities_per_memory], 2))

    embeddings: dict[str, list[float]] = {}
    skipped = 0
    mergeable = sorted(pairs)
    if weighted:
        embeddings = await _fetch_entity_embeddings(falkor, project_id)
        with_vectors = [p for p in mergeable if p[0] in embeddings and p[1] in embeddings]
        skipped = len(mergeable) - len(with_vectors)
        mergeable = with_vectors

    safe_batch = max(1, batch_size)
    batches = 0
    for start in range(0, len(mergeable), safe_batch):
        await writer.merge_cooccurrence_edges(
            list(mergeable[start : start + safe_batch]),
            GLOBAL_PROJECT_ID if project_id is None else project_id,
            project_id is None,
            embeddings,
            weighted=weighted,
        )
        batches += 1

    edges_after = await _count_cooccurs_edges(falkor, project_id)
    result = CooccurrenceDensifyResult(
        project_id=project_id,
        weighted=weighted,
        memories_scanned=len(rows),
        entities_with_embedding=len(embeddings),
        pairs_total=len(pairs),
        pairs_skipped_no_embedding=skipped,
        pairs_merged=len(mergeable),
        batches=batches,
        edges_before=edges_before,
        edges_after=edges_after,
    )
    logger.info(
        "CO_OCCURS densify (project=%s weighted=%s): %d memories, %d pairs "
        "(%d merged, %d skipped no-embedding), edges %d -> %d",
        project_id,
        weighted,
        result.memories_scanned,
        result.pairs_total,
        result.pairs_merged,
        result.pairs_skipped_no_embedding,
        edges_before,
        edges_after,
    )
    return result
