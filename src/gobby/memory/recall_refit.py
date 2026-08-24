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
4. ``GateDecision`` — the serialized, digest-bound record of one gate run.

The gate execution path that consumes all four — ``run_ship_gate()`` and
``run_ship_gate_from_store()`` — lives in
:mod:`gobby.memory.recall_ship_gate_run`.

Clustering parameters (``min_cluster_size``, ``min_samples``) are NOT swept
here: they change retrieval candidate sets, which logged-hit replay cannot
express (#17197). They are swept on the live synthetic-corpus arms in
``tests/memory/test_recall_benchmark.py``, which is judge-independent by
construction (planted corpus, no labels).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from gobby.config.persistence import MemoryConfig
from gobby.memory.recall_fit import (
    PairwiseEvalResult,
    ReplayParams,
    ReplayRow,
    WeightingMode,
    evaluate_pairwise,
)
from gobby.memory.recall_fit_shrinkage import FittedParams, LabeledFitReport
from gobby.memory.recall_ship_gate import (
    AUDIT_MIN_AGREEMENT,
    AUDIT_MIN_WILSON_LOWER_BOUND,
    DECISION_SCHEMA_VERSION,
    GateCohort,
    ShipAuditResult,
)
from gobby.memory.recall_ship_gate import (
    canonical_digest as _canonical_digest,
)
from gobby.memory.services._search_constants import _GRAPH_SYNTHETIC_SIM_DISCOUNT
from gobby.memory.services.knowledge_graph.writer import COOCCUR_ALPHA, COOCCUR_SUPPORT_CAP

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


# Below these floors a grid argmax is noise, not signal: with one preference
# pair per labeled request, 50 train pairs ≈ 50 requests — roughly the point
# where a 19-point grid stops overfitting coin flips — and 20 holdout pairs
# bound the comparison's standard error under ~0.11. Data-starved fits are
# rejected with an explicit reason instead of shipping on vapor.
MIN_TRAIN_PAIRS = 50
MIN_EVAL_PAIRS = 20
MIN_TRAIN_MIXED_REQUESTS = 20
MIN_EVAL_MIXED_REQUESTS = 10


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
    """One immutable fit, audit, and holdout gate outcome."""

    cohort: GateCohort
    report: LabeledFitReport
    static_params: ReplayParams
    static_eval: PairwiseEvalResult
    audit: ShipAuditResult
    shrinkage_requests: float
    shrinkage_selection_method: str
    min_train_pairs: int
    min_eval_pairs: int
    min_train_mixed_requests: int
    min_eval_mixed_requests: int
    guard_static: float
    guard_fitted: float
    sufficient_data: bool
    beats_static: bool
    guard_ok: bool
    audit_ok: bool
    holdout_consumption_key: str
    fit_settings_digest: str
    holdout_status: str
    ship: bool
    reasons: tuple[str, ...]

    @property
    def label_source(self) -> str:
        return self.cohort.label_source

    @property
    def weighting_mode(self) -> WeightingMode:
        return self.cohort.weighting_mode

    @property
    def split_version(self) -> str:
        return self.cohort.split_version

    @classmethod
    def from_record(cls, value: Mapping[str, Any]) -> GateDecision:
        """Rehydrate a completed persisted gate decision without reading holdout rows."""

        def params(record: object) -> ReplayParams:
            item = cast("Mapping[str, Any]", record)
            support_cap = item.get("cooccur_support_cap")
            return ReplayParams(
                half_life_days=item.get("half_life_days"),
                graph_synthetic_discount=item.get("graph_synthetic_discount"),
                cooccur_alpha=item.get("cooccur_alpha"),
                cooccur_support_cap=int(support_cap) if support_cap is not None else None,
            )

        def evaluation(record: object) -> PairwiseEvalResult:
            item = cast("Mapping[str, Any]", record)
            per_project = cast("Mapping[str, Any]", item.get("per_project") or {})
            return PairwiseEvalResult(
                pair_count=int(item["pair_count"]),
                mixed_request_count=int(item["mixed_request_count"]),
                weighted_pair_count=float(item["weighted_pair_count"]),
                accuracy=float(item["accuracy"]),
                per_project={str(key): float(score) for key, score in per_project.items()},
            )

        cohort_record = cast("Mapping[str, Any]", value["cohort_identity"])
        cohort = GateCohort(
            label_source=str(cohort_record["label_source"]),
            candidate_scope=str(cohort_record["candidate_scope"]),
            judge_protocol_version=str(cohort_record["judge_protocol_version"]),
            # Absent or null is the legacy era, which is a real cohort value.
            query_construction_version=(
                str(cohort_record["query_construction_version"])
                if cohort_record.get("query_construction_version") is not None
                else None
            ),
            weighting_regime_key=str(cohort_record["weighting_regime_key"]),
            judge_model_key=str(cohort_record["judge_model_key"]),
            judge_config_fingerprint=str(cohort_record["judge_config_fingerprint"]),
            data_cutoff=datetime.fromisoformat(str(cohort_record["data_cutoff"])),
            completion_cutoff=datetime.fromisoformat(str(cohort_record["completion_cutoff"])),
            project_id=(
                str(cohort_record["project_id"])
                if cohort_record.get("project_id") is not None
                else None
            ),
            weighting_mode=cast("WeightingMode", cohort_record["weighting_mode"]),
            split_version=str(cohort_record["split_version"]),
        )
        fitted_params = params(value["fitted_params"])
        train_pairs = int(value["train_pairs"])
        train_mixed_requests = int(value["train_mixed_requests"])
        report = LabeledFitReport(
            rows_total=int(value["rows_total"]),
            rows_labeled=int(value["rows_labeled"]),
            train_requests=int(value["train_requests"]),
            eval_requests=int(value["eval_requests"]),
            fitted=FittedParams(
                pooled=fitted_params,
                per_project={},
                pooled_pairs=train_pairs,
                project_pairs={},
                pooled_mixed_requests=train_mixed_requests,
                project_mixed_requests={},
            ),
            baseline_eval=evaluation(value["logged_baseline_eval"]),
            fitted_eval=evaluation(value["fitted_eval"]),
        )
        audit_record = cast("Mapping[str, Any]", value["audit"])
        audit = ShipAuditResult(
            status=str(audit_record["status"]),
            cohort_digest=str(value["cohort_digest"]),
            sample_digest=str(audit_record["sample_digest"]),
            unit_count=int(audit_record["unit_count"]),
            agreement=(
                float(audit_record["agreement"])
                if audit_record.get("agreement") is not None
                else None
            ),
            wilson_lower_bound=(
                float(audit_record["wilson_lower_bound"])
                if audit_record.get("wilson_lower_bound") is not None
                else None
            ),
        )
        floors = cast("Mapping[str, Any]", value["data_floors"])
        shrinkage = cast("Mapping[str, Any]", value["shrinkage"])
        gates = cast("Mapping[str, Any]", value["gates"])
        return cls(
            cohort=cohort,
            report=report,
            static_params=params(value["static_params"]),
            static_eval=evaluation(value["static_eval"]),
            audit=audit,
            shrinkage_requests=float(shrinkage["requests"]),
            shrinkage_selection_method=str(shrinkage["selection_method"]),
            min_train_pairs=int(floors["train_pairs"]),
            min_eval_pairs=int(floors["eval_pairs"]),
            min_train_mixed_requests=int(floors["train_mixed_requests"]),
            min_eval_mixed_requests=int(floors["eval_mixed_requests"]),
            guard_static=float(value["guard_static"]),
            guard_fitted=float(value["guard_fitted"]),
            sufficient_data=bool(gates["sufficient_data"]),
            beats_static=bool(gates["beats_static"]),
            guard_ok=bool(gates["guard_ok"]),
            audit_ok=bool(gates["audit_ok"]),
            holdout_consumption_key=str(value["holdout_consumption_key"]),
            fit_settings_digest=str(value["fit_settings_digest"]),
            holdout_status=str(value["holdout_status"]),
            ship=bool(value["ship"]),
            reasons=tuple(str(reason) for reason in cast("Sequence[Any]", value["reasons"])),
        )

    def to_record(self) -> dict[str, Any]:
        """Return canonical decision payload with a self-excluding digest."""

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

        record: dict[str, Any] = {
            "task": "#18426",
            "decision_schema_version": DECISION_SCHEMA_VERSION,
            "label_source": self.label_source,
            "cohort_identity": self.cohort.identity(),
            "cohort_digest": self.audit.cohort_digest,
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
            "shrinkage": {
                "requests": self.shrinkage_requests,
                "selection_method": self.shrinkage_selection_method,
            },
            "data_floors": {
                "train_pairs": self.min_train_pairs,
                "eval_pairs": self.min_eval_pairs,
                "train_mixed_requests": self.min_train_mixed_requests,
                "eval_mixed_requests": self.min_eval_mixed_requests,
            },
            "audit": {
                "status": self.audit.status,
                "sample_digest": self.audit.sample_digest,
                "unit_count": self.audit.unit_count,
                "agreement": self.audit.agreement,
                "wilson_lower_bound": self.audit.wilson_lower_bound,
                "minimum_agreement": AUDIT_MIN_AGREEMENT,
                "minimum_wilson_lower_bound": AUDIT_MIN_WILSON_LOWER_BOUND,
            },
            "holdout_consumption_key": self.holdout_consumption_key,
            "fit_settings_digest": self.fit_settings_digest,
            "holdout_status": self.holdout_status,
            "guard_static": self.guard_static,
            "guard_fitted": self.guard_fitted,
            "gates": {
                "sufficient_data": self.sufficient_data,
                "beats_static": self.beats_static,
                "guard_ok": self.guard_ok,
                "audit_ok": self.audit_ok,
            },
            "ship": self.ship,
            "reasons": list(self.reasons),
        }
        record["decision_digest"] = _canonical_digest(record)
        return record


def default_candidate_scope(label_source: str) -> str:
    """Resolve the fit projection for one label stream."""
    return "full" if label_source == "digest_shadow" else "injected"
