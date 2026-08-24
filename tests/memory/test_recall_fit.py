"""Unit tests for the offline labeled-data fit/replay harness (#17197).

Pure math and pipeline tests over synthetic ``ReplayRow`` fixtures — no
Postgres, no FalkorDB. The end-to-end path against real hub tables lives in
``tests/memory/test_recall_benchmark.py`` (labeled benchmark) and the loader
join in ``tests/storage/test_recall_signals.py``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from gobby.config.sessions import MemoryRecallConfig
from gobby.memory.recall import MAX_RECALL_MEMORIES, MemoryRecallRunner
from gobby.memory.recall_fit import (
    CandidateFilterParams,
    CandidateFilterReplayReport,
    ReplayParams,
    ReplayRow,
    _match_static_threshold,
    candidate_filter_score,
    candidate_replay_rows_from_signal_rows,
    estimate_position_propensities,
    evaluate_pairwise,
    ips_weight,
    replay_candidate_filter,
    replay_row_from_signal_row,
    replayed_similarity,
    replayed_sort_key,
    select_by_candidate_filter,
    select_by_static_constants,
    split_requests_per_project,
)
from gobby.memory.recall_fit_shrinkage import (
    SHRINKAGE_REQUEST_CANDIDATES,
    FittedParams,
    default_replay_grid,
    fit_and_evaluate,
    fit_partial_pooled,
    select_shrinkage_requests,
)
from gobby.memory.services._search_constants import _GRAPH_CONFIDENCE_SELECTION_FLOOR
from gobby.memory.services._search_results import build_results
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories import LocalMemoryManager, Memory, MemoryType

pytestmark = pytest.mark.unit


def _row(
    request_id: str = "req-1",
    memory_id: str = "mem-1",
    *,
    project_id: str | None = "proj-a",
    rank: int = 0,
    similarity: float | None = None,
    raw_semantic_score: float | None = None,
    temporal_decay_factor: float | None = None,
    ranking_score: float = 0.0,
    ranking_mode: str | None = None,
    graph_score: float | None = None,
    edge_cosine: float | None = None,
    edge_support_norm: float | None = None,
    edge_weight_blend: float | None = None,
    injection_position: int | None = 0,
    injection_group: str | None = "context",
    judge_useful: bool | None = None,
    label_source: str | None = None,
    logged_half_life_days: float | None = 30.0,
    logged_graph_discount: float | None = None,
) -> ReplayRow:
    return ReplayRow(
        recall_request_id=request_id,
        memory_id=memory_id,
        project_id=project_id,
        rank=rank,
        similarity=similarity,
        raw_semantic_score=raw_semantic_score,
        temporal_decay_factor=temporal_decay_factor,
        ranking_score=ranking_score,
        ranking_mode=ranking_mode,
        graph_score=graph_score,
        edge_cosine=edge_cosine,
        edge_support_norm=edge_support_norm,
        edge_weight_blend=edge_weight_blend,
        injection_position=injection_position,
        injection_group=injection_group,
        judge_useful=judge_useful,
        label_source=label_source,
        logged_half_life_days=logged_half_life_days,
        logged_graph_discount=logged_graph_discount,
    )


def _semantic_row(
    *,
    raw: float,
    decay: float,
    boost: float = 1.0,
    ranking_score: float = 0.0,
    **kwargs: object,
) -> ReplayRow:
    """A semantic hit whose logged similarity is raw * boost * decay."""
    return _row(
        similarity=raw * boost * decay,
        raw_semantic_score=raw,
        temporal_decay_factor=decay,
        ranking_mode="semantic_only",
        ranking_score=ranking_score,
        **kwargs,  # type: ignore[arg-type]
    )


class TestReplayAlgebra:
    def test_decay_reexponentiation_matches_direct_computation(self) -> None:
        # age 10d under h0=30 -> decay 0.5^(10/30); replay to h1=7 must equal
        # the directly computed 0.5^(10/7).
        age_days = 10.0
        decay_h30 = 0.5 ** (age_days / 30.0)
        row = _semantic_row(raw=0.8, decay=decay_h30)

        replayed = replayed_similarity(row, ReplayParams(half_life_days=7.0))

        assert replayed == pytest.approx(0.8 * 0.5 ** (age_days / 7.0))

    def test_semantic_replay_preserves_pre_decay_boost(self) -> None:
        # The 1.2 user-source boost is folded into logged similarity; replay
        # must carry it through unchanged.
        decay_h30 = 0.5 ** (20.0 / 30.0)
        row = _semantic_row(raw=0.5, decay=decay_h30, boost=1.2)

        replayed = replayed_similarity(row, ReplayParams(half_life_days=60.0))

        assert replayed == pytest.approx(0.5 * 1.2 * 0.5 ** (20.0 / 60.0))

    def test_replay_keeps_logged_similarity_when_logged_half_life_unknown(self) -> None:
        row = _semantic_row(raw=0.8, decay=0.7, logged_half_life_days=None)

        assert replayed_similarity(row, ReplayParams(half_life_days=7.0)) == row.similarity

    def test_no_params_replays_logged_similarity_exactly(self) -> None:
        row = _semantic_row(raw=0.8, decay=0.7)

        assert replayed_similarity(row, ReplayParams()) == row.similarity

    def test_graph_synthetic_discount_replay(self) -> None:
        row = _row(
            similarity=0.8 * 0.9 * 1.0,
            temporal_decay_factor=1.0,
            ranking_mode="graph_synthetic",
            graph_score=0.8,
            logged_graph_discount=0.9,
        )

        replayed = replayed_similarity(row, ReplayParams(graph_synthetic_discount=0.5))

        assert replayed == pytest.approx(0.8 * 0.5)

    def test_graph_synthetic_discount_recovered_when_not_logged(self) -> None:
        # sim = graph * 0.9 * decay with the discount missing from the row:
        # the replay recovers 0.9 algebraically, then re-exponentiates decay.
        decay_h30 = 0.5
        row = _row(
            similarity=0.8 * 0.9 * decay_h30,
            temporal_decay_factor=decay_h30,
            ranking_mode="graph_synthetic",
            graph_score=0.8,
            logged_graph_discount=None,
        )

        replayed = replayed_similarity(row, ReplayParams(half_life_days=15.0))

        assert replayed == pytest.approx(0.8 * 0.9 * 0.25)

    def test_alpha_reblend_rescales_graph_score(self) -> None:
        # Logged blend 0.7 = 0.5*0.8 + 0.5*0.6; alpha=1.0 -> new blend 0.8.
        row = _row(
            similarity=0.8 * 0.9 * 1.0,
            temporal_decay_factor=1.0,
            ranking_mode="graph_synthetic",
            graph_score=0.8,
            logged_graph_discount=0.9,
            edge_cosine=0.8,
            edge_support_norm=0.6,
            edge_weight_blend=0.7,
        )

        replayed = replayed_similarity(row, ReplayParams(cooccur_alpha=1.0))

        assert replayed == pytest.approx(0.8 * (0.8 / 0.7) * 0.9)

    def test_support_cap_reblend_recovers_raw_support(self) -> None:
        # support_norm 0.6 under logged cap 5 -> raw support 3; new cap 3
        # saturates -> support_norm 1.0 -> blend 0.5*0.8 + 0.5*1.0 = 0.9.
        row = _row(
            similarity=0.8 * 0.9 * 1.0,
            temporal_decay_factor=1.0,
            ranking_mode="graph_synthetic",
            graph_score=0.8,
            logged_graph_discount=0.9,
            edge_cosine=0.8,
            edge_support_norm=0.6,
            edge_weight_blend=0.7,
        )

        replayed = replayed_similarity(row, ReplayParams(cooccur_support_cap=3))

        assert replayed == pytest.approx(0.8 * (0.9 / 0.7) * 0.9)

    def test_sort_key_semantic_first_then_ranking_tiebreak(self) -> None:
        params = ReplayParams()
        with_sim = _semantic_row(raw=0.3, decay=1.0, ranking_score=0.1)
        no_sim = _row(ranking_score=0.9, ranking_mode="nonsemantic_fallback")
        tied_low = _semantic_row(raw=0.3, decay=1.0, ranking_score=0.05)

        assert replayed_sort_key(with_sim, params) > replayed_sort_key(no_sim, params)
        assert replayed_sort_key(with_sim, params) > replayed_sort_key(tied_low, params)


class TestPropensities:
    def test_label_coverage_with_smoothing(self) -> None:
        rows = [
            _row(
                "r1",
                "21000000-0000-4000-8000-000000000005",
                judge_useful=True,
                injection_position=0,
            ),
            _row("r2", "m2", judge_useful=None, injection_position=0),
        ]

        propensities = estimate_position_propensities(rows, smoothing=1.0)

        # (1 labeled + 1) / (2 injected + 2) = 0.5
        assert propensities[("context", 0)] == pytest.approx(0.5)

    def test_ips_weight_clips_and_falls_back_for_unseen_slots(self) -> None:
        rare = _row(
            "r1", "21000000-0000-4000-8000-000000000005", judge_useful=True, injection_position=7
        )
        unseen = _row("r2", "m2", judge_useful=True, injection_position=3)

        weight = ips_weight(rare, {("context", 7): 0.05}, clip=10.0)
        fallback = ips_weight(unseen, {("context", 7): 0.05}, clip=10.0)

        assert weight == 10.0  # 1/0.05 = 20, clipped
        assert fallback == 10.0  # unseen slot -> clip


class TestPairwiseObjective:
    def test_full_weighting_normalizes_each_mixed_request(self) -> None:
        rows = [
            *[
                _semantic_row(
                    raw=0.9,
                    decay=1.0,
                    request_id="wide",
                    memory_id=f"positive-{index}",
                    judge_useful=True,
                )
                for index in range(2)
            ],
            *[
                _semantic_row(
                    raw=0.1,
                    decay=1.0,
                    request_id="wide",
                    memory_id=f"negative-{index}",
                    judge_useful=False,
                )
                for index in range(3)
            ],
            _semantic_row(
                raw=0.9,
                decay=1.0,
                request_id="narrow",
                memory_id="positive",
                judge_useful=True,
            ),
            _semantic_row(
                raw=0.1,
                decay=1.0,
                request_id="narrow",
                memory_id="negative",
                judge_useful=False,
            ),
        ]

        result = evaluate_pairwise(
            rows,
            {},
            {},
            default_params=ReplayParams(),
            weighting_mode="full",
        )

        assert result.pair_count == 7
        assert result.mixed_request_count == 2
        assert result.weighted_pair_count == 2.0
        assert result.accuracy == 1.0

    def test_injected_weighting_preserves_relative_ips_within_request(self) -> None:
        rows = [
            _semantic_row(
                raw=0.9,
                decay=1.0,
                request_id="request",
                memory_id="rare-positive",
                injection_position=0,
                judge_useful=True,
            ),
            _semantic_row(
                raw=0.1,
                decay=1.0,
                request_id="request",
                memory_id="common-positive",
                injection_position=1,
                judge_useful=True,
            ),
            _semantic_row(
                raw=0.5,
                decay=1.0,
                request_id="request",
                memory_id="negative",
                injection_position=2,
                judge_useful=False,
            ),
        ]
        propensities = {("context", 0): 0.25, ("context", 1): 1.0}

        result = evaluate_pairwise(
            rows,
            {},
            propensities,
            default_params=ReplayParams(),
            weighting_mode="injected",
        )

        assert result.pair_count == 2
        assert result.mixed_request_count == 1
        assert result.weighted_pair_count == 1.0
        assert result.accuracy == pytest.approx(0.8)

    def test_one_wide_request_cannot_dominate_ten_narrow_requests(self) -> None:
        rows: list[ReplayRow] = []
        for index in range(10):
            rows.extend(
                [
                    _semantic_row(
                        raw=0.9,
                        decay=1.0,
                        request_id=f"narrow-{index}",
                        memory_id="positive",
                        judge_useful=True,
                    ),
                    _semantic_row(
                        raw=0.1,
                        decay=1.0,
                        request_id=f"narrow-{index}",
                        memory_id="negative",
                        judge_useful=False,
                    ),
                ]
            )
        rows.extend(
            [
                *[
                    _semantic_row(
                        raw=0.1,
                        decay=1.0,
                        request_id="wide",
                        memory_id=f"positive-{index}",
                        judge_useful=True,
                    )
                    for index in range(4)
                ],
                *[
                    _semantic_row(
                        raw=0.9,
                        decay=1.0,
                        request_id="wide",
                        memory_id=f"negative-{index}",
                        judge_useful=False,
                    )
                    for index in range(4)
                ],
            ]
        )

        result = evaluate_pairwise(
            rows,
            {},
            {},
            default_params=ReplayParams(),
            weighting_mode="full",
        )

        assert result.pair_count == 26
        assert result.mixed_request_count == 11
        assert result.weighted_pair_count == 11.0
        assert result.accuracy == pytest.approx(10 / 11)

    def test_unlabeled_rows_form_no_pairs(self) -> None:
        rows = [
            _semantic_row(
                raw=0.9, decay=1.0, request_id="r1", memory_id="useful", judge_useful=True
            ),
            _semantic_row(raw=0.5, decay=1.0, request_id="r1", memory_id="bad", judge_useful=False),
            _semantic_row(raw=0.7, decay=1.0, request_id="r1", memory_id="unlabeled"),
        ]

        result = evaluate_pairwise(rows, {}, {}, default_params=ReplayParams())

        assert result.pair_count == 1
        assert result.accuracy == pytest.approx(1.0)

    def test_misordered_pair_scores_zero_and_ties_half(self) -> None:
        misordered = [
            _semantic_row(
                raw=0.4, decay=1.0, request_id="r1", memory_id="useful", judge_useful=True
            ),
            _semantic_row(raw=0.8, decay=1.0, request_id="r1", memory_id="bad", judge_useful=False),
        ]
        tied = [
            _semantic_row(
                raw=0.6, decay=1.0, request_id="r2", memory_id="useful", judge_useful=True
            ),
            _semantic_row(raw=0.6, decay=1.0, request_id="r2", memory_id="bad", judge_useful=False),
        ]

        low = evaluate_pairwise(misordered, {}, {}, default_params=ReplayParams())
        half = evaluate_pairwise(tied, {}, {}, default_params=ReplayParams())

        assert low.accuracy == pytest.approx(0.0)
        assert half.accuracy == pytest.approx(0.5)

    def test_pairs_never_cross_requests(self) -> None:
        rows = [
            _semantic_row(
                raw=0.9, decay=1.0, request_id="r1", memory_id="useful", judge_useful=True
            ),
            _semantic_row(raw=0.5, decay=1.0, request_id="r2", memory_id="bad", judge_useful=False),
        ]

        result = evaluate_pairwise(rows, {}, {}, default_params=ReplayParams())

        assert result.pair_count == 0
        assert result.accuracy == 0.0

    def test_per_project_params_override_default(self) -> None:
        # Misordered under logged params; project override flips ordering.
        decay_useful = 0.9
        decay_bad = 0.7
        rows = [
            _semantic_row(
                raw=0.6, decay=decay_useful, request_id="r1", memory_id="useful", judge_useful=True
            ),
            _semantic_row(
                raw=0.8, decay=decay_bad, request_id="r1", memory_id="bad", judge_useful=False
            ),
        ]
        assert rows[0].similarity is not None and rows[1].similarity is not None
        assert rows[0].similarity < rows[1].similarity

        fixed = evaluate_pairwise(
            rows,
            {"proj-a": ReplayParams(half_life_days=7.0)},
            {},
            default_params=ReplayParams(),
        )
        broken = evaluate_pairwise(rows, {}, {}, default_params=ReplayParams())

        assert fixed.accuracy == pytest.approx(1.0)
        assert fixed.per_project == {"proj-a": pytest.approx(1.0)}
        assert broken.accuracy == pytest.approx(0.0)


class TestSplitAndPartialPooling:
    def test_split_is_deterministic_and_per_project(self) -> None:
        rows = [
            _row("a1", "m", project_id="proj-a"),
            _row("a2", "m", project_id="proj-a"),
            _row("a3", "m", project_id="proj-a"),
            _row("b1", "m", project_id="proj-b"),
            _row("b2", "m", project_id="proj-b"),
        ]

        train, evaluation = split_requests_per_project(rows)

        assert {r.recall_request_id for r in train} == {"a2", "a3", "b1"}
        assert {r.recall_request_id for r in evaluation} == {"a1", "b2"}
        # Both projects appear on both sides — the partial-pooling contract.
        assert {r.project_id for r in train} == {"proj-a", "proj-b"}
        assert {r.project_id for r in evaluation} == {"proj-a", "proj-b"}

    def test_split_rejects_stride_below_two(self) -> None:
        with pytest.raises(ValueError, match="eval_stride"):
            split_requests_per_project([_row()], eval_stride=1)

    def test_split_is_request_seeded_and_versioned(self) -> None:
        rows = [_row(f"request-{index}", "memory", project_id="project") for index in range(20)]

        train_v1, holdout_v1 = split_requests_per_project(rows, split_version="split-v1")
        reverse_train_v1, reverse_holdout_v1 = split_requests_per_project(
            list(reversed(rows)), split_version="split-v1"
        )
        _, holdout_v2 = split_requests_per_project(rows, split_version="split-v2")

        assert {row.recall_request_id for row in train_v1} == {
            row.recall_request_id for row in reverse_train_v1
        }
        assert {row.recall_request_id for row in holdout_v1} == {
            row.recall_request_id for row in reverse_holdout_v1
        }
        assert {row.recall_request_id for row in holdout_v1} != {
            row.recall_request_id for row in holdout_v2
        }

    def test_small_project_shrinks_toward_pooled(self) -> None:
        # proj-big: many pairs preferring h=7; proj-tiny: one pair preferring
        # h=120. Pooled fit lands on 7; tiny's fit must shrink ~all the way.
        rows: list[ReplayRow] = []
        for i in range(10):
            rows += [
                _semantic_row(
                    raw=0.6,
                    decay=0.9,
                    request_id=f"big-{i}",
                    memory_id="useful",
                    project_id="proj-big",
                    judge_useful=True,
                ),
                _semantic_row(
                    raw=0.8,
                    decay=0.7,
                    request_id=f"big-{i}",
                    memory_id="bad",
                    project_id="proj-big",
                    judge_useful=False,
                ),
            ]
        # Tiny project has one 4x4 request preferring the long half-life.
        # Its 16 raw pairs remain one independent shrinkage unit.
        rows += [
            *[
                _semantic_row(
                    raw=0.8,
                    decay=0.7,
                    request_id="tiny-1",
                    memory_id=f"useful-{index}",
                    project_id="proj-tiny",
                    judge_useful=True,
                )
                for index in range(4)
            ],
            *[
                _semantic_row(
                    raw=0.6,
                    decay=0.9,
                    request_id="tiny-1",
                    memory_id=f"bad-{index}",
                    project_id="proj-tiny",
                    judge_useful=False,
                )
                for index in range(4)
            ],
        ]
        grid = [ReplayParams(half_life_days=7.0), ReplayParams(half_life_days=120.0)]

        fitted = fit_partial_pooled(rows, grid, {}, shrinkage_requests=50.0)

        assert isinstance(fitted, FittedParams)
        assert fitted.pooled.half_life_days == 7.0
        assert fitted.project_pairs["proj-tiny"] == 16
        assert fitted.project_mixed_requests["proj-tiny"] == 1
        tiny = fitted.per_project["proj-tiny"]
        assert tiny.half_life_days is not None
        # lam = 1/51 -> 7 + (120-7)/51 ≈ 9.2: pinned to pooled, not to 120.
        assert tiny.half_life_days == pytest.approx(7.0 + 113.0 / 51.0)
        big = fitted.per_project["proj-big"]
        assert big.half_life_days is not None
        assert big.half_life_days < tiny.half_life_days

    def test_project_without_labeled_pairs_rides_pooled(self) -> None:
        rows = [
            _semantic_row(
                raw=0.6,
                decay=0.9,
                request_id="a-1",
                memory_id="useful",
                project_id="proj-a",
                judge_useful=True,
            ),
            _semantic_row(
                raw=0.8,
                decay=0.7,
                request_id="a-1",
                memory_id="bad",
                project_id="proj-a",
                judge_useful=False,
            ),
            _semantic_row(raw=0.5, decay=0.9, request_id="c-1", project_id="proj-c"),
        ]
        grid = [ReplayParams(half_life_days=7.0), ReplayParams(half_life_days=120.0)]

        fitted = fit_partial_pooled(rows, grid, {})

        assert fitted.project_pairs["proj-c"] == 0
        assert fitted.per_project["proj-c"] == fitted.pooled


class TestFitAndEvaluate:
    def _planted_rows(self) -> list[ReplayRow]:
        """Useful is recent, unuseful is old, logged ordering is inverted.

        Under the logged half-life (30d) the stale-but-high-raw memory wins
        (0.56 > 0.54); any shorter replayed half-life flips every request.
        """
        rows: list[ReplayRow] = []
        for project in ("proj-a", "proj-b"):
            for i in range(4):
                request = f"{project}-req-{i}"
                rows += [
                    _semantic_row(
                        raw=0.6,
                        decay=0.9,
                        request_id=request,
                        memory_id="useful",
                        project_id=project,
                        judge_useful=True,
                        injection_position=0,
                    ),
                    _semantic_row(
                        raw=0.8,
                        decay=0.7,
                        request_id=request,
                        memory_id="bad",
                        project_id=project,
                        judge_useful=False,
                        injection_position=1,
                    ),
                    _semantic_row(
                        raw=0.5,
                        decay=0.8,
                        request_id=request,
                        memory_id="unlabeled",
                        project_id=project,
                        injection_position=2,
                    ),
                ]
        return rows

    def test_recovers_planted_half_life_and_beats_baseline_on_holdout(self) -> None:
        report = fit_and_evaluate(
            self._planted_rows(), default_replay_grid(), weighting_mode="injected"
        )

        assert report.rows_total == 24
        assert report.rows_labeled == 16
        assert report.train_requests == 4
        assert report.eval_requests == 4
        assert report.fitted.pooled.half_life_days == 7.0
        assert report.baseline_eval.accuracy == pytest.approx(0.0)
        assert report.fitted_eval.accuracy == pytest.approx(1.0)
        assert set(report.fitted_eval.per_project) == {"proj-a", "proj-b"}
        for accuracy in report.fitted_eval.per_project.values():
            assert 0.0 <= accuracy <= 1.0

    def test_selects_shrinkage_on_synthetic_and_nested_training_rows(self) -> None:
        outer_train, _ = split_requests_per_project(self._planted_rows())
        synthetic_rows = [
            _semantic_row(
                raw=0.9,
                decay=1.0,
                request_id="synthetic",
                memory_id="positive",
                judge_useful=True,
            ),
            _semantic_row(
                raw=0.1,
                decay=1.0,
                request_id="synthetic",
                memory_id="negative",
                judge_useful=False,
            ),
        ]

        selection = select_shrinkage_requests(
            training_rows=outer_train,
            synthetic_rows=synthetic_rows,
            grid=default_replay_grid(),
        )

        assert tuple(selection.candidate_scores) == SHRINKAGE_REQUEST_CANDIDATES
        assert selection.selected_requests in SHRINKAGE_REQUEST_CANDIDATES
        assert selection.selection_method == "synthetic+nested-training-v1"
        assert selection.nested_train_requests + selection.nested_validation_requests == 4
        assert selection.synthetic_requests == 1


class TestReplayParamsValidation:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"half_life_days": 0.0},
            {"half_life_days": -1.0},
            {"cooccur_support_cap": 0},
            {"cooccur_alpha": 0.0},
            {"cooccur_alpha": 1.5},
        ],
    )
    def test_rejects_out_of_range_params(self, kwargs: dict[str, float]) -> None:
        with pytest.raises(ValueError):
            ReplayParams(**kwargs)  # type: ignore[arg-type]


class TestSignalRowAdapter:
    def test_maps_fetch_replay_rows_shape(self) -> None:
        row = replay_row_from_signal_row(
            {
                "recall_request_id": "req-1",
                "memory_id": "mem-1",
                "project_id": "proj-1",
                "rank": 2,
                "similarity": 0.72,
                "raw_semantic_score": None,
                "temporal_decay_factor": 1.0,
                "ranking_score": 0.4,
                "ranking_mode": "graph_synthetic",
                "graph_score": 0.8,
                "edge_cosine": 0.8,
                "edge_support_norm": 0.6,
                "edge_weight_blend": 0.7,
                "injection_position": 1,
                "injection_group": "pattern",
                "judge_useful": True,
                "label_source": "digest",
                "weighting": {"temporal_decay_half_life_days": 30.0},
                "graph_synthetic_similarity_discount": 0.9,
            }
        )

        assert row.recall_request_id == "req-1"
        assert row.rank == 2
        assert row.ranking_mode == "graph_synthetic"
        assert row.injection_group == "pattern"
        assert row.judge_useful is True
        assert row.logged_half_life_days == 30.0
        assert row.logged_graph_discount == 0.9
        # Frozen writer constants are the assumed logging-time values.
        assert row.logged_cooccur_alpha == 0.5
        assert row.logged_cooccur_support_cap == 5

    def test_unlabeled_row_defaults(self) -> None:
        row = replay_row_from_signal_row(
            {
                "recall_request_id": "req-1",
                "memory_id": "mem-2",
                "rank": 0,
                "ranking_score": None,
                "weighting": None,
            }
        )

        assert row.judge_useful is None
        assert row.ranking_score == 0.0
        assert row.logged_half_life_days is None


def _signal_row(
    *,
    request_id: str = "req-1",
    memory_id: str = "mem-1",
    rank: int = 0,
    query_text: str = "postgres connection pool exhaustion during migration",
    excerpt: str = "The migration exhausts the postgres connection pool.",
    similarity: float | None = 0.8,
    temporal_decay_factor: float | None = 1.0,
    judge_useful: bool | None = True,
    presented: list[dict[str, Any]] | None = None,
    project_id: str | None = "proj-a",
    search_via: str = "semantic",
    graph_score: float | None = None,
) -> dict[str, Any]:
    """One `fetch_shadow_replay_rows` row as the candidate replay consumes it.

    `similarity` is the decayed score the replay orders on; dividing
    `temporal_decay_factor` back out gives the score it thresholds on. Decay
    defaults to 1.0, which makes the two axes the same number.

    `search_via` and `graph_score` together say whether a row is a
    graph-expander find: `graph_score` is the raw entity-match confidence for
    any graph-sourced hit, and a `search_via` without `semantic` is what the
    live path records for a candidate the vector leg's own window missed.
    """
    return {
        "recall_request_id": request_id,
        "memory_id": memory_id,
        "project_id": project_id,
        "rank": rank,
        "similarity": similarity,
        "temporal_decay_factor": temporal_decay_factor,
        "judge_useful": judge_useful,
        "query_text": query_text,
        "search_via": search_via,
        "graph_score": graph_score,
        "presented": (
            presented
            if presented is not None
            else [{"memory_id": memory_id, "excerpt": excerpt, "neutral_key": "M1"}]
        ),
    }


PROJECT_SCOPE = "44444444-4444-4444-8444-444444444444"


def _stored_memory(memory_id: str) -> Memory:
    """One stored memory as `build_results` hydrates it, before any scoring.

    Aged a fixed span rather than pinned to a date, so the decay factor stays
    meaningfully below 1 forever instead of drifting toward an underflow that
    would eventually make the undecayed axis unrecoverable.
    """
    aged = datetime.now(UTC) - timedelta(days=45)
    return Memory(
        id=memory_id,
        memory_type=MemoryType.FACT,
        content=f"Recorded candidate {memory_id}.",
        created_at=aged,
        updated_at=aged,
        project_id=PROJECT_SCOPE,
        tags=["test"],
    )


class _FlatStorage:
    """The slice of `LocalMemoryManager` that `build_results` actually calls."""

    def __init__(self, memories: Sequence[Memory]) -> None:
        self._memories = list(memories)

    def get_memories(self, memory_ids: Sequence[str], *, scope: Any = None) -> list[Memory]:
        wanted = set(memory_ids)
        return [mem for mem in self._memories if mem.id in wanted]

    def get_memory(self, memory_id: str, *, scope: Any = None) -> Memory:
        for mem in self._memories:
            if mem.id == memory_id:
                return mem
        raise ValueError(memory_id)


def _recorded_rows(
    candidates: Sequence[Memory], graph_score_map: Mapping[str, float]
) -> list[dict[str, Any]]:
    """Log scored candidates the way a recall signal row records them.

    `graph_score` comes from the raw graph map rather than from the candidate,
    because that is what the live logger writes: every graph-sourced hit gets
    its undiscounted confidence recorded, including one the vector leg also
    found. Reconstructing the gate from those rows is exactly what replay does.
    """
    presented = [
        {"memory_id": mem.id, "excerpt": mem.content, "neutral_key": f"M{index + 1}"}
        for index, mem in enumerate(candidates)
    ]
    return [
        {
            "recall_request_id": "req-1",
            "memory_id": mem.id,
            "project_id": None,
            "rank": index,
            "similarity": mem.similarity,
            "temporal_decay_factor": mem.temporal_decay_factor,
            "judge_useful": True,
            "query_text": "recorded candidate set",
            "presented": presented,
            "search_via": mem.search_via,
            "graph_score": graph_score_map.get(mem.id),
        }
        for index, mem in enumerate(candidates)
    ]


def _request_rows(
    request_id: str,
    query_text: str,
    candidates: Sequence[tuple[str, str, float | None, bool | None]],
    *,
    project_id: str | None = "proj-a",
) -> list[dict[str, Any]]:
    """Build one request's rows sharing a single `presented` block."""
    presented = [
        {"memory_id": memory_id, "excerpt": excerpt, "neutral_key": f"M{index + 1}"}
        for index, (memory_id, excerpt, _similarity, _useful) in enumerate(candidates)
    ]
    return [
        _signal_row(
            request_id=request_id,
            memory_id=memory_id,
            rank=index,
            query_text=query_text,
            similarity=similarity,
            judge_useful=useful,
            presented=presented,
            project_id=project_id,
        )
        for index, (memory_id, _excerpt, similarity, useful) in enumerate(candidates)
    ]


_COHORT = {
    "label_source": "digest_shadow",
    "query_construction_version": "digest-enriched-natural-language-v2",
    "judge_protocol_version": "digest-shadow-query-relevance-v2",
}


class TestCandidateFilterScore:
    def test_full_query_coverage_scores_one(self) -> None:
        score = candidate_filter_score(
            "postgres connection pool exhaustion",
            "The postgres connection pool hits exhaustion under load.",
        )

        assert score == 1.0

    def test_partial_coverage_is_the_covered_share_of_query_terms(self) -> None:
        # Content tokens: postgres, connection, pool, exhaustion. Two covered.
        score = candidate_filter_score(
            "postgres connection pool exhaustion",
            "Tune the connection pool for the worker fleet.",
        )

        assert score == 0.5

    def test_stopwords_and_short_tokens_do_not_inflate_coverage(self) -> None:
        # "migration" is the sole content token on either side, so coverage is
        # complete; the shared "the"/"a"/"is"/"done" must not count as matches,
        # and the unmatched ones must not count against it either.
        score = candidate_filter_score(
            "is the migration done",
            "the a is of to migration",
        )

        assert score == 1.0

    def test_an_uncovered_query_term_lowers_coverage(self) -> None:
        # Content tokens: migration, problem. Only migration is covered.
        score = candidate_filter_score(
            "is the migration a problem",
            "the a is of to migration",
        )

        assert score == 0.5

    def test_a_query_with_no_content_tokens_scores_zero(self) -> None:
        assert candidate_filter_score("is it the one", "postgres connection pool") == 0.0

    def test_a_long_excerpt_is_not_penalized_for_saying_more(self) -> None:
        focused = candidate_filter_score("connection pool", "connection pool")
        verbose = candidate_filter_score(
            "connection pool",
            "connection pool sizing interacts with worker concurrency and idle timeouts "
            "across every deployment target we support.",
        )

        assert focused == verbose == 1.0


class TestCandidateReplayRowAdaptation:
    def test_the_excerpt_is_taken_from_the_matching_presented_entry(self) -> None:
        rows = candidate_replay_rows_from_signal_rows(
            _request_rows(
                "req-1",
                "connection pool",
                [
                    ("mem-1", "first excerpt", 0.9, True),
                    ("mem-2", "second excerpt", 0.4, False),
                ],
            )
        )

        assert [row.memory_id for row in rows] == ["mem-1", "mem-2"]
        assert [row.excerpt for row in rows] == ["first excerpt", "second excerpt"]

    def test_a_row_absent_from_the_snapshot_is_dropped_not_scored_empty(self) -> None:
        row = _signal_row(memory_id="mem-9", presented=[{"memory_id": "mem-1", "excerpt": "x"}])

        assert candidate_replay_rows_from_signal_rows([row]) == []

    def test_a_row_without_stored_query_text_is_dropped(self) -> None:
        row = _signal_row()
        row["query_text"] = None

        assert candidate_replay_rows_from_signal_rows([row]) == []


class TestSelection:
    def test_the_filter_admits_at_most_max_selected_best_first(self) -> None:
        rows = candidate_replay_rows_from_signal_rows(
            _request_rows(
                "req-1",
                "postgres connection pool exhaustion",
                [
                    ("mem-1", "connection pool", 0.9, True),
                    ("mem-2", "postgres connection pool exhaustion", 0.5, True),
                    ("mem-3", "postgres connection pool", 0.4, False),
                    ("mem-4", "postgres pool exhaustion", 0.3, False),
                ],
            )
        )

        selection = select_by_candidate_filter(rows, CandidateFilterParams(max_selected=3))

        assert [row.memory_id for row, _score in selection] == ["mem-2", "mem-3", "mem-4"]

    def test_the_filter_abstains_when_nothing_clears_its_floor(self) -> None:
        rows = candidate_replay_rows_from_signal_rows(
            _request_rows(
                "req-1",
                "postgres connection pool exhaustion",
                [("mem-1", "unrelated tailscale funnel notes", 0.99, True)],
            )
        )

        assert select_by_candidate_filter(rows, CandidateFilterParams()) == []

    def test_static_constants_drop_a_candidate_with_no_similarity(self) -> None:
        rows = candidate_replay_rows_from_signal_rows(
            _request_rows(
                "req-1",
                "connection pool",
                [
                    ("mem-1", "connection pool", None, True),
                    ("mem-2", "connection pool", 0.7, True),
                ],
            )
        )

        selection = select_by_static_constants(rows, min_similarity=0.65, max_selected=3)

        assert [row.memory_id for row, _score in selection] == ["mem-2"]

    def test_static_constants_threshold_undecayed_and_order_decayed(self) -> None:
        """#20831: the replayed arm has to move with the live floor's axis.

        `select_by_static_constants` exists to model what the shipped selection
        does, so once the live floor started dividing decay back out this arm
        had to as well -- otherwise phase-4 fitting ratifies a selection nothing
        performs. `aged` clears 0.70 only undecayed (0.60 / 0.8 = 0.75);
        `fresh` clears it on neither.
        """
        rows = candidate_replay_rows_from_signal_rows(
            [
                _signal_row(
                    request_id="req-1",
                    memory_id="aged",
                    rank=0,
                    similarity=0.60,
                    temporal_decay_factor=0.8,
                ),
                _signal_row(
                    request_id="req-1",
                    memory_id="fresh",
                    rank=1,
                    similarity=0.69,
                    temporal_decay_factor=1.0,
                ),
            ]
        )

        selection = select_by_static_constants(rows, min_similarity=0.70, max_selected=3)

        assert [row.memory_id for row, _score in selection] == ["aged"]
        assert [score for _row, score in selection] == [0.60], "ordering still reads the decayed"


class TestReplayedGraphConfidenceAxis:
    """#20879: the replayed arm has to judge a graph find the way the live gate does.

    Since #20873 a graph-expander find -- a memory the graph surfaced and the
    vector leg's own window missed -- is admitted on its entity-match
    confidence, with its cosine used only to rank it. An arm still reading the
    cosine disagrees with live selection on every such hit, and the Phase 4
    refit owns exactly the constants that gate them.
    """

    def test_a_graph_expander_find_is_admitted_on_confidence_not_its_cosine(self) -> None:
        # The expander's whole value is the low-cosine, high-confidence hit, so
        # `found` clears the confidence floor on a cosine that `missed`, judged
        # on the same number by the semantic leg, does not survive.
        rows = candidate_replay_rows_from_signal_rows(
            [
                _signal_row(
                    memory_id="found",
                    rank=0,
                    similarity=0.40,
                    search_via="graph",
                    graph_score=0.80,
                ),
                _signal_row(
                    memory_id="missed",
                    rank=1,
                    similarity=0.40,
                    search_via="semantic",
                ),
            ]
        )

        selection = select_by_static_constants(rows, min_similarity=0.70, max_selected=3)

        assert [row.memory_id for row, _score in selection] == ["found"]

    def test_a_graph_expander_find_under_the_confidence_floor_is_dropped(self) -> None:
        # 0.572 is the measured median of the live confidence distribution, and
        # a strong cosine must not rescue it: confidence is the whole admission.
        rows = candidate_replay_rows_from_signal_rows(
            [
                _signal_row(
                    memory_id="weak",
                    rank=0,
                    similarity=0.95,
                    search_via="graph|keyword",
                    graph_score=0.572,
                )
            ]
        )

        assert select_by_static_constants(rows, min_similarity=0.70, max_selected=3) == []

    def test_a_graph_hit_the_vector_leg_also_found_is_gated_on_its_cosine(self) -> None:
        # Live sets no confidence for a candidate already in the semantic
        # window, so letting entity confidence rescue a sub-floor cosine here
        # would widen the semantic axis under cover of the expander.
        rows = candidate_replay_rows_from_signal_rows(
            [
                _signal_row(
                    memory_id="both-legs",
                    rank=0,
                    similarity=0.40,
                    search_via="semantic|graph",
                    graph_score=0.95,
                )
            ]
        )

        assert select_by_static_constants(rows, min_similarity=0.70, max_selected=3) == []

    def test_an_unreachable_cosine_floor_still_admits_a_graph_expander_find(self) -> None:
        # The matched arm sweeps the cosine floor. A sweep that could gate the
        # graph axis would model a selection the live path never performs.
        rows = candidate_replay_rows_from_signal_rows(
            [
                _signal_row(
                    memory_id="found",
                    rank=0,
                    similarity=0.10,
                    search_via="graph",
                    graph_score=0.80,
                )
            ]
        )

        selection = select_by_static_constants(rows, min_similarity=1.0, max_selected=3)

        assert [row.memory_id for row, _score in selection] == ["found"]

    def test_the_confidence_floor_is_a_tunable_the_refit_can_sweep(self) -> None:
        rows = candidate_replay_rows_from_signal_rows(
            [
                _signal_row(
                    memory_id="found",
                    rank=0,
                    similarity=0.10,
                    search_via="graph",
                    graph_score=0.60,
                )
            ]
        )

        assert (
            select_by_static_constants(
                rows, min_similarity=0.70, max_selected=3, graph_confidence_min_score=0.55
            )
            != []
        )
        assert (
            select_by_static_constants(
                rows, min_similarity=0.70, max_selected=3, graph_confidence_min_score=0.65
            )
            == []
        )


class TestMatchedStaticThreshold:
    def test_the_threshold_grid_is_the_undecayed_axis_it_compares_on(self) -> None:
        """#20879: a grid of decayed values cannot reach an undecayed breakpoint.

        `select_by_static_constants` compares the undecayed score, so the
        breakpoints where mean-selected changes are the undecayed values. Both
        rows here decay by half, which puts every decayed grid point below both
        breakpoints -- so on that grid the arm selects two candidates at every
        threshold it can offer and the match fails outright, while the
        undecayed grid lands the target exactly.
        """
        rows = candidate_replay_rows_from_signal_rows(
            [
                _signal_row(
                    memory_id="aged-high", rank=0, similarity=0.40, temporal_decay_factor=0.5
                ),
                _signal_row(
                    memory_id="aged-low", rank=1, similarity=0.30, temporal_decay_factor=0.5
                ),
            ]
        )

        matched = _match_static_threshold(
            {"req-1": rows},
            target=1.0,
            max_selected=3,
            tolerance=0.05,
            graph_confidence_min_score=_GRAPH_CONFIDENCE_SELECTION_FLOOR,
        )

        assert matched == pytest.approx(0.80)

    def test_a_graph_expander_find_is_not_a_breakpoint_on_the_swept_axis(self) -> None:
        """A cohort of graph finds alone offers the sweep no cosine breakpoints.

        Their admission is decided by the confidence floor at every threshold,
        so the only grid point is the 0.0 floor and the reported mean counts
        exactly the find that clears the confidence floor.
        """
        rows = candidate_replay_rows_from_signal_rows(
            [
                _signal_row(
                    memory_id="strong",
                    rank=0,
                    similarity=0.50,
                    search_via="graph",
                    graph_score=0.80,
                ),
                _signal_row(
                    memory_id="weak",
                    rank=1,
                    similarity=0.45,
                    search_via="graph",
                    graph_score=0.512,
                ),
            ]
        )

        matched = _match_static_threshold(
            {"req-1": rows},
            target=1.0,
            max_selected=3,
            tolerance=0.05,
            graph_confidence_min_score=_GRAPH_CONFIDENCE_SELECTION_FLOOR,
        )

        assert matched == 0.0


# `graph-find` sits in the band where the two axes disagree: live admits it at
# 0.70 >= 0.653, while judging its discounted cosine would face 0.70 * 0.9 =
# 0.63 against the 0.70 selection floor and drop it. `graph-find-weak` clears
# the 0.611 search floor and misses the selection floor, so it reaches the gate
# and is refused there. `both-legs` carries a confidence high enough to rescue
# anything, and must not: the vector leg already scored it.
_GRAPH_SCORES = {"graph-find": 0.70, "graph-find-weak": 0.62, "both-legs": 0.95}


class TestReplayMatchesTheLiveGate:
    """The harness and the live gate agree on a recorded candidate set (#20879)."""

    def _live_candidates(self) -> list[Memory]:
        """Score one mixed candidate set through the real search result builder.

        Hand-building `Memory(similarity=...)` would let the test assert
        agreement with a live path it never ran; `build_results` is the step
        that decides which axis each candidate carries, so the fixture has to
        go through it.
        """
        stored = [
            _stored_memory("semantic-strong"),
            _stored_memory("semantic-weak"),
            _stored_memory("graph-find"),
            _stored_memory("graph-find-weak"),
            _stored_memory("both-legs"),
        ]
        return build_results(
            storage=cast(LocalMemoryManager, _FlatStorage(stored)),
            merged_ids=[mem.id for mem in stored],
            ranking_score_map={mem.id: 1.0 for mem in stored},
            qdrant_score_map={
                "semantic-strong": 0.88,
                "semantic-weak": 0.60,
                "both-legs": 0.60,
            },
            qdrant_set={"semantic-strong", "semantic-weak", "both-legs"},
            keyword_set=set(),
            graph_set={"graph-find", "graph-find-weak", "both-legs"},
            graph_score_map=_GRAPH_SCORES,
            rrf_applied=False,
            project_id=None,
            memory_type=None,
            tags_all=None,
            tags_any=None,
            tags_none=None,
            half_life=30.0,
            effective_min_score=0.55,
            limit=10,
        )

    def test_the_replayed_arm_selects_what_the_live_gate_selects(self) -> None:
        candidates = self._live_candidates()

        runner = MemoryRecallRunner(
            db=cast(HubDatabase, None),
            memory_manager=cast(Any, None),
            config=MemoryRecallConfig(),
        )
        live_selected, _drops = runner._filter_ranked(candidates, frozenset())
        live_ids = [payload["id"] for payload in live_selected]

        replayed = select_by_static_constants(
            candidate_replay_rows_from_signal_rows(_recorded_rows(candidates, _GRAPH_SCORES)),
            min_similarity=MemoryRecallConfig().selection_min_score,
            max_selected=MAX_RECALL_MEMORIES,
        )

        assert [row.memory_id for row, _score in replayed] == live_ids
        assert "graph-find" in live_ids, "the fixture must exercise the confidence axis"
        assert "semantic-strong" in live_ids, "the fixture must exercise the cosine axis"


class TestCandidateFilterReplay:
    def _cohort_rows(self) -> list[dict[str, Any]]:
        # req-1: both arms admit both candidates and rank the useful one first
        #        (the filter covers 4/4 query terms vs 3/4).
        # req-2: nothing is topically related, so the filter abstains while
        #        static admits two high-similarity useless memories.
        # req-3: unlabeled throughout — excluded from both arms outright.
        return [
            *_request_rows(
                "req-1",
                "postgres connection pool exhaustion",
                [
                    ("mem-1", "postgres connection pool exhaustion under migration", 0.90, True),
                    ("mem-2", "postgres connection pool", 0.80, False),
                ],
            ),
            *_request_rows(
                "req-2",
                "tailscale funnel certificate renewal",
                [
                    ("mem-3", "worktree cleanup after a merge", 0.95, False),
                    ("mem-4", "ruff formatting conventions", 0.70, False),
                ],
            ),
            *_request_rows(
                "req-3",
                "postgres connection pool exhaustion",
                [("mem-5", "postgres connection pool exhaustion", 0.90, None)],
            ),
        ]

    def _report(self, **kwargs: Any) -> CandidateFilterReplayReport:
        return replay_candidate_filter(
            self._cohort_rows(),
            cohort_identity=_COHORT,
            static_min_similarity=0.65,
            **kwargs,
        )

    def test_requests_with_no_label_are_excluded_from_every_arm(self) -> None:
        report = self._report()

        assert report.requests_total == 3
        assert report.requests_evaluated == 2
        assert report.requests_skipped_unlabeled == 1
        assert report.candidate_filter.requests_evaluated == 2
        assert report.static_constants.requests_evaluated == 2

    def test_the_filter_abstains_where_static_constants_inject(self) -> None:
        report = self._report()

        assert report.candidate_filter.abstention_rate == 0.5
        assert report.static_constants.abstention_rate == 0.0

    def test_a_right_silence_is_abstain_correct_not_regret(self) -> None:
        report = self._report()

        # req-2's sole abstention carried no useful label: a right silence.
        assert report.candidate_filter.abstain_correct == 1.0
        assert report.candidate_filter.abstain_regret == 0.0

    def test_a_missed_injection_is_counted_as_abstain_regret(self) -> None:
        rows = _request_rows(
            "req-1",
            "tailscale funnel certificate renewal",
            [("mem-1", "worktree cleanup after a merge", 0.9, True)],
        )

        report = replay_candidate_filter(rows, cohort_identity=_COHORT, static_min_similarity=0.65)

        assert report.candidate_filter.abstention_rate == 1.0
        assert report.candidate_filter.abstain_regret == 1.0
        assert report.candidate_filter.abstain_correct == 0.0

    def test_mean_selected_counts_abstentions_in_its_denominator(self) -> None:
        report = self._report()

        # The filter takes both of req-1's candidates and abstains on req-2,
        # so its mean is halved by the abstention rather than reported over
        # the requests it answered.
        assert report.candidate_filter.mean_selected == 1.0
        # Static admits both candidates in both requests.
        assert report.static_constants.mean_selected == 2.0

    def test_pairwise_accuracy_carries_its_own_denominator(self) -> None:
        report = self._report()

        # Both arms score 1.0, but each rests on a single mixed request out of
        # two evaluated: the accuracy is not a whole-population number, and
        # only the paired denominator says so.
        assert report.candidate_filter.pairwise_accuracy == 1.0
        assert report.candidate_filter.pairwise_requests == 1
        assert report.candidate_filter.requests_evaluated == 2
        assert report.static_constants.pairwise_accuracy == 1.0
        assert report.static_constants.pairwise_requests == 1
        assert report.static_constants.requests_evaluated == 2

    def test_an_always_abstaining_arm_scores_no_pairwise_requests(self) -> None:
        rows = _request_rows(
            "req-1",
            "tailscale funnel certificate renewal",
            [
                ("mem-1", "worktree cleanup after a merge", 0.9, True),
                ("mem-2", "ruff formatting conventions", 0.8, False),
            ],
        )

        report = replay_candidate_filter(rows, cohort_identity=_COHORT, static_min_similarity=0.65)

        assert report.candidate_filter.abstention_rate == 1.0
        assert report.candidate_filter.pairwise_requests == 0
        assert report.candidate_filter.pairwise_accuracy == 0.0

    def test_a_tied_selection_score_splits_pairwise_credit(self) -> None:
        rows = _request_rows(
            "req-1",
            "connection pool",
            [
                ("mem-1", "connection pool", 0.80, True),
                ("mem-2", "connection pool", 0.80, False),
            ],
        )

        report = replay_candidate_filter(rows, cohort_identity=_COHORT, static_min_similarity=0.65)

        assert report.static_constants.pairwise_requests == 1
        assert report.static_constants.pairwise_accuracy == 0.5

    def test_the_matched_arm_lands_on_the_filters_mean_selected(self) -> None:
        report = self._report()

        matched = report.static_constants_matched
        assert matched is not None
        assert matched.mean_selected == pytest.approx(
            report.candidate_filter.mean_selected, abs=report.mean_selected_match_tolerance
        )
        assert matched.selection_threshold > report.static_constants.selection_threshold

    def test_an_unmatchable_cohort_reports_no_matched_arm(self) -> None:
        # Every candidate shares one similarity, so the static arm can only
        # select all of them or none — it cannot land near 0.5 per request.
        rows = [
            *_request_rows(
                "req-1",
                "postgres connection pool exhaustion",
                [("mem-1", "postgres connection pool exhaustion", 0.9, True)],
            ),
            *_request_rows(
                "req-2",
                "tailscale funnel certificate renewal",
                [("mem-2", "worktree cleanup after a merge", 0.9, False)],
            ),
        ]

        report = replay_candidate_filter(
            rows,
            cohort_identity=_COHORT,
            static_min_similarity=0.65,
            mean_selected_match_tolerance=0.01,
        )

        assert report.candidate_filter.mean_selected == 0.5
        assert report.static_constants_matched is None

    def test_a_cohort_identity_without_the_fence_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="query_construction_version"):
            replay_candidate_filter(
                self._cohort_rows(),
                cohort_identity={"label_source": "digest_shadow"},
                static_min_similarity=0.65,
            )

    def test_a_blank_fence_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="query_construction_version"):
            replay_candidate_filter(
                self._cohort_rows(),
                cohort_identity={"query_construction_version": "  "},
                static_min_similarity=0.65,
            )

    def test_rows_dropped_for_a_missing_excerpt_stay_visible_in_the_report(self) -> None:
        rows = [
            *self._cohort_rows(),
            _signal_row(
                request_id="req-4",
                memory_id="mem-6",
                presented=[{"memory_id": "other", "excerpt": "x"}],
            ),
        ]

        report = replay_candidate_filter(rows, cohort_identity=_COHORT, static_min_similarity=0.65)

        assert report.rows_total == len(rows)
        assert report.rows_scored == len(rows) - 1

    def test_an_empty_cohort_yields_zeroed_arms_rather_than_dividing_by_zero(self) -> None:
        report = replay_candidate_filter([], cohort_identity=_COHORT, static_min_similarity=0.65)

        assert report.requests_evaluated == 0
        assert report.candidate_filter.abstention_rate == 0.0
        assert report.candidate_filter.mean_selected == 0.0
        assert report.static_constants_matched is None


class TestCandidateFilterReplayRecord:
    def _report(self) -> CandidateFilterReplayReport:
        return replay_candidate_filter(
            _request_rows(
                "req-1",
                "postgres connection pool exhaustion",
                [
                    ("mem-1", "postgres connection pool exhaustion", 0.90, True),
                    ("mem-2", "ruff formatting conventions", 0.80, False),
                ],
            ),
            cohort_identity=_COHORT,
            static_min_similarity=0.65,
        )

    def test_both_arms_carry_every_request_level_metric(self) -> None:
        record = self._report().to_record()

        required = {
            "requests_evaluated",
            "abstention_rate",
            "abstain_correct",
            "abstain_regret",
            "mean_selected",
            "pairwise_accuracy",
            "pairwise_requests",
        }
        for arm in ("candidate_filter", "static_constants"):
            assert required <= set(record["arms"][arm]), arm

    def test_the_record_names_the_cohort_identity_it_ran_under(self) -> None:
        record = self._report().to_record()

        assert record["cohort_identity"] == _COHORT
        assert (
            record["cohort_identity"]["query_construction_version"]
            == "digest-enriched-natural-language-v2"
        )

    def test_the_record_states_that_digest_evaluation_needs_v2_data(self) -> None:
        note = self._report().to_record()["digest_conditioned_evaluation"]

        assert "no-digest" in note
        assert "requires v2 data" in note

    def test_the_record_is_json_serializable(self) -> None:
        record = self._report().to_record()

        assert json.loads(json.dumps(record, sort_keys=True)) == record
