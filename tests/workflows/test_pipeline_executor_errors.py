"""Tests for execute() error handling and _execute_wait_step.

Split from the test_pipeline_executor monolith (#12210).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.workflows.definitions import PipelineDefinition, PipelineStep

pytestmark = pytest.mark.unit


class TestExecuteErrorHandling:
    """Tests for error handling in execute()."""

    @pytest.mark.asyncio
    async def test_execute_nonexistent_execution_id(
        self, mock_db, mock_execution_manager, mock_llm_service, simple_pipeline
    ) -> None:
        """Resuming nonexistent execution raises ValueError."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        mock_execution_manager.get_execution.return_value = None

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        with pytest.raises(ValueError, match="not found"):
            await executor.execute(
                pipeline=simple_pipeline,
                inputs={},
                project_id="proj-1",
                execution_id="pe-gone",
            )

    @pytest.mark.asyncio
    async def test_nesting_depth_limit(
        self, mock_db, mock_execution_manager, mock_llm_service, simple_pipeline
    ) -> None:
        """Pipeline nesting depth should be enforced."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        with pytest.raises(RuntimeError, match="depth limit"):
            await executor.execute(
                pipeline=simple_pipeline,
                inputs={},
                project_id="proj-1",
                _depth=999,
            )

    @pytest.mark.asyncio
    async def test_cross_pipeline_cycle_detected(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Cross-pipeline cycles should be detected."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        pipeline_a = PipelineDefinition(
            name="pipeline-a",
            steps=[PipelineStep(id="s1", exec="echo")],
        )

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        with pytest.raises(RuntimeError, match="cycle detected"):
            await executor.execute(
                pipeline=pipeline_a,
                inputs={},
                project_id="proj-1",
                _depth=2,
                _pipeline_stack=frozenset({"pipeline-b", "pipeline-a"}),
            )

    @pytest.mark.asyncio
    async def test_exec_step_failure_marks_step_failed(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Non-zero exit code from exec step should fail the pipeline."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        pipeline = PipelineDefinition(
            name="fail-pipeline",
            steps=[PipelineStep(id="fail_step", exec="exit 1")],
        )

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        with pytest.raises(RuntimeError, match="exit code"):
            await executor.execute(
                pipeline=pipeline,
                inputs={},
                project_id="proj-1",
            )


class TestExecuteWaitStep:
    """Tests for _execute_wait_step."""

    @pytest.mark.asyncio
    async def test_wait_step_no_completion_id_raises(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            completion_registry=MagicMock(),
        )

        rendered = MagicMock()
        rendered.id = "wait-step"
        rendered.wait = {"timeout": 60}

        with pytest.raises(ValueError, match="completion_id"):
            await executor._execute_wait_step(rendered, {})

    @pytest.mark.asyncio
    async def test_wait_step_no_registry_raises(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        rendered = MagicMock()
        rendered.id = "wait-step"
        rendered.wait = {"completion_id": "cid-1", "timeout": 60}

        with pytest.raises(RuntimeError, match="completion_registry"):
            await executor._execute_wait_step(rendered, {})

    @pytest.mark.asyncio
    async def test_wait_step_invalid_timeout_defaults(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        from gobby.workflows.pipeline_executor import PipelineExecutor

        registry = AsyncMock()
        registry.wait.return_value = {"result": "done"}

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            completion_registry=registry,
        )

        rendered = MagicMock()
        rendered.id = "wait-step"
        rendered.wait = {"completion_id": "cid-1", "timeout": "not-a-number"}

        result = await executor._execute_wait_step(rendered, {})
        assert result == {"result": "done"}
        registry.wait.assert_called_once_with("cid-1", timeout=600.0)
