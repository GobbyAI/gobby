"""Memory result hydration, filtering, scoring, and ordering."""

from __future__ import annotations

from gobby.memory.scoring import temporal_decay, undecay
from gobby.memory.services._search_constants import (
    _GRAPH_CONFIDENCE_SEARCH_FLOOR,
    _GRAPH_SYNTHETIC_SIM_DISCOUNT,
    _USER_SOURCE_BOOST,
)
from gobby.storage.memories import LocalMemoryManager, Memory
from gobby.storage.memories_scope import ALL_MEMORIES, MemoryScope


def build_results(
    *,
    storage: LocalMemoryManager,
    merged_ids: list[str],
    ranking_score_map: dict[str, float],
    qdrant_score_map: dict[str, float],
    qdrant_set: set[str],
    keyword_set: set[str],
    graph_set: set[str] | None,
    graph_score_map: dict[str, float] | None = None,
    rrf_applied: bool,
    project_id: str | None,
    memory_type: str | None,
    tags_all: list[str] | None,
    tags_any: list[str] | None,
    tags_none: list[str] | None,
    half_life: float,
    effective_min_score: float,
    limit: int,
    graph_synthetic_discount: float = _GRAPH_SYNTHETIC_SIM_DISCOUNT,
) -> list[Memory]:
    """Hydrate ranked IDs into active memories and apply search metadata."""
    scored: list[tuple[Memory, float, float | None]] = []
    scope = ALL_MEMORIES if project_id is None else MemoryScope.project_visible(project_id)
    memories_by_id = {mem.id: mem for mem in storage.get_memories(merged_ids, scope=scope)}

    for memory_id in merged_ids:
        mem = memories_by_id.get(memory_id)
        if mem is None:
            try:
                mem = storage.get_memory(memory_id, scope=scope)
            except ValueError:
                continue

        if memory_type and mem.memory_type != memory_type:
            continue
        if tags_all and not all(tag in (mem.tags or []) for tag in tags_all):
            continue
        if tags_any and not any(tag in (mem.tags or []) for tag in tags_any):
            continue
        if tags_none and any(tag in (mem.tags or []) for tag in tags_none):
            continue

        raw_semantic_score = qdrant_score_map.get(memory_id)
        decay_factor: float | None = None
        similarity: float | None = None
        synthetic_similarity = False
        # An expander find is a memory the graph surfaced and the semantic leg's
        # own window did not. A candidate in `qdrant_set` is a semantic hit the
        # graph also mentions, and it stays on the cosine axis: letting its
        # entity confidence rescue a sub-floor cosine would widen the semantic
        # axis under cover of the expander (#20873).
        graph_confidence: float | None = (
            None if memory_id in qdrant_set else (graph_score_map or {}).get(memory_id)
        )
        if raw_semantic_score is not None:
            similarity = raw_semantic_score
            if mem.source_type == "user":
                similarity *= _USER_SOURCE_BOOST
            decay_factor = temporal_decay(mem.updated_at, half_life)
            similarity *= decay_factor
        elif graph_confidence is not None:
            # Recall expander (#17104): a graph hit the collection could not score
            # at all still needs a place on the similarity axis, so it enters at a
            # discounted entity-match cosine and cannot outrank a real semantic
            # match. Its admission is decided on the confidence below either way.
            decay_factor = temporal_decay(mem.updated_at, half_life)
            similarity = graph_confidence * graph_synthetic_discount * decay_factor
            synthetic_similarity = True

        # The floor reads the undecayed score (#20858). Gating `similarity` gated
        # `cosine * boost * decay`, which at the live corpus median age of 25.9 days
        # demanded `cosine >= 1.002` to clear 0.55 -- unreachable, so every candidate
        # aged past the median was cut before the selection gate saw it while
        # null-similarity keyword hits, exempt below, kept their slots. Ranking still
        # uses the decayed value: age orders results, it no longer decides eligibility.
        #
        # A graph-expander find is judged on its confidence instead. Since
        # #20858 fills a real cosine in for every scorable candidate, the
        # expander's own hits -- entity-linked but differently worded, so
        # low-cosine by construction -- began meeting this floor on a score that
        # was never their admission evidence (#20873).
        if effective_min_score > 0:
            if graph_confidence is not None:
                if graph_confidence < _GRAPH_CONFIDENCE_SEARCH_FLOOR:
                    continue
            elif similarity is not None and undecay(similarity, decay_factor) < effective_min_score:
                continue

        sources = []
        if memory_id in qdrant_set:
            sources.append("semantic")
        if graph_set and memory_id in graph_set:
            sources.append("graph")
        if memory_id in keyword_set:
            sources.append("keyword")

        mem.search_via = "|".join(sources) or "unknown"
        mem.raw_semantic_score = raw_semantic_score
        mem.temporal_decay_factor = decay_factor
        mem.similarity = similarity
        mem.graph_confidence = graph_confidence
        mem.ranking_score = ranking_score_map.get(memory_id, 0.0)
        if synthetic_similarity:
            mem.ranking_mode = "graph_synthetic"
        elif rrf_applied:
            mem.ranking_mode = "rrf"
        elif memory_id in qdrant_set:
            # Provenance, not the presence of a score: since #20858 a candidate
            # another leg surfaced also carries a cosine, and calling that
            # `semantic_only` would name the wrong leg as its finder.
            mem.ranking_mode = "semantic_only"
        else:
            mem.ranking_mode = "nonsemantic_fallback"

        scored.append((mem, mem.ranking_score, similarity))

    # Semantic-first ordering on the cosine axis. RRF is only a tiebreak; making it
    # primary regressed the default graph_search=True path in #17105.
    scored.sort(
        key=lambda item: (
            item[2] is not None,
            item[2] if item[2] is not None else float("-inf"),
            item[1],
        ),
        reverse=True,
    )
    return [mem for mem, _, _ in scored[:limit]]
