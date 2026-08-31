"""Tests for gobby.agents.kill module."""

import asyncio
import logging
import signal
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import psutil
import pytest

from gobby.agents.kill import (
    _close_terminal_window,
    _run_subprocess,
    _validate_terminal_value,
    kill_agent,
    pid_matches_agent_identity,
)
from gobby.agents.tmux import configure_tmux
from gobby.config.tmux import TmuxConfig
from gobby.storage.agents import AgentRun

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _configured_tmux() -> None:
    """(Re)configure daemon tmux helpers; earlier runner-shutdown tests reset them."""
    configure_tmux(TmuxConfig())


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


class TestPidMatchesAgentIdentity:
    SESSION_ID = "ec032f4b-c626-4177-90ce-c1f3765c47d0"

    @staticmethod
    def _process(
        cmdline: list[str],
        *,
        environment: dict[str, str] | None = None,
    ) -> MagicMock:
        process = MagicMock(spec=psutil.Process)
        process.cmdline.return_value = cmdline
        process.environ.return_value = environment or {}
        return process

    @pytest.mark.asyncio
    async def test_claude_cmdline_session_marker_matches(self):
        process = self._process(["claude", "--model", "opus", "--session-id", self.SESSION_ID])
        process_factory = MagicMock(return_value=process)

        with patch("gobby.agents.kill.asyncio.to_thread", wraps=asyncio.to_thread) as to_thread:
            assert (
                await pid_matches_agent_identity(
                    1234,
                    provider="claude",
                    session_id=self.SESSION_ID,
                    process_factory=process_factory,
                )
                is True
            )

        to_thread.assert_awaited_once()
        process_factory.assert_called_once_with(1234)
        process.environ.assert_not_called()

    @pytest.mark.asyncio
    async def test_codex_without_argv_marker_matches_via_environment(self):
        process = self._process(
            ["codex", "--model", "gpt-5.6-sol", "-c", 'model_reasoning_effort="medium"'],
            environment={"GOBBY_SESSION_ID": self.SESSION_ID},
        )
        assert (
            await pid_matches_agent_identity(
                1234,
                provider="codex",
                session_id=self.SESSION_ID,
                process_factory=MagicMock(return_value=process),
            )
            is True
        )

    @pytest.mark.asyncio
    async def test_environment_session_mismatch_is_refused(self):
        process = self._process(
            ["codex", "--model", "gpt-5.6-sol"],
            environment={"GOBBY_SESSION_ID": "some-other-session"},
        )
        assert (
            await pid_matches_agent_identity(
                1234,
                provider="codex",
                session_id=self.SESSION_ID,
                process_factory=MagicMock(return_value=process),
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_missing_process_environment_is_refused(self):
        process = self._process(["codex", "--model", "gpt-5.6-sol"])
        process.environ.side_effect = psutil.NoSuchProcess(1234)
        assert (
            await pid_matches_agent_identity(
                1234,
                provider="codex",
                session_id=self.SESSION_ID,
                process_factory=MagicMock(return_value=process),
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_provider_mismatch_is_refused_without_env_lookup(self):
        process = self._process(["claude", "--session-id", self.SESSION_ID])
        assert (
            await pid_matches_agent_identity(
                1234,
                provider="codex",
                session_id=self.SESSION_ID,
                process_factory=MagicMock(return_value=process),
            )
            is False
        )
        process.environ.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [psutil.NoSuchProcess(1234), psutil.ZombieProcess(1234)],
        ids=["missing", "zombie"],
    )
    async def test_missing_or_zombie_process_is_refused(self, error: psutil.NoSuchProcess):
        process = self._process([])
        process.cmdline.side_effect = error
        assert (
            await pid_matches_agent_identity(
                1234,
                provider="codex",
                session_id=self.SESSION_ID,
                process_factory=MagicMock(return_value=process),
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_unreadable_cmdline_obeys_safety_policy(
        self,
        caplog: pytest.LogCaptureFixture,
    ):
        process = self._process([])
        process.cmdline.side_effect = psutil.AccessDenied(1234)
        caplog.set_level(logging.DEBUG, logger="gobby.agents.kill")

        for expected in (True, False):
            caplog.clear()
            assert (
                await pid_matches_agent_identity(
                    1234,
                    provider="codex",
                    session_id=self.SESSION_ID,
                    process_factory=MagicMock(return_value=process),
                    unverifiable_result=expected,
                )
                is expected
            )
            records = [record for record in caplog.records if record.name == "gobby.agents.kill"]
            assert any(
                "cmdline inspection failed: AccessDenied" in record.message for record in records
            )
            if expected:
                assert not [record for record in records if record.levelno >= logging.WARNING]
            else:
                assert any(record.levelno == logging.WARNING for record in records)

    @pytest.mark.asyncio
    async def test_empty_exception_message_uses_exception_class(
        self,
        caplog: pytest.LogCaptureFixture,
    ):
        caplog.set_level(logging.WARNING, logger="gobby.agents.kill")

        assert (
            await pid_matches_agent_identity(
                1234,
                provider="codex",
                session_id=self.SESSION_ID,
                process_factory=MagicMock(side_effect=TimeoutError()),
            )
            is False
        )
        assert "process inspection failed: TimeoutError" in caplog.text

    @pytest.mark.asyncio
    async def test_unreadable_environment_returns_unverifiable_result(
        self,
        caplog: pytest.LogCaptureFixture,
    ):
        process = self._process(["codex", "--model", "gpt-5.6-sol"])
        process.environ.side_effect = psutil.AccessDenied(1234)
        caplog.set_level(logging.DEBUG, logger="gobby.agents.kill")

        for expected in (False, True):
            caplog.clear()
            assert (
                await pid_matches_agent_identity(
                    1234,
                    provider="codex",
                    session_id=self.SESSION_ID,
                    process_factory=MagicMock(return_value=process),
                    unverifiable_result=expected,
                )
                is expected
            )
            records = [record for record in caplog.records if record.name == "gobby.agents.kill"]
            assert any(
                "environment inspection failed: AccessDenied" in record.message
                for record in records
            )
            if expected:
                assert not [record for record in records if record.levelno >= logging.WARNING]
            else:
                assert any(record.levelno == logging.WARNING for record in records)

    async def test_liveness_mismatch_logs_at_debug(self, caplog: pytest.LogCaptureFixture):
        process = MagicMock(spec=psutil.Process)
        process.cmdline.return_value = ["python", "other-provider"]
        process.environ.return_value = {}
        caplog.set_level(logging.DEBUG, logger="gobby.agents.kill")

        assert (
            await pid_matches_agent_identity(
                1234,
                provider="codex",
                session_id=self.SESSION_ID,
                process_factory=MagicMock(return_value=process),
                unverifiable_result=True,
            )
            is False
        )
        records = [record for record in caplog.records if record.name == "gobby.agents.kill"]
        assert any("no longer matches provider identity" in record.message for record in records)
        assert not [record for record in records if record.levelno >= logging.WARNING]


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
    @patch("gobby.agents.kill.os.killpg")
    @patch("gobby.agents.kill.os.getpgid", return_value=456)
    @patch("gobby.agents.kill.SessionManager")
    async def test_close_parent_pid_unix(self, mock_sm_cls, mock_getpgid, mock_killpg):
        mock_session = MagicMock()
        mock_session.terminal_context = {"parent_pid": "456"}
        mock_sm = MagicMock()
        mock_sm.get.return_value = mock_session
        mock_sm_cls.return_value = mock_sm

        res = await _close_terminal_window("sess1", MagicMock())
        assert res["success"] is True
        assert res["method"] == "parent_pid"
        assert res["pid"] == 456
        mock_getpgid.assert_called_once_with(456)
        mock_killpg.assert_called_once_with(456, signal.SIGTERM)

    @pytest.mark.asyncio
    @patch("gobby.agents.kill.psutil.Process")
    @patch("gobby.agents.kill.os.kill")
    @patch("gobby.agents.kill.SessionManager")
    async def test_close_parent_pid_refuses_recycled_pid(
        self,
        mock_sm_cls,
        mock_kill,
        mock_process,
    ):
        mock_session = MagicMock()
        mock_session.terminal_context = {"parent_pid": "456"}
        mock_sm = MagicMock()
        mock_sm.get.return_value = mock_session
        mock_sm_cls.return_value = mock_sm
        mock_process.return_value.cmdline.return_value = [
            "python",
            "qwen",
            "session-id",
            "other",
        ]

        res = await _close_terminal_window("sess1", MagicMock(), provider="claude")

        assert res["success"] is False
        assert res["method"] == "parent_pid"
        assert "does not match agent identity" in res["error"]
        mock_kill.assert_not_called()


class TestKillAgent:
    @pytest.fixture(autouse=True)
    def mock_agent_process(self) -> Iterator[MagicMock]:
        with patch("gobby.agents.kill.psutil.Process") as process_factory:
            process_factory.return_value.cmdline.return_value = [
                "python",
                "claude",
                "session-id",
                "sess1",
            ]
            yield process_factory

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
    @patch("gobby.agents.kill.os.kill")
    @patch("gobby.agents.kill._close_terminal_window")
    @patch("gobby.agents.kill._close_tmux_session")
    async def test_close_terminal_prefers_persisted_tmux_session(
        self,
        mock_close_tmux,
        mock_close_window,
        mock_kill,
        agent_run,
        mock_db,
    ):
        agent_run.pid = 999
        agent_run.terminal_id = "gobby-run-123"
        mock_close_tmux.return_value = {
            "success": True,
            "method": "terminal_kill",
            "terminal_id": "gobby-run-123",
        }

        res = await kill_agent(agent_run, mock_db, close_terminal=True)

        assert res["success"] is True
        assert res["method"] == "terminal_kill"
        assert res["terminal_close"] == mock_close_tmux.return_value
        mock_close_tmux.assert_awaited_once_with(
            agent_run,
            mock_db,
            terminal_action="cancel",
            terminal_reason="user_cancelled",
            timeout=5.0,
            terminal_services=None,
        )
        mock_close_window.assert_not_called()
        mock_kill.assert_not_called()

    @pytest.mark.asyncio
    @patch("gobby.agents.kill.os.kill")
    @patch("gobby.agents.kill._close_terminal_window")
    @patch("gobby.agents.kill._close_tmux_session")
    async def test_close_terminal_falls_back_to_session_context(
        self,
        mock_close_tmux,
        mock_close_window,
        mock_kill,
        agent_run,
        mock_db,
    ):
        agent_run.pid = 999
        agent_run.terminal_id = "gobby-run-123"
        mock_kill.side_effect = ProcessLookupError("already dead")
        mock_close_tmux.return_value = {"success": False, "error": "missing"}
        mock_close_window.return_value = {"success": True, "method": "tmux_kill_pane"}

        res = await kill_agent(agent_run, mock_db, close_terminal=True)

        assert res["success"] is True
        assert res["method"] == "tmux_kill_pane"
        mock_close_tmux.assert_awaited_once_with(
            agent_run,
            mock_db,
            terminal_action="cancel",
            terminal_reason="user_cancelled",
            timeout=5.0,
            terminal_services=None,
        )
        mock_close_window.assert_called_once()

    @pytest.mark.asyncio
    @patch("gobby.agents.kill.os.killpg")
    @patch("gobby.agents.kill.os.kill")
    @patch("gobby.agents.kill._close_terminal_window")
    async def test_close_terminal_true(
        self, mock_close, mock_kill, mock_killpg, agent_run, mock_db
    ):
        agent_run.pid = 999
        mock_kill.side_effect = [None, ProcessLookupError("closed")]
        mock_close.return_value = {"success": True, "method": "tmux"}
        res = await kill_agent(agent_run, mock_db, close_terminal=True)
        assert res["success"] is True
        assert res["method"] == "tmux"
        mock_close.assert_called_once()
        assert mock_kill.call_args_list == [((999, 0),), ((999, 0),)]
        mock_killpg.assert_not_called()

    @patch("gobby.agents.kill._signal_process_group")
    @patch("gobby.agents.kill.os.kill")
    @patch("gobby.agents.kill._close_terminal_window")
    async def test_failed_terminal_close_falls_back_to_direct_pid(
        self,
        mock_close: AsyncMock,
        mock_kill: MagicMock,
        mock_signal: MagicMock,
        agent_run: AgentRun,
        mock_db: MagicMock,
    ) -> None:
        agent_run.pid = 999
        mock_close.return_value = {"success": False, "error": "terminal unavailable"}

        res = await kill_agent(agent_run, mock_db, close_terminal=True, timeout=0)

        assert res["success"] is True
        assert res["pid"] == 999
        assert res["found_via"] == "db"
        mock_close.assert_awaited_once()
        mock_kill.assert_called_once_with(999, 0)
        mock_signal.assert_called_once_with(999, signal.SIGTERM)

    @pytest.mark.asyncio
    @patch("gobby.agents.kill.os.killpg")
    @patch("gobby.agents.kill.os.getpgid", return_value=999)
    @patch("gobby.agents.kill.os.kill")
    @patch("gobby.agents.kill._close_terminal_window")
    async def test_close_terminal_zero_timeout_escalates_alive_pid(
        self,
        mock_close,
        mock_kill,
        mock_getpgid,
        mock_killpg,
        agent_run,
        mock_db,
    ):
        agent_run.pid = 999
        mock_kill.return_value = None
        mock_close.return_value = {"success": True, "method": "tmux"}

        res = await kill_agent(agent_run, mock_db, close_terminal=True, timeout=0)

        assert res["success"] is False
        assert res["error"] == "PID 999 still alive after SIGKILL"
        assert res["pid"] == 999
        assert mock_kill.call_count >= 2
        mock_kill.assert_any_call(999, 0)
        mock_getpgid.assert_called_once_with(999)
        mock_killpg.assert_called_once_with(999, signal.SIGKILL)

    @pytest.mark.asyncio
    @patch("gobby.agents.kill.os.kill")
    @patch("gobby.agents.kill.os.killpg")
    @patch("gobby.agents.kill.os.getpgid", return_value=999)
    async def test_kill_by_explicit_pid(
        self, mock_getpgid, mock_killpg, mock_kill, agent_run, mock_db
    ):
        agent_run.pid = 999
        res = await kill_agent(agent_run, mock_db, timeout=0)
        assert res["success"] is True
        assert res["pid"] == 999
        assert res["found_via"] == "db"
        mock_getpgid.assert_called_once_with(999)
        mock_killpg.assert_called_once_with(999, signal.SIGTERM)

    @pytest.mark.asyncio
    @patch("gobby.agents.kill.os.kill")
    async def test_kill_by_explicit_pid_refuses_recycled_pid(
        self,
        mock_kill,
        agent_run,
        mock_db,
        caplog: pytest.LogCaptureFixture,
        mock_agent_process: MagicMock,
    ):
        agent_run.pid = 999
        mock_agent_process.return_value.cmdline.return_value = [
            "python",
            "other-provider",
            "session-id",
            "other-session",
        ]
        caplog.set_level(logging.WARNING, logger="gobby.agents.kill")

        res = await kill_agent(agent_run, mock_db, timeout=0)

        assert res["success"] is False
        assert res["pid"] == 999
        assert "does not match agent identity" in res["error"]
        mock_kill.assert_called_once_with(999, 0)
        assert "Refusing to signal PID 999: cmdline does not match provider identity" in caplog.text

    @pytest.mark.asyncio
    @patch("gobby.agents.kill.SessionManager")
    @patch("gobby.agents.kill.os.kill")
    @patch("gobby.agents.kill.os.killpg")
    @patch("gobby.agents.kill.os.getpgid", return_value=888)
    async def test_kill_pid_from_terminal_context(
        self, mock_getpgid, mock_killpg, mock_kill, mock_sm_cls, agent_run, mock_db
    ):
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
        mock_getpgid.assert_called_once_with(888)
        mock_killpg.assert_called_once_with(888, signal.SIGTERM)

    @pytest.mark.asyncio
    @patch("gobby.agents.kill._run_subprocess")
    @patch("gobby.agents.kill.os.kill")
    @patch("gobby.agents.kill.os.killpg")
    @patch("gobby.agents.kill.os.getpgid", return_value=777)
    async def test_kill_pid_from_pgrep(
        self, mock_getpgid, mock_killpg, mock_kill, mock_run, agent_run, mock_db
    ):
        agent_run.pid = None

        def _run_side_effect(*args, **kwargs):
            cmd = args[0] if args else ""
            if cmd == "pgrep":
                return (0, "777\n", "")
            return (1, "", "")

        mock_run.side_effect = _run_side_effect

        res = await kill_agent(agent_run, mock_db, timeout=0)
        assert res["success"] is True
        assert res["pid"] == 777
        assert res["found_via"] == "pgrep"
        mock_getpgid.assert_called_once_with(777)
        mock_killpg.assert_called_once_with(777, signal.SIGTERM)

    @pytest.mark.asyncio
    @patch("gobby.agents.kill._run_subprocess")
    @patch("gobby.agents.kill.os.kill")
    @patch("gobby.agents.kill.os.killpg")
    @patch("gobby.agents.kill.os.getpgid", return_value=778)
    async def test_kill_pid_from_pgrep_disambiguation(
        self, mock_getpgid, mock_killpg, mock_kill, mock_run, agent_run, mock_db
    ):
        agent_run.pid = None
        agent_run.provider = "claude"

        def _run_side_effect(*args, **kwargs):
            cmd = args[0] if args else ""
            if cmd == "pgrep":
                return (0, "777\n778\n", "")
            return (1, "", "")

        mock_run.side_effect = _run_side_effect

        res = await kill_agent(agent_run, mock_db, timeout=0)
        assert res["success"] is True
        assert res["pid"] == 778
        assert res["found_via"] == "pgrep_disambiguated"
        mock_getpgid.assert_called_once_with(778)
        mock_killpg.assert_called_once_with(778, signal.SIGTERM)

    @pytest.mark.asyncio
    @patch("gobby.agents.kill.os.kill")
    async def test_kill_already_dead_prior_to_signal(self, mock_kill, agent_run, mock_db):
        agent_run.pid = 999
        # os.kill(pid, 0) throws ProcessLookupError
        mock_kill.side_effect = ProcessLookupError("already dead")

        res = await kill_agent(agent_run, mock_db, timeout=0)
        assert res["success"] is True
        assert res["already_dead"] is True
        mock_kill.assert_called_once_with(999, 0)

    @pytest.mark.asyncio
    @patch("gobby.agents.kill.os.kill")
    @patch("gobby.agents.kill.asyncio.sleep")
    @patch("gobby.agents.kill.os.killpg")
    @patch("gobby.agents.kill.os.getpgid", return_value=999)
    async def test_kill_escalates_to_kill(
        self, mock_getpgid, mock_killpg, mock_sleep, mock_kill, agent_run, mock_db
    ):
        agent_run.pid = 999
        mock_kill.side_effect = [None, None, ProcessLookupError("dead after sigkill")]

        # Custom side effect for os.kill
        # call 1: os.kill(999, 0) -> pass
        # call 2: os.kill(999, SIGTERM) -> pass
        # calls 3-N in loop: os.kill(999, 0) -> wait until timeout expires...
        # Wait, if we use time logic, we need to mock asyncio.get_running_loop.time()
        with patch("gobby.agents.kill.asyncio.get_running_loop") as mock_loop_getter:
            mock_loop = MagicMock()
            # simulate time passing
            # Start, Loop 1 check, Exceeded deadline + extras to avoid StopIteration
            mock_loop.time.side_effect = [0.0, 0.0, 1.95, 10.0, 10.0, 10.0, 10.0]
            mock_loop_getter.return_value = mock_loop

            res = await kill_agent(agent_run, mock_db, timeout=2.0)

            assert res["success"] is True
            assert res["pid"] == 999
            assert res["signal"] == "TERM"
            assert res["found_via"] == "db"
            assert mock_kill.call_args_list == [((999, 0),), ((999, 0),), ((999, 0),)]
            mock_sleep.assert_awaited_once()
            assert mock_sleep.await_args.args[0] == pytest.approx(0.05)
            mock_getpgid.assert_any_call(999)
            mock_killpg.assert_any_call(999, signal.SIGTERM)
            mock_killpg.assert_any_call(999, signal.SIGKILL)

    @pytest.mark.asyncio
    @patch("gobby.agents.kill._run_subprocess")
    @patch("gobby.agents.kill._close_terminal_window")
    async def test_missing_child_session_does_not_target_parent_terminal(
        self,
        mock_close_window: AsyncMock,
        mock_run: AsyncMock,
        agent_run: AgentRun,
        mock_db: MagicMock,
    ) -> None:
        agent_run.child_session_id = None
        agent_run.parent_session_id = "parent-session"
        agent_run.pid = None

        res = await kill_agent(agent_run, mock_db, close_terminal=True)

        assert res == {
            "success": False,
            "error": "No target PID found",
            "error_code": "no_target_pid",
        }
        mock_close_window.assert_not_called()
        mock_run.assert_not_called()


@pytest.mark.asyncio
async def test_close_terminal_treats_exited_terminal_row_as_already_dead() -> None:
    """agent_runs.terminal_id outlives the row's live state; an exited row needs no kill."""
    from gobby.terminals import TerminalRuntimeRegistry
    from gobby.terminals.services import TerminalServices
    from tests.terminals.fakes import FakeRuntime, MemoryTerminalStore, make_memory_terminal

    terminal = make_memory_terminal()
    store = MemoryTerminalStore(terminal)
    store.mark_exited(terminal.id)
    runtime = FakeRuntime()
    registry = TerminalRuntimeRegistry()
    registry.register(runtime)
    run = AgentRun(
        id="run-exited",
        parent_session_id="parent1",
        child_session_id="sess1",
        provider="claude",
        prompt="do it",
        status="running",
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
        updated_at=datetime(2024, 1, 1, tzinfo=UTC),
        terminal_id=terminal.id,
    )

    result = await kill_agent(
        run,
        MagicMock(),
        close_terminal=True,
        terminal_services=TerminalServices(manager=store, registry=registry),
    )

    assert result["success"] is True
    assert result["already_dead"] is True
    assert result["method"] == "terminal_exited"
    assert result["terminal_close"]["terminal_id"] == terminal.id
    assert runtime.killed == []
