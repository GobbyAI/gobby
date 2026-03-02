"""Tests for wait_for_agent MCP tool.

Tests polling, timeout, success/error states, and edge cases.
"""

from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_runner() -> MagicMock:
    """Create a mock AgentRunner."""
    runner = MagicMock()
    return runner


@pytest.fixture
def agents_registry(mock_runner) -> object:
    """Create an agents registry with wait_for_agent tool."""
    from gobby.mcp_proxy.tools.agents import create_agents_registry

    return create_agents_registry(runner=mock_runner)


class TestWaitForAgent:
    """Tests for wait_for_agent tool."""

    @pytest.mark.asyncio
    async def test_already_completed_returns_immediately(
        self, mock_runner, agents_registry
    ) -> None:
        """Test that already-completed agent returns immediately without polling."""
        run = MagicMock()
        run.status = "success"
        mock_runner.get_run.return_value = run

        tool_func = agents_registry._tools["wait_for_agent"].func
        result = await tool_func(run_id="run-123", timeout=600, poll_interval=10)

        assert result["completed"] is True
        assert result["status"] == "success"
        assert result["timed_out"] is False
        assert result["wait_time"] == 0.0

    @pytest.mark.asyncio
    async def test_already_error_returns_immediately(self, mock_runner, agents_registry) -> None:
        """Test that errored agent returns immediately."""
        run = MagicMock()
        run.status = "error"
        mock_runner.get_run.return_value = run

        tool_func = agents_registry._tools["wait_for_agent"].func
        result = await tool_func(run_id="run-456", timeout=600, poll_interval=10)

        assert result["completed"] is False
        assert result["status"] == "error"
        assert result["timed_out"] is False

    @pytest.mark.asyncio
    async def test_already_timeout_returns_immediately(self, mock_runner, agents_registry) -> None:
        """Test that timed-out agent returns immediately."""
        run = MagicMock()
        run.status = "timeout"
        mock_runner.get_run.return_value = run

        tool_func = agents_registry._tools["wait_for_agent"].func
        result = await tool_func(run_id="run-789", timeout=600, poll_interval=10)

        assert result["completed"] is False
        assert result["status"] == "timeout"

    @pytest.mark.asyncio
    async def test_already_cancelled_returns_immediately(
        self, mock_runner, agents_registry
    ) -> None:
        """Test that cancelled agent returns immediately."""
        run = MagicMock()
        run.status = "cancelled"
        mock_runner.get_run.return_value = run

        tool_func = agents_registry._tools["wait_for_agent"].func
        result = await tool_func(run_id="run-abc", timeout=600, poll_interval=10)

        assert result["completed"] is False
        assert result["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_run_not_found(self, mock_runner, agents_registry) -> None:
        """Test that missing run returns error."""
        mock_runner.get_run.return_value = None

        tool_func = agents_registry._tools["wait_for_agent"].func
        result = await tool_func(run_id="nonexistent", timeout=600, poll_interval=10)

        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_polls_until_complete(self, mock_runner, agents_registry) -> None:
        """Test that tool polls until agent reaches terminal status."""
        running = MagicMock()
        running.status = "running"
        completed = MagicMock()
        completed.status = "success"

        # First call: running, second after sleep: running, third: success
        mock_runner.get_run.side_effect = [running, running, completed]

        tool_func = agents_registry._tools["wait_for_agent"].func
        result = await tool_func(run_id="run-poll", timeout=600, poll_interval=0.01)

        assert result["completed"] is True
        assert result["status"] == "success"
        assert result["timed_out"] is False
        assert result["wait_time"] > 0

    @pytest.mark.asyncio
    async def test_timeout_returns_timed_out(self, mock_runner, agents_registry) -> None:
        """Test that exceeding timeout returns timed_out=True."""
        running = MagicMock()
        running.status = "running"
        mock_runner.get_run.return_value = running

        tool_func = agents_registry._tools["wait_for_agent"].func
        # Very short timeout + poll interval to trigger timeout quickly
        result = await tool_func(run_id="run-slow", timeout=0.01, poll_interval=0.005)

        assert result["completed"] is False
        assert result["timed_out"] is True
        assert result["status"] == "running"

    @pytest.mark.asyncio
    async def test_run_disappears_during_wait(self, mock_runner, agents_registry) -> None:
        """Test handling when run disappears during polling."""
        running = MagicMock()
        running.status = "running"

        # First call: running, then disappears
        mock_runner.get_run.side_effect = [running, None]

        tool_func = agents_registry._tools["wait_for_agent"].func
        result = await tool_func(run_id="run-vanish", timeout=600, poll_interval=0.01)

        assert result["completed"] is False
        assert result["status"] == "unknown"
        assert "disappeared" in result.get("error", "")

    @pytest.mark.asyncio
    async def test_negative_poll_interval_defaults(self, mock_runner, agents_registry) -> None:
        """Test that negative poll_interval is corrected to default."""
        run = MagicMock()
        run.status = "success"
        mock_runner.get_run.return_value = run

        tool_func = agents_registry._tools["wait_for_agent"].func
        # Negative poll_interval should not cause issues
        result = await tool_func(run_id="run-neg", timeout=600, poll_interval=-5)

        assert result["completed"] is True

    @pytest.mark.asyncio
    async def test_result_includes_run_id(self, mock_runner, agents_registry) -> None:
        """Test that result always includes the run_id."""
        run = MagicMock()
        run.status = "success"
        mock_runner.get_run.return_value = run

        tool_func = agents_registry._tools["wait_for_agent"].func
        result = await tool_func(run_id="run-xyz", timeout=600, poll_interval=10)

        assert result["run_id"] == "run-xyz"
