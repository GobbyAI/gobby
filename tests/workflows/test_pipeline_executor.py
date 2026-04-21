"""Tests for PipelineExecutor class.

TDD tests for executing pipeline workflows.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.workflows.definitions import MCPStepConfig, PipelineDefinition, PipelineStep
from gobby.workflows.pipeline.renderer import StepRenderer
from gobby.workflows.pipeline_state import ExecutionStatus, StepStatus

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_db():
    """Create a mock database."""
    return MagicMock()


@pytest.fixture
def mock_execution_manager():
    """Create a mock LocalPipelineExecutionManager."""
    manager = MagicMock()
    # Default: create_execution returns a mock execution
    mock_execution = MagicMock()
    mock_execution.id = "pe-test-123"
    mock_execution.status = ExecutionStatus.PENDING
    manager.create_execution.return_value = mock_execution
    manager.get_execution.return_value = mock_execution
    manager.update_execution_status.return_value = mock_execution
    # Default: create_step_execution returns a mock step
    mock_step = MagicMock()
    mock_step.id = 1
    mock_step.status = StepStatus.PENDING
    manager.create_step_execution.return_value = mock_step
    manager.update_step_execution.return_value = mock_step
    manager.get_failed_steps.return_value = []
    return manager


@pytest.fixture
def mock_llm_service():
    """Create a mock LLM service.

    Uses MagicMock (not AsyncMock) because get_default_provider() is sync.
    The provider itself is AsyncMock since generate_text() is async.
    """
    service = MagicMock()
    mock_provider = AsyncMock()
    mock_provider.generate_text.return_value = "LLM response"
    service.get_default_provider.return_value = mock_provider
    return service


@pytest.fixture
def mock_template_engine():
    """Create a mock template engine."""
    engine = MagicMock()
    # Default: render returns the template unchanged
    engine.render.side_effect = lambda template, context: template
    return engine


@pytest.fixture
def mock_webhook_notifier():
    """Create a mock webhook notifier."""
    notifier = AsyncMock()
    return notifier


@pytest.fixture
def simple_pipeline():
    """Create a simple pipeline definition."""
    return PipelineDefinition(
        name="test-pipeline",
        description="A test pipeline",
        steps=[
            PipelineStep(id="step1", exec="echo hello"),
            PipelineStep(id="step2", exec="echo world"),
        ],
    )


@pytest.fixture
def pipeline_with_prompt():
    """Create a pipeline with a prompt step."""
    return PipelineDefinition(
        name="prompt-pipeline",
        steps=[
            PipelineStep(id="analyze", exec="echo analyzing"),
            PipelineStep(id="report", prompt="Generate report from $analyze.output"),
        ],
    )


@pytest.fixture
def pipeline_with_inputs():
    """Create a pipeline with inputs."""
    return PipelineDefinition(
        name="input-pipeline",
        inputs={
            "target": {"type": "string", "description": "Target to process"},
            "mode": {"type": "string", "default": "fast"},
        },
        steps=[
            PipelineStep(id="process", exec="echo processing"),
        ],
    )


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

        # Should get existing execution, not create new one
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

        # Verify inputs were serialized in execution record
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

        # Should create step executions for each step
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

        # Configure mock to return completed status
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

        # First status update should be to RUNNING
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

        # Step status should be updated to RUNNING then COMPLETED
        update_calls = mock_execution_manager.update_step_execution.call_args_list
        # At least some calls should set status to RUNNING or COMPLETED
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

        # At least one call should include output_json
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
        # Use Python to write to stderr (no shell redirection needed)
        result = await execute_exec_step(
            "python -c \"import sys; sys.stderr.write('error\\n')\"", context
        )

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

        # Should have non-zero exit code
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

        # Check the prompt was passed
        call_args = mock_llm_service.get_default_provider.return_value.generate_text.call_args
        assert "Generate a report" in str(call_args)

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

        # Should return error in response
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


class TestExecuteNestedPipeline:
    """Tests for _execute_nested_pipeline() method."""

    @pytest.fixture
    def mock_loader(self):
        """Create a mock workflow loader."""
        loader = MagicMock()
        # Default: return a simple pipeline
        nested_pipeline = PipelineDefinition(
            name="nested-pipeline",
            steps=[PipelineStep(id="nested_step", exec="echo nested")],
        )
        loader.load_pipeline.return_value = nested_pipeline
        return loader

    @pytest.mark.asyncio
    async def test_nested_pipeline_loads_pipeline(
        self, mock_db, mock_execution_manager, mock_llm_service, mock_loader
    ) -> None:
        """Test that nested pipeline loads the referenced pipeline."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )
        executor.loader = mock_loader

        context: dict = {"inputs": {}, "steps": {}}
        await executor._execute_nested_pipeline("child-pipeline", context, "proj-123")

        mock_loader.load_pipeline.assert_called_once_with("child-pipeline")

    @pytest.mark.asyncio
    async def test_nested_pipeline_returns_dict(
        self, mock_db, mock_execution_manager, mock_llm_service, mock_loader
    ) -> None:
        """Test that nested pipeline returns a dict result."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )
        executor.loader = mock_loader

        context: dict = {"inputs": {}, "steps": {}}
        result = await executor._execute_nested_pipeline("child-pipeline", context, "proj-123")

        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_nested_pipeline_surfaces_child_outputs(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that nested pipeline surfaces child outputs to parent step output."""
        import json

        from gobby.workflows.pipeline_executor import PipelineExecutor

        # Set up async loader returning a simple pipeline
        async_loader = AsyncMock()
        nested_pipeline = PipelineDefinition(
            name="child-pipeline",
            steps=[PipelineStep(id="step1", exec="echo done")],
        )
        async_loader.load_pipeline.return_value = nested_pipeline

        # Set up the mock execution to return outputs_json with child outputs
        child_outputs = {"orchestration_complete": True, "iteration": 3, "session_task": "#42"}
        completed_execution = MagicMock()
        completed_execution.id = "pe-child-456"
        completed_execution.status = ExecutionStatus.COMPLETED
        completed_execution.outputs_json = json.dumps(child_outputs)
        mock_execution_manager.update_execution_status.return_value = completed_execution

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )
        executor.loader = async_loader

        context: dict = {"inputs": {}, "steps": {}}
        result = await executor._execute_nested_pipeline("child-pipeline", context, "proj-123")

        # Wrapper fields present
        assert result["pipeline"] == "child-pipeline", f"Unexpected result: {result}"
        assert result["execution_id"] == "pe-child-456"
        assert result["status"] == "completed"
        # Child outputs namespaced under "output"
        assert result["output"]["orchestration_complete"] is True
        assert result["output"]["iteration"] == 3
        assert result["output"]["session_task"] == "#42"

    @pytest.mark.asyncio
    async def test_nested_pipeline_handles_not_found(
        self, mock_db, mock_execution_manager, mock_llm_service, mock_loader
    ) -> None:
        """Test that nested pipeline handles missing pipeline gracefully."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        mock_loader.load_pipeline.return_value = None

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )
        executor.loader = mock_loader

        context: dict = {"inputs": {}, "steps": {}}
        result = await executor._execute_nested_pipeline(
            "nonexistent-pipeline", context, "proj-123"
        )

        assert "error" in result

    @pytest.mark.asyncio
    async def test_nested_pipeline_without_loader(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that nested pipeline returns placeholder without loader."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )
        # No loader set

        context: dict = {"inputs": {}, "steps": {}}
        result = await executor._execute_nested_pipeline("child-pipeline", context, "proj-123")

        # Should indicate nested execution not available
        assert "error" in result or "pipeline" in result


class TestExecuteNestedPipelineDictForm:
    """Tests for dict-form invoke_pipeline handling (Bug fixes #9358)."""

    @pytest.fixture
    def mock_loader(self):
        """Create a mock async workflow loader."""
        loader = MagicMock()
        nested_pipeline = PipelineDefinition(
            name="command-listener",
            steps=[PipelineStep(id="nested_step", exec="echo nested")],
        )
        loader.load_pipeline = AsyncMock(return_value=nested_pipeline)
        return loader

    @pytest.mark.asyncio
    async def test_dict_form_extracts_pipeline_name(
        self, mock_db, mock_execution_manager, mock_llm_service, mock_loader
    ) -> None:
        """Bug 2: Dict-form invoke_pipeline should extract 'name' for loader."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )
        executor.loader = mock_loader

        pipeline_ref = {
            "name": "command-listener",
            "arguments": {"parent_session_id": "sess-123", "_current_iteration": 1},
        }
        context: dict = {"inputs": {}, "steps": {}}
        await executor._execute_nested_pipeline(pipeline_ref, context, "proj-123")

        mock_loader.load_pipeline.assert_called_once_with("command-listener")

    @pytest.mark.asyncio
    async def test_dict_form_uses_explicit_arguments(
        self, mock_db, mock_execution_manager, mock_llm_service, mock_loader
    ) -> None:
        """Bug 3: Explicit arguments from dict should be used as nested inputs."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )
        executor.loader = mock_loader

        explicit_args = {"parent_session_id": "sess-123", "_current_iteration": 1}
        pipeline_ref = {"name": "command-listener", "arguments": explicit_args}
        context: dict = {
            "inputs": {"parent_session_id": "sess-000", "_current_iteration": 0},
            "steps": {},
        }
        await executor._execute_nested_pipeline(pipeline_ref, context, "proj-123")

        # Verify execute() was called with explicit args, not parent inputs
        call_args = mock_execution_manager.create_execution.call_args
        inputs_json = call_args.kwargs.get("inputs_json") or call_args[1].get("inputs_json")
        if inputs_json:
            saved_inputs = json.loads(inputs_json)
            # The nested execution should use explicit_args, not parent inputs
            assert saved_inputs.get("_current_iteration") == 1
            assert saved_inputs.get("parent_session_id") == "sess-123"

    @pytest.mark.asyncio
    async def test_dict_form_falls_back_to_parent_inputs_without_arguments(
        self, mock_db, mock_execution_manager, mock_llm_service, mock_loader
    ) -> None:
        """Dict without 'arguments' key should inherit parent inputs."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )
        executor.loader = mock_loader

        pipeline_ref = {"name": "command-listener"}
        parent_inputs = {"parent_session_id": "sess-000", "_current_iteration": 0}
        context: dict = {"inputs": parent_inputs, "steps": {}}
        await executor._execute_nested_pipeline(pipeline_ref, context, "proj-123")

        call_args = mock_execution_manager.create_execution.call_args
        inputs_json = call_args.kwargs.get("inputs_json") or call_args[1].get("inputs_json")
        if inputs_json:
            saved_inputs = json.loads(inputs_json)
            assert saved_inputs.get("_current_iteration") == 0

    @pytest.mark.asyncio
    async def test_dict_form_propagates_session_id(
        self, mock_db, mock_execution_manager, mock_llm_service, mock_loader
    ) -> None:
        """Bug 4: session_id should be propagated to nested execution."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )
        executor.loader = mock_loader

        pipeline_ref = {"name": "command-listener", "arguments": {"_current_iteration": 1}}
        context: dict = {"inputs": {}, "steps": {}, "session_id": "sess-parent-999"}
        await executor._execute_nested_pipeline(pipeline_ref, context, "proj-123")

        # Verify the execution was created with session_id
        call_args = mock_execution_manager.create_execution.call_args
        session_id = call_args.kwargs.get("session_id") or call_args[1].get("session_id")
        assert session_id == "sess-parent-999"

    @pytest.mark.asyncio
    async def test_string_form_still_works(
        self, mock_db, mock_execution_manager, mock_llm_service, mock_loader
    ) -> None:
        """String-form invoke_pipeline should continue working unchanged."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )
        executor.loader = mock_loader

        context: dict = {"inputs": {"foo": "bar"}, "steps": {}, "session_id": "sess-x"}
        await executor._execute_nested_pipeline("command-listener", context, "proj-123")

        mock_loader.load_pipeline.assert_called_once_with("command-listener")

    @pytest.mark.asyncio
    async def test_dict_form_returns_pipeline_name_in_result(
        self, mock_db, mock_execution_manager, mock_llm_service, mock_loader
    ) -> None:
        """Dict-form result should contain the pipeline name, not the dict."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )
        executor.loader = mock_loader

        pipeline_ref = {"name": "command-listener", "arguments": {}}
        context: dict = {"inputs": {}, "steps": {}}
        result = await executor._execute_nested_pipeline(pipeline_ref, context, "proj-123")

        assert result["pipeline"] == "command-listener"

    @pytest.mark.asyncio
    async def test_dict_form_not_found_uses_name(
        self, mock_db, mock_execution_manager, mock_llm_service, mock_loader
    ) -> None:
        """Dict-form error result should reference pipeline name, not dict."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        mock_loader.load_pipeline.return_value = None
        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )
        executor.loader = mock_loader

        pipeline_ref = {"name": "nonexistent", "arguments": {}}
        context: dict = {"inputs": {}, "steps": {}}
        result = await executor._execute_nested_pipeline(pipeline_ref, context, "proj-123")

        assert result["pipeline"] == "nonexistent"
        assert "error" in result


class TestRendererInvokePipelineDict:
    """Tests for StepRenderer handling of dict-form invoke_pipeline (Bug fix #9358)."""

    @pytest.fixture
    def renderer(self):
        """Create a StepRenderer with a real template engine."""
        from gobby.workflows.templates import TemplateEngine

        engine = TemplateEngine()
        return StepRenderer(engine)

    def test_renders_dict_invoke_pipeline_name(self, renderer) -> None:
        """Bug 1: Template vars in invoke_pipeline dict 'name' should be rendered."""
        step = PipelineStep(
            id="next_iteration",
            invoke_pipeline={"name": "${{ inputs.pipeline_name }}", "arguments": {}},
        )
        context = {"inputs": {"pipeline_name": "command-listener"}, "steps": {}}
        rendered = renderer.render_step(step, context)

        assert rendered.invoke_pipeline["name"] == "command-listener"

    def test_renders_dict_invoke_pipeline_arguments(self, renderer) -> None:
        """Bug 1: Template vars in invoke_pipeline dict 'arguments' should be rendered."""
        step = PipelineStep(
            id="next_iteration",
            invoke_pipeline={
                "name": "command-listener",
                "arguments": {
                    "parent_session_id": "${{ inputs.parent_session_id }}",
                    "_current_iteration": "${{ inputs._current_iteration + 1 }}",
                },
            },
        )
        context = {
            "inputs": {"parent_session_id": "sess-abc", "_current_iteration": 2},
            "steps": {},
        }
        rendered = renderer.render_step(step, context)

        assert rendered.invoke_pipeline["arguments"]["parent_session_id"] == "sess-abc"
        assert rendered.invoke_pipeline["arguments"]["_current_iteration"] == 3

    def test_renders_dict_invoke_pipeline_coerces_types(self, renderer) -> None:
        """Arguments should be type-coerced (string '600' -> int 600)."""
        step = PipelineStep(
            id="next_iteration",
            invoke_pipeline={
                "name": "command-listener",
                "arguments": {"wait_timeout": "${{ inputs.wait_timeout }}"},
            },
        )
        context = {"inputs": {"wait_timeout": 600}, "steps": {}}
        rendered = renderer.render_step(step, context)

        assert rendered.invoke_pipeline["arguments"]["wait_timeout"] == 600

    def test_string_invoke_pipeline_unchanged(self, renderer) -> None:
        """String-form invoke_pipeline should not be affected by dict rendering."""
        step = PipelineStep(id="recurse", invoke_pipeline="some-pipeline")
        context = {"inputs": {}, "steps": {}}
        rendered = renderer.render_step(step, context)

        assert rendered.invoke_pipeline == "some-pipeline"

    def test_renders_session_id_in_arguments(self, renderer) -> None:
        """session_id from context should be available in invoke_pipeline arguments."""
        step = PipelineStep(
            id="next_iteration",
            invoke_pipeline={
                "name": "command-listener",
                "arguments": {"session_ref": "${{ session_id }}"},
            },
        )
        context = {"inputs": {}, "steps": {}, "session_id": "sess-xyz"}
        rendered = renderer.render_step(step, context)

        assert rendered.invoke_pipeline["arguments"]["session_ref"] == "sess-xyz"

    def test_renders_wait_completion_id(self, renderer) -> None:
        """Wait step completion_id template should be rendered."""
        step = PipelineStep(
            id="wait_researcher",
            wait={
                "completion_id": "${{ steps.spawn_researcher.output.run_id }}",
                "timeout": "${{ inputs.wait_timeout }}",
            },
        )
        context = {
            "inputs": {"wait_timeout": 600},
            "steps": {"spawn_researcher": {"output": {"run_id": "run-abc123"}}},
        }
        rendered = renderer.render_step(step, context)

        assert rendered.wait["completion_id"] == "run-abc123"
        assert rendered.wait["timeout"] == 600

    def test_renders_wait_preserves_literal_values(self, renderer) -> None:
        """Wait step with literal values should pass through unchanged."""
        step = PipelineStep(
            id="wait_fixed",
            wait={"completion_id": "fixed-id", "timeout": 300},
        )
        context = {"inputs": {}, "steps": {}}
        rendered = renderer.render_step(step, context)

        assert rendered.wait["completion_id"] == "fixed-id"
        assert rendered.wait["timeout"] == 300


class TestConditionEvaluation:
    """Tests for step condition evaluation."""

    @pytest.mark.asyncio
    async def test_should_run_step_returns_true_without_condition(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that steps without condition always run."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        step = PipelineStep(id="step1", exec="echo test")
        context: dict = {"inputs": {}, "steps": {}}

        result = executor.renderer.should_run_step(step, context)

        assert result is True

    @pytest.mark.asyncio
    async def test_should_run_step_evaluates_true_condition(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that step with true condition returns True."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        step = PipelineStep(id="step1", exec="echo test", condition="${{ True }}")
        context: dict = {"inputs": {}, "steps": {}}

        result = executor.renderer.should_run_step(step, context)

        assert result is True

    @pytest.mark.asyncio
    async def test_should_run_step_evaluates_false_condition(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that step with false condition returns False."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        step = PipelineStep(id="step1", exec="echo test", condition="${{ False }}")
        context: dict = {"inputs": {}, "steps": {}}

        result = executor.renderer.should_run_step(step, context)

        assert result is False

    @pytest.mark.asyncio
    async def test_should_run_step_uses_context_values(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that condition can reference context values."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        step = PipelineStep(
            id="step1", exec="echo test", condition="${{ inputs.get('mode') == 'deploy' }}"
        )
        context: dict = {"inputs": {"mode": "deploy"}, "steps": {}}

        result = executor.renderer.should_run_step(step, context)

        assert result is True

    @pytest.mark.asyncio
    async def test_should_run_step_template_context_false(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that ${{ }} condition with context comparison evaluates to false."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        step = PipelineStep(
            id="step1", exec="echo deploy", condition="${{ inputs.get('count', 0) >= 5 }}"
        )
        context: dict = {"inputs": {"count": 3}, "steps": {}}

        result = executor.renderer.should_run_step(step, context)

        assert result is False

    @pytest.mark.asyncio
    async def test_should_run_step_template_step_output_null(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that ${{ }} condition referencing None step output evaluates to false."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        step = PipelineStep(id="step2", exec="echo run", condition="${{ inputs.get('command') }}")
        context: dict = {"inputs": {"command": None}, "steps": {}}

        result = executor.renderer.should_run_step(step, context)

        assert result is False

    @pytest.mark.asyncio
    async def test_should_run_step_raw_expression_still_works(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that raw expressions without ${{ }} wrapper still work."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        step = PipelineStep(id="step1", exec="echo test", condition="False")
        context: dict = {"inputs": {}, "steps": {}}

        result = executor.renderer.should_run_step(step, context)

        assert result is False

    @pytest.mark.asyncio
    async def test_step_skipped_when_condition_false(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that step is skipped when condition is false."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        # Pipeline with conditional step
        pipeline = PipelineDefinition(
            name="conditional-pipeline",
            steps=[
                PipelineStep(id="always", exec="echo always"),
                PipelineStep(id="conditional", exec="echo conditional", condition="${{ False }}"),
            ],
        )

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        await executor.execute(
            pipeline=pipeline,
            inputs={},
            project_id="proj-123",
        )

        # Check that the conditional step was marked as skipped
        update_calls = mock_execution_manager.update_step_execution.call_args_list
        # Find calls with SKIPPED status
        skipped_calls = [c for c in update_calls if c.kwargs.get("status") == StepStatus.SKIPPED]
        assert len(skipped_calls) >= 1


class TestApprovalGateHandling:
    """Tests for approval gate handling in PipelineExecutor."""

    @pytest.fixture
    def pipeline_with_approval(self):
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

        # Check exception has required fields
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

        # Token should be non-empty and unique-ish (at least 16 chars for URL-safe token)
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

        # Check that step execution was updated with approval token
        update_calls = mock_execution_manager.update_step_execution.call_args_list
        # Find call that sets approval_token
        token_calls = [c for c in update_calls if c.kwargs.get("approval_token") is not None]
        assert len(token_calls) >= 1
        # Token in step record should match exception token
        assert token_calls[-1].kwargs["approval_token"] == exc_info.value.token

    @pytest.mark.asyncio
    async def test_approval_gate_updates_execution_status_to_waiting(
        self, mock_db, mock_execution_manager, mock_llm_service, pipeline_with_approval
    ) -> None:
        """Test that execution status is set to WAITING_APPROVAL when hitting approval gate."""
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.pipeline_state import ApprovalRequired, ExecutionStatus

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

        # Check that execution status was updated to WAITING_APPROVAL
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

        # Check that step status was updated to WAITING_APPROVAL
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

        # Should complete without raising ApprovalRequired
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

        # Should complete without raising ApprovalRequired
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

        # First step (build) should have been executed and completed
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

        # Webhook notifier should have been called
        mock_webhook_notifier.notify_approval_pending.assert_called_once()


class TestApproveMethod:
    """Tests for PipelineExecutor.approve() method."""

    @pytest.fixture
    def pipeline_with_approval(self):
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
        from gobby.workflows.pipeline_state import ExecutionStatus, StepStatus

        # Setup mock to return an execution when searched by token
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

    @pytest.mark.asyncio
    async def test_approve_invalid_token_raises_error(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that approve() raises ValueError for invalid token."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        # Setup mock to return None (token not found)
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
        from gobby.workflows.pipeline_state import ExecutionStatus, StepStatus

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

        # Check that step was marked as approved
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
        from gobby.workflows.pipeline_state import ExecutionStatus, StepStatus

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
        from gobby.workflows.pipeline_state import ExecutionStatus, StepStatus

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
        from gobby.workflows.pipeline_state import ExecutionStatus, StepStatus

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

        # Check that execution status was set to CANCELLED
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
        from gobby.workflows.pipeline_state import ExecutionStatus, StepStatus

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
        from gobby.workflows.pipeline_state import ExecutionStatus, StepStatus

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

        # Check that step was marked with error
        step_calls = mock_execution_manager.update_step_execution.call_args_list
        failed_calls = [c for c in step_calls if c.kwargs.get("status") == StepStatus.FAILED]
        assert len(failed_calls) >= 1


class TestDefaultInputMerging:
    """Tests for merging pipeline default inputs with caller-provided inputs."""

    @pytest.mark.asyncio
    async def test_default_inputs_used_when_caller_omits(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that pipeline definition defaults fill in missing caller inputs."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        pipeline = PipelineDefinition(
            name="defaults-pipeline",
            inputs={"city": "Little Rock", "state": "AR"},
            steps=[PipelineStep(id="step1", exec="echo test")],
        )

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        await executor.execute(pipeline=pipeline, inputs={}, project_id="proj-123")

        # Context inputs should contain the defaults
        step_input_json = mock_execution_manager.create_step_execution.call_args.kwargs[
            "input_json"
        ]
        context = json.loads(step_input_json)
        assert context["inputs"]["city"] == "Little Rock"
        assert context["inputs"]["state"] == "AR"

    @pytest.mark.asyncio
    async def test_caller_inputs_override_defaults(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that caller-provided inputs take precedence over defaults."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        pipeline = PipelineDefinition(
            name="defaults-pipeline",
            inputs={"city": "Little Rock", "state": "AR"},
            steps=[PipelineStep(id="step1", exec="echo test")],
        )

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )

        await executor.execute(pipeline=pipeline, inputs={"city": "Memphis"}, project_id="proj-123")

        step_input_json = mock_execution_manager.create_step_execution.call_args.kwargs[
            "input_json"
        ]
        context = json.loads(step_input_json)
        assert context["inputs"]["city"] == "Memphis"  # overridden
        assert context["inputs"]["state"] == "AR"  # default preserved


class TestNestedPipelineDepthLimit:
    """Tests for nested pipeline depth limit and cycle detection."""

    @pytest.mark.asyncio
    async def test_nested_pipeline_depth_limit_exceeded(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Exceeding depth limit raises RuntimeError."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        pipeline = PipelineDefinition(
            name="deep-pipeline",
            steps=[PipelineStep(id="s1", exec="echo hi")],
        )
        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )
        with pytest.raises(RuntimeError, match="nesting depth limit exceeded"):
            await executor.execute(
                pipeline=pipeline,
                inputs={},
                project_id="proj-123",
                _depth=11,
            )

    @pytest.mark.asyncio
    async def test_cross_pipeline_cycle_detection(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """A cross-pipeline cycle (A->B->A) raises RuntimeError."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        pipeline = PipelineDefinition(
            name="pipeline-a",
            steps=[PipelineStep(id="s1", exec="echo hi")],
        )
        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )
        with pytest.raises(RuntimeError, match="Pipeline cycle detected"):
            await executor.execute(
                pipeline=pipeline,
                inputs={},
                project_id="proj-123",
                _pipeline_stack=frozenset({"pipeline-a", "pipeline-b"}),
            )

    @pytest.mark.asyncio
    async def test_self_recursion_allowed(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Self-recursion (A->A) is allowed, bounded by depth limit."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        pipeline = PipelineDefinition(
            name="orchestrator",
            steps=[PipelineStep(id="s1", exec="echo hi")],
        )
        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )
        # Self-recursion should NOT raise cycle error
        try:
            await executor.execute(
                pipeline=pipeline,
                inputs={},
                project_id="proj-123",
                _pipeline_stack=frozenset({"orchestrator"}),
            )
        except RuntimeError as e:
            assert "cycle detected" not in str(e).lower()

    @pytest.mark.asyncio
    async def test_nested_pipeline_within_limit(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Depth within limit executes normally."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        pipeline = PipelineDefinition(
            name="ok-pipeline",
            steps=[PipelineStep(id="s1", exec="echo hi")],
        )
        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )
        # depth=5 is well within default limit of 10
        result = await executor.execute(
            pipeline=pipeline,
            inputs={},
            project_id="proj-123",
            _depth=5,
        )
        assert result is not None


class TestPipelineChildSession:
    """Tests for child session creation in pipeline execution."""

    @pytest.mark.asyncio
    async def test_execute_creates_child_session(
        self, mock_db, mock_execution_manager, mock_llm_service, simple_pipeline
    ) -> None:
        """Top-level pipeline creates a child session via session_manager."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        mock_session_manager = MagicMock()
        child_session = MagicMock()
        child_session.id = "child-session-123"
        mock_session_manager.register.return_value = child_session

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            session_manager=mock_session_manager,
        )

        await executor.execute(
            pipeline=simple_pipeline,
            inputs={},
            project_id="proj-123",
            session_id="caller-session-456",
        )

        mock_session_manager.register.assert_called_once()
        call_kwargs = mock_session_manager.register.call_args.kwargs
        assert call_kwargs["parent_session_id"] == "caller-session-456"
        assert call_kwargs["source"] == "pipeline"
        assert "pipeline-" in call_kwargs["external_id"]
        assert call_kwargs["title"] == "pipeline:test-pipeline"
        assert call_kwargs["agent_depth"] == 0

    @pytest.mark.asyncio
    async def test_execute_seeds_enforce_tool_schema_check_false(
        self, mock_db, mock_execution_manager, mock_llm_service, simple_pipeline, monkeypatch
    ) -> None:
        """Pipeline child session is seeded with enforce_tool_schema_check=False.

        Guards #12135: pipeline steps must be exempt from require-schema-before-call
        via a session-scoped variable, so the rule short-circuits before source
        resolution (which races against the just-written session row).
        """
        from gobby.workflows import pipeline_executor as pe_module
        from gobby.workflows.pipeline_executor import PipelineExecutor

        mock_session_manager = MagicMock()
        child_session = MagicMock()
        child_session.id = "child-session-exempt"
        mock_session_manager.register.return_value = child_session
        mock_session_manager.db = MagicMock()

        captured = MagicMock()
        monkeypatch.setattr(
            "gobby.workflows.state_manager.SessionVariableManager",
            lambda db: captured,
        )

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            session_manager=mock_session_manager,
        )

        await executor.execute(
            pipeline=simple_pipeline,
            inputs={},
            project_id="proj-123",
            session_id="caller-session-456",
        )

        captured.merge_variables.assert_called_once_with(
            "child-session-exempt",
            {"enforce_tool_schema_check": False},
        )
        assert pe_module is not None  # import guard — catches accidental module rename

    @pytest.mark.asyncio
    async def test_context_session_id_is_child(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Context session_id should be the child session, not the caller."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        mock_session_manager = MagicMock()
        child_session = MagicMock()
        child_session.id = "child-session-abc"
        mock_session_manager.register.return_value = child_session

        captured_context: dict = {}

        # Use a pipeline with a single step so we can capture the context
        pipeline = PipelineDefinition(
            name="ctx-test",
            steps=[PipelineStep(id="s1", exec="echo hi")],
        )

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            session_manager=mock_session_manager,
        )

        # Patch _execute_step to capture context
        original_execute_step = executor._execute_step

        async def capture_context(step, context, project_id):
            captured_context.update(context)
            return await original_execute_step(step, context, project_id)

        executor._execute_step = capture_context

        await executor.execute(
            pipeline=pipeline,
            inputs={},
            project_id="proj-123",
            session_id="caller-session-789",
        )

        assert captured_context["session_id"] == "child-session-abc"

    @pytest.mark.asyncio
    async def test_context_has_parent_session_id(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Context parent_session_id should be the original caller."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        mock_session_manager = MagicMock()
        child_session = MagicMock()
        child_session.id = "child-session-xyz"
        mock_session_manager.register.return_value = child_session

        captured_context: dict = {}

        pipeline = PipelineDefinition(
            name="parent-test",
            steps=[PipelineStep(id="s1", exec="echo hi")],
        )

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            session_manager=mock_session_manager,
        )

        async def capture_context(step, context, project_id):
            captured_context.update(context)
            return {"stdout": "hi", "stderr": "", "exit_code": 0}

        executor._execute_step = capture_context

        await executor.execute(
            pipeline=pipeline,
            inputs={},
            project_id="proj-123",
            session_id="original-caller",
        )

        assert captured_context["parent_session_id"] == "original-caller"

    @pytest.mark.asyncio
    async def test_mcp_steps_use_child_session_id(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Pipeline MCP steps resolve the child session via the executor-owned session_manager.

        Regression test for #12138: the handler must not rely on
        tool_proxy.session_manager — that attribute is always None in production
        (MCPClientManager never sets it). Both get_tool_schema and call_tool
        must receive the child UUID resolved through PipelineExecutor.session_manager.
        """
        from gobby.workflows.pipeline_executor import PipelineExecutor

        mock_session_manager = MagicMock()
        child_session = MagicMock()
        child_session.id = "child-session-mcp"
        mock_session_manager.register.return_value = child_session
        mock_session_manager.resolve_session_reference.return_value = "child-session-mcp"

        tool_proxy = AsyncMock()
        # Match production shape: MCPClientManager never sets this attribute, so
        # ToolProxyService.session_manager returns None. Keeping this None here
        # locks in that the handler must get its resolver from the executor.
        tool_proxy.session_manager = None
        tool_proxy.get_tool_schema.return_value = {
            "success": True,
            "tool": {"inputSchema": {}},
        }
        tool_proxy.call_tool.return_value = {"success": True, "executions": []}

        pipeline = PipelineDefinition(
            name="mcp-session-test",
            steps=[
                PipelineStep(
                    id="reentry_check",
                    mcp=MCPStepConfig(
                        server="gobby-workflows",
                        tool="list_pipeline_executions",
                    ),
                ),
            ],
        )

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            session_manager=mock_session_manager,
            tool_proxy_getter=lambda: tool_proxy,
        )

        await executor.execute(
            pipeline=pipeline,
            inputs={},
            project_id="proj-123",
            session_id="cron-session-123",
        )

        tool_proxy.get_tool_schema.assert_called_once_with(
            "gobby-workflows",
            "list_pipeline_executions",
            session_id="child-session-mcp",
        )
        tool_proxy.call_tool.assert_called_once_with(
            "gobby-workflows",
            "list_pipeline_executions",
            {},
            session_id="child-session-mcp",
        )

    @pytest.mark.asyncio
    async def test_mcp_steps_use_storage_backed_child_session_id(
        self, temp_db, mock_llm_service
    ) -> None:
        """Pipeline MCP steps resolve and dispatch against the real stored child session.

        This closes the remaining mock-only gap from #12138 by exercising the
        full PipelineExecutor -> LocalSessionManager -> execute_mcp_step path
        with real DB-backed session rows. The tool proxy still matches
        production shape: tool_proxy.session_manager stays None.
        """
        from gobby.storage.pipelines import LocalPipelineExecutionManager
        from gobby.storage.projects import LocalProjectManager
        from gobby.storage.sessions import LocalSessionManager
        from gobby.workflows.pipeline_executor import PipelineExecutor

        project = LocalProjectManager(temp_db).create("pipeline-storage-backed")
        project_id = project.id
        execution_manager = LocalPipelineExecutionManager(temp_db, project_id=project_id)
        session_manager = LocalSessionManager(temp_db)
        caller_session = session_manager.register(
            external_id="caller-ext-storage",
            machine_id="test-machine",
            source="codex",
            project_id=project_id,
            title="caller",
        )

        tool_proxy = AsyncMock()
        tool_proxy.session_manager = None
        tool_proxy.get_tool_schema.return_value = {
            "success": True,
            "tool": {"inputSchema": {}},
        }
        tool_proxy.call_tool.return_value = {"success": True, "executions": []}

        pipeline = PipelineDefinition(
            name="mcp-storage-backed-test",
            steps=[
                PipelineStep(
                    id="reentry_check",
                    mcp=MCPStepConfig(
                        server="gobby-workflows",
                        tool="list_pipeline_executions",
                    ),
                ),
            ],
        )

        executor = PipelineExecutor(
            db=temp_db,
            execution_manager=execution_manager,
            llm_service=mock_llm_service,
            session_manager=session_manager,
            tool_proxy_getter=lambda: tool_proxy,
        )

        execution = await executor.execute(
            pipeline=pipeline,
            inputs={},
            project_id=project_id,
            session_id=caller_session.id,
        )

        child_session = session_manager.find_by_external_id(
            f"pipeline-{execution.id}",
            "pipeline",
            project_id,
            "pipeline",
        )
        assert child_session is not None
        assert child_session.source == "pipeline"

        tool_proxy.get_tool_schema.assert_called_once_with(
            "gobby-workflows",
            "list_pipeline_executions",
            session_id=child_session.id,
        )
        tool_proxy.call_tool.assert_called_once_with(
            "gobby-workflows",
            "list_pipeline_executions",
            {},
            session_id=child_session.id,
        )
        stored_child = session_manager.get(child_session.id)
        assert stored_child is not None
        assert stored_child.source == "pipeline"

    @pytest.mark.asyncio
    async def test_session_id_injected_into_inputs(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """session_id should be auto-injected into inputs so ${{ inputs.session_id }} resolves."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        mock_session_manager = MagicMock()
        child_session = MagicMock()
        child_session.id = "child-session-injected"
        mock_session_manager.register.return_value = child_session

        captured_context: dict = {}

        pipeline = PipelineDefinition(
            name="sid-inject-test",
            steps=[PipelineStep(id="s1", exec="echo hi")],
        )

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            session_manager=mock_session_manager,
        )

        async def capture_context(step, context, project_id):
            captured_context.update(context)
            return {"stdout": "hi", "stderr": "", "exit_code": 0}

        executor._execute_step = capture_context

        await executor.execute(
            pipeline=pipeline,
            inputs={},
            project_id="proj-123",
            session_id="caller-session-orig",
        )

        # session_id in inputs should be the child session (same as context session_id)
        assert captured_context["inputs"]["session_id"] == "child-session-injected"
        assert captured_context["session_id"] == "child-session-injected"

    @pytest.mark.asyncio
    async def test_explicit_session_id_in_inputs_not_overwritten(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Explicit session_id in inputs should not be overwritten by auto-injection."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        mock_session_manager = MagicMock()
        child_session = MagicMock()
        child_session.id = "child-session-auto"
        mock_session_manager.register.return_value = child_session

        captured_context: dict = {}

        pipeline = PipelineDefinition(
            name="sid-explicit-test",
            steps=[PipelineStep(id="s1", exec="echo hi")],
        )

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            session_manager=mock_session_manager,
        )

        async def capture_context(step, context, project_id):
            captured_context.update(context)
            return {"stdout": "hi", "stderr": "", "exit_code": 0}

        executor._execute_step = capture_context

        await executor.execute(
            pipeline=pipeline,
            inputs={"session_id": "explicit-session-id"},
            project_id="proj-123",
            session_id="caller-session-orig",
        )

        # Explicit value in inputs should be preserved
        assert captured_context["inputs"]["session_id"] == "explicit-session-id"

    @pytest.mark.asyncio
    async def test_no_child_session_without_session_manager(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Without session_manager, session_id passes through unchanged."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        captured_context: dict = {}

        pipeline = PipelineDefinition(
            name="no-mgr-test",
            steps=[PipelineStep(id="s1", exec="echo hi")],
        )

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            # No session_manager
        )

        async def capture_context(step, context, project_id):
            captured_context.update(context)
            return {"stdout": "hi", "stderr": "", "exit_code": 0}

        executor._execute_step = capture_context

        await executor.execute(
            pipeline=pipeline,
            inputs={},
            project_id="proj-123",
            session_id="caller-direct",
        )

        assert captured_context["session_id"] == "caller-direct"

    @pytest.mark.asyncio
    async def test_fallback_to_system_session_without_session_id(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Without session_id, pipeline falls back to SYSTEM_SESSION_ID as parent."""
        from gobby.storage.sessions import SYSTEM_SESSION_ID
        from gobby.workflows.pipeline_executor import PipelineExecutor

        mock_session_manager = MagicMock()
        child_session = MagicMock()
        child_session.id = "child-of-system"
        mock_session_manager.register.return_value = child_session

        pipeline = PipelineDefinition(
            name="no-sid-test",
            steps=[PipelineStep(id="s1", exec="echo hi")],
        )

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            session_manager=mock_session_manager,
        )

        await executor.execute(
            pipeline=pipeline,
            inputs={},
            project_id="proj-123",
            # No session_id — should fall back to SYSTEM_SESSION_ID
        )

        mock_session_manager.register.assert_called_once()
        call_kwargs = mock_session_manager.register.call_args[1]
        assert call_kwargs["parent_session_id"] == SYSTEM_SESSION_ID

    @pytest.mark.asyncio
    async def test_nested_pipeline_inherits_session(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Nested pipelines (depth > 0) should NOT create a new child session."""
        from gobby.workflows.pipeline_executor import PipelineExecutor

        mock_session_manager = MagicMock()
        child_session = MagicMock()
        child_session.id = "child-session-top"
        mock_session_manager.register.return_value = child_session

        captured_contexts: list[dict] = []

        # Inner pipeline that the outer will invoke
        inner_pipeline = PipelineDefinition(
            name="inner",
            steps=[PipelineStep(id="inner_step", exec="echo inner")],
        )

        # Outer pipeline that invokes inner
        outer_pipeline = PipelineDefinition(
            name="outer",
            steps=[
                PipelineStep(id="nest", invoke_pipeline="inner"),
            ],
        )

        mock_loader = AsyncMock()
        mock_loader.load_pipeline.return_value = inner_pipeline

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            session_manager=mock_session_manager,
            loader=mock_loader,
        )

        # Patch _execute_step to capture context at each level
        original_execute_step = executor._execute_step

        async def capture_and_execute(step, context, project_id):
            captured_contexts.append(dict(context))
            return await original_execute_step(step, context, project_id)

        executor._execute_step = capture_and_execute

        await executor.execute(
            pipeline=outer_pipeline,
            inputs={},
            project_id="proj-123",
            session_id="caller-outer",
        )

        # register should be called exactly once (for the outer pipeline only)
        assert mock_session_manager.register.call_count == 1


class TestRendererParentSessionId:
    """Tests for parent_session_id in StepRenderer render and eval contexts."""

    def test_render_context_includes_parent_session_id(self) -> None:
        """StepRenderer.render_step() should expose parent_session_id."""
        from unittest.mock import MagicMock

        from gobby.workflows.pipeline.renderer import StepRenderer

        mock_engine = MagicMock()
        mock_engine.render.side_effect = lambda t, ctx: ctx.get("parent_session_id", "")

        renderer = StepRenderer(template_engine=mock_engine)
        step = MagicMock()
        step.id = "test"
        step.exec = "${{ parent_session_id }}"
        step.prompt = None
        step.mcp = None
        step.invoke_pipeline = None
        step.model_copy.return_value = step

        context = {
            "inputs": {},
            "steps": {},
            "session_id": "child-123",
            "parent_session_id": "parent-456",
        }

        renderer.render_step(step, context)

        # Verify the render call received parent_session_id in context
        render_call_ctx = mock_engine.render.call_args[0][1]
        assert render_call_ctx["parent_session_id"] == "parent-456"
        assert render_call_ctx["session_id"] == "child-123"

    def test_eval_context_includes_parent_session_id(self) -> None:
        """StepRenderer.should_run_step() should include parent_session_id in eval context."""
        from gobby.workflows.pipeline.renderer import StepRenderer

        renderer = StepRenderer()
        step = MagicMock()
        step.condition = "${{ parent_session_id == 'parent-789' }}"

        context = {
            "inputs": {},
            "steps": {},
            "session_id": "child-abc",
            "parent_session_id": "parent-789",
        }

        result = renderer.should_run_step(step, context)
        assert result is True

    def test_eval_context_parent_session_id_mismatch(self) -> None:
        """Condition using parent_session_id should evaluate correctly on mismatch."""
        from gobby.workflows.pipeline.renderer import StepRenderer

        renderer = StepRenderer(strict_conditions=True)
        step = MagicMock()
        step.condition = "${{ parent_session_id == 'wrong-id' }}"

        context = {
            "inputs": {},
            "steps": {},
            "session_id": "child-abc",
            "parent_session_id": "parent-789",
        }

        result = renderer.should_run_step(step, context)
        assert result is False


class TestBuildOutputs:
    """Tests for _build_outputs pipeline output rendering."""

    def test_build_outputs_pure_expression_with_len(
        self, mock_db, mock_execution_manager, mock_llm_service
    ):
        """Pipeline outputs with len() in pure ${{ }} expressions should evaluate correctly.

        Regression test: _build_outputs was routing all ${{ }} expressions through
        Jinja2 (which lacks len), instead of SafeExpressionEvaluator (which has it).
        """
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.templates import TemplateEngine

        pipeline = PipelineDefinition(
            name="test-outputs",
            steps=[PipelineStep(id="scan", exec="echo test")],
            outputs={
                "task_count": "${{ len(scan.output.tasks) }}",
                "has_tasks": "${{ len(scan.output.tasks) > 0 }}",
            },
        )

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            template_engine=TemplateEngine(),
        )

        context = {
            "inputs": {},
            "steps": {
                "scan": {
                    "status": "completed",
                    "output": {"tasks": ["t1", "t2", "t3"]},
                },
            },
        }

        outputs = executor._build_outputs(pipeline, context)
        assert outputs["task_count"] == 3
        assert outputs["has_tasks"] is True

    def test_build_outputs_mixed_string_uses_jinja2(
        self, mock_db, mock_execution_manager, mock_llm_service
    ):
        """Mixed strings (not pure ${{ }}) should still use Jinja2 rendering."""
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.templates import TemplateEngine

        pipeline = PipelineDefinition(
            name="test-outputs",
            steps=[PipelineStep(id="step1", exec="echo test")],
            outputs={
                "message": "Result: ${{ step1.output.value }}",
            },
        )

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            template_engine=TemplateEngine(),
        )

        context = {
            "inputs": {},
            "steps": {
                "step1": {
                    "status": "completed",
                    "output": {"value": "hello"},
                },
            },
        }

        outputs = executor._build_outputs(pipeline, context)
        assert outputs["message"] == "Result: hello"

    def test_build_outputs_conditional_expression_with_len(
        self, mock_db, mock_execution_manager, mock_llm_service
    ):
        """Conditional expressions with len() should work in pipeline outputs.

        This matches the orchestrator pattern:
          open_count: "${{ len(scan.output.tasks) if scan.output else 0 }}"
        """
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.templates import TemplateEngine

        pipeline = PipelineDefinition(
            name="test-outputs",
            steps=[PipelineStep(id="scan", exec="echo test")],
            outputs={
                "count": "${{ len(scan.output.tasks) if scan.output else 0 }}",
            },
        )

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            template_engine=TemplateEngine(),
        )

        context = {
            "inputs": {},
            "steps": {
                "scan": {
                    "status": "completed",
                    "output": {"tasks": ["a", "b"]},
                },
            },
        }

        outputs = executor._build_outputs(pipeline, context)
        assert outputs["count"] == 2

    def test_build_outputs_any_all_in_expressions(
        self, mock_db, mock_execution_manager, mock_llm_service
    ):
        """any() and all() should work in pipeline expressions.

        Regression test for orchestrator qa_gate failure:
          any(a.get('provider') == 'claude' for a in agents)
        """
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.templates import TemplateEngine

        pipeline = PipelineDefinition(
            name="test-any-all",
            steps=[PipelineStep(id="check", exec="echo test")],
            outputs={
                "has_claude": "${{ any(a.get('provider') == 'claude' for a in check.output.agents) }}",
                "all_claude": "${{ all(a.get('provider') == 'claude' for a in check.output.agents) }}",
            },
        )

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            template_engine=TemplateEngine(),
        )

        context = {
            "inputs": {},
            "steps": {
                "check": {
                    "status": "completed",
                    "output": {
                        "agents": [
                            {"provider": "gemini"},
                            {"provider": "claude"},
                        ]
                    },
                },
            },
        }

        outputs = executor._build_outputs(pipeline, context)
        assert outputs["has_claude"] is True
        assert outputs["all_claude"] is False


# ═══════════════════════════════════════════════════════════════════════
# Additional coverage: _coerce_rendered_value
# ═══════════════════════════════════════════════════════════════════════


class TestCoerceRenderedValue:
    """Tests for _coerce_rendered_value type coercion."""

    def test_coerce_true(self) -> None:
        from gobby.workflows.pipeline_executor import _coerce_rendered_value

        assert _coerce_rendered_value("True") is True
        assert _coerce_rendered_value("  true  ") is True

    def test_coerce_false(self) -> None:
        from gobby.workflows.pipeline_executor import _coerce_rendered_value

        assert _coerce_rendered_value("False") is False
        assert _coerce_rendered_value("  FALSE  ") is False

    def test_coerce_none(self) -> None:
        from gobby.workflows.pipeline_executor import _coerce_rendered_value

        assert _coerce_rendered_value("None") is None
        assert _coerce_rendered_value("  none  ") is None

    def test_coerce_integer(self) -> None:
        from gobby.workflows.pipeline_executor import _coerce_rendered_value

        assert _coerce_rendered_value("42") == 42
        assert _coerce_rendered_value("-1") == -1

    def test_coerce_float(self) -> None:
        from gobby.workflows.pipeline_executor import _coerce_rendered_value

        assert _coerce_rendered_value("3.14") == 3.14
        assert _coerce_rendered_value("-0.5") == -0.5

    def test_coerce_passthrough_string(self) -> None:
        from gobby.workflows.pipeline_executor import _coerce_rendered_value

        assert _coerce_rendered_value("hello") == "hello"

    def test_coerce_non_string_passthrough(self) -> None:
        from gobby.workflows.pipeline_executor import _coerce_rendered_value

        assert _coerce_rendered_value(42) == 42
        assert _coerce_rendered_value([1, 2]) == [1, 2]
        assert _coerce_rendered_value(None) is None


# ═══════════════════════════════════════════════════════════════════════
# Additional coverage: _emit_event error handling
# ═══════════════════════════════════════════════════════════════════════


class TestEmitEvent:
    """Tests for _emit_event error suppression."""

    @pytest.mark.asyncio
    async def test_emit_event_no_callback(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )
        # No event_callback - should not raise
        await executor._emit_event("test", "pe-1")

    @pytest.mark.asyncio
    async def test_emit_event_callback_error_suppressed(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        from gobby.workflows.pipeline_executor import PipelineExecutor

        callback = AsyncMock(side_effect=RuntimeError("callback failed"))
        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            event_callback=callback,
        )
        # Should not raise
        await executor._emit_event("test", "pe-1")

    @pytest.mark.asyncio
    async def test_emit_event_calls_callback(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        from gobby.workflows.pipeline_executor import PipelineExecutor

        callback = AsyncMock()
        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            event_callback=callback,
        )
        await executor._emit_event("pipeline_started", "pe-1", step_count=3)
        callback.assert_called_once_with("pipeline_started", "pe-1", step_count=3)


# ═══════════════════════════════════════════════════════════════════════
# Additional coverage: _notify_completion
# ═══════════════════════════════════════════════════════════════════════


class TestNotifyCompletion:
    """Tests for _notify_completion."""

    @pytest.mark.asyncio
    async def test_no_registry_does_nothing(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )
        # No completion_registry - should not raise
        await executor._notify_completion("pe-1", "completed", "test-pipe")

    @pytest.mark.asyncio
    async def test_notify_with_outputs(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        from gobby.workflows.pipeline_executor import PipelineExecutor

        registry = AsyncMock()
        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            completion_registry=registry,
        )
        await executor._notify_completion(
            "pe-1", "completed", "test-pipe", outputs={"result": "ok"}
        )
        registry.notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_notify_with_orchestration_complete(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        from gobby.workflows.pipeline_executor import PipelineExecutor

        registry = AsyncMock()
        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            completion_registry=registry,
        )
        outputs = {
            "orchestration_complete": "true",
            "session_task": "#42",
            "iteration": 3,
        }
        await executor._notify_completion("pe-1", "completed", "test-pipe", outputs=outputs)
        call_args = registry.notify.call_args
        assert "Orchestration complete" in call_args.kwargs.get("message", "")

    @pytest.mark.asyncio
    async def test_notify_error_suppressed(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        from gobby.workflows.pipeline_executor import PipelineExecutor

        registry = AsyncMock()
        registry.notify.side_effect = RuntimeError("boom")
        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            completion_registry=registry,
        )
        # Should not raise
        await executor._notify_completion("pe-1", "failed", "test-pipe", error="oops")

    @pytest.mark.asyncio
    async def test_notify_with_error_field(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        from gobby.workflows.pipeline_executor import PipelineExecutor

        registry = AsyncMock()
        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            completion_registry=registry,
        )
        await executor._notify_completion("pe-1", "failed", "test-pipe", error="step failed")
        call_args = registry.notify.call_args
        result = call_args[0][1]
        assert result["error"] == "step failed"


# ═══════════════════════════════════════════════════════════════════════
# Additional coverage: _close_pipeline_session
# ═══════════════════════════════════════════════════════════════════════


class TestClosePipelineSession:
    """Tests for _close_pipeline_session."""

    def test_no_session_id_does_nothing(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        from gobby.workflows.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
        )
        # Should not raise
        executor._close_pipeline_session(None, "caller-1")

    def test_same_session_does_nothing(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        from gobby.workflows.pipeline_executor import PipelineExecutor

        sm = MagicMock()
        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            session_manager=sm,
        )
        executor._close_pipeline_session("sess-1", "sess-1")
        sm.update_status.assert_not_called()

    def test_closes_different_session(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        from gobby.workflows.pipeline_executor import PipelineExecutor

        sm = MagicMock()
        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            session_manager=sm,
        )
        executor._close_pipeline_session("pipeline-sess", "caller-sess")
        sm.update_status.assert_called_once_with("pipeline-sess", "deleted")

    def test_close_session_error_suppressed(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        from gobby.workflows.pipeline_executor import PipelineExecutor

        sm = MagicMock()
        sm.update_status.side_effect = RuntimeError("DB error")
        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            session_manager=sm,
        )
        # Should not raise
        executor._close_pipeline_session("pipeline-sess", "caller-sess")


# ═══════════════════════════════════════════════════════════════════════
# Additional coverage: execute error handling and terminal status
# ═══════════════════════════════════════════════════════════════════════


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


# ═══════════════════════════════════════════════════════════════════════
# Additional coverage: _execute_wait_step
# ═══════════════════════════════════════════════════════════════════════


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
        # Should use 600.0 default
        registry.wait.assert_called_once_with("cid-1", timeout=600.0)


# ═══════════════════════════════════════════════════════════════════════
# Additional coverage: approve and reject
# ═══════════════════════════════════════════════════════════════════════


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
