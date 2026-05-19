"""Metric snapshot storage for time-series OTel data.

Stores periodic snapshots of get_all_metrics() output in SQLite
for dashboard charting. 24h retention, ~1440 rows max at 60s interval.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

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
        rows = self.db.fetchall(
            "SELECT timestamp, metrics_json FROM metric_snapshots "
            "WHERE timestamp >= datetime('now', ?) "
            "ORDER BY timestamp ASC LIMIT ?",
            (f"-{hours} hours", limit),
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
        cursor = self.db.execute(
            "DELETE FROM metric_snapshots WHERE timestamp < datetime('now', ?)",
            (f"-{retention_hours} hours",),
        )
        return cursor.rowcount

    def get_snapshot_count(self) -> int:
        """Return total number of snapshots."""
        row = self.db.fetchone("SELECT COUNT(*) as count FROM metric_snapshots")
        return row["count"] if row else 0
