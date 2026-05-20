"""Local pipeline execution storage manager."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sql_dialect import timestamp_plus_seconds_before_now_expr
from gobby.utils.id import generate_prefixed_id
from gobby.workflows.pipeline_state import (
    ExecutionStatus,
    PipelineExecution,
    StepExecution,
    StepStatus,
)

logger = logging.getLogger(__name__)


class LocalPipelineExecutionManager:
    """Manager for local pipeline execution storage."""

    def __init__(self, db: HubDatabase, project_id: str):
        """Initialize with database connection and project context.

        Args:
            db: Database connection
            project_id: Project ID for scoping executions
        """
        self.db = db
        self.project_id = project_id

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
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            "SELECT * FROM pipeline_executions WHERE id = ?",
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
            SET status = ?,
                resume_token = COALESCE(?, resume_token),
                outputs_json = COALESCE(?, outputs_json),
                completed_at = COALESCE(?, completed_at),
                updated_at = ?
            WHERE id = ?
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
            where = "WHERE project_id = ?"
            params.append(self.project_id)

        if status is not None:
            where += " AND status = ?"
            params.append(status.value)
        if pipeline_name is not None:
            where += " AND pipeline_name = ?"
            params.append(pipeline_name)
        if session_id is not None:
            where += " AND session_id = ?"
            params.append(session_id)
        if parent_execution_id is not None:
            where += " AND parent_execution_id = ?"
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
            "ORDER BY created_at DESC LIMIT ? OFFSET ?"
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
                WHERE (? IS NULL OR status = ?)
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
            query = "SELECT * FROM pipeline_executions WHERE project_id = ?"
            params.append(self.project_id)

        query += (
            " AND status IN (?, ?, ?) AND review_json IS NULL ORDER BY completed_at DESC LIMIT ?"
        )
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
            "UPDATE pipeline_executions SET review_json = ?, updated_at = ? WHERE id = ?",
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
            project_clause = "pe.project_id = ?"
            params.append(self.project_id)

        # Build LIKE conditions
        like_conditions = ["pe.pipeline_name LIKE ? ESCAPE '\\'"]
        params.append(like_pattern)

        if search_errors:
            like_conditions.append("se.error LIKE ? ESCAPE '\\'")
            params.append(like_pattern)

        if search_outputs:
            like_conditions.append("se.output_json LIKE ? ESCAPE '\\'")
            params.append(like_pattern)

        like_clause = " OR ".join(like_conditions)

        if status is not None:
            status_clause = " AND pe.status = ?"
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
            LIMIT ? OFFSET ?
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
            project_clause = "pe.project_id = ?"
            params.append(self.project_id)

        like_conditions = ["pe.pipeline_name LIKE ? ESCAPE '" + chr(92) + "'"]
        params.append(like_pattern)
        if search_errors:
            like_conditions.append("se.error LIKE ? ESCAPE '" + chr(92) + "'")
            params.append(like_pattern)
        if search_outputs:
            like_conditions.append("se.output_json LIKE ? ESCAPE '" + chr(92) + "'")
            params.append(like_pattern)
        like_clause = " OR ".join(like_conditions)

        if status is not None:
            status_clause = " AND pe.status = ?"
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
            "SELECT * FROM pipeline_executions WHERE resume_token = ?",
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
            "SELECT id FROM pipeline_executions WHERE id LIKE ? AND project_id = ?",
            (f"{ref}%", self.project_id),
        )
        if row:
            result: str = row["id"]
            return result

        raise ValueError(f"Cannot resolve execution reference: {ref}")

    # Step execution methods

    def create_step_execution(
        self,
        execution_id: str,
        step_id: str,
        input_json: str | None = None,
    ) -> StepExecution:
        """Create a new step execution.

        Args:
            execution_id: Parent pipeline execution ID
            step_id: Step ID from pipeline definition
            input_json: JSON string of step input

        Returns:
            Created StepExecution instance
        """
        self.db.execute(
            """
            INSERT INTO step_executions (
                execution_id, step_id, status, input_json
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                execution_id,
                step_id,
                StepStatus.PENDING.value,
                input_json,
            ),
        )

        # Get the created step by execution_id and step_id (unique combination)
        row = self.db.fetchone(
            "SELECT * FROM step_executions WHERE execution_id = ? AND step_id = ?",
            (execution_id, step_id),
        )
        if row is None:
            raise RuntimeError(f"Step {step_id} not found after creation")
        return StepExecution.from_row(row)

    def update_step_execution(
        self,
        step_execution_id: int,
        status: StepStatus | None = None,
        output_json: str | None = None,
        error: str | None = None,
        approval_token: str | None = None,
        approved_by: str | None = None,
        approval_timeout_seconds: int | None = None,
    ) -> StepExecution | None:
        """Update a step execution.

        Args:
            step_execution_id: Step execution ID (integer)
            status: New status
            output_json: JSON string of step output
            error: Error message (for failed status)
            approval_token: Token for approval gate
            approved_by: Who approved the step

        Returns:
            Updated StepExecution or None if not found
        """
        now = datetime.now(UTC).isoformat()

        # Build update parts dynamically (step_executions has no updated_at column)
        updates: list[str] = []
        params: list[Any] = []

        if status is not None:
            updates.append("status = ?")
            params.append(status.value)
            # Set timestamps based on status
            if status == StepStatus.RUNNING:
                updates.append("started_at = COALESCE(started_at, ?)")
                params.append(now)
            elif status in (StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.SKIPPED):
                updates.append("completed_at = COALESCE(completed_at, ?)")
                params.append(now)

        if output_json is not None:
            updates.append("output_json = ?")
            params.append(output_json)

        if error is not None:
            updates.append("error = ?")
            params.append(error)

        if approval_token is not None:
            updates.append("approval_token = ?")
            params.append(approval_token)

        if approved_by is not None:
            updates.append("approved_by = ?")
            params.append(approved_by)
            updates.append("approved_at = ?")
            params.append(now)

        if approval_timeout_seconds is not None:
            updates.append("approval_timeout_seconds = ?")
            params.append(approval_timeout_seconds)

        if not updates:
            # Nothing to update
            row = self.db.fetchone(
                "SELECT * FROM step_executions WHERE id = ?",
                (step_execution_id,),
            )
            return StepExecution.from_row(row) if row else None

        # Append step_execution_id for WHERE clause
        params.append(step_execution_id)

        # updates list contains only hardcoded column names, values are parameterized
        self.db.execute(
            f"UPDATE step_executions SET {', '.join(updates)} WHERE id = ?",  # nosec B608
            tuple(params),
        )

        row = self.db.fetchone(
            "SELECT * FROM step_executions WHERE id = ?",
            (step_execution_id,),
        )
        return StepExecution.from_row(row) if row else None

    def get_step_by_approval_token(self, token: str) -> StepExecution | None:
        """Get step execution by approval token.

        Args:
            token: Approval token

        Returns:
            StepExecution or None if not found
        """
        row = self.db.fetchone(
            "SELECT * FROM step_executions WHERE approval_token = ?",
            (token,),
        )
        return StepExecution.from_row(row) if row else None

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
            placeholders = ", ".join("?" for _ in ids)
            return f" AND {column_name} NOT IN ({placeholders})", tuple(ids)

        # Build exclusion clause for parameter binding
        exclude_clause, exclude_params = build_not_in_clause(exclude_ids, "execution_id")
        exec_exclude_clause, exec_exclude_params = build_not_in_clause(exclude_ids, "id")

        # Fail running step executions that belong to running pipeline executions
        self.db.execute(
            f"""
            UPDATE step_executions
            SET status = ?, error = 'Daemon restarted', completed_at = ?
            WHERE status = ?
              AND execution_id IN (
                  SELECT id FROM pipeline_executions
                  WHERE status = ? AND project_id = ?
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
            SET status = ?, outputs_json = ?, updated_at = ?
            WHERE status = ? AND project_id = ?{exec_exclude_clause}
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

    def get_expired_approval_steps(self) -> list[StepExecution]:
        """Get step executions where approval has timed out.

        Finds steps that are waiting_approval with a configured timeout
        where started_at + timeout_seconds < now.

        Returns:
            List of expired StepExecution instances.
        """
        timeout_expired_sql = timestamp_plus_seconds_before_now_expr(
            self.db,
            "se.started_at",
            "se.approval_timeout_seconds",
        )
        rows = self.db.fetchall(
            f"""
            SELECT se.* FROM step_executions se
            JOIN pipeline_executions pe ON se.execution_id = pe.id
            WHERE se.status = ?
              AND se.approval_timeout_seconds IS NOT NULL
              AND se.started_at IS NOT NULL
              AND {timeout_expired_sql}
              AND pe.project_id = ?
            """,  # nosec B608 - timeout expression is selected by storage dialect.
            (StepStatus.WAITING_APPROVAL.value, self.project_id),
        )
        return [StepExecution.from_row(row) for row in rows]

    # ── Completion Subscriber CRUD ──

    def add_completion_subscriber(self, completion_id: str, session_id: str) -> None:
        """Add a subscriber for a completion event (idempotent).

        Args:
            completion_id: Execution or run ID to subscribe to
            session_id: Session to notify on completion
        """
        self.db.execute(
            """
            INSERT INTO completion_subscribers (completion_id, session_id)
            VALUES (?, ?)
            ON CONFLICT (completion_id, session_id) DO NOTHING
            """,
            (completion_id, session_id),
        )

    def add_completion_subscribers(self, completion_id: str, session_ids: list[str]) -> None:
        """Bulk add subscribers for a completion event.

        Args:
            completion_id: Execution or run ID to subscribe to
            session_ids: Sessions to notify on completion
        """
        if not session_ids:
            return
        self.db.executemany(
            "INSERT INTO completion_subscribers (completion_id, session_id) "
            "VALUES (?, ?) ON CONFLICT (completion_id, session_id) DO NOTHING",
            [(completion_id, sid) for sid in session_ids],
        )

    def get_completion_subscribers(self, completion_id: str) -> list[str]:
        """Get all subscriber session IDs for a completion event.

        Args:
            completion_id: Execution or run ID

        Returns:
            List of subscribed session IDs
        """
        rows = self.db.fetchall(
            "SELECT session_id FROM completion_subscribers WHERE completion_id = ?",
            (completion_id,),
        )
        return [row["session_id"] for row in rows]

    def remove_completion_subscribers(self, completion_id: str) -> None:
        """Remove all subscribers for a completion event.

        Args:
            completion_id: Execution or run ID
        """
        self.db.execute(
            "DELETE FROM completion_subscribers WHERE completion_id = ?",
            (completion_id,),
        )

    def reset_steps_from(self, execution_id: str, from_step_id: str) -> int:
        """Reset a step and all subsequent steps to PENDING.

        Clears output, error, and timestamps. Returns count of reset steps.

        Raises:
            ValueError: If from_step_id is not found in the execution's steps.
        """
        steps = self.get_steps_for_execution(execution_id)
        found = False
        count = 0
        with self.db.transaction():
            for step in steps:
                if step.step_id == from_step_id:
                    found = True
                if found:
                    self.db.execute(
                        """
                        UPDATE step_executions
                        SET status = ?, output_json = NULL, error = NULL,
                            started_at = NULL, completed_at = NULL,
                            approval_token = NULL, approved_by = NULL, approved_at = NULL
                        WHERE id = ?
                        """,
                        (StepStatus.PENDING.value, step.id),
                    )
                    count += 1
        if not found:
            raise ValueError(f"Step '{from_step_id}' not found in execution '{execution_id}'")
        return count

    def get_failed_steps(self, execution_id: str) -> list[StepExecution]:
        """Get failed steps for an execution.

        Args:
            execution_id: Pipeline execution ID

        Returns:
            List of StepExecution instances with FAILED status
        """
        rows = self.db.fetchall(
            "SELECT * FROM step_executions WHERE execution_id = ? AND status = ?",
            (execution_id, StepStatus.FAILED.value),
        )
        return [StepExecution.from_row(row) for row in rows]

    def count_by_status(self) -> dict[str, int]:
        """Count executions grouped by status.

        Returns:
            Dict mapping status values to their counts.
        """
        rows = self.db.fetchall(
            "SELECT status, COUNT(*) as cnt FROM pipeline_executions WHERE project_id = ? GROUP BY status",
            (self.project_id,),
        )
        return {row["status"]: row["cnt"] for row in rows}

    def get_steps_for_executions(self, execution_ids: list[str]) -> dict[str, list[StepExecution]]:
        """Batch-load steps for multiple executions.

        Args:
            execution_ids: List of pipeline execution IDs.

        Returns:
            Dict mapping execution_id to list of StepExecution instances.
        """
        if not execution_ids:
            return {}
        placeholders = ", ".join("?" for _ in execution_ids)
        rows = self.db.fetchall(
            f"SELECT * FROM step_executions WHERE execution_id IN ({placeholders}) ORDER BY id",  # nosec B608
            tuple(execution_ids),
        )
        result: dict[str, list[StepExecution]] = {eid: [] for eid in execution_ids}
        for row in rows:
            step = StepExecution.from_row(row)
            result[step.execution_id].append(step)
        return result

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
            WHERE status = ?
              AND project_id = ?
              AND updated_at < ?
            ORDER BY updated_at ASC
            """,
            (ExecutionStatus.RUNNING.value, self.project_id, cutoff),
        )
        return [PipelineExecution.from_row(row) for row in rows]

    def get_steps_for_execution(self, execution_id: str) -> list[StepExecution]:
        """Get all steps for an execution.

        Args:
            execution_id: Pipeline execution ID

        Returns:
            List of StepExecution instances
        """
        rows = self.db.fetchall(
            "SELECT * FROM step_executions WHERE execution_id = ? ORDER BY id",
            (execution_id,),
        )
        return [StepExecution.from_row(row) for row in rows]
