"""Tests for the recall-signal hub tables (#17196).

Covers contract docs/contracts/memory-usefulness-label.md: promoted signal
rows (§3), injection outcomes (§5), usefulness labels (§6), and the
fit-eligible join on (recall_request_id, memory_id).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from gobby.storage.recall_signals import RecallSignalStore

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.integration


@pytest.fixture
def store(temp_db: HubDatabase) -> RecallSignalStore:
    return RecallSignalStore(temp_db)


def _event(request_id: str = "req-1", session_id: str = "sess-1") -> dict[str, object]:
    return {
        "schema_version": 3,
        "timestamp": "2026-07-09T12:00:00+00:00",
        "session_id": session_id,
        "recall_request_id": request_id,
        "project_id": "proj-1",
        "caller": "memory.recall",
        "query": "how does dispatch work",
        "merged_ids": ["mem-1", "mem-2"],
        "returned_ids": ["mem-1", "mem-2"],
        "rrf_applied": True,
        "graph_synthetic_similarity_discount": 0.9,
        "ranking_score_map": {"mem-1": 0.9, "mem-2": 0.4},
        "graph_score_map": {"mem-2": 0.8},
        "weighting": {"graph_edge_weighting": True},
        "hits": [
            {
                "memory_id": "mem-1",
                "rank": 0,
                "search_via": "semantic",
                "similarity": 0.9,
                "raw_semantic_score": 0.9,
                "temporal_decay_factor": 1.0,
                "ranking_score": 0.9,
                "ranking_mode": "rrf",
                "graph_score": None,
                "edge_cosine": None,
                "edge_support_norm": None,
                "edge_weight_blend": None,
                "edge_decay_factor": None,
            },
            {
                "memory_id": "mem-2",
                "rank": 1,
                "search_via": "graph",
                "similarity": 0.72,
                "raw_semantic_score": None,
                "temporal_decay_factor": 1.0,
                "ranking_score": 0.4,
                "ranking_mode": "graph_synthetic",
                "graph_score": 0.8,
                "edge_cosine": 0.8,
                "edge_support_norm": 0.6,
                "edge_weight_blend": 0.7,
                "edge_decay_factor": 1.0,
            },
        ],
    }


class TestInsertSignalEvent:
    def test_populates_request_and_hit_rows(self, store: RecallSignalStore) -> None:
        assert store.insert_signal_event(_event()) is True

        request = store.db.fetchone(
            "SELECT * FROM recall_signal_requests WHERE recall_request_id = %s",
            ("req-1",),
        )
        assert request is not None
        assert request["session_id"] == "sess-1"
        assert request["caller"] == "memory.recall"
        # The hub layer may return JSONB as its serialized text form.
        assert json.loads(request["merged_ids"]) == ["mem-1", "mem-2"]
        assert json.loads(request["returned_ids"]) == ["mem-1", "mem-2"]
        assert request["rrf_applied"] is True
        assert request["schema_version"] == 3

        hits = store.db.fetchall(
            "SELECT * FROM recall_signal_hits WHERE recall_request_id = %s ORDER BY rank",
            ("req-1",),
        )
        assert len(hits) == 2
        assert hits[0]["memory_id"] == "mem-1"
        assert hits[0]["edge_cosine"] is None
        # §3.2 edge-component breakdown survives promotion.
        assert hits[1]["memory_id"] == "mem-2"
        assert hits[1]["edge_cosine"] == pytest.approx(0.8)
        assert hits[1]["edge_support_norm"] == pytest.approx(0.6)
        assert hits[1]["edge_weight_blend"] == pytest.approx(0.7)
        assert hits[1]["edge_decay_factor"] == pytest.approx(1.0)

    def test_replay_is_idempotent(self, store: RecallSignalStore) -> None:
        assert store.insert_signal_event(_event()) is True
        assert store.insert_signal_event(_event()) is False

        count = store.db.fetchone(
            "SELECT count(*) AS n FROM recall_signal_hits WHERE recall_request_id = %s",
            ("req-1",),
        )
        assert count is not None and count["n"] == 2

    def test_rejects_events_without_join_key(self, store: RecallSignalStore) -> None:
        event = _event()
        event.pop("session_id")
        assert store.insert_signal_event(event) is False


class TestInjectionOutcomes:
    def test_records_injected_with_position_and_group(self, store: RecallSignalStore) -> None:
        inserted = store.record_injection_outcomes(
            [
                {
                    "session_id": "sess-1",
                    "recall_request_id": "req-1",
                    "memory_id": "mem-1",
                    "project_id": "proj-1",
                    "outcome": "injected",
                    "injection_position": 0,
                    "injection_group": "fact",
                    "turn_seq": 7,
                    "caller": "memory.recall",
                },
                {
                    "session_id": "sess-1",
                    "recall_request_id": "req-1",
                    "memory_id": "mem-2",
                    "outcome": "filtered",
                    "drop_reason": "already_injected",
                    "caller": "memory.recall",
                },
            ]
        )
        assert inserted == 2

        rows = store.db.fetchall(
            "SELECT * FROM recall_injection_outcomes WHERE recall_request_id = %s "
            "ORDER BY memory_id",
            ("req-1",),
        )
        assert rows[0]["outcome"] == "injected"
        assert rows[0]["injection_position"] == 0
        assert rows[0]["injection_group"] == "fact"
        assert rows[0]["turn_seq"] == 7
        assert rows[0]["drop_reason"] is None
        assert rows[1]["outcome"] == "filtered"
        assert rows[1]["drop_reason"] == "already_injected"
        assert rows[1]["injection_position"] is None

    def test_first_write_wins_per_request_memory(self, store: RecallSignalStore) -> None:
        row = {
            "session_id": "sess-1",
            "recall_request_id": "req-1",
            "memory_id": "mem-1",
            "outcome": "injected",
            "injection_position": 0,
            "caller": "memory.recall",
        }
        assert store.record_injection_outcomes([row]) == 1
        assert store.record_injection_outcomes([{**row, "injection_position": 5}]) == 0

    def test_unknown_drop_reason_coerced_to_other(self, store: RecallSignalStore) -> None:
        store.record_injection_outcomes(
            [
                {
                    "session_id": "sess-1",
                    "recall_request_id": "req-1",
                    "memory_id": "mem-1",
                    "outcome": "filtered",
                    "drop_reason": "not_a_reason",
                    "drop_detail": "selector_not_selected",
                }
            ]
        )
        row = store.db.fetchone(
            "SELECT drop_reason, drop_detail FROM recall_injection_outcomes "
            "WHERE recall_request_id = %s",
            ("req-1",),
        )
        assert row is not None
        assert row["drop_reason"] == "other"
        assert row["drop_detail"] == "selector_not_selected"

    def test_invalid_rows_skipped(self, store: RecallSignalStore) -> None:
        inserted = store.record_injection_outcomes(
            [
                {"session_id": "sess-1", "outcome": "injected"},
                {
                    "session_id": "sess-1",
                    "recall_request_id": "req-1",
                    "memory_id": "mem-1",
                    "outcome": "banana",
                },
            ]
        )
        assert inserted == 0


class TestUsefulnessLabels:
    def _label(self) -> dict[str, object]:
        return {
            "session_id": "sess-1",
            "recall_request_id": "req-1",
            "memory_id": "mem-1",
            "project_id": "proj-1",
            "label_source": "llm_judge",
            "judge_useful": True,
            "judge_confidence": 0.9,
            "judge_model": "gemma4:31b",
            "judge_protocol_version": "17193-v1",
            "position_randomized": True,
            "length_controlled": True,
            "rationale": "memory named the exact flag",
            "labeled_at": "2026-07-09T12:34:56+00:00",
        }

    def test_append_and_unique_key(self, store: RecallSignalStore) -> None:
        assert store.insert_usefulness_label(self._label()) is True
        # Same (request, memory, source, protocol) is a no-op.
        assert store.insert_usefulness_label(self._label()) is False
        # A new protocol version appends instead of mutating (§6).
        relabel = {**self._label(), "judge_protocol_version": "17193-v2", "judge_useful": False}
        assert store.insert_usefulness_label(relabel) is True

        rows = store.db.fetchall(
            "SELECT judge_protocol_version, judge_useful FROM recall_usefulness "
            "WHERE recall_request_id = %s ORDER BY judge_protocol_version",
            ("req-1",),
        )
        assert [(r["judge_protocol_version"], r["judge_useful"]) for r in rows] == [
            ("17193-v1", True),
            ("17193-v2", False),
        ]

    def test_rejects_invalid_source_and_missing_fields(self, store: RecallSignalStore) -> None:
        assert store.insert_usefulness_label({**self._label(), "label_source": "vibes"}) is False
        missing = self._label()
        missing.pop("judge_protocol_version")
        assert store.insert_usefulness_label(missing) is False


class TestFitRowJoin:
    def test_join_on_recall_request_id_and_memory_id(self, store: RecallSignalStore) -> None:
        store.insert_signal_event(_event())
        store.record_injection_outcomes(
            [
                {
                    "session_id": "sess-1",
                    "recall_request_id": "req-1",
                    "memory_id": "mem-2",
                    "outcome": "injected",
                    "injection_position": 0,
                    "injection_group": "fact",
                    "caller": "memory.recall",
                },
                {
                    "session_id": "sess-1",
                    "recall_request_id": "req-1",
                    "memory_id": "mem-1",
                    "outcome": "filtered",
                    "drop_reason": "already_injected",
                    "caller": "memory.recall",
                },
            ]
        )
        for memory_id in ("mem-1", "mem-2"):
            store.insert_usefulness_label(
                {
                    "session_id": "sess-1",
                    "recall_request_id": "req-1",
                    "memory_id": memory_id,
                    "label_source": "llm_judge",
                    "judge_useful": memory_id == "mem-2",
                    "judge_protocol_version": "17193-v1",
                    "position_randomized": True,
                    "length_controlled": True,
                    "labeled_at": "2026-07-09T13:00:00+00:00",
                }
            )

        rows = store.fetch_fit_rows(recall_request_id="req-1")

        # Only the injected memory is fit-eligible; the filtered one informs
        # propensity estimation only (contract §4).
        assert len(rows) == 1
        row = rows[0]
        assert row["memory_id"] == "mem-2"
        assert row["injection_position"] == 0
        assert row["injection_group"] == "fact"
        assert row["judge_useful"] is True
        assert row["edge_cosine"] == pytest.approx(0.8)
        assert row["rrf_applied"] is True
        assert row["weighting"] == {"graph_edge_weighting": True}

    def test_project_filter(self, store: RecallSignalStore) -> None:
        store.insert_signal_event(_event())
        rows = store.fetch_fit_rows(project_id="other-project")
        assert rows == []


class TestReplayRowJoin:
    """fetch_replay_rows: the #17197 harness loader (LEFT JOIN labels)."""

    def _seed(self, store: RecallSignalStore) -> None:
        store.insert_signal_event(_event())
        store.record_injection_outcomes(
            [
                {
                    "session_id": "sess-1",
                    "recall_request_id": "req-1",
                    "memory_id": "mem-1",
                    "outcome": "injected",
                    "injection_position": 1,
                    "injection_group": "context",
                    "caller": "memory.recall",
                },
                {
                    "session_id": "sess-1",
                    "recall_request_id": "req-1",
                    "memory_id": "mem-2",
                    "outcome": "injected",
                    "injection_position": 0,
                    "injection_group": "fact",
                    "caller": "memory.recall",
                },
            ]
        )
        # Only mem-2 is labeled, and only in the digest stream.
        store.insert_usefulness_label(
            {
                "session_id": "sess-1",
                "recall_request_id": "req-1",
                "memory_id": "mem-2",
                "label_source": "digest",
                "judge_useful": True,
                "judge_protocol_version": "17195-digest-v1",
                "position_randomized": False,
                "length_controlled": False,
                "labeled_at": "2026-07-09T13:00:00+00:00",
            }
        )

    def test_returns_unlabeled_injected_rows_with_null_label(
        self, store: RecallSignalStore
    ) -> None:
        self._seed(store)

        rows = store.fetch_replay_rows(label_source="digest")

        # Both injected hits come back — the unlabeled one feeds propensity
        # denominators (never-retrieved/unlabeled ≠ negative).
        assert [row["memory_id"] for row in rows] == ["mem-1", "mem-2"]
        unlabeled, labeled = rows
        assert unlabeled["judge_useful"] is None
        assert unlabeled["label_source"] is None
        assert unlabeled["injection_position"] == 1
        assert labeled["judge_useful"] is True
        assert labeled["label_source"] == "digest"
        assert labeled["injection_group"] == "fact"
        # Request-level replay context is attached.
        assert labeled["weighting"] == {"graph_edge_weighting": True}
        assert labeled["graph_synthetic_similarity_discount"] == pytest.approx(0.9)

    def test_label_streams_stay_separable(self, store: RecallSignalStore) -> None:
        self._seed(store)

        rows = store.fetch_replay_rows(label_source="llm_judge")

        # Digest labels never leak into a judge-stream fit: same feature
        # rows, zero labels.
        assert len(rows) == 2
        assert all(row["judge_useful"] is None for row in rows)

    def test_latest_label_wins_within_stream(self, store: RecallSignalStore) -> None:
        self._seed(store)
        store.insert_usefulness_label(
            {
                "session_id": "sess-1",
                "recall_request_id": "req-1",
                "memory_id": "mem-2",
                "label_source": "digest",
                "judge_useful": False,
                "judge_protocol_version": "17195-digest-v2",
                "position_randomized": False,
                "length_controlled": False,
                "labeled_at": "2026-07-10T13:00:00+00:00",
            }
        )

        rows = store.fetch_replay_rows(label_source="digest")

        labeled = [row for row in rows if row["memory_id"] == "mem-2"]
        assert len(labeled) == 1
        assert labeled[0]["judge_useful"] is False
        assert labeled[0]["judge_protocol_version"] == "17195-digest-v2"

    def test_since_bounds_the_replay_window(self, store: RecallSignalStore) -> None:
        # The #17201 drift monitor replays only the recent live window.
        self._seed(store)

        before_seed = datetime(2026, 7, 1, tzinfo=UTC)
        after_seed = datetime(2026, 7, 10, tzinfo=UTC)

        assert len(store.fetch_replay_rows(label_source="digest", since=before_seed)) == 2
        assert store.fetch_replay_rows(label_source="digest", since=after_seed) == []

    def test_excludes_filtered_outcomes_and_scopes_by_project(
        self, store: RecallSignalStore
    ) -> None:
        store.insert_signal_event(_event())
        store.record_injection_outcomes(
            [
                {
                    "session_id": "sess-1",
                    "recall_request_id": "req-1",
                    "memory_id": "mem-1",
                    "outcome": "filtered",
                    "drop_reason": "already_injected",
                    "caller": "memory.recall",
                },
            ]
        )

        assert store.fetch_replay_rows(label_source="digest") == []
        assert store.fetch_replay_rows(label_source="digest", project_id="proj-1") == []


class TestJsonlBackfill:
    def test_load_signal_events_jsonl(self, store: RecallSignalStore, tmp_path: Path) -> None:
        path = tmp_path / "recall_signal.jsonl"
        lines = [
            json.dumps(_event("req-1")),
            "not-json",
            json.dumps({"schema_version": 2}),  # missing join keys → skipped
            json.dumps(_event("req-2")),
            json.dumps(_event("req-1")),  # replay → skipped
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        assert store.load_signal_events_jsonl(path) == 2

    def test_backfill_usefulness_labels_maps_retro_id(
        self, store: RecallSignalStore, tmp_path: Path
    ) -> None:
        path = tmp_path / "calibration-dataset.jsonl"
        row = {
            "retro_id": "retro:sess-9:8",
            "session_id": "sess-9",
            "memory_id": "mem-9",
            "project_id": "proj-1",
            "label_source": "llm_judge",
            "judge_useful": False,
            "judge_confidence": 1.0,
            "judge_model": "gemma4:31b",
            "judge_protocol_version": "17193-v1",
            "position_randomized": True,
            "length_controlled": True,
            "timestamp": "2026-06-10T19:04:13.320Z",
            "feature_extractor_version": "retro-v1",
        }
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")

        assert store.backfill_usefulness_labels_jsonl(path) == 1

        stored = store.db.fetchone(
            "SELECT recall_request_id, judge_useful, feature_extractor_version "
            "FROM recall_usefulness WHERE memory_id = %s",
            ("mem-9",),
        )
        assert stored is not None
        assert stored["recall_request_id"] == "retro:sess-9:8"
        assert stored["judge_useful"] is False
        assert stored["feature_extractor_version"] == "retro-v1"
