from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

from gobby.storage.sql_dialect import json_text_expr, older_than_now_expr

logger = logging.getLogger(__name__)


class SpanStorage:
    """Storage manager for OpenTelemetry spans in SQLite."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def save_span(self, span_data: dict[str, Any]) -> None:
        """Insert a single span record."""
        self.save_spans([span_data])

    def save_spans(self, spans: list[dict[str, Any]]) -> None:
        """Batch insert multiple span records."""
        if not spans:
            return

        query = """
        INSERT INTO spans (
            span_id, trace_id, parent_span_id, name, kind,
            start_time_ns, end_time_ns, status, status_message,
            attributes_json, events_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """

        rows = []
        for s in spans:
            rows.append(
                (
                    s["span_id"],
                    s["trace_id"],
                    s.get("parent_span_id"),
                    s["name"],
                    s.get("kind"),
                    s["start_time_ns"],
                    s.get("end_time_ns"),
                    s.get("status"),
                    s.get("status_message"),
                    json.dumps(s.get("attributes", {})),
                    json.dumps(s.get("events", [])),
                )
            )

        try:
            self.db.executemany(query, rows)
        except Exception as e:
            logger.error(f"Failed to save spans: {e}")
            raise

    def get_trace(self, trace_id: str) -> list[dict[str, Any]]:
        """Retrieve all spans for a trace, ordered by start time."""
        query = "SELECT * FROM spans WHERE trace_id = ? ORDER BY start_time_ns ASC"
        rows = self.db.fetchall(query, (trace_id,))
        return [self._row_to_dict(row) for row in rows]

    def get_recent_traces(
        self,
        limit: int = 50,
        offset: int = 0,
        project_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Retrieve recent traces, represented by their root (earliest) span."""
        conditions: list[str] = []
        params: list[Any] = []

        if project_id:
            project_id_sql = json_text_expr(self.db, "attributes_json", "project_id")
            conditions.append(f"({project_id_sql} = ? OR {project_id_sql} IS NULL)")
            params.append(project_id)
        if status:
            conditions.append("status = ?")
            params.append(status)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        # Using a single query with ROW_NUMBER() to get the root span per trace_id
        # and joining it with the max start_time_ns for ordering.
        query = f"""
        WITH TraceLastActivity AS (
            SELECT trace_id, MAX(start_time_ns) as last_activity
            FROM spans
            {where}
            GROUP BY trace_id
            ORDER BY last_activity DESC
            LIMIT ? OFFSET ?
        ),
        RootSpans AS (
            SELECT *,
                   ROW_NUMBER() OVER(PARTITION BY trace_id ORDER BY start_time_ns ASC) as rn
            FROM spans
            WHERE trace_id IN (SELECT trace_id FROM TraceLastActivity)
        )
        SELECT r.*
        FROM RootSpans r
        JOIN TraceLastActivity tla ON r.trace_id = tla.trace_id
        WHERE r.rn = 1
        ORDER BY tla.last_activity DESC
        """
        params.extend([limit, offset])
        rows = self.db.fetchall(query, tuple(params))
        return [self._row_to_dict(row) for row in rows]

    def get_traces_by_session(
        self, session_id: str, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Retrieve traces that have a matching session_id in their attributes."""
        session_id_sql = json_text_expr(self.db, "attributes_json", "session_id")
        query = f"""
        WITH SessionTraces AS (
            SELECT trace_id, MAX(start_time_ns) as last_activity
            FROM spans
            WHERE {session_id_sql} = ?
            GROUP BY trace_id
            ORDER BY last_activity DESC
            LIMIT ? OFFSET ?
        ),
        RootSpans AS (
            SELECT *,
                   ROW_NUMBER() OVER(PARTITION BY trace_id ORDER BY start_time_ns ASC) as rn
            FROM spans
            WHERE trace_id IN (SELECT trace_id FROM SessionTraces)
        )
        SELECT r.*
        FROM RootSpans r
        JOIN SessionTraces st ON r.trace_id = st.trace_id
        WHERE r.rn = 1
        ORDER BY st.last_activity DESC
        """  # nosec B608 - JSON expression is generated from a static key.
        rows = self.db.fetchall(query, (session_id, limit, offset))
        return [self._row_to_dict(row) for row in rows]

    def get_trace_count_by_session(self, session_id: str) -> int:
        """Get the total number of distinct traces for a session."""
        session_id_sql = json_text_expr(self.db, "attributes_json", "session_id")
        query = f"""
        SELECT COUNT(DISTINCT trace_id) as count
        FROM spans
        WHERE {session_id_sql} = ?
        """  # nosec B608 - JSON expression is generated from a static key.
        row = self.db.fetchone(query, (session_id,))
        return row["count"] if row else 0

    def delete_old_spans(self, retention_days: int = 7) -> int:
        """Delete spans older than the specified retention period."""
        cutoff_sql = older_than_now_expr(self.db, "created_at", "?", "day")
        query = f"DELETE FROM spans WHERE {cutoff_sql}"  # nosec B608
        cursor = self.db.execute(query, (retention_days,))
        return cursor.rowcount

    def get_span_count(self) -> int:
        """Return the total number of spans in storage."""
        row = self.db.fetchone("SELECT COUNT(*) as count FROM spans")
        return row["count"] if row else 0

    def _row_to_dict(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """Convert a database row to a dictionary with parsed JSON fields."""
        d = dict(row)
        if "attributes_json" in d:
            d["attributes"] = json.loads(d.pop("attributes_json") or "{}")
        if "events_json" in d:
            d["events"] = json.loads(d.pop("events_json") or "[]")
        return d
