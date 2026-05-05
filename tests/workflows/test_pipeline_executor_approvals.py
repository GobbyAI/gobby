"""Tests for approval gate handling, approve(), and reject() methods.

Split from the test_pipeline_executor monolith (#12210).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.workflows.definitions import PipelineDefinition, PipelineStep
from gobby.workflows.pipeline_state import ExecutionStatus

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
        self, mock_db, mock_execution_manager, mock_llm_service, pipeline_with_approval
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
        self, mock_db, mock_execution_manager, mock_llm_service, pipeline_with_approval
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
        self, mock_db, mock_execution_manager, mock_llm_service, pipeline_with_approval
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
        self, mock_db, mock_execution_manager, mock_llm_service, pipeline_with_approval
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
        self, mock_db, mock_execution_manager, mock_llm_service, pipeline_with_approval
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
        self, mock_db, mock_execution_manager, mock_llm_service, pipeline_with_approval
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
        self, mock_db, mock_execution_manager, mock_llm_service, simple_pipeline
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
        self, mock_db, mock_execution_manager, mock_llm_service
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
        self, mock_db, mock_execution_manager, mock_llm_service, pipeline_with_approval
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
        mock_db,
        mock_execution_manager,
        mock_llm_service,
        mock_webhook_notifier,
        pipeline_with_approval,
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
        self, mock_db, mock_execution_manager, mock_llm_service
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
        mock_execution_manager.get_step_by_approval_token.return_value = mock_step

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

        await executor.approve("test-token-xyz", approved_by="user@example.com")

        mock_execution_manager.get_step_by_approval_token.assert_called_once_with("test-token-xyz")
        assert mock_execution_manager.get_step_by_approval_token.call_count == 1
        assert mock_execution_manager.get_step_by_approval_token.call_args is not None

    @pytest.mark.asyncio
    async def test_approve_invalid_token_raises_error(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that approve() raises ValueError for invalid token."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        mock_execution_manager.get_step_by_approval_token.return_value = None

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        with pytest.raises(ValueError, match="Invalid.*token"):
            await executor.approve("invalid-token", approved_by=None)

    @pytest.mark.asyncio
    async def test_approve_marks_step_as_approved(
        self, mock_db, mock_execution_manager, mock_llm_service
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
        mock_execution_manager.get_step_by_approval_token.return_value = mock_step

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

        await executor.approve("test-token-xyz", approved_by="user@example.com")

        update_calls = mock_execution_manager.update_step_execution.call_args_list
        approval_calls = [c for c in update_calls if c.kwargs.get("approved_by") is not None]
        assert len(approval_calls) >= 1
        assert approval_calls[0].kwargs["approved_by"] == "user@example.com"

    @pytest.mark.asyncio
    async def test_approve_returns_execution(
        self, mock_db, mock_execution_manager, mock_llm_service
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
        mock_execution_manager.get_step_by_approval_token.return_value = mock_step

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
        self, mock_db, mock_execution_manager, mock_llm_service
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
        mock_execution_manager.get_step_by_approval_token.return_value = mock_step

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

        await executor.reject("test-token-xyz", rejected_by="user@example.com")

        mock_execution_manager.get_step_by_approval_token.assert_called_once_with("test-token-xyz")
        assert mock_execution_manager.get_step_by_approval_token.call_count == 1
        assert mock_execution_manager.get_step_by_approval_token.call_args is not None

    @pytest.mark.asyncio
    async def test_reject_invalid_token_raises_error(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that reject() raises ValueError for invalid token."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        mock_execution_manager.get_step_by_approval_token.return_value = None

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        with pytest.raises(ValueError, match="Invalid.*token"):
            await executor.reject("invalid-token", rejected_by=None)

    @pytest.mark.asyncio
    async def test_reject_sets_status_to_cancelled(
        self, mock_db, mock_execution_manager, mock_llm_service
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
        mock_execution_manager.get_step_by_approval_token.return_value = mock_step

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
        self, mock_db, mock_execution_manager, mock_llm_service
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
        mock_execution_manager.get_step_by_approval_token.return_value = mock_step

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
        self, mock_db, mock_execution_manager, mock_llm_service
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
        mock_execution_manager.get_step_by_approval_token.return_value = mock_step

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

        await executor.reject("test-token-xyz", rejected_by="admin@example.com")

        step_calls = mock_execution_manager.update_step_execution.call_args_list
        failed_calls = [c for c in step_calls if c.kwargs.get("status") == StepStatus.FAILED]
        assert len(failed_calls) >= 1


class TestApproveReject:
    """Tests for approve() and reject() methods."""

    @pytest.mark.asyncio
    async def test_approve_without_loader(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        from gobby.workflows.pipeline_executor import PipelineExecutor

        mock_exec = MagicMock()
        mock_exec.id = "pe-1"
        mock_exec.pipeline_name = "test"

        approval_mgr = AsyncMock()
        approval_mgr.approve_step.return_value = mock_exec

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )
        executor.approval_manager = approval_mgr

        result = await executor.approve("tok-1")
        assert result.id == "pe-1"

    @pytest.mark.asyncio
    async def test_reject_delegates_to_approval_manager(
        self, mock_db, mock_execution_manager, mock_llm_service
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
