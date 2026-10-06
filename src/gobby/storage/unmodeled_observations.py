"""Storage for bounded transcript unknown-observation telemetry."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4

from gobby.storage.hub.protocol import Cursor, HubDatabase, Transaction
from gobby.utils.datetime import normalize_datetime_model, require_stored_datetime

logger = logging.getLogger(__name__)


@contextmanager
def _batch_transaction(db: HubDatabase, batch_id: str) -> Iterator[Transaction]:
    """Time acquisition and transaction exit without recording database details."""
    started = perf_counter()
    exiting: float | None = None
    logger.info("unmodeled_batch batch=%s phase=connection event=started", batch_id)
    try:
        with db.transaction() as txn:
            logger.info(
                "unmodeled_batch batch=%s phase=connection event=finished elapsed_s=%.6f",
                batch_id,
                perf_counter() - started,
            )
            try:
                yield txn
            finally:
                exiting = perf_counter()
                logger.info("unmodeled_batch batch=%s phase=commit event=started", batch_id)
    finally:
        if exiting is not None:
            logger.info(
                "unmodeled_batch batch=%s phase=commit event=finished elapsed_s=%.6f",
                batch_id,
                perf_counter() - exiting,
            )


def _batch_execute(
    txn: Transaction, batch_id: str, phase: str, sql: str, parameters: tuple[str]
) -> Cursor:
    started = perf_counter()
    logger.info("unmodeled_batch batch=%s phase=%s event=started", batch_id, phase)
    try:
        return txn.execute(sql, parameters)
    finally:
        logger.info(
            "unmodeled_batch batch=%s phase=%s event=finished elapsed_s=%.6f",
            batch_id,
            phase,
            perf_counter() - started,
        )


_HASH_VERSION = "unmodeled-observation-sample-v2"
_MAX_KEYS = 50
_MAX_DICT_ITEMS = 20
_MAX_LIST_ITEMS = 8
_MAX_STRING_CHARS = 240
_MAX_DEPTH = 3
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
)

COUNT_SEMANTICS = "retention-window distinct transcript occurrences"


@dataclass(frozen=True)
class UnmodeledObservationInput:
    # None when the transcript block carries no resolvable session uuid;
    # the columns are native uuid and dedup is NULLS NOT DISTINCT.
    session_id: str | None
    source: str
    kind: str
    name: str
    sample: dict[str, Any]
    server_name: str = ""
    tool_type: str = ""
    source_ref: str = ""
    source_line: int | None = None


@normalize_datetime_model(
    required=(
        "first_seen_at",
        "last_seen_at",
    )
)
@dataclass(frozen=True)
class UnmodeledObservationRow:
    source: str
    kind: str
    name: str
    server_name: str
    tool_type: str
    count: int
    first_seen_at: datetime
    last_seen_at: datetime
    example_session_id: str | None
    sample_keys: list[str]
    sample_hash: str


class UnmodeledObservationStore:
    """Transactional writer/query surface for unmodeled transcript observations."""

    def __init__(self, db: HubDatabase) -> None:
        self._db = db

    def record(self, observation: UnmodeledObservationInput) -> bool:
        """Record one occurrence; return True only when aggregate count incremented."""
        source_ref = observation.source_ref or ""
        if not source_ref:
            logger.info(
                "Unmodeled transcript block observed without stable source_ref",
                extra={
                    "session_id": observation.session_id,
                    "source": observation.source,
                    "kind": observation.kind,
                    "observation_name": observation.name,
                    "sample_keys": sample_keys(observation.sample),
                },
            )
            return False

        keys = sample_keys(observation.sample)
        sample_hash = stable_sample_hash(observation.sample)
        server_name = observation.server_name or ""
        tool_type = observation.tool_type or ""

        with self._db.transaction() as txn:
            inserted = txn.execute(
                """
                INSERT INTO unmodeled_observation_events (
                    id,
                    session_id,
                    source,
                    kind,
                    name,
                    server_name,
                    tool_type,
                    source_ref,
                    source_line,
                    sample_keys,
                    sample_hash
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (
                    session_id,
                    source,
                    kind,
                    name,
                    server_name,
                    tool_type,
                    source_ref,
                    sample_hash
                ) DO NOTHING
                RETURNING id
                """,
                (
                    str(uuid4()),
                    observation.session_id,
                    observation.source,
                    observation.kind,
                    observation.name,
                    server_name,
                    tool_type,
                    source_ref,
                    observation.source_line,
                    json.dumps(keys),
                    sample_hash,
                ),
            ).fetchone()

            if inserted is not None:
                txn.execute(
                    """
                    INSERT INTO unmodeled_observations (
                        source,
                        kind,
                        name,
                        server_name,
                        tool_type,
                        count,
                        first_seen_at,
                        last_seen_at,
                        example_session_id,
                        sample_keys,
                        sample_hash
                    )
                    VALUES (%s, %s, %s, %s, %s, 1, NOW(), NOW(), %s, %s::jsonb, %s)
                    ON CONFLICT (source, kind, name, server_name, tool_type)
                    DO UPDATE SET
                        count = unmodeled_observations.count + 1,
                        last_seen_at = EXCLUDED.last_seen_at,
                        example_session_id = EXCLUDED.example_session_id,
                        sample_keys = EXCLUDED.sample_keys,
                        sample_hash = EXCLUDED.sample_hash
                    """,
                    (
                        observation.source,
                        observation.kind,
                        observation.name,
                        server_name,
                        tool_type,
                        observation.session_id,
                        json.dumps(keys),
                        sample_hash,
                    ),
                )
                return True

            txn.execute(
                """
                UPDATE unmodeled_observation_events
                SET last_seen_at = NOW()
                WHERE session_id IS NOT DISTINCT FROM %s
                  AND source = %s
                  AND kind = %s
                  AND name = %s
                  AND server_name = %s
                  AND tool_type = %s
                  AND source_ref = %s
                  AND sample_hash = %s
                """,
                (
                    observation.session_id,
                    observation.source,
                    observation.kind,
                    observation.name,
                    server_name,
                    tool_type,
                    source_ref,
                    sample_hash,
                ),
            )
            txn.execute(
                """
                UPDATE unmodeled_observations
                SET last_seen_at = NOW()
                WHERE source = %s
                  AND kind = %s
                  AND name = %s
                  AND server_name = %s
                  AND tool_type = %s
                """,
                (
                    observation.source,
                    observation.kind,
                    observation.name,
                    server_name,
                    tool_type,
                ),
            )
            return False

    def record_many(self, observations: list[UnmodeledObservationInput]) -> int:
        """Persist one rendered window in three statements and one transaction.

        Keep the same distinct-occurrence and last-seen semantics as ``record``;
        only the number of database round trips changes.
        """
        events: dict[tuple[str, ...], dict[str, Any]] = {}
        for observation in observations:
            if not observation.source_ref:
                logger.info(
                    "Unmodeled transcript block observed without stable source_ref",
                    extra={
                        "session_id": observation.session_id,
                        "source": observation.source,
                        "kind": observation.kind,
                        "observation_name": observation.name,
                        "sample_keys": sample_keys(observation.sample),
                    },
                )
                continue
            event = {
                "id": str(uuid4()),
                "session_id": str(UUID(observation.session_id)) if observation.session_id else None,
                "source": observation.source,
                "kind": observation.kind,
                "name": observation.name,
                "server_name": observation.server_name or "",
                "tool_type": observation.tool_type or "",
                "source_ref": observation.source_ref,
                "source_line": observation.source_line,
                "sample_keys": sample_keys(observation.sample),
                "sample_hash": stable_sample_hash(observation.sample),
            }
            key = tuple(
                str(event[field] or "")
                for field in (
                    "source",
                    "kind",
                    "name",
                    "server_name",
                    "tool_type",
                    "session_id",
                    "source_ref",
                    "sample_hash",
                )
            )
            events[key] = event
        if not events:
            return 0
        # Consistent lock order also lets overlapping concurrent batches compose.
        payload = json.dumps([events[key] for key in sorted(events)])
        batch_id = uuid4().hex
        with _batch_transaction(self._db, batch_id) as txn:
            inserted = _batch_execute(
                txn,
                batch_id,
                "insert_events",
                """
                INSERT INTO unmodeled_observation_events (
                    id, session_id, source, kind, name, server_name, tool_type,
                    source_ref, source_line, sample_keys, sample_hash
                )
                SELECT id, session_id, source, kind, name, server_name, tool_type,
                       source_ref, source_line, sample_keys, sample_hash
                FROM jsonb_to_recordset(%s::jsonb) AS x(
                    id uuid, session_id uuid, source text, kind text, name text,
                    server_name text, tool_type text, source_ref text, source_line integer,
                    sample_keys jsonb, sample_hash text
                )
                ON CONFLICT (session_id, source, kind, name, server_name, tool_type,
                             source_ref, sample_hash) DO NOTHING
                RETURNING source, kind, name, server_name, tool_type,
                          session_id, source_ref, sample_hash
                """,
                (payload,),
            ).fetchall()
            _batch_execute(
                txn,
                batch_id,
                "refresh_events",
                """
                UPDATE unmodeled_observation_events AS event SET last_seen_at = NOW()
                FROM jsonb_to_recordset(%s::jsonb) AS x(
                    session_id uuid, source text, kind text, name text,
                    server_name text, tool_type text, source_ref text, sample_hash text
                )
                WHERE event.session_id IS NOT DISTINCT FROM x.session_id
                  AND event.source = x.source AND event.kind = x.kind AND event.name = x.name
                  AND event.server_name = x.server_name AND event.tool_type = x.tool_type
                  AND event.source_ref = x.source_ref AND event.sample_hash = x.sample_hash
                """,
                (payload,),
            )
            counts: dict[tuple[str, ...], int] = {}
            fields = ("source", "kind", "name", "server_name", "tool_type")
            novel: set[tuple[str, ...]] = set()
            for row in inserted:
                key = tuple(str(row[field]) for field in fields)
                counts[key] = counts.get(key, 0) + 1
                novel.add(
                    (
                        *key,
                        str(UUID(str(row["session_id"]))) if row["session_id"] else "",
                        str(row["source_ref"]),
                        str(row["sample_hash"]),
                    )
                )
            aggregates: dict[tuple[str, ...], dict[str, Any]] = {}
            for identity, event in events.items():
                key = tuple(str(event[field]) for field in fields)
                if identity in novel or key not in aggregates:
                    aggregates[key] = {**event, "count": counts.get(key, 0)}
            _batch_execute(
                txn,
                batch_id,
                "refresh_aggregates",
                """
                WITH incoming AS (
                    SELECT * FROM jsonb_to_recordset(%s::jsonb) AS x(
                        source text, kind text, name text, server_name text, tool_type text,
                        count bigint, session_id uuid, sample_keys jsonb, sample_hash text
                    )
                ), refreshed AS (
                    UPDATE unmodeled_observations AS aggregate SET last_seen_at = NOW()
                    FROM incoming AS x
                    WHERE x.count = 0
                      AND aggregate.source = x.source AND aggregate.kind = x.kind
                      AND aggregate.name = x.name AND aggregate.server_name = x.server_name
                      AND aggregate.tool_type = x.tool_type
                )
                INSERT INTO unmodeled_observations (
                    source, kind, name, server_name, tool_type, count, first_seen_at,
                    last_seen_at, example_session_id, sample_keys, sample_hash
                )
                SELECT source, kind, name, server_name, tool_type, count, NOW(), NOW(),
                       session_id, sample_keys, sample_hash
                FROM incoming WHERE count > 0
                ON CONFLICT (source, kind, name, server_name, tool_type) DO UPDATE SET
                    count = unmodeled_observations.count + EXCLUDED.count,
                    last_seen_at = EXCLUDED.last_seen_at,
                    example_session_id = CASE WHEN EXCLUDED.count > 0
                        THEN EXCLUDED.example_session_id
                        ELSE unmodeled_observations.example_session_id END,
                    sample_keys = CASE WHEN EXCLUDED.count > 0
                        THEN EXCLUDED.sample_keys ELSE unmodeled_observations.sample_keys END,
                    sample_hash = CASE WHEN EXCLUDED.count > 0
                        THEN EXCLUDED.sample_hash ELSE unmodeled_observations.sample_hash END
                """,
                (json.dumps([aggregates[key] for key in sorted(aggregates)]),),
            )
        return len(inserted)

    def list_observations(
        self,
        *,
        source: str | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[UnmodeledObservationRow]:
        """Return aggregate rows sorted as a discovery worklist."""
        clauses: list[str] = []
        params: list[Any] = []
        if source:
            clauses.append("source = %s")
            params.append(source)
        if kind:
            clauses.append("kind = %s")
            params.append(kind)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 500)))

        rows = self._db.fetchall(
            f"""
            SELECT
                source,
                kind,
                name,
                server_name,
                tool_type,
                count,
                first_seen_at,
                last_seen_at,
                example_session_id,
                sample_keys,
                sample_hash
            FROM unmodeled_observations
            {where_sql}
            ORDER BY count DESC, last_seen_at DESC, source ASC, kind ASC, name ASC
            LIMIT %s
            """,
            tuple(params),
        )
        return [_row_from_db(row) for row in rows]

    def prune_events_older_than(self, *, retention_days: int) -> int:
        """Delete old occurrence guards; aggregate counts are retention-window counts."""
        days = max(1, int(retention_days))
        with self._db.transaction() as txn:
            deleted_rows = txn.execute(
                """
                DELETE FROM unmodeled_observation_events
                WHERE last_seen_at < NOW() - (%s * INTERVAL '1 day')
                RETURNING source, kind, name, server_name, tool_type
                """,
                (days,),
            ).fetchall()
            affected_keys = {
                (
                    row["source"],
                    row["kind"],
                    row["name"],
                    row["server_name"],
                    row["tool_type"],
                )
                for row in deleted_rows
            }
            for key in affected_keys:
                remaining = txn.execute(
                    """
                    SELECT
                        COUNT(*) AS count,
                        MIN(first_seen_at) AS first_seen_at,
                        MAX(last_seen_at) AS last_seen_at,
                        (ARRAY_AGG(session_id ORDER BY last_seen_at DESC, first_seen_at DESC))[1]
                            AS example_session_id,
                        (ARRAY_AGG(sample_keys ORDER BY last_seen_at DESC, first_seen_at DESC))[1]
                            AS sample_keys,
                        (ARRAY_AGG(sample_hash ORDER BY last_seen_at DESC, first_seen_at DESC))[1]
                            AS sample_hash
                    FROM unmodeled_observation_events
                    WHERE source = %s
                      AND kind = %s
                      AND name = %s
                      AND server_name = %s
                      AND tool_type = %s
                    """,
                    key,
                ).fetchone()
                if remaining is not None and int(remaining["count"]) > 0:
                    txn.execute(
                        """
                        UPDATE unmodeled_observations
                        SET
                            count = %s,
                            first_seen_at = %s,
                            last_seen_at = %s,
                            example_session_id = %s,
                            sample_keys = %s::jsonb,
                            sample_hash = %s
                        WHERE source = %s
                          AND kind = %s
                          AND name = %s
                          AND server_name = %s
                          AND tool_type = %s
                        """,
                        (
                            remaining["count"],
                            remaining["first_seen_at"],
                            remaining["last_seen_at"],
                            remaining["example_session_id"],
                            json.dumps(remaining["sample_keys"]),
                            remaining["sample_hash"],
                            *key,
                        ),
                    )
                    continue

                txn.execute(
                    """
                    DELETE FROM unmodeled_observations
                    WHERE source = %s
                      AND kind = %s
                      AND name = %s
                      AND server_name = %s
                      AND tool_type = %s
                    """,
                    key,
                )
        return len(deleted_rows)


def sample_keys(sample: dict[str, Any]) -> list[str]:
    return sorted(str(key)[:120] for key in sample.keys())[:_MAX_KEYS]


def stable_sample_hash(sample: dict[str, Any]) -> str:
    payload = {
        "version": _HASH_VERSION,
        "structure": _hash_structure(sample, depth=0),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _hash_structure(value: Any, *, depth: int) -> Any:
    if depth >= _MAX_DEPTH:
        return _shape(value)
    if isinstance(value, dict):
        shaped: dict[str, Any] = {}
        for key in sorted(value.keys(), key=str)[:_MAX_DICT_ITEMS]:
            key_text = str(key)
            shaped[key_text] = _hash_structure(value[key], depth=depth + 1)
        if len(value) > _MAX_DICT_ITEMS:
            shaped["..."] = f"{len(value) - _MAX_DICT_ITEMS} more keys"
        return shaped
    if isinstance(value, list):
        return {
            "type": "list",
            "length": len(value),
            "items": [_hash_structure(item, depth=depth + 1) for item in value[:_MAX_LIST_ITEMS]],
        }
    if isinstance(value, tuple):
        return _hash_structure(list(value), depth=depth)
    return _shape(value)


def _redact_value(value: Any, *, depth: int) -> Any:
    if depth >= _MAX_DEPTH:
        return _shape(value)
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key in sorted(value.keys(), key=str)[:_MAX_DICT_ITEMS]:
            key_text = str(key)
            if _is_sensitive_key(key_text):
                redacted[key_text] = "[redacted]"
            else:
                redacted[key_text] = _redact_value(value[key], depth=depth + 1)
        if len(value) > _MAX_DICT_ITEMS:
            redacted["..."] = f"{len(value) - _MAX_DICT_ITEMS} more keys"
        return redacted
    if isinstance(value, list):
        items = [_redact_value(item, depth=depth + 1) for item in value[:_MAX_LIST_ITEMS]]
        if len(value) > _MAX_LIST_ITEMS:
            items.append(f"... {len(value) - _MAX_LIST_ITEMS} more items")
        return items
    if isinstance(value, tuple):
        return _redact_value(list(value), depth=depth)
    if isinstance(value, str):
        if len(value) > _MAX_STRING_CHARS:
            return f"{value[:_MAX_STRING_CHARS]}...<truncated:{len(value)}>"
        return value
    if isinstance(value, int | float | bool) or value is None:
        return value
    return str(value)[:_MAX_STRING_CHARS]


def _shape(value: Any) -> str:
    if isinstance(value, dict):
        return f"<dict:{len(value)}>"
    if isinstance(value, list | tuple):
        return f"<list:{len(value)}>"
    return f"<{type(value).__name__}>"


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _row_from_db(row: Any) -> UnmodeledObservationRow:
    raw_keys = row["sample_keys"]
    if isinstance(raw_keys, str):
        try:
            decoded = json.loads(raw_keys)
        except json.JSONDecodeError:
            decoded = []
    elif isinstance(raw_keys, list):
        decoded = raw_keys
    else:
        decoded = []

    return UnmodeledObservationRow(
        source=row["source"],
        kind=row["kind"],
        name=row["name"],
        server_name=row["server_name"] or "",
        tool_type=row["tool_type"] or "",
        count=int(row["count"]),
        first_seen_at=require_stored_datetime(row["first_seen_at"], "first_seen_at"),
        last_seen_at=require_stored_datetime(row["last_seen_at"], "last_seen_at"),
        example_session_id=row["example_session_id"],
        sample_keys=[str(key) for key in decoded],
        sample_hash=row["sample_hash"],
    )
