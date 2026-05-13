"""Tests for MCP step type in pipeline definitions and executor.

Tests MCPStepConfig model, PipelineStep with mcp field,
execute_mcp_step handler, and template rendering with type coercion.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from gobby.workflows.definitions import MCPStepConfig, PipelineStep
from gobby.workflows.pipeline.handlers import execute_mcp_step

pytestmark = pytest.mark.unit


# =============================================================================
# MCPStepConfig model tests
# =============================================================================


class TestMCPStepConfig:
    """Tests for MCPStepConfig Pydantic model."""

    def test_minimal_config(self) -> None:
        """Test creating config with required fields only."""
        config = MCPStepConfig(server="gobby-tasks", tool="suggest_next_task")
        assert config.server == "gobby-tasks"
        assert config.tool == "suggest_next_task"
        assert config.arguments is None

    def test_config_with_arguments(self) -> None:
        """Test creating config with arguments."""
        config = MCPStepConfig(
            server="gobby-agents",
            tool="spawn_agent",
            arguments={"prompt": "Do work", "agent": "developer-gemini", "timeout": 600},
        )
        assert config.server == "gobby-agents"
        assert config.tool == "spawn_agent"
        assert config.arguments["prompt"] == "Do work"
        assert config.arguments["timeout"] == 600

    def test_config_empty_arguments(self) -> None:
        """Test config with explicit empty dict arguments."""
        config = MCPStepConfig(server="s", tool="t", arguments={})
        assert config.arguments == {}

    def test_config_requires_server(self) -> None:
        """Test that server is required."""
        with pytest.raises(ValidationError):
            MCPStepConfig(tool="some_tool")

    def test_config_requires_tool(self) -> None:
        """Test that tool is required."""
        with pytest.raises(ValidationError):
            MCPStepConfig(server="some_server")


# =============================================================================
# PipelineStep with mcp field tests
# =============================================================================


class TestPipelineStepMCP:
    """Tests for PipelineStep with mcp execution type."""

    def test_mcp_step(self) -> None:
        """Test creating a step with mcp field."""
        step = PipelineStep(
            id="find_work",
            mcp=MCPStepConfig(
                server="gobby-tasks",
                tool="suggest_next_task",
                arguments={"parent_task_id": "#123"},
            ),
        )
        assert step.id == "find_work"
        assert step.mcp is not None
        assert step.mcp.server == "gobby-tasks"
        assert step.mcp.tool == "suggest_next_task"
        assert step.exec is None
        assert step.prompt is None
        assert step.invoke_pipeline is None

    def test_mcp_mutually_exclusive_with_exec(self) -> None:
        """Test that mcp and exec are mutually exclusive."""
        with pytest.raises(ValidationError) as exc_info:
            PipelineStep(
                id="invalid",
                exec="echo hello",
                mcp=MCPStepConfig(server="s", tool="t"),
            )
        assert (
            "mutually exclusive" in str(exc_info.value).lower()
            or "only one" in str(exc_info.value).lower()
        )

    def test_mcp_mutually_exclusive_with_prompt(self) -> None:
        """Test that mcp and prompt are mutually exclusive."""
        with pytest.raises(ValidationError) as exc_info:
            PipelineStep(
                id="invalid",
                prompt="Do something",
                mcp=MCPStepConfig(server="s", tool="t"),
            )
        assert (
            "mutually exclusive" in str(exc_info.value).lower()
            or "only one" in str(exc_info.value).lower()
        )

    def test_mcp_mutually_exclusive_with_invoke_pipeline(self) -> None:
        """Test that mcp and invoke_pipeline are mutually exclusive."""
        with pytest.raises(ValidationError) as exc_info:
            PipelineStep(
                id="invalid",
                invoke_pipeline="other-pipeline",
                mcp=MCPStepConfig(server="s", tool="t"),
            )
        assert (
            "mutually exclusive" in str(exc_info.value).lower()
            or "only one" in str(exc_info.value).lower()
        )

    def test_mcp_step_with_condition(self) -> None:
        """Test mcp step with condition."""
        step = PipelineStep(
            id="conditional_mcp",
            mcp=MCPStepConfig(server="s", tool="t"),
            condition="steps.prev.output.task_id",
        )
        assert step.condition is not None
        assert step.mcp is not None


# =============================================================================
# Pipeline executor MCP step execution tests
# =============================================================================


@pytest.fixture
def mock_db():
    return MagicMock()


@pytest.fixture
def mock_execution_manager():
    manager = MagicMock()
    mock_execution = MagicMock()
    mock_execution.id = "pe-test-123"
    mock_step = MagicMock()
    mock_step.id = 1
    manager.create_execution.return_value = mock_execution
    manager.get_execution.return_value = mock_execution
    manager.update_execution_status.return_value = mock_execution
    manager.create_step_execution.return_value = mock_step
    manager.update_step_execution.return_value = mock_step
    manager.get_failed_steps.return_value = []
    return manager


@pytest.fixture
def mock_llm_service():
    return AsyncMock()


@pytest.fixture
def mock_tool_proxy():
    proxy = AsyncMock()
    proxy.get_tool_schema = AsyncMock(return_value={"success": True, "tool": {"inputSchema": {}}})
    proxy.call_tool = AsyncMock(return_value={"success": True, "task_id": "#42"})
    # Default no-op session_manager stub so the helper's resolution branch is a
    # pass-through when tests don't care about external_id lookups.
    proxy.session_manager = None
    return proxy


def _make_session_manager(
    *,
    resolve_to: str | None = None,
    resolve_exc=None,
    external_id: str | None = None,
) -> MagicMock:
    """Build a standalone session_manager stub for execute_mcp_step tests."""
    session_manager = MagicMock()
    session_manager.db = MagicMock()
    if resolve_exc is not None:
        session_manager.resolve_session_reference.side_effect = resolve_exc
    else:
        session_manager.resolve_session_reference.return_value = resolve_to
    session = MagicMock()
    session.external_id = external_id
    session.project_id = "proj-abc"
    session_manager.get.return_value = session
    return session_manager


class TestExecuteMCPStep:
    """Tests for execute_mcp_step handler function."""

    @pytest.mark.asyncio
    async def test_mcp_step_calls_tool_proxy(self, mock_tool_proxy) -> None:
        """Test that MCP step calls tool_proxy.call_tool with correct args."""
        step = PipelineStep(
            id="test_step",
            mcp=MCPStepConfig(
                server="gobby-tasks",
                tool="suggest_next_task",
                arguments={"parent_task_id": "#123"},
            ),
        )

        context: dict = {"inputs": {}, "steps": {}}
        result = await execute_mcp_step(step, context, lambda: mock_tool_proxy)

        mock_tool_proxy.get_tool_schema.assert_not_called()
        mock_tool_proxy.call_tool.assert_called_once_with(
            "gobby-tasks",
            "suggest_next_task",
            {"parent_task_id": "#123"},
            session_id=None,
        )
        # success key is stripped by handler (commit 509f7ad5)
        assert "success" not in result
        assert result["task_id"] == "#42"

    @pytest.mark.asyncio
    async def test_mcp_step_prefetches_schema_for_pipeline_session(self, mock_tool_proxy) -> None:
        """Pipeline MCP steps unlock the target tool before execution."""
        step = PipelineStep(
            id="test_step",
            mcp=MCPStepConfig(server="gobby-workflows", tool="list_pipeline_executions"),
        )

        session_manager = _make_session_manager(resolve_to="pipeline-session-123")
        context: dict = {"inputs": {}, "steps": {}, "session_id": "pipeline-session-123"}
        await execute_mcp_step(
            step, context, lambda: mock_tool_proxy, session_manager=session_manager
        )

        mock_tool_proxy.get_tool_schema.assert_called_once_with(
            "gobby-workflows",
            "list_pipeline_executions",
            session_id="pipeline-session-123",
        )
        assert mock_tool_proxy.get_tool_schema.call_count == 1
        assert mock_tool_proxy.get_tool_schema.call_args is not None
        mock_tool_proxy.call_tool.assert_called_once_with(
            "gobby-workflows",
            "list_pipeline_executions",
            {},
            session_id="pipeline-session-123",
        )
        assert mock_tool_proxy.call_tool.call_count == 1
        assert mock_tool_proxy.call_tool.call_args is not None

    @pytest.mark.asyncio
    async def test_mcp_step_no_arguments(self, mock_tool_proxy) -> None:
        """Test MCP step with no arguments passes empty dict."""
        step = PipelineStep(
            id="test_step",
            mcp=MCPStepConfig(server="gobby-agents", tool="wait_for_agent"),
        )

        context: dict = {"inputs": {}, "steps": {}}
        await execute_mcp_step(step, context, lambda: mock_tool_proxy)

        mock_tool_proxy.get_tool_schema.assert_not_called()
        assert mock_tool_proxy.get_tool_schema.call_count == 0
        assert not mock_tool_proxy.get_tool_schema.called
        mock_tool_proxy.call_tool.assert_called_once_with(
            "gobby-agents", "wait_for_agent", {}, session_id=None
        )
        assert mock_tool_proxy.call_tool.call_count == 1
        assert mock_tool_proxy.call_tool.call_args is not None

    @pytest.mark.asyncio
    async def test_mcp_step_raises_without_tool_proxy_getter(self) -> None:
        """Test that MCP step raises RuntimeError without tool_proxy_getter."""
        step = PipelineStep(
            id="test_step",
            mcp=MCPStepConfig(server="s", tool="t"),
        )

        context: dict = {"inputs": {}, "steps": {}}
        with pytest.raises(RuntimeError, match="requires tool_proxy_getter"):
            await execute_mcp_step(step, context, None)

    @pytest.mark.asyncio
    async def test_mcp_step_raises_when_tool_proxy_returns_none(self) -> None:
        """Test that MCP step raises when tool_proxy_getter returns None."""
        step = PipelineStep(
            id="test_step",
            mcp=MCPStepConfig(server="s", tool="t"),
        )

        context: dict = {"inputs": {}, "steps": {}}
        with pytest.raises(RuntimeError, match="returned None"):
            await execute_mcp_step(step, context, lambda: None)

    @pytest.mark.asyncio
    async def test_execute_mcp_step_resolves_external_id_before_session_context(
        self, mock_tool_proxy
    ) -> None:
        """External_id passed as session_id resolves to platform UUID before dispatch."""
        session_manager = _make_session_manager(
            resolve_to="platform-uuid-999",
            external_id="external-uuid-abc",
        )
        step = PipelineStep(
            id="test_step",
            mcp=MCPStepConfig(server="gobby-workflows", tool="list_pipeline_executions"),
        )

        context: dict = {"inputs": {}, "steps": {}, "session_id": "external-uuid-abc"}
        await execute_mcp_step(
            step, context, lambda: mock_tool_proxy, session_manager=session_manager
        )

        # Schema prefetch and call_tool both see the resolved platform UUID
        assert mock_tool_proxy.get_tool_schema.call_args.kwargs["session_id"] == "platform-uuid-999"
        assert mock_tool_proxy.call_tool.call_args.kwargs["session_id"] == "platform-uuid-999"

    @pytest.mark.asyncio
    async def test_execute_mcp_step_unresolvable_session_id_skips_set_session_context(
        self,
        mock_tool_proxy,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unresolvable session ref logs warning and falls through to the no-session path."""
        import logging as _logging

        session_manager = _make_session_manager(
            resolve_to=None, resolve_exc=ValueError("Session not found")
        )
        mock_warning = MagicMock()
        monkeypatch.setattr("gobby.utils.session_context.logger.warning", mock_warning)
        step = PipelineStep(
            id="test_step",
            mcp=MCPStepConfig(server="gobby-workflows", tool="list_pipeline_executions"),
        )

        context: dict = {"inputs": {}, "steps": {}, "session_id": "bogus-ref"}
        caplog.set_level(_logging.WARNING, logger="gobby.utils.session_context")
        await execute_mcp_step(
            step, context, lambda: mock_tool_proxy, session_manager=session_manager
        )

        assert mock_warning.call_count == 1
        assert "could not resolve session ref" in mock_warning.call_args.args[0]
        assert mock_tool_proxy.get_tool_schema.call_args.kwargs["session_id"] is None
        assert mock_tool_proxy.call_tool.call_args.kwargs["session_id"] is None

    @pytest.mark.asyncio
    async def test_execute_mcp_step_ignores_tool_proxy_session_manager_in_production(
        self, mock_tool_proxy
    ) -> None:
        """The handler must resolve via the session_manager kwarg, not tool_proxy.session_manager.

        In production tool_proxy.session_manager is always None (MCPClientManager
        never sets it). Reading it instead of the executor-owned resolver is what
        caused #12138 — the pipeline child UUID never reached tool_proxy.call_tool.
        """
        # Wrong resolver — would resolve to the parent session and block the call.
        wrong_manager = _make_session_manager(resolve_to="parent-session-WRONG")
        mock_tool_proxy.session_manager = wrong_manager

        # Correct resolver, passed explicitly.
        correct_manager = _make_session_manager(resolve_to="pipeline-child-CORRECT")

        step = PipelineStep(
            id="test_step",
            mcp=MCPStepConfig(server="gobby-workflows", tool="list_pipeline_executions"),
        )
        context: dict = {"inputs": {}, "steps": {}, "session_id": "pipeline-child-ref"}
        await execute_mcp_step(
            step, context, lambda: mock_tool_proxy, session_manager=correct_manager
        )

        # Both dispatch paths must use the kwarg-resolved UUID, not the proxy's.
        assert (
            mock_tool_proxy.get_tool_schema.call_args.kwargs["session_id"]
            == "pipeline-child-CORRECT"
        )
        assert mock_tool_proxy.call_tool.call_args.kwargs["session_id"] == "pipeline-child-CORRECT"
        wrong_manager.resolve_session_reference.assert_not_called()

    @pytest.mark.asyncio
    async def test_mcp_step_raises_on_failure_result(self) -> None:
        """Test that MCP step raises RuntimeError when result has success=False."""
        mock_proxy = AsyncMock()
        mock_proxy.call_tool = AsyncMock(return_value={"success": False, "error": "Tool not found"})

        step = PipelineStep(
            id="failing_step",
            mcp=MCPStepConfig(server="s", tool="missing_tool"),
        )

        context: dict = {"inputs": {}, "steps": {}}
        with pytest.raises(RuntimeError, match="failed"):
            await execute_mcp_step(step, context, lambda: mock_proxy)


class TestMCPStepInPipelineExecute:
    """Tests for MCP step execution within full pipeline execute flow."""

    @pytest.mark.asyncio
    async def test_mcp_step_executes_in_pipeline(
        self, mock_db, mock_execution_manager, mock_llm_service, mock_tool_proxy
    ) -> None:
        """Test that MCP steps execute correctly within the pipeline flow."""
        from gobby.workflows.definitions import PipelineDefinition
        from gobby.workflows.pipeline_executor import PipelineExecutor

        pipeline = PipelineDefinition(
            name="mcp-pipeline",
            steps=[
                PipelineStep(
                    id="mcp_step",
                    mcp=MCPStepConfig(
                        server="gobby-tasks",
                        tool="suggest_next_task",
                    ),
                ),
            ],
        )

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            tool_proxy_getter=lambda: mock_tool_proxy,
        )

        await executor.execute(pipeline=pipeline, inputs={}, project_id="proj-123")

        mock_tool_proxy.call_tool.assert_called_once()
        assert mock_tool_proxy.call_tool.call_count == 1
        assert mock_tool_proxy.call_tool.call_args is not None
        mock_execution_manager.create_step_execution.assert_called_once()
        assert mock_execution_manager.create_step_execution.call_count == 1
        assert mock_execution_manager.create_step_execution.call_args is not None


# =============================================================================
# Template rendering + type coercion tests
# =============================================================================


class TestMCPTemplateRendering:
    """Tests for template rendering in MCP step arguments with type coercion."""

    @pytest.mark.asyncio
    async def test_render_mcp_arguments_with_template(
        self, mock_db, mock_execution_manager, mock_llm_service, mock_tool_proxy
    ) -> None:
        """Test that ${{ }} templates are rendered in MCP arguments."""
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.templates import TemplateEngine

        template_engine = TemplateEngine()

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            template_engine=template_engine,
            tool_proxy_getter=lambda: mock_tool_proxy,
        )

        step = PipelineStep(
            id="templated_step",
            mcp=MCPStepConfig(
                server="gobby-agents",
                tool="spawn_agent",
                arguments={
                    "prompt": "Work on ${{ inputs.task_title }}",
                    "timeout": "${{ inputs.wait_timeout }}",
                },
            ),
        )

        context: dict = {
            "inputs": {"task_title": "Fix bug #42", "wait_timeout": "600"},
            "steps": {},
        }

        rendered = executor.renderer.render_step(step, context)

        # String value should be rendered
        assert rendered.mcp.arguments["prompt"] == "Work on Fix bug #42"
        # Numeric string should be coerced to int
        assert rendered.mcp.arguments["timeout"] == 600
        assert isinstance(rendered.mcp.arguments["timeout"], int)

    @pytest.mark.asyncio
    async def test_coerce_boolean_values(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that boolean strings are coerced to bool."""
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.templates import TemplateEngine

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            template_engine=TemplateEngine(),
        )

        step = PipelineStep(
            id="bool_step",
            mcp=MCPStepConfig(
                server="s",
                tool="t",
                arguments={
                    "force": "${{ inputs.force_flag }}",
                    "verbose": "${{ inputs.verbose }}",
                },
            ),
        )

        context: dict = {
            "inputs": {"force_flag": "true", "verbose": "false"},
            "steps": {},
        }

        rendered = executor.renderer.render_step(step, context)
        assert rendered.mcp.arguments["force"] is True
        assert rendered.mcp.arguments["verbose"] is False

    @pytest.mark.asyncio
    async def test_coerce_null_values(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that null/none strings are coerced to None."""
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.templates import TemplateEngine

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            template_engine=TemplateEngine(),
        )

        step = PipelineStep(
            id="null_step",
            mcp=MCPStepConfig(
                server="s",
                tool="t",
                arguments={"param": "${{ inputs.maybe_null }}"},
            ),
        )

        context: dict = {
            "inputs": {"maybe_null": "null"},
            "steps": {},
        }

        rendered = executor.renderer.render_step(step, context)
        assert rendered.mcp.arguments["param"] is None

    @pytest.mark.asyncio
    async def test_coerce_float_values(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that float strings are coerced to float."""
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.templates import TemplateEngine

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            template_engine=TemplateEngine(),
        )

        step = PipelineStep(
            id="float_step",
            mcp=MCPStepConfig(
                server="s",
                tool="t",
                arguments={"ratio": "${{ inputs.ratio }}"},
            ),
        )

        context: dict = {
            "inputs": {"ratio": "0.75"},
            "steps": {},
        }

        rendered = executor.renderer.render_step(step, context)
        assert rendered.mcp.arguments["ratio"] == 0.75
        assert isinstance(rendered.mcp.arguments["ratio"], float)

    @pytest.mark.asyncio
    async def test_nested_dict_arguments_rendered(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that nested dict arguments are recursively rendered."""
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.templates import TemplateEngine

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            template_engine=TemplateEngine(),
        )

        step = PipelineStep(
            id="nested_step",
            mcp=MCPStepConfig(
                server="s",
                tool="t",
                arguments={
                    "outer": {
                        "inner_str": "${{ inputs.name }}",
                        "inner_num": "${{ inputs.count }}",
                    }
                },
            ),
        )

        context: dict = {
            "inputs": {"name": "test", "count": "5"},
            "steps": {},
        }

        rendered = executor.renderer.render_step(step, context)
        assert rendered.mcp.arguments["outer"]["inner_str"] == "test"
        assert rendered.mcp.arguments["outer"]["inner_num"] == 5

    @pytest.mark.asyncio
    async def test_pure_expression_preserves_list(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that a pure ${{ expr }} returning a list preserves the list type."""
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.templates import TemplateEngine

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            template_engine=TemplateEngine(),
        )

        step = PipelineStep(
            id="list_step",
            mcp=MCPStepConfig(
                server="gobby-tasks",
                tool="find_file_overlaps",
                arguments={"task_ids": "${{ steps.execute.output.created }}"},
            ),
        )

        context: dict = {
            "inputs": {},
            "steps": {"execute": {"output": {"created": ["#9633", "#9634", "#9635"]}}},
        }

        rendered = executor.renderer.render_step(step, context)
        assert rendered.mcp.arguments["task_ids"] == ["#9633", "#9634", "#9635"]
        assert isinstance(rendered.mcp.arguments["task_ids"], list)

    @pytest.mark.asyncio
    async def test_pure_expression_preserves_dict(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that a pure ${{ expr }} returning a dict preserves the dict type."""
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.templates import TemplateEngine

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            template_engine=TemplateEngine(),
        )

        step = PipelineStep(
            id="dict_step",
            mcp=MCPStepConfig(
                server="s",
                tool="t",
                arguments={"config": "${{ steps.prev.output.settings }}"},
            ),
        )

        context: dict = {
            "inputs": {},
            "steps": {"prev": {"output": {"settings": {"timeout": 600, "retries": 3}}}},
        }

        rendered = executor.renderer.render_step(step, context)
        assert rendered.mcp.arguments["config"] == {"timeout": 600, "retries": 3}
        assert isinstance(rendered.mcp.arguments["config"], dict)

    @pytest.mark.asyncio
    async def test_mixed_string_with_list_renders_as_string(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that mixed strings containing ${{ }} still render as strings."""
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.templates import TemplateEngine

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            template_engine=TemplateEngine(),
        )

        step = PipelineStep(
            id="mixed_step",
            mcp=MCPStepConfig(
                server="s",
                tool="t",
                arguments={"prompt": "Process tasks: ${{ steps.prev.output.ids }}"},
            ),
        )

        context: dict = {
            "inputs": {},
            "steps": {"prev": {"output": {"ids": ["#1", "#2"]}}},
        }

        rendered = executor.renderer.render_step(step, context)
        assert isinstance(rendered.mcp.arguments["prompt"], str)
        assert "Process tasks:" in rendered.mcp.arguments["prompt"]

    @pytest.mark.asyncio
    async def test_pure_expression_preserves_scalar_types(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that pure expressions also work correctly for scalar values."""
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.templates import TemplateEngine

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            template_engine=TemplateEngine(),
        )

        step = PipelineStep(
            id="scalar_step",
            mcp=MCPStepConfig(
                server="s",
                tool="t",
                arguments={
                    "count": "${{ inputs.count }}",
                    "name": "${{ inputs.name }}",
                    "flag": "${{ inputs.flag }}",
                },
            ),
        )

        context: dict = {
            "inputs": {"count": 42, "name": "test", "flag": True},
            "steps": {},
        }

        rendered = executor.renderer.render_step(step, context)
        assert rendered.mcp.arguments["count"] == 42
        assert isinstance(rendered.mcp.arguments["count"], int)
        assert rendered.mcp.arguments["name"] == "test"
        assert rendered.mcp.arguments["flag"] is True

    @pytest.mark.asyncio
    async def test_render_does_not_mutate_original(
        self, mock_db, mock_execution_manager, mock_llm_service
    ) -> None:
        """Test that rendering doesn't mutate the original step definition."""
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.templates import TemplateEngine

        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            template_engine=TemplateEngine(),
        )

        original_args = {"timeout": "${{ inputs.timeout }}"}
        step = PipelineStep(
            id="immutable_step",
            mcp=MCPStepConfig(server="s", tool="t", arguments=original_args),
        )

        context: dict = {"inputs": {"timeout": "300"}, "steps": {}}
        rendered = executor.renderer.render_step(step, context)

        # Original should be unchanged
        assert step.mcp.arguments["timeout"] == "${{ inputs.timeout }}"
        # Rendered should have coerced value
        assert rendered.mcp.arguments["timeout"] == 300
