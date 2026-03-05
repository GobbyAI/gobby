"""Tests for pipeline integration with completion registry.

Covers:
- INTERRUPTED status in ExecutionStatus
- wait step type in PipelineStep
- wait step execution in pipeline executor
- fail_stale → interrupt_stale rename in storage
"""

from __future__ import annotations

import asyncio

import pytest

from gobby.workflows.pipeline_state import ExecutionStatus

pytestmark = pytest.mark.integration


class TestInterruptedStatus:
    """INTERRUPTED status exists and behaves correctly."""

    def test_interrupted_status_exists(self) -> None:
        assert ExecutionStatus.INTERRUPTED.value == "interrupted"

    def test_all_terminal_statuses(self) -> None:
        """COMPLETED, FAILED, CANCELLED are terminal. INTERRUPTED is non-terminal."""
        terminal = {ExecutionStatus.COMPLETED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED}
        non_terminal = {
            ExecutionStatus.PENDING,
            ExecutionStatus.RUNNING,
            ExecutionStatus.WAITING_APPROVAL,
            ExecutionStatus.INTERRUPTED,
        }
        all_statuses = set(ExecutionStatus)
        assert terminal | non_terminal == all_statuses


class TestWaitStepDefinition:
    """wait field on PipelineStep."""

    def test_wait_step_is_valid_execution_type(self) -> None:
        from gobby.workflows.definitions import PipelineStep

        step = PipelineStep(
            id="wait_researcher",
            wait={"completion_id": "run-abc123", "timeout": 600},
        )
        assert step.wait is not None
        assert step.wait["completion_id"] == "run-abc123"
        assert step.wait["timeout"] == 600

    def test_wait_step_mutually_exclusive_with_exec(self) -> None:
        from gobby.workflows.definitions import PipelineStep

        with pytest.raises(ValueError, match="mutually exclusive"):
            PipelineStep(
                id="bad",
                wait={"completion_id": "x"},
                exec="echo hello",
            )

    def test_wait_step_alone_is_valid(self) -> None:
        from gobby.workflows.definitions import PipelineStep

        step = PipelineStep(id="wait_step", wait={"completion_id": "pe-123"})
        assert step.exec is None
        assert step.prompt is None
        assert step.mcp is None
        assert step.wait is not None


class TestWaitStepExecution:
    """Pipeline executor handles wait steps correctly."""

    @pytest.mark.asyncio
    async def test_wait_step_blocks_until_notify(self) -> None:
        """wait step should block until the completion registry fires."""
        from unittest.mock import MagicMock

        from gobby.events.completion_registry import CompletionEventRegistry
        from gobby.workflows.definitions import PipelineDefinition, PipelineStep
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution

        registry = CompletionEventRegistry()

        pending_exec = PipelineExecution(
            id="pe-test",
            pipeline_name="test",
            project_id="proj-1",
            status=ExecutionStatus.PENDING,
            created_at="2025-01-01",
            updated_at="2025-01-01",
        )
        running_exec = PipelineExecution(
            id="pe-test",
            pipeline_name="test",
            project_id="proj-1",
            status=ExecutionStatus.RUNNING,
            created_at="2025-01-01",
            updated_at="2025-01-01",
        )
        completed_exec = PipelineExecution(
            id="pe-test",
            pipeline_name="test",
            project_id="proj-1",
            status=ExecutionStatus.COMPLETED,
            created_at="2025-01-01",
            updated_at="2025-01-01",
        )

        mock_em = MagicMock()
        mock_em.create_execution.return_value = pending_exec
        # First call = RUNNING, second call = COMPLETED
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

        # Register the completion event that the wait step will block on
        registry.register("run-abc", subscribers=[])

        # Notify after a short delay
        async def _notify_soon() -> None:
            await asyncio.sleep(0.05)
            await registry.notify("run-abc", {"agent_status": "success", "output": "done"})

        asyncio.create_task(_notify_soon())

        pipeline = PipelineDefinition(
            name="test-pipeline",
            steps=[
                PipelineStep(id="wait_agent", wait={"completion_id": "run-abc", "timeout": 2}),
            ],
        )

        result = await executor.execute(
            pipeline=pipeline,
            inputs={},
            project_id="proj-1",
        )
        assert result.status == ExecutionStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_wait_step_timeout_fails_pipeline(self) -> None:
        """wait step timeout should fail the pipeline."""
        from unittest.mock import MagicMock

        from gobby.events.completion_registry import CompletionEventRegistry
        from gobby.workflows.definitions import PipelineDefinition, PipelineStep
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.pipeline_state import ExecutionStatus

        registry = CompletionEventRegistry()

        mock_em = MagicMock()
        mock_em.create_execution.return_value = MagicMock(
            id="pe-test",
            pipeline_name="test",
            project_id="proj-1",
            status=ExecutionStatus.PENDING,
        )
        mock_em.update_execution_status.return_value = MagicMock(
            id="pe-test",
            pipeline_name="test",
            project_id="proj-1",
            status=ExecutionStatus.RUNNING,
        )
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

        # Register but never notify — should timeout
        registry.register("run-timeout", subscribers=[])

        pipeline = PipelineDefinition(
            name="test-pipeline",
            steps=[
                PipelineStep(
                    id="wait_agent",
                    wait={"completion_id": "run-timeout", "timeout": 0.05},
                ),
            ],
        )

        with pytest.raises(asyncio.TimeoutError):
            await executor.execute(
                pipeline=pipeline,
                inputs={},
                project_id="proj-1",
            )


class TestInterruptStaleExecutions:
    """Storage uses INTERRUPTED instead of FAILED for stale running executions."""

    def test_interrupt_stale_method_exists(self) -> None:
        """The renamed method should exist."""
        from gobby.storage.pipelines import LocalPipelineExecutionManager

        assert hasattr(LocalPipelineExecutionManager, "interrupt_stale_running_executions")

    def test_fail_stale_still_works_as_alias(self) -> None:
        """Backwards compat: fail_stale_running_executions still works."""
        from gobby.storage.pipelines import LocalPipelineExecutionManager

        assert hasattr(LocalPipelineExecutionManager, "fail_stale_running_executions")
