"""Tests for activate_workflow pipeline step type."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


class TestPipelineStepValidation:
    """Tests for PipelineStep accepting activate_workflow."""

    def test_activate_workflow_step_accepted(self) -> None:
        """PipelineStep accepts activate_workflow as a valid execution type."""
        from gobby.workflows.definitions import PipelineStep

        step = PipelineStep(
            id="activate",
            activate_workflow={"name": "auto-task", "variables": {"x": 1}},
        )
        assert step.activate_workflow is not None
        assert step.activate_workflow["name"] == "auto-task"

    def test_activate_workflow_mutually_exclusive_with_prompt(self) -> None:
        """activate_workflow cannot be combined with prompt."""
        from gobby.workflows.definitions import PipelineStep

        with pytest.raises(ValueError, match="mutually exclusive"):
            PipelineStep(
                id="bad",
                prompt="Do something",
                activate_workflow={"name": "test"},
            )

    def test_spawn_session_rejected(self) -> None:
        """PipelineStep no longer accepts spawn_session as an execution type."""
        from gobby.workflows.definitions import PipelineStep

        with pytest.raises(ValueError, match="requires at least one execution type"):
            PipelineStep(
                id="spawn",
                spawn_session={"cli": "claude", "prompt": "Do work"},
            )


class TestActivateWorkflowExecution:
    """Tests for activate_workflow step execution in pipeline executor.

    activate_workflow pipeline steps are removed — they fail fast.
    """

    @pytest.mark.asyncio
    async def test_activate_workflow_step_raises_error(self) -> None:
        """activate_workflow step raises an error (step type removed)."""
        from gobby.workflows.definitions import PipelineStep
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=MagicMock(),
            execution_manager=MagicMock(),
            llm_service=MagicMock(),
        )

        step = PipelineStep(
            id="activate",
            activate_workflow={
                "name": "auto-task",
                "session_id": "uuid-sess-1",
                "variables": {"task": "fix-bug"},
            },
        )

        with pytest.raises(RuntimeError, match="activate_workflow"):
            await executor._execute_step(step, {"inputs": {}, "steps": {}, "env": {}}, "proj-1")

    @pytest.mark.asyncio
    async def test_activate_workflow_fails_fast_without_loader(self) -> None:
        """activate_workflow raises before any loader behavior is consulted."""
        from gobby.workflows.definitions import PipelineStep
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=MagicMock(),
            execution_manager=MagicMock(),
            llm_service=MagicMock(),
        )

        step = PipelineStep(
            id="activate",
            activate_workflow={"name": "test-wf", "session_id": "sess-1"},
        )

        with pytest.raises(RuntimeError, match="activate_workflow"):
            await executor._execute_step(step, {"inputs": {}, "steps": {}, "env": {}}, "proj-1")
