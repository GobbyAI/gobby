"""Unit tests for the offline labeled-data fit/replay harness (#17197).

Pure math and pipeline tests over synthetic ``ReplayRow`` fixtures — no
Postgres, no FalkorDB. The end-to-end path against real hub tables lives in
``tests/memory/test_recall_benchmark.py`` (labeled benchmark) and the loader
join in ``tests/storage/test_recall_signals.py``.
"""

from __future__ import annotations

import pytest

from gobby.memory.recall_fit import (
    SHRINKAGE_REQUEST_CANDIDATES,
    FittedParams,
    ReplayParams,
    ReplayRow,
    default_replay_grid,
    estimate_position_propensities,
    evaluate_pairwise,
    fit_and_evaluate,
    fit_partial_pooled,
    ips_weight,
    replay_row_from_signal_row,
    replayed_similarity,
    replayed_sort_key,
    select_shrinkage_requests,
    split_requests_per_project,
)

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
