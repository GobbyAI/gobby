"""Tests for completion registry wiring into PipelineExecutor and AgentRunner."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from gobby.events.completion_registry import CompletionEventRegistry
from gobby.workflows.pipeline_state import ExecutionStatus

pytestmark = pytest.mark.unit


class TestPipelineExecutorNotifiesRegistry:
    """PipelineExecutor notifies completion registry on completion/failure."""

    @pytest.mark.asyncio
    async def test_notify_on_pipeline_completed(self) -> None:
        """Registry.notify() called when pipeline completes successfully."""
        from gobby.workflows.definitions import PipelineDefinition, PipelineStep
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.pipeline_state import PipelineExecution

        registry = CompletionEventRegistry()

        pending_exec = PipelineExecution(
            id="pe-complete",
            pipeline_name="test",
            project_id="proj-1",
            status=ExecutionStatus.PENDING,
            created_at="2025-01-01",
            updated_at="2025-01-01",
        )
        running_exec = PipelineExecution(
            id="pe-complete",
            pipeline_name="test",
            project_id="proj-1",
            status=ExecutionStatus.RUNNING,
            created_at="2025-01-01",
            updated_at="2025-01-01",
        )
        completed_exec = PipelineExecution(
            id="pe-complete",
            pipeline_name="test",
            project_id="proj-1",
            status=ExecutionStatus.COMPLETED,
            created_at="2025-01-01",
            updated_at="2025-01-01",
        )

        mock_em = MagicMock()
        mock_em.create_execution.return_value = pending_exec
        mock_em.update_execution_status.side_effect = [running_exec, completed_exec]
        mock_em.get_steps_for_execution.return_value = []
        mock_em.create_step_execution.return_value = MagicMock(
            id=1, status=MagicMock(value="pending")
        )
        mock_em.update_step_execution.return_value = None

        executor = PipelineExecutor(
            db=MagicMock(),
            execution_manager=mock_em,
            llm_service=None,
            completion_registry=registry,
        )

        # Register and track the event
        registry.register("pe-complete", subscribers=[])

        pipeline = PipelineDefinition(
            name="test-pipe",
            steps=[PipelineStep(id="step1", exec="echo ok")],
        )

        await executor.execute(
            pipeline=pipeline,
            inputs={},
            project_id="proj-1",
        )

        # The registry should have been notified
        result = registry.get_result("pe-complete")
        assert result is not None
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_notify_on_pipeline_failed(self) -> None:
        """Registry.notify() called when pipeline fails."""
        from gobby.workflows.definitions import PipelineDefinition, PipelineStep
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.pipeline_state import PipelineExecution

        registry = CompletionEventRegistry()

        pending_exec = PipelineExecution(
            id="pe-fail",
            pipeline_name="test",
            project_id="proj-1",
            status=ExecutionStatus.PENDING,
            created_at="2025-01-01",
            updated_at="2025-01-01",
        )
        running_exec = PipelineExecution(
            id="pe-fail",
            pipeline_name="test",
            project_id="proj-1",
            status=ExecutionStatus.RUNNING,
            created_at="2025-01-01",
            updated_at="2025-01-01",
        )
        failed_exec = PipelineExecution(
            id="pe-fail",
            pipeline_name="test",
            project_id="proj-1",
            status=ExecutionStatus.FAILED,
            created_at="2025-01-01",
            updated_at="2025-01-01",
        )

        mock_em = MagicMock()
        mock_em.create_execution.return_value = pending_exec
        mock_em.update_execution_status.side_effect = [running_exec, failed_exec]
        mock_em.get_steps_for_execution.return_value = []
        mock_em.create_step_execution.return_value = MagicMock(
            id=1, status=MagicMock(value="pending")
        )
        mock_em.update_step_execution.return_value = None

        executor = PipelineExecutor(
            db=MagicMock(),
            execution_manager=mock_em,
            llm_service=None,
            completion_registry=registry,
        )

        registry.register("pe-fail", subscribers=[])

        pipeline = PipelineDefinition(
            name="failing-pipe",
            steps=[PipelineStep(id="bad_step", exec="echo ok")],
        )

        # Patch _execute_step to raise a real exception
        with patch.object(executor, "_execute_step", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="boom"):
                await executor.execute(
                    pipeline=pipeline,
                    inputs={},
                    project_id="proj-1",
                )

        result = registry.get_result("pe-fail")
        assert result is not None
        assert result["status"] == "failed"

    @pytest.mark.asyncio
    async def test_no_notification_without_registry(self) -> None:
        """Pipeline works fine without completion_registry (backward compat)."""
        from gobby.workflows.definitions import PipelineDefinition, PipelineStep
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.pipeline_state import PipelineExecution

        pending_exec = PipelineExecution(
            id="pe-noreg",
            pipeline_name="test",
            project_id="proj-1",
            status=ExecutionStatus.PENDING,
            created_at="2025-01-01",
            updated_at="2025-01-01",
        )
        running_exec = PipelineExecution(
            id="pe-noreg",
            pipeline_name="test",
            project_id="proj-1",
            status=ExecutionStatus.RUNNING,
            created_at="2025-01-01",
            updated_at="2025-01-01",
        )
        completed_exec = PipelineExecution(
            id="pe-noreg",
            pipeline_name="test",
            project_id="proj-1",
            status=ExecutionStatus.COMPLETED,
            created_at="2025-01-01",
            updated_at="2025-01-01",
        )

        mock_em = MagicMock()
        mock_em.create_execution.return_value = pending_exec
        mock_em.update_execution_status.side_effect = [running_exec, completed_exec]
        mock_em.get_steps_for_execution.return_value = []
        mock_em.create_step_execution.return_value = MagicMock(
            id=1, status=MagicMock(value="pending")
        )
        mock_em.update_step_execution.return_value = None

        # No completion_registry passed
        executor = PipelineExecutor(
            db=MagicMock(),
            execution_manager=mock_em,
            llm_service=None,
        )

        pipeline = PipelineDefinition(
            name="test-pipe",
            steps=[PipelineStep(id="step1", exec="echo ok")],
        )

        # Should not raise
        result = await executor.execute(
            pipeline=pipeline,
            inputs={},
            project_id="proj-1",
        )
        assert result.status == ExecutionStatus.COMPLETED
