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
            id="1dc281e5-b5f3-5a79-89ad-cc808da561fe",
            pipeline_name="test",
            project_id="aa81136a-134a-5bf3-bcd4-adac1fe28e9b",
            status=ExecutionStatus.PENDING,
            created_at="2025-01-01",
            updated_at="2025-01-01",
        )
        running_exec = PipelineExecution(
            id="1dc281e5-b5f3-5a79-89ad-cc808da561fe",
            pipeline_name="test",
            project_id="aa81136a-134a-5bf3-bcd4-adac1fe28e9b",
            status=ExecutionStatus.RUNNING,
            created_at="2025-01-01",
            updated_at="2025-01-01",
        )
        completed_exec = PipelineExecution(
            id="1dc281e5-b5f3-5a79-89ad-cc808da561fe",
            pipeline_name="test",
            project_id="aa81136a-134a-5bf3-bcd4-adac1fe28e9b",
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
        mock_em.get_failed_steps.return_value = []

        executor = PipelineExecutor(
            db=MagicMock(),
            execution_manager=mock_em,
            llm_service=None,
            completion_registry=registry,
        )

        # Register and track the event
        registry.register("1dc281e5-b5f3-5a79-89ad-cc808da561fe", subscribers=[])

        pipeline = PipelineDefinition(
            name="test-pipe",
            steps=[PipelineStep(id="step1", exec="echo ok")],
        )

        await executor.execute(
            pipeline=pipeline,
            inputs={},
            project_id="aa81136a-134a-5bf3-bcd4-adac1fe28e9b",
        )

        # The registry should have been notified
        result = registry.get_result("1dc281e5-b5f3-5a79-89ad-cc808da561fe")
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
            id="e5552960-d7db-5100-ac45-b791d63c9567",
            pipeline_name="test",
            project_id="aa81136a-134a-5bf3-bcd4-adac1fe28e9b",
            status=ExecutionStatus.PENDING,
            created_at="2025-01-01",
            updated_at="2025-01-01",
        )
        running_exec = PipelineExecution(
            id="e5552960-d7db-5100-ac45-b791d63c9567",
            pipeline_name="test",
            project_id="aa81136a-134a-5bf3-bcd4-adac1fe28e9b",
            status=ExecutionStatus.RUNNING,
            created_at="2025-01-01",
            updated_at="2025-01-01",
        )
        failed_exec = PipelineExecution(
            id="e5552960-d7db-5100-ac45-b791d63c9567",
            pipeline_name="test",
            project_id="aa81136a-134a-5bf3-bcd4-adac1fe28e9b",
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

        registry.register("e5552960-d7db-5100-ac45-b791d63c9567", subscribers=[])

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
                    project_id="aa81136a-134a-5bf3-bcd4-adac1fe28e9b",
                )

        result = registry.get_result("e5552960-d7db-5100-ac45-b791d63c9567")
        assert result is not None
        assert result["status"] == "failed"

    @pytest.mark.asyncio
    async def test_no_notification_without_registry(self) -> None:
        """Pipeline works fine without completion_registry (backward compat)."""
        from gobby.workflows.definitions import PipelineDefinition, PipelineStep
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.pipeline_state import PipelineExecution

        pending_exec = PipelineExecution(
            id="fccf894a-eb14-5c61-b51d-8304116bce55",
            pipeline_name="test",
            project_id="aa81136a-134a-5bf3-bcd4-adac1fe28e9b",
            status=ExecutionStatus.PENDING,
            created_at="2025-01-01",
            updated_at="2025-01-01",
        )
        running_exec = PipelineExecution(
            id="fccf894a-eb14-5c61-b51d-8304116bce55",
            pipeline_name="test",
            project_id="aa81136a-134a-5bf3-bcd4-adac1fe28e9b",
            status=ExecutionStatus.RUNNING,
            created_at="2025-01-01",
            updated_at="2025-01-01",
        )
        completed_exec = PipelineExecution(
            id="fccf894a-eb14-5c61-b51d-8304116bce55",
            pipeline_name="test",
            project_id="aa81136a-134a-5bf3-bcd4-adac1fe28e9b",
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
        mock_em.get_failed_steps.return_value = []

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
            project_id="aa81136a-134a-5bf3-bcd4-adac1fe28e9b",
        )
        assert result.status == ExecutionStatus.COMPLETED
        assert result.id == "fccf894a-eb14-5c61-b51d-8304116bce55"
