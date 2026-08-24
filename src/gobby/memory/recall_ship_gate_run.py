"""Ship-gate execution: bind one frozen cohort, reserve holdout, decide.

Split out of :mod:`gobby.memory.recall_refit` (#20776), which keeps the replay
grid, the judge-independent guard battery, and decision serialization. This
module is the execution path those pieces feed: it binds every cohort fence
into a :class:`~gobby.memory.recall_ship_gate.GateCohort`, reconstructs the
prompt-hash-bound human audit, reserves holdout request IDs before loading
their features, and applies the data, static, and guard gates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from gobby.memory.recall_fit import (
    REQUEST_SPLIT_VERSION,
    ReplayParams,
    ReplayRow,
    WeightingMode,
    estimate_position_propensities,
    evaluate_pairwise,
    replay_row_from_signal_row,
    split_request_ids_per_project,
    split_requests_per_project,
)
from gobby.memory.recall_fit_shrinkage import fit_and_evaluate_partitioned
from gobby.memory.recall_refit import (
    MIN_EVAL_MIXED_REQUESTS,
    MIN_EVAL_PAIRS,
    MIN_TRAIN_MIXED_REQUESTS,
    MIN_TRAIN_PAIRS,
    GateDecision,
    default_candidate_scope,
    guard_accuracy,
    refit_grid,
    static_replay_params,
)
from gobby.memory.recall_ship_gate import (
    AUDIT_SAMPLE_REQUESTS,
    GateCohort,
    build_ship_audit_sample,
)
from gobby.memory.recall_ship_gate import (
    canonical_digest as _canonical_digest,
)
from gobby.memory.recall_ship_gate import (
    evaluate_ship_audit as _evaluate_ship_audit,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence
    from datetime import datetime

    from gobby.storage.recall_signals import RecallSignalStore

_SHIP_COHORT_REQUEST_LIMIT = 1_000_000


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
    query_construction_version: str | None,
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
    resolved_weighting_mode: WeightingMode = weighting_mode or (
        "full" if candidate_scope == "full" else "injected"
    )
    if resolved_weighting_mode != candidate_scope:
        raise ValueError("weighting_mode must match candidate_scope")
    cohort = GateCohort(
        label_source=label_source,
        candidate_scope=candidate_scope,
        judge_protocol_version=judge_protocol_version,
        query_construction_version=query_construction_version,
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
    query_construction_version: str | None,
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
    resolved_weighting_mode: WeightingMode = weighting_mode or (
        "full" if scope == "full" else "injected"
    )
    if resolved_weighting_mode != scope:
        raise ValueError("weighting_mode must match candidate_scope")
    cohort = GateCohort(
        label_source=label_source,
        candidate_scope=scope,
        judge_protocol_version=judge_protocol_version,
        query_construction_version=query_construction_version,
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
        query_construction_version=query_construction_version,
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
        query_construction_version=query_construction_version,
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
        (target.request_id, target.memory_id): target.prompt_hash for target in audit_sample.targets
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
        "query_construction_version": query_construction_version,
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
        query_construction_version=query_construction_version,
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
