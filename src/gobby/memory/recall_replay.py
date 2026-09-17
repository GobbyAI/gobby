"""Replay model for logged recall-signal rows (#17197, split out in #22491).

One logged hit (``ReplayRow``) is re-scored under counterfactual constants
(``ReplayParams``), and a request's rows are re-ordered by ``order_results`` --
the policy live ``build_results`` orders on -- so the offline fit, the ship gate
and the drift monitor judge the ranking agents actually receive. The metrics that
consume this model live in ``recall_fit``.

Replay algebra (exact unless noted):

- Temporal decay is exponential (``0.5 ** (age / half_life)``), so a row
  logged under half-life ``h0`` replays under ``h1`` as
  ``decay ** (h0 / h1)`` — exact, no timestamps needed.
- Semantic rows: ``similarity = base * decay`` where ``base`` preserves every
  pre-decay factor (raw score, user-source boost) at its logged value.
- ``graph_synthetic`` rows: ``similarity = graph_score * discount * decay``;
  the logged discount is recovered algebraically when the request row lacks
  it. Re-blending ``COOCCUR_ALPHA``/``COOCCUR_SUPPORT_CAP`` rescales
  ``graph_score`` by the attributed edge's new/old blend ratio — first-order:
  exact for single-edge attribution, approximate for multi-hop aggregates.
  Raw support is recovered from ``edge_support_norm`` under the logging-time
  cap; a saturated norm (1.0) only lower-bounds support, so re-caps upward
  are conservative there.

Ordering:

- Undecayed similarity leads the similarity order and decay only breaks its ties
  (#21010). Every row of a request was logged under one half-life, so a
  counterfactual half-life rescales all their decay factors monotonically:
  ``half_life_days`` moves the replayed ``similarity`` value and never the order.
  The constants that do move it are the ones that change an undecayed score:
  ``graph_synthetic_discount``, ``cooccur_alpha`` and ``cooccur_support_cap``.
- Replay orders the rows logged for a request. Live search interleaves its whole
  candidate pool before the near-duplicate collapse and the limit cut, so the
  replayed order is exact when the logged rows are that pool and an approximation
  otherwise.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from gobby.memory.scoring import undecay
from gobby.memory.services._search_ranking import HitScores, order_results
from gobby.memory.services.knowledge_graph.writer import (
    COOCCUR_ALPHA,
    COOCCUR_SUPPORT_CAP,
)


@dataclass(frozen=True)
class ReplayRow:
    """One injected hit with its logged full-ranking-path features.

    ``judge_useful is None`` means unlabeled: the row informs propensity
    estimation only and never forms preference pairs.
    """

    recall_request_id: str
    memory_id: str
    project_id: str | None
    rank: int
    similarity: float | None
    raw_semantic_score: float | None
    temporal_decay_factor: float | None
    ranking_score: float
    ranking_mode: str | None
    graph_score: float | None
    edge_cosine: float | None
    edge_support_norm: float | None
    edge_weight_blend: float | None
    injection_position: int | None
    injection_group: str | None
    judge_useful: bool | None
    label_source: str | None
    logged_half_life_days: float | None
    logged_graph_discount: float | None
    logged_cooccur_alpha: float = COOCCUR_ALPHA
    logged_cooccur_support_cap: int = COOCCUR_SUPPORT_CAP


@dataclass(frozen=True)
class ReplayParams:
    """Counterfactual recall constants. ``None`` keeps the logged value.

    ``half_life_days`` and ``graph_synthetic_discount`` replay exactly;
    ``cooccur_alpha``/``cooccur_support_cap`` rescale ``graph_synthetic`` rows
    to first order via the attributed edge components.
    """

    half_life_days: float | None = None
    graph_synthetic_discount: float | None = None
    cooccur_alpha: float | None = None
    cooccur_support_cap: int | None = None

    def __post_init__(self) -> None:
        if self.half_life_days is not None and self.half_life_days <= 0:
            raise ValueError(f"half_life_days must be positive, got {self.half_life_days}")
        if self.cooccur_support_cap is not None and self.cooccur_support_cap <= 0:
            raise ValueError(
                f"cooccur_support_cap must be positive, got {self.cooccur_support_cap}"
            )
        if self.cooccur_alpha is not None and not 0.0 < self.cooccur_alpha <= 1.0:
            raise ValueError(f"cooccur_alpha must be in (0, 1], got {self.cooccur_alpha}")


def replay_row_from_signal_row(row: Mapping[str, Any]) -> ReplayRow:
    """Adapt one ``RecallSignalStore.fetch_replay_rows`` dict to a ``ReplayRow``.

    The logging-time half-life comes from the request ``weighting`` snapshot;
    the logging-time co-occurrence constants are not logged (they were frozen
    module constants), so the writer's current values are assumed.
    """
    weighting = row.get("weighting") or {}
    half_life = weighting.get("temporal_decay_half_life_days")
    return ReplayRow(
        recall_request_id=str(row["recall_request_id"]),
        memory_id=str(row["memory_id"]),
        project_id=row.get("project_id"),
        rank=int(row["rank"]),
        similarity=float_or_none(row.get("similarity")),
        raw_semantic_score=float_or_none(row.get("raw_semantic_score")),
        temporal_decay_factor=float_or_none(row.get("temporal_decay_factor")),
        ranking_score=float_or_none(row.get("ranking_score")) or 0.0,
        ranking_mode=row.get("ranking_mode"),
        graph_score=float_or_none(row.get("graph_score")),
        edge_cosine=float_or_none(row.get("edge_cosine")),
        edge_support_norm=float_or_none(row.get("edge_support_norm")),
        edge_weight_blend=float_or_none(row.get("edge_weight_blend")),
        injection_position=row.get("injection_position"),
        injection_group=row.get("injection_group"),
        judge_useful=row.get("judge_useful"),
        label_source=row.get("label_source"),
        logged_half_life_days=float_or_none(half_life),
        logged_graph_discount=float_or_none(row.get("graph_synthetic_similarity_discount")),
    )


def float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _replayed_decay(row: ReplayRow, params: ReplayParams) -> float | None:
    """Logged decay factor re-exponentiated to the counterfactual half-life."""
    decay = row.temporal_decay_factor
    if decay is None:
        return None
    if params.half_life_days is None or row.logged_half_life_days is None:
        return decay
    if decay <= 0.0:
        return decay
    return float(decay ** (row.logged_half_life_days / params.half_life_days))


def _replayed_edge_blend_ratio(row: ReplayRow, params: ReplayParams) -> float:
    """First-order graph-score rescale from the attributed edge components."""
    if params.cooccur_alpha is None and params.cooccur_support_cap is None:
        return 1.0
    if row.edge_cosine is None or row.edge_support_norm is None or not row.edge_weight_blend:
        return 1.0
    alpha = params.cooccur_alpha if params.cooccur_alpha is not None else row.logged_cooccur_alpha
    cap = (
        params.cooccur_support_cap
        if params.cooccur_support_cap is not None
        else row.logged_cooccur_support_cap
    )
    # Recover raw support under the logging-time cap; a saturated norm only
    # lower-bounds it, so re-caps upward are conservative for those rows.
    raw_support = row.edge_support_norm * row.logged_cooccur_support_cap
    support_norm = min(raw_support, float(cap)) / float(cap)
    new_blend = alpha * row.edge_cosine + (1.0 - alpha) * support_norm
    return new_blend / row.edge_weight_blend


def _replayed_undecayed(row: ReplayRow, params: ReplayParams) -> float | None:
    """The similarity before decay under ``params``; None when the row cannot be re-derived."""
    logged_decay = row.temporal_decay_factor
    if row.similarity is None or logged_decay is None or logged_decay <= 0.0:
        return None
    if row.ranking_mode == "graph_synthetic":
        if row.graph_score is None:
            return None
        discount = params.graph_synthetic_discount
        if discount is None:
            discount = row.logged_graph_discount
        if discount is None:
            # Recover the logged discount algebraically from the logged blend.
            if row.graph_score <= 0.0:
                return None
            discount = row.similarity / (row.graph_score * logged_decay)
        return row.graph_score * _replayed_edge_blend_ratio(row, params) * discount
    if row.raw_semantic_score is not None:
        # Preserves every pre-decay factor (raw score, user boost).
        return row.similarity / logged_decay
    return None


def replayed_scores(row: ReplayRow, params: ReplayParams) -> HitScores:
    """The scores live search would order ``row`` on under ``params``."""
    undecayed = _replayed_undecayed(row, params)
    decay = _replayed_decay(row, params)
    if undecayed is not None and decay is not None:
        return HitScores(undecayed, undecayed * decay, row.ranking_score)
    # A row that cannot be re-derived keeps its logged similarity, so its
    # logged decay divides out.
    if row.similarity is None:
        return HitScores(None, None, row.ranking_score)
    return HitScores(
        undecay(row.similarity, row.temporal_decay_factor), row.similarity, row.ranking_score
    )


def replayed_similarity(row: ReplayRow, params: ReplayParams) -> float | None:
    """Recompute the blended similarity under counterfactual parameters."""
    return replayed_scores(row, params).decayed


def replayed_order(rows: Sequence[ReplayRow], params: ReplayParams) -> list[ReplayRow]:
    """One request's ``rows`` in the order live search returns them under ``params``."""
    return order_results(rows, lambda row: replayed_scores(row, params))
