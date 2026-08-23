"""Debug snapshot emission for memory search."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable

from gobby.memory.services._search_constants import _GRAPH_SYNTHETIC_SIM_DISCOUNT
from gobby.memory.services._search_models import SearchDebugHit, SearchDebugSnapshot
from gobby.storage.memories import Memory

logger = logging.getLogger(__name__)


def _rationale_hash(rationale: str | None) -> str | None:
    if not rationale:
        return None
    return hashlib.sha256(rationale.encode("utf-8")).hexdigest()


def emit_search_debug(
    *,
    search_debug_sink: Callable[[SearchDebugSnapshot], None] | None,
    query: str,
    project_id: str | None,
    session_id: str | None,
    recall_request_id: str | None,
    caller: str,
    merged_ids: list[str],
    returned: list[Memory],
    ranking_score_map: dict[str, float],
    rrf_applied: bool,
    constants_provenance: str,
    bm25_query: str | None = None,
    graph_score_map: dict[str, float] | None = None,
    graph_component_map: dict[str, dict[str, float | None]] | None = None,
    graph_synthetic_similarity_discount: float | None = None,
) -> None:
    """Emit a best-effort search diagnostics snapshot."""
    if search_debug_sink is None:
        return

    graph_scores = dict(graph_score_map or {})
    # The logged discount must be the EFFECTIVE value (#17200 fitted constants
    # may differ from the static default) so offline refits replay correctly.
    effective_discount = (
        graph_synthetic_similarity_discount
        if graph_synthetic_similarity_discount is not None
        else _GRAPH_SYNTHETIC_SIM_DISCOUNT
    )
    snapshot = SearchDebugSnapshot(
        merged_ids=list(merged_ids),
        returned_ids=[mem.id for mem in returned],
        ranking_score_map=dict(ranking_score_map),
        rrf_applied=rrf_applied,
        graph_synthetic_similarity_discount=effective_discount,
        query=query,
        bm25_query=bm25_query,
        project_id=project_id,
        session_id=session_id,
        recall_request_id=recall_request_id,
        caller=caller,
        constants_provenance=constants_provenance,
        graph_score_map=graph_scores,
        graph_component_map=dict(graph_component_map or {}),
        returned_hits=[
            SearchDebugHit(
                memory_id=mem.id,
                rank=rank,
                search_via=mem.search_via,
                similarity=mem.similarity,
                raw_semantic_score=mem.raw_semantic_score,
                temporal_decay_factor=mem.temporal_decay_factor,
                ranking_score=mem.ranking_score,
                ranking_mode=mem.ranking_mode,
                graph_score=graph_scores.get(mem.id),
                content_hash=hashlib.sha256(mem.content.encode("utf-8")).hexdigest(),
                rationale_hash=_rationale_hash(getattr(mem, "rationale", None)),
            )
            for rank, mem in enumerate(returned)
        ],
    )
    try:
        search_debug_sink(snapshot)
    except Exception:
        logger.debug("Search debug sink failed", exc_info=True)
