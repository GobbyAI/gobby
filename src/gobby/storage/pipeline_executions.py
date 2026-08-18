"""Pipeline execution persistence methods."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import utc_now
from gobby.utils.uuid_validation import is_full_uuid
from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution, StepStatus

logger = logging.getLogger("gobby.storage.pipelines")


def _execution_search_filter(
    query: str,
    *,
    search_errors: bool,
    search_outputs: bool,
    status: ExecutionStatus | None,
) -> tuple[str, str, list[Any]]:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    conditions = ["pe.pipeline_name LIKE %s ESCAPE '\\'"]
    params: list[Any] = [pattern]
    if search_errors:
        conditions.append("se.error LIKE %s ESCAPE '\\'")
        params.append(pattern)
    if search_outputs:
        conditions.append("CAST(se.output_json AS TEXT) LIKE %s ESCAPE '\\'")
        params.append(pattern)
    status_clause = ""
    if status is not None:
        status_clause = " AND pe.status = %s"
        params.append(status.value)
    return " OR ".join(conditions), status_clause, params


class PipelineExecutionNotFoundError(RuntimeError):
    """Raised when an execution-scoped update matches no rows."""


class PipelineExecutionStorageMixin:
    """Pipeline execution CRUD, queries, search, and recovery methods."""

    db: HubDatabase
    project_id: str | None

    def _project_predicate(self, column_name: str = "project_id") -> tuple[str, tuple[str, ...]]:
        """Return a scoped predicate, or an always-true predicate for all projects."""
        if self.project_id is None:
            return "1 = 1", ()
        return f"{column_name} = %s", (self.project_id,)

    def _require_project_id(self) -> str:
        """Return project id for write paths that must be project-scoped."""
        if self.project_id is None:
            raise ValueError("project_id is required")
        return self.project_id

    def create_execution(
        self,
        pipeline_name: str,
        inputs_json: str | None = None,
        session_id: str | None = None,
        parent_execution_id: str | None = None,
        continuation_prompt: str | None = None,
        definition_json: str | None = None,
        project_id: str | None = None,
    ) -> PipelineExecution:
        """Create a new pipeline execution.

        Args:
            pipeline_name: Name of the pipeline being executed
            inputs_json: JSON string of input parameters
            session_id: Session that triggered the execution
            parent_execution_id: Parent execution for nested pipelines
            continuation_prompt: Instructions for wake notification on completion
            definition_json: Snapshot of the pipeline definition at execution time
            project_id: Per-execution project override

        Returns:
            Created PipelineExecution instance
        """
        resolved_project_id = project_id or self._require_project_id()
        execution_id = str(uuid.uuid4())

        with self.db.transaction():
            row = self.db.execute(
                """
                INSERT INTO pipeline_executions (
                    id, pipeline_name, project_id, status, inputs_json,
                    session_id, parent_execution_id, continuation_prompt,
                    definition_json
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING created_at, updated_at
                """,
                (
                    execution_id,
                    pipeline_name,
                    resolved_project_id,
                    ExecutionStatus.PENDING.value,
                    inputs_json,
                    session_id,
                    parent_execution_id,
                    continuation_prompt,
                    definition_json,
                ),
            ).fetchone()

        if row is None:
            raise RuntimeError(f"Failed to insert pipeline execution {execution_id}")
        return PipelineExecution(
            id=execution_id,
            pipeline_name=pipeline_name,
            project_id=resolved_project_id,
            status=ExecutionStatus.PENDING,
            inputs_json=inputs_json,
            session_id=session_id,
            parent_execution_id=parent_execution_id,
            continuation_prompt=continuation_prompt,
            definition_json=definition_json,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_execution(self, execution_id: str) -> PipelineExecution | None:
        """Get execution by ID.

        Args:
            execution_id: Execution UUID

        Returns:
            PipelineExecution or None if not found
        """
        project_clause, project_params = self._project_predicate()
        row = self.db.fetchone(
            f"SELECT * FROM pipeline_executions WHERE id = %s AND {project_clause}",  # nosec B608
            (execution_id, *project_params),
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
            Updated PipelineExecution or None if not found or already terminal
        """
        now = utc_now()
        completed_at = (
            now
            if status
            in (
                ExecutionStatus.COMPLETED,
                ExecutionStatus.FAILED,
                ExecutionStatus.CANCELLED,
            )
            else None
        )

        project_clause, project_params = self._project_predicate()
        cursor = self.db.execute(
            # project_clause is generated by the storage manager.
            f"""
            UPDATE pipeline_executions
            SET status = %s,
                resume_token = COALESCE(%s, resume_token),
                outputs_json = COALESCE(%s, outputs_json),
                completed_at = COALESCE(%s, completed_at),
                updated_at = %s
            WHERE id = %s
              AND {project_clause}
              AND status NOT IN (%s, %s, %s)
            """,  # nosec
            (
                status.value,
                resume_token,
                outputs_json,
                completed_at,
                now,
                execution_id,
                *project_params,
                ExecutionStatus.COMPLETED.value,
                ExecutionStatus.FAILED.value,
                ExecutionStatus.CANCELLED.value,
            ),
        )

        if cursor.rowcount == 0:
            return None
        return self.get_execution(execution_id)

    def update_stalled_execution_status(
        self,
        execution_id: str,
        status: ExecutionStatus,
        observed_status: ExecutionStatus,
        observed_updated_at: datetime,
        outputs_json: str | None = None,
    ) -> PipelineExecution | None:
        """Resolve a heartbeat stall only while the scanned state is current."""
        valid_transition = (
            observed_status == ExecutionStatus.RUNNING
            and status in (ExecutionStatus.RUNNING, ExecutionStatus.FAILED)
        ) or (observed_status == ExecutionStatus.PENDING and status == ExecutionStatus.FAILED)
        if not valid_transition:
            raise ValueError("Stalled executions can only remain running or transition to failed")

        now = utc_now()
        completed_at = now if status == ExecutionStatus.FAILED else None
        project_clause, project_params = self._project_predicate()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                # project_clause is generated by the storage manager.
                f"""
                UPDATE pipeline_executions
                SET status = %s,
                    outputs_json = COALESCE(%s, outputs_json),
                    completed_at = COALESCE(%s, completed_at),
                    updated_at = %s
                WHERE id = %s
                  AND {project_clause}
                  AND status = %s
                  AND updated_at = %s
                """,  # nosec
                (
                    status.value,
                    outputs_json,
                    completed_at,
                    now,
                    execution_id,
                    *project_params,
                    observed_status.value,
                    observed_updated_at,
                ),
            )
            if cursor.rowcount == 0:
                return None
            if observed_status == ExecutionStatus.PENDING:
                conn.execute(
                    "DELETE FROM task_dispatch_mutex WHERE run_id = %s",
                    (execution_id,),
                )
        return self.get_execution(execution_id)

    def claim_failed_execution_for_resume(self, execution_id: str) -> PipelineExecution | None:
        """Atomically transition a failed execution to running for one resume caller."""
        project_clause, project_params = self._project_predicate()
        cursor = self.db.execute(
            # project_clause is generated by the storage manager.
            f"""
            UPDATE pipeline_executions
            SET status = %s,
                completed_at = NULL,
                updated_at = %s
            WHERE id = %s
              AND status = %s
              AND {project_clause}
            """,  # nosec
            (
                ExecutionStatus.RUNNING.value,
                utc_now(),
                execution_id,
                ExecutionStatus.FAILED.value,
                *project_params,
            ),
        )
        if cursor.rowcount == 0:
            return None
        return self.get_execution(execution_id)

    def update_execution_session(self, execution_id: str, session_id: str) -> None:
        """Persist the session that owns a running execution.

        Top-level pipelines run under a dedicated child session; agents
        spawned by pipeline steps carry that child session as their parent.
        The heartbeat's agent-liveness check resolves agents through this
        column, so it must point at the executing session (the trigger
        session, when any, remains reachable as the child's parent).
        """
        project_clause, project_params = self._project_predicate()
        cursor = self.db.execute(
            # project_clause is generated by the storage manager.
            f"""
            UPDATE pipeline_executions
            SET session_id = %s, updated_at = %s
            WHERE id = %s AND {project_clause}
            """,  # nosec
            (session_id, utc_now(), execution_id, *project_params),
        )
        if cursor.rowcount == 0:
            message = f"Pipeline execution {execution_id} was not found in current project scope"
            logger.warning(message)
            raise PipelineExecutionNotFoundError(message)

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
        project_clause, project_params = self._project_predicate()
        params: list[Any] = [*project_params]
        where = f"WHERE {project_clause}"

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
            # where is assembled from fixed predicates above.
            f"SELECT * FROM pipeline_executions {where} "  # nosec
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

    def list_recovery_project_ids(
        self,
        *,
        limit: int = 100,
        after_project_id: str | None = None,
    ) -> list[str]:
        """List project IDs with recoverable executions, one bounded page at a time."""
        if limit <= 0:
            raise ValueError(f"limit must be > 0, got {limit}")

        after_clause = " AND project_id > %s" if after_project_id is not None else ""
        params: list[Any] = [
            ExecutionStatus.PENDING.value,
            ExecutionStatus.RUNNING.value,
            ExecutionStatus.INTERRUPTED.value,
        ]
        if after_project_id is not None:
            params.append(after_project_id)
        params.append(limit)
        rows = self.db.fetchall(
            # after_clause is either empty or a fixed parameterized predicate.
            f"""
            SELECT DISTINCT project_id
            FROM pipeline_executions
            WHERE status IN (%s, %s, %s)
            {after_clause}
            ORDER BY project_id
            LIMIT %s
            """,  # nosec
            tuple(params),
        )
        return [str(row["project_id"]) for row in rows]

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
        # where is assembled from fixed predicates above.
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
        """  # nosec
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
            # where is assembled from fixed predicates above.
            f"SELECT status, COUNT(*) AS cnt FROM pipeline_executions {where} "  # nosec
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
        project_predicate, project_params = self._project_predicate()
        params: list[Any] = list(project_params)
        query = f"SELECT * FROM pipeline_executions WHERE {project_predicate}"  # nosec B608

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
        now = utc_now()
        project_clause, project_params = self._project_predicate()
        # project_clause is generated internally by _project_predicate, not caller input.
        query = (
            "UPDATE pipeline_executions SET review_json = %s, updated_at = %s "
            f"WHERE id = %s AND {project_clause}"  # nosec B608
        )
        self.db.execute(
            query,
            (review_json, now, execution_id, *project_params),
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
        project_clause, project_params = self._project_predicate("pe.project_id")
        like_clause, status_clause, filter_params = _execution_search_filter(
            query,
            search_errors=search_errors,
            search_outputs=search_outputs,
            status=status,
        )
        params: list[Any] = [*project_params, *filter_params]

        params.extend([limit, offset])

        # Clauses are assembled from fixed predicates and parameter placeholders.
        sql = f"""
            SELECT DISTINCT pe.* FROM pipeline_executions pe
            LEFT JOIN step_executions se ON se.execution_id = pe.id
            WHERE {project_clause}
              AND ({like_clause}){status_clause}
            ORDER BY pe.created_at DESC
            LIMIT %s OFFSET %s
        """  # nosec

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
        project_clause, project_params = self._project_predicate("pe.project_id")
        like_clause, status_clause, filter_params = _execution_search_filter(
            query,
            search_errors=search_errors,
            search_outputs=search_outputs,
            status=status,
        )
        params: list[Any] = [*project_params, *filter_params]

        # Clauses are assembled from fixed predicates and parameter placeholders.
        sql = f"""
            SELECT COUNT(*) AS cnt FROM (
                SELECT DISTINCT pe.id FROM pipeline_executions pe
                LEFT JOIN step_executions se ON se.execution_id = pe.id
                WHERE {project_clause}
                  AND ({like_clause}){status_clause}
            ) AS matching_executions
        """  # nosec
        row = self.db.fetchone(sql, tuple(params))
        return int(row["cnt"]) if row else 0

    def get_execution_by_resume_token(self, token: str) -> PipelineExecution | None:
        """Get execution by resume token.

        Args:
            token: Resume token

        Returns:
            PipelineExecution or None if not found
        """
        project_clause, project_params = self._project_predicate()
        row = self.db.fetchone(
            f"SELECT * FROM pipeline_executions WHERE resume_token = %s AND {project_clause}",  # nosec B608
            (token, *project_params),
        )
        return PipelineExecution.from_row(row) if row else None

    def resolve_execution_reference(self, ref: str) -> str:
        """Resolve an execution reference to a UUID.

        Supports:
        - Full UUID
        - UUID prefix (matches by prefix)

        Args:
            ref: Execution reference

        Returns:
            Execution UUID

        Raises:
            ValueError: If reference cannot be resolved
        """
        # Try exact match first; short prefixes are not valid uuid literals and
        # would error against the uuid PK column.
        if is_full_uuid(ref):
            execution = self.get_execution(ref)
            if execution:
                return execution.id

        # Try prefix match.
        escaped_ref = ref.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        project_clause, project_params = self._project_predicate()
        rows = self.db.fetchall(
            # project_clause is generated by the storage manager.
            f"""
            SELECT id FROM pipeline_executions
            WHERE id::text LIKE %s ESCAPE '\\' AND {project_clause}
            ORDER BY id ASC
            LIMIT 2
            """,  # nosec
            (f"{escaped_ref}%", *project_params),
        )
        if len(rows) > 1:
            raise ValueError(f"Execution reference is ambiguous: {ref}")
        if rows:
            result: str = rows[0]["id"]
            return result

        raise ValueError(f"Cannot resolve execution reference: {ref}")

    def interrupt_stale_running_executions(
        self,
        exclude_ids: set[str] | None = None,
        pending_stall_threshold_seconds: int = 120,
    ) -> int:
        """Mark running and stale pending executions as interrupted.

        Called during daemon startup to recover from unclean shutdowns.
        Uses INTERRUPTED status (non-terminal) instead of FAILED so pipelines
        with resume_on_restart=true can be re-queued.
        Leaves waiting_approval executions alone (they can still be approved).

        Args:
            exclude_ids: Execution IDs to skip (e.g. resumable pipelines).
            pending_stall_threshold_seconds: Pending inactivity required before recovery.

        Returns:
            Number of executions marked as interrupted.
        """
        return self._mark_stale_running_executions(
            exclude_ids=exclude_ids,
            status=ExecutionStatus.INTERRUPTED,
            pending_stall_threshold_seconds=pending_stall_threshold_seconds,
        )

    def fail_stale_running_executions(
        self,
        exclude_ids: set[str] | None = None,
        pending_stall_threshold_seconds: int = 120,
    ) -> int:
        """Mark running and stale pending executions as FAILED (terminal).

        Used by the executor startup sweep (#17756): a freshly created
        per-project executor owns no background tasks, so RUNNING executions
        in its scope are restart orphans that nothing will resume. The
        daemon-startup recovery flow keeps interrupt_stale_running_executions
        instead, so resume_on_restart pipelines stay re-queueable.
        Leaves waiting_approval executions alone (they can still be approved).

        Args:
            exclude_ids: Execution IDs to skip (e.g. live detached runs).
            pending_stall_threshold_seconds: Pending inactivity required before recovery.

        Returns:
            Number of executions marked as failed.
        """
        return self._mark_stale_running_executions(
            exclude_ids=exclude_ids,
            status=ExecutionStatus.FAILED,
            pending_stall_threshold_seconds=pending_stall_threshold_seconds,
        )

    def _mark_stale_running_executions(
        self,
        exclude_ids: set[str] | None,
        *,
        status: ExecutionStatus,
        pending_stall_threshold_seconds: int,
    ) -> int:
        """Move RUNNING and stale PENDING executions to *status*."""
        now = utc_now()
        pending_cutoff = now - timedelta(seconds=pending_stall_threshold_seconds)

        def build_not_in_clause(
            ids: set[str] | None, column_name: str
        ) -> tuple[str, tuple[str, ...]]:
            if not ids:
                return "", ()
            ordered_ids = tuple(sorted(ids))
            placeholders = ", ".join("%s" for _ in ordered_ids)
            return f" AND {column_name} NOT IN ({placeholders})", ordered_ids

        # Build exclusion clause for parameter binding
        exclude_clause, exclude_params = build_not_in_clause(exclude_ids, "execution_id")
        exec_exclude_clause, exec_exclude_params = build_not_in_clause(exclude_ids, "id")
        project_clause, project_params = self._project_predicate()

        with self.db.transaction() as conn:
            # Fail running step executions that belong to running pipeline executions.
            conn.execute(
                # Both clauses contain generated predicates/placeholders only.
                f"""
                UPDATE step_executions
                SET status = %s, error = 'Daemon restarted', completed_at = %s
                WHERE status = %s
                  AND execution_id IN (
                      SELECT id FROM pipeline_executions
                      WHERE status = %s AND {project_clause}
                  ){exclude_clause}
                """,  # nosec
                (
                    StepStatus.FAILED.value,
                    now,
                    StepStatus.RUNNING.value,
                    ExecutionStatus.RUNNING.value,
                    *project_params,
                    *exclude_params,
                ),
            )

            # Move running and stale pending executions to the requested recovery status.
            cursor = conn.execute(
                # Both clauses contain generated predicates/placeholders only.
                f"""
                UPDATE pipeline_executions
                SET status = %s, outputs_json = %s, updated_at = %s
                WHERE (
                    status = %s
                    OR (status = %s AND updated_at < %s)
                )
                  AND {project_clause}{exec_exclude_clause}
                RETURNING id
                """,  # nosec
                (
                    status.value,
                    '{"error": "Daemon restarted before or during pipeline execution"}',
                    now,
                    ExecutionStatus.RUNNING.value,
                    ExecutionStatus.PENDING.value,
                    pending_cutoff,
                    *project_params,
                    *exec_exclude_params,
                ),
            )
            recovered_ids = [str(row["id"]) for row in cursor.fetchall()]
            if recovered_ids:
                placeholders = ", ".join("%s" for _ in recovered_ids)
                conn.execute(
                    # placeholders are generated from the recovered row count.
                    f"DELETE FROM task_dispatch_mutex WHERE run_id IN ({placeholders})",  # nosec
                    tuple(recovered_ids),
                )

        count = len(recovered_ids)
        if count > 0:
            logger.info(
                "Marked %s stale pipeline executions as %s after restart", count, status.value
            )
        return count

    def count_by_status(self) -> dict[str, int]:
        """Count executions grouped by status.

        Returns:
            Dict mapping status values to their counts.
        """
        project_clause, project_params = self._project_predicate()
        rows = self.db.fetchall(
            # project_clause is generated by the storage manager.
            f"SELECT status, COUNT(*) as cnt FROM pipeline_executions WHERE {project_clause} GROUP BY status",  # nosec
            project_params,
        )
        return {row["status"]: row["cnt"] for row in rows}

    def get_stalled_executions(self, stall_threshold_seconds: int) -> list[PipelineExecution]:
        """Get active executions that haven't been updated within the threshold.

        Args:
            stall_threshold_seconds: Seconds of inactivity before considering stalled

        Returns:
            List of stalled PipelineExecution instances
        """
        cutoff = utc_now() - timedelta(seconds=stall_threshold_seconds)

        project_clause, project_params = self._project_predicate()
        rows = self.db.fetchall(
            # project_clause is generated by the storage manager.
            f"""
            SELECT * FROM pipeline_executions
            WHERE status IN (%s, %s)
              AND {project_clause}
              AND updated_at < %s
            ORDER BY updated_at ASC
            """,  # nosec
            (
                ExecutionStatus.PENDING.value,
                ExecutionStatus.RUNNING.value,
                *project_params,
                cutoff,
            ),
        )
        return [PipelineExecution.from_row(row) for row in rows]
