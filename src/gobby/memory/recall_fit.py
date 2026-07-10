"""Offline fit/eval over logged recall-signal rows (#17197, epic #17099).

This module generalizes the offline recall benchmark harness to real labeled
data. It consumes injected per-hit feature rows from the promoted hub tables
(``RecallSignalStore.fetch_replay_rows``: hits ⋈ injected outcomes ⋈ request
context, LEFT JOIN usefulness labels) and replays the FULL ranking path —
the ``SearchService`` blend ordering from ``build_results`` (semantic-first on
the similarity axis with temporal decay and ``ranking_mode`` semantics, RRF
``ranking_score`` as tiebreak) — under counterfactual parameters, without
re-running retrieval.

Scope and semantics (contract: docs/contracts/memory-usefulness-label.md):

- **Precision-side only.** Labels exist only for memories that were actually
  injected, so this data can say "what we injected was (not) useful" — it can
  never say what we *failed* to retrieve. Recall-side (false-negative)
  evaluation stays with the synthetic corpus arms in
  ``tests/memory/test_recall_benchmark.py``.
- **Never-retrieved memories are unlabeled, not negative.** The pairwise
  objective forms pairs only between explicitly labeled rows within the same
  recall request. Rows without a label contribute to propensity estimation
  (denominators) only.
- **IPS position-propensity weighting** (arXiv:1608.04468): pairs are weighted
  by the inverse examination propensity of the *positive* row, estimated from
  label coverage per ``(injection_group, injection_position)`` — position is
  the rendered ordinal, never recall rank. Weights are clipped for variance
  control (arXiv:2008.10242).
- **Per-project partial pooling, not naive full pooling.** Requests are split
  train/eval within each project; per-project fits are shrunk toward the
  pooled fit in proportion to per-project pair support.

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
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from gobby.memory.services.knowledge_graph.writer import (
    COOCCUR_ALPHA,
    COOCCUR_SUPPORT_CAP,
)

_SORT_NONE_SIM = float("-inf")

# Propensity keys are (injection_group, injection_position); position is the
# rendered ordinal within the injection block, per the label contract §5.
PropensityKey = tuple[str | None, int]


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
        similarity=_float_or_none(row.get("similarity")),
        raw_semantic_score=_float_or_none(row.get("raw_semantic_score")),
        temporal_decay_factor=_float_or_none(row.get("temporal_decay_factor")),
        ranking_score=_float_or_none(row.get("ranking_score")) or 0.0,
        ranking_mode=row.get("ranking_mode"),
        graph_score=_float_or_none(row.get("graph_score")),
        edge_cosine=_float_or_none(row.get("edge_cosine")),
        edge_support_norm=_float_or_none(row.get("edge_support_norm")),
        edge_weight_blend=_float_or_none(row.get("edge_weight_blend")),
        injection_position=row.get("injection_position"),
        injection_group=row.get("injection_group"),
        judge_useful=row.get("judge_useful"),
        label_source=row.get("label_source"),
        logged_half_life_days=_float_or_none(half_life),
        logged_graph_discount=_float_or_none(row.get("graph_synthetic_similarity_discount")),
    )


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Replay: counterfactual similarity + build_results ordering                   #
# --------------------------------------------------------------------------- #


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


def replayed_similarity(row: ReplayRow, params: ReplayParams) -> float | None:
    """Recompute the blended similarity under counterfactual parameters."""
    decay = _replayed_decay(row, params)
    if row.ranking_mode == "graph_synthetic":
        return _replayed_graph_synthetic(row, params, decay)
    if row.raw_semantic_score is not None and row.similarity is not None:
        logged_decay = row.temporal_decay_factor
        if decay is None or logged_decay is None or logged_decay <= 0.0:
            return row.similarity
        # base preserves every pre-decay factor (raw score, user boost).
        base = row.similarity / logged_decay
        return base * decay
    return row.similarity


def _replayed_graph_synthetic(
    row: ReplayRow, params: ReplayParams, decay: float | None
) -> float | None:
    if row.graph_score is None or row.similarity is None:
        return row.similarity
    logged_decay = row.temporal_decay_factor
    if decay is None or logged_decay is None or logged_decay <= 0.0:
        return row.similarity
    discount = params.graph_synthetic_discount
    if discount is None:
        discount = row.logged_graph_discount
    if discount is None:
        # Recover the logged discount algebraically from the logged blend.
        if row.graph_score <= 0.0:
            return row.similarity
        discount = row.similarity / (row.graph_score * logged_decay)
    graph_score = row.graph_score * _replayed_edge_blend_ratio(row, params)
    return graph_score * discount * decay


def replayed_sort_key(row: ReplayRow, params: ReplayParams) -> tuple[bool, float, float]:
    """The ``build_results`` ordering: semantic-first, RRF as tiebreak."""
    sim = replayed_similarity(row, params)
    return (sim is not None, sim if sim is not None else _SORT_NONE_SIM, row.ranking_score)


# --------------------------------------------------------------------------- #
# IPS position propensities                                                    #
# --------------------------------------------------------------------------- #


def estimate_position_propensities(
    rows: Iterable[ReplayRow], *, smoothing: float = 1.0
) -> dict[PropensityKey, float]:
    """Label coverage per (injection_group, injection_position), smoothed.

    Approximates the examination propensity P(labeled | injected at slot).
    Unlabeled injected rows count in denominators — that is the entire reason
    the replay loader returns them. Rows without an ``injection_position``
    (shouldn't exist for injected outcomes) are ignored.
    """
    injected: dict[PropensityKey, int] = {}
    labeled: dict[PropensityKey, int] = {}
    for row in rows:
        if row.injection_position is None:
            continue
        key = (row.injection_group, row.injection_position)
        injected[key] = injected.get(key, 0) + 1
        if row.judge_useful is not None:
            labeled[key] = labeled.get(key, 0) + 1
    return {
        key: (labeled.get(key, 0) + smoothing) / (count + 2.0 * smoothing)
        for key, count in injected.items()
    }


def ips_weight(
    row: ReplayRow,
    propensities: Mapping[PropensityKey, float],
    *,
    clip: float = 10.0,
) -> float:
    """Clipped inverse-propensity weight for one labeled row."""
    if row.injection_position is None:
        return 1.0
    propensity = propensities.get((row.injection_group, row.injection_position))
    if propensity is None or propensity <= 0.0:
        return clip
    return min(1.0 / propensity, clip)


# --------------------------------------------------------------------------- #
# Pairwise IPS objective                                                       #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PairwiseEvalResult:
    """IPS-weighted pairwise ordering accuracy over labeled preference pairs."""

    pair_count: int
    weighted_pair_count: float
    accuracy: float
    per_project: dict[str, float]


def evaluate_pairwise(
    rows: Sequence[ReplayRow],
    params_for_project: Mapping[str | None, ReplayParams],
    propensities: Mapping[PropensityKey, float],
    *,
    default_params: ReplayParams,
    clip: float = 10.0,
) -> PairwiseEvalResult:
    """Score (useful, not-useful) pairs within each request under replay.

    A pair is correct when the useful row sorts strictly above the not-useful
    row under the ``build_results`` key; exact key ties earn half credit.
    Pairs are weighted by the positive row's clipped IPS weight. Unlabeled
    rows never form pairs (never-retrieved / unlabeled ≠ negative).
    """
    by_request: dict[str, list[ReplayRow]] = {}
    for row in rows:
        by_request.setdefault(row.recall_request_id, []).append(row)

    pair_count = 0
    weighted_total = 0.0
    weighted_correct = 0.0
    project_totals: dict[str, float] = {}
    project_correct: dict[str, float] = {}

    for request_rows in by_request.values():
        project_id = request_rows[0].project_id
        params = params_for_project.get(project_id, default_params)
        keys = {row.memory_id: replayed_sort_key(row, params) for row in request_rows}
        positives = [r for r in request_rows if r.judge_useful is True]
        negatives = [r for r in request_rows if r.judge_useful is False]
        for pos in positives:
            weight = ips_weight(pos, propensities, clip=clip)
            for neg in negatives:
                pair_count += 1
                credit = _pair_credit(keys[pos.memory_id], keys[neg.memory_id])
                weighted_total += weight
                weighted_correct += weight * credit
                bucket = project_id or ""
                project_totals[bucket] = project_totals.get(bucket, 0.0) + weight
                project_correct[bucket] = project_correct.get(bucket, 0.0) + weight * credit

    accuracy = weighted_correct / weighted_total if weighted_total > 0 else 0.0
    per_project = {
        project: project_correct[project] / total
        for project, total in project_totals.items()
        if total > 0
    }
    return PairwiseEvalResult(
        pair_count=pair_count,
        weighted_pair_count=weighted_total,
        accuracy=accuracy,
        per_project=per_project,
    )


def _pair_credit(
    positive_key: tuple[bool, float, float], negative_key: tuple[bool, float, float]
) -> float:
    if positive_key > negative_key:
        return 1.0
    if positive_key == negative_key:
        return 0.5
    return 0.0


# --------------------------------------------------------------------------- #
# Per-project split + partial-pooled fit                                       #
# --------------------------------------------------------------------------- #


def split_requests_per_project(
    rows: Sequence[ReplayRow], *, eval_stride: int = 2
) -> tuple[list[ReplayRow], list[ReplayRow]]:
    """Deterministic train/eval split of requests *within* each project.

    Distinct request IDs are sorted per project and every ``eval_stride``-th
    request goes to eval. Splitting inside each project (rather than pooling
    all requests) keeps every project represented on both sides, which the
    partial-pooled fit and its evaluation both rely on.
    """
    if eval_stride < 2:
        raise ValueError(f"eval_stride must be >= 2, got {eval_stride}")
    requests_by_project: dict[str | None, list[str]] = {}
    for row in rows:
        bucket = requests_by_project.setdefault(row.project_id, [])
        if row.recall_request_id not in bucket:
            bucket.append(row.recall_request_id)

    eval_requests: set[str] = set()
    for request_ids in requests_by_project.values():
        for index, request_id in enumerate(sorted(request_ids)):
            if index % eval_stride == eval_stride - 1:
                eval_requests.add(request_id)

    train = [row for row in rows if row.recall_request_id not in eval_requests]
    evaluation = [row for row in rows if row.recall_request_id in eval_requests]
    return train, evaluation


@dataclass(frozen=True)
class FittedParams:
    """Pooled fit plus per-project fits shrunk toward it (partial pooling)."""

    pooled: ReplayParams
    per_project: dict[str | None, ReplayParams]
    pooled_pairs: int
    project_pairs: dict[str | None, int]


def _labeled_pair_count(rows: Sequence[ReplayRow]) -> int:
    by_request: dict[str, tuple[int, int]] = {}
    for row in rows:
        pos, neg = by_request.get(row.recall_request_id, (0, 0))
        if row.judge_useful is True:
            pos += 1
        elif row.judge_useful is False:
            neg += 1
        by_request[row.recall_request_id] = (pos, neg)
    return sum(pos * neg for pos, neg in by_request.values())


def _grid_best(
    rows: Sequence[ReplayRow],
    grid: Sequence[ReplayParams],
    propensities: Mapping[PropensityKey, float],
    *,
    clip: float,
) -> ReplayParams:
    best = grid[0]
    best_accuracy = -1.0
    for candidate in grid:
        result = evaluate_pairwise(rows, {}, propensities, default_params=candidate, clip=clip)
        if result.accuracy > best_accuracy:
            best = candidate
            best_accuracy = result.accuracy
    return best


def _shrink_value(
    project_value: float | None, pooled_value: float | None, lam: float
) -> float | None:
    """Blend one parameter; ``None`` means "keep logged" and cannot blend."""
    if project_value is None or pooled_value is None:
        return project_value if lam >= 0.5 else pooled_value
    return lam * project_value + (1.0 - lam) * pooled_value


def _shrink_params(project: ReplayParams, pooled: ReplayParams, lam: float) -> ReplayParams:
    cap = _shrink_value(
        float(project.cooccur_support_cap) if project.cooccur_support_cap is not None else None,
        float(pooled.cooccur_support_cap) if pooled.cooccur_support_cap is not None else None,
        lam,
    )
    return ReplayParams(
        half_life_days=_shrink_value(project.half_life_days, pooled.half_life_days, lam),
        graph_synthetic_discount=_shrink_value(
            project.graph_synthetic_discount, pooled.graph_synthetic_discount, lam
        ),
        cooccur_alpha=_shrink_value(project.cooccur_alpha, pooled.cooccur_alpha, lam),
        cooccur_support_cap=round(cap) if cap is not None else None,
    )


def fit_partial_pooled(
    rows: Sequence[ReplayRow],
    grid: Sequence[ReplayParams],
    propensities: Mapping[PropensityKey, float],
    *,
    shrinkage_pairs: float = 50.0,
    clip: float = 10.0,
) -> FittedParams:
    """Grid-fit pooled params, then per-project params shrunk toward pooled.

    Shrinkage weight ``lam = n_p / (n_p + shrinkage_pairs)`` where ``n_p`` is
    the project's labeled pair count: small projects ride the pooled fit,
    well-supported projects keep their own optimum. This is the
    partial-pooling contract of #17197 — no naive full pooling, no
    unregularized per-project fits.
    """
    if not grid:
        raise ValueError("grid must contain at least one ReplayParams candidate")
    pooled = _grid_best(rows, grid, propensities, clip=clip)

    rows_by_project: dict[str | None, list[ReplayRow]] = {}
    for row in rows:
        rows_by_project.setdefault(row.project_id, []).append(row)

    per_project: dict[str | None, ReplayParams] = {}
    project_pairs: dict[str | None, int] = {}
    for project_id, project_rows in rows_by_project.items():
        pairs = _labeled_pair_count(project_rows)
        project_pairs[project_id] = pairs
        if pairs == 0:
            per_project[project_id] = pooled
            continue
        project_best = _grid_best(project_rows, grid, propensities, clip=clip)
        lam = pairs / (pairs + shrinkage_pairs)
        per_project[project_id] = _shrink_params(project_best, pooled, lam)

    return FittedParams(
        pooled=pooled,
        per_project=per_project,
        pooled_pairs=_labeled_pair_count(rows),
        project_pairs=project_pairs,
    )


# --------------------------------------------------------------------------- #
# End-to-end harness entry point                                               #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LabeledFitReport:
    """Everything #17198's ship gate needs from one fit-and-holdout run."""

    rows_total: int
    rows_labeled: int
    train_requests: int
    eval_requests: int
    fitted: FittedParams
    baseline_eval: PairwiseEvalResult
    fitted_eval: PairwiseEvalResult


def fit_and_evaluate(
    rows: Sequence[ReplayRow],
    grid: Sequence[ReplayParams],
    *,
    eval_stride: int = 2,
    smoothing: float = 1.0,
    clip: float = 10.0,
    shrinkage_pairs: float = 50.0,
) -> LabeledFitReport:
    """Split per project, fit with partial pooling, evaluate on the holdout.

    The baseline arm replays the *logged* parameters (``ReplayParams()`` with
    every field ``None``) on the same holdout — the fitted-vs-static
    comparison #17198's must-beat-static gate consumes. Propensities are
    estimated on the training split only, then reused for the holdout so the
    two arms are weighted identically.
    """
    train, evaluation = split_requests_per_project(rows, eval_stride=eval_stride)
    propensities = estimate_position_propensities(train, smoothing=smoothing)
    fitted = fit_partial_pooled(
        train, grid, propensities, shrinkage_pairs=shrinkage_pairs, clip=clip
    )
    baseline = ReplayParams()
    baseline_eval = evaluate_pairwise(
        evaluation, {}, propensities, default_params=baseline, clip=clip
    )
    fitted_eval = evaluate_pairwise(
        evaluation,
        fitted.per_project,
        propensities,
        default_params=fitted.pooled,
        clip=clip,
    )
    return LabeledFitReport(
        rows_total=len(rows),
        rows_labeled=sum(1 for row in rows if row.judge_useful is not None),
        train_requests=len({row.recall_request_id for row in train}),
        eval_requests=len({row.recall_request_id for row in evaluation}),
        fitted=fitted,
        baseline_eval=baseline_eval,
        fitted_eval=fitted_eval,
    )


def default_replay_grid() -> list[ReplayParams]:
    """Half-life sweep around today's defaults; other constants stay logged.

    #17198 widens this to the (alpha, cap) dimensions; the harness default
    keeps the exactly-replayable axis so grid size stays trivial.
    """
    baseline = ReplayParams()
    return [baseline] + [
        replace(baseline, half_life_days=half_life) for half_life in (7.0, 14.0, 30.0, 60.0, 120.0)
    ]
