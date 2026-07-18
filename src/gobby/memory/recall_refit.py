"""Offline closed-form re-fit of recall constants with a must-beat-static ship gate.

#17198 (epic #17099 Phase 2b). Consumes the #17197 replay/fit harness
(:mod:`gobby.memory.recall_fit`) and adds the shipping policy around it:

1. ``static_replay_params()`` — the frozen constants shipping today, pulled
   from their production sources so this module can never drift from them.
2. ``refit_grid()`` — an interpretable sweep anchored at the static point:
   single-axis half-life and graph-discount variations plus the full
   (``cooccur_alpha`` × ``cooccur_support_cap``) product (the two edge-blend
   parameters plausibly interact; the others are swept one axis at a time so
   every fitted point stays directly comparable to today's constants). The
   grid is ordered ``[logged-baseline, static, ...]`` and the fit keeps the
   first strict maximum, so ties regularize toward no-change.
3. ``judge_independent_guard_rows()`` — a constructed planted-truth battery.
   Ground truth comes from construction, never from a judge, so a fit that
   exploits judge-label artifacts (reward hacking, arXiv:2210.10760) cannot
   also satisfy it by construction. Static constants score 1.0 on the
   battery; parameter regions that violate the encoded domain priors
   (half-life 7d, support-only or cosine-only edge blends, collapsed graph
   discounts) score below 1.0 and are unshippable (safe exploration,
   arXiv:2002.00467).
4. ``run_ship_gate()`` / ``run_ship_gate_from_store()`` — fit, hold out,
   compare against the *static* constants (not just the logged baseline),
   apply the three gates (sufficient data, beats static, no guard
   regression), and return a serializable :class:`GateDecision`.

Clustering parameters (``min_cluster_size``, ``min_samples``) are NOT swept
here: they change retrieval candidate sets, which logged-hit replay cannot
express (#17197). They are swept on the live synthetic-corpus arms in
``tests/memory/test_recall_benchmark.py``, which is judge-independent by
construction (planted corpus, no labels).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from gobby.config.persistence import MemoryConfig
from gobby.memory.recall_fit import (
    REQUEST_SPLIT_VERSION,
    LabeledFitReport,
    PairwiseEvalResult,
    ReplayParams,
    ReplayRow,
    WeightingMode,
    estimate_position_propensities,
    evaluate_pairwise,
    evaluation_protocol_identity,
    fit_and_evaluate,
    replay_row_from_signal_row,
    split_requests_per_project,
)
from gobby.memory.services._search_constants import _GRAPH_SYNTHETIC_SIM_DISCOUNT
from gobby.memory.services.knowledge_graph.writer import COOCCUR_ALPHA, COOCCUR_SUPPORT_CAP

if TYPE_CHECKING:
    from collections.abc import Sequence

    from gobby.storage.recall_signals import RecallSignalStore

# Below these floors a grid argmax is noise, not signal: with one preference
# pair per labeled request, 50 train pairs ≈ 50 requests — roughly the point
# where a 19-point grid stops overfitting coin flips — and 20 holdout pairs
# bound the comparison's standard error under ~0.11. Data-starved fits are
# rejected with an explicit reason instead of shipping on vapor.
MIN_TRAIN_PAIRS = 50
MIN_EVAL_PAIRS = 20

HALF_LIFE_GRID: tuple[float, ...] = (7.0, 14.0, 60.0, 120.0)
DISCOUNT_GRID: tuple[float, ...] = (0.8, 1.0)
ALPHA_GRID: tuple[float, ...] = (0.25, 0.5, 0.75, 1.0)
CAP_GRID: tuple[int, ...] = (3, 5, 8)


def static_replay_params() -> ReplayParams:
    """Today's shipped constants, read from their production sources."""
    half_life = MemoryConfig.model_fields["temporal_decay_half_life_days"].default
    return ReplayParams(
        half_life_days=float(half_life),
        graph_synthetic_discount=_GRAPH_SYNTHETIC_SIM_DISCOUNT,
        cooccur_alpha=COOCCUR_ALPHA,
        cooccur_support_cap=COOCCUR_SUPPORT_CAP,
    )


def refit_grid() -> list[ReplayParams]:
    """Static-anchored sweep: axis singles plus the alpha × cap product.

    Every non-baseline point carries explicit values on all four axes, so a
    fitted result is a complete, concrete constant set (what #17200 promotes
    to config). Order matters: ``fit_partial_pooled`` keeps the first strict
    maximum, so [baseline, static, ...] prefers no-change on ties.
    """
    static = static_replay_params()
    grid: list[ReplayParams] = [ReplayParams(), static]
    grid.extend(replace(static, half_life_days=h) for h in HALF_LIFE_GRID)
    grid.extend(replace(static, graph_synthetic_discount=d) for d in DISCOUNT_GRID)
    grid.extend(
        replace(static, cooccur_alpha=alpha, cooccur_support_cap=cap)
        for alpha in ALPHA_GRID
        for cap in CAP_GRID
        if not (alpha == static.cooccur_alpha and cap == static.cooccur_support_cap)
    )
    return grid


def _guard_semantic(
    request_id: str,
    memory_id: str,
    *,
    raw: float,
    decay: float,
    useful: bool,
) -> ReplayRow:
    return ReplayRow(
        recall_request_id=request_id,
        memory_id=memory_id,
        project_id=None,
        rank=0,
        similarity=raw * decay,
        raw_semantic_score=raw,
        temporal_decay_factor=decay,
        ranking_score=0.0,
        ranking_mode="similarity",
        graph_score=None,
        edge_cosine=None,
        edge_support_norm=None,
        edge_weight_blend=None,
        injection_position=None,
        injection_group="guard",
        judge_useful=useful,
        label_source="constructed",
        logged_half_life_days=30.0,
        logged_graph_discount=None,
    )


def _guard_graph(
    request_id: str,
    memory_id: str,
    *,
    graph: float,
    decay: float,
    useful: bool,
    edge_cosine: float | None = None,
    edge_support_norm: float | None = None,
) -> ReplayRow:
    blend: float | None = None
    if edge_cosine is not None and edge_support_norm is not None:
        blend = COOCCUR_ALPHA * edge_cosine + (1.0 - COOCCUR_ALPHA) * edge_support_norm
    return ReplayRow(
        recall_request_id=request_id,
        memory_id=memory_id,
        project_id=None,
        rank=0,
        similarity=graph * _GRAPH_SYNTHETIC_SIM_DISCOUNT * decay,
        raw_semantic_score=None,
        temporal_decay_factor=decay,
        ranking_score=0.0,
        ranking_mode="graph_synthetic",
        graph_score=graph,
        edge_cosine=edge_cosine,
        edge_support_norm=edge_support_norm,
        edge_weight_blend=blend,
        injection_position=None,
        injection_group="guard",
        judge_useful=useful,
        label_source="constructed",
        logged_half_life_days=30.0,
        logged_graph_discount=_GRAPH_SYNTHETIC_SIM_DISCOUNT,
    )


def judge_independent_guard_rows() -> list[ReplayRow]:
    """Planted-truth ranking priors; no judge output anywhere in the labels.

    Each pair lives in its own request and probes one parameter axis (the
    other axes cancel within the pair: temporal pairs are semantic-only,
    graph pairs share their decay factor). Logged at half-life 30d, discount
    0.9, alpha 0.5, cap 5 — the static point, which scores 1.0 by
    construction. The encoded envelope:

    - T1: a strong semantic match beats a weak fresh one at any sane
      half-life (fails only for sub-week recency collapse).
    - T2: a 12-day-old raw-0.95 match beats a fresh raw-0.45 one — half-life
      7d violates this prior and is unshippable; 14d+ passes.
    - T3: a fresh decent match beats a month-old marginally-stronger one —
      guards against decay effectively disabling (very long half-lives
      within the grid still pass).
    - G1/G2: graph-synthetic discounts collapsing toward 0 (or inflating
      past 1) flip the semantic/graph ordering.
    - E1: a strong-cosine weak-support edge must beat a weak-cosine
      popularity edge — support-dominant blends (alpha 0.25) fail.
    - E2: a well-supported decent-cosine edge must beat an unsupported
      borderline-cosine edge — cosine-only blends (alpha 1.0) fail.
    """
    return [
        # T1 — semantic anchor: relevant .95 raw at 4.6d vs irrelevant .35 raw fresh.
        _guard_semantic("guard-T1", "t1-rel", raw=0.95, decay=0.9, useful=True),
        _guard_semantic("guard-T1", "t1-irr", raw=0.35, decay=0.98, useful=False),
        # T2 — stale-relevant prior: .95 raw at 12.5d vs .45 raw fresh.
        _guard_semantic("guard-T2", "t2-rel", raw=0.95, decay=0.75, useful=True),
        _guard_semantic("guard-T2", "t2-irr", raw=0.45, decay=0.96, useful=False),
        # T3 — fresh-relevant prior: .6 raw fresh vs .65 raw at 30d.
        _guard_semantic("guard-T3", "t3-rel", raw=0.6, decay=0.95, useful=True),
        _guard_semantic("guard-T3", "t3-irr", raw=0.65, decay=0.5, useful=False),
        # G1 — a real graph neighbor must survive discounts down to 0.8.
        _guard_graph("guard-G1", "g1-rel", graph=0.9, decay=0.9, useful=True),
        _guard_semantic("guard-G1", "g1-irr", raw=0.5, decay=0.9, useful=False),
        # G2 — a modest graph neighbor must not outrank a strong semantic hit.
        _guard_semantic("guard-G2", "g2-rel", raw=0.8, decay=0.9, useful=True),
        _guard_graph("guard-G2", "g2-irr", graph=0.6, decay=0.9, useful=False),
        # E1 — cosine must carry: strong-cosine/weak-support over popularity.
        _guard_graph(
            "guard-E1",
            "e1-rel",
            graph=0.8,
            decay=0.9,
            useful=True,
            edge_cosine=0.9,
            edge_support_norm=0.2,
        ),
        _guard_graph(
            "guard-E1",
            "e1-irr",
            graph=0.6,
            decay=0.9,
            useful=False,
            edge_cosine=0.2,
            edge_support_norm=1.0,
        ),
        # E2 — support must count: supported decent cosine over unsupported.
        _guard_graph(
            "guard-E2",
            "e2-rel",
            graph=0.85,
            decay=0.9,
            useful=True,
            edge_cosine=0.65,
            edge_support_norm=1.0,
        ),
        _guard_graph(
            "guard-E2",
            "e2-irr",
            graph=0.4,
            decay=0.9,
            useful=False,
            edge_cosine=0.7,
            edge_support_norm=0.0,
        ),
    ]


def guard_accuracy(params: ReplayParams) -> float:
    """Battery pass rate for one candidate; guard rows carry unit weights."""
    return evaluate_pairwise(judge_independent_guard_rows(), {}, {}, default_params=params).accuracy


@dataclass(frozen=True)
class GateDecision:
    """One recorded fit + gate outcome; ``ship`` is the whole verdict."""

    label_source: str
    weighting_mode: WeightingMode
    split_version: str
    report: LabeledFitReport
    static_params: ReplayParams
    static_eval: PairwiseEvalResult
    guard_static: float
    guard_fitted: float
    sufficient_data: bool
    beats_static: bool
    guard_ok: bool
    ship: bool
    reasons: tuple[str, ...]

    def to_record(self) -> dict[str, Any]:
        """JSON-serializable decision record; callers stamp timestamps."""

        def params_dict(params: ReplayParams) -> dict[str, float | int | None]:
            return {
                "half_life_days": params.half_life_days,
                "graph_synthetic_discount": params.graph_synthetic_discount,
                "cooccur_alpha": params.cooccur_alpha,
                "cooccur_support_cap": params.cooccur_support_cap,
            }

        def eval_dict(result: PairwiseEvalResult) -> dict[str, Any]:
            return {
                "pair_count": result.pair_count,
                "mixed_request_count": result.mixed_request_count,
                "weighted_pair_count": result.weighted_pair_count,
                "accuracy": result.accuracy,
                "per_project": dict(result.per_project),
            }

        return {
            "task": "#17198",
            "label_source": self.label_source,
            "cohort_identity": {
                "label_source": self.label_source,
                "weighting_mode": self.weighting_mode,
                **evaluation_protocol_identity(split_version=self.split_version),
            },
            "rows_total": self.report.rows_total,
            "rows_labeled": self.report.rows_labeled,
            "train_requests": self.report.train_requests,
            "eval_requests": self.report.eval_requests,
            "train_pairs": self.report.fitted.pooled_pairs,
            "train_mixed_requests": self.report.fitted.pooled_mixed_requests,
            "fitted_params": params_dict(self.report.fitted.pooled),
            "static_params": params_dict(self.static_params),
            "fitted_eval": eval_dict(self.report.fitted_eval),
            "static_eval": eval_dict(self.static_eval),
            "logged_baseline_eval": eval_dict(self.report.baseline_eval),
            "guard_static": self.guard_static,
            "guard_fitted": self.guard_fitted,
            "gates": {
                "sufficient_data": self.sufficient_data,
                "beats_static": self.beats_static,
                "guard_ok": self.guard_ok,
            },
            "ship": self.ship,
            "reasons": list(self.reasons),
        }


def run_ship_gate(
    rows: Sequence[ReplayRow],
    *,
    label_source: str,
    grid: Sequence[ReplayParams] | None = None,
    eval_stride: int = 2,
    smoothing: float = 1.0,
    clip: float = 10.0,
    shrinkage_requests: float = 50.0,
    weighting_mode: WeightingMode = "full",
    split_version: str = REQUEST_SPLIT_VERSION,
    min_train_pairs: int = MIN_TRAIN_PAIRS,
    min_eval_pairs: int = MIN_EVAL_PAIRS,
) -> GateDecision:
    """Fit on one label stream and decide ship vs keep-static.

    The three gates, all required:

    1. sufficient data — train/holdout pair floors; a starved fit is noise.
    2. beats static — holdout accuracy strictly above the static constants
       replayed on the same rows with the same train-side propensities.
    3. guard — the fitted pooled point must not regress the constructed
       judge-independent battery relative to static (which scores 1.0).

    The guard evaluates the *pooled* fit: per-project fits shrink toward it
    and inherit its envelope.
    """
    fit_grid = list(grid) if grid is not None else refit_grid()
    report = fit_and_evaluate(
        rows,
        fit_grid,
        eval_stride=eval_stride,
        smoothing=smoothing,
        clip=clip,
        shrinkage_requests=shrinkage_requests,
        weighting_mode=weighting_mode,
        split_version=split_version,
    )

    # Re-derive the identical deterministic split/propensities so the static
    # arm is scored on exactly the holdout the fitted arm was scored on.
    train, evaluation = split_requests_per_project(
        rows, eval_stride=eval_stride, split_version=split_version
    )
    propensities = estimate_position_propensities(train, smoothing=smoothing)
    static = static_replay_params()
    static_eval = evaluate_pairwise(
        evaluation,
        {},
        propensities,
        default_params=static,
        clip=clip,
        weighting_mode=weighting_mode,
    )

    guard_static = guard_accuracy(static)
    guard_fitted = guard_accuracy(report.fitted.pooled)

    sufficient_data = (
        report.fitted.pooled_pairs >= min_train_pairs and static_eval.pair_count >= min_eval_pairs
    )
    beats_static = report.fitted_eval.accuracy > static_eval.accuracy
    guard_ok = guard_fitted >= guard_static

    reasons: list[str] = []
    if not sufficient_data:
        reasons.append(
            "insufficient labeled data: "
            f"{report.fitted.pooled_pairs} train pairs (need {min_train_pairs}), "
            f"{static_eval.pair_count} holdout pairs (need {min_eval_pairs})"
        )
    if not beats_static:
        reasons.append(
            "fitted parameters do not beat the static constants on the holdout: "
            f"fitted {report.fitted_eval.accuracy:.4f} vs static {static_eval.accuracy:.4f}"
        )
    if not guard_ok:
        reasons.append(
            "judge-independent guard regression: "
            f"fitted {guard_fitted:.4f} vs static {guard_static:.4f}"
        )
    ship = sufficient_data and beats_static and guard_ok
    if ship:
        reasons.append(
            "fitted parameters beat the static constants on the holdout and "
            "pass the judge-independent guard"
        )

    return GateDecision(
        label_source=label_source,
        weighting_mode=weighting_mode,
        split_version=split_version,
        report=report,
        static_params=static,
        static_eval=static_eval,
        guard_static=guard_static,
        guard_fitted=guard_fitted,
        sufficient_data=sufficient_data,
        beats_static=beats_static,
        guard_ok=guard_ok,
        ship=ship,
        reasons=tuple(reasons),
    )


def run_ship_gate_from_store(
    store: RecallSignalStore,
    *,
    label_source: str = "digest",
    project_id: str | None = None,
    limit: int = 5000,
    grid: Sequence[ReplayParams] | None = None,
    min_train_pairs: int = MIN_TRAIN_PAIRS,
    min_eval_pairs: int = MIN_EVAL_PAIRS,
) -> GateDecision:
    """Load one label stream through the replay join and run the ship gate."""
    rows = [
        replay_row_from_signal_row(row)
        for row in store.fetch_replay_rows(
            label_source=label_source, project_id=project_id, limit=limit
        )
    ]
    return run_ship_gate(
        rows,
        label_source=label_source,
        grid=grid,
        min_train_pairs=min_train_pairs,
        min_eval_pairs=min_eval_pairs,
    )
