"""Tests for MCP step type in pipeline definitions and executor.

Tests MCPStepConfig model, PipelineStep with mcp field,
execute_mcp_step handler, and template rendering with type coercion.
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.types import CallToolResult, TextContent
from pydantic import ValidationError

from gobby.mcp_proxy.tools.internal import normalize_internal_success_result
from gobby.mcp_proxy.tools.spawn_agent import _factory
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.workflows.definitions import MCPStepConfig, PipelineStep
from gobby.workflows.pipeline.handlers import execute_mcp_step

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["spawn_agent", "dispatch_batch"])
@pytest.mark.parametrize("has_manager", [False, True])
async def test_pipeline_spawn_refuses_unresolved_supplied_caller(
    tool: str,
    has_manager: bool,
) -> None:
    sessions = MagicMock(spec=SessionManager)
    sessions.db = None
    sessions.resolve_session_reference.side_effect = ValueError("unknown caller")
    proxy = MagicMock()
    proxy.get_tool_schema = AsyncMock()
    proxy.call_tool = AsyncMock(return_value={"success": True})
    step = PipelineStep(id="spawn", mcp=MCPStepConfig(server="gobby-agents", tool=tool))
    with pytest.raises(RuntimeError, match="spawnable_agents") as refused:
        await execute_mcp_step(
            step,
            {"session_id": "unknown-caller"},
            lambda: proxy,
            session_manager=sessions if has_manager else None,
        )
    assert "caller session cannot be resolved" in str(refused.value)
    proxy.get_tool_schema.assert_not_awaited()
    proxy.call_tool.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "allowed, network, caller_name",
    [
        (False, None, "pipeline-caller"),
        (True, None, "pipeline-caller"),
        (True, "trusted", "pipeline-caller"),
        (True, "trusted", "default"),
        (True, "none", "orchestrator"),
        (True, "trusted", "root"),
        (True, "none", "root"),
    ],
    ids=[
        "forbidden",
        "allowed",
        "network-forbidden",
        "default-network",
        "orchestrator-network",
        "root-trusted",
        "root-none",
    ],
)
async def test_pipeline_spawn_obeys_spawnable_agents(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    allowed: bool,
    network: str | None,
    caller_name: str,
) -> None:
    """Use the real spawn closure even when the pipeline skips before_tool rules."""
    project_id = str(sample_project["id"])
    sessions = SessionManager(temp_db)
    root = sessions.register("pipeline-root", None, "test", project_id=project_id)
    child = sessions.register(
        "pipeline-child",
        None,
        "test",
        project_id=project_id,
        parent_session_id=root.id,
        agent_depth=1,
    )
    run = LocalAgentRunManager(temp_db).create(
        parent_session_id=root.id,
        provider="claude",
        prompt="work",
        agent_name=caller_name,
        child_session_id=child.id,
    )
    sessions.update_terminal_pickup_metadata(child.id, agent_run_id=run.id)
    definitions = AgentDefinitionManager(temp_db)
    for name, spawnable in [
        (caller_name, ["pipeline-target"] if allowed else []),
        ("pipeline-target", []),
    ]:
        definitions.create(
            name,
            {
                "name": name,
                "provider": "claude",
                "prompts": {"agent": "Work."},
                "workflows": {"rule_selectors": {"include": []}},
                "spawnable_agents": spawnable,
            },
            project_id=project_id,
        )
    monkeypatch.setattr(
        _factory,
        "_resolve_spawn_project_context_with_provenance",
        lambda **kwargs: ({"id": project_id}, "/test", True),
    )
    launch = AsyncMock(return_value={"success": True, "run_id": "mock-launch"})
    monkeypatch.setattr(_factory, "spawn_agent_impl", launch)
    registry = _factory.create_spawn_agent_registry(
        MagicMock(),
        session_manager=sessions,
        db=temp_db,
    )
    proxy = MagicMock()
    proxy.get_tool_schema = AsyncMock()

    async def call_tool(server: str, tool: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
        assert server == "gobby-agents"
        assert kwargs["enforce_workflow"] is False
        return await registry.call(tool, arguments)

    proxy.call_tool = AsyncMock(side_effect=call_tool)
    step = PipelineStep(
        id="spawn",
        mcp=MCPStepConfig(
            server="gobby-agents",
            tool="spawn_agent",
            arguments={
                "prompt": "work",
                "agent": "pipeline-target",
                "parent_session_id": root.id,
                "network": network,
            },
        ),
    )
    context: dict[str, Any] = {
        "session_id": root.id if caller_name == "root" else child.id,
        "project_id": project_id,
    }
    if allowed and (network is None or caller_name in ("default", "orchestrator", "root")):
        result = await execute_mcp_step(step, context, lambda: proxy, session_manager=sessions)
        assert result["run_id"] == "mock-launch"
        launch.assert_awaited_once()
    else:
        with pytest.raises(RuntimeError, match="network" if network else "spawnable_agents"):
            await execute_mcp_step(step, context, lambda: proxy, session_manager=sessions)
        launch.assert_not_awaited()


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
            arguments={"prompt": "Do work", "agent": "developer-grok", "timeout": 600},
        )
        assert config.server == "gobby-agents"
        assert config.tool == "spawn_agent"
        assert config.arguments is not None
        assert config.arguments["prompt"] == "Do work"
        assert config.arguments["timeout"] == 600

    def test_config_empty_arguments(self) -> None:
        """Test config with explicit empty dict arguments."""
        config = MCPStepConfig(server="s", tool="t", arguments={})
        assert config.arguments == {}

    def test_config_requires_server(self) -> None:
        """Test that server is required."""
        with pytest.raises(ValidationError):
            MCPStepConfig.model_validate({"tool": "some_tool"})

    def test_config_requires_tool(self) -> None:
        """Test that tool is required."""
        with pytest.raises(ValidationError):
            MCPStepConfig.model_validate({"server": "some_server"})


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
def mock_db() -> MagicMock:
    return MagicMock()


@pytest.fixture
def mock_execution_manager() -> MagicMock:
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
def mock_llm_service() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_tool_proxy() -> AsyncMock:
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
    resolve_exc: Exception | None = None,
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
    @pytest.mark.parametrize("sdk_reply", [False, True])
    async def test_mcp_failure_is_an_operator_visible_step_error(self, sdk_reply: bool) -> None:
        from gobby.workflows.pipeline_state import PipelineStepError

        error = "Unsupported spawn provider: pipeline"
        reply = (
            CallToolResult(content=[TextContent(type="text", text=error)], is_error=True)
            if sdk_reply
            else {"success": False, "error": error}
        )
        proxy = AsyncMock()
        proxy.call_tool = AsyncMock(return_value=reply)
        step = PipelineStep(
            id="inbox-manager", mcp=MCPStepConfig(server="gobby-agents", tool="spawn_agent")
        )

        with pytest.raises(PipelineStepError, match=error):
            await execute_mcp_step(step, {"inputs": {}, "steps": {}}, lambda: proxy)

    @pytest.mark.asyncio
    async def test_mcp_step_calls_tool_proxy(self, mock_tool_proxy: AsyncMock) -> None:
        """Test that MCP step calls tool_proxy.call_tool with correct args."""
        step = PipelineStep(
            id="test_step",
            mcp=MCPStepConfig(
                server="gobby-tasks",
                tool="suggest_next_task",
                arguments={"parent_task_id": "#123"},
            ),
        )

        context: dict[str, Any] = {"inputs": {}, "steps": {}}
        result = await execute_mcp_step(step, context, lambda: mock_tool_proxy)

        mock_tool_proxy.get_tool_schema.assert_not_called()
        mock_tool_proxy.call_tool.assert_called_once_with(
            "gobby-tasks",
            "suggest_next_task",
            {"parent_task_id": "#123"},
            session_id=None,
            enforce_workflow=False,
        )
        # success key is stripped by handler (commit 509f7ad5)
        assert "success" not in result
        assert result["task_id"] == "#42"

    @pytest.mark.asyncio
    async def test_mcp_step_prefetches_schema_for_pipeline_session(
        self, mock_tool_proxy: AsyncMock
    ) -> None:
        """Pipeline MCP steps unlock the target tool before execution."""
        step = PipelineStep(
            id="test_step",
            mcp=MCPStepConfig(server="gobby-workflows", tool="list_pipeline_executions"),
        )

        session_manager = _make_session_manager(resolve_to="pipeline-session-123")
        context: dict[str, Any] = {"inputs": {}, "steps": {}, "session_id": "pipeline-session-123"}
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
            enforce_workflow=False,
        )
        assert mock_tool_proxy.call_tool.call_count == 1
        assert mock_tool_proxy.call_tool.call_args is not None

    @pytest.mark.asyncio
    async def test_mcp_step_no_arguments(self, mock_tool_proxy: AsyncMock) -> None:
        """Test MCP step with no arguments passes empty dict."""
        step = PipelineStep(
            id="test_step",
            mcp=MCPStepConfig(server="gobby-agents", tool="wait_for_agent"),
        )

        context: dict[str, Any] = {"inputs": {}, "steps": {}}
        await execute_mcp_step(step, context, lambda: mock_tool_proxy)

        mock_tool_proxy.get_tool_schema.assert_not_called()
        assert mock_tool_proxy.get_tool_schema.call_count == 0
        assert not mock_tool_proxy.get_tool_schema.called
        mock_tool_proxy.call_tool.assert_called_once_with(
            "gobby-agents", "wait_for_agent", {}, session_id=None, enforce_workflow=False
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

        context: dict[str, Any] = {"inputs": {}, "steps": {}}
        with pytest.raises(RuntimeError, match="requires tool_proxy_getter"):
            await execute_mcp_step(step, context, None)

    @pytest.mark.asyncio
    async def test_mcp_step_raises_when_tool_proxy_returns_none(self) -> None:
        """Test that MCP step raises when tool_proxy_getter returns None."""
        step = PipelineStep(
            id="test_step",
            mcp=MCPStepConfig(server="s", tool="t"),
        )

        context: dict[str, Any] = {"inputs": {}, "steps": {}}
        with pytest.raises(RuntimeError, match="returned None"):
            await execute_mcp_step(step, context, lambda: None)

    @pytest.mark.asyncio
    async def test_execute_mcp_step_resolves_external_id_before_session_context(
        self, mock_tool_proxy: AsyncMock
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

        context: dict[str, Any] = {"inputs": {}, "steps": {}, "session_id": "external-uuid-abc"}
        await execute_mcp_step(
            step, context, lambda: mock_tool_proxy, session_manager=session_manager
        )

        # Schema prefetch and call_tool both see the resolved platform UUID
        assert mock_tool_proxy.get_tool_schema.call_args.kwargs["session_id"] == "platform-uuid-999"
        assert mock_tool_proxy.call_tool.call_args.kwargs["session_id"] == "platform-uuid-999"

    @pytest.mark.asyncio
    async def test_execute_mcp_step_unresolvable_session_id_skips_set_session_context(
        self,
        mock_tool_proxy: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Unresolvable pipeline session refs are debug-only and fall through."""
        import logging as _logging

        session_manager = _make_session_manager(
            resolve_to=None, resolve_exc=ValueError("Session not found")
        )
        session_manager.db = None
        step = PipelineStep(
            id="test_step",
            mcp=MCPStepConfig(server="gobby-workflows", tool="list_pipeline_executions"),
        )

        context: dict[str, Any] = {
            "inputs": {},
            "steps": {},
            "session_id": "#6858",
            "project_id": "pipeline-project",
        }
        caplog.set_level(_logging.DEBUG, logger="gobby.utils.session_context")
        await execute_mcp_step(
            step, context, lambda: mock_tool_proxy, session_manager=session_manager
        )

        session_manager.resolve_session_reference.assert_called_once_with(
            "#6858", "pipeline-project"
        )
        assert any(
            rec.levelno == _logging.DEBUG and "could not resolve session ref" in rec.message
            for rec in caplog.records
        )
        assert not any(
            rec.levelno >= _logging.WARNING and "could not resolve session ref" in rec.message
            for rec in caplog.records
        )
        assert mock_tool_proxy.get_tool_schema.call_args.kwargs["session_id"] is None
        assert mock_tool_proxy.call_tool.call_args.kwargs["session_id"] is None

    @pytest.mark.asyncio
    async def test_execute_mcp_step_ignores_tool_proxy_session_manager_in_production(
        self, mock_tool_proxy: AsyncMock
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
        context: dict[str, Any] = {"inputs": {}, "steps": {}, "session_id": "pipeline-child-ref"}
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

        context: dict[str, Any] = {"inputs": {}, "steps": {}}
        with pytest.raises(RuntimeError, match="failed"):
            await execute_mcp_step(step, context, lambda: mock_proxy)

    @pytest.mark.asyncio
    async def test_mcp_step_passes_success_reply_with_null_error(self) -> None:
        """A success reply whose error field is null passes once the proxy strips success."""
        reply = normalize_internal_success_result(
            {"success": True, "run_id": "run-1", "error": None, "pane_ref": "0:0:1:1"}
        )
        assert reply == {"run_id": "run-1", "error": None, "pane_ref": "0:0:1:1"}
        mock_proxy = AsyncMock()
        mock_proxy.call_tool = AsyncMock(return_value=reply)
        step = PipelineStep(id="seat_a", mcp=MCPStepConfig(server="s", tool="spawn_agent"))

        result = await execute_mcp_step(step, {"inputs": {}, "steps": {}}, lambda: mock_proxy)

        assert result == {"run_id": "run-1", "error": None, "pane_ref": "0:0:1:1"}

    @pytest.mark.asyncio
    async def test_mcp_step_failure_with_null_error_names_no_none(self) -> None:
        """A failure reply with a null error reports the fallback message."""
        mock_proxy = AsyncMock()
        mock_proxy.call_tool = AsyncMock(return_value={"success": False, "error": None})
        step = PipelineStep(id="seat_a", mcp=MCPStepConfig(server="s", tool="spawn_agent"))

        with pytest.raises(RuntimeError, match=r"returned error: Unknown MCP tool error$"):
            await execute_mcp_step(step, {"inputs": {}, "steps": {}}, lambda: mock_proxy)

    @pytest.mark.asyncio
    async def test_mcp_step_fails_closed_on_sdk_error_result(self) -> None:
        """A downstream CallToolResult with is_error=True never becomes a step value."""
        mock_proxy = AsyncMock()
        mock_proxy.call_tool = AsyncMock(
            return_value=CallToolResult(
                content=[TextContent(type="text", text="boom")], is_error=True
            )
        )
        step = PipelineStep(id="external", mcp=MCPStepConfig(server="ext", tool="explode"))

        context: dict[str, Any] = {"inputs": {}, "steps": {}}
        with pytest.raises(RuntimeError, match="ext:explode returned error: boom"):
            await execute_mcp_step(step, context, lambda: mock_proxy)

    @pytest.mark.asyncio
    async def test_mcp_step_converts_sdk_success_result(self) -> None:
        """A successful CallToolResult yields its text and structured content."""
        mock_proxy = AsyncMock()
        mock_proxy.call_tool = AsyncMock(
            return_value=CallToolResult(
                content=[
                    TextContent(type="text", text="line one"),
                    TextContent(type="text", text="line two"),
                ],
                structured_content={"count": 2},
                is_error=False,
            )
        )
        step = PipelineStep(id="external", mcp=MCPStepConfig(server="ext", tool="lines"))

        context: dict[str, Any] = {"inputs": {}, "steps": {}}
        result = await execute_mcp_step(step, context, lambda: mock_proxy)

        assert result == {"result": "line one\nline two", "structured_content": {"count": 2}}


class TestMCPStepInPipelineExecute:
    """Tests for MCP step execution within full pipeline execute flow."""

    @pytest.mark.asyncio
    async def test_mcp_step_executes_in_pipeline(
        self,
        mock_db: MagicMock,
        mock_execution_manager: MagicMock,
        mock_llm_service: AsyncMock,
        mock_tool_proxy: AsyncMock,
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

    @pytest.mark.asyncio
    async def test_step_output_with_null_error_completes(
        self,
        mock_db: MagicMock,
        mock_execution_manager: MagicMock,
        mock_llm_service: AsyncMock,
        mock_tool_proxy: AsyncMock,
    ) -> None:
        """A step whose output carries a null error completes with that output."""
        from gobby.workflows.definitions import PipelineDefinition
        from gobby.workflows.pipeline_executor import PipelineExecutor
        from gobby.workflows.pipeline_state import StepStatus

        mock_tool_proxy.call_tool = AsyncMock(
            return_value=normalize_internal_success_result(
                {"success": True, "run_id": "run-1", "error": None, "pane_ref": "0:0:1:1"}
            )
        )
        pipeline = PipelineDefinition(
            name="seat-pipeline",
            steps=[PipelineStep(id="seat_a", mcp=MCPStepConfig(server="s", tool="spawn_agent"))],
        )
        executor = PipelineExecutor(
            db=mock_db,
            execution_manager=mock_execution_manager,
            llm_service=mock_llm_service,
            tool_proxy_getter=lambda: mock_tool_proxy,
        )

        await executor.execute(pipeline=pipeline, inputs={}, project_id="proj-123")

        completed = [
            call.kwargs
            for call in mock_execution_manager.update_step_execution.call_args_list
            if call.kwargs.get("status") == StepStatus.COMPLETED
        ]
        assert [json.loads(kwargs["output_json"]) for kwargs in completed] == [
            {"run_id": "run-1", "error": None, "pane_ref": "0:0:1:1"}
        ]


# =============================================================================
# Template rendering + type coercion tests
# =============================================================================


class TestMCPTemplateRendering:
    """Tests for template rendering in MCP step arguments with type coercion."""

    @pytest.mark.asyncio
    async def test_render_mcp_arguments_with_template(
        self,
        mock_db: MagicMock,
        mock_execution_manager: MagicMock,
        mock_llm_service: AsyncMock,
        mock_tool_proxy: AsyncMock,
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

        context: dict[str, Any] = {
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
        self, mock_db: MagicMock, mock_execution_manager: MagicMock, mock_llm_service: AsyncMock
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

        context: dict[str, Any] = {
            "inputs": {"force_flag": "true", "verbose": "false"},
            "steps": {},
        }

        rendered = executor.renderer.render_step(step, context)
        assert rendered.mcp.arguments["force"] is True
        assert rendered.mcp.arguments["verbose"] is False

    @pytest.mark.asyncio
    async def test_drop_null_values(
        self, mock_db: MagicMock, mock_execution_manager: MagicMock, mock_llm_service: AsyncMock
    ) -> None:
        """Null-like MCP arguments are omitted from the rendered call."""
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

        context: dict[str, Any] = {
            "inputs": {"maybe_null": "null"},
            "steps": {},
        }

        rendered = executor.renderer.render_step(step, context)
        assert "param" not in rendered.mcp.arguments

    @pytest.mark.asyncio
    async def test_coerce_float_values(
        self, mock_db: MagicMock, mock_execution_manager: MagicMock, mock_llm_service: AsyncMock
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

        context: dict[str, Any] = {
            "inputs": {"ratio": "0.75"},
            "steps": {},
        }

        rendered = executor.renderer.render_step(step, context)
        assert rendered.mcp.arguments["ratio"] == 0.75
        assert isinstance(rendered.mcp.arguments["ratio"], float)

    @pytest.mark.asyncio
    async def test_nested_dict_arguments_rendered(
        self, mock_db: MagicMock, mock_execution_manager: MagicMock, mock_llm_service: AsyncMock
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

        context: dict[str, Any] = {
            "inputs": {"name": "test", "count": "5"},
            "steps": {},
        }

        rendered = executor.renderer.render_step(step, context)
        assert rendered.mcp.arguments["outer"]["inner_str"] == "test"
        assert rendered.mcp.arguments["outer"]["inner_num"] == 5

    @pytest.mark.asyncio
    async def test_pure_expression_preserves_list(
        self, mock_db: MagicMock, mock_execution_manager: MagicMock, mock_llm_service: AsyncMock
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

        context: dict[str, Any] = {
            "inputs": {},
            "steps": {"execute": {"output": {"created": ["#9633", "#9634", "#9635"]}}},
        }

        rendered = executor.renderer.render_step(step, context)
        assert rendered.mcp.arguments["task_ids"] == ["#9633", "#9634", "#9635"]
        assert isinstance(rendered.mcp.arguments["task_ids"], list)

    @pytest.mark.asyncio
    async def test_pure_expression_preserves_dict(
        self, mock_db: MagicMock, mock_execution_manager: MagicMock, mock_llm_service: AsyncMock
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

        context: dict[str, Any] = {
            "inputs": {},
            "steps": {"prev": {"output": {"settings": {"timeout": 600, "retries": 3}}}},
        }

        rendered = executor.renderer.render_step(step, context)
        assert rendered.mcp.arguments["config"] == {"timeout": 600, "retries": 3}
        assert isinstance(rendered.mcp.arguments["config"], dict)

    @pytest.mark.asyncio
    async def test_mixed_string_with_list_renders_as_string(
        self, mock_db: MagicMock, mock_execution_manager: MagicMock, mock_llm_service: AsyncMock
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

        context: dict[str, Any] = {
            "inputs": {},
            "steps": {"prev": {"output": {"ids": ["#1", "#2"]}}},
        }

        rendered = executor.renderer.render_step(step, context)
        assert isinstance(rendered.mcp.arguments["prompt"], str)
        assert "Process tasks:" in rendered.mcp.arguments["prompt"]

    @pytest.mark.asyncio
    async def test_pure_expression_preserves_scalar_types(
        self, mock_db: MagicMock, mock_execution_manager: MagicMock, mock_llm_service: AsyncMock
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

        context: dict[str, Any] = {
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
        self, mock_db: MagicMock, mock_execution_manager: MagicMock, mock_llm_service: AsyncMock
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

        context: dict[str, Any] = {"inputs": {"timeout": "300"}, "steps": {}}
        rendered = executor.renderer.render_step(step, context)

        # Original should be unchanged
        assert step.mcp is not None
        assert step.mcp.arguments is not None
        assert step.mcp.arguments["timeout"] == "${{ inputs.timeout }}"
        # Rendered should have coerced value
        assert rendered.mcp.arguments["timeout"] == 300
