"""Data models used by memory search orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field


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
    # Stored vectors for ``merged_ids`` when the store could serve them in one
    # retrieve; materialization uses them to collapse near-duplicates (#21010).
    vectors: dict[str, list[float]] | None = None
