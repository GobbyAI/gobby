"""Pipeline step execution persistence methods."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sql_dialect import timestamp_plus_seconds_before_now_expr
from gobby.workflows.pipeline_state import StepExecution, StepStatus


class PipelineStepStorageMixin:
    """Pipeline step execution CRUD and query methods."""

    db: HubDatabase
    project_id: str | None

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
        row = self.db.fetchone(
            """
            INSERT INTO step_executions (
                execution_id, step_id, status, input_json
            )
            VALUES (%s, %s, %s, %s)
            RETURNING *
            """,
            (
                execution_id,
                step_id,
                StepStatus.PENDING.value,
                input_json,
            ),
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
            updates.append("status = %s")
            params.append(status.value)
            # Set timestamps based on status
            if status == StepStatus.RUNNING:
                updates.append("started_at = COALESCE(started_at, %s)")
                params.append(now)
            elif status in (StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.SKIPPED):
                updates.append("completed_at = COALESCE(completed_at, %s)")
                params.append(now)

        if output_json is not None:
            updates.append("output_json = %s")
            params.append(output_json)

        if error is not None:
            updates.append("error = %s")
            params.append(error)

        if approval_token is not None:
            updates.append("approval_token = %s")
            params.append(approval_token)

        if approved_by is not None:
            updates.append("approved_by = %s")
            params.append(approved_by)
            updates.append("approved_at = %s")
            params.append(now)

        if approval_timeout_seconds is not None:
            updates.append("approval_timeout_seconds = %s")
            params.append(approval_timeout_seconds)

        if not updates:
            # Nothing to update
            row = self.db.fetchone(
                "SELECT * FROM step_executions WHERE id = %s",
                (step_execution_id,),
            )
            return StepExecution.from_row(row) if row else None

        # Append step_execution_id for WHERE clause
        params.append(step_execution_id)

        # updates list contains only hardcoded column names, values are parameterized
        self.db.execute(
            f"UPDATE step_executions SET {', '.join(updates)} WHERE id = %s",  # nosec B608
            tuple(params),
        )

        row = self.db.fetchone(
            "SELECT * FROM step_executions WHERE id = %s",
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
            "SELECT * FROM step_executions WHERE approval_token = %s",
            (token,),
        )
        return StepExecution.from_row(row) if row else None

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
        project_predicate = (
            "pe.project_id IS NULL" if self.project_id is None else "pe.project_id = %s"
        )
        params: tuple[Any, ...] = (
            (StepStatus.WAITING_APPROVAL.value,)
            if self.project_id is None
            else (StepStatus.WAITING_APPROVAL.value, self.project_id)
        )
        rows = self.db.fetchall(
            f"""
SELECT se.* FROM step_executions se
JOIN pipeline_executions pe ON se.execution_id = pe.id
WHERE se.status = %s
AND se.approval_timeout_seconds IS NOT NULL
AND se.started_at IS NOT NULL
AND {timeout_expired_sql}
AND {project_predicate}
""",  # nosec B608 # timeout expression is selected by storage dialect.
            params,
        )
        return [StepExecution.from_row(row) for row in rows]

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
                        SET status = %s, output_json = NULL, error = NULL,
                            started_at = NULL, completed_at = NULL,
                            approval_token = NULL, approved_by = NULL, approved_at = NULL
                        WHERE id = %s
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
            "SELECT * FROM step_executions WHERE execution_id = %s AND status = %s",
            (execution_id, StepStatus.FAILED.value),
        )
        return [StepExecution.from_row(row) for row in rows]

    def get_steps_for_executions(self, execution_ids: list[str]) -> dict[str, list[StepExecution]]:
        """Batch-load steps for multiple executions.

        Args:
            execution_ids: List of pipeline execution IDs.

        Returns:
            Dict mapping execution_id to list of StepExecution instances.
        """
        if not execution_ids:
            return {}
        placeholders = ", ".join("%s" for _ in execution_ids)
        rows = self.db.fetchall(
            f"SELECT * FROM step_executions WHERE execution_id IN ({placeholders}) ORDER BY id",  # nosec B608
            tuple(execution_ids),
        )
        result: dict[str, list[StepExecution]] = {eid: [] for eid in execution_ids}
        for row in rows:
            step = StepExecution.from_row(row)
            result[step.execution_id].append(step)
        return result

    def get_steps_for_execution(self, execution_id: str) -> list[StepExecution]:
        """Get all steps for an execution.

        Args:
            execution_id: Pipeline execution ID

        Returns:
            List of StepExecution instances
        """
        rows = self.db.fetchall(
            "SELECT * FROM step_executions WHERE execution_id = %s ORDER BY id",
            (execution_id,),
        )
        return [StepExecution.from_row(row) for row in rows]
