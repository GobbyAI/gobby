"""Tests for MCP tool changes: run_pipeline and wait_for_agent removal."""

from __future__ import annotations

import inspect

import pytest

pytestmark = pytest.mark.unit


class TestRunPipelineNoWait:
    """run_pipeline no longer accepts wait/wait_timeout parameters."""

    def test_no_wait_parameter(self) -> None:
        from gobby.mcp_proxy.tools.workflows._pipeline_execution import run_pipeline

        sig = inspect.signature(run_pipeline)
        assert "wait" not in sig.parameters

    def test_no_wait_timeout_parameter(self) -> None:
        from gobby.mcp_proxy.tools.workflows._pipeline_execution import run_pipeline

        sig = inspect.signature(run_pipeline)
        assert "wait_timeout" not in sig.parameters

    def test_has_continuation_prompt_parameter(self) -> None:
        from gobby.mcp_proxy.tools.workflows._pipeline_execution import run_pipeline

        sig = inspect.signature(run_pipeline)
        assert "continuation_prompt" in sig.parameters

    @pytest.mark.asyncio
    async def test_run_pipeline_returns_immediately(self) -> None:
        """run_pipeline always returns immediately with execution_id."""
        from unittest.mock import AsyncMock, MagicMock

        from gobby.mcp_proxy.tools.workflows._pipeline_execution import run_pipeline

        mock_loader = AsyncMock()
        mock_loader.load_pipeline.return_value = MagicMock(
            name="test-pipe",
            steps=[MagicMock()],
            inputs={},
            outputs={},
        )

        mock_execution = MagicMock(id="pe-test123")
        mock_executor = MagicMock()
        mock_executor.execution_manager.create_execution.return_value = mock_execution
        mock_executor.execute = AsyncMock()

        result = await run_pipeline(
            loader=mock_loader,
            executor=mock_executor,
            name="test-pipe",
            inputs={"task_id": "#123"},
            project_id="proj-1",
            session_id="sess-1",
            continuation_prompt="Wire the dependencies",
        )

        assert result["success"] is True
        assert result["execution_id"] == "pe-test123"
        assert result["status"] == "running"
        assert (
            "will be notified" in result["message"].lower()
            or "started" in result["message"].lower()
        )


class TestWaitForAgentRemoved:
    """wait_for_agent tool is no longer registered."""

    def test_wait_for_agent_not_in_registry(self) -> None:
        """The agents registry should not contain wait_for_agent."""
        from unittest.mock import MagicMock

        from gobby.mcp_proxy.tools.agents import create_agents_registry

        mock_runner = MagicMock()
        mock_runner.can_spawn.return_value = (True, "ok", 0)
        mock_runner.get_run.return_value = None
        mock_runner.list_runs.return_value = []

        registry = create_agents_registry(
            runner=mock_runner,
            session_manager=MagicMock(),
        )

        tool_names = [t["name"] for t in registry.list_tools()]
        assert "wait_for_agent" not in tool_names


class TestSpawnAgentContinuationPrompt:
    """spawn_agent should accept continuation_prompt parameter."""

    def test_spawn_agent_exists_in_registry(self) -> None:
        from unittest.mock import MagicMock

        from gobby.mcp_proxy.tools.agents import create_agents_registry

        mock_runner = MagicMock()
        mock_runner.can_spawn.return_value = (True, "ok", 0)

        registry = create_agents_registry(
            runner=mock_runner,
            session_manager=MagicMock(),
        )

        tool_names = [t["name"] for t in registry.list_tools()]
        assert "spawn_agent" in tool_names
