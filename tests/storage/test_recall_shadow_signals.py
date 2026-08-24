"""Shadow-cohort tests for the recall-signal hub tables.

Split from :mod:`tests.storage.test_recall_signals` (#20776): replay
projection, audit sampling, human verdicts, gate holdout reservation, and the
claim state machine. Cohort admission and the query-construction fence stay in
the original module beside the non-shadow join tests.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import TYPE_CHECKING, Any

import pytest

from gobby.storage.recall_signals import RecallSignalStore
from tests.storage.recall_signal_fixtures import (
    complete_shadow_request,
    shadow_event,
    shadow_labels,
    shadow_snapshot,
)

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.integration


@pytest.fixture
def store(temp_db: HubDatabase) -> RecallSignalStore:
    return RecallSignalStore(temp_db)


class TestShadowReplay:
    def test_full_scope_projects_non_injected_hits_and_aligns_request_limit(
        self, store: RecallSignalStore
    ) -> None:
        complete_shadow_request(store, "req-replay-a")
        complete_shadow_request(store, "req-replay-b")
        assert (
            store.record_injection_outcomes(
                [
                    {
                        "session_id": "sess-shadow",
                        "recall_request_id": "req-replay-a",
                        "memory_id": "mem-1",
                        "project_id": "proj-1",
                        "outcome": "injected",
                        "injection_position": 0,
                        "injection_group": "fact",
                        "turn_seq": 7,
                        "caller": "memory.recall",
                    }
                ]
            )
            == 1
        )

        rows = store.fetch_replay_rows(
            label_source="digest_shadow",
            candidate_scope="full",
            judge_protocol_version="shadow-v1",
            query_construction_version=None,
            weighting_regime_key="[true,false,false,false]",
            judge_model_key="judge-model",
            judge_config_fingerprint="judge-fingerprint",
            data_cutoff=datetime(2026, 7, 17, 12, 10, tzinfo=UTC),
            completion_cutoff=datetime(2026, 7, 17, 12, 40, tzinfo=UTC),
            limit=1,
        )

        assert [(row["recall_request_id"], row["memory_id"]) for row in rows] == [
            ("req-replay-a", "mem-1"),
            ("req-replay-a", "mem-2"),
        ]
        assert rows[0]["outcome"] == "injected"
        assert rows[1]["outcome"] is None

    def test_exact_request_ids_limit_feature_reads_to_reserved_partition(
        self, store: RecallSignalStore
    ) -> None:
        complete_shadow_request(store, "req-reserved-a")
        complete_shadow_request(store, "req-reserved-b")

        rows = store.fetch_shadow_replay_rows(
            label_source="digest_shadow",
            candidate_scope="full",
            judge_protocol_version="shadow-v1",
            query_construction_version=None,
            weighting_regime_key="[true,false,false,false]",
            judge_model_key="judge-model",
            judge_config_fingerprint="judge-fingerprint",
            data_cutoff=datetime(2026, 7, 17, 12, 10, tzinfo=UTC),
            completion_cutoff=datetime(2026, 7, 17, 12, 40, tzinfo=UTC),
            project_id=None,
            limit=10,
            request_ids=["req-reserved-b"],
        )

        assert {row["recall_request_id"] for row in rows} == {"req-reserved-b"}


class TestShadowSampling:
    def test_ship_samples_one_candidate_per_request_and_rejects_since(
        self, store: RecallSignalStore
    ) -> None:
        for request_id in ("req-sample-a", "req-sample-b", "req-sample-c"):
            complete_shadow_request(store, request_id)
        args: dict[str, Any] = {
            "label_source": "digest_shadow",
            "protocol_version": "shadow-v1",
            "query_construction_version": None,
            "judge_model_key": "judge-model",
            "judge_config_fingerprint": "judge-fingerprint",
            "regime_key": "[true,false,false,false]",
            "candidate_scope": "full",
            "n_requests": 2,
            "data_cutoff": datetime(2026, 7, 17, 12, 10, tzinfo=UTC),
            "completion_cutoff": datetime(2026, 7, 17, 12, 40, tzinfo=UTC),
        }

        with pytest.raises(ValueError, match="since"):
            store.sample_usefulness_labels(
                **args,
                mode="ship",
                since=datetime(2026, 7, 1, tzinfo=UTC),
            )
        rows = store.sample_usefulness_labels(**args, mode="ship")

        assert len(rows) == 2
        assert len({row["recall_request_id"] for row in rows}) == 2
        assert all(row["presented"] for row in rows)
        diagnostic = store.sample_usefulness_labels(
            **args,
            mode="diagnostic",
            since=datetime(2026, 7, 10, tzinfo=UTC),
        )
        assert diagnostic == []

    def test_audit_scored_replay_rows_include_presentation_snapshot(
        self, store: RecallSignalStore
    ) -> None:
        complete_shadow_request(store, "req-audit-scored")

        rows = store.fetch_shadow_replay_rows(
            phase="audit_scored",
            label_source="digest_shadow",
            candidate_scope="full",
            judge_protocol_version="shadow-v1",
            query_construction_version=None,
            weighting_regime_key="[true,false,false,false]",
            judge_model_key="judge-model",
            judge_config_fingerprint="judge-fingerprint",
            data_cutoff=datetime(2026, 7, 17, 12, 10, tzinfo=UTC),
            completion_cutoff=datetime(2026, 7, 17, 12, 40, tzinfo=UTC),
            project_id=None,
            limit=10,
        )

        assert rows
        assert rows[0]["system_prompt"] == "score query relevance"
        assert rows[0]["query_text"] == "how does dispatch work"
        assert rows[0]["presented"]


class TestShadowAuditVerdicts:
    def test_round_trip_is_bound_to_sample_and_prompt_hash(self, store: RecallSignalStore) -> None:
        complete_shadow_request(store, "req-audit")
        verdict = {
            "request_id": "req-audit",
            "memory_id": "mem-1",
            "prompt_hash": "prompt-req-audit",
            "human_verdict": True,
            "reviewer": "reviewer-1",
            "created_at": "2026-07-17T13:00:00+00:00",
        }

        inserted = store.insert_audit_verdicts(
            [verdict], cohort_digest="cohort-1", sample_digest="sample-1"
        )
        rows = store.fetch_audit_verdicts(
            "cohort-1",
            "sample-1",
            expected_prompt_hashes={("req-audit", "mem-1"): "prompt-req-audit"},
        )

        assert inserted == 1
        assert len(rows) == 1
        assert rows[0]["human_verdict"] is True
        assert store.fetch_audit_verdicts("cohort-1", "sample-other") == []
        with pytest.raises(ValueError, match="prompt_hash"):
            store.fetch_audit_verdicts(
                "cohort-1",
                "sample-1",
                expected_prompt_hashes={("req-audit", "mem-1"): "stale-prompt"},
            )


class TestGateHoldoutReservation:
    def test_reservation_is_idempotent_and_burns_request_ids_globally(
        self, store: RecallSignalStore
    ) -> None:
        now = datetime(2026, 7, 17, 12, tzinfo=UTC)
        first = store.reserve_gate_holdout(
            holdout_consumption_key="gate-1",
            fit_settings_digest="settings-1",
            holdout_partition_ids=["req-hold-a", "req-hold-b"],
            min_requests=2,
            now=now,
        )
        busy = store.reserve_gate_holdout(
            holdout_consumption_key="gate-1",
            fit_settings_digest="settings-1",
            holdout_partition_ids=["req-hold-a", "req-hold-b"],
            min_requests=2,
            now=now + timedelta(minutes=1),
        )

        assert first.status == "reserved"
        assert first.request_ids == ("req-hold-a", "req-hold-b")
        assert first.claim_token is not None
        assert busy.status == "in_progress"
        with pytest.raises(ValueError, match="fit_settings_digest"):
            store.reserve_gate_holdout(
                holdout_consumption_key="gate-1",
                fit_settings_digest="settings-changed",
                holdout_partition_ids=[],
                min_requests=2,
                now=now + timedelta(minutes=11),
            )

        completed = store.complete_gate_run(
            "gate-1",
            first.claim_token,
            ship=True,
            decision={"ship": True, "score": 0.9},
            now=now + timedelta(minutes=2),
        )
        rerun = store.reserve_gate_holdout(
            holdout_consumption_key="gate-1",
            fit_settings_digest="settings-1",
            holdout_partition_ids=[],
            min_requests=2,
            now=now + timedelta(minutes=3),
        )
        burned = store.reserve_gate_holdout(
            holdout_consumption_key="gate-2",
            fit_settings_digest="settings-2",
            holdout_partition_ids=["req-hold-a", "req-hold-b"],
            min_requests=1,
            now=now + timedelta(minutes=3),
        )

        assert completed.status == "complete"
        assert rerun.status == "complete"
        assert rerun.decision == {"ship": True, "score": 0.9}
        assert burned.status == "insufficient"

    def test_expired_reservation_reclaims_identical_holdout(self, store: RecallSignalStore) -> None:
        now = datetime(2026, 7, 17, 12, tzinfo=UTC)
        first = store.reserve_gate_holdout(
            holdout_consumption_key="gate-reclaim",
            fit_settings_digest="settings-reclaim",
            holdout_partition_ids=["req-reclaim"],
            min_requests=1,
            now=now,
        )
        reclaimed = store.reserve_gate_holdout(
            holdout_consumption_key="gate-reclaim",
            fit_settings_digest="settings-reclaim",
            holdout_partition_ids=[],
            min_requests=1,
            now=now + timedelta(minutes=11),
        )

        assert reclaimed.status == "reserved"
        assert reclaimed.request_ids == first.request_ids
        assert reclaimed.claim_token != first.claim_token

    def test_concurrent_keys_cannot_consume_the_same_request_id(
        self, store: RecallSignalStore
    ) -> None:
        barrier = Barrier(2)

        def reserve(key: str) -> str:
            barrier.wait()
            result = store.reserve_gate_holdout(
                holdout_consumption_key=key,
                fit_settings_digest=f"settings-{key}",
                holdout_partition_ids=["req-shared"],
                min_requests=1,
            )
            return result.status

        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = sorted(executor.map(reserve, ("gate-concurrent-a", "gate-concurrent-b")))

        assert statuses == ["insufficient", "reserved"]


class TestShadowClaims:
    def test_claim_lease_blocks_then_allows_reclaim(self, store: RecallSignalStore) -> None:
        assert store.insert_signal_event(shadow_event("req-claim")) is True
        now = datetime(2026, 7, 17, 12, tzinfo=UTC)

        first_token = store.claim_shadow_request(
            "sess-shadow",
            "req-claim",
            label_source="digest_shadow",
            judge_protocol_version="shadow-v1",
            query_construction_version=None,
            now=now,
        )
        blocked = store.claim_shadow_request(
            "sess-shadow",
            "req-claim",
            label_source="digest_shadow",
            judge_protocol_version="shadow-v1",
            query_construction_version=None,
            now=now + timedelta(minutes=9),
        )
        reclaimed_token = store.claim_shadow_request(
            "sess-shadow",
            "req-claim",
            label_source="digest_shadow",
            judge_protocol_version="shadow-v1",
            query_construction_version=None,
            now=now + timedelta(minutes=11),
        )

        assert first_token is not None
        assert blocked is None
        assert reclaimed_token is not None
        assert reclaimed_token != first_token
        state = store.db.fetchone(
            "SELECT status, attempts FROM recall_shadow_judge_state WHERE recall_request_id = %s",
            ("req-claim",),
        )
        assert state is not None
        assert dict(state) == {"status": "claimed", "attempts": 2}

    def test_atomic_labels_commit_with_snapshot_and_complete_claim(
        self, store: RecallSignalStore
    ) -> None:
        assert store.insert_signal_event(shadow_event("req-atomic")) is True
        token = store.claim_shadow_request(
            "sess-shadow",
            "req-atomic",
            label_source="digest_shadow",
            judge_protocol_version="shadow-v1",
            query_construction_version=None,
        )
        assert token is not None

        inserted = store.insert_usefulness_labels_atomic(
            shadow_labels("req-atomic"),
            shadow_snapshot("req-atomic"),
            token,
        )

        assert inserted is True
        labels = store.db.fetchall(
            "SELECT memory_id, judge_useful FROM recall_usefulness "
            "WHERE recall_request_id = %s ORDER BY memory_id",
            ("req-atomic",),
        )
        assert [dict(row) for row in labels] == [
            {"memory_id": "mem-1", "judge_useful": True},
            {"memory_id": "mem-2", "judge_useful": False},
        ]
        snapshot = store.db.fetchone(
            "SELECT prompt_hash FROM recall_shadow_prompt_snapshot WHERE recall_request_id = %s",
            ("req-atomic",),
        )
        assert snapshot is not None
        assert snapshot["prompt_hash"] == "prompt-req-atomic"
        state = store.db.fetchone(
            "SELECT status FROM recall_shadow_judge_state WHERE recall_request_id = %s",
            ("req-atomic",),
        )
        assert state is not None
        assert state["status"] == "complete"

    def test_atomic_mapping_mismatch_rolls_back_snapshot_and_marks_retryable(
        self, store: RecallSignalStore
    ) -> None:
        assert store.insert_signal_event(shadow_event("req-conflict")) is True
        conflicting = shadow_labels("req-conflict")[0]
        conflicting["judge_useful"] = False
        assert store.insert_usefulness_label(conflicting) is True
        token = store.claim_shadow_request(
            "sess-shadow",
            "req-conflict",
            label_source="digest_shadow",
            judge_protocol_version="shadow-v1",
            query_construction_version=None,
        )
        assert token is not None

        inserted = store.insert_usefulness_labels_atomic(
            shadow_labels("req-conflict"),
            shadow_snapshot("req-conflict"),
            token,
        )

        assert inserted is False
        snapshot = store.db.fetchone(
            "SELECT 1 FROM recall_shadow_prompt_snapshot WHERE recall_request_id = %s",
            ("req-conflict",),
        )
        assert snapshot is None
        state = store.db.fetchone(
            "SELECT status, last_error FROM recall_shadow_judge_state WHERE recall_request_id = %s",
            ("req-conflict",),
        )
        assert state is not None
        assert dict(state) == {
            "status": "retryable",
            "last_error": "label_mapping_mismatch",
        }

    def test_atomic_snapshot_conflict_rolls_back_new_labels(self, store: RecallSignalStore) -> None:
        assert store.insert_signal_event(shadow_event("req-snapshot-conflict")) is True
        stale = shadow_snapshot("req-snapshot-conflict")
        stale["prompt_hash"] = "stale-prompt"
        store.db.execute(
            """
            INSERT INTO recall_shadow_prompt_snapshot
                (recall_request_id, label_source, judge_protocol_version,
                 system_prompt, query_text, presented, prompt_hash, judge_model,
                 judge_config_fingerprint, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                stale["recall_request_id"],
                stale["label_source"],
                stale["judge_protocol_version"],
                stale["system_prompt"],
                stale["query_text"],
                json.dumps(stale["presented"]),
                stale["prompt_hash"],
                stale["judge_model"],
                stale["judge_config_fingerprint"],
                datetime.fromisoformat(str(stale["created_at"])),
            ),
        )
        token = store.claim_shadow_request(
            "sess-shadow",
            "req-snapshot-conflict",
            label_source="digest_shadow",
            judge_protocol_version="shadow-v1",
            query_construction_version=None,
        )
        assert token is not None

        inserted = store.insert_usefulness_labels_atomic(
            shadow_labels("req-snapshot-conflict"),
            shadow_snapshot("req-snapshot-conflict"),
            token,
        )

        assert inserted is False
        label_count = store.db.fetchone(
            "SELECT COUNT(*) AS count FROM recall_usefulness WHERE recall_request_id = %s",
            ("req-snapshot-conflict",),
        )
        assert label_count is not None
        assert label_count["count"] == 0

    def test_stale_claim_token_cannot_write_or_replace_newer_claim(
        self, store: RecallSignalStore
    ) -> None:
        assert store.insert_signal_event(shadow_event("req-zombie")) is True
        now = datetime(2026, 7, 17, 12, tzinfo=UTC)
        stale_token = store.claim_shadow_request(
            "sess-shadow",
            "req-zombie",
            label_source="digest_shadow",
            judge_protocol_version="shadow-v1",
            query_construction_version=None,
            now=now,
        )
        current_token = store.claim_shadow_request(
            "sess-shadow",
            "req-zombie",
            label_source="digest_shadow",
            judge_protocol_version="shadow-v1",
            query_construction_version=None,
            now=now + timedelta(minutes=11),
        )
        assert stale_token is not None
        assert current_token is not None

        inserted = store.insert_usefulness_labels_atomic(
            shadow_labels("req-zombie"),
            shadow_snapshot("req-zombie"),
            stale_token,
        )

        assert inserted is False
        state = store.db.fetchone(
            "SELECT status, claim_token FROM recall_shadow_judge_state "
            "WHERE recall_request_id = %s",
            ("req-zombie",),
        )
        assert state is not None
        assert dict(state) == {"status": "claimed", "claim_token": current_token}

    def test_retryable_backoff_and_terminal_state_control_polling(
        self, store: RecallSignalStore
    ) -> None:
        assert store.insert_signal_event(shadow_event("req-state")) is True
        now = datetime(2026, 7, 17, 12, tzinfo=UTC)
        token = store.claim_shadow_request(
            "sess-shadow",
            "req-state",
            label_source="digest_shadow",
            judge_protocol_version="shadow-v1",
            query_construction_version=None,
            now=now,
        )
        assert token is not None
        assert store.mark_shadow_claim_retryable(
            "req-state",
            label_source="digest_shadow",
            judge_protocol_version="shadow-v1",
            claim_token=token,
            error="invalid_response",
            now=now,
        )

        assert (
            store.fetch_unshadowed_requests(
                "sess-shadow",
                label_source="digest_shadow",
                judge_protocol_version="shadow-v1",
                query_construction_version=None,
                limit=10,
                now=now + timedelta(hours=1),
            )
            == []
        )
        assert [
            row["recall_request_id"]
            for row in store.fetch_unshadowed_requests(
                "sess-shadow",
                label_source="digest_shadow",
                judge_protocol_version="shadow-v1",
                query_construction_version=None,
                limit=10,
                now=now + timedelta(hours=2),
            )
        ] == ["req-state"]

        terminal_token = store.claim_shadow_request(
            "sess-shadow",
            "req-state",
            label_source="digest_shadow",
            judge_protocol_version="shadow-v1",
            query_construction_version=None,
            now=now + timedelta(hours=2),
        )
        assert terminal_token is not None
        assert store.mark_shadow_claim_terminal(
            "req-state",
            label_source="digest_shadow",
            judge_protocol_version="shadow-v1",
            claim_token=terminal_token,
            error="content_drift",
            now=now + timedelta(hours=2),
        )
        assert (
            store.fetch_unshadowed_requests(
                "sess-shadow",
                label_source="digest_shadow",
                judge_protocol_version="shadow-v1",
                query_construction_version=None,
                limit=10,
                now=now + timedelta(days=30),
            )
            == []
        )
