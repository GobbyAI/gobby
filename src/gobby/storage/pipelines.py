"""Local pipeline execution storage manager."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from gobby.storage.database import DatabaseProtocol
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

    def __init__(self, db: DatabaseProtocol, project_id: str):
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
    ) -> PipelineExecution:
        """Create a new pipeline execution.

        Args:
            pipeline_name: Name of the pipeline being executed
            inputs_json: JSON string of input parameters
            session_id: Session that triggered the execution
            parent_execution_id: Parent execution for nested pipelines

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
                    session_id, parent_execution_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution_id,
                    pipeline_name,
                    self.project_id,
                    ExecutionStatus.PENDING.value,
                    inputs_json,
                    session_id,
                    parent_execution_id,
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
            now if status in (ExecutionStatus.COMPLETED, ExecutionStatus.FAILED) else None
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

    def list_executions(
        self,
        status: ExecutionStatus | None = None,
        pipeline_name: str | None = None,
        limit: int = 50,
    ) -> list[PipelineExecution]:
        """List executions for the project.

        Args:
            status: Filter by status
            pipeline_name: Filter by pipeline name
            limit: Maximum number of results

        Returns:
            List of PipelineExecution instances
        """
        query = "SELECT * FROM pipeline_executions WHERE project_id = ?"
        params: list[Any] = [self.project_id]

        if status is not None:
            query += " AND status = ?"
            params.append(status.value)

        if pipeline_name is not None:
            query += " AND pipeline_name = ?"
            params.append(pipeline_name)

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        rows = self.db.fetchall(query, tuple(params))
        return [PipelineExecution.from_row(row) for row in rows]

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

    def fail_stale_running_executions(self) -> int:
        """Mark running executions and their steps as failed.

        Called during daemon startup to recover from unclean shutdowns.
        Leaves waiting_approval executions alone (they can still be approved).

        Returns:
            Number of executions marked as failed.
        """
        now = datetime.now(UTC).isoformat()

        # Fail running step executions that belong to running pipeline executions
        self.db.execute(
            """
            UPDATE step_executions
            SET status = ?, error = 'Daemon restarted', completed_at = ?
            WHERE status = ?
              AND execution_id IN (
                  SELECT id FROM pipeline_executions
                  WHERE status = ? AND project_id = ?
              )
            """,
            (
                StepStatus.FAILED.value,
                now,
                StepStatus.RUNNING.value,
                ExecutionStatus.RUNNING.value,
                self.project_id,
            ),
        )

        # Fail running pipeline executions
        cursor = self.db.execute(
            """
            UPDATE pipeline_executions
            SET status = ?, outputs_json = ?, completed_at = ?, updated_at = ?
            WHERE status = ? AND project_id = ?
            """,
            (
                ExecutionStatus.FAILED.value,
                '{"error": "Daemon restarted while execution was in progress"}',
                now,
                now,
                ExecutionStatus.RUNNING.value,
                self.project_id,
            ),
        )

        count: int = cursor.rowcount if cursor else 0
        if count > 0:
            logger.info(f"Marked {count} stale running executions as failed after restart")
        return count

    def get_expired_approval_steps(self) -> list[StepExecution]:
        """Get step executions where approval has timed out.

        Finds steps that are waiting_approval with a configured timeout
        where started_at + timeout_seconds < now.

        Returns:
            List of expired StepExecution instances.
        """
        rows = self.db.fetchall(
            """
            SELECT se.* FROM step_executions se
            JOIN pipeline_executions pe ON se.execution_id = pe.id
            WHERE se.status = ?
              AND se.approval_timeout_seconds IS NOT NULL
              AND se.started_at IS NOT NULL
              AND datetime(se.started_at, '+' || se.approval_timeout_seconds || ' seconds') < datetime('now')
              AND pe.project_id = ?
            """,
            (StepStatus.WAITING_APPROVAL.value, self.project_id),
        )
        return [StepExecution.from_row(row) for row in rows]

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
