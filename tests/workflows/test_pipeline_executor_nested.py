"""Tests for nested pipeline execution, dict-form invoke_pipeline, and depth/cycle enforcement.

Split from the test_pipeline_executor monolith (#12210).
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.workflows.definitions import PipelineDefinition, PipelineStep
from gobby.workflows.pipeline.renderer import StepRenderer
from gobby.workflows.pipeline_state import ExecutionStatus

pytestmark = pytest.mark.unit


class TestExecuteNestedPipeline:
    """Tests for _execute_nested_pipeline() method."""

    @pytest.fixture
    def mock_loader(self) -> MagicMock:
        """Create a mock workflow loader."""
        loader = MagicMock()
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
        from gobby.workflows.pipeline_executor import PipelineExecutor

        async_loader = AsyncMock()
        nested_pipeline = PipelineDefinition(
            name="child-pipeline",
            steps=[PipelineStep(id="step1", exec="echo done")],
        )
        async_loader.load_pipeline.return_value = nested_pipeline

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

        assert result["pipeline"] == "child-pipeline", f"Unexpected result: {result}"
        assert result["execution_id"] == "pe-child-456"
        assert result["status"] == "completed"
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

        context: dict = {"inputs": {}, "steps": {}}
        result = await executor._execute_nested_pipeline("child-pipeline", context, "proj-123")

        assert "error" in result or "pipeline" in result


class TestExecuteNestedPipelineDictForm:
    """Tests for dict-form invoke_pipeline handling (Bug fixes #9358)."""

    @pytest.fixture
    def mock_loader(self) -> MagicMock:
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

        call_args = mock_execution_manager.create_execution.call_args
        inputs_json = call_args.kwargs.get("inputs_json") or call_args[1].get("inputs_json")
        assert inputs_json is not None, "create_execution was not called with inputs_json"
        saved_inputs = json.loads(inputs_json)
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
        """Self-recursion (A->A) is allowed, bounded by depth limit.

        We don't require execute to succeed (the test env may raise for other
        reasons — approval gates, depth ceiling, etc). We require only that
        whatever RuntimeError surfaces is NOT the cross-pipeline cycle error.
        """
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
        try:
            await executor.execute(
                pipeline=pipeline,
                inputs={},
                project_id="proj-123",
                _pipeline_stack=frozenset({"orchestrator"}),
            )
        except RuntimeError as exc:
            # Self-recursion must never be classified as a cycle — the depth
            # limit is the only legitimate bound on re-entrancy.
            assert "cycle detected" not in str(exc).lower()
        else:
            # Clean success is also fine; nothing more to assert.
            pass

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
        result = await executor.execute(
            pipeline=pipeline,
            inputs={},
            project_id="proj-123",
            _depth=5,
        )
        assert result is not None
