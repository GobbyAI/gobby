"""Storage operations for query-relevance shadow labels."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar, Literal
from uuid import uuid4

from gobby.utils.datetime import utc_now
from gobby.utils.sql import render_internal_sql

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

ShadowCohortPhase = Literal["polling", "fitting", "drift", "audit_scored", "audit_status"]
SUPPORTED_SHADOW_SIGNAL_SCHEMA_VERSIONS = (4,)
SHADOW_CLAIM_LEASE = timedelta(minutes=10)

_IMMUTABLE_ELIGIBILITY = (
    "r.caller = 'memory.recall'",
    "NULLIF(BTRIM(r.query), '') IS NOT NULL",
    "r.schema_version = 4",
    "EXISTS (SELECT 1 FROM recall_signal_hits present "
    "WHERE present.recall_request_id = r.recall_request_id)",
    "NOT EXISTS ("
    "SELECT 1 FROM ("
    "SELECT candidate.content_hash FROM recall_signal_hits candidate "
    "WHERE candidate.recall_request_id = r.recall_request_id "
    "ORDER BY candidate.rank LIMIT 8"
    ") top_candidate WHERE top_candidate.content_hash IS NULL"
    ")",
)
_TOP_LABEL_MISSING = (
    "EXISTS ("
    "SELECT 1 FROM ("
    "SELECT candidate.memory_id FROM recall_signal_hits candidate "
    "WHERE candidate.recall_request_id = r.recall_request_id "
    "ORDER BY candidate.rank LIMIT 8"
    ") top_candidate WHERE NOT EXISTS ("
    "SELECT 1 FROM recall_usefulness label "
    "WHERE label.recall_request_id = r.recall_request_id "
    "AND label.memory_id = top_candidate.memory_id "
    "AND label.label_source = %s "
    "AND label.judge_protocol_version = %s"
    ")"
    ")"
)
_REGIME_KEY_SQL = (
    "CONCAT('[', "
    "CASE WHEN COALESCE((r.weighting ->> 'graph_edge_weighting')::boolean, false) "
    "THEN 'true' ELSE 'false' END, ',', "
    "CASE WHEN COALESCE((r.weighting ->> 'graph_edge_decay')::boolean, false) "
    "THEN 'true' ELSE 'false' END, ',', "
    "CASE WHEN COALESCE((r.weighting ->> 'materialize_cooccurrence')::boolean, false) "
    "THEN 'true' ELSE 'false' END, ',', "
    "CASE WHEN COALESCE((r.weighting ->> 'cluster_recall_expansion')::boolean, false) "
    "THEN 'true' ELSE 'false' END, ']')"
)


@dataclass(frozen=True)
class ShadowJudgePoll:
    """Serialize shadow-judge claims within one session."""

    session_id: str
    PRIORITY: ClassVar[int] = 925


class ShadowCohortAmbiguityError(ValueError):
    """Raised when a scored cohort spans multiple values of a required fence."""

    def __init__(self, dimension: str, counts: Mapping[str, int]) -> None:
        self.dimension = dimension
        self.counts = dict(counts)
        super().__init__(f"ambiguous {dimension}: {self.counts}")


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _parse_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise ValueError("timestamp must be an ISO-8601 string or datetime")


class RecallShadowSignalStoreMixin:
    """Focused shadow-label API mixed into :class:`RecallSignalStore`."""

    db: HubDatabase

    def shadow_cohort_query(
        self,
        phase: ShadowCohortPhase,
        *,
        label_source: str,
        judge_protocol_version: str,
        session_id: str | None = None,
        recall_request_id: str | None = None,
        project_id: str | None = None,
        constants_provenance: str | None = None,
        judge_model_key: str | None = None,
        judge_config_fingerprint: str | None = None,
        weighting_regime_key: str | None = None,
        data_cutoff: datetime | None = None,
        completion_cutoff: datetime | None = None,
        since: datetime | None = None,
        limit: int = 500,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Return request-level rows from the centralized shadow cohort."""
        if phase not in {"polling", "fitting", "drift", "audit_scored", "audit_status"}:
            raise ValueError(f"unknown shadow cohort phase: {phase}")
        if limit <= 0:
            return []
        conditions = list(_IMMUTABLE_ELIGIBILITY)
        if session_id is not None:
            conditions.append("r.session_id = %s")
        if recall_request_id is not None:
            conditions.append("r.recall_request_id = %s")
        if project_id is not None:
            conditions.append("r.project_id = %s")
        if constants_provenance is not None:
            conditions.append("r.constants_provenance = %s")
        identity_params: list[Any] = [
            value
            for value in (session_id, recall_request_id, project_id, constants_provenance)
            if value is not None
        ]

        if phase == "polling":
            current_time = now or utc_now()
            conditions.extend(
                (
                    _TOP_LABEL_MISSING,
                    "(state.status IS NULL "
                    "OR (state.status = 'claimed' "
                    "AND (state.lease_expires_at IS NULL OR state.lease_expires_at <= %s)) "
                    "OR (state.status = 'retryable' "
                    "AND (state.next_attempt_at IS NULL OR state.next_attempt_at <= %s)))",
                )
            )
            params: list[Any] = [
                label_source,
                judge_protocol_version,
                *identity_params,
                label_source,
                judge_protocol_version,
                current_time,
                current_time,
                limit,
            ]
            query = render_internal_sql(
                """
                SELECT r.session_id, r.recall_request_id, r.project_id, r.query,
                       r.weighting, r.constants_provenance, r.created_at,
                       state.status, state.attempts, state.next_attempt_at,
                       state.lease_expires_at
                FROM recall_signal_requests r
                LEFT JOIN recall_shadow_judge_state state
                  ON state.recall_request_id = r.recall_request_id
                 AND state.label_source = %s
                 AND state.judge_protocol_version = %s
                WHERE {where}
                ORDER BY r.created_at, r.recall_request_id
                LIMIT %s
                """,
                where=" AND ".join(conditions),
            )
            rows = self.db.fetchall(query, tuple(params))
            return self._normalize_cohort_rows(rows)

        if phase == "audit_status":
            if since is not None:
                conditions.append("r.created_at >= %s")
                identity_params.append(since)
            conditions.append(
                "(state.status IS DISTINCT FROM 'complete' OR snapshot.recall_request_id IS NULL "
                f"OR {_TOP_LABEL_MISSING})"
            )
            query = render_internal_sql(
                """
                SELECT r.session_id, r.recall_request_id, r.project_id, r.query,
                       r.weighting, r.constants_provenance, r.created_at,
                       CASE WHEN state.status IN ('claimed', 'retryable', 'terminal')
                            THEN state.status ELSE 'incomplete' END AS status,
                       state.attempts, state.next_attempt_at, state.lease_expires_at,
                       state.last_error
                FROM recall_signal_requests r
                LEFT JOIN recall_shadow_judge_state state
                  ON state.recall_request_id = r.recall_request_id
                 AND state.label_source = %s
                 AND state.judge_protocol_version = %s
                LEFT JOIN recall_shadow_prompt_snapshot snapshot
                  ON snapshot.recall_request_id = r.recall_request_id
                 AND snapshot.label_source = %s
                 AND snapshot.judge_protocol_version = %s
                WHERE {where}
                ORDER BY r.created_at, r.recall_request_id
                LIMIT %s
                """,
                where=" AND ".join(conditions),
            )
            rows = self.db.fetchall(
                query,
                (
                    label_source,
                    judge_protocol_version,
                    label_source,
                    judge_protocol_version,
                    *identity_params,
                    label_source,
                    judge_protocol_version,
                    limit,
                ),
            )
            return self._normalize_cohort_rows(rows)

        if data_cutoff is None or completion_cutoff is None:
            raise ValueError("scored shadow cohorts require data_cutoff and completion_cutoff")
        resolved = self._resolve_scored_fences(
            label_source=label_source,
            judge_protocol_version=judge_protocol_version,
            session_id=session_id,
            recall_request_id=recall_request_id,
            project_id=project_id,
            constants_provenance=constants_provenance,
            judge_model_key=judge_model_key,
            judge_config_fingerprint=judge_config_fingerprint,
            weighting_regime_key=weighting_regime_key,
            data_cutoff=data_cutoff,
            completion_cutoff=completion_cutoff,
        )
        if resolved is None:
            return []
        judge_model_key, judge_config_fingerprint, weighting_regime_key = resolved
        conditions.extend(
            (
                f"NOT {_TOP_LABEL_MISSING}",
                "state.status = 'complete'",
                "snapshot.judge_model = %s",
                "snapshot.judge_config_fingerprint = %s",
                f"{_REGIME_KEY_SQL} = %s",
                "r.created_at <= %s",
                "snapshot.created_at <= %s",
            )
        )
        query = render_internal_sql(
            """
            SELECT r.session_id, r.recall_request_id, r.project_id, r.query,
                   r.weighting, r.constants_provenance, r.created_at,
                   state.status, state.attempts, state.next_attempt_at,
                   state.lease_expires_at, snapshot.system_prompt,
                   snapshot.query_text, snapshot.presented, snapshot.prompt_hash,
                   snapshot.judge_model, snapshot.judge_config_fingerprint,
                   snapshot.created_at AS snapshot_created_at,
                   {regime} AS weighting_regime_key
            FROM recall_signal_requests r
            JOIN recall_shadow_judge_state state
              ON state.recall_request_id = r.recall_request_id
             AND state.label_source = %s
             AND state.judge_protocol_version = %s
            JOIN recall_shadow_prompt_snapshot snapshot
              ON snapshot.recall_request_id = r.recall_request_id
             AND snapshot.label_source = %s
             AND snapshot.judge_protocol_version = %s
            WHERE {where}
            ORDER BY r.created_at, r.recall_request_id
            LIMIT %s
            """,
            regime=_REGIME_KEY_SQL,
            where=" AND ".join(conditions),
        )
        rows = self.db.fetchall(
            query,
            (
                label_source,
                judge_protocol_version,
                label_source,
                judge_protocol_version,
                *identity_params,
                label_source,
                judge_protocol_version,
                judge_model_key,
                judge_config_fingerprint,
                weighting_regime_key,
                data_cutoff,
                completion_cutoff,
                limit,
            ),
        )
        return self._normalize_cohort_rows(rows)

    def fetch_shadow_replay_rows(
        self,
        *,
        phase: ShadowCohortPhase = "fitting",
        label_source: str,
        candidate_scope: str,
        judge_protocol_version: str,
        weighting_regime_key: str,
        judge_model_key: str,
        judge_config_fingerprint: str,
        data_cutoff: datetime,
        completion_cutoff: datetime,
        project_id: str | None,
        limit: int,
        constants_provenance: str | None = None,
        request_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Project request-aligned replay rows from an admitted shadow cohort."""
        if phase not in {"fitting", "drift", "audit_scored"}:
            raise ValueError(
                "shadow replay rows support only fitting, drift, or audit_scored cohorts"
            )
        if candidate_scope not in {"full", "injected"}:
            raise ValueError("candidate_scope must be 'full' or 'injected'")
        cohort = self.shadow_cohort_query(
            phase,
            label_source=label_source,
            judge_protocol_version=judge_protocol_version,
            project_id=project_id,
            constants_provenance=constants_provenance,
            judge_model_key=judge_model_key,
            judge_config_fingerprint=judge_config_fingerprint,
            weighting_regime_key=weighting_regime_key,
            data_cutoff=data_cutoff,
            completion_cutoff=completion_cutoff,
            limit=limit,
        )
        admitted_ids = [str(row["recall_request_id"]) for row in cohort]
        if request_ids is not None:
            requested = set(request_ids)
            admitted_ids = [request_id for request_id in admitted_ids if request_id in requested]
        if not admitted_ids:
            return []
        placeholders = ", ".join("%s" for _ in admitted_ids)
        scope_condition = "AND o.outcome = 'injected'" if candidate_scope == "injected" else ""
        query = render_internal_sql(
            """
            SELECT h.session_id, h.recall_request_id, h.memory_id, h.project_id,
                   h.rank, h.content_hash, h.search_via, h.similarity,
                   h.raw_semantic_score, h.temporal_decay_factor,
                   h.ranking_score, h.ranking_mode, h.graph_score,
                   h.edge_cosine, h.edge_support_norm, h.edge_weight_blend,
                   h.edge_decay_factor, o.outcome, o.drop_reason,
                   o.injection_position, o.injection_group, o.turn_seq,
                   u.label_source, u.judge_useful, u.judge_confidence,
                   u.judge_model, u.judge_protocol_version,
                   r.rrf_applied, r.weighting, r.caller,
                   r.graph_synthetic_similarity_discount,
                   r.constants_provenance, r.created_at AS request_created_at,
                   snapshot.system_prompt, snapshot.query_text,
                   snapshot.prompt_hash, snapshot.presented,
                   snapshot.created_at AS snapshot_created_at,
                   snapshot.judge_config_fingerprint
            FROM recall_signal_hits h
            JOIN recall_signal_requests r
              ON r.session_id = h.session_id
             AND r.recall_request_id = h.recall_request_id
            JOIN recall_usefulness u
              ON u.recall_request_id = h.recall_request_id
             AND u.memory_id = h.memory_id
             AND u.label_source = %s
             AND u.judge_protocol_version = %s
            JOIN recall_shadow_prompt_snapshot snapshot
              ON snapshot.recall_request_id = h.recall_request_id
             AND snapshot.label_source = %s
             AND snapshot.judge_protocol_version = %s
            LEFT JOIN recall_injection_outcomes o
              ON o.recall_request_id = h.recall_request_id
             AND o.memory_id = h.memory_id
            WHERE h.recall_request_id IN ({placeholders})
              {scope}
            ORDER BY r.created_at, h.recall_request_id, h.rank
            """,
            placeholders=placeholders,
            scope=scope_condition,
        )
        rows = self.db.fetchall(
            query,
            (
                label_source,
                judge_protocol_version,
                label_source,
                judge_protocol_version,
                *admitted_ids,
            ),
        )
        return self._normalize_cohort_rows(rows)

    def insert_audit_verdicts(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        cohort_digest: str,
        sample_digest: str,
    ) -> int:
        """Persist immutable human verdicts bound to one cohort sample."""
        if not cohort_digest or not sample_digest:
            raise ValueError("cohort_digest and sample_digest are required")
        inserted = 0
        with self.db.transaction() as transaction:
            for row in rows:
                request_id = str(row.get("request_id") or "")
                memory_id = str(row.get("memory_id") or "")
                prompt_hash = str(row.get("prompt_hash") or "")
                reviewer = str(row.get("reviewer") or "")
                verdict = row.get("human_verdict")
                if (
                    not request_id
                    or not memory_id
                    or not prompt_hash
                    or not reviewer
                    or not isinstance(verdict, bool)
                ):
                    raise ValueError(
                        "audit verdict rows require ids, prompt hash, reviewer, and bool"
                    )
                snapshot = transaction.execute(
                    """
                    SELECT 1 FROM recall_shadow_prompt_snapshot
                    WHERE recall_request_id = %s AND prompt_hash = %s
                    """,
                    (request_id, prompt_hash),
                ).fetchone()
                if snapshot is None:
                    raise ValueError(f"prompt_hash does not match snapshot for {request_id}")
                cursor = transaction.execute(
                    """
                    INSERT INTO recall_shadow_audit_verdicts
                        (cohort_digest, sample_digest, request_id, memory_id,
                         prompt_hash, human_verdict, reviewer)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (cohort_digest, request_id, memory_id) DO NOTHING
                    """,
                    (
                        cohort_digest,
                        sample_digest,
                        request_id,
                        memory_id,
                        prompt_hash,
                        verdict,
                        reviewer,
                    ),
                )
                inserted += cursor.rowcount
                stored = transaction.execute(
                    """
                    SELECT sample_digest, prompt_hash, human_verdict, reviewer
                    FROM recall_shadow_audit_verdicts
                    WHERE cohort_digest = %s AND request_id = %s AND memory_id = %s
                    """,
                    (cohort_digest, request_id, memory_id),
                ).fetchone()
                expected = {
                    "sample_digest": sample_digest,
                    "prompt_hash": prompt_hash,
                    "human_verdict": verdict,
                    "reviewer": reviewer,
                }
                if stored is None or dict(stored) != expected:
                    raise ValueError(
                        f"conflicting audit verdict for {cohort_digest}/{request_id}/{memory_id}"
                    )
        return inserted

    def fetch_audit_verdicts(
        self,
        cohort_digest: str,
        sample_digest: str,
        *,
        expected_prompt_hashes: Mapping[tuple[str, str], str] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch exact-sample verdicts and reject stale presentation bindings."""
        rows = self.db.fetchall(
            """
            SELECT cohort_digest, sample_digest, request_id, memory_id,
                   prompt_hash, human_verdict, reviewer, created_at
            FROM recall_shadow_audit_verdicts
            WHERE cohort_digest = %s AND sample_digest = %s
            ORDER BY request_id, memory_id
            """,
            (cohort_digest, sample_digest),
        )
        result = [dict(row) for row in rows]
        for row in result:
            key = (str(row["request_id"]), str(row["memory_id"]))
            if expected_prompt_hashes is not None:
                expected = expected_prompt_hashes.get(key)
                if expected is None or expected != row["prompt_hash"]:
                    raise ValueError(f"prompt_hash mismatch for {key[0]}/{key[1]}")
            snapshot = self.db.fetchone(
                """
                SELECT 1 FROM recall_shadow_prompt_snapshot
                WHERE recall_request_id = %s AND prompt_hash = %s
                """,
                (row["request_id"], row["prompt_hash"]),
            )
            if snapshot is None:
                raise ValueError(f"prompt_hash no longer matches snapshot for {key[0]}")
        return result

    def _resolve_scored_fences(
        self,
        *,
        label_source: str,
        judge_protocol_version: str,
        session_id: str | None,
        recall_request_id: str | None,
        project_id: str | None,
        constants_provenance: str | None,
        judge_model_key: str | None,
        judge_config_fingerprint: str | None,
        weighting_regime_key: str | None,
        data_cutoff: datetime,
        completion_cutoff: datetime,
    ) -> tuple[str, str, str] | None:
        values = {
            "judge_model_key": judge_model_key,
            "judge_config_fingerprint": judge_config_fingerprint,
            "weighting_regime_key": weighting_regime_key,
        }
        expressions = {
            "judge_model_key": "snapshot.judge_model",
            "judge_config_fingerprint": "snapshot.judge_config_fingerprint",
            "weighting_regime_key": _REGIME_KEY_SQL,
        }
        for dimension in values:
            if values[dimension] is not None:
                continue
            conditions = [
                *_IMMUTABLE_ELIGIBILITY,
                f"NOT {_TOP_LABEL_MISSING}",
                "state.status = 'complete'",
                "r.created_at <= %s",
                "snapshot.created_at <= %s",
            ]
            params: list[Any] = [
                label_source,
                judge_protocol_version,
                label_source,
                judge_protocol_version,
                label_source,
                judge_protocol_version,
                data_cutoff,
                completion_cutoff,
            ]
            if session_id is not None:
                conditions.append("r.session_id = %s")
                params.append(session_id)
            if recall_request_id is not None:
                conditions.append("r.recall_request_id = %s")
                params.append(recall_request_id)
            if project_id is not None:
                conditions.append("r.project_id = %s")
                params.append(project_id)
            if constants_provenance is not None:
                conditions.append("r.constants_provenance = %s")
                params.append(constants_provenance)
            for other_dimension, other_value in values.items():
                if other_dimension == dimension or other_value is None:
                    continue
                conditions.append(f"{expressions[other_dimension]} = %s")
                params.append(other_value)
            query = render_internal_sql(
                """
                SELECT {expression} AS fence_value, COUNT(*) AS request_count
                FROM recall_signal_requests r
                JOIN recall_shadow_judge_state state
                  ON state.recall_request_id = r.recall_request_id
                 AND state.label_source = %s
                 AND state.judge_protocol_version = %s
                JOIN recall_shadow_prompt_snapshot snapshot
                  ON snapshot.recall_request_id = r.recall_request_id
                 AND snapshot.label_source = %s
                 AND snapshot.judge_protocol_version = %s
                WHERE {where}
                GROUP BY {expression}
                ORDER BY {expression}
                """,
                expression=expressions[dimension],
                where=" AND ".join(conditions),
            )
            rows = self.db.fetchall(query, tuple(params))
            counts = {str(row["fence_value"]): int(row["request_count"]) for row in rows}
            if not counts:
                return None
            if len(counts) != 1:
                raise ShadowCohortAmbiguityError(dimension, counts)
            values[dimension] = next(iter(counts))
        model = values["judge_model_key"]
        fingerprint = values["judge_config_fingerprint"]
        regime = values["weighting_regime_key"]
        if model is None or fingerprint is None or regime is None:
            return None
        return model, fingerprint, regime

    @staticmethod
    def _normalize_cohort_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["weighting"] = _json_value(item.get("weighting"))
            if "presented" in item:
                item["presented"] = _json_value(item["presented"])
            normalized.append(item)
        return normalized

    def fetch_unshadowed_requests(
        self,
        session_id: str,
        *,
        label_source: str,
        judge_protocol_version: str,
        limit: int,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Return due, eligible requests and their ordered top-eight hits."""
        requests = self.shadow_cohort_query(
            "polling",
            session_id=session_id,
            label_source=label_source,
            judge_protocol_version=judge_protocol_version,
            limit=limit,
            now=now,
        )
        for request in requests:
            hits = self.db.fetchall(
                """
                SELECT memory_id, rank, content_hash, search_via, similarity,
                       raw_semantic_score, temporal_decay_factor, ranking_score,
                       ranking_mode, graph_score, edge_cosine, edge_support_norm,
                       edge_weight_blend, edge_decay_factor
                FROM recall_signal_hits
                WHERE recall_request_id = %s
                ORDER BY rank
                LIMIT 8
                """,
                (request["recall_request_id"],),
            )
            request["hits"] = [dict(hit) for hit in hits]
        return requests

    def claim_shadow_request(
        self,
        session_id: str,
        recall_request_id: str,
        *,
        label_source: str,
        judge_protocol_version: str,
        now: datetime | None = None,
    ) -> str | None:
        """Claim one due request under a short per-session lock."""
        current_time = now or utc_now()
        with self.db.transaction_immediate(ShadowJudgePoll(session_id)) as transaction:
            due = self.shadow_cohort_query(
                "polling",
                session_id=session_id,
                recall_request_id=recall_request_id,
                label_source=label_source,
                judge_protocol_version=judge_protocol_version,
                limit=1,
                now=current_time,
            )
            if not due:
                return None

            claim_token = str(uuid4())
            transaction.execute(
                """
                INSERT INTO recall_shadow_judge_state
                    (recall_request_id, label_source, judge_protocol_version,
                     status, attempts, lease_expires_at, claim_token, updated_at)
                VALUES (%s, %s, %s, 'claimed', 1, %s, %s, %s)
                ON CONFLICT (recall_request_id, label_source, judge_protocol_version)
                DO UPDATE SET
                    status = 'claimed',
                    attempts = recall_shadow_judge_state.attempts + 1,
                    next_attempt_at = NULL,
                    lease_expires_at = EXCLUDED.lease_expires_at,
                    claim_token = EXCLUDED.claim_token,
                    last_error = NULL,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    recall_request_id,
                    label_source,
                    judge_protocol_version,
                    current_time + SHADOW_CLAIM_LEASE,
                    claim_token,
                    current_time,
                ),
            )
            return claim_token

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
