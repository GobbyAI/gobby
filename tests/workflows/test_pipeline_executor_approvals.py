"""Tests for approval gate handling, approve(), and reject() methods.

Split from the test_pipeline_executor monolith (#12210).
"""

import asyncio
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.workflows.definitions import PipelineDefinition, PipelineStep
from gobby.workflows.pipeline.gatekeeper import ApprovalManager
from gobby.workflows.pipeline_state import ExecutionStatus, StepStatus

pytestmark = pytest.mark.unit


class TestApprovalGateHandling:
    """Tests for approval gate handling in PipelineExecutor."""

    @pytest.fixture
    def pipeline_with_approval(self) -> PipelineDefinition:
        """Create a pipeline with an approval gate step."""
        from gobby.workflows.definitions import PipelineApproval

        return PipelineDefinition(
            name="approval-pipeline",
            steps=[
                PipelineStep(id="build", exec="echo build"),
                PipelineStep(
                    id="deploy",
                    exec="echo deploy",
                    approval=PipelineApproval(
                        required=True,
                        message="Approve deployment to production?",
                    ),
                ),
            ],
        )

    @pytest.mark.asyncio
    async def test_approval_gate_raises_approval_required(
        self,
        mock_db: MagicMock,
        mock_execution_manager: MagicMock,
        mock_llm_service: MagicMock,
        pipeline_with_approval: PipelineDefinition,
    ) -> None:
        """Test that step with approval=required=True raises ApprovalRequired."""
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.pipeline_state import ApprovalRequired

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        with pytest.raises(ApprovalRequired) as exc_info:
            await executor.execute(
                pipeline=pipeline_with_approval,
                inputs={},
                project_id="proj-123",
            )

        assert exc_info.value.execution_id == "pe-test-123"
        assert exc_info.value.step_id == "deploy"
        assert exc_info.value.token is not None
        assert len(exc_info.value.token) > 0

    @pytest.mark.asyncio
    async def test_approval_gate_generates_unique_token(
        self,
        mock_db: MagicMock,
        mock_execution_manager: MagicMock,
        mock_llm_service: MagicMock,
        pipeline_with_approval: PipelineDefinition,
    ) -> None:
        """Test that approval gate generates a unique approval token."""
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.pipeline_state import ApprovalRequired

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        with pytest.raises(ApprovalRequired) as exc_info:
            await executor.execute(
                pipeline=pipeline_with_approval,
                inputs={},
                project_id="proj-123",
            )

        token = exc_info.value.token
        assert token is not None
        assert len(token) >= 16

    @pytest.mark.asyncio
    async def test_approval_gate_includes_message(
        self,
        mock_db: MagicMock,
        mock_execution_manager: MagicMock,
        mock_llm_service: MagicMock,
        pipeline_with_approval: PipelineDefinition,
    ) -> None:
        """Test that approval gate includes the approval message."""
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.pipeline_state import ApprovalRequired

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        with pytest.raises(ApprovalRequired) as exc_info:
            await executor.execute(
                pipeline=pipeline_with_approval,
                inputs={},
                project_id="proj-123",
            )

        assert exc_info.value.message == "Approve deployment to production?"

    @pytest.mark.asyncio
    async def test_approval_gate_stores_token_in_step_execution(
        self,
        mock_db: MagicMock,
        mock_execution_manager: MagicMock,
        mock_llm_service: MagicMock,
        pipeline_with_approval: PipelineDefinition,
    ) -> None:
        """Test that approval token is stored in step execution record."""
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.pipeline_state import ApprovalRequired

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        with pytest.raises(ApprovalRequired) as exc_info:
            await executor.execute(
                pipeline=pipeline_with_approval,
                inputs={},
                project_id="proj-123",
            )

        update_calls = mock_execution_manager.update_step_execution.call_args_list
        token_calls = [c for c in update_calls if c.kwargs.get("approval_token") is not None]
        assert len(token_calls) >= 1
        assert token_calls[-1].kwargs["approval_token"] == exc_info.value.token

    @pytest.mark.asyncio
    async def test_approval_gate_updates_execution_status_to_waiting(
        self,
        mock_db: MagicMock,
        mock_execution_manager: MagicMock,
        mock_llm_service: MagicMock,
        pipeline_with_approval: PipelineDefinition,
    ) -> None:
        """Test that execution status is set to WAITING_APPROVAL when hitting approval gate."""
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.pipeline_state import ApprovalRequired

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        with pytest.raises(ApprovalRequired):
            await executor.execute(
                pipeline=pipeline_with_approval,
                inputs={},
                project_id="proj-123",
            )

        status_calls = mock_execution_manager.update_execution_status.call_args_list
        waiting_calls = [
            c for c in status_calls if c.kwargs.get("status") == ExecutionStatus.WAITING_APPROVAL
        ]
        assert len(waiting_calls) >= 1

    @pytest.mark.asyncio
    async def test_approval_gate_updates_step_status_to_waiting(
        self,
        mock_db: MagicMock,
        mock_execution_manager: MagicMock,
        mock_llm_service: MagicMock,
        pipeline_with_approval: PipelineDefinition,
    ) -> None:
        """Test that step status is set to WAITING_APPROVAL when approval required."""
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.pipeline_state import ApprovalRequired, StepStatus

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        with pytest.raises(ApprovalRequired):
            await executor.execute(
                pipeline=pipeline_with_approval,
                inputs={},
                project_id="proj-123",
            )

        step_calls = mock_execution_manager.update_step_execution.call_args_list
        waiting_calls = [
            c for c in step_calls if c.kwargs.get("status") == StepStatus.WAITING_APPROVAL
        ]
        assert len(waiting_calls) >= 1

    @pytest.mark.asyncio
    async def test_step_without_approval_does_not_pause(
        self,
        mock_db: MagicMock,
        mock_execution_manager: MagicMock,
        mock_llm_service: MagicMock,
        simple_pipeline: PipelineDefinition,
    ) -> None:
        """Test that steps without approval gate do not pause."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        result = await executor.execute(
            pipeline=simple_pipeline,
            inputs={},
            project_id="proj-123",
        )

        assert result is not None

    @pytest.mark.asyncio
    async def test_approval_required_false_does_not_pause(
        self, mock_db: MagicMock, mock_execution_manager: MagicMock, mock_llm_service: MagicMock
    ) -> None:
        """Test that approval.required=False does not pause execution."""
        from gobby.workflows.definitions import PipelineApproval
        from gobby.workflows.pipeline_executor import PipelineExecutor

        pipeline = PipelineDefinition(
            name="no-approval-pipeline",
            steps=[
                PipelineStep(
                    id="step1",
                    exec="echo step",
                    approval=PipelineApproval(required=False),
                ),
            ],
        )

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        result = await executor.execute(
            pipeline=pipeline,
            inputs={},
            project_id="proj-123",
        )

        assert result is not None

    @pytest.mark.asyncio
    async def test_approval_gate_executes_previous_steps_first(
        self,
        mock_db: MagicMock,
        mock_execution_manager: MagicMock,
        mock_llm_service: MagicMock,
        pipeline_with_approval: PipelineDefinition,
    ) -> None:
        """Test that steps before approval gate are executed first."""
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.pipeline_state import ApprovalRequired

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        with pytest.raises(ApprovalRequired):
            await executor.execute(
                pipeline=pipeline_with_approval,
                inputs={},
                project_id="proj-123",
            )

        step_calls = mock_execution_manager.create_step_execution.call_args_list
        assert len(step_calls) >= 1
        first_step = step_calls[0].kwargs["step_id"]
        assert first_step == "build"

    @pytest.mark.asyncio
    async def test_approval_gate_calls_webhook_notifier(
        self,
        mock_db: MagicMock,
        mock_execution_manager: MagicMock,
        mock_llm_service: MagicMock,
        mock_webhook_notifier: AsyncMock,
        pipeline_with_approval: PipelineDefinition,
    ) -> None:
        """Test that approval gate calls webhook notifier if configured."""
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.pipeline_state import ApprovalRequired

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            webhook_notifier=mock_webhook_notifier,
        )

        with pytest.raises(ApprovalRequired):
            await executor.execute(
                pipeline=pipeline_with_approval,
                inputs={},
                project_id="proj-123",
            )

        mock_webhook_notifier.notify_approval_pending.assert_called_once()


class TestApproveMethod:
    """Tests for PipelineExecutor.approve() method."""

    @pytest.fixture
    def pipeline_with_approval(self) -> PipelineDefinition:
        """Create a pipeline with an approval gate step."""
        from gobby.workflows.definitions import PipelineApproval

        return PipelineDefinition(
            name="approval-pipeline",
            steps=[
                PipelineStep(id="build", exec="echo build"),
                PipelineStep(
                    id="deploy",
                    exec="echo deploy",
                    approval=PipelineApproval(
                        required=True,
                        message="Approve deployment?",
                    ),
                ),
                PipelineStep(id="notify", exec="echo done"),
            ],
        )

    @pytest.mark.asyncio
    async def test_approve_finds_execution_by_token(
        self, mock_db: MagicMock, mock_execution_manager: MagicMock, mock_llm_service: MagicMock
    ) -> None:
        """Test that approve() finds the execution by approval token."""
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.pipeline_state import StepStatus

        mock_step = MagicMock()
        mock_step.id = 1
        mock_step.execution_id = "pe-test-123"
        mock_step.step_id = "deploy"
        mock_step.approval_token = "test-token-xyz"
        mock_step.status = StepStatus.WAITING_APPROVAL
        mock_execution_manager.consume_step_approval.return_value = mock_step

        mock_execution = MagicMock()
        mock_execution.id = "pe-test-123"
        mock_execution.pipeline_name = "approval-pipeline"
        mock_execution.status = ExecutionStatus.WAITING_APPROVAL
        mock_execution_manager.get_execution.return_value = mock_execution
        mock_execution_manager.update_execution_status.return_value = mock_execution
        run_db = AsyncMock(side_effect=lambda func, *args, **kwargs: func(*args, **kwargs))

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            run_db=run_db,
        )

        result = await executor.approve("test-token-xyz", approved_by="user@example.com")

        assert result.id == "pe-test-123"
        assert result.status == ExecutionStatus.WAITING_APPROVAL
        mock_execution_manager.consume_step_approval.assert_called_once_with(
            "test-token-xyz",
            status=StepStatus.PENDING,
            approved_by="user@example.com",
        )
        offloaded = [call.args[0] for call in run_db.await_args_list]
        assert mock_execution_manager.consume_step_approval in offloaded
        assert mock_execution_manager.get_execution in offloaded

    @pytest.mark.asyncio
    async def test_approve_invalid_token_raises_error(
        self, mock_db: MagicMock, mock_execution_manager: MagicMock, mock_llm_service: MagicMock
    ) -> None:
        """Test that approve() raises ValueError for invalid token."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        mock_execution_manager.consume_step_approval.return_value = None

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        with pytest.raises(ValueError, match="Invalid.*token"):
            await executor.approve("invalid-token", approved_by=None)

    @pytest.mark.asyncio
    async def test_approve_marks_step_as_approved(
        self, mock_db: MagicMock, mock_execution_manager: MagicMock, mock_llm_service: MagicMock
    ) -> None:
        """Test that approve() marks the step as approved."""
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.pipeline_state import StepStatus

        mock_step = MagicMock()
        mock_step.id = 1
        mock_step.execution_id = "pe-test-123"
        mock_step.step_id = "deploy"
        mock_step.approval_token = "test-token-xyz"
        mock_step.status = StepStatus.WAITING_APPROVAL
        mock_execution_manager.consume_step_approval.return_value = mock_step

        mock_execution = MagicMock()
        mock_execution.id = "pe-test-123"
        mock_execution.pipeline_name = "approval-pipeline"
        mock_execution.status = ExecutionStatus.WAITING_APPROVAL
        mock_execution_manager.get_execution.return_value = mock_execution
        mock_execution_manager.update_execution_status.return_value = mock_execution

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        result = await executor.approve("test-token-xyz", approved_by="user@example.com")

        assert result.id == "pe-test-123"
        assert result.status == ExecutionStatus.WAITING_APPROVAL
        mock_execution_manager.consume_step_approval.assert_called_once_with(
            "test-token-xyz",
            status=StepStatus.PENDING,
            approved_by="user@example.com",
        )

    @pytest.mark.asyncio
    async def test_approve_returns_execution(
        self, mock_db: MagicMock, mock_execution_manager: MagicMock, mock_llm_service: MagicMock
    ) -> None:
        """Test that approve() returns the updated execution."""
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.pipeline_state import StepStatus

        mock_step = MagicMock()
        mock_step.id = 1
        mock_step.execution_id = "pe-test-123"
        mock_step.step_id = "deploy"
        mock_step.approval_token = "test-token-xyz"
        mock_step.status = StepStatus.WAITING_APPROVAL
        mock_execution_manager.consume_step_approval.return_value = mock_step

        mock_execution = MagicMock()
        mock_execution.id = "pe-test-123"
        mock_execution.pipeline_name = "approval-pipeline"
        mock_execution.status = ExecutionStatus.COMPLETED
        mock_execution_manager.get_execution.return_value = mock_execution
        mock_execution_manager.update_execution_status.return_value = mock_execution

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        result = await executor.approve("test-token-xyz", approved_by=None)

        assert result is not None
        assert result.id == "pe-test-123"


class TestRejectMethod:
    """Tests for PipelineExecutor.reject() method."""

    @pytest.mark.asyncio
    async def test_reject_finds_execution_by_token(
        self, mock_db: MagicMock, mock_execution_manager: MagicMock, mock_llm_service: MagicMock
    ) -> None:
        """Test that reject() finds the execution by approval token."""
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.pipeline_state import StepStatus

        mock_step = MagicMock()
        mock_step.id = 1
        mock_step.execution_id = "pe-test-123"
        mock_step.step_id = "deploy"
        mock_step.approval_token = "test-token-xyz"
        mock_step.status = StepStatus.WAITING_APPROVAL
        mock_execution_manager.consume_step_approval.return_value = mock_step

        mock_execution = MagicMock()
        mock_execution.id = "pe-test-123"
        mock_execution.pipeline_name = "approval-pipeline"
        mock_execution.status = ExecutionStatus.CANCELLED
        mock_execution_manager.get_execution.return_value = mock_execution
        mock_execution_manager.update_execution_status.return_value = mock_execution
        run_db = AsyncMock(side_effect=lambda func, *args, **kwargs: func(*args, **kwargs))

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            run_db=run_db,
        )

        result = await executor.reject("test-token-xyz", rejected_by="user@example.com")

        assert result.id == "pe-test-123"
        assert result.status == ExecutionStatus.CANCELLED
        mock_execution_manager.consume_step_approval.assert_called_once_with(
            "test-token-xyz",
            status=StepStatus.FAILED,
            error="Rejected by user@example.com",
        )
        assert mock_execution_manager.update_execution_status in [
            call.args[0] for call in run_db.await_args_list
        ]

    @pytest.mark.asyncio
    async def test_reject_invalid_token_raises_error(
        self, mock_db: MagicMock, mock_execution_manager: MagicMock, mock_llm_service: MagicMock
    ) -> None:
        """Test that reject() raises ValueError for invalid token."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        mock_execution_manager.consume_step_approval.return_value = None

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        with pytest.raises(ValueError, match="Invalid.*token"):
            await executor.reject("invalid-token", rejected_by=None)

    @pytest.mark.asyncio
    async def test_reject_sets_status_to_cancelled(
        self, mock_db: MagicMock, mock_execution_manager: MagicMock, mock_llm_service: MagicMock
    ) -> None:
        """Test that reject() sets execution status to CANCELLED."""
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.pipeline_state import StepStatus

        mock_step = MagicMock()
        mock_step.id = 1
        mock_step.execution_id = "pe-test-123"
        mock_step.step_id = "deploy"
        mock_step.approval_token = "test-token-xyz"
        mock_step.status = StepStatus.WAITING_APPROVAL
        mock_execution_manager.consume_step_approval.return_value = mock_step

        mock_execution = MagicMock()
        mock_execution.id = "pe-test-123"
        mock_execution.pipeline_name = "approval-pipeline"
        mock_execution.status = ExecutionStatus.CANCELLED
        mock_execution_manager.get_execution.return_value = mock_execution
        mock_execution_manager.update_execution_status.return_value = mock_execution

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        await executor.reject("test-token-xyz", rejected_by=None)

        status_calls = mock_execution_manager.update_execution_status.call_args_list
        cancelled_calls = [
            c for c in status_calls if c.kwargs.get("status") == ExecutionStatus.CANCELLED
        ]
        assert len(cancelled_calls) >= 1

    @pytest.mark.asyncio
    async def test_reject_returns_execution(
        self, mock_db: MagicMock, mock_execution_manager: MagicMock, mock_llm_service: MagicMock
    ) -> None:
        """Test that reject() returns the updated execution."""
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.pipeline_state import StepStatus

        mock_step = MagicMock()
        mock_step.id = 1
        mock_step.execution_id = "pe-test-123"
        mock_step.step_id = "deploy"
        mock_step.approval_token = "test-token-xyz"
        mock_step.status = StepStatus.WAITING_APPROVAL
        mock_execution_manager.consume_step_approval.return_value = mock_step

        mock_execution = MagicMock()
        mock_execution.id = "pe-test-123"
        mock_execution.pipeline_name = "approval-pipeline"
        mock_execution.status = ExecutionStatus.CANCELLED
        mock_execution_manager.get_execution.return_value = mock_execution
        mock_execution_manager.update_execution_status.return_value = mock_execution

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        result = await executor.reject("test-token-xyz", rejected_by=None)

        assert result is not None
        assert result.id == "pe-test-123"

    @pytest.mark.asyncio
    async def test_reject_marks_step_as_failed(
        self, mock_db: MagicMock, mock_execution_manager: MagicMock, mock_llm_service: MagicMock
    ) -> None:
        """Test that reject() marks the step as failed/rejected."""
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.pipeline_state import StepStatus

        mock_step = MagicMock()
        mock_step.id = 1
        mock_step.execution_id = "pe-test-123"
        mock_step.step_id = "deploy"
        mock_step.approval_token = "test-token-xyz"
        mock_step.status = StepStatus.WAITING_APPROVAL
        mock_execution_manager.consume_step_approval.return_value = mock_step

        mock_execution = MagicMock()
        mock_execution.id = "pe-test-123"
        mock_execution.pipeline_name = "approval-pipeline"
        mock_execution.status = ExecutionStatus.CANCELLED
        mock_execution_manager.get_execution.return_value = mock_execution
        mock_execution_manager.update_execution_status.return_value = mock_execution

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        result = await executor.reject("test-token-xyz", rejected_by="admin@example.com")

        assert result.id == "pe-test-123"
        assert result.status == ExecutionStatus.CANCELLED
        mock_execution_manager.consume_step_approval.assert_called_once_with(
            "test-token-xyz",
            status=StepStatus.FAILED,
            error="Rejected by admin@example.com",
        )


class TestApproveReject:
    """Tests for approve() and reject() methods."""

    @pytest.mark.asyncio
    async def test_approve_uses_definition_snapshot_instead_of_edited_pipeline(
        self, mock_db: MagicMock, mock_execution_manager: MagicMock, mock_llm_service: MagicMock
    ) -> None:
        from gobby.workflows.pipeline_executor import PipelineExecutor

        snapshot = PipelineDefinition(
            name="test",
            steps=[PipelineStep(id="run", exec="echo snapshot")],
        )
        mock_exec = MagicMock()
        mock_exec.id = "pe-1"
        mock_exec.pipeline_name = "test"
        mock_exec.project_id = "project-1"
        mock_exec.definition_json = snapshot.model_dump_json()
        mock_exec.inputs_json = None

        approval_mgr = AsyncMock()
        approval_mgr.approve_step.return_value = mock_exec
        loader = AsyncMock()
        loader.load_pipeline.return_value = PipelineDefinition(
            name="test",
            steps=[PipelineStep(id="renamed", exec="echo edited")],
        )

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            loader=loader,
        )
        executor.approval_manager = approval_mgr
        executor.execute = AsyncMock(return_value=mock_exec)  # type: ignore[method-assign]

        result = await executor.approve("tok-1")

        assert result.id == "pe-1"
        resumed_pipeline = executor.execute.await_args.kwargs["pipeline"]
        assert resumed_pipeline.steps[0].exec == "echo snapshot"
        loader.load_pipeline.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_approve_missing_pipeline_surfaces_resume_error(
        self, mock_db: MagicMock, mock_execution_manager: MagicMock, mock_llm_service: MagicMock
    ) -> None:
        from gobby.workflows.pipeline_executor import PipelineExecutor

        mock_exec = MagicMock()
        mock_exec.id = "pe-1"
        mock_exec.pipeline_name = "deleted"
        mock_exec.project_id = "project-1"
        mock_exec.definition_json = None

        approval_mgr = AsyncMock()
        approval_mgr.approve_step.return_value = mock_exec
        loader = AsyncMock()
        loader.load_pipeline.return_value = None

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            loader=loader,
        )
        executor.approval_manager = approval_mgr

        with pytest.raises(ValueError, match="Pipeline 'deleted' not found for resume"):
            await executor.approve("tok-1")

    @pytest.mark.asyncio
    async def test_reject_delegates_to_approval_manager(
        self, mock_db: MagicMock, mock_execution_manager: MagicMock, mock_llm_service: MagicMock
    ) -> None:
        from gobby.workflows.pipeline_executor import PipelineExecutor

        mock_exec = MagicMock()
        mock_exec.id = "pe-1"
        mock_exec.status = ExecutionStatus.CANCELLED

        approval_mgr = AsyncMock()
        approval_mgr.reject_step.return_value = mock_exec

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )
        executor.approval_manager = approval_mgr

        result = await executor.reject("tok-1")
        assert result.status == ExecutionStatus.CANCELLED


_REPLAY_PROJECT_ID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"


def _create_waiting_approval(
    db: HubDatabase,
    token: str,
) -> tuple[LocalPipelineExecutionManager, str]:
    db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) "
        "VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (_REPLAY_PROJECT_ID, "Approval Replay Project"),
    )
    manager = LocalPipelineExecutionManager(db, project_id=_REPLAY_PROJECT_ID)
    execution = manager.create_execution(pipeline_name="approval-replay")
    waiting = manager.update_execution_status(
        execution.id,
        ExecutionStatus.WAITING_APPROVAL,
    )
    assert waiting is not None
    step = manager.create_step_execution(execution_id=execution.id, step_id="gate")
    manager.update_step_execution(
        step.id,
        status=StepStatus.WAITING_APPROVAL,
        approval_token=token,
    )
    return manager, execution.id


@pytest.mark.integration
class TestApprovalReplayIntegration:
    async def test_reject_after_approve_cannot_cancel_completed_execution(
        self,
        temp_db: HubDatabase,
    ) -> None:
        token = "approve-then-reject"
        manager, execution_id = _create_waiting_approval(temp_db, token)
        approvals = ApprovalManager(manager)

        await approvals.approve_step(token, approved_by="reviewer")
        completed = manager.update_execution_status(execution_id, ExecutionStatus.COMPLETED)
        assert completed is not None

        with pytest.raises(ValueError, match="already used"):
            await approvals.reject_step(token, rejected_by="reviewer")

        execution = manager.get_execution(execution_id)
        step = manager.get_steps_for_execution(execution_id)[0]
        assert execution is not None
        assert execution.status == ExecutionStatus.COMPLETED
        assert step.status == StepStatus.PENDING
        assert step.approval_token is None

    async def test_approve_after_reject_cannot_rewrite_rejected_step(
        self,
        temp_db: HubDatabase,
    ) -> None:
        token = "reject-then-approve"
        manager, execution_id = _create_waiting_approval(temp_db, token)
        approvals = ApprovalManager(manager)

        rejected = await approvals.reject_step(token, rejected_by="reviewer")
        assert rejected.status == ExecutionStatus.CANCELLED

        with pytest.raises(ValueError, match="already used"):
            await approvals.approve_step(token, approved_by="reviewer")

        execution = manager.get_execution(execution_id)
        step = manager.get_steps_for_execution(execution_id)[0]
        assert execution is not None
        assert execution.status == ExecutionStatus.CANCELLED
        assert step.status == StepStatus.FAILED
        assert step.approval_token is None

    async def test_concurrent_double_approve_runs_post_gate_once(
        self,
        temp_db: HubDatabase,
    ) -> None:
        token = "concurrent-double-approve"
        manager, execution_id = _create_waiting_approval(temp_db, token)
        arrivals = 0
        both_arrived = asyncio.Event()

        async def synchronized_run_db(
            func: Callable[..., object],
            *args: object,
            **kwargs: object,
        ) -> object:
            nonlocal arrivals
            arrivals += 1
            if arrivals == 2:
                both_arrived.set()
            await asyncio.wait_for(both_arrived.wait(), timeout=1)
            return await asyncio.to_thread(func, *args, **kwargs)

        approvals = ApprovalManager(manager, run_db=synchronized_run_db)
        post_gate_runs = 0

        async def approve_and_run_post_gate() -> None:
            nonlocal post_gate_runs
            await approvals.approve_step(token, approved_by="reviewer")
            post_gate_runs += 1

        results = await asyncio.gather(
            approve_and_run_post_gate(),
            approve_and_run_post_gate(),
            return_exceptions=True,
        )

        assert post_gate_runs == 1
        assert sum(result is None for result in results) == 1
        assert sum(isinstance(result, ValueError) for result in results) == 1
        step = manager.get_steps_for_execution(execution_id)[0]
        assert step.status == StepStatus.PENDING
        assert step.approval_token is None
