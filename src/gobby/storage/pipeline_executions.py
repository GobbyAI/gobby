"""Pipeline execution persistence methods."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.id import generate_prefixed_id
from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution, StepStatus

logger = logging.getLogger("gobby.storage.pipelines")


class PipelineExecutionStorageMixin:
    """Pipeline execution CRUD, queries, search, and recovery methods."""

    db: HubDatabase
    project_id: str

    def create_execution(
        self,
        pipeline_name: str,
        inputs_json: str | None = None,
        session_id: str | None = None,
        parent_execution_id: str | None = None,
        continuation_prompt: str | None = None,
        definition_json: str | None = None,
    ) -> PipelineExecution:
        """Create a new pipeline execution.

        Args:
            pipeline_name: Name of the pipeline being executed
            inputs_json: JSON string of input parameters
            session_id: Session that triggered the execution
            parent_execution_id: Parent execution for nested pipelines
            continuation_prompt: Instructions for wake notification on completion
            definition_json: Snapshot of the pipeline definition at execution time

        Returns:
            Created PipelineExecution instance
        """
        execution_id = generate_prefixed_id("pe")
        now = datetime.now(UTC).isoformat()

        with self.db.transaction():
            self.db.execute(
                """
                INSERT INTO pipeline_executions (
                    id, pipeline_name, project_id, status, inputs_json,
                    session_id, parent_execution_id, continuation_prompt,
                    definition_json, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    execution_id,
                    pipeline_name,
                    self.project_id,
                    ExecutionStatus.PENDING.value,
                    inputs_json,
                    session_id,
                    parent_execution_id,
                    continuation_prompt,
                    definition_json,
                    now,
                    now,
                ),
            )

        execution = self.get_execution(execution_id)
        if execution is None:
            raise RuntimeError(f"Execution {execution_id} not found after creation")
        return execution

    def get_execution(self, execution_id: str) -> PipelineExecution | None:
        """Get execution by ID.

        Args:
            execution_id: Execution UUID

        Returns:
            PipelineExecution or None if not found
        """
        row = self.db.fetchone(
            "SELECT * FROM pipeline_executions WHERE id = %s",
            (execution_id,),
        )
        return PipelineExecution.from_row(row) if row else None

    def update_execution_status(
        self,
        execution_id: str,
        status: ExecutionStatus,
        resume_token: str | None = None,
        outputs_json: str | None = None,
    ) -> PipelineExecution | None:
        """Update execution status.

        Args:
            execution_id: Execution UUID
            status: New status
            resume_token: Resume token for approval gates
            outputs_json: JSON string of outputs (for completed status)

        Returns:
            Updated PipelineExecution or None if not found
        """
        now = datetime.now(UTC).isoformat()
        completed_at = (
            now
            if status
            in (
                ExecutionStatus.COMPLETED,
                ExecutionStatus.FAILED,
                ExecutionStatus.CANCELLED,
                ExecutionStatus.INTERRUPTED,
            )
            else None
        )

        self.db.execute(
            """
            UPDATE pipeline_executions
            SET status = %s,
                resume_token = COALESCE(%s, resume_token),
                outputs_json = COALESCE(%s, outputs_json),
                completed_at = COALESCE(%s, completed_at),
                updated_at = %s
            WHERE id = %s
            """,
            (
                status.value,
                resume_token,
                outputs_json,
                completed_at,
                now,
                execution_id,
            ),
        )

        return self.get_execution(execution_id)

    def _build_executions_filter(
        self,
        *,
        status: ExecutionStatus | None = None,
        pipeline_name: str | None = None,
        session_id: str | None = None,
        parent_execution_id: str | None = None,
    ) -> tuple[str, list[Any]]:
        """Build the WHERE fragment + params shared by list/count/status_summary.

        Returns a fragment that always begins with ``WHERE `` and is scoped to
        ``self.project_id`` (NULL-aware).
        """
        params: list[Any] = []
        if self.project_id is None:
            where = "WHERE project_id IS NULL"
        else:
            where = "WHERE project_id = %s"
            params.append(self.project_id)

        if status is not None:
            where += " AND status = %s"
            params.append(status.value)
        if pipeline_name is not None:
            where += " AND pipeline_name = %s"
            params.append(pipeline_name)
        if session_id is not None:
            where += " AND session_id = %s"
            params.append(session_id)
        if parent_execution_id is not None:
            where += " AND parent_execution_id = %s"
            params.append(parent_execution_id)

        return where, params

    def list_executions(
        self,
        status: ExecutionStatus | None = None,
        pipeline_name: str | None = None,
        session_id: str | None = None,
        parent_execution_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[PipelineExecution]:
        """List executions for the project.

        Args:
            status: Filter by status
            pipeline_name: Filter by pipeline name
            session_id: Filter by triggering session
            parent_execution_id: Filter by parent execution (nested pipelines)
            limit: Maximum number of results (must be > 0)
            offset: Number of leading rows to skip (must be >= 0)

        Returns:
            List of PipelineExecution instances

        Raises:
            ValueError: If ``limit <= 0`` or ``offset < 0``.
        """
        if limit <= 0:
            raise ValueError(f"limit must be > 0, got {limit}")
        if offset < 0:
            raise ValueError(f"offset must be >= 0, got {offset}")

        where, params = self._build_executions_filter(
            status=status,
            pipeline_name=pipeline_name,
            session_id=session_id,
            parent_execution_id=parent_execution_id,
        )
        sql = (
            f"SELECT * FROM pipeline_executions {where} "  # nosec B608 # fragment built from typed inputs
            "ORDER BY created_at DESC LIMIT %s OFFSET %s"
        )
        params.extend([limit, offset])

        rows = self.db.fetchall(sql, tuple(params))
        return [PipelineExecution.from_row(row) for row in rows]

    def count_executions(
        self,
        status: ExecutionStatus | None = None,
        pipeline_name: str | None = None,
        session_id: str | None = None,
        parent_execution_id: str | None = None,
    ) -> int:
        """Count executions matching the same filters as ``list_executions``.

        Returns:
            Total number of matching executions (independent of limit/offset).
        """
        where, params = self._build_executions_filter(
            status=status,
            pipeline_name=pipeline_name,
            session_id=session_id,
            parent_execution_id=parent_execution_id,
        )
        sql = f"SELECT COUNT(*) AS cnt FROM pipeline_executions {where}"  # nosec B608
        row = self.db.fetchone(sql, tuple(params))
        return int(row["cnt"]) if row else 0

    def execution_metrics(
        self,
        status: ExecutionStatus | None = None,
        pipeline_name: str | None = None,
        session_id: str | None = None,
        parent_execution_id: str | None = None,
    ) -> tuple[int, dict[str, int]]:
        """Return filtered total and status summary in one query."""
        where, params = self._build_executions_filter(
            pipeline_name=pipeline_name,
            session_id=session_id,
            parent_execution_id=parent_execution_id,
        )
        status_value = status.value if status is not None else None
        sql = f"""
            WITH filtered AS (
                SELECT status FROM pipeline_executions {where}
            ),
            total AS (
                SELECT COUNT(*) AS cnt
                FROM filtered
                WHERE (%s::text IS NULL OR status = %s)
            ),
            summary AS (
                SELECT status, COUNT(*) AS cnt
                FROM filtered
                GROUP BY status
            )
            SELECT '__total__' AS status, cnt FROM total
            UNION ALL
            SELECT status, cnt FROM summary
        """  # nosec B608 # WHERE fragment built from typed inputs.
        rows = self.db.fetchall(sql, (*params, status_value, status_value))
        total = 0
        summary: dict[str, int] = {}
        for row in rows:
            row_status = str(row["status"])
            count = int(row["cnt"])
            if row_status == "__total__":
                total = count
            else:
                summary[row_status] = count
        return total, summary

    def status_summary_for_executions(
        self,
        pipeline_name: str | None = None,
        session_id: str | None = None,
        parent_execution_id: str | None = None,
    ) -> dict[str, int]:
        """Filter-scoped status counts.

        Applies the same filters as ``list_executions`` minus the ``status``
        predicate, then groups by status. Useful for paginated UIs that want
        to show "X running / Y completed" within the active filter scope.

        Returns:
            Dict mapping status values to counts.
        """
        where, params = self._build_executions_filter(
            pipeline_name=pipeline_name,
            session_id=session_id,
            parent_execution_id=parent_execution_id,
        )
        sql = (
            f"SELECT status, COUNT(*) AS cnt FROM pipeline_executions {where} "  # nosec B608
            "GROUP BY status"
        )
        rows = self.db.fetchall(sql, tuple(params))
        return {row["status"]: int(row["cnt"]) for row in rows}

    def get_unreviewed_completions(self, limit: int = 10) -> list[PipelineExecution]:
        """Get terminal executions that have no review.

        Returns completed, failed, or cancelled executions where
        review_json is NULL, ordered by completion time (newest first).

        Args:
            limit: Maximum number of results

        Returns:
            List of PipelineExecution instances awaiting review
        """
        params: list[Any] = []
        if self.project_id is None:
            query = "SELECT * FROM pipeline_executions WHERE project_id IS NULL"
        else:
            query = "SELECT * FROM pipeline_executions WHERE project_id = %s"
            params.append(self.project_id)

        query += " AND status IN (%s, %s, %s) AND review_json IS NULL ORDER BY completed_at DESC LIMIT %s"
        params.extend(
            [
                ExecutionStatus.COMPLETED.value,
                ExecutionStatus.FAILED.value,
                ExecutionStatus.CANCELLED.value,
                limit,
            ]
        )

        rows = self.db.fetchall(query, tuple(params))
        return [PipelineExecution.from_row(row) for row in rows]

    def store_review(self, execution_id: str, review_json: str) -> None:
        """Store a review JSON blob on a pipeline execution.

        Args:
            execution_id: Execution ID to update
            review_json: JSON string containing the review data
        """
        now = datetime.now(UTC).isoformat()
        self.db.execute(
            "UPDATE pipeline_executions SET review_json = %s, updated_at = %s WHERE id = %s",
            (review_json, now, execution_id),
        )

    def search_executions(
        self,
        query: str,
        search_errors: bool = True,
        search_outputs: bool = False,
        status: ExecutionStatus | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[PipelineExecution]:
        """Search executions by text across pipeline_name and optionally step errors/outputs.

        Args:
            query: Search text (matched with LIKE)
            search_errors: Also search step_executions.error text
            search_outputs: Also search step_executions.output_json text
            status: Filter by status
            limit: Maximum number of results (must be > 0)
            offset: Number of leading rows to skip (must be >= 0)

        Returns:
            List of matching PipelineExecution instances

        Raises:
            ValueError: If limit <= 0 or offset < 0.
        """
        if limit <= 0:
            raise ValueError(f"limit must be > 0, got {limit}")
        if offset < 0:
            raise ValueError(f"offset must be >= 0, got {offset}")
        escaped_query = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like_pattern = f"%{escaped_query}%"
        params: list[Any] = []

        # Build WHERE conditions
        if self.project_id is None:
            project_clause = "pe.project_id IS NULL"
        else:
            project_clause = "pe.project_id = %s"
            params.append(self.project_id)

        # Build LIKE conditions
        like_conditions = ["pe.pipeline_name LIKE %s ESCAPE '\\'"]
        params.append(like_pattern)

        if search_errors:
            like_conditions.append("se.error LIKE %s ESCAPE '\\'")
            params.append(like_pattern)

        if search_outputs:
            like_conditions.append("se.output_json LIKE %s ESCAPE '\\'")
            params.append(like_pattern)

        like_clause = " OR ".join(like_conditions)

        if status is not None:
            status_clause = " AND pe.status = %s"
            params.append(status.value)
        else:
            status_clause = ""

        params.extend([limit, offset])

        sql = f"""
            SELECT DISTINCT pe.* FROM pipeline_executions pe
            LEFT JOIN step_executions se ON se.execution_id = pe.id
            WHERE {project_clause}
              AND ({like_clause}){status_clause}
            ORDER BY pe.created_at DESC
            LIMIT %s OFFSET %s
        """  # nosec B608

        rows = self.db.fetchall(sql, tuple(params))
        return [PipelineExecution.from_row(row) for row in rows]

    def count_search_executions(
        self,
        query: str,
        search_errors: bool = True,
        search_outputs: bool = False,
        status: ExecutionStatus | None = None,
    ) -> int:
        """Count executions matching the same filters as ``search_executions``.

        Returns:
            Total number of matching executions (independent of limit/offset).
        """
        escaped_query = (
            query.replace(chr(92), chr(92) * 2)
            .replace("%", chr(92) + "%")
            .replace("_", chr(92) + "_")
        )
        like_pattern = f"%{escaped_query}%"
        params: list[Any] = []

        if self.project_id is None:
            project_clause = "pe.project_id IS NULL"
        else:
            project_clause = "pe.project_id = %s"
            params.append(self.project_id)

        like_conditions = ["pe.pipeline_name LIKE %s ESCAPE '" + chr(92) + "'"]
        params.append(like_pattern)
        if search_errors:
            like_conditions.append("se.error LIKE %s ESCAPE '" + chr(92) + "'")
            params.append(like_pattern)
        if search_outputs:
            like_conditions.append("se.output_json LIKE %s ESCAPE '" + chr(92) + "'")
            params.append(like_pattern)
        like_clause = " OR ".join(like_conditions)

        if status is not None:
            status_clause = " AND pe.status = %s"
            params.append(status.value)
        else:
            status_clause = ""

        sql = f"""
            SELECT COUNT(*) AS cnt FROM (
                SELECT DISTINCT pe.id FROM pipeline_executions pe
                LEFT JOIN step_executions se ON se.execution_id = pe.id
                WHERE {project_clause}
                  AND ({like_clause}){status_clause}
            )
        """  # nosec B608
        row = self.db.fetchone(sql, tuple(params))
        return int(row["cnt"]) if row else 0

    def get_execution_by_resume_token(self, token: str) -> PipelineExecution | None:
        """Get execution by resume token.

        Args:
            token: Resume token

        Returns:
            PipelineExecution or None if not found
        """
        row = self.db.fetchone(
            "SELECT * FROM pipeline_executions WHERE resume_token = %s",
            (token,),
        )
        return PipelineExecution.from_row(row) if row else None

    def resolve_execution_reference(self, ref: str) -> str:
        """Resolve an execution reference to a UUID.

        Supports:
        - Full UUID: pe-abc123... or UUID format
        - UUID prefix: pe-abc1 (matches by prefix)

        Args:
            ref: Execution reference

        Returns:
            Execution UUID

        Raises:
            ValueError: If reference cannot be resolved
        """
        # Try exact match first
        execution = self.get_execution(ref)
        if execution:
            return execution.id

        # Try prefix match
        row = self.db.fetchone(
            "SELECT id FROM pipeline_executions WHERE id LIKE %s AND project_id = %s",
            (f"{ref}%", self.project_id),
        )
        if row:
            result: str = row["id"]
            return result

        raise ValueError(f"Cannot resolve execution reference: {ref}")

    def interrupt_stale_running_executions(self, exclude_ids: set[str] | None = None) -> int:
        """Mark running executions and their steps as interrupted.

        Called during daemon startup to recover from unclean shutdowns.
        Uses INTERRUPTED status (non-terminal) instead of FAILED so pipelines
        with resume_on_restart=true can be re-queued.
        Leaves waiting_approval executions alone (they can still be approved).

        Args:
            exclude_ids: Execution IDs to skip (e.g. resumable pipelines).

        Returns:
            Number of executions marked as interrupted.
        """
        now = datetime.now(UTC).isoformat()

        def build_not_in_clause(
            ids: set[str] | None, column_name: str
        ) -> tuple[str, tuple[str, ...]]:
            if not ids:
                return "", ()
            placeholders = ", ".join("%s" for _ in ids)
            return f" AND {column_name} NOT IN ({placeholders})", tuple(ids)

        # Build exclusion clause for parameter binding
        exclude_clause, exclude_params = build_not_in_clause(exclude_ids, "execution_id")
        exec_exclude_clause, exec_exclude_params = build_not_in_clause(exclude_ids, "id")

        # Fail running step executions that belong to running pipeline executions
        self.db.execute(
            f"""
            UPDATE step_executions
            SET status = %s, error = 'Daemon restarted', completed_at = %s
            WHERE status = %s
              AND execution_id IN (
                  SELECT id FROM pipeline_executions
                  WHERE status = %s AND project_id = %s
              ){exclude_clause}
            """,  # nosec B608
            (
                StepStatus.FAILED.value,
                now,
                StepStatus.RUNNING.value,
                ExecutionStatus.RUNNING.value,
                self.project_id,
                *exclude_params,
            ),
        )

        # Mark running pipeline executions as interrupted
        cursor = self.db.execute(
            f"""
            UPDATE pipeline_executions
            SET status = %s, outputs_json = %s, updated_at = %s
            WHERE status = %s AND project_id = %s{exec_exclude_clause}
            """,  # nosec B608
            (
                ExecutionStatus.INTERRUPTED.value,
                '{"error": "Daemon restarted while execution was in progress"}',
                now,
                ExecutionStatus.RUNNING.value,
                self.project_id,
                *exec_exclude_params,
            ),
        )

        count: int = cursor.rowcount if cursor else 0
        if count > 0:
            logger.info(f"Marked {count} stale running executions as interrupted after restart")
        return count

    def fail_stale_running_executions(self, exclude_ids: set[str] | None = None) -> int:
        """Backwards-compatible alias for interrupt_stale_running_executions."""
        return self.interrupt_stale_running_executions(exclude_ids=exclude_ids)

    def count_by_status(self) -> dict[str, int]:
        """Count executions grouped by status.

        Returns:
            Dict mapping status values to their counts.
        """
        rows = self.db.fetchall(
            "SELECT status, COUNT(*) as cnt FROM pipeline_executions WHERE project_id = %s GROUP BY status",
            (self.project_id,),
        )
        return {row["status"]: row["cnt"] for row in rows}

    def get_stalled_executions(self, stall_threshold_seconds: int) -> list[PipelineExecution]:
        """Get running executions that haven't been updated within the threshold.

        Args:
            stall_threshold_seconds: Seconds of inactivity before considering stalled

        Returns:
            List of stalled PipelineExecution instances
        """
        from datetime import timedelta

        cutoff = (datetime.now(UTC) - timedelta(seconds=stall_threshold_seconds)).isoformat()

        rows = self.db.fetchall(
            """
            SELECT * FROM pipeline_executions
            WHERE status = %s
              AND project_id = %s
              AND updated_at < %s
            ORDER BY updated_at ASC
            """,
            (ExecutionStatus.RUNNING.value, self.project_id, cutoff),
        )
        return [PipelineExecution.from_row(row) for row in rows]
