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
4. ``run_ship_gate()`` / ``run_ship_gate_from_store()`` — bind every cohort
fence, reconstruct the prompt-hash-bound human audit, reserve holdout request
IDs before loading their features, and apply data, static, and guard gates.

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
    REQUEST_SPLIT_VERSION,
    FittedParams,
    LabeledFitReport,
    PairwiseEvalResult,
    ReplayParams,
    ReplayRow,
    WeightingMode,
    estimate_position_propensities,
    evaluate_pairwise,
    fit_and_evaluate_partitioned,
    replay_row_from_signal_row,
    split_request_ids_per_project,
    split_requests_per_project,
)
from gobby.memory.recall_ship_gate import (
    AUDIT_MIN_AGREEMENT,
    AUDIT_MIN_WILSON_LOWER_BOUND,
    AUDIT_SAMPLE_REQUESTS,
    DECISION_SCHEMA_VERSION,
    GateCohort,
    ShipAuditResult,
    build_ship_audit_sample,
)
from gobby.memory.recall_ship_gate import (
    canonical_digest as _canonical_digest,
)
from gobby.memory.recall_ship_gate import (
    evaluate_ship_audit as _evaluate_ship_audit,
)
from gobby.memory.services._search_constants import _GRAPH_SYNTHETIC_SIM_DISCOUNT
from gobby.memory.services.knowledge_graph.writer import COOCCUR_ALPHA, COOCCUR_SUPPORT_CAP

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from gobby.storage.recall_signals import RecallSignalStore

# Below these floors a grid argmax is noise, not signal: with one preference
# pair per labeled request, 50 train pairs ≈ 50 requests — roughly the point
# where a 19-point grid stops overfitting coin flips — and 20 holdout pairs
# bound the comparison's standard error under ~0.11. Data-starved fits are
# rejected with an explicit reason instead of shipping on vapor.
MIN_TRAIN_PAIRS = 50
MIN_EVAL_PAIRS = 20
MIN_TRAIN_MIXED_REQUESTS = 20
MIN_EVAL_MIXED_REQUESTS = 10
_SHIP_COHORT_REQUEST_LIMIT = 1_000_000


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


def _fit_settings_digest(
    grid: Sequence[ReplayParams],
    *,
    eval_stride: int,
    smoothing: float,
    clip: float,
    shrinkage_requests: float,
    shrinkage_selection_method: str,
    min_train_pairs: int,
    min_eval_pairs: int,
    min_train_mixed_requests: int,
    min_eval_mixed_requests: int,
) -> str:
    return _canonical_digest(
        {
            "grid": [
                {
                    "half_life_days": point.half_life_days,
                    "graph_synthetic_discount": point.graph_synthetic_discount,
                    "cooccur_alpha": point.cooccur_alpha,
                    "cooccur_support_cap": point.cooccur_support_cap,
                }
                for point in grid
            ],
            "eval_stride": eval_stride,
            "smoothing": smoothing,
            "clip": clip,
            "shrinkage_requests": shrinkage_requests,
            "shrinkage_selection_method": shrinkage_selection_method,
            "min_train_pairs": min_train_pairs,
            "min_eval_pairs": min_eval_pairs,
            "min_train_mixed_requests": min_train_mixed_requests,
            "min_eval_mixed_requests": min_eval_mixed_requests,
        }
    )


def _holdout_consumption_key(cohort: GateCohort) -> str:
    return _canonical_digest({"purpose": "recall-ship-holdout", "cohort": cohort.identity()})


def run_ship_gate(
    rows: Sequence[ReplayRow],
    *,
    label_source: str,
    candidate_scope: str,
    judge_protocol_version: str,
    weighting_regime_key: str,
    judge_model_key: str,
    judge_config_fingerprint: str,
    data_cutoff: datetime,
    completion_cutoff: datetime,
    audit_rows: Sequence[Mapping[str, Any]],
    audit_verdicts: Sequence[Mapping[str, Any]],
    project_id: str | None = None,
    grid: Sequence[ReplayParams] | None = None,
    eval_stride: int = 2,
    smoothing: float = 1.0,
    clip: float = 10.0,
    shrinkage_requests: float = 50.0,
    shrinkage_selection_method: str = "fixed-before-holdout",
    weighting_mode: WeightingMode | None = None,
    split_version: str = REQUEST_SPLIT_VERSION,
    min_train_pairs: int = MIN_TRAIN_PAIRS,
    min_eval_pairs: int = MIN_EVAL_PAIRS,
    min_train_mixed_requests: int = MIN_TRAIN_MIXED_REQUESTS,
    min_eval_mixed_requests: int = MIN_EVAL_MIXED_REQUESTS,
    holdout_request_ids: Collection[str] | None = None,
    holdout_status: str = "provided",
) -> GateDecision:
    """Fit one frozen cohort and apply data, audit, static, and guard gates."""
    resolved_weighting_mode: WeightingMode = (
        weighting_mode or ("full" if candidate_scope == "full" else "injected")
    )
    if resolved_weighting_mode != candidate_scope:
        raise ValueError("weighting_mode must match candidate_scope")
    cohort = GateCohort(
        label_source=label_source,
        candidate_scope=candidate_scope,
        judge_protocol_version=judge_protocol_version,
        weighting_regime_key=weighting_regime_key,
        judge_model_key=judge_model_key,
        judge_config_fingerprint=judge_config_fingerprint,
        data_cutoff=data_cutoff,
        completion_cutoff=completion_cutoff,
        project_id=project_id,
        weighting_mode=resolved_weighting_mode,
        split_version=split_version,
    )
    fit_grid = list(grid) if grid is not None else refit_grid()
    if holdout_request_ids is None:
        train, evaluation = split_requests_per_project(
            rows,
            eval_stride=eval_stride,
            split_version=split_version,
        )
    else:
        holdout_ids = set(holdout_request_ids)
        train = [row for row in rows if row.recall_request_id not in holdout_ids]
        evaluation = [row for row in rows if row.recall_request_id in holdout_ids]
    report = fit_and_evaluate_partitioned(
        train,
        evaluation,
        fit_grid,
        smoothing=smoothing,
        clip=clip,
        shrinkage_requests=shrinkage_requests,
        weighting_mode=resolved_weighting_mode,
    )

    propensities = estimate_position_propensities(train, smoothing=smoothing)
    static = static_replay_params()
    static_eval = evaluate_pairwise(
        evaluation,
        {},
        propensities,
        default_params=static,
        clip=clip,
        weighting_mode=resolved_weighting_mode,
    )

    audit_sample = build_ship_audit_sample(
        audit_rows,
        cohort=cohort,
        train_request_ids={row.recall_request_id for row in train},
    )
    audit = _evaluate_ship_audit(audit_sample, audit_verdicts)

    guard_static = guard_accuracy(static)
    guard_fitted = guard_accuracy(report.fitted.pooled)

    sufficient_data = (
        report.fitted.pooled_pairs >= min_train_pairs
        and static_eval.pair_count >= min_eval_pairs
        and report.fitted.pooled_mixed_requests >= min_train_mixed_requests
        and static_eval.mixed_request_count >= min_eval_mixed_requests
    )
    beats_static = report.fitted_eval.accuracy > static_eval.accuracy
    guard_ok = guard_fitted >= guard_static
    audit_ok = audit.ok

    reasons: list[str] = []
    if not sufficient_data:
        reasons.append(
            "insufficient labeled data: "
            f"{report.fitted.pooled_pairs} train pairs (need {min_train_pairs}), "
            f"{static_eval.pair_count} holdout pairs (need {min_eval_pairs}), "
            f"{report.fitted.pooled_mixed_requests} train mixed requests "
            f"(need {min_train_mixed_requests}), "
            f"{static_eval.mixed_request_count} holdout mixed requests "
            f"(need {min_eval_mixed_requests})"
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
    if not audit_ok:
        reasons.append(f"ship audit failed closed: {audit.status}")
    ship = sufficient_data and beats_static and guard_ok and audit_ok
    if ship:
        reasons.append(
            "fitted parameters beat the static constants and clear data, "
            "human-audit, and guard gates"
        )

    fit_settings_digest = _fit_settings_digest(
        fit_grid,
        eval_stride=eval_stride,
        smoothing=smoothing,
        clip=clip,
        shrinkage_requests=shrinkage_requests,
        shrinkage_selection_method=shrinkage_selection_method,
        min_train_pairs=min_train_pairs,
        min_eval_pairs=min_eval_pairs,
        min_train_mixed_requests=min_train_mixed_requests,
        min_eval_mixed_requests=min_eval_mixed_requests,
    )
    holdout_consumption_key = _holdout_consumption_key(cohort)

    return GateDecision(
        cohort=cohort,
        report=report,
        static_params=static,
        static_eval=static_eval,
        audit=audit,
        shrinkage_requests=shrinkage_requests,
        shrinkage_selection_method=shrinkage_selection_method,
        min_train_pairs=min_train_pairs,
        min_eval_pairs=min_eval_pairs,
        min_train_mixed_requests=min_train_mixed_requests,
        min_eval_mixed_requests=min_eval_mixed_requests,
        guard_static=guard_static,
        guard_fitted=guard_fitted,
        sufficient_data=sufficient_data,
        beats_static=beats_static,
        guard_ok=guard_ok,
        audit_ok=audit_ok,
        holdout_consumption_key=holdout_consumption_key,
        fit_settings_digest=fit_settings_digest,
        holdout_status=holdout_status,
        ship=ship,
        reasons=tuple(reasons),
    )


def run_ship_gate_from_store(
    store: RecallSignalStore,
    *,
    label_source: str,
    judge_protocol_version: str,
    weighting_regime_key: str,
    judge_model_key: str,
    judge_config_fingerprint: str,
    data_cutoff: datetime,
    completion_cutoff: datetime,
    candidate_scope: str | None = None,
    project_id: str | None = None,
    grid: Sequence[ReplayParams] | None = None,
    eval_stride: int = 2,
    smoothing: float = 1.0,
    clip: float = 10.0,
    shrinkage_requests: float = 50.0,
    shrinkage_selection_method: str = "fixed-before-holdout",
    weighting_mode: WeightingMode | None = None,
    split_version: str = REQUEST_SPLIT_VERSION,
    min_train_pairs: int = MIN_TRAIN_PAIRS,
    min_eval_pairs: int = MIN_EVAL_PAIRS,
    min_train_mixed_requests: int = MIN_TRAIN_MIXED_REQUESTS,
    min_eval_mixed_requests: int = MIN_EVAL_MIXED_REQUESTS,
) -> GateDecision:
    """Audit training rows, reserve holdout IDs, then read and evaluate holdout rows."""
    scope = candidate_scope or default_candidate_scope(label_source)
    resolved_weighting_mode: WeightingMode = (
        weighting_mode or ("full" if scope == "full" else "injected")
    )
    if resolved_weighting_mode != scope:
        raise ValueError("weighting_mode must match candidate_scope")
    cohort = GateCohort(
        label_source=label_source,
        candidate_scope=scope,
        judge_protocol_version=judge_protocol_version,
        weighting_regime_key=weighting_regime_key,
        judge_model_key=judge_model_key,
        judge_config_fingerprint=judge_config_fingerprint,
        data_cutoff=data_cutoff,
        completion_cutoff=completion_cutoff,
        project_id=project_id,
        weighting_mode=resolved_weighting_mode,
        split_version=split_version,
    )
    fit_grid = list(grid) if grid is not None else refit_grid()
    cohort_rows = store.shadow_cohort_query(
        "fitting",
        label_source=label_source,
        judge_protocol_version=judge_protocol_version,
        project_id=project_id,
        judge_model_key=judge_model_key,
        judge_config_fingerprint=judge_config_fingerprint,
        weighting_regime_key=weighting_regime_key,
        data_cutoff=data_cutoff,
        completion_cutoff=completion_cutoff,
        limit=_SHIP_COHORT_REQUEST_LIMIT,
    )
    request_projects = [
        (
            str(row["project_id"]) if row.get("project_id") is not None else None,
            str(row["recall_request_id"]),
        )
        for row in cohort_rows
    ]
    train_request_ids, holdout_partition_ids = split_request_ids_per_project(
        request_projects,
        eval_stride=eval_stride,
        split_version=split_version,
    )

    training_signal_rows = store.fetch_shadow_replay_rows(
        label_source=label_source,
        candidate_scope=scope,
        judge_protocol_version=judge_protocol_version,
        weighting_regime_key=weighting_regime_key,
        judge_model_key=judge_model_key,
        judge_config_fingerprint=judge_config_fingerprint,
        data_cutoff=data_cutoff,
        completion_cutoff=completion_cutoff,
        project_id=project_id,
        limit=_SHIP_COHORT_REQUEST_LIMIT,
        request_ids=sorted(train_request_ids),
    )
    training_rows = [replay_row_from_signal_row(row) for row in training_signal_rows]
    audit_sample = build_ship_audit_sample(
        training_signal_rows,
        cohort=cohort,
        train_request_ids=train_request_ids,
    )
    expected_hashes = {
        (target.request_id, target.memory_id): target.prompt_hash
        for target in audit_sample.targets
    }
    audit_verdicts: list[dict[str, Any]] = []
    if len(audit_sample.targets) == AUDIT_SAMPLE_REQUESTS:
        try:
            audit_verdicts = store.fetch_audit_verdicts(
                audit_sample.cohort_digest,
                audit_sample.sample_digest,
                expected_prompt_hashes=expected_hashes,
            )
        except ValueError:
            first = audit_sample.targets[0]
            audit_verdicts = [
                {
                    "request_id": first.request_id,
                    "memory_id": first.memory_id,
                    "prompt_hash": "",
                    "human_verdict": first.judge_useful,
                }
            ]
    audit = _evaluate_ship_audit(audit_sample, audit_verdicts)

    common_args: dict[str, Any] = {
        "label_source": label_source,
        "candidate_scope": scope,
        "judge_protocol_version": judge_protocol_version,
        "weighting_regime_key": weighting_regime_key,
        "judge_model_key": judge_model_key,
        "judge_config_fingerprint": judge_config_fingerprint,
        "data_cutoff": data_cutoff,
        "completion_cutoff": completion_cutoff,
        "audit_rows": training_signal_rows,
        "audit_verdicts": audit_verdicts,
        "project_id": project_id,
        "grid": fit_grid,
        "eval_stride": eval_stride,
        "smoothing": smoothing,
        "clip": clip,
        "shrinkage_requests": shrinkage_requests,
        "shrinkage_selection_method": shrinkage_selection_method,
        "weighting_mode": resolved_weighting_mode,
        "split_version": split_version,
        "min_train_pairs": min_train_pairs,
        "min_eval_pairs": min_eval_pairs,
        "min_train_mixed_requests": min_train_mixed_requests,
        "min_eval_mixed_requests": min_eval_mixed_requests,
    }
    if not audit.ok:
        return run_ship_gate(
            training_rows,
            **common_args,
            holdout_request_ids=(),
            holdout_status="not_reserved",
        )

    fit_settings_digest = _fit_settings_digest(
        fit_grid,
        eval_stride=eval_stride,
        smoothing=smoothing,
        clip=clip,
        shrinkage_requests=shrinkage_requests,
        shrinkage_selection_method=shrinkage_selection_method,
        min_train_pairs=min_train_pairs,
        min_eval_pairs=min_eval_pairs,
        min_train_mixed_requests=min_train_mixed_requests,
        min_eval_mixed_requests=min_eval_mixed_requests,
    )
    holdout_consumption_key = _holdout_consumption_key(cohort)
    reservation = store.reserve_gate_holdout(
        holdout_consumption_key=holdout_consumption_key,
        fit_settings_digest=fit_settings_digest,
        holdout_partition_ids=sorted(holdout_partition_ids),
        min_requests=min_eval_mixed_requests,
    )
    if reservation.status == "complete":
        if reservation.decision is None:
            raise RuntimeError("completed gate reservation has no decision")
        return GateDecision.from_record(reservation.decision)
    if reservation.status == "in_progress":
        raise RuntimeError("gate holdout reservation is already in progress")
    if reservation.status == "insufficient":
        return run_ship_gate(
            training_rows,
            **common_args,
            holdout_request_ids=(),
            holdout_status="insufficient",
        )
    if reservation.claim_token is None:
        raise RuntimeError("reserved gate holdout has no claim token")

    holdout_signal_rows = store.fetch_shadow_replay_rows(
        label_source=label_source,
        candidate_scope=scope,
        judge_protocol_version=judge_protocol_version,
        weighting_regime_key=weighting_regime_key,
        judge_model_key=judge_model_key,
        judge_config_fingerprint=judge_config_fingerprint,
        data_cutoff=data_cutoff,
        completion_cutoff=completion_cutoff,
        project_id=project_id,
        limit=_SHIP_COHORT_REQUEST_LIMIT,
        request_ids=reservation.request_ids,
    )
    holdout_rows = [replay_row_from_signal_row(row) for row in holdout_signal_rows]
    decision = run_ship_gate(
        [*training_rows, *holdout_rows],
        **common_args,
        holdout_request_ids=reservation.request_ids,
        holdout_status="reserved",
    )
    store.complete_gate_run(
        holdout_consumption_key,
        reservation.claim_token,
        ship=decision.ship,
        decision=decision.to_record(),
    )
    return decision
