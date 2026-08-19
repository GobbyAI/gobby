"""Tests for pipeline resume functionality."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.workflows.definitions import PipelineApproval, PipelineDefinition, PipelineStep
from gobby.workflows.pipeline_executor import PipelineExecutor
from gobby.workflows.pipeline_state import ApprovalRequired, ExecutionStatus, StepStatus

pytestmark = [pytest.mark.unit, pytest.mark.no_config_protection]


@pytest.fixture
def mock_db():
    return MagicMock()


@pytest.fixture
def mock_execution_manager():
    manager = MagicMock()
    # Default execution
    mock_execution = MagicMock()
    mock_execution.id = "pe-test-123"
    mock_execution.status = ExecutionStatus.PENDING
    mock_execution.inputs_json = "{}"
    manager.create_execution.return_value = mock_execution
    manager.get_execution.return_value = mock_execution
    manager.update_execution_status.return_value = mock_execution

    # Default step
    mock_step = MagicMock()
    mock_step.id = 1
    mock_step.status = StepStatus.PENDING
    manager.create_step_execution.return_value = mock_step
    manager.update_step_execution.return_value = mock_step

    # Mock get_steps_for_execution to return empty list by default
    manager.get_steps_for_execution.return_value = []

    return manager


@pytest.fixture
def mock_llm_service():
    return AsyncMock()


@pytest.fixture
def mock_loader():
    loader = MagicMock()
    loader.load_pipeline = AsyncMock()
    return loader


class TestPipelineResume:
    """Tests for resuming pipeline execution."""

    @pytest.mark.asyncio
    async def test_approve_resumes_execution_and_runs_next_step(
        self, mock_db, mock_execution_manager, mock_llm_service, mock_loader
    ) -> None:
        """Test that approve() resumes pipeline execution and runs subsequent steps."""

        # 1. Setup Pipeline with 2 steps: 1st needs approval, 2nd is simple exec
        pipeline = PipelineDefinition(
            name="resume-pipeline",
            steps=[
                PipelineStep(
                    id="step1", exec="echo step1", approval=PipelineApproval(required=True)
                ),
                PipelineStep(id="step2", exec="echo step2"),
            ],
        )
        mock_loader.load_pipeline.return_value = pipeline

        # 2. Setup state for approve() call
        # The step waiting for approval
        waiting_step = MagicMock()
        waiting_step.id = 101
        waiting_step.execution_id = "pe-resume-123"
        waiting_step.step_id = "step1"
        waiting_step.approval_token = "valid-token"
        waiting_step.status = StepStatus.WAITING_APPROVAL

        mock_execution_manager.consume_step_approval.return_value = waiting_step

        # The execution record
        execution = MagicMock()
        execution.id = "pe-resume-123"
        execution.pipeline_name = "resume-pipeline"
        execution.status = ExecutionStatus.WAITING_APPROVAL
        execution.inputs_json = json.dumps({"env": "prod"})
        execution.project_id = "test-project"

        mock_execution_manager.get_execution.return_value = execution
        mock_execution_manager.get_failed_steps.return_value = []

        # Create the approved pending step object that execute() will run on resume.
        approved_step1 = MagicMock()
        approved_step1.id = 101
        approved_step1.step_id = "step1"
        approved_step1.status = StepStatus.PENDING
        approved_step1.approved_at = "2026-07-15T00:00:00Z"
        approved_step1.output_json = None

        # Configure the mock to return this step when execute() checks for existing steps
        mock_execution_manager.get_steps_for_execution.return_value = [approved_step1]

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            loader=mock_loader,
        )

        # 3. Call approve()
        await executor.approve("valid-token")

        # 4. Assertions
        mock_loader.load_pipeline.assert_awaited_once_with("resume-pipeline", "test-project")

        # Verify step1's token was atomically consumed while marking it approved.
        mock_execution_manager.consume_step_approval.assert_called_once_with(
            "valid-token",
            status=StepStatus.PENDING,
            approved_by=None,
        )

        # Verify step2 was executed (create_step_execution called for step2)
        calls = mock_execution_manager.create_step_execution.call_args_list
        step2_calls = [c for c in calls if c.kwargs.get("step_id") == "step2"]

        assert len(step2_calls) > 0, "Pipeline execution did not resume to step2 after approval"

    @pytest.mark.integration
    async def test_approve_runs_gated_action_and_output_conditioned_step(
        self,
        temp_db: HubDatabase,
        sample_project: dict[str, object],
        mock_llm_service: AsyncMock,
        mock_loader: MagicMock,
    ) -> None:
        project_id = str(sample_project["id"])
        pipeline = PipelineDefinition(
            name="approval-action-pipeline",
            steps=[
                PipelineStep(
                    id="gated",
                    exec="printf approved-output",
                    approval=PipelineApproval(required=True),
                ),
                PipelineStep(
                    id="downstream",
                    exec="printf downstream-output",
                    condition=("${{ steps['gated']['output']['stdout'] == 'approved-output' }}"),
                ),
            ],
        )
        mock_loader.load_pipeline.return_value = pipeline
        manager = LocalPipelineExecutionManager(temp_db, project_id=project_id)
        executor = PipelineExecutor(
            db=temp_db,
            execution_manager=manager,
            llm_service=mock_llm_service,
            loader=mock_loader,
        )

        with pytest.raises(ApprovalRequired) as exc_info:
            await executor.execute(pipeline=pipeline, inputs={}, project_id=project_id)

        completed = await executor.approve(exc_info.value.token, approved_by="reviewer")
        steps = {step.step_id: step for step in manager.get_steps_for_execution(completed.id)}

        assert completed.status == ExecutionStatus.COMPLETED
        assert steps["gated"].status == StepStatus.COMPLETED
        assert steps["gated"].approved_by == "reviewer"
        assert steps["gated"].approved_at is not None
        assert json.loads(steps["gated"].output_json or "null")["stdout"] == "approved-output"
        assert steps["downstream"].status == StepStatus.COMPLETED
        assert json.loads(steps["downstream"].output_json or "null")["stdout"] == (
            "downstream-output"
        )

    async def test_approve_disabled_pipeline_cancels_execution(
        self, mock_db, mock_execution_manager, mock_llm_service, mock_loader
    ) -> None:
        pipeline = PipelineDefinition(
            name="disabled-resume-pipeline",
            enabled=False,
            steps=[
                PipelineStep(
                    id="step1",
                    exec="echo step1",
                    approval=PipelineApproval(required=True),
                )
            ],
        )
        mock_loader.load_pipeline.return_value = pipeline

        waiting_step = MagicMock(
            id=101,
            execution_id="pe-disabled-123",
            step_id="step1",
            approval_token="disabled-token",
            status=StepStatus.WAITING_APPROVAL,
        )
        mock_execution_manager.get_step_by_approval_token.return_value = waiting_step

        execution = MagicMock(
            id="pe-disabled-123",
            pipeline_name=pipeline.name,
            status=ExecutionStatus.WAITING_APPROVAL,
            project_id="test-project",
        )
        mock_execution_manager.get_execution.return_value = execution

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            loader=mock_loader,
        )

        with pytest.raises(ValueError, match="disabled"):
            await executor.approve("disabled-token")

        mock_execution_manager.update_execution_status.assert_called_once_with(
            execution_id=execution.id,
            status=ExecutionStatus.CANCELLED,
        )
        mock_execution_manager.create_step_execution.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancelled_execution_rejects_resume(
        self, mock_db, mock_execution_manager, mock_llm_service, mock_loader
    ) -> None:
        """Cancelled executions are terminal and cannot be resumed."""
        pipeline = PipelineDefinition(
            name="spawn-pipeline",
            steps=[PipelineStep(id="spawn_agent", exec="echo spawn")],
        )

        execution = MagicMock()
        execution.id = "pe-cancelled-123"
        execution.status = ExecutionStatus.CANCELLED

        mock_execution_manager.get_execution.return_value = execution

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            loader=mock_loader,
        )

        with pytest.raises(ValueError, match="terminal"):
            await executor.execute(
                pipeline=pipeline,
                inputs={},
                project_id="test-project",
                execution_id="pe-cancelled-123",
            )

    @pytest.mark.asyncio
    async def test_completed_execution_rejects_resume(
        self, mock_db, mock_execution_manager, mock_llm_service, mock_loader
    ) -> None:
        """Completed executions are terminal and cannot be resumed."""
        pipeline = PipelineDefinition(
            name="spawn-pipeline",
            steps=[PipelineStep(id="spawn_agent", exec="echo spawn")],
        )

        execution = MagicMock()
        execution.id = "pe-completed-123"
        execution.status = ExecutionStatus.COMPLETED

        mock_execution_manager.get_execution.return_value = execution

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            loader=mock_loader,
        )

        with pytest.raises(ValueError, match="terminal"):
            await executor.execute(
                pipeline=pipeline,
                inputs={},
                project_id="test-project",
                execution_id="pe-completed-123",
            )

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_failed_execution_reexecutes_all_steps(
        self,
        temp_db: HubDatabase,
        sample_project: dict[str, object],
        mock_llm_service,
        mock_loader,
    ) -> None:
        """Failed executions reset and reuse real step rows before re-executing."""
        pipeline = PipelineDefinition(
            name="spawn-pipeline",
            steps=[PipelineStep(id="spawn_agent", exec="echo spawn")],
        )
        project_id = str(sample_project["id"])
        execution_manager = LocalPipelineExecutionManager(temp_db, project_id=project_id)
        execution = execution_manager.create_execution(pipeline_name=pipeline.name)
        execution_manager.update_execution_status(
            execution.id,
            status=ExecutionStatus.FAILED,
            outputs_json=json.dumps({"error": "original failure"}),
        )
        stale_step = execution_manager.create_step_execution(
            execution_id=execution.id,
            step_id="spawn_agent",
        )
        execution_manager.update_step_execution(
            stale_step.id,
            status=StepStatus.COMPLETED,
            output_json=json.dumps({"session_id": "stale"}),
        )

        executor = PipelineExecutor(
            db=temp_db,
            execution_manager=execution_manager,
            llm_service=mock_llm_service,
            loader=mock_loader,
        )
        executor._execute_step = AsyncMock(return_value={"session_id": "fresh"})

        resumed = await executor.execute(
            pipeline=pipeline,
            inputs={},
            project_id=project_id,
            execution_id=execution.id,
        )

        steps = execution_manager.get_steps_for_execution(execution.id)
        assert resumed.status == ExecutionStatus.COMPLETED
        assert len(steps) == 1
        assert steps[0].id == stale_step.id
        assert steps[0].status == StepStatus.COMPLETED
        assert json.loads(steps[0].output_json or "null") == {"session_id": "fresh"}
        executor._execute_step.assert_awaited_once()
