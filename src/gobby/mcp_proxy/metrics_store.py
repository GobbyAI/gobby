"""Hub database storage for tool call metrics."""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import normalize_datetime_model, utc_now

logger = logging.getLogger(__name__)

# Default retention period for metrics
DEFAULT_RETENTION_DAYS = 7


@normalize_datetime_model(
    required=(
        "created_at",
        "updated_at",
    ),
    optional=("last_called_at",),
)
@dataclass
class ToolMetrics:
    """Tool metrics data model."""

    id: str
    project_id: str
    server_name: str
    tool_name: str
    call_count: int
    success_count: int
    failure_count: int
    total_latency_ms: float
    avg_latency_ms: float | None
    last_called_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> "ToolMetrics":
        """Create ToolMetrics from database row."""
        return cls(
            id=row["id"],
            project_id=row["project_id"],
            server_name=row["server_name"],
            tool_name=row["tool_name"],
            call_count=row["call_count"],
            success_count=row["success_count"],
            failure_count=row["failure_count"],
            total_latency_ms=row["total_latency_ms"],
            avg_latency_ms=row["avg_latency_ms"],
            last_called_at=row["last_called_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "project_id": self.project_id,
            "server_name": self.server_name,
            "tool_name": self.tool_name,
            "call_count": self.call_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "total_latency_ms": self.total_latency_ms,
            "avg_latency_ms": self.avg_latency_ms,
            "success_rate": (self.success_count / self.call_count if self.call_count > 0 else None),
            "last_called_at": self.last_called_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class ToolMetricsStore:
    """
    Persistence layer for tool call metrics using the hub database.

    Handles all direct database interactions for recording and querying tool metrics.
    """

    def __init__(self, db: HubDatabase):
        """
        Initialize the metrics store.

        Args:
            db: Hub database adapter for persistence
        """
        self.db = db

    def record_call(
        self,
        server_name: str,
        tool_name: str,
        project_id: str,
        latency_ms: float,
        success: bool = True,
    ) -> None:
        """
        Record a tool call with its metrics in the hub database.

        Args:
            server_name: Name of the MCP server
            tool_name: Name of the tool
            project_id: Project ID the call was made from
            latency_ms: Execution time in milliseconds
            success: Whether the call succeeded
        """
        now = utc_now()
        metrics_id = str(uuid.uuid4())
        success_inc = 1 if success else 0
        failure_inc = 0 if success else 1

        self.db.execute(
            """
            INSERT INTO tool_metrics (
                id, project_id, server_name, tool_name,
                call_count, success_count, failure_count,
                total_latency_ms, avg_latency_ms,
                last_called_at
            ) VALUES (%s, %s, %s, %s, 1, %s, %s, %s, %s, %s)
            ON CONFLICT(project_id, server_name, tool_name) DO UPDATE SET
                call_count = tool_metrics.call_count + 1,
                success_count = tool_metrics.success_count + %s,
                failure_count = tool_metrics.failure_count + %s,
                total_latency_ms = tool_metrics.total_latency_ms + %s,
                avg_latency_ms = (tool_metrics.total_latency_ms + %s) /
                                 (tool_metrics.call_count + 1),
                last_called_at = %s,
                updated_at = %s
            """,
            (
                # INSERT values
                metrics_id,
                project_id,
                server_name,
                tool_name,
                success_inc,
                failure_inc,
                latency_ms,
                latency_ms,
                now,
                # ON CONFLICT UPDATE values
                success_inc,
                failure_inc,
                latency_ms,
                latency_ms,
                now,
                now,
            ),
        )

    def get_metrics(
        self,
        project_id: str | None = None,
        server_name: str | None = None,
        tool_name: str | None = None,
    ) -> list[Any]:
        """
        Get raw metrics rows from the hub database, optionally filtered.

        Args:
            project_id: Filter by project ID
            server_name: Filter by server name
            tool_name: Filter by tool name

        Returns:
            List of database rows as dictionaries
        """
        conditions = []
        params: list[Any] = []

        if project_id:
            conditions.append("project_id = %s")
            params.append(project_id)
        if server_name:
            conditions.append("server_name = %s")
            params.append(server_name)
        if tool_name:
            conditions.append("tool_name = %s")
            params.append(tool_name)

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        return self.db.fetchall(
            f"SELECT * FROM tool_metrics WHERE {where_clause} ORDER BY call_count DESC",  # nosec # fixed predicates; bound values.
            tuple(params),
        )

    def get_top_tools(
        self,
        project_id: str | None = None,
        limit: int = 10,
        order_by: str = "call_count",
    ) -> list[Any]:
        """
        Get top tools from the hub database.
        """
        valid_order_columns = {"call_count", "success_count", "avg_latency_ms"}
        if order_by not in valid_order_columns:
            order_by = "call_count"

        if project_id:
            return self.db.fetchall(
                f"SELECT * FROM tool_metrics WHERE project_id = %s ORDER BY {order_by} DESC LIMIT %s",  # nosec # order_by is allowlisted.
                (project_id, limit),
            )
        else:
            return self.db.fetchall(
                f"SELECT * FROM tool_metrics ORDER BY {order_by} DESC LIMIT %s",  # nosec # order_by is allowlisted.
                (limit,),
            )

    def get_tool_success_rate(
        self,
        server_name: str,
        tool_name: str,
        project_id: str,
    ) -> float | None:
        """
        Get success rate for a specific tool from the hub database.
        """
        row = self.db.fetchone(
            """
            SELECT success_count, call_count
            FROM tool_metrics
            WHERE project_id = %s AND server_name = %s AND tool_name = %s
            """,
            (project_id, server_name, tool_name),
        )

        if row and row["call_count"] > 0:
            return float(row["success_count"]) / float(row["call_count"])
        return None

    def get_failing_tools(
        self,
        project_id: str | None = None,
        threshold: float = 0.5,
        limit: int = 10,
    ) -> list[Any]:
        """
        Get tools with failure rate above a threshold from the hub database.
        """
        if project_id:
            return self.db.fetchall(
                """
                SELECT *,
                    CAST(failure_count AS REAL) / CAST(call_count AS REAL) as failure_rate
                FROM tool_metrics
                WHERE project_id = %s
                    AND call_count > 0
                    AND CAST(failure_count AS REAL) / CAST(call_count AS REAL) >= %s
                ORDER BY failure_rate DESC
                LIMIT %s
                """,
                (project_id, threshold, limit),
            )
        else:
            return self.db.fetchall(
                """
                SELECT *,
                    CAST(failure_count AS REAL) / CAST(call_count AS REAL) as failure_rate
                FROM tool_metrics
                WHERE call_count > 0
                    AND CAST(failure_count AS REAL) / CAST(call_count AS REAL) >= %s
                ORDER BY failure_rate DESC
                LIMIT %s
                """,
                (threshold, limit),
            )

    def reset_metrics(
        self,
        project_id: str | None = None,
        server_name: str | None = None,
        tool_name: str | None = None,
    ) -> int:
        """
        Reset/delete metrics in the hub database.
        """
        filters: list[tuple[str, str]] = []
        if project_id:
            filters.append(("project_id", project_id))
        if server_name:
            filters.append(("server_name", server_name))
        if tool_name:
            filters.append(("tool_name", tool_name))

        if not filters:
            raise ValueError("reset_metrics requires at least one filter")

        params = tuple(value for _, value in filters)
        deleted_tool_metrics = 0
        with self.db.transaction() as txn:
            for table, tool_column in (
                ("tool_metrics", "tool_name"),
                ("tool_metrics_daily", "tool_name"),
                ("metrics_events", "name"),
            ):
                conditions = ["event_type = %s"] if table == "metrics_events" else []
                conditions.extend(
                    f"{tool_column if column == 'tool_name' else column} = %s"
                    for column, _ in filters
                )
                where_clause = " AND ".join(conditions)
                table_params = ("tool_call", *params) if table == "metrics_events" else params
                cursor = txn.execute(
                    f"DELETE FROM {table} WHERE {where_clause}",  # nosec # table and predicates come from fixed tuples.
                    table_params,
                )
                if table == "tool_metrics":
                    deleted_tool_metrics = cursor.rowcount

        return deleted_tool_metrics

    def aggregate_to_daily(self, retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
        """
        Aggregate old metrics into daily summaries.
        """
        cutoff = utc_now() - timedelta(days=retention_days)

        rows = self.db.fetchall(
            """
            SELECT
                project_id,
                server_name,
                tool_name,
                date(last_called_at) as metric_date,
                SUM(call_count) as total_calls,
                SUM(success_count) as total_success,
                SUM(failure_count) as total_failure,
                SUM(total_latency_ms) as total_latency
            FROM tool_metrics
            WHERE last_called_at < %s
            GROUP BY project_id, server_name, tool_name, date(last_called_at)
            """,
            (cutoff,),
        )

        if not rows:
            return 0

        aggregated = 0

        for row in rows:
            total_calls = row["total_calls"]
            avg_latency = row["total_latency"] / total_calls if total_calls > 0 else None

            self.db.execute(
                """
                INSERT INTO tool_metrics_daily (
                    project_id, server_name, tool_name, date,
                    call_count, success_count, failure_count,
                    total_latency_ms, avg_latency_ms
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(project_id, server_name, tool_name, date) DO UPDATE SET
                    call_count = tool_metrics_daily.call_count + excluded.call_count,
                    success_count = tool_metrics_daily.success_count + excluded.success_count,
                    failure_count = tool_metrics_daily.failure_count + excluded.failure_count,
                    total_latency_ms = tool_metrics_daily.total_latency_ms +
                                       excluded.total_latency_ms,
                    avg_latency_ms = (
                        tool_metrics_daily.total_latency_ms + excluded.total_latency_ms
                    ) / (tool_metrics_daily.call_count + excluded.call_count)
                """,
                (
                    row["project_id"],
                    row["server_name"],
                    row["tool_name"],
                    row["metric_date"],
                    total_calls,
                    row["total_success"],
                    row["total_failure"],
                    row["total_latency"],
                    avg_latency,
                ),
            )
            aggregated += 1

        return aggregated

    def cleanup_old_metrics(self, cutoff: datetime) -> int:
        """Atomically roll up and delete metrics older than ``cutoff``."""
        with self.db.transaction() as txn:
            row = txn.execute(
                """
                WITH archived AS (
                    DELETE FROM tool_metrics
                    WHERE last_called_at < %s
                    RETURNING
                        project_id, server_name, tool_name, last_called_at,
                        call_count, success_count, failure_count, total_latency_ms
                ),
                rollup AS (
                    SELECT
                        project_id,
                        server_name,
                        tool_name,
                        date(last_called_at) AS metric_date,
                        SUM(call_count) AS total_calls,
                        SUM(success_count) AS total_success,
                        SUM(failure_count) AS total_failure,
                        SUM(total_latency_ms) AS total_latency
                    FROM archived
                    GROUP BY project_id, server_name, tool_name, date(last_called_at)
                ),
                upserted AS (
                    INSERT INTO tool_metrics_daily (
                        project_id, server_name, tool_name, date,
                        call_count, success_count, failure_count,
                        total_latency_ms, avg_latency_ms
                    )
                    SELECT
                        project_id,
                        server_name,
                        tool_name,
                        metric_date,
                        total_calls,
                        total_success,
                        total_failure,
                        total_latency,
                        total_latency / NULLIF(total_calls, 0)
                    FROM rollup
                    ON CONFLICT(project_id, server_name, tool_name, date) DO UPDATE SET
                        call_count = tool_metrics_daily.call_count + excluded.call_count,
                        success_count = tool_metrics_daily.success_count + excluded.success_count,
                        failure_count = tool_metrics_daily.failure_count + excluded.failure_count,
                        total_latency_ms = tool_metrics_daily.total_latency_ms +
                                           excluded.total_latency_ms,
                        avg_latency_ms = (
                            tool_metrics_daily.total_latency_ms + excluded.total_latency_ms
                        ) / (tool_metrics_daily.call_count + excluded.call_count)
                    RETURNING 1
                )
                SELECT COUNT(*) AS deleted_count FROM archived
                """,
                (cutoff,),
            ).fetchone()

        return int(row["deleted_count"]) if row is not None else 0

    def get_daily_metrics(
        self,
        project_id: str | None = None,
        server_name: str | None = None,
        tool_name: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[Any]:
        """
        Get aggregated daily metrics from the hub database.
        """
        conditions = []
        params: list[Any] = []

        if project_id:
            conditions.append("project_id = %s")
            params.append(project_id)
        if server_name:
            conditions.append("server_name = %s")
            params.append(server_name)
        if tool_name:
            conditions.append("tool_name = %s")
            params.append(tool_name)
        if start_date:
            conditions.append("date >= %s")
            params.append(start_date)
        if end_date:
            conditions.append("date <= %s")
            params.append(end_date)

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        return self.db.fetchall(
            f"SELECT * FROM tool_metrics_daily WHERE {where_clause} ORDER BY date DESC, call_count DESC",  # nosec # fixed predicates; bound values.
            tuple(params),
        )

    def get_retention_stats(self) -> Any:
        """
        Get statistics about metrics retention from the hub database.
        """
        return self.db.fetchone(
            """
            SELECT
                COUNT(*) as total_count,
                MIN(last_called_at) as oldest,
                MAX(last_called_at) as newest,
                SUM(call_count) as total_calls
            FROM tool_metrics
            """
        )
