"""Local pipeline execution storage manager."""

from __future__ import annotations

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipeline_executions import PipelineExecutionStorageMixin
from gobby.storage.pipeline_steps import PipelineStepStorageMixin
from gobby.storage.pipeline_subscribers import PipelineCompletionSubscriberMixin
from gobby.workflows.pipeline_state import ExecutionStatus, StepStatus

__all__ = ["LocalPipelineExecutionManager"]


class LocalPipelineExecutionManager(
    PipelineExecutionStorageMixin,
    PipelineStepStorageMixin,
    PipelineCompletionSubscriberMixin,
):
    """Manager for local pipeline execution storage."""

    db: HubDatabase
    project_id: str | None

    def __init__(self, db: HubDatabase, project_id: str | None) -> None:
        """Initialize with database connection and project context.

        Args:
            db: Database connection
            project_id: Project ID for scoped access; empty or ``None`` selects all projects
        """
        self.db = db
        self.project_id = project_id or None

    def expire_approval_timeout(self, *, step_execution_id: int, execution_id: str) -> None:
        """Fail an expired approval step and cancel its execution atomically."""
        with self.db.transaction():
            step = self.update_step_execution(
                step_execution_id=step_execution_id,
                status=StepStatus.FAILED,
                error="Approval timed out",
            )
            if step is None:
                raise ValueError(f"Step execution {step_execution_id} not found")

            execution = self.update_execution_status(
                execution_id=execution_id,
                status=ExecutionStatus.CANCELLED,
            )
            if execution is None:
                raise ValueError(f"Pipeline execution {execution_id} not found")
