"""Data models used by memory search orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field

from gobby.memory.services._search_constants import _GRAPH_SYNTHETIC_SIM_DISCOUNT


@dataclass(frozen=True)
class SearchDebugHit:
    """Returned hit features captured for observational search telemetry."""

    memory_id: str
    rank: int
    search_via: str | None
    similarity: float | None
    raw_semantic_score: float | None
    temporal_decay_factor: float | None
    ranking_score: float | None
    ranking_mode: str | None
    graph_score: float | None
    content_hash: str
    rationale: str | None = None


@dataclass(frozen=True)
class SearchDebugSnapshot:
    """Diagnostic ranking snapshot emitted after a search path materializes results."""

    merged_ids: list[str]
    returned_ids: list[str]
    ranking_score_map: dict[str, float]
    rrf_applied: bool
    query: str = ""
    project_id: str | None = None
    session_id: str | None = None
    recall_request_id: str | None = None
    caller: str = "memory.search"
    constants_provenance: str = "static"
    graph_score_map: dict[str, float] = field(default_factory=dict)
    # Edge-weight component breakdown per memory_id (contract §3.2):
    # edge_cosine, edge_support_norm, edge_weight_blend, edge_decay_factor.
    # Only populated for hits admitted through weighted graph traversal.
    graph_component_map: dict[str, dict[str, float | None]] = field(default_factory=dict)
    returned_hits: list[SearchDebugHit] = field(default_factory=list)
    graph_synthetic_similarity_discount: float = _GRAPH_SYNTHETIC_SIM_DISCOUNT


@dataclass
class _Candidates:
    """One round of merged ranked candidates feeding result materialization.

    ``exhausted`` is True when no contributing source returned a full page at the
    requested candidate count, meaning a larger fetch cannot surface new IDs and
    backfill should stop.
    """

    merged_ids: list[str]
    ranking_score_map: dict[str, float]
    qdrant_score_map: dict[str, float]
    qdrant_ranked: list[str]
    keyword_ranked: list[str]
    rrf_applied: bool
    graph_ranked: list[str] = field(default_factory=list)
    graph_score_map: dict[str, float] | None = None
    graph_component_map: dict[str, dict[str, float | None]] | None = None
    exhausted: bool = True
