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

    activate_workflow pipeline steps are removed — they always return an error.
    """

    @pytest.mark.asyncio
    async def test_activate_workflow_step_returns_error(self) -> None:
        """activate_workflow step returns error (step type removed)."""
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

        result = await executor._execute_step(
            step, {"inputs": {}, "steps": {}, "env": {}}, "proj-1"
        )

        assert result is not None
        assert "error" in result

    @pytest.mark.asyncio
    async def test_activate_workflow_fails_without_loader(self) -> None:
        """activate_workflow returns error when loader not configured."""
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

        result = await executor._execute_step(
            step, {"inputs": {}, "steps": {}, "env": {}}, "proj-1"
        )

        assert result is not None
        assert "error" in result
