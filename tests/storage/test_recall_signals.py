"""Tests for the recall-signal hub tables (#17196).

Covers contract docs/contracts/memory-usefulness-label.md: promoted signal
rows (§3), injection outcomes (§5), usefulness labels (§6), and the
fit-eligible join on (recall_request_id, memory_id).
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import TYPE_CHECKING, Any

import pytest

from gobby.storage.recall_signals import (
    LABEL_SOURCES,
    PRODUCIBLE_LABEL_SOURCES,
    RecallSignalStore,
    ShadowCohortAmbiguityError,
)

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


def _shadow_event(
    request_id: str,
    *,
    session_id: str = "sess-shadow",
    query: str = "how does dispatch work",
    schema_version: int = 4,
    complete_hashes: bool = True,
    query_construction_version: str | None = None,
) -> dict[str, object]:
    event = _event(request_id=request_id, session_id=session_id)
    event["schema_version"] = schema_version
    event["query"] = query
    event["constants_provenance"] = "static"
    if query_construction_version is not None:
        weighting = event["weighting"]
        assert isinstance(weighting, dict)
        weighting["query_construction_version"] = query_construction_version
    hits = event["hits"]
    assert isinstance(hits, list)
    for hit in hits:
        assert isinstance(hit, dict)
        hit["content_hash"] = f"hash-{hit['memory_id']}" if complete_hashes else None
    return event


def _shadow_snapshot(request_id: str) -> dict[str, object]:
    return {
        "recall_request_id": request_id,
        "label_source": "digest_shadow",
        "judge_protocol_version": "shadow-v1",
        "system_prompt": "score query relevance",
        "query_text": "how does dispatch work",
        "presented": [
            {
                "neutral_key": "A",
                "memory_id": "mem-1",
                "order_index": 0,
                "excerpt": "dispatch uses a staged pipeline",
                "content_hash": "hash-mem-1",
            },
            {
                "neutral_key": "B",
                "memory_id": "mem-2",
                "order_index": 1,
                "excerpt": "dispatch claims tasks atomically",
                "content_hash": "hash-mem-2",
            },
        ],
        "prompt_hash": f"prompt-{request_id}",
        "judge_model": "judge-model",
        "judge_config_fingerprint": "judge-fingerprint",
        "created_at": "2026-07-17T12:30:00+00:00",
    }


def _shadow_labels(request_id: str) -> list[dict[str, object]]:
    return [
        {
            "project_id": "proj-1",
            "session_id": "sess-shadow",
            "recall_request_id": request_id,
            "memory_id": memory_id,
            "label_source": "digest_shadow",
            "judge_useful": useful,
            "judge_confidence": 0.9,
            "judge_model": "judge-model",
            "judge_protocol_version": "shadow-v1",
            "position_randomized": True,
            "length_controlled": True,
            "rationale": "comparative relevance",
            "labeled_at": "2026-07-17T12:30:00+00:00",
        }
        for memory_id, useful in (("mem-1", True), ("mem-2", False))
    ]


def _complete_shadow_request(
    store: RecallSignalStore,
    request_id: str,
    *,
    protocol_version: str = "shadow-v1",
    judge_model: str = "judge-model",
    judge_config_fingerprint: str = "judge-fingerprint",
    snapshot_created_at: str = "2026-07-17T12:30:00+00:00",
    query_construction_version: str | None = None,
) -> None:
    assert (
        store.insert_signal_event(
            _shadow_event(request_id, query_construction_version=query_construction_version)
        )
        is True
    )
    token = store.claim_shadow_request(
        "sess-shadow",
        request_id,
        label_source="digest_shadow",
        judge_protocol_version=protocol_version,
        query_construction_version=query_construction_version,
    )
    assert token is not None
    labels = _shadow_labels(request_id)
    for label in labels:
        label["judge_protocol_version"] = protocol_version
        label["judge_model"] = judge_model
    snapshot = _shadow_snapshot(request_id)
    snapshot["judge_protocol_version"] = protocol_version
    snapshot["judge_model"] = judge_model
    snapshot["judge_config_fingerprint"] = judge_config_fingerprint
    assert store.insert_usefulness_labels_atomic(labels, snapshot, token) is True
    # Snapshot completion time is DB-owned (DEFAULT now()); backdate it so the
    # cutoff-based cohort assertions stay deterministic.
    store.db.execute(
        "UPDATE recall_shadow_prompt_snapshot SET created_at = %s "
        "WHERE recall_request_id = %s AND judge_protocol_version = %s",
        (datetime.fromisoformat(snapshot_created_at), request_id, protocol_version),
    )


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

    def test_digest_is_historical_and_digest_shadow_is_producible(
        self, store: RecallSignalStore
    ) -> None:
        assert "digest" in LABEL_SOURCES
        assert "digest" not in PRODUCIBLE_LABEL_SOURCES
        assert "digest_shadow" in PRODUCIBLE_LABEL_SOURCES

        historical = self._label()
        historical["label_source"] = "digest"
        shadow = self._label()
        shadow["label_source"] = "digest_shadow"

        assert store.insert_usefulness_label(historical) is False
        assert store.insert_usefulness_label(shadow) is True
        store.db.execute(
            """
            INSERT INTO recall_usefulness
                (project_id, session_id, recall_request_id, memory_id, label_source,
                 judge_useful, judge_protocol_version, position_randomized,
                 length_controlled, labeled_at)
            VALUES (%s, %s, %s, %s, 'digest', %s, %s, %s, %s, %s)
            """,
            (
                historical["project_id"],
                historical["session_id"],
                historical["recall_request_id"],
                historical["memory_id"],
                historical["judge_useful"],
                historical["judge_protocol_version"],
                historical["position_randomized"],
                historical["length_controlled"],
                datetime.fromisoformat(str(historical["labeled_at"])),
            ),
        )
        persisted = store.db.fetchone(
            "SELECT label_source FROM recall_usefulness WHERE label_source = 'digest'"
        )
        assert persisted is not None
        assert persisted["label_source"] == "digest"


class TestShadowCohort:
    def test_polling_uses_shared_candidate_eligibility(self, store: RecallSignalStore) -> None:
        assert store.insert_signal_event(_shadow_event("req-valid")) is True
        assert store.insert_signal_event(_shadow_event("req-blank", query="   ")) is True
        assert store.insert_signal_event(_shadow_event("req-old", schema_version=3)) is True
        assert store.insert_signal_event(_shadow_event("req-null", complete_hashes=False)) is True

        rows = store.fetch_unshadowed_requests(
            "sess-shadow",
            label_source="digest_shadow",
            judge_protocol_version="shadow-v1",
            query_construction_version=None,
            limit=10,
        )

        assert [row["recall_request_id"] for row in rows] == ["req-valid"]
        assert [hit["memory_id"] for hit in rows[0]["hits"]] == ["mem-1", "mem-2"]

    def test_scored_phases_require_exact_complete_snapshot_cohort(
        self, store: RecallSignalStore
    ) -> None:
        _complete_shadow_request(store, "req-scored")
        assert store.insert_signal_event(_shadow_event("req-incomplete")) is True
        incomplete_token = store.claim_shadow_request(
            "sess-shadow",
            "req-incomplete",
            label_source="digest_shadow",
            judge_protocol_version="shadow-v1",
            query_construction_version=None,
        )
        assert incomplete_token is not None
        cohort_args = {
            "label_source": "digest_shadow",
            "judge_protocol_version": "shadow-v1",
            "query_construction_version": None,
            "judge_model_key": "judge-model",
            "judge_config_fingerprint": "judge-fingerprint",
            "weighting_regime_key": "[true,false,false,false]",
            "data_cutoff": datetime(2026, 7, 17, 12, 10, tzinfo=UTC),
            "completion_cutoff": datetime(2026, 7, 17, 12, 40, tzinfo=UTC),
            "limit": 10,
        }

        for phase in ("fitting", "drift", "audit_scored"):
            rows = store.shadow_cohort_query(phase, **cohort_args)
            assert [row["recall_request_id"] for row in rows] == ["req-scored"]

        late_completion = {
            **cohort_args,
            "completion_cutoff": datetime(2026, 7, 17, 12, 20, tzinfo=UTC),
        }
        assert store.shadow_cohort_query("fitting", **late_completion) == []
        status = store.shadow_cohort_query(
            "audit_status",
            label_source="digest_shadow",
            judge_protocol_version="shadow-v1",
            query_construction_version=None,
            session_id="sess-shadow",
            limit=10,
        )
        assert [(row["recall_request_id"], row["status"]) for row in status] == [
            ("req-incomplete", "claimed")
        ]

    def test_scored_cohort_reports_model_ambiguity_counts(self, store: RecallSignalStore) -> None:
        _complete_shadow_request(store, "req-model-a", judge_model="judge-a")
        _complete_shadow_request(store, "req-model-b", judge_model="judge-b")

        with pytest.raises(ShadowCohortAmbiguityError) as error:
            store.shadow_cohort_query(
                "fitting",
                label_source="digest_shadow",
                judge_protocol_version="shadow-v1",
                query_construction_version=None,
                judge_config_fingerprint="judge-fingerprint",
                weighting_regime_key="[true,false,false,false]",
                data_cutoff=datetime(2026, 7, 17, 12, 10, tzinfo=UTC),
                completion_cutoff=datetime(2026, 7, 17, 12, 40, tzinfo=UTC),
            )

        assert error.value.dimension == "judge_model_key"
        assert error.value.counts == {"judge-a": 1, "judge-b": 1}

    def test_scored_cohort_reports_fingerprint_ambiguity_counts(
        self, store: RecallSignalStore
    ) -> None:
        _complete_shadow_request(
            store, "req-fingerprint-a", judge_config_fingerprint="fingerprint-a"
        )
        _complete_shadow_request(
            store, "req-fingerprint-b", judge_config_fingerprint="fingerprint-b"
        )

        with pytest.raises(ShadowCohortAmbiguityError) as error:
            store.shadow_cohort_query(
                "fitting",
                label_source="digest_shadow",
                judge_protocol_version="shadow-v1",
                query_construction_version=None,
                judge_model_key="judge-model",
                weighting_regime_key="[true,false,false,false]",
                data_cutoff=datetime(2026, 7, 17, 12, 10, tzinfo=UTC),
                completion_cutoff=datetime(2026, 7, 17, 12, 40, tzinfo=UTC),
            )

        assert error.value.dimension == "judge_config_fingerprint"
        assert error.value.counts == {"fingerprint-a": 1, "fingerprint-b": 1}

    def test_scored_cohort_reports_regime_ambiguity_counts(self, store: RecallSignalStore) -> None:
        _complete_shadow_request(store, "req-regime-a")
        _complete_shadow_request(store, "req-regime-b")
        store.db.execute(
            "UPDATE recall_signal_requests SET weighting = %s WHERE recall_request_id = %s",
            (json.dumps({}), "req-regime-b"),
        )

        with pytest.raises(ShadowCohortAmbiguityError) as error:
            store.shadow_cohort_query(
                "fitting",
                label_source="digest_shadow",
                judge_protocol_version="shadow-v1",
                query_construction_version=None,
                judge_model_key="judge-model",
                judge_config_fingerprint="judge-fingerprint",
                data_cutoff=datetime(2026, 7, 17, 12, 10, tzinfo=UTC),
                completion_cutoff=datetime(2026, 7, 17, 12, 40, tzinfo=UTC),
            )

        assert error.value.dimension == "weighting_regime_key"
        assert error.value.counts == {
            "[false,false,false,false]": 1,
            "[true,false,false,false]": 1,
        }

    def test_drift_cohort_filters_constants_provenance(self, store: RecallSignalStore) -> None:
        _complete_shadow_request(store, "req-provenance")
        args = {
            "label_source": "digest_shadow",
            "judge_protocol_version": "shadow-v1",
            "query_construction_version": None,
            "judge_model_key": "judge-model",
            "judge_config_fingerprint": "judge-fingerprint",
            "weighting_regime_key": "[true,false,false,false]",
            "data_cutoff": datetime(2026, 7, 17, 12, 10, tzinfo=UTC),
            "completion_cutoff": datetime(2026, 7, 17, 12, 40, tzinfo=UTC),
        }

        assert (
            store.shadow_cohort_query("drift", **args, constants_provenance="decision-other") == []
        )
        rows = store.shadow_cohort_query("drift", **args, constants_provenance="static")
        assert [row["recall_request_id"] for row in rows] == ["req-provenance"]


class TestQueryConstructionFence:
    """The cutover fence: one cohort never spans two query-construction eras."""

    def test_v2_poller_does_not_claim_legacy_requests(self, store: RecallSignalStore) -> None:
        """4.1.5: a v2 protocol label on a legacy-query retrieval is contamination.

        The poller's construction version is required rather than defaulted
        precisely so this filter cannot be skipped by omission.
        """
        assert store.insert_signal_event(_shadow_event("req-legacy")) is True
        assert (
            store.insert_signal_event(
                _shadow_event("req-v2", query_construction_version="nl-embed-v1")
            )
            is True
        )

        polled = store.fetch_unshadowed_requests(
            "sess-shadow",
            label_source="digest_shadow",
            judge_protocol_version="shadow-v2",
            query_construction_version="nl-embed-v1",
            limit=10,
        )

        assert [row["recall_request_id"] for row in polled] == ["req-v2"]
        assert (
            store.claim_shadow_request(
                "sess-shadow",
                "req-legacy",
                label_source="digest_shadow",
                judge_protocol_version="shadow-v2",
                query_construction_version="nl-embed-v1",
            )
            is None
        )

    def test_supersede_legacy_cohort_inserts_and_is_idempotent(
        self, store: RecallSignalStore
    ) -> None:
        """4.1.6: an unclaimed backlog request has no state row, so the sweep inserts.

        A plain UPDATE would no-op on exactly the rows that matter, and the second
        run has to rewrite the same terminal rows rather than find new ones.
        """
        assert store.insert_signal_event(_shadow_event("req-backlog")) is True
        assert (
            store.insert_signal_event(
                _shadow_event("req-v2", query_construction_version="nl-embed-v1")
            )
            is True
        )

        first = store.supersede_legacy_cohort(
            label_source="digest_shadow",
            judge_protocol_version="shadow-v1",
        )
        second = store.supersede_legacy_cohort(
            label_source="digest_shadow",
            judge_protocol_version="shadow-v1",
        )

        assert (first, second) == (1, 1)
        rows = store.db.fetchall(
            "SELECT recall_request_id, status, claim_token, next_attempt_at, "
            "lease_expires_at, last_error FROM recall_shadow_judge_state "
            "ORDER BY recall_request_id"
        )
        assert [dict(row) for row in rows] == [
            {
                "recall_request_id": "req-backlog",
                "status": "terminal",
                "claim_token": None,
                "next_attempt_at": None,
                "lease_expires_at": None,
                "last_error": "query_construction_version_superseded",
            }
        ]

    def test_supersede_legacy_cohort_preserves_complete_rows(
        self, store: RecallSignalStore
    ) -> None:
        """4.1.7: a committed v1 label is valid evidence, so the sweep leaves it alone."""
        _complete_shadow_request(store, "req-labeled")
        assert store.insert_signal_event(_shadow_event("req-backlog")) is True
        before = store.db.fetchone(
            "SELECT * FROM recall_shadow_judge_state WHERE recall_request_id = %s",
            ("req-labeled",),
        )
        assert before is not None

        superseded = store.supersede_legacy_cohort(
            label_source="digest_shadow",
            judge_protocol_version="shadow-v1",
        )

        assert superseded == 1
        after = store.db.fetchone(
            "SELECT * FROM recall_shadow_judge_state WHERE recall_request_id = %s",
            ("req-labeled",),
        )
        assert after is not None
        assert dict(after) == dict(before)

    def test_cohort_cannot_mix_query_construction_versions(self, store: RecallSignalStore) -> None:
        """4.1.8: a replay spanning the cutover still resolves to exactly one era."""
        _complete_shadow_request(store, "req-legacy-scored")
        _complete_shadow_request(store, "req-v2-scored", query_construction_version="nl-embed-v1")
        replay_args = {
            "label_source": "digest_shadow",
            "candidate_scope": "full",
            "judge_protocol_version": "shadow-v1",
            "weighting_regime_key": "[true,false,false,false]",
            "judge_model_key": "judge-model",
            "judge_config_fingerprint": "judge-fingerprint",
            "data_cutoff": datetime(2026, 7, 17, 12, 10, tzinfo=UTC),
            "completion_cutoff": datetime(2026, 7, 17, 12, 40, tzinfo=UTC),
            "project_id": None,
            "limit": 10,
        }

        legacy = store.fetch_shadow_replay_rows(**replay_args, query_construction_version=None)
        current = store.fetch_shadow_replay_rows(
            **replay_args, query_construction_version="nl-embed-v1"
        )

        assert {row["recall_request_id"] for row in legacy} == {"req-legacy-scored"}
        assert {row["recall_request_id"] for row in current} == {"req-v2-scored"}


class TestShadowReplay:
    def test_full_scope_projects_non_injected_hits_and_aligns_request_limit(
        self, store: RecallSignalStore
    ) -> None:
        _complete_shadow_request(store, "req-replay-a")
        _complete_shadow_request(store, "req-replay-b")
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
        _complete_shadow_request(store, "req-reserved-a")
        _complete_shadow_request(store, "req-reserved-b")

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
            _complete_shadow_request(store, request_id)
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
        _complete_shadow_request(store, "req-audit-scored")

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
        _complete_shadow_request(store, "req-audit")
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
        assert store.insert_signal_event(_shadow_event("req-claim")) is True
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
        assert store.insert_signal_event(_shadow_event("req-atomic")) is True
        token = store.claim_shadow_request(
            "sess-shadow",
            "req-atomic",
            label_source="digest_shadow",
            judge_protocol_version="shadow-v1",
            query_construction_version=None,
        )
        assert token is not None

        inserted = store.insert_usefulness_labels_atomic(
            _shadow_labels("req-atomic"),
            _shadow_snapshot("req-atomic"),
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
        assert store.insert_signal_event(_shadow_event("req-conflict")) is True
        conflicting = _shadow_labels("req-conflict")[0]
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
            _shadow_labels("req-conflict"),
            _shadow_snapshot("req-conflict"),
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
        assert store.insert_signal_event(_shadow_event("req-snapshot-conflict")) is True
        stale = _shadow_snapshot("req-snapshot-conflict")
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
            _shadow_labels("req-snapshot-conflict"),
            _shadow_snapshot("req-snapshot-conflict"),
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
        assert store.insert_signal_event(_shadow_event("req-zombie")) is True
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
            _shadow_labels("req-zombie"),
            _shadow_snapshot("req-zombie"),
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
        assert store.insert_signal_event(_shadow_event("req-state")) is True
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
        # Only mem-2 is labeled, and only in the ablation stream.
        store.insert_usefulness_label(
            {
                "session_id": "sess-1",
                "recall_request_id": "req-1",
                "memory_id": "mem-2",
                "label_source": "ablation",
                "judge_useful": True,
                "judge_protocol_version": "17195-ablation-v1",
                "position_randomized": False,
                "length_controlled": False,
                "labeled_at": "2026-07-09T13:00:00+00:00",
            }
        )

    def test_returns_unlabeled_injected_rows_with_null_label(
        self, store: RecallSignalStore
    ) -> None:
        self._seed(store)

        rows = store.fetch_replay_rows(label_source="ablation")

        # Both injected hits come back — the unlabeled one feeds propensity
        # denominators (never-retrieved/unlabeled ≠ negative).
        assert [row["memory_id"] for row in rows] == ["mem-1", "mem-2"]
        unlabeled, labeled = rows
        assert unlabeled["judge_useful"] is None
        assert unlabeled["label_source"] is None
        assert unlabeled["injection_position"] == 1
        assert labeled["judge_useful"] is True
        assert labeled["label_source"] == "ablation"
        assert labeled["injection_group"] == "fact"
        # Request-level replay context is attached.
        assert labeled["weighting"] == {"graph_edge_weighting": True}
        assert labeled["graph_synthetic_similarity_discount"] == pytest.approx(0.9)

    def test_label_streams_stay_separable(self, store: RecallSignalStore) -> None:
        self._seed(store)

        rows = store.fetch_replay_rows(label_source="llm_judge")

        # Ablation labels never leak into a judge-stream fit: same feature
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
                "label_source": "ablation",
                "judge_useful": False,
                "judge_protocol_version": "17195-ablation-v2",
                "position_randomized": False,
                "length_controlled": False,
                "labeled_at": "2026-07-10T13:00:00+00:00",
            }
        )

        rows = store.fetch_replay_rows(label_source="ablation")

        labeled = [row for row in rows if row["memory_id"] == "mem-2"]
        assert len(labeled) == 1
        assert labeled[0]["judge_useful"] is False
        assert labeled[0]["judge_protocol_version"] == "17195-ablation-v2"

    def test_since_bounds_the_replay_window(self, store: RecallSignalStore) -> None:
        # The #17201 drift monitor replays only the recent live window.
        self._seed(store)

        before_seed = datetime(2026, 7, 1, tzinfo=UTC)
        after_seed = datetime(2026, 7, 10, tzinfo=UTC)

        assert len(store.fetch_replay_rows(label_source="ablation", since=before_seed)) == 2
        assert store.fetch_replay_rows(label_source="ablation", since=after_seed) == []

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

        assert store.fetch_replay_rows(label_source="ablation") == []
        assert store.fetch_replay_rows(label_source="ablation", project_id="proj-1") == []


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
