import signal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.registry import RunningAgent, RunningAgentRegistry

pytestmark = pytest.mark.unit


class TestRunningAgentRegistryKill:
    """Tests for RunningAgentRegistry.kill method."""

    @pytest.fixture
    def registry(self):
        return RunningAgentRegistry()

    @pytest.fixture
    def mock_os_kill(self):
        with patch("os.kill") as mock:
            yield mock

    @pytest.fixture
    def mock_subprocess(self):
        with patch(
            "gobby.agents.registry.RunningAgentRegistry._run_subprocess", new_callable=AsyncMock
        ) as mock:
            mock.return_value = (0, "12345", "")
            yield mock

    @pytest.mark.asyncio
    async def test_kill_headless_success(self, registry, mock_os_kill):
        """Kill autonomous agent sends signal to PID."""
        agent = RunningAgent(
            run_id="ar-autonomous",
            session_id="sess",
            parent_session_id="parent",
            mode="autonomous",
            pid=12345,
        )
        registry.add(agent)

        # Sequence of calls:
        # 1. check alive (pid, 0) -> success (None)
        # 2. send signal (pid, SIGTERM) -> success (None)
        # 3. wait loop check (pid, 0) -> raise ProcessLookupError (simulating death)
        mock_os_kill.side_effect = [None, None, ProcessLookupError()]

        result = await registry.kill("ar-autonomous")

        assert result["success"] is True
        mock_os_kill.assert_any_call(12345, signal.SIGTERM)
        assert registry.get("ar-autonomous") is None

    @pytest.mark.asyncio
    async def test_kill_not_found(self, registry):
        """Kill returns success with already_completed if agent not in registry."""
        result = await registry.kill("nonexistent")
        assert result["success"] is True
        assert result["already_completed"] is True

    @pytest.mark.asyncio
    async def test_kill_no_pid(self, registry):
        """Kill returns error if agent has no PID."""
        agent = RunningAgent(
            run_id="ar-no-pid",
            session_id="sess",
            parent_session_id="parent",
            mode="autonomous",
            pid=None,
        )
        registry.add(agent)

        result = await registry.kill("ar-no-pid")
        assert result["success"] is False
        assert "No target PID" in result["error"]

    @pytest.mark.asyncio
    async def test_kill_already_dead(self, registry, mock_os_kill):
        """Kill handles ProcessLookupError gracefully."""
        mock_os_kill.side_effect = ProcessLookupError()
        agent = RunningAgent(
            run_id="ar-dead",
            session_id="sess",
            parent_session_id="parent",
            mode="autonomous",
            pid=12345,
        )
        registry.add(agent)

        result = await registry.kill("ar-dead")

        assert result["success"] is True
        assert result.get("already_dead") is True
        assert registry.get("ar-dead") is None

    @pytest.mark.asyncio
    async def test_kill_in_process(self, registry):
        """Kill cancels in-process task."""
        mock_task = MagicMock()
        agent = RunningAgent(
            run_id="ar-process",
            session_id="sess",
            parent_session_id="parent",
            mode="in_process",
            task=mock_task,
        )
        registry.add(agent)

        result = await registry.kill("ar-process")

        assert result["success"] is True
        mock_task.cancel.assert_called_once()
        assert registry.get("ar-process") is None

    @pytest.mark.asyncio
    async def test_kill_terminal_pgrep_fallback(self, registry, mock_os_kill, mock_subprocess):
        """Kill terminal agent falls back to pgrep if no PID."""
        agent = RunningAgent(
            run_id="ar-term",
            session_id="sess-term",
            parent_session_id="parent",
            mode="terminal",
            pid=None,
        )
        registry.add(agent)

        # Mock pgrep finding a PID
        mock_subprocess.return_value = (0, "99999\n", "")

        # Simulate process death loop: alive -> signal -> dead
        mock_os_kill.side_effect = [None, None, ProcessLookupError()]

        result = await registry.kill("ar-term")

        assert result["success"] is True
        assert result["pid"] == 99999
        mock_os_kill.assert_any_call(99999, signal.SIGTERM)


class TestRunningAgentRegistryCloseTerminal:
    """Tests for _close_terminal_window methods."""

    @pytest.fixture
    def registry(self):
        return RunningAgentRegistry()

    @pytest.fixture
    def mock_subprocess(self):
        with patch(
            "gobby.agents.registry.RunningAgentRegistry._run_subprocess", new_callable=AsyncMock
        ) as mock:
            yield mock

    @pytest.mark.asyncio
    async def test_close_tmux_pane(self, registry, mock_subprocess):
        """Close terminal uses tmux kill-pane if context present."""
        agent = RunningAgent(
            run_id="ar-tmux",
            session_id="sess-tmux",
            parent_session_id="parent",
            mode="terminal",
        )

        # Mock session context retrieval
        with patch("gobby.storage.sessions.LocalSessionManager.get") as mock_get_session:
            mock_session = MagicMock()
            mock_session.terminal_context = {"tmux_pane": "%100"}
            mock_get_session.return_value = mock_session

            # Mock tmux display-message success
            mock_subprocess.side_effect = [
                (0, "%100", ""),  # display-message
                (0, "", ""),  # kill-pane
            ]

            result = await registry._close_terminal_window(agent)

            assert result["success"] is True
            assert result["method"] == "tmux_kill_pane"

            # Verify kill-pane called
            args = mock_subprocess.call_args_list[1][0]
            assert "tmux" in args
            assert "kill-pane" in args
            assert "%100" in args

    # kitty remote control close is not yet implemented — test removed
