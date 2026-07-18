"""Unit tests for the #17198 refit ship gate (gobby.memory.recall_refit).

No DB: rows are constructed ReplayRows. The planted datasets encode known
inversions so the expected grid winner — and therefore each gate outcome —
is arithmetically determined, not asserted by faith.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from gobby.memory.recall_fit import ReplayParams, ReplayRow
from gobby.memory.recall_refit import (
    MIN_EVAL_PAIRS,
    MIN_TRAIN_PAIRS,
    GateDecision,
    guard_accuracy,
    judge_independent_guard_rows,
    refit_grid,
    run_ship_gate,
    static_replay_params,
)
from gobby.memory.services._search_constants import _GRAPH_SYNTHETIC_SIM_DISCOUNT
from gobby.memory.services.knowledge_graph.writer import COOCCUR_ALPHA, COOCCUR_SUPPORT_CAP

pytestmark = pytest.mark.unit


def _semantic(
    request_id: str,
    memory_id: str,
    *,
    raw: float,
    decay: float,
    useful: bool,
    position: int,
) -> ReplayRow:
    return ReplayRow(
        recall_request_id=request_id,
        memory_id=memory_id,
        project_id="proj-a",
        rank=position,
        similarity=raw * decay,
        raw_semantic_score=raw,
        temporal_decay_factor=decay,
        ranking_score=0.0,
        ranking_mode="semantic_only",
        graph_score=None,
        edge_cosine=None,
        edge_support_norm=None,
        edge_weight_blend=None,
        injection_position=position,
        injection_group="context",
        judge_useful=useful,
        label_source="digest",
        logged_half_life_days=30.0,
        logged_graph_discount=None,
    )


def _planted_requests(
    count: int,
    *,
    rel_raw: float,
    rel_decay: float,
    irr_raw: float,
    irr_decay: float,
) -> list[ReplayRow]:
    """``count`` requests, each one labeled (useful, not-useful) pair."""
    rows: list[ReplayRow] = []
    for i in range(count):
        request_id = f"req-{i:02d}"
        rows.append(
            _semantic(request_id, "mem-rel", raw=rel_raw, decay=rel_decay, useful=True, position=0)
        )
        rows.append(
            _semantic(request_id, "mem-irr", raw=irr_raw, decay=irr_decay, useful=False, position=1)
        )
    return rows


class TestStaticAnchor:
    def test_static_params_match_production_constants(self) -> None:
        static = static_replay_params()
        assert static.half_life_days == 30.0
        assert static.graph_synthetic_discount == _GRAPH_SYNTHETIC_SIM_DISCOUNT
        assert static.cooccur_alpha == COOCCUR_ALPHA
        assert static.cooccur_support_cap == COOCCUR_SUPPORT_CAP


class TestRefitGrid:
    def test_grid_starts_with_logged_baseline_then_static(self) -> None:
        grid = refit_grid()
        assert grid[0] == ReplayParams()
        assert grid[1] == static_replay_params()

    def test_grid_covers_axes_without_duplicates(self) -> None:
        grid = refit_grid()
        static = static_replay_params()
        # 2 anchors + 4 half-life singles + 2 discount singles + (4*3 - 1) alpha×cap.
        assert len(grid) == 19
        assert len(set(grid)) == 19
        assert replace(static, half_life_days=60.0) in grid
        assert replace(static, graph_synthetic_discount=1.0) in grid
        assert replace(static, cooccur_alpha=0.75, cooccur_support_cap=8) in grid

    def test_non_baseline_points_are_fully_concrete(self) -> None:
        for params in refit_grid()[1:]:
            assert params.half_life_days is not None
            assert params.graph_synthetic_discount is not None
            assert params.cooccur_alpha is not None
            assert params.cooccur_support_cap is not None


class TestGuardBattery:
    def test_static_and_logged_baseline_score_one(self) -> None:
        assert guard_accuracy(static_replay_params()) == 1.0
        assert guard_accuracy(ReplayParams()) == 1.0

    def test_grid_envelope_is_exactly_the_documented_one(self) -> None:
        """h=7, alpha=0.25, and alpha=1.0 each violate one encoded prior."""
        for params in refit_grid():
            outside = params.half_life_days == 7.0 or params.cooccur_alpha in (0.25, 1.0)
            accuracy = guard_accuracy(params)
            if outside:
                assert accuracy < 1.0, params
            else:
                assert accuracy == 1.0, params

    def test_degenerate_parameters_fail_the_battery(self) -> None:
        static = static_replay_params()
        assert guard_accuracy(replace(static, half_life_days=1.0)) < 1.0
        assert guard_accuracy(replace(static, graph_synthetic_discount=0.1)) < 1.0

    def test_guard_rows_carry_no_judge_labels(self) -> None:
        for row in judge_independent_guard_rows():
            assert row.label_source == "constructed"
            assert row.injection_position is None  # unit IPS weight


class TestShipGate:
    def test_ship_path_fits_sixty_day_half_life(self) -> None:
        # Logged (h=30): useful .75*.6=.45 < not-useful .5*.95=.475 — inverted.
        # h=60 re-exponentiation fixes it (.581 vs .487); h=7/14 do not.
        rows = _planted_requests(12, rel_raw=0.75, rel_decay=0.6, irr_raw=0.5, irr_decay=0.95)
        decision = run_ship_gate(rows, label_source="digest", min_train_pairs=5, min_eval_pairs=3)
        assert decision.report.fitted.pooled == replace(static_replay_params(), half_life_days=60.0)
        assert decision.sufficient_data is True
        assert decision.beats_static is True
        assert decision.guard_ok is True
        assert decision.ship is True
        assert decision.static_eval.accuracy == 0.0
        assert decision.report.fitted_eval.accuracy == 1.0
        assert any("beat the static constants" in reason for reason in decision.reasons)

    def test_reject_insufficient_data_with_default_floors(self) -> None:
        rows = _planted_requests(12, rel_raw=0.75, rel_decay=0.6, irr_raw=0.5, irr_decay=0.95)
        decision = run_ship_gate(rows, label_source="digest")
        assert decision.report.fitted.pooled_pairs < MIN_TRAIN_PAIRS
        assert decision.static_eval.pair_count < MIN_EVAL_PAIRS
        assert decision.sufficient_data is False
        assert decision.ship is False
        assert any("insufficient labeled data" in reason for reason in decision.reasons)

    def test_reject_guard_regression_on_recency_hacked_labels(self) -> None:
        # Labels systematically favor recency: fresh mediocre useful, staler
        # stronger not-useful. The grid's best judge-label fit is h=7, which
        # violates the constructed stale-relevant prior (guard pair T2) — the
        # reward-hacking gate must reject even though the holdout improves.
        rows = _planted_requests(12, rel_raw=0.6, rel_decay=0.9, irr_raw=0.8, irr_decay=0.7)
        decision = run_ship_gate(rows, label_source="digest", min_train_pairs=5, min_eval_pairs=3)
        assert decision.report.fitted.pooled == replace(static_replay_params(), half_life_days=7.0)
        assert decision.beats_static is True
        assert decision.guard_fitted < decision.guard_static == 1.0
        assert decision.guard_ok is False
        assert decision.ship is False
        assert any("judge-independent guard regression" in r for r in decision.reasons)

    def test_reject_when_static_is_already_optimal(self) -> None:
        # Correctly ordered at the logged constants: every grid point ties at
        # 1.0, the first-strict-max rule keeps the logged baseline, and the
        # fitted arm cannot strictly beat static — no-change wins.
        rows = _planted_requests(12, rel_raw=0.9, rel_decay=0.9, irr_raw=0.3, irr_decay=0.9)
        decision = run_ship_gate(rows, label_source="digest", min_train_pairs=5, min_eval_pairs=3)
        assert decision.report.fitted.pooled == ReplayParams()
        assert decision.beats_static is False
        assert decision.guard_ok is True
        assert decision.ship is False
        assert any("do not beat the static constants" in r for r in decision.reasons)

    def test_static_eval_scores_the_same_holdout_as_fitted(self) -> None:
        rows = _planted_requests(12, rel_raw=0.75, rel_decay=0.6, irr_raw=0.5, irr_decay=0.95)
        decision = run_ship_gate(rows, label_source="digest", min_train_pairs=5, min_eval_pairs=3)
        assert decision.static_eval.pair_count == decision.report.fitted_eval.pair_count
        assert decision.static_eval.weighted_pair_count == pytest.approx(
            decision.report.fitted_eval.weighted_pair_count
        )

    def test_to_record_is_json_serializable_and_complete(self) -> None:
        rows = _planted_requests(12, rel_raw=0.75, rel_decay=0.6, irr_raw=0.5, irr_decay=0.95)
        decision = run_ship_gate(rows, label_source="digest", min_train_pairs=5, min_eval_pairs=3)
        record = decision.to_record()
        parsed = json.loads(json.dumps(record))
        assert parsed["task"] == "#17198"
        assert parsed["label_source"] == "digest"
        assert parsed["cohort_identity"] == {
            "label_source": "digest",
            "weighting_mode": "full",
            "split_version": "recall-request-hash-split-v1",
            "evaluator_version": "recall-request-normalized-pairwise-v1",
            "audit_sampler_version": "recall-training-request-sampler-v1",
        }
        assert parsed["ship"] is True
        assert parsed["fitted_params"]["half_life_days"] == 60.0
        assert parsed["static_params"]["half_life_days"] == 30.0
        assert parsed["gates"] == {
            "sufficient_data": True,
            "beats_static": True,
            "guard_ok": True,
        }
        assert parsed["fitted_eval"]["accuracy"] == 1.0
        assert parsed["train_pairs"] == parsed["train_mixed_requests"]
        assert parsed["fitted_eval"]["pair_count"] == parsed["fitted_eval"]["mixed_request_count"]
        assert parsed["static_eval"]["accuracy"] == 0.0
        assert parsed["reasons"]

    def test_empty_rows_reject_cleanly(self) -> None:
        decision = run_ship_gate([], label_source="digest")
        assert isinstance(decision, GateDecision)
        assert decision.ship is False
        assert decision.sufficient_data is False
        assert decision.report.rows_total == 0
