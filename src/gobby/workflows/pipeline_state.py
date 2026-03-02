"""Pipeline execution state models.

This module defines the runtime state models for pipeline executions,
including execution status tracking, step execution records, and the
ApprovalRequired exception for approval gates.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ExecutionStatus(Enum):
    """Status values for pipeline executions."""

    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(Enum):
    """Status values for individual step executions."""

    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class PipelineExecution:
    """Execution state for a pipeline.

    Tracks the overall execution of a pipeline including inputs, outputs,
    status, and optional resume token for approval gates.
    """

    id: str  # Format: pe-{12hex}
    pipeline_name: str
    project_id: str
    status: ExecutionStatus
    created_at: str
    updated_at: str
    inputs_json: str | None = None
    outputs_json: str | None = None
    completed_at: str | None = None
    resume_token: str | None = None  # Token for resuming after approval
    session_id: str | None = None  # Session that triggered execution
    parent_execution_id: str | None = None  # For nested pipeline invocations

    @classmethod
    def from_row(cls, row: Any) -> PipelineExecution:
        """Create PipelineExecution from database row."""
        return cls(
            id=row["id"],
            pipeline_name=row["pipeline_name"],
            project_id=row["project_id"],
            status=ExecutionStatus(row["status"]),
            inputs_json=row["inputs_json"],
            outputs_json=row["outputs_json"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
            resume_token=row["resume_token"],
            session_id=row["session_id"],
            parent_execution_id=row["parent_execution_id"],
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            "id": self.id,
            "pipeline_name": self.pipeline_name,
            "project_id": self.project_id,
            "status": self.status.value,
            "inputs_json": self.inputs_json,
            "outputs_json": self.outputs_json,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "resume_token": self.resume_token,
            "session_id": self.session_id,
            "parent_execution_id": self.parent_execution_id,
        }


@dataclass
class StepExecution:
    """Execution state for a single pipeline step.

    Tracks individual step execution including input/output, timing,
    errors, and approval state.
    """

    id: int  # Auto-increment integer
    execution_id: str  # Parent pipeline execution ID
    step_id: str  # Step ID from pipeline definition
    status: StepStatus
    started_at: str | None = None
    completed_at: str | None = None
    input_json: str | None = None
    output_json: str | None = None
    error: str | None = None
    approval_token: str | None = None  # Unique token for this step's approval
    approved_by: str | None = None  # Who approved (email, user ID, etc.)
    approved_at: str | None = None
    approval_timeout_seconds: int | None = None  # Timeout for approval gate

    @classmethod
    def from_row(cls, row: Any) -> StepExecution:
        """Create StepExecution from database row."""
        return cls(
            id=row["id"],
            execution_id=row["execution_id"],
            step_id=row["step_id"],
            status=StepStatus(row["status"]),
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            input_json=row["input_json"],
            output_json=row["output_json"],
            error=row["error"],
            approval_token=row["approval_token"],
            approved_by=row["approved_by"],
            approved_at=row["approved_at"],
            approval_timeout_seconds=row["approval_timeout_seconds"]
            if "approval_timeout_seconds" in row.keys()
            else None,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            "id": self.id,
            "execution_id": self.execution_id,
            "step_id": self.step_id,
            "status": self.status.value,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "input_json": self.input_json,
            "output_json": self.output_json,
            "error": self.error,
            "approval_token": self.approval_token,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "approval_timeout_seconds": self.approval_timeout_seconds,
        }


class ApprovalRequired(Exception):
    """Exception raised when a pipeline step requires approval.

    This exception is raised during pipeline execution when a step
    has an approval gate. The execution pauses and waits for external
    approval via the resume token.
    """

    def __init__(
        self,
        execution_id: str,
        step_id: str,
        token: str,
        message: str,
    ) -> None:
        self.execution_id = execution_id
        self.step_id = step_id
        self.token = token
        self.message = message
        super().__init__(
            f"Approval required for step '{step_id}' in execution '{execution_id}': {message}"
        )
