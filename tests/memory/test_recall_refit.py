"""Unit tests for the #17198 refit ship gate (gobby.memory.recall_refit).

No DB: rows are constructed ReplayRows. The planted datasets encode known
inversions so the expected grid winner — and therefore each gate outcome —
is arithmetically determined, not asserted by faith.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.memory.recall_constants import RECALL_QUERY_CONSTRUCTION_VERSION
from gobby.memory.recall_fit import ReplayParams, ReplayRow, split_requests_per_project
from gobby.memory.recall_refit import (
    MIN_EVAL_PAIRS,
    MIN_TRAIN_PAIRS,
    GateDecision,
    default_candidate_scope,
    guard_accuracy,
    judge_independent_guard_rows,
    refit_grid,
    static_replay_params,
)
from gobby.memory.recall_ship_gate import (
    AUDIT_SAMPLE_REQUESTS,
    GateCohort,
    build_ship_audit_sample,
)
from gobby.memory.recall_ship_gate_run import run_ship_gate, run_ship_gate_from_store
from gobby.memory.services._search_constants import _GRAPH_SYNTHETIC_SIM_DISCOUNT
from gobby.memory.services.knowledge_graph.writer import COOCCUR_ALPHA, COOCCUR_SUPPORT_CAP

pytestmark = pytest.mark.unit


def _gate_cohort() -> GateCohort:
    return GateCohort(
        label_source="digest_shadow",
        candidate_scope="full",
        judge_protocol_version="protocol-v1",
        weighting_regime_key="regime-v1",
        judge_model_key="judge-v1",
        judge_config_fingerprint="config-v1",
        data_cutoff=datetime(2026, 7, 17, 12, tzinfo=UTC),
        completion_cutoff=datetime(2026, 7, 17, 13, tzinfo=UTC),
        project_id=None,
        weighting_mode="full",
        query_construction_version=RECALL_QUERY_CONSTRUCTION_VERSION,
    )


def _audit_candidates(rows: list[ReplayRow]) -> list[dict[str, object]]:
    return [
        {
            "recall_request_id": row.recall_request_id,
            "memory_id": row.memory_id,
            "prompt_hash": f"prompt-{row.recall_request_id}",
            "judge_useful": row.judge_useful,
        }
        for row in rows
    ]


def _ship_gate_kwargs(rows: list[ReplayRow]) -> dict[str, object]:
    cohort = _gate_cohort()
    train, _evaluation = split_requests_per_project(rows)
    candidates = _audit_candidates(rows)
    sample = build_ship_audit_sample(
        candidates,
        cohort=cohort,
        train_request_ids={row.recall_request_id for row in train},
    )
    verdicts = [
        {
            "request_id": target.request_id,
            "memory_id": target.memory_id,
            "prompt_hash": target.prompt_hash,
            "human_verdict": target.judge_useful,
        }
        for target in sample.targets
    ]
    return {
        "label_source": cohort.label_source,
        "candidate_scope": cohort.candidate_scope,
        "judge_protocol_version": cohort.judge_protocol_version,
        "query_construction_version": cohort.query_construction_version,
        "weighting_regime_key": cohort.weighting_regime_key,
        "judge_model_key": cohort.judge_model_key,
        "judge_config_fingerprint": cohort.judge_config_fingerprint,
        "data_cutoff": cohort.data_cutoff,
        "completion_cutoff": cohort.completion_cutoff,
        "audit_rows": candidates,
        "audit_verdicts": verdicts,
    }


def _signal_rows(rows: list[ReplayRow]) -> list[dict[str, object]]:
    return [
        {
            "recall_request_id": row.recall_request_id,
            "memory_id": row.memory_id,
            "project_id": row.project_id,
            "rank": row.rank,
            "similarity": row.similarity,
            "raw_semantic_score": row.raw_semantic_score,
            "temporal_decay_factor": row.temporal_decay_factor,
            "ranking_score": row.ranking_score,
            "ranking_mode": row.ranking_mode,
            "graph_score": row.graph_score,
            "edge_cosine": row.edge_cosine,
            "edge_support_norm": row.edge_support_norm,
            "edge_weight_blend": row.edge_weight_blend,
            "injection_position": row.injection_position,
            "injection_group": row.injection_group,
            "judge_useful": row.judge_useful,
            "label_source": "digest_shadow",
            "weighting": {"temporal_decay_half_life_days": row.logged_half_life_days},
            "graph_synthetic_similarity_discount": row.logged_graph_discount,
            "prompt_hash": f"prompt-{row.recall_request_id}",
        }
        for row in rows
    ]


class _RecordingGateStore:
    def __init__(self, rows: list[ReplayRow]) -> None:
        self.raw_rows = _signal_rows(rows)
        _train_ids, self.holdout_ids = split_requests_per_project(rows)
        self.holdout_ids = {row.recall_request_id for row in self.holdout_ids}
        self.audit_fetched = False
        self.reserved = False
        self.completed = False
        self.calls: list[str] = []

    def shadow_cohort_query(self, phase: str, **_kwargs: Any) -> list[dict[str, object]]:
        assert phase == "fitting"
        self.calls.append("cohort")
        by_request: dict[str, dict[str, object]] = {}
        for row in self.raw_rows:
            request_id = str(row["recall_request_id"])
            by_request[request_id] = {
                "recall_request_id": request_id,
                "project_id": row["project_id"],
            }
        return list(by_request.values())

    def fetch_shadow_replay_rows(self, **kwargs: Any) -> list[dict[str, object]]:
        request_ids = set(kwargs["request_ids"])
        reading_holdout = bool(request_ids & self.holdout_ids)
        if reading_holdout:
            assert self.reserved
            self.calls.append("holdout_rows")
        else:
            self.calls.append("training_rows")
        return [row for row in self.raw_rows if row["recall_request_id"] in request_ids]

    def fetch_audit_verdicts(
        self,
        cohort_digest: str,
        sample_digest: str,
        *,
        expected_prompt_hashes: dict[tuple[str, str], str],
    ) -> list[dict[str, object]]:
        self.audit_fetched = True
        self.calls.append("audit")
        useful = {
            (str(row["recall_request_id"]), str(row["memory_id"])): row["judge_useful"]
            for row in self.raw_rows
        }
        return [
            {
                "cohort_digest": cohort_digest,
                "sample_digest": sample_digest,
                "request_id": request_id,
                "memory_id": memory_id,
                "prompt_hash": prompt_hash,
                "human_verdict": useful[(request_id, memory_id)],
            }
            for (request_id, memory_id), prompt_hash in expected_prompt_hashes.items()
        ]

    def reserve_gate_holdout(self, **kwargs: Any) -> SimpleNamespace:
        assert self.audit_fetched
        self.reserved = True
        self.calls.append("reserve")
        return SimpleNamespace(
            status="reserved",
            request_ids=tuple(kwargs["holdout_partition_ids"]),
            claim_token="gate-token",
            decision=None,
            ship=None,
        )

    def complete_gate_run(self, *_args: object, **_kwargs: Any) -> SimpleNamespace:
        self.completed = True
        self.calls.append("complete")
        return SimpleNamespace(status="complete")


class _IncompleteAuditStore(_RecordingGateStore):
    def fetch_audit_verdicts(
        self,
        cohort_digest: str,
        sample_digest: str,
        *,
        expected_prompt_hashes: dict[tuple[str, str], str],
    ) -> list[dict[str, object]]:
        rows = super().fetch_audit_verdicts(
            cohort_digest,
            sample_digest,
            expected_prompt_hashes=expected_prompt_hashes,
        )
        return rows[:-1]


class _CompletedGateStore(_RecordingGateStore):
    def __init__(self, rows: list[ReplayRow], record: dict[str, Any]) -> None:
        super().__init__(rows)
        self.record = record

    def reserve_gate_holdout(self, **_kwargs: Any) -> SimpleNamespace:
        assert self.audit_fetched
        self.calls.append("reserve")
        return SimpleNamespace(
            status="complete",
            request_ids=(),
            claim_token=None,
            decision=self.record,
            ship=self.record["ship"],
        )


def _run_recording_store_gate(store: Any) -> GateDecision:
    cohort = _gate_cohort()
    return run_ship_gate_from_store(
        store,
        label_source=cohort.label_source,
        candidate_scope=cohort.candidate_scope,
        judge_protocol_version=cohort.judge_protocol_version,
        query_construction_version=cohort.query_construction_version,
        weighting_regime_key=cohort.weighting_regime_key,
        judge_model_key=cohort.judge_model_key,
        judge_config_fingerprint=cohort.judge_config_fingerprint,
        data_cutoff=cohort.data_cutoff,
        completion_cutoff=cohort.completion_cutoff,
    )


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


class TestGateCohortIdentity:
    def test_identity_carries_the_query_construction_version(self) -> None:
        """4.1.2: the era is part of what the cohort digest freezes.

        Two cohorts that differ only in which query built their retrievals are
        different populations, so they must not hash to one holdout key.
        """
        cohort = _gate_cohort()
        legacy = replace(cohort, query_construction_version=None)

        assert cohort.identity()["query_construction_version"] == (
            RECALL_QUERY_CONSTRUCTION_VERSION
        )
        assert legacy.identity()["query_construction_version"] is None
        assert cohort.digest != legacy.digest

    def test_a_blank_construction_version_is_rejected(self) -> None:
        """An empty string is neither a legacy cohort nor a named era."""
        with pytest.raises(ValueError, match="query_construction_version"):
            replace(_gate_cohort(), query_construction_version="   ")


class TestShipAuditSample:
    @pytest.mark.parametrize(
        ("label_source", "expected"),
        [("digest_shadow", "full"), ("ablation", "injected")],
    )
    def test_default_scope_is_bound_to_label_stream(self, label_source: str, expected: str) -> None:
        assert default_candidate_scope(label_source) == expected

    def test_is_deterministic_and_uses_only_training_requests(self) -> None:
        rows = _planted_requests(
            120,
            rel_raw=0.75,
            rel_decay=0.6,
            irr_raw=0.5,
            irr_decay=0.95,
        )
        train, evaluation = split_requests_per_project(rows)
        train_ids = {row.recall_request_id for row in train}
        evaluation_ids = {row.recall_request_id for row in evaluation}
        candidates = _audit_candidates(rows)

        first = build_ship_audit_sample(
            candidates,
            cohort=_gate_cohort(),
            train_request_ids=train_ids,
        )
        repeated = build_ship_audit_sample(
            list(reversed(candidates)),
            cohort=_gate_cohort(),
            train_request_ids=train_ids,
        )

        assert first == repeated
        assert len(first.targets) == AUDIT_SAMPLE_REQUESTS == 50
        assert len({target.request_id for target in first.targets}) == 50
        assert {target.request_id for target in first.targets}.isdisjoint(evaluation_ids)
        assert first.cohort_digest
        assert first.sample_digest


class TestStoreShipGate:
    def test_reserves_before_reading_holdout_and_completes_once(self) -> None:
        rows = _planted_requests(
            120,
            rel_raw=0.75,
            rel_decay=0.6,
            irr_raw=0.5,
            irr_decay=0.95,
        )
        store = _RecordingGateStore(rows)
        decision = _run_recording_store_gate(store)

        assert decision.ship is True
        assert store.completed is True
        assert store.calls == [
            "cohort",
            "training_rows",
            "audit",
            "reserve",
            "holdout_rows",
            "complete",
        ]

    def test_failed_audit_never_reserves_or_reads_holdout(self) -> None:
        rows = _planted_requests(
            120,
            rel_raw=0.75,
            rel_decay=0.6,
            irr_raw=0.5,
            irr_decay=0.95,
        )
        store = _IncompleteAuditStore(rows)

        decision = _run_recording_store_gate(store)

        assert decision.audit.status == "incomplete"
        assert decision.ship is False
        assert "reserve" not in store.calls
        assert "holdout_rows" not in store.calls

    def test_completed_rerun_rehydrates_stored_decision_without_holdout_read(self) -> None:
        rows = _planted_requests(
            120,
            rel_raw=0.75,
            rel_decay=0.6,
            irr_raw=0.5,
            irr_decay=0.95,
        )
        record = run_ship_gate(rows, **_ship_gate_kwargs(rows)).to_record()
        store = _CompletedGateStore(rows, record)

        repeated = _run_recording_store_gate(store)

        assert repeated.to_record() == record
        assert "holdout_rows" not in store.calls
        assert "complete" not in store.calls


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
        rows = _planted_requests(
            120,
            rel_raw=0.75,
            rel_decay=0.6,
            irr_raw=0.5,
            irr_decay=0.95,
        )
        decision = run_ship_gate(rows, **_ship_gate_kwargs(rows))
        assert decision.report.fitted.pooled == replace(static_replay_params(), half_life_days=60.0)
        assert decision.sufficient_data is True
        assert decision.audit_ok is True
        assert decision.beats_static is True
        assert decision.guard_ok is True
        assert decision.ship is True
        assert decision.static_eval.accuracy == 0.0
        assert decision.report.fitted_eval.accuracy == 1.0
        assert any("beat the static constants" in reason for reason in decision.reasons)

    def test_rejects_incomplete_or_below_threshold_audit(self) -> None:
        rows = _planted_requests(
            120,
            rel_raw=0.75,
            rel_decay=0.6,
            irr_raw=0.5,
            irr_decay=0.95,
        )
        incomplete_kwargs = _ship_gate_kwargs(rows)
        incomplete_kwargs["audit_verdicts"] = list(incomplete_kwargs["audit_verdicts"])[:-1]

        incomplete = run_ship_gate(rows, **incomplete_kwargs)

        assert incomplete.audit_ok is False
        assert incomplete.audit.status == "incomplete"
        assert incomplete.ship is False

        below_kwargs = _ship_gate_kwargs(rows)
        below_verdicts = [dict(row) for row in below_kwargs["audit_verdicts"]]
        for row in below_verdicts[:11]:
            row["human_verdict"] = not row["human_verdict"]
        below_kwargs["audit_verdicts"] = below_verdicts

        below = run_ship_gate(rows, **below_kwargs)

        assert below.audit_ok is False
        assert below.audit.status == "below_threshold"
        assert below.audit.agreement == pytest.approx(0.78)
        assert below.ship is False

    def test_rejects_stale_prompt_or_digest_mismatch(self) -> None:
        rows = _planted_requests(
            120,
            rel_raw=0.75,
            rel_decay=0.6,
            irr_raw=0.5,
            irr_decay=0.95,
        )
        stale_kwargs = _ship_gate_kwargs(rows)
        stale_verdicts = [dict(row) for row in stale_kwargs["audit_verdicts"]]
        stale_verdicts[0]["prompt_hash"] = "stale-prompt"
        stale_kwargs["audit_verdicts"] = stale_verdicts

        stale = run_ship_gate(rows, **stale_kwargs)

        assert stale.audit.status == "stale_prompt_hash"
        assert stale.ship is False

        digest_kwargs = _ship_gate_kwargs(rows)
        digest_verdicts = [dict(row) for row in digest_kwargs["audit_verdicts"]]
        digest_verdicts[0]["sample_digest"] = "wrong-sample"
        digest_kwargs["audit_verdicts"] = digest_verdicts

        mismatched = run_ship_gate(rows, **digest_kwargs)

        assert mismatched.audit.status == "digest_mismatch"
        assert mismatched.ship is False

    def test_decision_digest_and_cohort_record_are_canonical(self) -> None:
        rows = _planted_requests(
            120,
            rel_raw=0.75,
            rel_decay=0.6,
            irr_raw=0.5,
            irr_decay=0.95,
        )
        decision = run_ship_gate(rows, **_ship_gate_kwargs(rows))

        record = decision.to_record()
        decision_digest = record.pop("decision_digest")
        expected = hashlib.sha256(
            json.dumps(
                record,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()

        assert decision_digest == expected
        assert record["decision_schema_version"] == "recall-ship-decision-v2"
        assert record["cohort_identity"] == _gate_cohort().identity()
        assert record["audit"]["unit_count"] == 50
        assert record["audit"]["status"] == "passed"

    def test_reject_insufficient_data_with_default_floors(self) -> None:
        rows = _planted_requests(12, rel_raw=0.75, rel_decay=0.6, irr_raw=0.5, irr_decay=0.95)
        decision = run_ship_gate(rows, **_ship_gate_kwargs(rows))
        assert decision.report.fitted.pooled_pairs < MIN_TRAIN_PAIRS
        assert decision.static_eval.pair_count < MIN_EVAL_PAIRS
        assert decision.sufficient_data is False
        assert decision.ship is False
        assert any("insufficient labeled data" in reason for reason in decision.reasons)

    def test_rejects_when_raw_pair_floor_passes_but_mixed_request_floor_fails(self) -> None:
        rows = _planted_requests(
            120,
            rel_raw=0.75,
            rel_decay=0.6,
            irr_raw=0.5,
            irr_decay=0.95,
        )
        train, _evaluation = split_requests_per_project(rows)
        mixed_train_ids = sorted({row.recall_request_id for row in train})[:19]
        mixed_rows = [
            replace(row, judge_useful=False)
            if row.judge_useful is True and row.recall_request_id not in mixed_train_ids
            else row
            for row in rows
        ]

        decision = run_ship_gate(
            mixed_rows,
            **_ship_gate_kwargs(mixed_rows),
            min_train_pairs=1,
            min_eval_pairs=1,
        )

        assert decision.report.fitted.pooled_pairs == 19
        assert decision.report.fitted.pooled_mixed_requests == 19
        assert decision.sufficient_data is False
        assert decision.ship is False

    def test_reject_guard_regression_on_recency_hacked_labels(self) -> None:
        # Labels systematically favor recency: fresh mediocre useful, staler
        # stronger not-useful. The grid's best judge-label fit is h=7, which
        # violates the constructed stale-relevant prior (guard pair T2) — the
        # reward-hacking gate must reject even though the holdout improves.
        rows = _planted_requests(120, rel_raw=0.6, rel_decay=0.9, irr_raw=0.8, irr_decay=0.7)
        decision = run_ship_gate(rows, **_ship_gate_kwargs(rows))
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
        rows = _planted_requests(120, rel_raw=0.9, rel_decay=0.9, irr_raw=0.3, irr_decay=0.9)
        decision = run_ship_gate(rows, **_ship_gate_kwargs(rows))
        assert decision.report.fitted.pooled == ReplayParams()
        assert decision.beats_static is False
        assert decision.guard_ok is True
        assert decision.ship is False
        assert any("do not beat the static constants" in r for r in decision.reasons)

    def test_static_eval_scores_the_same_holdout_as_fitted(self) -> None:
        rows = _planted_requests(120, rel_raw=0.75, rel_decay=0.6, irr_raw=0.5, irr_decay=0.95)
        decision = run_ship_gate(rows, **_ship_gate_kwargs(rows))
        assert decision.static_eval.pair_count == decision.report.fitted_eval.pair_count
        assert decision.static_eval.weighted_pair_count == pytest.approx(
            decision.report.fitted_eval.weighted_pair_count
        )

    def test_to_record_is_json_serializable_and_complete(self) -> None:
        rows = _planted_requests(120, rel_raw=0.75, rel_decay=0.6, irr_raw=0.5, irr_decay=0.95)
        decision = run_ship_gate(rows, **_ship_gate_kwargs(rows))
        record = decision.to_record()
        parsed = json.loads(json.dumps(record))
        assert parsed["task"] == "#18426"
        assert parsed["label_source"] == "digest_shadow"
        assert parsed["cohort_identity"] == _gate_cohort().identity()
        assert parsed["ship"] is True
        assert parsed["fitted_params"]["half_life_days"] == 60.0
        assert parsed["static_params"]["half_life_days"] == 30.0
        assert parsed["gates"] == {
            "sufficient_data": True,
            "beats_static": True,
            "guard_ok": True,
            "audit_ok": True,
        }
        assert parsed["audit"]["status"] == "passed"
        assert parsed["decision_digest"]
        assert parsed["fitted_eval"]["accuracy"] == 1.0
        assert parsed["train_pairs"] == parsed["train_mixed_requests"]
        assert parsed["fitted_eval"]["pair_count"] == parsed["fitted_eval"]["mixed_request_count"]
        assert parsed["static_eval"]["accuracy"] == 0.0
        assert parsed["reasons"]

    def test_empty_rows_reject_cleanly(self) -> None:
        decision = run_ship_gate([], **_ship_gate_kwargs([]))
        assert isinstance(decision, GateDecision)
        assert decision.ship is False
        assert decision.sufficient_data is False
        assert decision.report.rows_total == 0
