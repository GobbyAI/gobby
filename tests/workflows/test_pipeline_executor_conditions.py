"""Tests for step condition evaluation and default input merging.

Split from the test_pipeline_executor monolith (#12210).
"""

import json

import pytest

from gobby.workflows.definitions import PipelineDefinition, PipelineStep
from gobby.workflows.pipeline_state import StepStatus

pytestmark = pytest.mark.unit


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

        update_calls = mock_execution_manager.update_step_execution.call_args_list
        skipped_calls = [c for c in update_calls if c.kwargs.get("status") == StepStatus.SKIPPED]
        assert len(skipped_calls) >= 1


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
        assert context["inputs"]["city"] == "Memphis"
        assert context["inputs"]["state"] == "AR"
