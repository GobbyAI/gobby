"""Postgres hub storage for promoted recall-signal features, injection
outcomes, and usefulness labels.

Contract: docs/contracts/memory-usefulness-label.md (#17196). The canonical
labeled-row key is (project_id, session_id, recall_request_id, memory_id);
`fetch_fit_rows` materializes the fit-eligible join
features ⋈ injection_outcome ⋈ label on that key.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from gobby.storage.recall_shadow_claim_transitions import RecallShadowClaimTransitionMixin
from gobby.storage.recall_shadow_gate import RecallShadowGateStoreMixin
from gobby.storage.recall_shadow_labels import RecallShadowLabelStoreMixin
from gobby.storage.recall_shadow_sampling import RecallShadowSamplingMixin
from gobby.storage.recall_shadow_signals import (
    RecallShadowSignalStoreMixin,
)
from gobby.storage.recall_shadow_signals import (
    ShadowCohortAmbiguityError as ShadowCohortAmbiguityError,
)
from gobby.utils.datetime import utc_now
from gobby.utils.sql import render_internal_sql

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)

_HIT_FLOAT_FIELDS = (
    "similarity",
    "raw_semantic_score",
    "temporal_decay_factor",
    "ranking_score",
    "graph_score",
    "edge_cosine",
    "edge_support_norm",
    "edge_weight_blend",
    "edge_decay_factor",
)

INJECTION_DROP_REASONS = frozenset(
    {
        "already_injected",
        "review_lesson",
        "empty_content",
        "payload_empty",
        "budget",
        "other",
    }
)

LABEL_SOURCES = frozenset({"llm_judge", "ablation", "digest", "digest_shadow", "human"})
PRODUCIBLE_LABEL_SOURCES = LABEL_SOURCES - {"digest"}


def _parse_timestamp(value: Any) -> datetime:
    """Parse an event timestamp, falling back to now for missing/invalid values."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return utc_now()


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _json_value(value: Any) -> Any:
    """Decode a JSONB column that the hub layer may hand back as a string."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value
    return value


class RecallSignalStore(
    RecallShadowSignalStoreMixin,
    RecallShadowLabelStoreMixin,
    RecallShadowSamplingMixin,
    RecallShadowClaimTransitionMixin,
    RecallShadowGateStoreMixin,
):
    """CRUD for the recall-signal hub tables (#17196).

    All writes are idempotent (ON CONFLICT DO NOTHING) so JSONL backfills and
    at-least-once forward dual-writes can replay safely.
    """

    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    # -- promoted signal events (§3) ------------------------------------

    def insert_signal_event(self, event: dict[str, Any]) -> bool:
        """Insert one recall-signal event (request row + per-hit rows).

        Returns True when the request row was newly inserted, False when the
        (session_id, recall_request_id) key already existed or the event is
        missing its key. Hit rows piggyback on the same transaction.
        """
        session_id = event.get("session_id")
        recall_request_id = event.get("recall_request_id")
        if not session_id or not recall_request_id:
            return False

        project_id = event.get("project_id")
        created_at = _parse_timestamp(event.get("timestamp"))
        hits = event.get("hits") or []

        with self.db.transaction() as txn:
            cursor = txn.execute(
                """
                INSERT INTO recall_signal_requests
                (session_id, recall_request_id, project_id, caller, query,
                 merged_ids, returned_ids, rrf_applied,
                 graph_synthetic_similarity_discount, ranking_score_map,
                 graph_score_map, weighting, constants_provenance,
                 schema_version, created_at)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s::jsonb,
                        %s::jsonb, %s::jsonb, %s, %s, %s)
                ON CONFLICT (session_id, recall_request_id) DO NOTHING
                """,
                (
                    session_id,
                    recall_request_id,
                    project_id,
                    event.get("caller") or "memory.search",
                    event.get("query"),
                    json.dumps(list(event.get("merged_ids") or [])),
                    json.dumps(list(event.get("returned_ids") or [])),
                    bool(event.get("rrf_applied")),
                    _float_or_none(event.get("graph_synthetic_similarity_discount")),
                    json.dumps(event.get("ranking_score_map") or {}),
                    json.dumps(event.get("graph_score_map") or {}),
                    json.dumps(event.get("weighting") or {}),
                    event.get("constants_provenance") or "static",
                    int(event.get("schema_version") or 0),
                    created_at,
                ),
            )
            inserted = cursor.rowcount > 0
            for hit in hits:
                if not isinstance(hit, dict) or not hit.get("memory_id"):
                    continue
                floats = {field: _float_or_none(hit.get(field)) for field in _HIT_FLOAT_FIELDS}
                txn.execute(
                    """
                    INSERT INTO recall_signal_hits
                    (session_id, recall_request_id, memory_id, project_id, rank,
                     search_via, similarity, raw_semantic_score,
                     temporal_decay_factor, ranking_score, ranking_mode,
                     graph_score, edge_cosine, edge_support_norm,
                     edge_weight_blend, edge_decay_factor, content_hash, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s)
                    ON CONFLICT (recall_request_id, memory_id) DO NOTHING
                    """,
                    (
                        session_id,
                        recall_request_id,
                        str(hit["memory_id"]),
                        project_id,
                        int(hit.get("rank") or 0),
                        hit.get("search_via"),
                        floats["similarity"],
                        floats["raw_semantic_score"],
                        floats["temporal_decay_factor"],
                        floats["ranking_score"],
                        hit.get("ranking_mode"),
                        floats["graph_score"],
                        floats["edge_cosine"],
                        floats["edge_support_norm"],
                        floats["edge_weight_blend"],
                        floats["edge_decay_factor"],
                        hit.get("content_hash"),
                        created_at,
                    ),
                )
        return inserted

    def load_signal_events_jsonl(self, path: Path) -> int:
        """Backfill promoted signal tables from a recall_signal.jsonl file.

        Skips unparseable lines and events without join keys. Returns the
        number of newly inserted request rows.
        """
        inserted = 0
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(event, dict) and self.insert_signal_event(event):
                    inserted += 1
        return inserted

    # -- injection outcomes (§5) ----------------------------------------

    def record_injection_outcomes(self, rows: list[dict[str, Any]]) -> int:
        """Insert injection-outcome rows; invalid rows are skipped.

        Each row needs session_id, recall_request_id, memory_id, and outcome
        ('injected' with injection_position, or 'filtered' with drop_reason).
        Returns the number of newly inserted rows.
        """
        inserted = 0
        with self.db.transaction() as txn:
            for row in rows:
                outcome = row.get("outcome")
                if (
                    not row.get("session_id")
                    or not row.get("recall_request_id")
                    or not row.get("memory_id")
                    or outcome not in ("injected", "filtered")
                ):
                    logger.debug("Skipping invalid injection-outcome row: %s", row)
                    continue
                drop_reason = row.get("drop_reason")
                if outcome == "filtered" and drop_reason not in INJECTION_DROP_REASONS:
                    drop_reason = "other"
                cursor = txn.execute(
                    """
                    INSERT INTO recall_injection_outcomes
                    (session_id, recall_request_id, memory_id, project_id,
                     outcome, drop_reason, drop_detail, injection_position,
                     injection_group, turn_seq, caller)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (recall_request_id, memory_id) DO NOTHING
                    """,
                    (
                        row["session_id"],
                        row["recall_request_id"],
                        str(row["memory_id"]),
                        row.get("project_id"),
                        outcome,
                        drop_reason if outcome == "filtered" else None,
                        row.get("drop_detail"),
                        row.get("injection_position") if outcome == "injected" else None,
                        row.get("injection_group"),
                        row.get("turn_seq"),
                        row.get("caller") or "memory.recall",
                    ),
                )
                inserted += cursor.rowcount
        return inserted

    # -- usefulness labels (§6) ------------------------------------------

    def insert_usefulness_label(self, row: dict[str, Any]) -> bool:
        """Append one usefulness-label row (idempotent on the §6 unique key)."""
        label_source = row.get("label_source")
        if (
            not row.get("session_id")
            or not row.get("recall_request_id")
            or not row.get("memory_id")
            or label_source not in PRODUCIBLE_LABEL_SOURCES
            or not isinstance(row.get("judge_useful"), bool)
            or not row.get("judge_protocol_version")
        ):
            return False
        cursor = self.db.execute(
            """
            INSERT INTO recall_usefulness
            (project_id, session_id, recall_request_id, memory_id, label_source,
             judge_useful, judge_confidence, judge_model, judge_protocol_version,
             position_randomized, length_controlled, ablation_delta,
             ablation_method, rationale, feature_extractor_version, labeled_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (recall_request_id, memory_id, label_source,
                         judge_protocol_version) DO NOTHING
            """,
            (
                row.get("project_id"),
                row["session_id"],
                row["recall_request_id"],
                str(row["memory_id"]),
                label_source,
                row["judge_useful"],
                _float_or_none(row.get("judge_confidence")),
                row.get("judge_model"),
                row["judge_protocol_version"],
                bool(row.get("position_randomized")),
                bool(row.get("length_controlled")),
                _float_or_none(row.get("ablation_delta")),
                row.get("ablation_method"),
                row.get("rationale"),
                row.get("feature_extractor_version"),
                _parse_timestamp(row.get("labeled_at") or row.get("timestamp")),
            ),
        )
        return cursor.rowcount > 0

    def backfill_usefulness_labels_jsonl(self, path: Path) -> int:
        """Load retrospective labels (Phase 0b calibration-dataset rows).

        Accepts the #17193 harness row shape: `retro_id` maps to
        recall_request_id (synthetic `retro:<session_id>:<turn_seq>` ids that
        never join the signal table; calibration/volume use only).
        Returns the number of newly inserted label rows.
        """
        inserted = 0
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(row, dict):
                    continue
                if "recall_request_id" not in row and row.get("retro_id"):
                    row = {**row, "recall_request_id": row["retro_id"]}
                if self.insert_usefulness_label(row):
                    inserted += 1
        return inserted

    # -- analytic join (§3 labeled rows) ---------------------------------

    def fetch_fit_rows(
        self,
        *,
        recall_request_id: str | None = None,
        project_id: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return fit-eligible labeled rows: hit ⋈ outcome(injected) ⋈ label.

        Joined on (recall_request_id, memory_id) per the contract; request-level
        weighting/rrf context is attached from recall_signal_requests.
        """
        conditions = ["o.outcome = 'injected'"]
        params: list[Any] = []
        if recall_request_id is not None:
            conditions.append("h.recall_request_id = %s")
            params.append(recall_request_id)
        if project_id is not None:
            conditions.append("r.project_id = %s")
            params.append(project_id)
        params.append(limit)

        query = render_internal_sql(
            """
            SELECT h.session_id, h.recall_request_id, h.memory_id, h.project_id,
                   h.rank, h.search_via, h.similarity, h.raw_semantic_score,
                   h.temporal_decay_factor, h.ranking_score, h.ranking_mode,
                   h.graph_score, h.edge_cosine, h.edge_support_norm,
                   h.edge_weight_blend, h.edge_decay_factor,
                   o.injection_position, o.injection_group, o.turn_seq,
                   u.label_source, u.judge_useful, u.judge_confidence,
                   u.judge_protocol_version, u.ablation_delta,
                   r.rrf_applied, r.weighting, r.caller
            FROM recall_signal_hits h
            JOIN recall_injection_outcomes o
              ON o.recall_request_id = h.recall_request_id
             AND o.memory_id = h.memory_id
            JOIN recall_usefulness u
              ON u.recall_request_id = h.recall_request_id
             AND u.memory_id = h.memory_id
            JOIN recall_signal_requests r
              ON r.session_id = h.session_id
             AND r.recall_request_id = h.recall_request_id
            WHERE {where}
            ORDER BY h.recall_request_id, h.rank
            LIMIT %s
            """,
            where=" AND ".join(conditions),
        )
        cursor = self.db.execute(query, tuple(params))
        rows = cursor.fetchall()
        return [{**dict(row), "weighting": _json_value(row["weighting"])} for row in rows]

    def fetch_replay_rows(
        self,
        *,
        label_source: str,
        project_id: str | None = None,
        limit: int = 5000,
        since: datetime | None = None,
        candidate_scope: str | None = None,
        judge_protocol_version: str | None = None,
        query_construction_version: str | None = None,
        weighting_regime_key: str | None = None,
        judge_model_key: str | None = None,
        judge_config_fingerprint: str | None = None,
        data_cutoff: datetime | None = None,
        completion_cutoff: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Return ALL injected hits with an optional label from one source.

        The offline replay/fit harness (``gobby.memory.recall_fit``, #17197)
        needs every injected row — labeled rows form preference pairs,
        unlabeled rows feed position-propensity denominators. Hence LEFT JOIN
        on ``recall_usefulness`` where ``fetch_fit_rows`` inner-joins.

        ``label_source`` is required: the contract forbids silently mixing
        label streams (digest vs llm_judge) in a fit. When a pair carries
        several append-only label rows under that source, the newest
        ``labeled_at`` wins. ``judge_useful`` is NULL for unlabeled rows.

        ``since`` bounds the window by request ``created_at`` — the drift
        monitor (#17201) replays only the recent live window.

        ``query_construction_version`` fences a ``digest_shadow`` cohort to one
        query era and is forwarded verbatim, ``None`` included: absence of the
        key *is* the legacy era, so it cannot be rejected as a missing fence.
        Non-shadow label streams predate the fence and their query is unchanged.
        """
        if label_source == "digest_shadow":
            required = {
                "candidate_scope": candidate_scope,
                "judge_protocol_version": judge_protocol_version,
                "weighting_regime_key": weighting_regime_key,
                "judge_model_key": judge_model_key,
                "judge_config_fingerprint": judge_config_fingerprint,
                "data_cutoff": data_cutoff,
                "completion_cutoff": completion_cutoff,
            }
            missing = [name for name, value in required.items() if value is None]
            if missing:
                raise ValueError(f"missing digest_shadow replay fences: {', '.join(missing)}")
            if since is not None:
                raise ValueError("since is not part of a digest_shadow replay cohort")
            return self.fetch_shadow_replay_rows(
                label_source=label_source,
                candidate_scope=cast(str, candidate_scope),
                judge_protocol_version=cast(str, judge_protocol_version),
                query_construction_version=query_construction_version,
                weighting_regime_key=cast(str, weighting_regime_key),
                judge_model_key=cast(str, judge_model_key),
                judge_config_fingerprint=cast(str, judge_config_fingerprint),
                data_cutoff=cast(datetime, data_cutoff),
                completion_cutoff=cast(datetime, completion_cutoff),
                project_id=project_id,
                limit=limit,
            )

        conditions = ["o.outcome = 'injected'"]
        params: list[Any] = [label_source]
        if project_id is not None:
            conditions.append("r.project_id = %s")
            params.append(project_id)
        if since is not None:
            conditions.append("r.created_at >= %s")
            params.append(since)
        params.append(limit)

        query = render_internal_sql(
            """
            SELECT h.session_id, h.recall_request_id, h.memory_id, h.project_id,
                   h.rank, h.search_via, h.similarity, h.raw_semantic_score,
                   h.temporal_decay_factor, h.ranking_score, h.ranking_mode,
                   h.graph_score, h.edge_cosine, h.edge_support_norm,
                   h.edge_weight_blend, h.edge_decay_factor,
                   o.injection_position, o.injection_group, o.turn_seq,
                   u.label_source, u.judge_useful, u.judge_confidence,
                   u.judge_protocol_version,
                   r.rrf_applied, r.weighting, r.caller,
                   r.graph_synthetic_similarity_discount
            FROM recall_signal_hits h
            JOIN recall_injection_outcomes o
              ON o.recall_request_id = h.recall_request_id
             AND o.memory_id = h.memory_id
            JOIN recall_signal_requests r
              ON r.session_id = h.session_id
             AND r.recall_request_id = h.recall_request_id
            LEFT JOIN LATERAL (
                SELECT label_source, judge_useful, judge_confidence,
                       judge_protocol_version
                FROM recall_usefulness u
                WHERE u.recall_request_id = h.recall_request_id
                  AND u.memory_id = h.memory_id
                  AND u.label_source = %s
                ORDER BY u.labeled_at DESC, u.id DESC
                LIMIT 1
            ) u ON TRUE
            WHERE {where}
            ORDER BY h.recall_request_id, h.rank
            LIMIT %s
            """,
            where=" AND ".join(conditions),
        )
        cursor = self.db.execute(query, tuple(params))
        rows = cursor.fetchall()
        return [{**dict(row), "weighting": _json_value(row["weighting"])} for row in rows]
