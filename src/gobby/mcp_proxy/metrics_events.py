"""Event-based metrics storage for tool calls, rule evaluations, and skill usage."""

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)

# Default retention period before archiving
DEFAULT_RETENTION_DAYS = 30

# Time range presets for dashboard queries
RANGE_DELTAS: dict[str, timedelta | None] = {
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "12h": timedelta(hours=12),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "all": None,
}


class MetricsEventStore:
    """
    Event log storage for metrics.

    Records raw events (tool calls, rule evaluations, skill usage) with full
    dimensions (session_id, timestamp, event_type). All dashboard queries hit
    this table directly with timestamp filters. Events older than the retention
    period are rolled into metrics_events_archive as aggregate totals.
    """

    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def record_event(
        self,
        event_type: str,
        name: str,
        project_id: str | None = None,
        session_id: str | None = None,
        server_name: str | None = None,
        success: bool = True,
        latency_ms: float | None = None,
        result: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record a raw metrics event."""
        self.db.execute(
            """
            INSERT INTO metrics_events (
                event_type, project_id, session_id, server_name,
                name, success, latency_ms, result, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_type,
                project_id,
                session_id,
                server_name,
                name,
                1 if success else 0,
                latency_ms,
                result,
                json.dumps(metadata) if metadata else None,
            ),
        )

    def get_session_tool_breakdown(self, session_id: str) -> list[dict[str, Any]]:
        """Get per-tool call counts and latency for a session."""
        rows = self.db.fetchall(
            """
            SELECT
                server_name,
                name AS tool_name,
                COUNT(*) AS call_count,
                SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS success_count,
                SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failure_count,
                ROUND(AVG(latency_ms), 2) AS avg_latency_ms,
                ROUND(SUM(latency_ms), 2) AS total_latency_ms
            FROM metrics_events
            WHERE session_id = ? AND event_type = 'tool_call'
            GROUP BY server_name, name
            ORDER BY call_count DESC
            """,
            (session_id,),
        )
        return [dict(row) for row in rows]

    def get_rule_stats(
        self,
        since: datetime | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get aggregate rule evaluation stats."""
        conditions = ["event_type = 'rule_eval'"]
        params: list[Any] = []

        if since:
            conditions.append("created_at >= ?")
            params.append(since.isoformat())
        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)

        where = " AND ".join(conditions)
        rows = self.db.fetchall(
            f"""
            SELECT
                name AS rule_name,
                COUNT(*) AS eval_count,
                SUM(CASE WHEN result = 'block' THEN 1 ELSE 0 END) AS block_count,
                SUM(CASE WHEN result = 'allow' THEN 1 ELSE 0 END) AS allow_count,
                ROUND(AVG(latency_ms), 2) AS avg_latency_ms
            FROM metrics_events
            WHERE {where}
            GROUP BY name
            ORDER BY eval_count DESC
            """,
            tuple(params),
        )
        return [dict(row) for row in rows]

    def get_skill_stats(
        self,
        since: datetime | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get aggregate skill usage stats."""
        conditions = ["event_type IN ('skill_search', 'skill_invoke')"]
        params: list[Any] = []

        if since:
            conditions.append("created_at >= ?")
            params.append(since.isoformat())
        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)

        where = " AND ".join(conditions)
        rows = self.db.fetchall(
            f"""
            SELECT
                name AS skill_name,
                event_type,
                COUNT(*) AS count,
                ROUND(AVG(latency_ms), 2) AS avg_latency_ms
            FROM metrics_events
            WHERE {where}
            GROUP BY name, event_type
            ORDER BY count DESC
            """,
            tuple(params),
        )
        return [dict(row) for row in rows]

    def query_events(
        self,
        event_type: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        session_id: str | None = None,
        name: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Flexible filtered query on raw events."""
        conditions: list[str] = []
        params: list[Any] = []

        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        if since:
            conditions.append("created_at >= ?")
            params.append(since.isoformat())
        if until:
            conditions.append("created_at < ?")
            params.append(until.isoformat())
        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if name:
            conditions.append("name = ?")
            params.append(name)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        rows = self.db.fetchall(
            f"""
            SELECT * FROM metrics_events
            {where}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            tuple(params),
        )
        return [dict(row) for row in rows]

    def get_timeseries(
        self,
        event_type: str,
        range_key: str = "24h",
        name: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Get time-bucketed metrics for dashboard charts.

        For ranges up to 30d, queries metrics_events directly.
        For "all", adds archive totals for lifetime counts.

        Returns dict with 'buckets' (time-series data) and optionally
        'archive_totals' for all-time queries.
        """
        delta = RANGE_DELTAS.get(range_key)
        if delta is None and range_key != "all":
            delta = timedelta(hours=24)  # fallback

        # Determine bucket size based on range
        if range_key in ("1h", "6h"):
            bucket_fmt = "%Y-%m-%dT%H:%M:00"  # per-minute
            bucket_label = "minute"
        elif range_key in ("12h", "24h"):
            bucket_fmt = "%Y-%m-%dT%H:00:00"  # per-hour
            bucket_label = "hour"
        else:
            bucket_fmt = "%Y-%m-%d"  # per-day
            bucket_label = "day"

        conditions = ["event_type = ?"]
        params: list[Any] = [event_type]

        if delta:
            since = datetime.now(UTC) - delta
            conditions.append("created_at >= ?")
            params.append(since.isoformat())
        if name:
            conditions.append("name = ?")
            params.append(name)
        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)

        where = " AND ".join(conditions)

        rows = self.db.fetchall(
            f"""
            SELECT
                strftime('{bucket_fmt}', created_at) AS bucket,
                COUNT(*) AS call_count,
                SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS success_count,
                SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failure_count,
                ROUND(AVG(latency_ms), 2) AS avg_latency_ms,
                SUM(CASE WHEN result = 'block' THEN 1 ELSE 0 END) AS block_count,
                SUM(CASE WHEN result = 'allow' THEN 1 ELSE 0 END) AS allow_count
            FROM metrics_events
            WHERE {where}
            GROUP BY bucket
            ORDER BY bucket ASC
            """,
            tuple(params),
        )

        result: dict[str, Any] = {
            "range": range_key,
            "bucket_size": bucket_label,
            "event_type": event_type,
            "buckets": [dict(row) for row in rows],
        }

        # For "all" range, include archive totals
        if range_key == "all":
            result["archive_totals"] = self.get_archive_totals(event_type=event_type, name=name)

        return result

    def get_archive_totals(
        self,
        event_type: str | None = None,
        name: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query lifetime aggregate totals from the archive."""
        conditions: list[str] = []
        params: list[Any] = []

        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        if name:
            conditions.append("name = ?")
            params.append(name)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        rows = self.db.fetchall(
            f"""
            SELECT
                event_type, project_id, server_name, name,
                call_count, success_count, failure_count,
                total_latency_ms, block_count, allow_count
            FROM metrics_events_archive
            {where}
            ORDER BY call_count DESC
            """,
            tuple(params),
        )
        return [dict(row) for row in rows]

    def archive_old_events(self, retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
        """
        Roll events older than retention period into archive, then delete originals.

        Returns the number of events archived.
        """
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()

        # UPSERT aggregated counts into archive.
        # Use COALESCE to keep nullable dimensions deterministic in conflict targets.
        self.db.execute(
            """
            INSERT INTO metrics_events_archive (
                event_type, project_id, server_name, name,
                call_count, success_count, failure_count,
                total_latency_ms, block_count, allow_count
            )
            SELECT
                event_type,
                COALESCE(project_id, ''),
                COALESCE(server_name, ''),
                name,
                COUNT(*),
                SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END),
                SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END),
                COALESCE(SUM(latency_ms), 0),
                SUM(CASE WHEN result = 'block' THEN 1 ELSE 0 END),
                SUM(CASE WHEN result = 'allow' THEN 1 ELSE 0 END)
            FROM metrics_events
            WHERE created_at < ?
            GROUP BY event_type, COALESCE(project_id, ''), COALESCE(server_name, ''), name
            ON CONFLICT(event_type, project_id, server_name, name) DO UPDATE SET
                call_count = call_count + excluded.call_count,
                success_count = success_count + excluded.success_count,
                failure_count = failure_count + excluded.failure_count,
                total_latency_ms = total_latency_ms + excluded.total_latency_ms,
                block_count = block_count + excluded.block_count,
                allow_count = allow_count + excluded.allow_count
            """,
            (cutoff,),
        )

        # Delete archived events
        cursor = self.db.execute(
            "DELETE FROM metrics_events WHERE created_at < ?",
            (cutoff,),
        )
        deleted = cursor.rowcount if hasattr(cursor, "rowcount") else 0
        if deleted:
            logger.info(f"Archived {deleted} metrics events older than {retention_days} days")
        return deleted
