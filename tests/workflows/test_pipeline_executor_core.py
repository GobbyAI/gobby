"""Tests for PipelineExecutor initialization, execute(), and basic step execution.

Split from the test_pipeline_executor monolith (#12210).
"""

import json
from unittest.mock import MagicMock

import pytest

from gobby.workflows.pipeline_state import ExecutionStatus, StepStatus

pytestmark = pytest.mark.unit


class TestPipelineExecutorInit:
    """Tests for PipelineExecutor initialization."""

    def test_init_with_required_dependencies(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that executor initializes with required dependencies."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        assert executor.db is mock_db
        assert executor.execution_manager is mock_execution_manager
        assert executor.llm_service is mock_llm_service

    def test_init_with_optional_dependencies(
        self,
        mock_db,
        mock_execution_manager,
        mock_llm_service,
        mock_template_engine,
        mock_webhook_notifier,
    ) -> None:
        """Test that executor initializes with optional dependencies."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            template_engine=mock_template_engine,
            webhook_notifier=mock_webhook_notifier,
        )

        assert executor.renderer.template_engine is mock_template_engine
        assert executor.webhook_notifier is mock_webhook_notifier
        assert executor.renderer is not None
        assert executor.approval_manager is not None


class TestPipelineExecutorExecute:
    """Tests for PipelineExecutor.execute() method."""

    @pytest.mark.asyncio
    async def test_execute_creates_execution_record(
        self, mock_db, mock_execution_manager, mock_llm_service, simple_pipeline
    ) -> None:
        """Test that execute() creates a PipelineExecution record."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        await executor.execute(
            pipeline=simple_pipeline,
            inputs={},
            project_id="proj-123",
        )

        mock_execution_manager.create_execution.assert_called_once()
        call_kwargs = mock_execution_manager.create_execution.call_args
        assert call_kwargs.kwargs["pipeline_name"] == "test-pipeline"
        assert call_kwargs.kwargs["inputs_json"] is not None

    @pytest.mark.asyncio
    async def test_execute_with_existing_execution_id(
        self, mock_db, mock_execution_manager, mock_llm_service, simple_pipeline
    ) -> None:
        """Test that execute() uses existing execution ID if provided."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        await executor.execute(
            pipeline=simple_pipeline,
            inputs={},
            project_id="proj-123",
            execution_id="pe-existing-456",
        )

        mock_execution_manager.get_execution.assert_called_with("pe-existing-456")

    @pytest.mark.asyncio
    async def test_execute_builds_context_with_inputs(
        self, mock_db, mock_execution_manager, mock_llm_service, pipeline_with_inputs
    ) -> None:
        """Test that execute() builds context with input values."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        inputs = {"target": "/path/to/file", "mode": "thorough"}

        await executor.execute(
            pipeline=pipeline_with_inputs,
            inputs=inputs,
            project_id="proj-123",
        )

        call_kwargs = mock_execution_manager.create_execution.call_args.kwargs
        inputs_json = call_kwargs["inputs_json"]
        assert json.loads(inputs_json) == inputs

    @pytest.mark.asyncio
    async def test_execute_iterates_steps_in_order(
        self, mock_db, mock_execution_manager, mock_llm_service, simple_pipeline
    ) -> None:
        """Test that execute() iterates through steps in order."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        await executor.execute(
            pipeline=simple_pipeline,
            inputs={},
            project_id="proj-123",
        )

        assert mock_execution_manager.create_step_execution.call_count == 2
        calls = mock_execution_manager.create_step_execution.call_args_list
        assert calls[0].kwargs["step_id"] == "step1"
        assert calls[1].kwargs["step_id"] == "step2"

    @pytest.mark.asyncio
    async def test_execute_returns_execution_with_status(
        self, mock_db, mock_execution_manager, mock_llm_service, simple_pipeline
    ) -> None:
        """Test that execute() returns execution with final status."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        completed_execution = MagicMock()
        completed_execution.id = "pe-test-123"
        completed_execution.status = ExecutionStatus.COMPLETED
        mock_execution_manager.update_execution_status.return_value = completed_execution

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
        assert result.status == ExecutionStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_execute_updates_status_to_running(
        self, mock_db, mock_execution_manager, mock_llm_service, simple_pipeline
    ) -> None:
        """Test that execute() updates status to RUNNING when starting."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        await executor.execute(
            pipeline=simple_pipeline,
            inputs={},
            project_id="proj-123",
        )

        calls = mock_execution_manager.update_execution_status.call_args_list
        first_call = calls[0]
        assert first_call.kwargs["status"] == ExecutionStatus.RUNNING


class TestPipelineExecutorStepExecution:
    """Tests for step execution within PipelineExecutor."""

    @pytest.mark.asyncio
    async def test_execute_step_updates_step_status(
        self, mock_db, mock_execution_manager, mock_llm_service, simple_pipeline
    ) -> None:
        """Test that step execution updates step status."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        await executor.execute(
            pipeline=simple_pipeline,
            inputs={},
            project_id="proj-123",
        )

        update_calls = mock_execution_manager.update_step_execution.call_args_list
        statuses = [call.kwargs.get("status") for call in update_calls if call.kwargs.get("status")]
        assert StepStatus.RUNNING in statuses or StepStatus.COMPLETED in statuses

    @pytest.mark.asyncio
    async def test_execute_stores_step_output(
        self, mock_db, mock_execution_manager, mock_llm_service, simple_pipeline
    ) -> None:
        """Test that step execution stores output in step record."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        await executor.execute(
            pipeline=simple_pipeline,
            inputs={},
            project_id="proj-123",
        )

        update_calls = mock_execution_manager.update_step_execution.call_args_list
        has_output = any(call.kwargs.get("output_json") is not None for call in update_calls)
        assert has_output


class TestExecuteExecStep:
    """Tests for _execute_exec_step() method."""

    @pytest.mark.asyncio
    async def test_exec_step_runs_shell_command(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that exec step runs a shell command."""
        from gobby.workflows.pipeline.handlers import execute_exec_step

        context: dict = {"inputs": {}, "steps": {}}
        result = await execute_exec_step("echo hello", context)

        assert result is not None
        assert "stdout" in result
        assert "hello" in result["stdout"]

    @pytest.mark.asyncio
    async def test_exec_step_captures_stdout(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that exec step captures stdout."""
        from gobby.workflows.pipeline.handlers import execute_exec_step

        context: dict = {"inputs": {}, "steps": {}}
        result = await execute_exec_step("echo 'test output'", context)

        assert result["stdout"].strip() == "test output"
        assert result["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_exec_step_captures_stderr(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that exec step captures stderr."""
        from gobby.workflows.pipeline.handlers import execute_exec_step

        context: dict = {"inputs": {}, "steps": {}}
        # Use sh redirection to write to stderr — avoids nested quoting.
        result = await execute_exec_step("sh -c 'echo error >&2'", context)

        assert "stderr" in result
        assert "error" in result["stderr"]

    @pytest.mark.asyncio
    async def test_exec_step_handles_command_failure(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that exec step handles command failure gracefully."""
        from gobby.workflows.pipeline.handlers import execute_exec_step

        context: dict = {"inputs": {}, "steps": {}}
        result = await execute_exec_step("exit 1", context)

        assert result["exit_code"] == 1

    @pytest.mark.asyncio
    async def test_exec_step_handles_nonexistent_command(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that exec step handles non-existent commands."""
        from gobby.workflows.pipeline.handlers import execute_exec_step

        context: dict = {"inputs": {}, "steps": {}}
        result = await execute_exec_step("nonexistent_command_xyz_123", context)

        assert result["exit_code"] != 0

    @pytest.mark.asyncio
    async def test_exec_step_returns_dict_output(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that exec step returns dict with stdout, stderr, exit_code."""
        from gobby.workflows.pipeline.handlers import execute_exec_step

        context: dict = {"inputs": {}, "steps": {}}
        result = await execute_exec_step("echo test", context)

        assert isinstance(result, dict)
        assert "stdout" in result
        assert "stderr" in result
        assert "exit_code" in result


class TestExecutePromptStep:
    """Tests for _execute_prompt_step() method."""

    @pytest.mark.asyncio
    async def test_prompt_step_calls_llm_service(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that prompt step calls the LLM service."""
        from gobby.workflows.pipeline.handlers import execute_prompt_step

        mock_llm_service.get_default_provider.return_value.generate_text.return_value = (
            "LLM response text"
        )

        context: dict = {"inputs": {}, "steps": {}}
        await execute_prompt_step("Analyze this data", context, mock_llm_service)

        mock_llm_service.get_default_provider.return_value.generate_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_prompt_step_returns_response(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that prompt step returns the LLM response."""
        from gobby.workflows.pipeline.handlers import execute_prompt_step

        mock_llm_service.get_default_provider.return_value.generate_text.return_value = (
            "Generated analysis"
        )

        context: dict = {"inputs": {}, "steps": {}}
        result = await execute_prompt_step("Analyze this", context, mock_llm_service)

        assert result is not None
        assert "response" in result
        assert result["response"] == "Generated analysis"

    @pytest.mark.asyncio
    async def test_prompt_step_passes_prompt_to_llm(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that prompt step passes the prompt text to LLM."""
        from gobby.workflows.pipeline.handlers import execute_prompt_step

        mock_llm_service.get_default_provider.return_value.generate_text.return_value = "Response"

        context: dict = {"inputs": {}, "steps": {}}
        await execute_prompt_step("Generate a report", context, mock_llm_service)

        # Inspect args/kwargs directly instead of str(call_args) so the test
        # fails cleanly if the call signature changes.
        call_args = mock_llm_service.get_default_provider.return_value.generate_text.call_args
        actual_prompt = call_args.kwargs.get("prompt")
        if actual_prompt is None and call_args.args:
            actual_prompt = call_args.args[0]
        assert actual_prompt == "Generate a report"

    @pytest.mark.asyncio
    async def test_prompt_step_handles_llm_error(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that prompt step handles LLM errors gracefully."""
        from gobby.workflows.pipeline.handlers import execute_prompt_step

        mock_llm_service.get_default_provider.return_value.generate_text.side_effect = RuntimeError(
            "LLM API error"
        )

        context: dict = {"inputs": {}, "steps": {}}
        result = await execute_prompt_step("Generate something", context, mock_llm_service)

        assert result is not None
        assert "error" in result

    @pytest.mark.asyncio
    async def test_prompt_step_returns_dict_structure(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that prompt step returns proper dict structure."""
        from gobby.workflows.pipeline.handlers import execute_prompt_step

        mock_llm_service.get_default_provider.return_value.generate_text.return_value = (
            "Test response"
        )

        context: dict = {"inputs": {}, "steps": {}}
        result = await execute_prompt_step("Test prompt", context, mock_llm_service)

        assert isinstance(result, dict)
        assert "response" in result
