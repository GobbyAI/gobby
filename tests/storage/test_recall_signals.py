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

from gobby.storage.recall_signals import (
    LABEL_SOURCES,
    PRODUCIBLE_LABEL_SOURCES,
    RecallSignalStore,
    ShadowCohortAmbiguityError,
)
from tests.storage.recall_signal_fixtures import (
    complete_shadow_request,
    shadow_event,
    signal_event,
)

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.integration


@pytest.fixture
def store(temp_db: HubDatabase) -> RecallSignalStore:
    return RecallSignalStore(temp_db)


class TestInsertSignalEvent:
    def test_populates_request_and_hit_rows(self, store: RecallSignalStore) -> None:
        assert store.insert_signal_event(signal_event()) is True

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
        assert store.insert_signal_event(signal_event()) is True
        assert store.insert_signal_event(signal_event()) is False

        count = store.db.fetchone(
            "SELECT count(*) AS n FROM recall_signal_hits WHERE recall_request_id = %s",
            ("req-1",),
        )
        assert count is not None and count["n"] == 2

    def test_rejects_events_without_join_key(self, store: RecallSignalStore) -> None:
        event = signal_event()
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
        assert store.insert_signal_event(shadow_event("req-valid")) is True
        assert store.insert_signal_event(shadow_event("req-blank", query="   ")) is True
        assert store.insert_signal_event(shadow_event("req-old", schema_version=3)) is True
        assert store.insert_signal_event(shadow_event("req-null", complete_hashes=False)) is True

        rows = store.fetch_unshadowed_requests(
            "sess-shadow",
            label_source="digest_shadow",
            judge_protocol_version="shadow-v1",
            query_construction_version=None,
            limit=10,
        )

        assert [row["recall_request_id"] for row in rows] == ["req-valid"]
        assert [hit["memory_id"] for hit in rows[0]["hits"]] == ["mem-1", "mem-2"]

    def test_polling_admits_archived_recall_and_agent_search_callers(
        self, store: RecallSignalStore
    ) -> None:
        """#21011: the live cohort is agent-driven search; probe searches never enter."""
        admitted = {
            "req-recall": "memory.recall",
            "req-search": "mcp_proxy.memory.search_memories",
            "req-review": "mcp_proxy.memory.review_task_memories",
        }
        excluded = {
            "req-probe": "mcp_proxy.memory.create_memory.similar_existing",
            "req-cli": "cli.memory.recall",
        }
        for request_id, caller in {**admitted, **excluded}.items():
            assert store.insert_signal_event(shadow_event(request_id, caller=caller)) is True

        rows = store.fetch_unshadowed_requests(
            "sess-shadow",
            label_source="digest_shadow",
            judge_protocol_version="shadow-v1",
            query_construction_version=None,
            limit=10,
        )

        assert sorted(row["recall_request_id"] for row in rows) == sorted(admitted)

    def test_scored_phases_require_exact_complete_snapshot_cohort(
        self, store: RecallSignalStore
    ) -> None:
        complete_shadow_request(store, "req-scored")
        assert store.insert_signal_event(shadow_event("req-incomplete")) is True
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
        complete_shadow_request(store, "req-model-a", judge_model="judge-a")
        complete_shadow_request(store, "req-model-b", judge_model="judge-b")

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
        complete_shadow_request(
            store, "req-fingerprint-a", judge_config_fingerprint="fingerprint-a"
        )
        complete_shadow_request(
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
        complete_shadow_request(store, "req-regime-a")
        complete_shadow_request(store, "req-regime-b")
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
        complete_shadow_request(store, "req-provenance")
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
        assert store.insert_signal_event(shadow_event("req-legacy")) is True
        assert (
            store.insert_signal_event(
                shadow_event("req-v2", query_construction_version="nl-embed-v1")
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
        assert store.insert_signal_event(shadow_event("req-backlog")) is True
        assert (
            store.insert_signal_event(
                shadow_event("req-v2", query_construction_version="nl-embed-v1")
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
        complete_shadow_request(store, "req-labeled")
        assert store.insert_signal_event(shadow_event("req-backlog")) is True
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
        complete_shadow_request(store, "req-legacy-scored")
        complete_shadow_request(store, "req-v2-scored", query_construction_version="nl-embed-v1")
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

    def test_cutover_leaves_v1_label_rows_untouched(self, store: RecallSignalStore) -> None:
        """4.1.12: the v1 cohort is frozen, not migrated.

        Cutting over runs the sweep and then judges under the new protocol.
        Neither step may rewrite, delete, or re-judge a v1 label: those rows
        stay valid evidence under the era that produced them, and the fence is
        what keeps them out of v2 cohorts.
        """
        complete_shadow_request(store, "req-v1-labeled")
        assert store.insert_signal_event(shadow_event("req-v1-backlog")) is True
        before = [
            dict(row)
            for row in store.db.fetchall(
                "SELECT * FROM recall_usefulness ORDER BY recall_request_id, memory_id"
            )
        ]
        assert len(before) == 2

        store.supersede_legacy_cohort(
            label_source="digest_shadow",
            judge_protocol_version="shadow-v1",
        )
        complete_shadow_request(
            store,
            "req-v2-labeled",
            protocol_version="shadow-v2",
            query_construction_version="nl-embed-v1",
        )

        after = [
            dict(row)
            for row in store.db.fetchall(
                "SELECT * FROM recall_usefulness WHERE judge_protocol_version = %s "
                "ORDER BY recall_request_id, memory_id",
                ("shadow-v1",),
            )
        ]
        assert after == before
        total = store.db.fetchone("SELECT count(*) AS n FROM recall_usefulness")
        assert total is not None and total["n"] == 4


class TestFitRowJoin:
    def test_join_on_recall_request_id_and_memory_id(self, store: RecallSignalStore) -> None:
        store.insert_signal_event(signal_event())
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
        store.insert_signal_event(signal_event())
        rows = store.fetch_fit_rows(project_id="other-project")
        assert rows == []


class TestReplayRowJoin:
    """fetch_replay_rows: the #17197 harness loader (LEFT JOIN labels)."""

    def _seed(self, store: RecallSignalStore) -> None:
        store.insert_signal_event(signal_event())
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
        store.insert_signal_event(signal_event())
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
            json.dumps(signal_event("req-1")),
            "not-json",
            json.dumps({"schema_version": 2}),  # missing join keys → skipped
            json.dumps(signal_event("req-2")),
            json.dumps(signal_event("req-1")),  # replay → skipped
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
