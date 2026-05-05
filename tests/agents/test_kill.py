"""Tests for gobby.agents.kill module."""

import signal
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.kill import (
    _close_terminal_window,
    _run_subprocess,
    _validate_terminal_value,
    kill_agent,
)
from gobby.storage.agents import AgentRun

pytestmark = pytest.mark.unit


class TestRunSubprocess:
    @pytest.mark.asyncio
    async def test_run_subprocess_success(self):
        rc, out, err = await _run_subprocess(sys.executable, "-c", 'print("hello")', timeout=1.0)
        assert rc == 0
        assert out.strip() == "hello"
        assert err == ""

    @pytest.mark.asyncio
    @patch("gobby.agents.kill.asyncio.create_subprocess_exec")
    async def test_run_subprocess_timeout(self, mock_create):
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(side_effect=TimeoutError("timeout"))
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()
        mock_create.return_value = mock_proc

        with pytest.raises(TimeoutError):
            await _run_subprocess("sleep", "10", timeout=0.1)

        mock_proc.kill.assert_called_once()
        assert mock_proc.kill.call_count == 1
        assert mock_proc.kill.call_args is not None
        mock_proc.wait.assert_called_once()
        assert mock_proc.wait.call_count == 1
        assert mock_proc.wait.call_args is not None


class TestValidateTerminalValue:
    def test_valid_patterns(self):
        assert _validate_terminal_value("tmux_pane", "%123") is True
        assert _validate_terminal_value("parent_pid", "1234") is True
        assert _validate_terminal_value("session_id", "my-test-sess") is True

    def test_invalid_patterns(self):
        assert _validate_terminal_value("tmux_pane", "123") is False
        assert _validate_terminal_value("tmux_pane", "%abc") is False
        assert _validate_terminal_value("parent_pid", "-1") is False
        assert _validate_terminal_value("session_id", "my sess") is False
        assert _validate_terminal_value("unknown_key", "val") is False


class TestCloseTerminalWindow:
    @pytest.mark.asyncio
    @patch("gobby.agents.kill.SessionManager")
    @patch("gobby.agents.kill._run_subprocess")
    async def test_close_tmux_pane(self, mock_run, mock_sm_cls):
        mock_session = MagicMock()
        mock_session.terminal_context = {"tmux_pane": "%99"}
        mock_sm = MagicMock()
        mock_sm.get.return_value = mock_session
        mock_sm_cls.return_value = mock_sm

        # display-message passes, then kill-pane passes
        mock_run.side_effect = [(0, "%99\n", ""), (0, "", "")]

        res = await _close_terminal_window("sess1", MagicMock())
        assert res["success"] is True
        assert res["method"] == "tmux_kill_pane"
        assert res["pane"] == "%99"

    @pytest.mark.asyncio
    @patch("gobby.agents.kill.sys")
    @patch("gobby.agents.kill.SessionManager")
    @patch("gobby.agents.kill._run_subprocess")
    async def test_close_taskkill_windows(self, mock_run, mock_sm_cls, mock_sys):
        mock_sys.platform = "win32"
        mock_session = MagicMock()
        mock_session.terminal_context = {"parent_pid": "123"}
        mock_sm = MagicMock()
        mock_sm.get.return_value = mock_session
        mock_sm_cls.return_value = mock_sm

        mock_run.return_value = (0, "", "")

        res = await _close_terminal_window("sess1", MagicMock())
        assert res["success"] is True
        assert res["method"] == "taskkill_tree"
        assert res["pid"] == "123"

    @pytest.mark.asyncio
    @patch("gobby.agents.kill.os.kill")
    @patch("gobby.agents.kill.SessionManager")
    async def test_close_parent_pid_unix(self, mock_sm_cls, mock_kill):
        mock_session = MagicMock()
        mock_session.terminal_context = {"parent_pid": "456"}
        mock_sm = MagicMock()
        mock_sm.get.return_value = mock_session
        mock_sm_cls.return_value = mock_sm

        res = await _close_terminal_window("sess1", MagicMock())
        assert res["success"] is True
        assert res["method"] == "parent_pid"
        assert res["pid"] == 456
        mock_kill.assert_called_with(456, signal.SIGTERM)


class TestKillAgent:
    @pytest.fixture
    def mock_db(self) -> MagicMock:
        return MagicMock()

    @pytest.fixture
    def agent_run(self) -> AgentRun:
        return AgentRun(
            id="run1",
            parent_session_id="parent1",
            child_session_id="sess1",
            provider="claude",
            prompt="do it",
            status="running",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
        )

    @pytest.mark.asyncio
    @patch("gobby.agents.kill._close_terminal_window")
    async def test_close_terminal_true(self, mock_close, agent_run, mock_db):
        mock_close.return_value = {"success": True, "method": "tmux"}
        res = await kill_agent(agent_run, mock_db, close_terminal=True)
        assert res["success"] is True
        assert res["method"] == "tmux"
        mock_close.assert_called_once()

    @pytest.mark.asyncio
    @patch("gobby.agents.kill.os.kill")
    async def test_kill_by_explicit_pid(self, mock_kill, agent_run, mock_db):
        agent_run.pid = 999
        res = await kill_agent(agent_run, mock_db, timeout=0)
        assert res["success"] is True
        assert res["pid"] == 999
        assert res["found_via"] == "db"
        mock_kill.assert_called_with(999, signal.SIGTERM)

    @pytest.mark.asyncio
    @patch("gobby.agents.kill.SessionManager")
    @patch("gobby.agents.kill.os.kill")
    async def test_kill_pid_from_terminal_context(self, mock_kill, mock_sm_cls, agent_run, mock_db):
        agent_run.pid = None
        mock_session = MagicMock()
        mock_session.terminal_context = {"parent_pid": "888"}
        mock_sm = MagicMock()
        mock_sm.get.return_value = mock_session
        mock_sm_cls.return_value = mock_sm

        res = await kill_agent(agent_run, mock_db, timeout=0)
        assert res["success"] is True
        assert res["pid"] == 888
        assert res["found_via"] == "terminal_context"
        mock_kill.assert_called_with(888, signal.SIGTERM)

    @pytest.mark.asyncio
    @patch("gobby.agents.kill._run_subprocess")
    @patch("gobby.agents.kill.os.kill")
    async def test_kill_pid_from_pgrep(self, mock_kill, mock_run, agent_run, mock_db):
        agent_run.pid = None
        # pgrep returns pid 777
        mock_run.return_value = (0, "777\n", "")

        res = await kill_agent(agent_run, mock_db, timeout=0)
        assert res["success"] is True
        assert res["pid"] == 777
        assert res["found_via"] == "pgrep"
        mock_kill.assert_called_with(777, signal.SIGTERM)

    @pytest.mark.asyncio
    @patch("gobby.agents.kill._run_subprocess")
    @patch("gobby.agents.kill.os.kill")
    async def test_kill_pid_from_pgrep_disambiguation(
        self, mock_kill, mock_run, agent_run, mock_db
    ):
        agent_run.pid = None
        agent_run.provider = "claude"

        def _run_side_effect(*args, **kwargs):
            cmd = args[0] if args else ""
            if cmd == "pgrep":
                return (0, "777\n778\n", "")
            if cmd == "ps":
                if args[2] == "777":
                    return (0, "python claude session-id sess1", "")
                if args[2] == "778":
                    # Both PIDs match — kill_agent picks the highest PID for disambiguation
                    return (0, "python claude session-id sess1 --wrapper", "")
            return (1, "", "")

        mock_run.side_effect = _run_side_effect

        res = await kill_agent(agent_run, mock_db, timeout=0)
        assert res["success"] is True
        assert res["pid"] == 778
        assert res["found_via"] == "pgrep_disambiguated"
        mock_kill.assert_called_with(778, signal.SIGTERM)

    @pytest.mark.asyncio
    @patch("gobby.agents.kill.os.kill")
    async def test_kill_already_dead_prior_to_signal(self, mock_kill, agent_run, mock_db):
        agent_run.pid = 999
        # os.kill(pid, 0) throws ProcessLookupError
        mock_kill.side_effect = ProcessLookupError("already dead")

        res = await kill_agent(agent_run, mock_db, timeout=0)
        assert res["success"] is True
        assert res["already_dead"] is True

    @pytest.mark.asyncio
    @patch("gobby.agents.kill.os.kill")
    @patch("gobby.agents.kill.asyncio.sleep")
    async def test_kill_escalates_to_kill(self, mock_sleep, mock_kill, agent_run, mock_db):
        agent_run.pid = 999

        # Custom side effect for os.kill
        # call 1: os.kill(999, 0) -> pass
        # call 2: os.kill(999, SIGTERM) -> pass
        # calls 3-N in loop: os.kill(999, 0) -> wait until timeout expires...
        # Wait, if we use time logic, we need to mock asyncio.get_running_loop.time()
        with patch("gobby.agents.kill.asyncio.get_running_loop") as mock_loop_getter:
            mock_loop = MagicMock()
            # simulate time passing
            # Start, Loop 1 check, Exceeded deadline + extras to avoid StopIteration
            mock_loop.time.side_effect = [0.0, 0.0, 10.0, 10.0, 10.0, 10.0]
            mock_loop_getter.return_value = mock_loop

            res = await kill_agent(agent_run, mock_db, timeout=2.0)

            assert res["success"] is True
            # Verify SIGKILL was sent
            assert mock_kill.call_args_list[-1] == ((999, signal.SIGKILL),)
