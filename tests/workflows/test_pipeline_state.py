"""Tests for Pipeline execution state models.

TDD tests for ExecutionStatus, PipelineExecution, StepExecution, and ApprovalRequired.
"""

from datetime import UTC, datetime

import pytest

from gobby.workflows.pipeline_state import (
    ApprovalRequired,
    ExecutionStatus,
    PipelineExecution,
    StepExecution,
    StepStatus,
)

pytestmark = pytest.mark.unit


class TestExecutionStatus:
    """Tests for ExecutionStatus enum."""

    def test_all_statuses_exist(self) -> None:
        """Test that all expected execution statuses exist."""
        assert ExecutionStatus.PENDING.value == "pending"
        assert ExecutionStatus.RUNNING.value == "running"
        assert ExecutionStatus.WAITING_APPROVAL.value == "waiting_approval"
        assert ExecutionStatus.COMPLETED.value == "completed"
        assert ExecutionStatus.FAILED.value == "failed"
        assert ExecutionStatus.CANCELLED.value == "cancelled"

    def test_status_from_string(self) -> None:
        """Test creating status from string value."""
        assert ExecutionStatus("pending") == ExecutionStatus.PENDING
        assert ExecutionStatus("running") == ExecutionStatus.RUNNING
        assert ExecutionStatus("waiting_approval") == ExecutionStatus.WAITING_APPROVAL
        assert ExecutionStatus("completed") == ExecutionStatus.COMPLETED
        assert ExecutionStatus("failed") == ExecutionStatus.FAILED
        assert ExecutionStatus("cancelled") == ExecutionStatus.CANCELLED


class TestStepStatus:
    """Tests for StepStatus enum."""

    def test_all_statuses_exist(self) -> None:
        """Test that all expected step statuses exist."""
        assert StepStatus.PENDING.value == "pending"
        assert StepStatus.RUNNING.value == "running"
        assert StepStatus.WAITING_APPROVAL.value == "waiting_approval"
        assert StepStatus.COMPLETED.value == "completed"
        assert StepStatus.FAILED.value == "failed"
        assert StepStatus.SKIPPED.value == "skipped"


class TestPipelineExecution:
    """Tests for PipelineExecution dataclass."""

    def test_create_minimal(self) -> None:
        """Test creating minimal pipeline execution."""
        execution = PipelineExecution(
            id="pe-abc123456789",
            pipeline_name="test-pipeline",
            project_id="proj-123",
            status=ExecutionStatus.PENDING,
            created_at="2026-02-01T12:00:00Z",
            updated_at="2026-02-01T12:00:00Z",
        )
        assert execution.id == "pe-abc123456789"
        assert execution.pipeline_name == "test-pipeline"
        assert execution.project_id == "proj-123"
        assert execution.status == ExecutionStatus.PENDING
        assert execution.inputs_json is None
        assert execution.outputs_json is None
        assert execution.resume_token is None
        assert execution.session_id is None
        assert execution.parent_execution_id is None

    def test_create_full(self) -> None:
        """Test creating pipeline execution with all fields."""
        execution = PipelineExecution(
            id="pe-abc123456789",
            pipeline_name="test-pipeline",
            project_id="proj-123",
            status=ExecutionStatus.WAITING_APPROVAL,
            inputs_json='{"files": ["a.py"]}',
            outputs_json='{"result": "success"}',
            created_at="2026-02-01T12:00:00Z",
            updated_at="2026-02-01T12:30:00Z",
            completed_at="2026-02-01T12:30:00Z",
            resume_token="token-xyz",
            session_id="sess-456",
            parent_execution_id="pe-parent123",
        )
        assert execution.inputs_json == '{"files": ["a.py"]}'
        assert execution.outputs_json == '{"result": "success"}'
        # Stored timestamps normalize to aware datetimes on the model.
        assert execution.completed_at == datetime(2026, 2, 1, 12, 30, tzinfo=UTC)
        assert execution.resume_token == "token-xyz"
        assert execution.session_id == "sess-456"
        assert execution.parent_execution_id == "pe-parent123"

    def test_from_row(self) -> None:
        """Test creating PipelineExecution from database row."""
        row = {
            "id": "pe-abc123456789",
            "pipeline_name": "code-review",
            "project_id": "proj-xyz",
            "status": "running",
            "inputs_json": '{"branch": "feature"}',
            "outputs_json": None,
            "created_at": "2026-02-01T10:00:00Z",
            "updated_at": "2026-02-01T10:05:00Z",
            "completed_at": None,
            "resume_token": None,
            "session_id": "sess-abc",
            "parent_execution_id": None,
        }
        execution = PipelineExecution.from_row(row)
        assert execution.id == "pe-abc123456789"
        assert execution.pipeline_name == "code-review"
        assert execution.project_id == "proj-xyz"
        assert execution.status == ExecutionStatus.RUNNING
        assert execution.inputs_json == '{"branch": "feature"}'
        assert execution.outputs_json is None
        assert execution.session_id == "sess-abc"

    def test_to_dict(self) -> None:
        """Test converting PipelineExecution to dictionary."""
        execution = PipelineExecution(
            id="pe-abc123456789",
            pipeline_name="test-pipeline",
            project_id="proj-123",
            status=ExecutionStatus.COMPLETED,
            inputs_json='{"input": 1}',
            outputs_json='{"output": 2}',
            created_at="2026-02-01T12:00:00Z",
            updated_at="2026-02-01T12:30:00Z",
            completed_at="2026-02-01T12:30:00Z",
            resume_token=None,
            session_id="sess-123",
            parent_execution_id=None,
        )
        d = execution.to_dict()
        assert d["id"] == "pe-abc123456789"
        assert d["pipeline_name"] == "test-pipeline"
        assert d["project_id"] == "proj-123"
        assert d["status"] == "completed"
        assert d["inputs_json"] == '{"input": 1}'
        assert d["outputs_json"] == '{"output": 2}'
        assert d["session_id"] == "sess-123"

    def test_all_status_values(self) -> None:
        """Test creating execution with each status value."""
        for status in ExecutionStatus:
            execution = PipelineExecution(
                id=f"pe-{status.value}",
                pipeline_name="test",
                project_id="proj",
                status=status,
                created_at="2026-02-01T12:00:00Z",
                updated_at="2026-02-01T12:00:00Z",
            )
            assert execution.status == status


class TestStepExecution:
    """Tests for StepExecution dataclass."""

    def test_create_minimal(self) -> None:
        """Test creating minimal step execution."""
        step = StepExecution(
            id=1,
            execution_id="pe-abc123",
            step_id="analyze",
            status=StepStatus.PENDING,
        )
        assert step.id == 1
        assert step.execution_id == "pe-abc123"
        assert step.step_id == "analyze"
        assert step.status == StepStatus.PENDING
        assert step.started_at is None
        assert step.completed_at is None
        assert step.input_json is None
        assert step.output_json is None
        assert step.error is None
        assert step.approval_token is None
        assert step.approved_by is None
        assert step.approved_at is None

    def test_create_full(self) -> None:
        """Test creating step execution with all fields."""
        step = StepExecution(
            id=42,
            execution_id="pe-abc123",
            step_id="deploy",
            status=StepStatus.COMPLETED,
            started_at="2026-02-01T12:00:00Z",
            completed_at="2026-02-01T12:05:00Z",
            input_json='{"target": "prod"}',
            output_json='{"deployed": true}',
            error=None,
            approval_token="approve-xyz",
            approved_by="user@example.com",
            approved_at="2026-02-01T12:02:00Z",
        )
        assert step.id == 42
        assert step.step_id == "deploy"
        assert step.status == StepStatus.COMPLETED
        assert step.input_json == '{"target": "prod"}'
        assert step.output_json == '{"deployed": true}'
        assert step.approval_token == "approve-xyz"
        assert step.approved_by == "user@example.com"

    def test_create_with_error(self) -> None:
        """Test creating step execution with error."""
        step = StepExecution(
            id=5,
            execution_id="pe-abc123",
            step_id="test",
            status=StepStatus.FAILED,
            started_at="2026-02-01T12:00:00Z",
            completed_at="2026-02-01T12:01:00Z",
            error="Command failed with exit code 1",
        )
        assert step.status == StepStatus.FAILED
        assert step.error == "Command failed with exit code 1"

    def test_from_row(self) -> None:
        """Test creating StepExecution from database row."""
        row = {
            "id": 10,
            "execution_id": "pe-xyz789",
            "step_id": "build",
            "status": "running",
            "started_at": "2026-02-01T14:00:00Z",
            "completed_at": None,
            "input_json": '{"config": "release"}',
            "output_json": None,
            "error": None,
            "approval_token": None,
            "approved_by": None,
            "approved_at": None,
        }
        step = StepExecution.from_row(row)
        assert step.id == 10
        assert step.execution_id == "pe-xyz789"
        assert step.step_id == "build"
        assert step.status == StepStatus.RUNNING
        assert step.started_at == datetime(2026, 2, 1, 14, 0, tzinfo=UTC)
        assert step.input_json == '{"config": "release"}'

    def test_to_dict(self) -> None:
        """Test converting StepExecution to dictionary."""
        step = StepExecution(
            id=7,
            execution_id="pe-abc123",
            step_id="verify",
            status=StepStatus.WAITING_APPROVAL,
            started_at="2026-02-01T12:00:00Z",
            completed_at=None,
            input_json=None,
            output_json=None,
            error=None,
            approval_token="token-123",
            approved_by=None,
            approved_at=None,
        )
        d = step.to_dict()
        assert d["id"] == 7
        assert d["execution_id"] == "pe-abc123"
        assert d["step_id"] == "verify"
        assert d["status"] == "waiting_approval"
        assert d["approval_token"] == "token-123"

    def test_all_status_values(self) -> None:
        """Test creating step with each status value."""
        for status in StepStatus:
            step = StepExecution(
                id=1,
                execution_id="pe-test",
                step_id="test",
                status=status,
            )
            assert step.status == status


class TestApprovalRequired:
    """Tests for ApprovalRequired exception."""

    def test_create_exception(self) -> None:
        """Test creating ApprovalRequired exception."""
        exc = ApprovalRequired(
            execution_id="pe-abc123",
            step_id="review",
            token="token-xyz789",
            message="Please review the changes before proceeding.",
        )
        assert exc.execution_id == "pe-abc123"
        assert exc.step_id == "review"
        assert exc.token == "token-xyz789"
        assert exc.message == "Please review the changes before proceeding."

    def test_exception_message(self) -> None:
        """Test that exception has readable string representation."""
        exc = ApprovalRequired(
            execution_id="pe-abc123",
            step_id="deploy",
            token="token-123",
            message="Approve production deployment?",
        )
        exc_str = str(exc)
        assert "pe-abc123" in exc_str or "deploy" in exc_str or "Approve" in exc_str

    def test_exception_is_raiseable(self) -> None:
        """Test that ApprovalRequired can be raised and caught."""
        exc = ApprovalRequired(
            execution_id="pe-test",
            step_id="gate",
            token="tok",
            message="Gate approval required",
        )
        with pytest.raises(ApprovalRequired) as exc_info:
            raise exc
        caught = exc_info.value
        assert caught.execution_id == "pe-test"
        assert caught.step_id == "gate"
        assert caught.token == "tok"
        assert caught.message == "Gate approval required"

    def test_exception_attributes_accessible(self) -> None:
        """Test that all attributes are accessible after catching."""
        try:
            raise ApprovalRequired(
                execution_id="pe-123",
                step_id="manual",
                token="approval-token",
                message="Manual approval needed",
            )
        except ApprovalRequired as e:
            assert e.execution_id == "pe-123"
            assert e.step_id == "manual"
            assert e.token == "approval-token"
            assert e.message == "Manual approval needed"
