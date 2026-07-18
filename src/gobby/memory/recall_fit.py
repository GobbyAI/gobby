"""Offline fit/eval over logged recall-signal rows (#17197, epic #17099).

This module generalizes the offline recall benchmark harness to real labeled
data. It consumes request-aligned per-hit feature rows from the promoted hub
tables and replays the FULL ranking path —
the ``SearchService`` blend ordering from ``build_results`` (semantic-first on
the similarity axis with temporal decay and ``ranking_mode`` semantics, RRF
``ranking_score`` as tiebreak) — under counterfactual parameters, without
re-running retrieval.

Scope and semantics (contract: docs/contracts/memory-usefulness-label.md):

- **Request-balanced evaluation.** Every request containing both relevance
  classes contributes one effective unit regardless of its pair cardinality.
- **Never-retrieved memories are unlabeled, not negative.** The pairwise
  objective forms pairs only between explicitly labeled rows within the same
  recall request. Rows without a label contribute to propensity estimation
  (denominators) only.
- **Scope-specific weighting.** Full shadow cohorts weight pairs uniformly.
  Injected cohorts preserve relative clipped IPS weights within each request.
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
from hashlib import sha256
from typing import Any, Literal

from gobby.memory.services.knowledge_graph.writer import (
    COOCCUR_ALPHA,
    COOCCUR_SUPPORT_CAP,
)

_SORT_NONE_SIM = float("-inf")

# Propensity keys are (injection_group, injection_position); position is the
# rendered ordinal within the injection block, per the label contract §5.
PropensityKey = tuple[str | None, int]
WeightingMode = Literal["full", "injected"]

REQUEST_SPLIT_VERSION = "recall-request-hash-split-v1"
PAIRWISE_EVALUATOR_VERSION = "recall-request-normalized-pairwise-v1"
AUDIT_SAMPLER_VERSION = "recall-training-request-sampler-v1"
SHRINKAGE_SELECTION_METHOD = "synthetic+nested-training-v1"
SHRINKAGE_REQUEST_CANDIDATES: tuple[float, ...] = (2.0, 5.0, 10.0, 20.0, 50.0)


def evaluation_protocol_identity(*, split_version: str = REQUEST_SPLIT_VERSION) -> dict[str, str]:
    """Version fields that bind fitting, audit sampling, and holdout evaluation."""
    return {
        "split_version": split_version,
        "evaluator_version": PAIRWISE_EVALUATOR_VERSION,
        "audit_sampler_version": AUDIT_SAMPLER_VERSION,
    }


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
    """Request-normalized ordering accuracy over labeled preference pairs."""

    pair_count: int
    mixed_request_count: int
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
    weighting_mode: WeightingMode = "full",
) -> PairwiseEvalResult:
    """Score (useful, not-useful) pairs within each request under replay.

    A pair is correct when the useful row sorts strictly above the not-useful
    row under the ``build_results`` key; exact key ties earn half credit.
    Every mixed request has total weight 1. Full-candidate cohorts weight each
    pair uniformly; injected cohorts preserve relative positive-row IPS weights
    within that request. Unlabeled rows never form preference pairs.
    """
    if weighting_mode not in ("full", "injected"):
        raise ValueError(f"unsupported weighting_mode: {weighting_mode}")

    by_request: dict[str, list[ReplayRow]] = {}
    for row in rows:
        by_request.setdefault(row.recall_request_id, []).append(row)

    pair_count = 0
    mixed_request_count = 0
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
        if not positives or not negatives:
            continue

        mixed_request_count += 1
        if weighting_mode == "full":
            positive_weights = [1.0] * len(positives)
        else:
            positive_weights = [ips_weight(pos, propensities, clip=clip) for pos in positives]
        request_denominator = len(negatives) * sum(positive_weights)

        bucket = project_id or ""
        for pos, positive_weight in zip(positives, positive_weights, strict=True):
            pair_weight = positive_weight / request_denominator
            for neg in negatives:
                pair_count += 1
                credit = _pair_credit(keys[pos.memory_id], keys[neg.memory_id])
                weighted_correct += pair_weight * credit
                project_correct[bucket] = project_correct.get(bucket, 0.0) + pair_weight * credit
        weighted_total += 1.0
        project_totals[bucket] = project_totals.get(bucket, 0.0) + 1.0

    accuracy = weighted_correct / weighted_total if weighted_total > 0 else 0.0
    per_project = {
        project: project_correct[project] / total
        for project, total in project_totals.items()
        if total > 0
    }
    return PairwiseEvalResult(
        pair_count=pair_count,
        mixed_request_count=mixed_request_count,
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
    rows: Sequence[ReplayRow],
    *,
    eval_stride: int = 2,
    split_version: str = REQUEST_SPLIT_VERSION,
) -> tuple[list[ReplayRow], list[ReplayRow]]:
    """Deterministic train/eval split of requests *within* each project.

    Request IDs seed a versioned hash ordering within each project; every
    ``eval_stride``-th request goes to holdout. Input ordering cannot affect
    the frozen partition.
    """
    if eval_stride < 2:
        raise ValueError(f"eval_stride must be >= 2, got {eval_stride}")
    if not split_version.strip():
        raise ValueError("split_version must be non-empty")
    requests_by_project: dict[str | None, set[str]] = {}
    for row in rows:
        requests_by_project.setdefault(row.project_id, set()).add(row.recall_request_id)

    eval_requests: set[str] = set()
    for request_ids in requests_by_project.values():
        seeded_request_ids = sorted(
            request_ids,
            key=lambda request_id: (
                sha256(f"{split_version}\0{request_id}".encode()).digest(),
                request_id,
            ),
        )
        for index, request_id in enumerate(seeded_request_ids):
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
    pooled_mixed_requests: int
    project_mixed_requests: dict[str | None, int]


@dataclass(frozen=True)
class ShrinkageSelection:
    """Frozen request-unit prior chosen without evaluating outer holdout rows."""

    selected_requests: float
    candidate_scores: dict[float, float]
    selection_method: str
    nested_train_requests: int
    nested_validation_requests: int
    synthetic_requests: int


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


def _mixed_request_count(rows: Sequence[ReplayRow]) -> int:
    by_request: dict[str, tuple[bool, bool]] = {}
    for row in rows:
        has_positive, has_negative = by_request.get(row.recall_request_id, (False, False))
        if row.judge_useful is True:
            has_positive = True
        elif row.judge_useful is False:
            has_negative = True
        by_request[row.recall_request_id] = (has_positive, has_negative)
    return sum(has_positive and has_negative for has_positive, has_negative in by_request.values())


def _grid_best(
    rows: Sequence[ReplayRow],
    grid: Sequence[ReplayParams],
    propensities: Mapping[PropensityKey, float],
    *,
    clip: float,
    weighting_mode: WeightingMode,
) -> ReplayParams:
    best = grid[0]
    best_accuracy = -1.0
    for candidate in grid:
        result = evaluate_pairwise(
            rows,
            {},
            propensities,
            default_params=candidate,
            clip=clip,
            weighting_mode=weighting_mode,
        )
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
    shrinkage_requests: float = 50.0,
    clip: float = 10.0,
    weighting_mode: WeightingMode = "full",
) -> FittedParams:
    """Grid-fit pooled params, then per-project params shrunk toward pooled.

    Shrinkage weight ``lam = n_p / (n_p + shrinkage_requests)`` where ``n_p``
    is the project's mixed-request count: small projects ride the pooled fit,
    well-supported projects keep their own optimum. This is the
    partial-pooling contract of #17197 — no naive full pooling, no
    unregularized per-project fits.
    """
    if not grid:
        raise ValueError("grid must contain at least one ReplayParams candidate")
    if shrinkage_requests < 0.0:
        raise ValueError("shrinkage_requests must be non-negative")
    pooled = _grid_best(rows, grid, propensities, clip=clip, weighting_mode=weighting_mode)

    rows_by_project: dict[str | None, list[ReplayRow]] = {}
    for row in rows:
        rows_by_project.setdefault(row.project_id, []).append(row)

    per_project: dict[str | None, ReplayParams] = {}
    project_pairs: dict[str | None, int] = {}
    project_mixed_requests: dict[str | None, int] = {}
    for project_id, project_rows in rows_by_project.items():
        pairs = _labeled_pair_count(project_rows)
        mixed_requests = _mixed_request_count(project_rows)
        project_pairs[project_id] = pairs
        project_mixed_requests[project_id] = mixed_requests
        if mixed_requests == 0:
            per_project[project_id] = pooled
            continue
        project_best = _grid_best(
            project_rows,
            grid,
            propensities,
            clip=clip,
            weighting_mode=weighting_mode,
        )
        lam = mixed_requests / (mixed_requests + shrinkage_requests)
        per_project[project_id] = _shrink_params(project_best, pooled, lam)

    return FittedParams(
        pooled=pooled,
        per_project=per_project,
        pooled_pairs=_labeled_pair_count(rows),
        project_pairs=project_pairs,
        pooled_mixed_requests=_mixed_request_count(rows),
        project_mixed_requests=project_mixed_requests,
    )


def select_shrinkage_requests(
    *,
    training_rows: Sequence[ReplayRow],
    synthetic_rows: Sequence[ReplayRow],
    grid: Sequence[ReplayParams],
    candidates: Sequence[float] = SHRINKAGE_REQUEST_CANDIDATES,
    eval_stride: int = 2,
    split_version: str = REQUEST_SPLIT_VERSION,
    smoothing: float = 1.0,
    clip: float = 10.0,
    weighting_mode: WeightingMode = "full",
) -> ShrinkageSelection:
    """Choose shrinkage on planted data plus validation nested inside training."""
    if not candidates:
        raise ValueError("candidates must contain at least one request-unit prior")
    if any(candidate <= 0.0 for candidate in candidates):
        raise ValueError("shrinkage request candidates must be positive")

    nested_train, nested_validation = split_requests_per_project(
        training_rows,
        eval_stride=eval_stride,
        split_version=f"{split_version}:{SHRINKAGE_SELECTION_METHOD}",
    )
    propensities = estimate_position_propensities(nested_train, smoothing=smoothing)
    candidate_scores: dict[float, float] = {}
    for candidate in candidates:
        fitted = fit_partial_pooled(
            nested_train,
            grid,
            propensities,
            shrinkage_requests=candidate,
            clip=clip,
            weighting_mode=weighting_mode,
        )
        nested_result = evaluate_pairwise(
            nested_validation,
            fitted.per_project,
            propensities,
            default_params=fitted.pooled,
            clip=clip,
            weighting_mode=weighting_mode,
        )
        synthetic_result = evaluate_pairwise(
            synthetic_rows,
            fitted.per_project,
            propensities,
            default_params=fitted.pooled,
            clip=clip,
            weighting_mode=weighting_mode,
        )
        evaluated_requests = (
            nested_result.mixed_request_count + synthetic_result.mixed_request_count
        )
        if evaluated_requests == 0:
            raise ValueError("shrinkage selection requires mixed validation requests")
        candidate_scores[float(candidate)] = (
            nested_result.accuracy * nested_result.mixed_request_count
            + synthetic_result.accuracy * synthetic_result.mixed_request_count
        ) / evaluated_requests

    selected = max(candidate_scores, key=candidate_scores.__getitem__)
    return ShrinkageSelection(
        selected_requests=selected,
        candidate_scores=candidate_scores,
        selection_method=SHRINKAGE_SELECTION_METHOD,
        nested_train_requests=len({row.recall_request_id for row in nested_train}),
        nested_validation_requests=len({row.recall_request_id for row in nested_validation}),
        synthetic_requests=len({row.recall_request_id for row in synthetic_rows}),
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
    shrinkage_requests: float = 50.0,
    weighting_mode: WeightingMode = "full",
    split_version: str = REQUEST_SPLIT_VERSION,
) -> LabeledFitReport:
    """Split per project, fit with partial pooling, evaluate on the holdout.

    The baseline arm replays the *logged* parameters (``ReplayParams()`` with
    every field ``None``) on the same holdout — the fitted-vs-static
    comparison #17198's must-beat-static gate consumes. Propensities are
    estimated on the training split only, then reused for the holdout so the
    two arms are weighted identically.
    """
    train, evaluation = split_requests_per_project(
        rows, eval_stride=eval_stride, split_version=split_version
    )
    propensities = estimate_position_propensities(train, smoothing=smoothing)
    fitted = fit_partial_pooled(
        train,
        grid,
        propensities,
        shrinkage_requests=shrinkage_requests,
        clip=clip,
        weighting_mode=weighting_mode,
    )
    baseline = ReplayParams()
    baseline_eval = evaluate_pairwise(
        evaluation,
        {},
        propensities,
        default_params=baseline,
        clip=clip,
        weighting_mode=weighting_mode,
    )
    fitted_eval = evaluate_pairwise(
        evaluation,
        fitted.per_project,
        propensities,
        default_params=fitted.pooled,
        clip=clip,
        weighting_mode=weighting_mode,
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
