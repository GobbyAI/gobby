"""Test run storage manager.

Provides CRUD operations for test/lint/typecheck run records.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from gobby.storage.database import DatabaseProtocol
from gobby.storage.test_run_models import TestRun
from gobby.utils.id import generate_prefixed_id

logger = logging.getLogger(__name__)


class TestRunStorage:
    """Manager for test run storage."""

    def __init__(self, db: DatabaseProtocol):
        self.db = db

    def create_run(
        self,
        category: str,
        command: str,
        session_id: str | None = None,
        project_id: str | None = None,
    ) -> TestRun:
        """Create a new test run record."""
        run_id = generate_prefixed_id("tr", length=12)
        now = datetime.now(UTC).isoformat()

        run = TestRun(
            id=run_id,
            category=category,
            command=command,
            status="running",
            started_at=now,
            created_at=now,
            session_id=session_id,
            project_id=project_id,
        )

        self.db.execute(
            """
            INSERT INTO test_runs (
                id, session_id, project_id, category, command,
                status, exit_code, summary, output_file,
                started_at, completed_at, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.id,
                run.session_id,
                run.project_id,
                run.category,
                run.command,
                run.status,
                run.exit_code,
                run.summary,
                run.output_file,
                run.started_at,
                run.completed_at,
                run.created_at,
            ),
        )

        return run

    _VALID_UPDATE_FIELDS = frozenset(
        {
            "status",
            "exit_code",
            "summary",
            "output_file",
            "completed_at",
        }
    )

    def update_run(self, run_id: str, **fields: Any) -> TestRun | None:
        """Update test run fields."""
        if not fields:
            return self.get_run(run_id)

        invalid_fields = set(fields.keys()) - self._VALID_UPDATE_FIELDS
        if invalid_fields:
            raise ValueError(f"Invalid field names: {invalid_fields}")

        set_clause = ", ".join(f"{key} = ?" for key in fields.keys())
        values = list(fields.values()) + [run_id]

        self.db.execute(
            f"UPDATE test_runs SET {set_clause} WHERE id = ?",  # nosec B608
            tuple(values),
        )

        return self.get_run(run_id)

    def get_run(self, run_id: str) -> TestRun | None:
        """Get a test run by ID."""
        row = self.db.fetchone("SELECT * FROM test_runs WHERE id = ?", (run_id,))
        return TestRun.from_row(row) if row else None

    def list_runs(
        self,
        session_id: str | None = None,
        project_id: str | None = None,
        limit: int = 20,
    ) -> list[TestRun]:
        """List test runs with optional filters."""
        conditions: list[str] = []
        params: list[Any] = []

        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if project_id:
            conditions.append("project_id = ?")
            params.append(project_id)

        where_clause = " AND ".join(conditions) if conditions else "1=1"
        params.append(limit)

        rows = self.db.fetchall(
            f"""
            SELECT * FROM test_runs
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT ?
            """,  # nosec B608
            tuple(params),
        )
        return [TestRun.from_row(row) for row in rows]

    def cleanup_stale_runs(self) -> int:
        """Mark any 'running' test runs as failed (interrupted by daemon restart)."""
        now = datetime.now(UTC).isoformat()
        cursor = self.db.execute(
            "UPDATE test_runs SET status = 'failed', summary = 'Interrupted by daemon restart', completed_at = ? WHERE status = 'running'",
            (now,),
        )
        count = cursor.rowcount
        if count > 0:
            logger.info("Cleaned up %d stale test runs from previous daemon session", count)
        return count

    def cleanup_old_runs(self, days: int = 7) -> int:
        """Delete runs older than the given number of days."""
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        cursor = self.db.execute(
            "DELETE FROM test_runs WHERE created_at < ?",
            (cutoff,),
        )
        deleted = cursor.rowcount
        if deleted > 0:
            logger.info("Cleaned up %d test runs older than %d days", deleted, days)
        return deleted
