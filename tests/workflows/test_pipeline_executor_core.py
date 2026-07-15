"""Tests for PipelineExecutor initialization, execute(), and basic step execution.

Split from the test_pipeline_executor monolith (#12210).
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.pipelines import PipelineConfig
from gobby.workflows.definitions import WebhookConfig, WebhookEndpoint
from gobby.workflows.pipeline_state import ExecutionStatus, StepStatus
from gobby.workflows.pipeline_webhooks import WebhookNotifier
from gobby.workflows.webhook_executor import WebhookExecutor

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

        run_db = AsyncMock(side_effect=lambda func, *args, **kwargs: func(*args, **kwargs))
        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            run_db=run_db,
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
        offloaded = [call.args[0] for call in run_db.await_args_list]
        assert executor._create_execution_record in offloaded
        assert mock_execution_manager.update_execution_status in offloaded
        assert mock_execution_manager.get_steps_for_execution in offloaded
        assert mock_execution_manager.create_step_execution in offloaded
        assert mock_execution_manager.update_step_execution in offloaded
        assert mock_execution_manager.get_failed_steps in offloaded

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

        mock_execution_manager.get_execution.assert_any_call("pe-existing-456")
        assert mock_execution_manager.get_execution.call_count >= 1
        assert mock_execution_manager.get_execution.call_args is not None

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

    async def test_execute_notifies_completion_webhook(
        self,
        mock_db,
        mock_execution_manager,
        mock_llm_service,
        mock_webhook_notifier,
        simple_pipeline,
    ) -> None:
        from gobby.workflows.pipeline_executor import PipelineExecutor

        completed_execution = MagicMock()
        completed_execution.id = "pe-test-123"
        completed_execution.status = ExecutionStatus.COMPLETED
        mock_execution_manager.update_execution_status.return_value = completed_execution
        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            webhook_notifier=mock_webhook_notifier,
        )

        result = await executor.execute(
            pipeline=simple_pipeline,
            inputs={},
            project_id="proj-123",
        )

        assert result.status == ExecutionStatus.COMPLETED
        mock_webhook_notifier.notify_complete.assert_awaited_once_with(
            execution=completed_execution,
            pipeline=simple_pipeline,
        )

    async def test_webhook_transport_failure_does_not_change_completed_state(
        self,
        mock_db,
        mock_execution_manager,
        mock_llm_service,
        simple_pipeline,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from gobby.workflows.pipeline_executor import PipelineExecutor

        completed_execution = MagicMock()
        completed_execution.id = "pe-test-123"
        completed_execution.pipeline_name = simple_pipeline.name
        completed_execution.status = ExecutionStatus.COMPLETED
        completed_execution.outputs_json = None
        completed_execution.completed_at = "2026-07-15T12:00:00Z"
        mock_execution_manager.update_execution_status.return_value = completed_execution
        simple_pipeline.webhooks = WebhookConfig(
            on_complete=WebhookEndpoint(url="https://example.com/complete")
        )
        transport = MagicMock(spec=WebhookExecutor)
        transport.execute = AsyncMock(side_effect=ValueError("unsafe webhook target"))
        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            webhook_notifier=WebhookNotifier(
                base_url="https://gobby.local",
                executor=transport,
            ),
        )

        with caplog.at_level("ERROR"):
            result = await executor.execute(
                pipeline=simple_pipeline,
                inputs={},
                project_id="proj-123",
            )

        assert result.status == ExecutionStatus.COMPLETED
        assert "unsafe webhook target" in caplog.text

    async def test_execute_notifies_failure_webhook(
        self,
        mock_db,
        mock_execution_manager,
        mock_llm_service,
        mock_webhook_notifier,
        simple_pipeline,
    ) -> None:
        from gobby.workflows.pipeline_executor import PipelineExecutor

        failed_execution = MagicMock()
        failed_execution.id = "pe-test-123"
        failed_execution.status = ExecutionStatus.FAILED
        mock_execution_manager.update_execution_status.return_value = failed_execution
        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            webhook_notifier=mock_webhook_notifier,
        )
        executor._execute_step = AsyncMock(side_effect=RuntimeError("step failed"))

        with pytest.raises(RuntimeError, match="step failed"):
            await executor.execute(
                pipeline=simple_pipeline,
                inputs={},
                project_id="proj-123",
            )

        mock_webhook_notifier.notify_failure.assert_awaited_once_with(
            execution=failed_execution,
            pipeline=simple_pipeline,
            error="step failed",
        )

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

    @pytest.mark.asyncio
    async def test_execute_stops_before_next_step_when_execution_is_cancelled(
        self, mock_db, mock_execution_manager, mock_llm_service, simple_pipeline
    ) -> None:
        """A persisted cancellation is terminal and must not be overwritten."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        running_execution = MagicMock(id="pe-test-123", status=ExecutionStatus.RUNNING)
        cancelled_execution = MagicMock(id="pe-test-123", status=ExecutionStatus.CANCELLED)
        mock_execution_manager.get_execution.side_effect = [
            running_execution,
            cancelled_execution,
        ]
        mock_execution_manager.update_execution_status.return_value = running_execution
        mock_execution_manager.get_steps_for_execution.return_value = []
        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        result = await executor.execute(
            pipeline=simple_pipeline,
            inputs={},
            project_id="proj-123",
            execution_id="pe-test-123",
        )

        assert result is cancelled_execution
        mock_execution_manager.create_step_execution.assert_not_called()
        statuses = [
            call.kwargs["status"]
            for call in mock_execution_manager.update_execution_status.call_args_list
        ]
        assert statuses == [ExecutionStatus.RUNNING]

    @pytest.mark.asyncio
    async def test_execute_does_not_complete_step_after_cancellation_during_step(
        self, mock_db, mock_execution_manager, mock_llm_service, simple_pipeline
    ) -> None:
        """A cancellation persisted during a shielded step wins over its output."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        running_execution = MagicMock(id="pe-test-123", status=ExecutionStatus.RUNNING)
        cancelled_execution = MagicMock(id="pe-test-123", status=ExecutionStatus.CANCELLED)
        mock_execution_manager.get_execution.side_effect = [
            running_execution,
            running_execution,
            cancelled_execution,
        ]
        mock_execution_manager.update_execution_status.return_value = running_execution
        mock_execution_manager.get_steps_for_execution.return_value = []
        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )
        executor._execute_step = AsyncMock(return_value={"result": "too-late"})

        result = await executor.execute(
            pipeline=simple_pipeline,
            inputs={},
            project_id="proj-123",
            execution_id="pe-test-123",
        )

        assert result is cancelled_execution
        completed_updates = [
            call
            for call in mock_execution_manager.update_step_execution.call_args_list
            if call.kwargs.get("status") == StepStatus.COMPLETED
        ]
        assert completed_updates == []


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

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("timeout_seconds", "inputs", "expected_timeout"),
        [
            (45, {}, 45),
            ("${{ inputs.exec_timeout }}", {"exec_timeout": 12.5}, 12.5),
        ],
    )
    async def test_exec_step_passes_rendered_timeout_to_handler(
        self,
        mock_db,
        mock_execution_manager,
        mock_llm_service,
        timeout_seconds,
        inputs,
        expected_timeout,
    ) -> None:
        """Configured exec timeouts reach the handler without mutating shared context."""
        from gobby.workflows.definitions import PipelineStep
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.templates import TemplateEngine

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            template_engine=TemplateEngine(),
        )
        step = PipelineStep(id="exec", exec="echo ok", timeout_seconds=timeout_seconds)
        context = {"inputs": inputs, "steps": {}}
        handler = AsyncMock(return_value={"exit_code": 0})

        with patch("gobby.workflows.pipeline_executor.execute_exec_step", handler):
            await executor._execute_step(step, context, "project")

        assert handler.await_args.args[1]["timeout_seconds"] == expected_timeout
        assert "timeout_seconds" not in context

    @pytest.mark.asyncio
    async def test_exec_step_omits_timeout_to_preserve_handler_default(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Omitted step timeout leaves the handler's 300-second default in effect."""
        from gobby.workflows.definitions import PipelineStep
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )
        context = {"inputs": {}, "steps": {}}
        observed_timeouts = []
        wait_for = asyncio.wait_for

        async def capture_timeout(awaitable, timeout):
            observed_timeouts.append(timeout)
            return await wait_for(awaitable, timeout=timeout)

        with patch(
            "gobby.workflows.pipeline.handlers.asyncio.wait_for",
            side_effect=capture_timeout,
        ):
            await executor._execute_step(
                PipelineStep(id="exec", exec="echo ok"), context, "project"
            )

        assert observed_timeouts == [300]
        assert "timeout_seconds" not in context

    @pytest.mark.asyncio
    @pytest.mark.parametrize("rendered_timeout", [0, -1, "invalid"])
    async def test_exec_step_rejects_invalid_rendered_timeout(
        self,
        mock_db,
        mock_execution_manager,
        mock_llm_service,
        rendered_timeout,
    ) -> None:
        """Templated exec timeouts are revalidated after rendering."""
        from gobby.workflows.definitions import PipelineStep
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.templates import TemplateEngine

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            template_engine=TemplateEngine(),
        )
        step = PipelineStep(
            id="exec",
            exec="echo ok",
            timeout_seconds="${{ inputs.exec_timeout }}",
        )

        with pytest.raises(ValueError, match="timeout_seconds"):
            await executor._execute_step(
                step,
                {"inputs": {"exec_timeout": rendered_timeout}, "steps": {}},
                "project",
            )


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

        prompt_step_config = PipelineConfig().prompt_step
        mock_llm_service.call_feature.return_value = "LLM response text"

        context: dict = {"inputs": {}, "steps": {}}
        await execute_prompt_step(
            "Analyze this data", context, mock_llm_service, prompt_step_config
        )

        mock_llm_service.call_feature.assert_awaited_once()
        assert mock_llm_service.call_feature.await_args is not None
        assert mock_llm_service.call_feature.await_args.args[0] is prompt_step_config

    @pytest.mark.asyncio
    async def test_prompt_step_returns_response(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that prompt step returns the LLM response."""
        from gobby.workflows.pipeline.handlers import execute_prompt_step

        prompt_step_config = PipelineConfig().prompt_step
        mock_llm_service.call_feature.return_value = "Generated analysis"

        context: dict = {"inputs": {}, "steps": {}}
        result = await execute_prompt_step(
            "Analyze this", context, mock_llm_service, prompt_step_config
        )

        assert result is not None
        assert "response" in result
        assert result["response"] == "Generated analysis"

    @pytest.mark.asyncio
    async def test_prompt_step_passes_prompt_to_llm(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that prompt step passes the prompt text to LLM."""
        from gobby.workflows.pipeline.handlers import execute_prompt_step

        prompt_step_config = PipelineConfig().prompt_step
        mock_llm_service.call_feature.return_value = "Response"

        context: dict = {"inputs": {}, "steps": {}}
        await execute_prompt_step(
            "Generate a report", context, mock_llm_service, prompt_step_config
        )

        # Inspect args/kwargs directly instead of str(call_args) so the test
        # fails cleanly if the call signature changes.
        call_args = mock_llm_service.call_feature.await_args
        actual_prompt = call_args.kwargs.get("prompt")
        if actual_prompt is None and len(call_args.args) > 1:
            actual_prompt = call_args.args[1]
        assert actual_prompt == "Generate a report"
        assert call_args.kwargs["caller"] == "workflows.pipeline.prompt_step"

    @pytest.mark.asyncio
    async def test_prompt_step_handles_llm_error(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that prompt step handles LLM errors gracefully."""
        from gobby.workflows.pipeline.handlers import execute_prompt_step

        prompt_step_config = PipelineConfig().prompt_step
        mock_llm_service.call_feature.side_effect = RuntimeError("LLM API error")

        context: dict = {"inputs": {}, "steps": {}}
        result = await execute_prompt_step(
            "Generate something", context, mock_llm_service, prompt_step_config
        )

        assert result is not None
        assert "error" in result

    @pytest.mark.asyncio
    async def test_prompt_step_returns_dict_structure(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that prompt step returns proper dict structure."""
        from gobby.workflows.pipeline.handlers import execute_prompt_step

        prompt_step_config = PipelineConfig().prompt_step
        mock_llm_service.call_feature.return_value = "Test response"

        context: dict = {"inputs": {}, "steps": {}}
        result = await execute_prompt_step(
            "Test prompt", context, mock_llm_service, prompt_step_config
        )

        assert isinstance(result, dict)
        assert "response" in result

    @pytest.mark.asyncio
    async def test_executor_prompt_step_uses_pipeline_config(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that executor prompt steps use the configured prompt-step feature."""
        from gobby.workflows.definitions import PipelineStep
        from gobby.workflows.pipeline_executor import PipelineExecutor

        pipeline_config = PipelineConfig()
        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            pipeline_config=pipeline_config,
        )

        step = PipelineStep(id="prompt", prompt="Use configured feature")
        context: dict = {"inputs": {}, "steps": {}}
        mock_llm_service.call_feature.return_value = "LLM response"
        result = await executor._execute_step(step, context, "project")

        assert result == {"response": "LLM response"}
        mock_llm_service.call_feature.assert_awaited_once()
        assert mock_llm_service.call_feature.await_args.args[0] is pipeline_config.prompt_step
