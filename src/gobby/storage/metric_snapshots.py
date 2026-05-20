"""Metric snapshot storage for time-series OTel data.

Stores periodic snapshots of get_all_metrics() output in SQLite
for dashboard charting. 24h retention, ~1440 rows max at 60s interval.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from gobby.storage.sql_dialect import newer_than_now_expr, older_than_now_expr

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)


class MetricSnapshotStorage:
    """Storage manager for periodic metric snapshots."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def save_snapshot(self, metrics: dict[str, Any]) -> None:
        """Save a metrics snapshot as JSON."""
        try:
            self.db.execute(
                "INSERT INTO metric_snapshots (metrics_json) VALUES (?)",
                (json.dumps(metrics),),
            )
        except Exception as e:
            logger.error(f"Failed to save metric snapshot: {e}")

    def get_snapshots(self, hours: int = 1, limit: int = 120) -> list[dict[str, Any]]:
        """Get recent snapshots within the time window.

        Returns list of {timestamp, metrics} dicts ordered by time ASC.
        """
        recent_sql = newer_than_now_expr(self.db, "timestamp", "?", "hour")
        rows = self.db.fetchall(
            f"SELECT timestamp, metrics_json FROM metric_snapshots "
            f"WHERE {recent_sql} "
            f"ORDER BY timestamp ASC LIMIT ?",  # nosec B608 - cutoff expr is dialect-owned.
            (hours, limit),
        )
        results = []
        for row in rows:
            try:
                metrics = json.loads(row["metrics_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            results.append(
                {
                    "timestamp": row["timestamp"],
                    "metrics": metrics,
                }
            )
        return results

    def delete_old_snapshots(self, retention_hours: int = 24) -> int:
        """Purge snapshots older than retention period."""
        expired_sql = older_than_now_expr(self.db, "timestamp", "?", "hour")
        cursor = self.db.execute(
            f"DELETE FROM metric_snapshots WHERE {expired_sql}",  # nosec B608
            (retention_hours,),
        )
        return cursor.rowcount

    def get_snapshot_count(self) -> int:
        """Return total number of snapshots."""
        row = self.db.fetchone("SELECT COUNT(*) as count FROM metric_snapshots")
        return row["count"] if row else 0
