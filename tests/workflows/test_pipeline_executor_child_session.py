"""Tests for pipeline child session creation, MCP step session resolution, and renderer parent_session_id.

Split from the test_pipeline_executor monolith (#12210).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.workflows.definitions import MCPStepConfig, PipelineDefinition, PipelineStep

pytestmark = pytest.mark.unit


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
    @pytest.mark.integration
    async def test_mcp_steps_use_storage_backed_child_session_id(
        self, temp_db, mock_llm_service
    ) -> None:
        """Pipeline MCP steps resolve and dispatch against the real stored child session.

        This closes the remaining mock-only gap from #12138 by exercising the
        full PipelineExecutor -> SessionManager -> execute_mcp_step path
        with real DB-backed session rows. The tool proxy still matches
        production shape: tool_proxy.session_manager stays None.
        """
        from gobby.storage.pipelines import LocalPipelineExecutionManager
        from gobby.storage.projects import LocalProjectManager
        from gobby.storage.sessions import SessionManager
        from gobby.workflows.pipeline_executor import PipelineExecutor

        project = LocalProjectManager(temp_db).create("pipeline-storage-backed")
        project_id = project.id
        execution_manager = LocalPipelineExecutionManager(temp_db, project_id=project_id)
        session_manager = SessionManager(temp_db)
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

        inner_pipeline = PipelineDefinition(
            name="inner",
            steps=[PipelineStep(id="inner_step", exec="echo inner")],
        )

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

        assert mock_session_manager.register.call_count == 1


class TestRendererParentSessionId:
    """Tests for parent_session_id in StepRenderer render and eval contexts."""

    def test_render_context_includes_parent_session_id(self) -> None:
        """StepRenderer.render_step() should expose parent_session_id."""
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
