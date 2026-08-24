"""Partial-pooled parameter fitting over logged recall-signal rows (#17197).

``recall_fit`` owns the replay algebra and the evaluation metrics; this module
owns the search procedure that consumes them. Fitting grid-searches pooled
constants, refits per project, and shrinks each project toward the pooled fit
in proportion to its own mixed-request support, so a project with three
labeled requests cannot drag the constants around.

Shrinkage strength is itself fitted, on planted synthetic data plus a
validation split nested *inside* training, so the outer holdout stays
untouched by every selection decision.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from gobby.memory.recall_fit import (
    REQUEST_SPLIT_VERSION,
    PairwiseEvalResult,
    PropensityKey,
    ReplayParams,
    ReplayRow,
    WeightingMode,
    estimate_position_propensities,
    evaluate_pairwise,
    split_requests_per_project,
)

SHRINKAGE_SELECTION_METHOD = "synthetic+nested-training-v1"
SHRINKAGE_REQUEST_CANDIDATES: tuple[float, ...] = (2.0, 5.0, 10.0, 20.0, 50.0)


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


def fit_and_evaluate_partitioned(
    train: Sequence[ReplayRow],
    evaluation: Sequence[ReplayRow],
    grid: Sequence[ReplayParams],
    *,
    smoothing: float = 1.0,
    clip: float = 10.0,
    shrinkage_requests: float = 50.0,
    weighting_mode: WeightingMode = "full",
) -> LabeledFitReport:
    """Fit on an already-frozen training partition and evaluate its holdout."""
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
        rows_total=len(train) + len(evaluation),
        rows_labeled=sum(1 for row in (*train, *evaluation) if row.judge_useful is not None),
        train_requests=len({row.recall_request_id for row in train}),
        eval_requests=len({row.recall_request_id for row in evaluation}),
        fitted=fitted,
        baseline_eval=baseline_eval,
        fitted_eval=fitted_eval,
    )


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
    return fit_and_evaluate_partitioned(
        train,
        evaluation,
        grid,
        smoothing=smoothing,
        shrinkage_requests=shrinkage_requests,
        clip=clip,
        weighting_mode=weighting_mode,
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
