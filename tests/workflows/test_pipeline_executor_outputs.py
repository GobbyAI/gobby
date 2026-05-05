"""Tests for _build_outputs, _coerce_rendered_value, _emit_event, _notify_completion, _close_pipeline_session.

Split from the test_pipeline_executor monolith (#12210).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.workflows.definitions import PipelineDefinition, PipelineStep

pytestmark = pytest.mark.unit


class TestBuildOutputs:
    """Tests for _build_outputs pipeline output rendering."""

    def test_build_outputs_pure_expression_with_len(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
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
    ) -> None:
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
    ) -> None:
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
    ) -> None:
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
        await executor._emit_event("test", "pe-1")
        assert executor.event_callback is None

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
        await executor._emit_event("test", "pe-1")
        assert callback.await_count == 1

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
        await executor._notify_completion("pe-1", "completed", "test-pipe")
        assert executor.completion_registry is None

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
        await executor._notify_completion("pe-1", "failed", "test-pipe", error="oops")
        assert registry.notify.await_count == 1

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
        executor._close_pipeline_session(None, "caller-1")
        assert executor.session_manager is None

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
        assert sm.update_status.call_count == 1
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
        executor._close_pipeline_session("pipeline-sess", "caller-sess")
        assert sm.update_status.call_count == 1
