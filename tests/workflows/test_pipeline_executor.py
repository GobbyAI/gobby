"""Tests for PipelineExecutor class.

TDD tests for executing pipeline workflows.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.workflows.definitions import PipelineDefinition, PipelineStep
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
            PipelineStep(id="analyze", exec="./analyze.sh"),
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
            PipelineStep(id="process", exec="./process.sh $inputs.target"),
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

        step = PipelineStep(
            id="step2", exec="echo run", condition="${{ inputs.get('command') }}"
        )
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
