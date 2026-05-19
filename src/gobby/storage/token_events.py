"""Per-event token usage storage and aggregation helpers."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from gobby.sessions.model_family import normalize_model
from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)

TimeSeriesGranularity = Literal["30m", "1h", "1d"]
VALID_GRANULARITIES: frozenset[TimeSeriesGranularity] = frozenset({"30m", "1h", "1d"})


@dataclass(frozen=True)
class TokenEvent:
    """Canonical token event payload."""

    session_id: str
    project_id: str | None
    message_id: str | None
    source: str
    origin: str
    model: str | None
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    event_at: str
    context_window: int | None = None
    metadata: dict[str, Any] | None = None
    model_family: str | None = None

    def normalized_model_family(self) -> str | None:
        return self.model_family or normalize_model(self.model)


def canonicalize_event_timestamp(value: datetime | str | None) -> str:
    """Return a stable UTC RFC3339 timestamp for token event storage."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            dt = datetime.now(UTC)
    else:
        dt = datetime.now(UTC)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _bucket_expression(granularity: TimeSeriesGranularity) -> str:
    if granularity == "30m":
        return (
            "CASE "
            "WHEN CAST(strftime('%M', event_at) AS INTEGER) < 30 "
            "THEN strftime('%Y-%m-%dT%H:00:00Z', event_at) "
            "ELSE strftime('%Y-%m-%dT%H:30:00Z', event_at) "
            "END"
        )
    if granularity == "1d":
        return "strftime('%Y-%m-%dT00:00:00Z', event_at)"
    return "strftime('%Y-%m-%dT%H:00:00Z', event_at)"


def _coerce_int(value: Any, *, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return default
        try:
            return int(stripped)
        except ValueError:
            return default
    return default


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        value = row[key]
    except (KeyError, TypeError, IndexError):
        return default

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return default


class TokenEventStore:
    """Token event ledger and aggregation facade."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def record(self, event: TokenEvent) -> bool:
        """Insert a token event row idempotently."""
        cursor = self.db.execute(
            """
            INSERT OR IGNORE INTO token_events (
                session_id,
                project_id,
                message_id,
                source,
                origin,
                model,
                model_family,
                input_tokens,
                output_tokens,
                cache_creation_tokens,
                cache_read_tokens,
                context_window,
                event_at,
                metadata
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.session_id,
                event.project_id,
                event.message_id,
                event.source,
                event.origin,
                event.model,
                event.normalized_model_family(),
                event.input_tokens,
                event.output_tokens,
                event.cache_creation_tokens,
                event.cache_read_tokens,
                event.context_window,
                canonicalize_event_timestamp(event.event_at),
                json.dumps(event.metadata) if event.metadata is not None else None,
            ),
        )
        rowcount = getattr(cursor, "rowcount", None)
        if isinstance(rowcount, int):
            return rowcount > 0
        lastrowid = getattr(cursor, "lastrowid", None)
        if isinstance(lastrowid, int):
            return lastrowid > 0
        return True

    def delete_session_events(self, session_id: str, *, origin: str | None = None) -> int:
        """Delete token events for a session."""
        if origin:
            cursor = self.db.execute(
                "DELETE FROM token_events WHERE session_id = ? AND origin = ?",
                (session_id, origin),
            )
        else:
            cursor = self.db.execute("DELETE FROM token_events WHERE session_id = ?", (session_id,))
        rowcount = getattr(cursor, "rowcount", None)
        return rowcount if isinstance(rowcount, int) else 0

    def count_session_events(self, session_id: str) -> int:
        row = self.db.fetchone(
            "SELECT COUNT(*) AS count FROM token_events WHERE session_id = ?",
            (session_id,),
        )
        return _coerce_int(_row_value(row, "count"), default=0)

    def get_session_totals(self, session_id: str) -> dict[str, int]:
        row = self.db.fetchone(
            """
            SELECT
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens
            FROM token_events
            WHERE session_id = ?
            """,
            (session_id,),
        )
        return {
            "input_tokens": _coerce_int(_row_value(row, "input_tokens"), default=0),
            "output_tokens": _coerce_int(_row_value(row, "output_tokens"), default=0),
            "cache_creation_tokens": _coerce_int(
                _row_value(row, "cache_creation_tokens"),
                default=0,
            ),
            "cache_read_tokens": _coerce_int(_row_value(row, "cache_read_tokens"), default=0),
        }

    def list_session_events(
        self,
        session_id: str,
        *,
        limit: int = 500,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            raise ValueError("limit must be a positive integer")
        params: list[Any] = [session_id]
        since_sql = ""
        if since:
            since_sql = "AND event_at > ?"
            params.append(canonicalize_event_timestamp(since))
        params.append(limit)

        rows = self.db.fetchall(
            f"""
            SELECT
                id,
                session_id,
                project_id,
                message_id,
                source,
                origin,
                model,
                model_family,
                input_tokens,
                output_tokens,
                cache_creation_tokens,
                cache_read_tokens,
                context_window,
                event_at,
                created_at,
                metadata
            FROM token_events
            WHERE session_id = ?
              {since_sql}
            ORDER BY event_at DESC, id DESC
            LIMIT ?
            """,
            tuple(params),
        )
        return [self._row_to_event_dict(row) for row in rows]

    def get_breakdown(
        self,
        *,
        hours: int | None = None,
        days: int | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        where_sql, params = self._window_where(hours=hours, days=days, project_id=project_id)

        totals_row = self.db.fetchone(
            f"""
            SELECT
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                COUNT(DISTINCT session_id) AS session_count
            FROM token_events
            WHERE 1=1 {where_sql}
            """,
            tuple(params),
        )

        totals = {
            "input_tokens": _coerce_int(_row_value(totals_row, "input_tokens"), default=0),
            "output_tokens": _coerce_int(_row_value(totals_row, "output_tokens"), default=0),
            "cache_read_tokens": _coerce_int(
                _row_value(totals_row, "cache_read_tokens"),
                default=0,
            ),
            "cache_creation_tokens": _coerce_int(
                _row_value(totals_row, "cache_creation_tokens"),
                default=0,
            ),
            "session_count": _coerce_int(_row_value(totals_row, "session_count"), default=0),
        }

        by_source: dict[str, dict[str, int]] = {}
        source_rows = self.db.fetchall(
            f"""
            SELECT
                source,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                COUNT(DISTINCT session_id) AS session_count
            FROM token_events
            WHERE 1=1 {where_sql}
            GROUP BY source
            """,
            tuple(params),
        )
        for row in source_rows:
            key = str(_row_value(row, "source", "unknown") or "unknown")
            by_source[key] = {
                "input_tokens": _coerce_int(_row_value(row, "input_tokens"), default=0),
                "output_tokens": _coerce_int(_row_value(row, "output_tokens"), default=0),
                "cache_read_tokens": _coerce_int(
                    _row_value(row, "cache_read_tokens"),
                    default=0,
                ),
                "cache_creation_tokens": _coerce_int(
                    _row_value(row, "cache_creation_tokens"),
                    default=0,
                ),
                "session_count": _coerce_int(_row_value(row, "session_count"), default=0),
            }

        by_model: dict[str, dict[str, int]] = {}
        model_rows = self.db.fetchall(
            f"""
            SELECT
                COALESCE(model_family, 'unknown') AS model_family,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                COUNT(DISTINCT session_id) AS session_count
            FROM token_events
            WHERE 1=1 {where_sql}
            GROUP BY model_family
            ORDER BY input_tokens + output_tokens DESC
            """,
            tuple(params),
        )
        for row in model_rows:
            model_key = _row_value(row, "model_family", _row_value(row, "model", "unknown"))
            key = str(model_key or "unknown")
            by_model[key] = {
                "input_tokens": _coerce_int(_row_value(row, "input_tokens"), default=0),
                "output_tokens": _coerce_int(_row_value(row, "output_tokens"), default=0),
                "cache_read_tokens": _coerce_int(
                    _row_value(row, "cache_read_tokens"),
                    default=0,
                ),
                "cache_creation_tokens": _coerce_int(
                    _row_value(row, "cache_creation_tokens"),
                    default=0,
                ),
                "session_count": _coerce_int(_row_value(row, "session_count"), default=0),
            }

        return {
            "totals": totals,
            "by_source": by_source,
            "by_model": by_model,
        }

    def get_timeseries(
        self,
        *,
        hours: int = 24,
        project_id: str | None = None,
        granularity: TimeSeriesGranularity = "1h",
    ) -> list[dict[str, Any]]:
        if granularity not in VALID_GRANULARITIES:
            raise ValueError(f"Unsupported granularity: {granularity}")

        where_sql, params = self._window_where(hours=hours, project_id=project_id)
        bucket_expr = _bucket_expression(granularity)
        rows = self.db.fetchall(
            f"""
            SELECT
                {bucket_expr} AS bucket,
                COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens_spent
            FROM token_events
            WHERE 1=1 {where_sql}
            GROUP BY bucket
            ORDER BY bucket ASC
            """,
            tuple(params),
        )
        return [
            {
                "timestamp": str(_row_value(row, "bucket", "")),
                "tokens_spent": _coerce_int(_row_value(row, "tokens_spent"), default=0),
            }
            for row in rows
            if _row_value(row, "bucket") is not None
        ]

    def _window_where(
        self,
        *,
        hours: int | None = None,
        days: int | None = None,
        project_id: str | None = None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []

        if hours is not None and hours > 0:
            clauses.append("AND event_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)")
            params.append(f"-{hours} hours")
        elif days is not None and days > 0:
            clauses.append("AND event_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)")
            params.append(f"-{days} days")

        if project_id:
            clauses.append("AND project_id = ?")
            params.append(project_id)

        return " ".join(clauses), params

    @staticmethod
    def _row_to_event_dict(row: Any) -> dict[str, Any]:
        metadata = _row_value(row, "metadata")
        parsed_metadata: dict[str, Any] | None = None
        if isinstance(metadata, str) and metadata:
            try:
                loaded = json.loads(metadata)
                if isinstance(loaded, dict):
                    parsed_metadata = loaded
            except json.JSONDecodeError:
                logger.debug(
                    "Failed to parse token event metadata",
                    extra={
                        "id": _coerce_int(_row_value(row, "id"), default=0),
                        "session_id": _row_value(row, "session_id"),
                        "project_id": _row_value(row, "project_id"),
                        "message_id": _row_value(row, "message_id"),
                        "model": _row_value(row, "model"),
                        "raw_metadata_present": bool(metadata),
                        "raw_metadata_size": len(metadata),
                    },
                    exc_info=True,
                )

        return {
            "id": _coerce_int(_row_value(row, "id"), default=0),
            "session_id": _row_value(row, "session_id"),
            "project_id": _row_value(row, "project_id"),
            "message_id": _row_value(row, "message_id"),
            "source": _row_value(row, "source"),
            "origin": _row_value(row, "origin"),
            "model": _row_value(row, "model"),
            "model_family": _row_value(row, "model_family"),
            "input_tokens": _coerce_int(_row_value(row, "input_tokens"), default=0),
            "output_tokens": _coerce_int(_row_value(row, "output_tokens"), default=0),
            "cache_creation_tokens": _coerce_int(
                _row_value(row, "cache_creation_tokens"),
                default=0,
            ),
            "cache_read_tokens": _coerce_int(_row_value(row, "cache_read_tokens"), default=0),
            "context_window": _row_value(row, "context_window"),
            "event_at": _row_value(row, "event_at"),
            "created_at": _row_value(row, "created_at"),
            "metadata": parsed_metadata,
        }


def build_session_usage_payload(
    *,
    session_id: str,
    project_id: str | None,
    model: str | None,
    context_window: int | None,
    totals: dict[str, int],
    updated_at: datetime | str | None = None,
) -> dict[str, Any]:
    """Build a websocket payload for session aggregate usage refresh."""
    return {
        "type": "session_usage_updated",
        "session_id": session_id,
        "project_id": project_id,
        "model": model,
        "context_window": context_window,
        "usage_input_tokens": totals.get("input_tokens", 0),
        "usage_output_tokens": totals.get("output_tokens", 0),
        "usage_cache_creation_tokens": totals.get("cache_creation_tokens", 0),
        "usage_cache_read_tokens": totals.get("cache_read_tokens", 0),
        "updated_at": canonicalize_event_timestamp(updated_at),
    }


def build_token_event_payload(
    event: dict[str, Any],
    *,
    session_totals: dict[str, int],
) -> dict[str, Any]:
    """Build a websocket payload for an inserted token event."""
    return {
        "type": "token_event",
        "session_id": event["session_id"],
        "project_id": event.get("project_id"),
        "message_id": event.get("message_id"),
        "source": event.get("source"),
        "origin": event.get("origin"),
        "event_at": event["event_at"],
        "model": event.get("model"),
        "model_family": event.get("model_family"),
        "input_tokens": event["input_tokens"],
        "output_tokens": event["output_tokens"],
        "cache_creation_tokens": event["cache_creation_tokens"],
        "cache_read_tokens": event["cache_read_tokens"],
        "context_window": event.get("context_window"),
        "session_totals": {
            "input_tokens": session_totals.get("input_tokens", 0),
            "output_tokens": session_totals.get("output_tokens", 0),
            "cache_creation_tokens": session_totals.get("cache_creation_tokens", 0),
            "cache_read_tokens": session_totals.get("cache_read_tokens", 0),
        },
    }


def merge_event_totals(events: Sequence[dict[str, Any]]) -> dict[str, int]:
    """Aggregate token totals from a list of event-like mappings."""
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
    }
    for event in events:
        totals["input_tokens"] += _coerce_int(event.get("input_tokens"))
        totals["output_tokens"] += _coerce_int(event.get("output_tokens"))
        totals["cache_creation_tokens"] += _coerce_int(event.get("cache_creation_tokens"))
        totals["cache_read_tokens"] += _coerce_int(event.get("cache_read_tokens"))
    return totals
