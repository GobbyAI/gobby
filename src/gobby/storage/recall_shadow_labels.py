"""Atomic shadow-label writes and legacy-cohort retirement.

Split out of :mod:`gobby.storage.recall_shadow_signals` (#20776), which keeps
cohort resolution, polling, replay, and audit reads. This module owns the
label lifecycle: the one transaction that commits a judged batch, and the
sweep that retires the pre-cutover backlog nobody will ever judge.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from gobby.storage.recall_shadow_signals import (
    ShadowJudgePoll,
    _json_value,
    _parse_datetime,
)
from gobby.utils.datetime import utc_now

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

_SUPERSEDED_ERROR = "query_construction_version_superseded"


class RecallShadowLabelStoreMixin:
    """Label-lifecycle writes mixed into :class:`RecallSignalStore`."""

    db: HubDatabase

    def supersede_legacy_cohort(
        self,
        *,
        label_source: str,
        judge_protocol_version: str,
    ) -> int:
        """Retire the unjudged pre-cutover backlog for one label stream.

        Judging a legacy-query retrieval under the current protocol is exactly
        the contamination the query-construction fence exists to prevent, so
        once the poller has moved on the remainder is marked terminal instead.

        This inserts rather than updates: ``claim_shadow_request`` is the only
        writer of ``recall_shadow_judge_state``, so a request that was never
        claimed has no row at all and a plain ``UPDATE`` would no-op on exactly
        the backlog that matters. Requests already carrying a ``complete`` row
        are left untouched -- their labels are valid evidence under the version
        that produced them. Re-running selects nothing new and rewrites the same
        terminal rows, so the sweep is safe to repeat and safe to run before the
        protocol flip.
        """
        current_time = utc_now()
        cursor = self.db.execute(
            """
            INSERT INTO recall_shadow_judge_state
                (recall_request_id, label_source, judge_protocol_version,
                 status, attempts, next_attempt_at, lease_expires_at,
                 claim_token, last_error, updated_at)
            SELECT r.recall_request_id, %s, %s, 'terminal', 0, NULL, NULL, NULL, %s, %s
            FROM recall_signal_requests r
            WHERE r.weighting ->> 'query_construction_version' IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM recall_shadow_judge_state complete_state
                  WHERE complete_state.recall_request_id = r.recall_request_id
                    AND complete_state.label_source = %s
                    AND complete_state.judge_protocol_version = %s
                    AND complete_state.status = 'complete'
              )
            ON CONFLICT (recall_request_id, label_source, judge_protocol_version)
            DO UPDATE SET
                status = 'terminal',
                next_attempt_at = NULL,
                lease_expires_at = NULL,
                claim_token = NULL,
                last_error = EXCLUDED.last_error,
                updated_at = EXCLUDED.updated_at
            """,
            (
                label_source,
                judge_protocol_version,
                _SUPERSEDED_ERROR,
                current_time,
                label_source,
                judge_protocol_version,
            ),
        )
        return int(cursor.rowcount)

    def insert_usefulness_labels_atomic(
        self,
        rows: Sequence[Mapping[str, Any]],
        snapshot: Mapping[str, Any],
        claim_token: str,
    ) -> bool:
        """Atomically persist an exact label mapping, prompt snapshot, and completion."""
        if not rows or not claim_token:
            return False
        request_id = str(snapshot.get("recall_request_id") or "")
        label_source = str(snapshot.get("label_source") or "")
        protocol_version = str(snapshot.get("judge_protocol_version") or "")
        if not request_id or label_source != "digest_shadow" or not protocol_version:
            return False
        required_snapshot_fields = (
            "system_prompt",
            "query_text",
            "prompt_hash",
            "judge_model",
            "judge_config_fingerprint",
        )
        if any(snapshot.get(field) is None for field in required_snapshot_fields):
            return False

        expected: dict[str, bool] = {}
        session_id = str(rows[0].get("session_id") or "")
        for row in rows:
            memory_id = str(row.get("memory_id") or "")
            useful = row.get("judge_useful")
            if (
                not memory_id
                or memory_id in expected
                or not isinstance(useful, bool)
                or row.get("recall_request_id") != request_id
                or row.get("label_source") != label_source
                or row.get("judge_protocol_version") != protocol_version
                or row.get("judge_model") != snapshot.get("judge_model")
                or row.get("session_id") != session_id
            ):
                return False
            expected[memory_id] = useful

        presented = snapshot.get("presented")
        if not isinstance(presented, list):
            return False
        presented_ids = {
            str(item.get("memory_id"))
            for item in presented
            if isinstance(item, dict) and item.get("memory_id")
        }
        if presented_ids != set(expected):
            return False

        if not session_id:
            return False
        current_time = utc_now()
        with self.db.transaction_immediate(ShadowJudgePoll(session_id)) as transaction:
            claim = transaction.execute(
                """
                SELECT attempts FROM recall_shadow_judge_state
                WHERE recall_request_id = %s
                  AND label_source = %s
                  AND judge_protocol_version = %s
                  AND status = 'claimed'
                  AND claim_token = %s
                """,
                (request_id, label_source, protocol_version, claim_token),
            ).fetchone()
            if claim is None:
                return False

            savepoint = transaction.savepoint("shadow_label_batch")
            transaction.executemany(
                """
                INSERT INTO recall_usefulness
                    (project_id, session_id, recall_request_id, memory_id, label_source,
                     judge_useful, judge_confidence, judge_model, judge_protocol_version,
                     position_randomized, length_controlled, ablation_delta,
                     ablation_method, rationale, feature_extractor_version, labeled_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (recall_request_id, memory_id, label_source,
                             judge_protocol_version) DO NOTHING
                """,
                [
                    (
                        row.get("project_id"),
                        row["session_id"],
                        request_id,
                        str(row["memory_id"]),
                        label_source,
                        row["judge_useful"],
                        row.get("judge_confidence"),
                        row.get("judge_model"),
                        protocol_version,
                        bool(row.get("position_randomized")),
                        bool(row.get("length_controlled")),
                        row.get("ablation_delta"),
                        row.get("ablation_method"),
                        row.get("rationale"),
                        row.get("feature_extractor_version"),
                        _parse_datetime(row.get("labeled_at") or row.get("timestamp")),
                    )
                    for row in rows
                ],
            )
            transaction.execute(
                """
                INSERT INTO recall_shadow_prompt_snapshot
                    (recall_request_id, label_source, judge_protocol_version,
                     system_prompt, query_text, presented, prompt_hash, judge_model,
                     judge_config_fingerprint)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (recall_request_id, label_source, judge_protocol_version)
                DO NOTHING
                """,
                (
                    request_id,
                    label_source,
                    protocol_version,
                    snapshot["system_prompt"],
                    snapshot["query_text"],
                    json.dumps(presented),
                    snapshot["prompt_hash"],
                    snapshot["judge_model"],
                    snapshot["judge_config_fingerprint"],
                ),
            )
            stored_rows = transaction.execute(
                """
                SELECT memory_id, judge_useful FROM recall_usefulness
                WHERE recall_request_id = %s
                  AND label_source = %s
                  AND judge_protocol_version = %s
                """,
                (request_id, label_source, protocol_version),
            ).fetchall()
            stored = {str(row["memory_id"]): bool(row["judge_useful"]) for row in stored_rows}
            stored_snapshot = transaction.execute(
                """
                SELECT system_prompt, query_text, presented, prompt_hash, judge_model,
                       judge_config_fingerprint
                FROM recall_shadow_prompt_snapshot
                WHERE recall_request_id = %s
                  AND label_source = %s
                  AND judge_protocol_version = %s
                """,
                (request_id, label_source, protocol_version),
            ).fetchone()
            expected_snapshot = {
                "system_prompt": snapshot["system_prompt"],
                "query_text": snapshot["query_text"],
                "presented": presented,
                "prompt_hash": snapshot["prompt_hash"],
                "judge_model": snapshot["judge_model"],
                "judge_config_fingerprint": snapshot["judge_config_fingerprint"],
            }
            actual_snapshot = dict(stored_snapshot) if stored_snapshot is not None else {}
            if "presented" in actual_snapshot:
                actual_snapshot["presented"] = _json_value(actual_snapshot["presented"])
            mapping_mismatch = stored != expected
            snapshot_mismatch = actual_snapshot != expected_snapshot
            if mapping_mismatch or snapshot_mismatch:
                savepoint.rollback()
                mismatch_error = (
                    "label_mapping_mismatch" if mapping_mismatch else "snapshot_mismatch"
                )
                retry = transaction.execute(
                    """
                    UPDATE recall_shadow_judge_state
                    SET status = 'retryable',
                        next_attempt_at = %s,
                        lease_expires_at = NULL,
                        claim_token = NULL,
                        last_error = %s,
                        updated_at = %s
                    WHERE recall_request_id = %s
                      AND label_source = %s
                      AND judge_protocol_version = %s
                      AND claim_token = %s
                    """,
                    (
                        current_time + timedelta(hours=min(2 ** int(claim["attempts"]), 24)),
                        mismatch_error,
                        current_time,
                        request_id,
                        label_source,
                        protocol_version,
                        claim_token,
                    ),
                )
                if retry.rowcount != 1:
                    raise RuntimeError("shadow claim changed while marking label mismatch")
                return False

            savepoint.release()
            completed = transaction.execute(
                """
                UPDATE recall_shadow_judge_state
                SET status = 'complete', next_attempt_at = NULL,
                    lease_expires_at = NULL, claim_token = NULL,
                    last_error = NULL, updated_at = %s
                WHERE recall_request_id = %s
                  AND label_source = %s
                  AND judge_protocol_version = %s
                  AND claim_token = %s
                """,
                (current_time, request_id, label_source, protocol_version, claim_token),
            )
            if completed.rowcount != 1:
                raise RuntimeError("shadow claim changed while completing labels")
            return True
