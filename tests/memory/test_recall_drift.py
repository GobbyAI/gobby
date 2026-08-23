"""Tests for #17201: recall-quality drift detection + regression alarm.

The core validation-criteria scenario: a window with an injected regression
(useful rows sorting below not-useful rows under the effective constants)
must fire the alarm against the recorded holdout baseline, and the alarm's
response path must name the #17200 one-flag rollback
(``memory.use_fitted_recall_constants=false``).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from gobby.config.app import DaemonConfig
from gobby.config.persistence import MemoryConfig
from gobby.memory.recall_constants import RecallConstants
from gobby.memory.recall_drift import (
    DriftThresholds,
    evaluate_recall_drift,
    replay_params_from_constants,
    run_drift_check_from_store,
)
from gobby.memory.recall_fit import ReplayRow, evaluation_protocol_identity
from gobby.runner_maintenance import recall_drift_monitor_loop
from tests.config_runtime_helpers import static_runtime_capture

FITTED_PARAMS: dict[str, Any] = {
    "half_life_days": 21.0,
    "graph_synthetic_discount": 0.85,
    "cooccur_alpha": 0.6,
    "cooccur_support_cap": 4,
}

FITTED = RecallConstants(
    half_life_days=21.0,
    graph_synthetic_discount=0.85,
    cooccur_alpha=0.6,
    cooccur_support_cap=4,
    source="fitted",
    provenance="decision-digest-123",
)

STATIC = RecallConstants(
    half_life_days=30.0,
    graph_synthetic_discount=0.9,
    cooccur_alpha=0.5,
    cooccur_support_cap=5,
    source="static",
    provenance="static",
    reason="use_fitted_recall_constants disabled",
)


def _row(
    request_id: str,
    memory_id: str,
    *,
    useful: bool | None,
    similarity: float,
    position: int,
) -> ReplayRow:
    return ReplayRow(
        recall_request_id=request_id,
        memory_id=memory_id,
        project_id="proj-1",
        rank=0,
        similarity=similarity,
        raw_semantic_score=similarity,
        temporal_decay_factor=1.0,
        ranking_score=similarity,
        ranking_mode="rrf",
        graph_score=None,
        edge_cosine=None,
        edge_support_norm=None,
        edge_weight_blend=None,
        injection_position=position,
        injection_group="context",
        judge_useful=useful,
        label_source="digest" if useful is not None else None,
        logged_half_life_days=30.0,
        logged_graph_discount=None,
    )


def _window(n_requests: int, *, regressed_requests: int = 0) -> list[ReplayRow]:
    """One preference pair per request; regressed pairs sort inverted."""
    rows: list[ReplayRow] = []
    for i in range(n_requests):
        request_id = f"req-{i:03d}"
        inverted = i < regressed_requests
        useful_sim = 0.2 if inverted else 0.9
        useless_sim = 0.9 if inverted else 0.2
        rows.append(_row(request_id, "mem-good", useful=True, similarity=useful_sim, position=0))
        rows.append(_row(request_id, "mem-bad", useful=False, similarity=useless_sim, position=1))
    return rows


def gate_record(
    *,
    ship: bool = True,
    fitted_accuracy: float = 0.9,
    static_accuracy: float = 0.85,
    holdout_pairs: int = 40,
    decision_digest: str = "decision-digest-123",
    evaluator_version: str | None = None,
) -> dict[str, Any]:
    """A minimal GateDecision.to_record() shape with both holdout eval blocks."""

    def eval_block(accuracy: float) -> dict[str, Any]:
        return {
            "pair_count": holdout_pairs,
            "weighted_pair_count": float(holdout_pairs),
            "accuracy": accuracy,
            "per_project": {},
        }

    evaluation_identity = evaluation_protocol_identity()
    if evaluator_version is not None:
        evaluation_identity["evaluator_version"] = evaluator_version
    return {
        "task": "#18426",
        "decision_schema_version": "recall-ship-decision-v2",
        "label_source": "digest_shadow",
        "cohort_identity": {
            "label_source": "digest_shadow",
            "candidate_scope": "full",
            "judge_protocol_version": "shadow-protocol-v1",
            "query_construction_version": "nl-embed-v1",
            "weighting_regime_key": "rrf:1|graph:1",
            "judge_model_key": "judge/model-v1",
            "judge_config_fingerprint": "judge-config-v1",
            "data_cutoff": "2026-07-01T00:00:00+00:00",
            "completion_cutoff": "2026-07-02T00:00:00+00:00",
            "project_id": None,
            "weighting_mode": "full",
            **evaluation_identity,
        },
        "fitted_params": dict(FITTED_PARAMS),
        "static_params": {
            "half_life_days": 30.0,
            "graph_synthetic_discount": 0.9,
            "cooccur_alpha": 0.5,
            "cooccur_support_cap": 5,
        },
        "fitted_eval": eval_block(fitted_accuracy),
        "static_eval": eval_block(static_accuracy),
        "gates": {"sufficient_data": ship, "beats_static": ship, "guard_ok": True},
        "ship": ship,
        "reasons": [],
        "decision_digest": decision_digest,
    }


class TestRegressionAlarm:
    def test_injected_regression_fires_alarm(self) -> None:
        rows = _window(25, regressed_requests=25)

        report = evaluate_recall_drift(rows, record=gate_record(), constants=FITTED)

        assert report.status == "regressed"
        assert report.alarm is True
        assert report.live_accuracy == pytest.approx(0.0)
        assert report.baseline_accuracy == pytest.approx(0.9)
        assert any("fell" in reason for reason in report.reasons)

    def test_alarm_response_is_the_one_flag_rollback(self) -> None:
        rows = _window(25, regressed_requests=25)

        report = evaluate_recall_drift(rows, record=gate_record(), constants=FITTED)

        assert "use_fitted_recall_constants=false" in report.response
        assert "run_ship_gate_from_store" in report.response

    def test_healthy_window_does_not_alarm(self) -> None:
        rows = _window(25)

        report = evaluate_recall_drift(rows, record=gate_record(), constants=FITTED)

        assert report.status == "ok"
        assert report.alarm is False
        assert report.live_accuracy == pytest.approx(1.0)
        assert report.live_pair_count == 25

    def test_drop_within_threshold_is_ok(self) -> None:
        # 22/25 correct = 0.88 live vs 0.9 baseline: drop 0.02 <= 0.05.
        rows = _window(25, regressed_requests=3)

        report = evaluate_recall_drift(rows, record=gate_record(), constants=FITTED)

        assert report.live_accuracy == pytest.approx(0.88)
        assert report.status == "ok"
        assert report.alarm is False

    def test_static_regime_alarm_points_at_ship_gate_not_flag(self) -> None:
        rows = _window(25, regressed_requests=25)

        report = evaluate_recall_drift(rows, record=gate_record(), constants=STATIC)

        assert report.status == "regressed"
        assert report.alarm is True
        # Static baseline comes from the record's static holdout arm.
        assert report.baseline_accuracy == pytest.approx(0.85)
        assert "no flag to flip" in report.response
        assert "run_ship_gate_from_store" in report.response

    def test_custom_threshold_is_honored(self) -> None:
        rows = _window(25, regressed_requests=3)  # live 0.88, drop 0.02

        report = evaluate_recall_drift(
            rows,
            record=gate_record(),
            constants=FITTED,
            thresholds=DriftThresholds(accuracy_drop=0.01),
        )

        assert report.status == "regressed"
        assert report.alarm is True


class TestFloors:
    def test_starved_live_window_never_alarms(self) -> None:
        rows = _window(5, regressed_requests=5)

        report = evaluate_recall_drift(rows, record=gate_record(), constants=FITTED)

        assert report.status == "insufficient_data"
        assert report.alarm is False
        assert report.live_pair_count == 5
        assert report.live_mixed_request_count == 5
        assert any("not enough signal" in reason for reason in report.reasons)

    def test_missing_record_reports_no_baseline(self) -> None:
        rows = _window(25, regressed_requests=25)

        report = evaluate_recall_drift(
            rows,
            record=None,
            constants=STATIC,
            record_load_error="no gate decision record at /tmp/missing.json",
        )

        assert report.status == "no_baseline"
        assert report.alarm is False
        assert "no gate decision record at /tmp/missing.json" in report.reasons

    def test_data_starved_reject_record_gives_no_baseline(self) -> None:
        # The real-world #17198 outcome: reject with a 0-pair holdout.
        record = gate_record(ship=False, holdout_pairs=0)
        rows = _window(25, regressed_requests=25)

        report = evaluate_recall_drift(rows, record=record, constants=STATIC)

        assert report.status == "no_baseline"
        assert report.alarm is False
        assert any("need 20" in reason for reason in report.reasons)

    def test_unshipped_record_gives_no_fitted_baseline(self) -> None:
        record = gate_record(ship=False)
        rows = _window(25, regressed_requests=25)

        report = evaluate_recall_drift(rows, record=record, constants=FITTED)

        assert report.status == "no_baseline"
        assert any("did not ship" in reason for reason in report.reasons)

    def test_non_finite_baseline_accuracy_gives_no_baseline(self) -> None:
        record = gate_record()
        record["fitted_eval"]["accuracy"] = float("nan")
        rows = _window(25)

        report = evaluate_recall_drift(rows, record=record, constants=FITTED)

        assert report.status == "no_baseline"
        assert any("not a finite number" in reason for reason in report.reasons)


class TestReportShape:
    def test_to_record_is_json_serializable(self) -> None:
        rows = _window(25, regressed_requests=25)

        record = evaluate_recall_drift(
            rows,
            record=gate_record(),
            constants=FITTED,
            label_source="digest_shadow",
        ).to_record()

        parsed = json.loads(json.dumps(record))
        assert parsed["task"] == "#17201"
        assert parsed["alarm"] is True
        assert parsed["constants_source"] == "fitted"
        assert parsed["label_source"] == "digest_shadow"
        assert parsed["live_mixed_request_count"] == 25
        assert parsed["min_mixed_requests"] == 10
        assert parsed["response"]
        assert parsed["reasons"]

    def test_replay_params_match_effective_constants(self) -> None:
        params = replay_params_from_constants(FITTED)

        assert params.half_life_days == FITTED.half_life_days
        assert params.graph_synthetic_discount == FITTED.graph_synthetic_discount
        assert params.cooccur_alpha == FITTED.cooccur_alpha
        assert params.cooccur_support_cap == FITTED.cooccur_support_cap

    def test_empty_window_reports_none_live_accuracy(self) -> None:
        report = evaluate_recall_drift([], record=gate_record(), constants=FITTED)

        assert report.live_accuracy is None
        assert report.live_pair_count == 0
        assert report.status == "insufficient_data"


def _signal_row(
    request_id: str,
    memory_id: str,
    *,
    useful: bool | None,
    similarity: float,
    position: int,
) -> dict[str, Any]:
    """One fetch_replay_rows-shaped dict for the store-level runner."""
    return {
        "recall_request_id": request_id,
        "memory_id": memory_id,
        "project_id": "proj-1",
        "rank": 0,
        "similarity": similarity,
        "raw_semantic_score": similarity,
        "temporal_decay_factor": 1.0,
        "ranking_score": similarity,
        "ranking_mode": "rrf",
        "graph_score": None,
        "edge_cosine": None,
        "edge_support_norm": None,
        "edge_weight_blend": None,
        "injection_position": position,
        "injection_group": "context",
        "judge_useful": useful,
        "label_source": "digest" if useful is not None else None,
        "weighting": {"temporal_decay_half_life_days": 30.0},
        "graph_synthetic_similarity_discount": 0.9,
    }


def _regressed_signal_rows(n_requests: int = 25) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i in range(n_requests):
        request_id = f"req-{i:03d}"
        rows.append(_signal_row(request_id, "mem-good", useful=True, similarity=0.2, position=0))
        rows.append(_signal_row(request_id, "mem-bad", useful=False, similarity=0.9, position=1))
    return rows


class FakeStore:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    def fetch_shadow_replay_rows(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        return self.rows


class TestRunFromStore:
    def _config(self, tmp_path: Any) -> MemoryConfig:
        record_path = tmp_path / "decision.json"
        record_path.write_text(json.dumps(gate_record()), encoding="utf-8")
        return MemoryConfig(
            use_fitted_recall_constants=True,
            fitted_recall_decision_path=str(record_path),
        )

    def test_regression_alarm_fires_and_logs_response(
        self, tmp_path: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = FakeStore(_regressed_signal_rows())
        config = self._config(tmp_path)

        with caplog.at_level(logging.WARNING, logger="gobby.memory.recall_drift"):
            report = run_drift_check_from_store(store, config)

        assert report.alarm is True
        assert report.constants_source == "fitted"
        alarm_logs = [r for r in caplog.records if "drift alarm" in r.getMessage()]
        assert alarm_logs
        assert "use_fitted_recall_constants=false" in alarm_logs[0].getMessage()
        # Drift replays the shipped cohort, so it reads the shipped query era.
        assert store.calls[0]["query_construction_version"] == "nl-embed-v1"

    def test_cohort_and_provenance_come_from_shipped_record(self, tmp_path: Any) -> None:
        store = FakeStore([])
        record_path = tmp_path / "decision.json"
        record_path.write_text(json.dumps(gate_record()), encoding="utf-8")
        config = MemoryConfig(
            use_fitted_recall_constants=True,
            fitted_recall_decision_path=str(record_path),
            recall_drift_accuracy_drop=0.2,
        )

        report = run_drift_check_from_store(store, config)

        call = store.calls[0]
        assert call["phase"] == "drift"
        assert call["label_source"] == "digest_shadow"
        assert call["candidate_scope"] == "full"
        assert call["judge_protocol_version"] == "shadow-protocol-v1"
        assert call["weighting_regime_key"] == "rrf:1|graph:1"
        assert call["judge_model_key"] == "judge/model-v1"
        assert call["judge_config_fingerprint"] == "judge-config-v1"
        assert call["constants_provenance"] == "decision-digest-123"
        assert call["project_id"] is None
        assert "since" not in call
        assert report.accuracy_drop == 0.2
        assert report.status == "insufficient_data"

    def test_below_mixed_request_floor_is_insufficient_data(self, tmp_path: Any) -> None:
        store = FakeStore(_regressed_signal_rows(9))
        report = run_drift_check_from_store(store, self._config(tmp_path))

        assert report.status == "insufficient_data"
        assert report.live_mixed_request_count == 9
        assert report.alarm is False

    def test_mismatched_evaluator_version_is_idle(self, tmp_path: Any) -> None:
        store = FakeStore(_regressed_signal_rows())
        record_path = tmp_path / "decision.json"
        record_path.write_text(
            json.dumps(gate_record(evaluator_version="obsolete-evaluator")),
            encoding="utf-8",
        )
        config = MemoryConfig(
            use_fitted_recall_constants=True,
            fitted_recall_decision_path=str(record_path),
        )

        report = run_drift_check_from_store(store, config)

        assert report.status == "idle"
        assert report.alarm is False
        assert store.calls == []
        assert any("evaluator" in reason for reason in report.reasons)

    def test_static_regime_without_record_reports_no_baseline(self, tmp_path: Any) -> None:
        store = FakeStore(_regressed_signal_rows())
        config = MemoryConfig(
            use_fitted_recall_constants=False,
            fitted_recall_decision_path=str(tmp_path / "missing.json"),
        )

        report = run_drift_check_from_store(store, config)

        assert report.constants_source == "static"
        assert report.status == "idle"
        assert report.alarm is False
        assert store.calls == []


@pytest.mark.asyncio
async def test_monitor_loop_runs_check_until_shutdown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    calls: list[Any] = []

    def fake_check(store: Any, config: Any, **kwargs: Any) -> Any:
        calls.append(config)
        return None

    monkeypatch.setattr("gobby.memory.recall_drift.run_drift_check_from_store", fake_check)
    config = MemoryConfig()

    await recall_drift_monitor_loop(
        object(),
        lambda: bool(calls),
        capture_bundle=static_runtime_capture(DaemonConfig(memory=config)),
        interval_seconds=0.01,
    )

    assert calls == [config]
