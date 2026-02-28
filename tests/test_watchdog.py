"""Tests for daemon watchdog process.

Exercises the real Watchdog class with mocked external I/O (httpx, subprocess, os.kill).
Targets 90%+ statement coverage of src/gobby/watchdog.py.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from gobby.config.watchdog import WatchdogConfig
from gobby.watchdog import Watchdog, get_gobby_home

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config() -> WatchdogConfig:
    return WatchdogConfig(
        failure_threshold=3,
        health_check_interval=1,
        restart_cooldown=10,
        max_restarts_per_hour=5,
    )


@pytest.fixture
def watchdog(config: WatchdogConfig, tmp_path: Path) -> Watchdog:
    with patch("gobby.watchdog.get_gobby_home", return_value=tmp_path):
        return Watchdog(daemon_port=60887, config=config, verbose=False)


@pytest.fixture
def verbose_watchdog(config: WatchdogConfig, tmp_path: Path) -> Watchdog:
    with patch("gobby.watchdog.get_gobby_home", return_value=tmp_path):
        return Watchdog(daemon_port=60887, config=config, verbose=True)


# ---------------------------------------------------------------------------
# get_gobby_home()
# ---------------------------------------------------------------------------


class TestGetGobbyHome:
    def test_default_home(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            result = get_gobby_home()
            assert result == Path.home() / ".gobby"

    def test_custom_home_via_env(self) -> None:
        with patch.dict("os.environ", {"GOBBY_HOME": "/custom/gobby"}):
            result = get_gobby_home()
            assert result == Path("/custom/gobby")


# ---------------------------------------------------------------------------
# Watchdog.__init__
# ---------------------------------------------------------------------------


class TestWatchdogInit:
    def test_default_config(self, tmp_path: Path) -> None:
        with patch("gobby.watchdog.get_gobby_home", return_value=tmp_path):
            wd = Watchdog(daemon_port=9999)
        assert wd.daemon_port == 9999
        assert isinstance(wd.config, WatchdogConfig)
        assert wd.verbose is False
        assert wd.consecutive_failures == 0
        assert wd.running is True
        assert wd.last_restart_time == 0

    def test_custom_config(self, config: WatchdogConfig, tmp_path: Path) -> None:
        with patch("gobby.watchdog.get_gobby_home", return_value=tmp_path):
            wd = Watchdog(daemon_port=1234, config=config, verbose=True)
        assert wd.daemon_port == 1234
        assert wd.config is config
        assert wd.verbose is True

    def test_restart_times_deque_maxlen(self, config: WatchdogConfig, tmp_path: Path) -> None:
        with patch("gobby.watchdog.get_gobby_home", return_value=tmp_path):
            wd = Watchdog(daemon_port=1234, config=config)
        assert wd.restart_times.maxlen == config.max_restarts_per_hour


# ---------------------------------------------------------------------------
# _handle_shutdown
# ---------------------------------------------------------------------------


class TestHandleShutdown:
    def test_sets_running_false(self, watchdog: Watchdog) -> None:
        assert watchdog.running is True
        watchdog._handle_shutdown(signal.SIGTERM, None)
        assert watchdog.running is False

    def test_with_sigint(self, watchdog: Watchdog) -> None:
        watchdog._handle_shutdown(signal.SIGINT, None)
        assert watchdog.running is False


# ---------------------------------------------------------------------------
# check_health
# ---------------------------------------------------------------------------


class TestCheckHealth:
    @patch("gobby.watchdog.httpx.get")
    def test_healthy_response(self, mock_get: MagicMock, watchdog: Watchdog) -> None:
        mock_get.return_value = MagicMock(status_code=200)
        assert watchdog.check_health() is True
        mock_get.assert_called_once_with(
            "http://localhost:60887/api/admin/health",
            timeout=5.0,
        )

    @patch("gobby.watchdog.httpx.get")
    def test_unhealthy_status_code(self, mock_get: MagicMock, watchdog: Watchdog) -> None:
        mock_get.return_value = MagicMock(status_code=500)
        assert watchdog.check_health() is False

    @patch("gobby.watchdog.httpx.get")
    def test_status_code_503(self, mock_get: MagicMock, watchdog: Watchdog) -> None:
        mock_get.return_value = MagicMock(status_code=503)
        assert watchdog.check_health() is False

    @patch("gobby.watchdog.httpx.get")
    def test_connection_refused(self, mock_get: MagicMock, watchdog: Watchdog) -> None:
        mock_get.side_effect = httpx.ConnectError("Connection refused")
        assert watchdog.check_health() is False

    @patch("gobby.watchdog.httpx.get")
    def test_timeout(self, mock_get: MagicMock, watchdog: Watchdog) -> None:
        mock_get.side_effect = httpx.TimeoutException("timed out")
        assert watchdog.check_health() is False

    @patch("gobby.watchdog.httpx.get")
    def test_generic_exception(self, mock_get: MagicMock, watchdog: Watchdog) -> None:
        mock_get.side_effect = RuntimeError("unexpected error")
        assert watchdog.check_health() is False

    @patch("gobby.watchdog.httpx.get")
    def test_verbose_logs_debug_on_success(
        self, mock_get: MagicMock, verbose_watchdog: Watchdog
    ) -> None:
        mock_get.return_value = MagicMock(status_code=200)
        assert verbose_watchdog.check_health() is True

    @patch("gobby.watchdog.httpx.get")
    def test_non_verbose_skips_debug_log(self, mock_get: MagicMock, watchdog: Watchdog) -> None:
        mock_get.return_value = MagicMock(status_code=200)
        # Should succeed without verbose debug logging
        assert watchdog.check_health() is True
        assert watchdog.verbose is False


# ---------------------------------------------------------------------------
# _is_daemon_running
# ---------------------------------------------------------------------------


class TestIsDaemonRunning:
    @patch("gobby.watchdog.get_gobby_home")
    def test_no_pid_file(self, mock_home: MagicMock, watchdog: Watchdog, tmp_path: Path) -> None:
        mock_home.return_value = tmp_path
        assert watchdog._is_daemon_running() is False

    @patch("gobby.watchdog.get_gobby_home")
    def test_valid_pid_current_process(
        self, mock_home: MagicMock, watchdog: Watchdog, tmp_path: Path
    ) -> None:
        pid_file = tmp_path / "gobby.pid"
        pid_file.write_text(str(os.getpid()))
        mock_home.return_value = tmp_path
        assert watchdog._is_daemon_running() is True

    @patch("gobby.watchdog.get_gobby_home")
    def test_dead_pid(self, mock_home: MagicMock, watchdog: Watchdog, tmp_path: Path) -> None:
        pid_file = tmp_path / "gobby.pid"
        pid_file.write_text("999999999")
        mock_home.return_value = tmp_path
        assert watchdog._is_daemon_running() is False

    @patch("gobby.watchdog.get_gobby_home")
    def test_invalid_pid_value_error(
        self, mock_home: MagicMock, watchdog: Watchdog, tmp_path: Path
    ) -> None:
        pid_file = tmp_path / "gobby.pid"
        pid_file.write_text("not-a-number")
        mock_home.return_value = tmp_path
        assert watchdog._is_daemon_running() is False

    @patch("gobby.watchdog.get_gobby_home")
    @patch("gobby.watchdog.os.kill", side_effect=OSError("Operation not permitted"))
    def test_os_error_on_kill(
        self, mock_kill: MagicMock, mock_home: MagicMock, watchdog: Watchdog, tmp_path: Path
    ) -> None:
        pid_file = tmp_path / "gobby.pid"
        pid_file.write_text("12345")
        mock_home.return_value = tmp_path
        assert watchdog._is_daemon_running() is False


# ---------------------------------------------------------------------------
# _circuit_breaker_triggered
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_not_triggered_when_no_restarts(self, watchdog: Watchdog) -> None:
        assert watchdog._circuit_breaker_triggered() is False

    def test_not_triggered_when_below_threshold(self, watchdog: Watchdog) -> None:
        watchdog.restart_times.append(time.time())
        assert watchdog._circuit_breaker_triggered() is False

    def test_triggered_when_at_max(self, watchdog: Watchdog) -> None:
        now = time.time()
        for _ in range(watchdog.config.max_restarts_per_hour):
            watchdog.restart_times.append(now)
        assert watchdog._circuit_breaker_triggered() is True

    def test_old_restarts_not_counted(self, watchdog: Watchdog) -> None:
        old = time.time() - 3700  # Over an hour ago
        for _ in range(watchdog.config.max_restarts_per_hour):
            watchdog.restart_times.append(old)
        assert watchdog._circuit_breaker_triggered() is False

    def test_mix_of_old_and_recent(self, watchdog: Watchdog) -> None:
        old = time.time() - 3700
        now = time.time()
        # 3 old + 2 recent = below threshold of 5
        for _ in range(3):
            watchdog.restart_times.append(old)
        for _ in range(2):
            watchdog.restart_times.append(now)
        assert watchdog._circuit_breaker_triggered() is False


# ---------------------------------------------------------------------------
# _cooldown_active
# ---------------------------------------------------------------------------


class TestCooldown:
    def test_no_cooldown_initially(self, watchdog: Watchdog) -> None:
        assert watchdog._cooldown_active() is False

    def test_cooldown_active_after_recent_restart(self, watchdog: Watchdog) -> None:
        watchdog.last_restart_time = time.time()
        assert watchdog._cooldown_active() is True

    def test_cooldown_expired(self, watchdog: Watchdog) -> None:
        watchdog.last_restart_time = time.time() - watchdog.config.restart_cooldown - 1
        assert watchdog._cooldown_active() is False


# ---------------------------------------------------------------------------
# should_restart
# ---------------------------------------------------------------------------


class TestShouldRestart:
    def test_below_threshold(self, watchdog: Watchdog) -> None:
        watchdog.consecutive_failures = 1
        assert watchdog.should_restart() is False

    def test_at_threshold(self, watchdog: Watchdog) -> None:
        watchdog.consecutive_failures = 3
        assert watchdog.should_restart() is True

    def test_above_threshold(self, watchdog: Watchdog) -> None:
        watchdog.consecutive_failures = 10
        assert watchdog.should_restart() is True

    def test_cooldown_blocks_restart(self, watchdog: Watchdog) -> None:
        watchdog.consecutive_failures = 3
        watchdog.last_restart_time = time.time()
        assert watchdog.should_restart() is False

    def test_circuit_breaker_blocks_restart(self, watchdog: Watchdog) -> None:
        watchdog.consecutive_failures = 3
        now = time.time()
        for _ in range(watchdog.config.max_restarts_per_hour):
            watchdog.restart_times.append(now)
        assert watchdog.should_restart() is False

    def test_cooldown_checked_before_circuit_breaker(self, watchdog: Watchdog) -> None:
        """Verify cooldown is checked first (line ordering in should_restart)."""
        watchdog.consecutive_failures = 3
        watchdog.last_restart_time = time.time()
        # Even if circuit breaker would NOT trigger, cooldown blocks first
        assert watchdog.should_restart() is False


# ---------------------------------------------------------------------------
# restart_daemon
# ---------------------------------------------------------------------------


class TestRestartDaemon:
    @patch("gobby.watchdog.time.sleep")
    @patch("gobby.watchdog.subprocess.Popen")
    @patch("gobby.watchdog.load_config")
    @patch("gobby.watchdog.get_gobby_home")
    def test_restart_success_no_existing_pid(
        self,
        mock_home: MagicMock,
        mock_load_config: MagicMock,
        mock_popen: MagicMock,
        mock_sleep: MagicMock,
        watchdog: Watchdog,
        tmp_path: Path,
    ) -> None:
        mock_home.return_value = tmp_path

        # Setup config mock
        mock_config = MagicMock()
        mock_config.logging.client = str(tmp_path / "daemon.log")
        mock_config.logging.client_error = str(tmp_path / "daemon-error.log")
        mock_load_config.return_value = mock_config

        # Setup Popen mock
        mock_process = MagicMock()
        mock_process.pid = 42
        mock_popen.return_value = mock_process

        # Make health check succeed after restart
        with patch.object(watchdog, "check_health", return_value=True):
            result = watchdog.restart_daemon()

        assert result is True
        assert watchdog.consecutive_failures == 0
        assert watchdog.last_restart_time > 0
        assert len(watchdog.restart_times) == 1

        # Verify PID file was written
        pid_file = tmp_path / "gobby.pid"
        assert pid_file.exists()
        assert pid_file.read_text() == "42"

    @patch("gobby.watchdog.time.sleep")
    @patch("gobby.watchdog.subprocess.Popen")
    @patch("gobby.watchdog.load_config")
    @patch("gobby.watchdog.get_gobby_home")
    @patch("gobby.watchdog.os.kill")
    def test_restart_stops_existing_daemon_gracefully(
        self,
        mock_kill: MagicMock,
        mock_home: MagicMock,
        mock_load_config: MagicMock,
        mock_popen: MagicMock,
        mock_sleep: MagicMock,
        watchdog: Watchdog,
        tmp_path: Path,
    ) -> None:
        mock_home.return_value = tmp_path

        # Create existing PID file
        pid_file = tmp_path / "gobby.pid"
        pid_file.write_text("1234")

        # First os.kill(pid, SIGTERM) succeeds, second os.kill(pid, 0) raises ProcessLookupError
        mock_kill.side_effect = [None, ProcessLookupError()]

        mock_config = MagicMock()
        mock_config.logging.client = str(tmp_path / "daemon.log")
        mock_config.logging.client_error = str(tmp_path / "daemon-error.log")
        mock_load_config.return_value = mock_config

        mock_process = MagicMock()
        mock_process.pid = 5678
        mock_popen.return_value = mock_process

        with patch.object(watchdog, "check_health", return_value=True):
            result = watchdog.restart_daemon()

        assert result is True
        # Verify SIGTERM was sent
        mock_kill.assert_any_call(1234, signal.SIGTERM)

    @patch("gobby.watchdog.time.sleep")
    @patch("gobby.watchdog.subprocess.Popen")
    @patch("gobby.watchdog.load_config")
    @patch("gobby.watchdog.get_gobby_home")
    @patch("gobby.watchdog.os.kill")
    def test_restart_force_kills_when_graceful_fails(
        self,
        mock_kill: MagicMock,
        mock_home: MagicMock,
        mock_load_config: MagicMock,
        mock_popen: MagicMock,
        mock_sleep: MagicMock,
        watchdog: Watchdog,
        tmp_path: Path,
    ) -> None:
        mock_home.return_value = tmp_path
        pid_file = tmp_path / "gobby.pid"
        pid_file.write_text("1234")

        # SIGTERM succeeds, then 100 os.kill(pid, 0) checks succeed (process still alive),
        # then SIGKILL succeeds, then final check raises ProcessLookupError
        effects = [None]  # SIGTERM
        effects += [None] * 100  # 100 os.kill(pid, 0) checks that all succeed
        effects += [None]  # SIGKILL succeeds
        mock_kill.side_effect = effects

        mock_config = MagicMock()
        mock_config.logging.client = str(tmp_path / "daemon.log")
        mock_config.logging.client_error = str(tmp_path / "daemon-error.log")
        mock_load_config.return_value = mock_config

        mock_process = MagicMock()
        mock_process.pid = 5678
        mock_popen.return_value = mock_process

        with patch.object(watchdog, "check_health", return_value=True):
            result = watchdog.restart_daemon()

        assert result is True
        # Verify SIGKILL was sent
        mock_kill.assert_any_call(1234, signal.SIGKILL)

    @patch("gobby.watchdog.time.sleep")
    @patch("gobby.watchdog.subprocess.Popen")
    @patch("gobby.watchdog.load_config")
    @patch("gobby.watchdog.get_gobby_home")
    @patch("gobby.watchdog.os.kill")
    def test_restart_force_kill_process_already_dead(
        self,
        mock_kill: MagicMock,
        mock_home: MagicMock,
        mock_load_config: MagicMock,
        mock_popen: MagicMock,
        mock_sleep: MagicMock,
        watchdog: Watchdog,
        tmp_path: Path,
    ) -> None:
        mock_home.return_value = tmp_path
        pid_file = tmp_path / "gobby.pid"
        pid_file.write_text("1234")

        # SIGTERM succeeds, 100 alive checks, SIGKILL raises ProcessLookupError
        effects = [None]  # SIGTERM
        effects += [None] * 100  # process stays alive
        effects += [ProcessLookupError()]  # SIGKILL - already dead
        mock_kill.side_effect = effects

        mock_config = MagicMock()
        mock_config.logging.client = str(tmp_path / "daemon.log")
        mock_config.logging.client_error = str(tmp_path / "daemon-error.log")
        mock_load_config.return_value = mock_config

        mock_process = MagicMock()
        mock_process.pid = 5678
        mock_popen.return_value = mock_process

        with patch.object(watchdog, "check_health", return_value=True):
            result = watchdog.restart_daemon()

        assert result is True

    @patch("gobby.watchdog.time.sleep")
    @patch("gobby.watchdog.subprocess.Popen")
    @patch("gobby.watchdog.load_config")
    @patch("gobby.watchdog.get_gobby_home")
    @patch("gobby.watchdog.os.kill")
    def test_restart_existing_pid_invalid_value(
        self,
        mock_kill: MagicMock,
        mock_home: MagicMock,
        mock_load_config: MagicMock,
        mock_popen: MagicMock,
        mock_sleep: MagicMock,
        watchdog: Watchdog,
        tmp_path: Path,
    ) -> None:
        mock_home.return_value = tmp_path
        pid_file = tmp_path / "gobby.pid"
        pid_file.write_text("not-a-number")

        mock_config = MagicMock()
        mock_config.logging.client = str(tmp_path / "daemon.log")
        mock_config.logging.client_error = str(tmp_path / "daemon-error.log")
        mock_load_config.return_value = mock_config

        mock_process = MagicMock()
        mock_process.pid = 42
        mock_popen.return_value = mock_process

        with patch.object(watchdog, "check_health", return_value=True):
            result = watchdog.restart_daemon()

        assert result is True
        # PID file should be cleaned up
        assert not (tmp_path / "gobby.pid").read_text() == "not-a-number"

    @patch("gobby.watchdog.time.sleep")
    @patch("gobby.watchdog.subprocess.Popen")
    @patch("gobby.watchdog.load_config")
    @patch("gobby.watchdog.get_gobby_home")
    @patch("gobby.watchdog.os.kill")
    def test_restart_error_stopping_daemon(
        self,
        mock_kill: MagicMock,
        mock_home: MagicMock,
        mock_load_config: MagicMock,
        mock_popen: MagicMock,
        mock_sleep: MagicMock,
        watchdog: Watchdog,
        tmp_path: Path,
    ) -> None:
        mock_home.return_value = tmp_path
        pid_file = tmp_path / "gobby.pid"
        pid_file.write_text("1234")

        # Generic exception when trying to stop
        mock_kill.side_effect = OSError("Permission denied")

        mock_config = MagicMock()
        mock_config.logging.client = str(tmp_path / "daemon.log")
        mock_config.logging.client_error = str(tmp_path / "daemon-error.log")
        mock_load_config.return_value = mock_config

        mock_process = MagicMock()
        mock_process.pid = 42
        mock_popen.return_value = mock_process

        with patch.object(watchdog, "check_health", return_value=True):
            result = watchdog.restart_daemon()

        # Should still succeed because starting new daemon works
        assert result is True

    @patch("gobby.watchdog.time.sleep")
    @patch("gobby.watchdog.subprocess.Popen")
    @patch("gobby.watchdog.load_config")
    @patch("gobby.watchdog.get_gobby_home")
    def test_restart_health_check_never_passes(
        self,
        mock_home: MagicMock,
        mock_load_config: MagicMock,
        mock_popen: MagicMock,
        mock_sleep: MagicMock,
        watchdog: Watchdog,
        tmp_path: Path,
    ) -> None:
        mock_home.return_value = tmp_path

        mock_config = MagicMock()
        mock_config.logging.client = str(tmp_path / "daemon.log")
        mock_config.logging.client_error = str(tmp_path / "daemon-error.log")
        mock_load_config.return_value = mock_config

        mock_process = MagicMock()
        mock_process.pid = 42
        mock_popen.return_value = mock_process

        with patch.object(watchdog, "check_health", return_value=False):
            result = watchdog.restart_daemon()

        assert result is False

    @patch("gobby.watchdog.time.sleep")
    @patch("gobby.watchdog.load_config")
    @patch("gobby.watchdog.get_gobby_home")
    def test_restart_popen_fails(
        self,
        mock_home: MagicMock,
        mock_load_config: MagicMock,
        mock_sleep: MagicMock,
        watchdog: Watchdog,
        tmp_path: Path,
    ) -> None:
        mock_home.return_value = tmp_path

        mock_config = MagicMock()
        mock_config.logging.client = str(tmp_path / "daemon.log")
        mock_config.logging.client_error = str(tmp_path / "daemon-error.log")
        mock_load_config.return_value = mock_config

        with patch("gobby.watchdog.subprocess.Popen", side_effect=OSError("No such file")):
            result = watchdog.restart_daemon()

        assert result is False

    @patch("gobby.watchdog.time.sleep")
    @patch("gobby.watchdog.load_config", side_effect=Exception("Config load error"))
    @patch("gobby.watchdog.get_gobby_home")
    def test_restart_load_config_fails(
        self,
        mock_home: MagicMock,
        mock_load_config: MagicMock,
        mock_sleep: MagicMock,
        watchdog: Watchdog,
        tmp_path: Path,
    ) -> None:
        mock_home.return_value = tmp_path
        result = watchdog.restart_daemon()
        assert result is False

    @patch("gobby.watchdog.time.sleep")
    @patch("gobby.watchdog.subprocess.Popen")
    @patch("gobby.watchdog.load_config")
    @patch("gobby.watchdog.get_gobby_home")
    def test_restart_verbose_mode_adds_flag(
        self,
        mock_home: MagicMock,
        mock_load_config: MagicMock,
        mock_popen: MagicMock,
        mock_sleep: MagicMock,
        verbose_watchdog: Watchdog,
        tmp_path: Path,
    ) -> None:
        mock_home.return_value = tmp_path

        mock_config = MagicMock()
        mock_config.logging.client = str(tmp_path / "daemon.log")
        mock_config.logging.client_error = str(tmp_path / "daemon-error.log")
        mock_load_config.return_value = mock_config

        mock_process = MagicMock()
        mock_process.pid = 42
        mock_popen.return_value = mock_process

        with patch.object(verbose_watchdog, "check_health", return_value=True):
            result = verbose_watchdog.restart_daemon()

        assert result is True
        # Verify --verbose flag was included
        popen_args = mock_popen.call_args
        cmd = popen_args[0][0] if popen_args[0] else popen_args[1]["cmd"]
        assert "--verbose" in cmd

    @patch("gobby.watchdog.time.sleep")
    @patch("gobby.watchdog.subprocess.Popen")
    @patch("gobby.watchdog.load_config")
    @patch("gobby.watchdog.get_gobby_home")
    @patch("gobby.watchdog.os.kill")
    def test_restart_existing_pid_process_not_found(
        self,
        mock_kill: MagicMock,
        mock_home: MagicMock,
        mock_load_config: MagicMock,
        mock_popen: MagicMock,
        mock_sleep: MagicMock,
        watchdog: Watchdog,
        tmp_path: Path,
    ) -> None:
        """When SIGTERM raises ProcessLookupError, we clean up PID file."""
        mock_home.return_value = tmp_path
        pid_file = tmp_path / "gobby.pid"
        pid_file.write_text("1234")

        mock_kill.side_effect = ProcessLookupError()

        mock_config = MagicMock()
        mock_config.logging.client = str(tmp_path / "daemon.log")
        mock_config.logging.client_error = str(tmp_path / "daemon-error.log")
        mock_load_config.return_value = mock_config

        mock_process = MagicMock()
        mock_process.pid = 42
        mock_popen.return_value = mock_process

        with patch.object(watchdog, "check_health", return_value=True):
            result = watchdog.restart_daemon()

        assert result is True


# ---------------------------------------------------------------------------
# run (main loop)
# ---------------------------------------------------------------------------


class TestRun:
    @patch("gobby.watchdog.time.sleep")
    @patch("gobby.watchdog.get_gobby_home")
    def test_run_healthy_then_shutdown(
        self,
        mock_home: MagicMock,
        mock_sleep: MagicMock,
        watchdog: Watchdog,
        tmp_path: Path,
    ) -> None:
        """Run loop with healthy checks, then shutdown."""
        mock_home.return_value = tmp_path

        call_count = 0

        def health_side_effect() -> bool:
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                watchdog.running = False
            return True

        with patch.object(watchdog, "check_health", side_effect=health_side_effect):
            watchdog.run()

        assert watchdog.consecutive_failures == 0
        # PID file should be cleaned up
        assert not (tmp_path / "watchdog.pid").exists()

    @patch("gobby.watchdog.time.sleep")
    @patch("gobby.watchdog.get_gobby_home")
    def test_run_writes_watchdog_pid_file(
        self,
        mock_home: MagicMock,
        mock_sleep: MagicMock,
        watchdog: Watchdog,
        tmp_path: Path,
    ) -> None:
        mock_home.return_value = tmp_path
        watchdog.running = False  # Exit immediately

        with patch.object(watchdog, "check_health", return_value=True):
            watchdog.run()

        # PID file is cleaned in finally, but was written during run
        assert not (tmp_path / "watchdog.pid").exists()

    @patch("gobby.watchdog.time.sleep")
    @patch("gobby.watchdog.get_gobby_home")
    def test_run_failure_increments_and_triggers_restart(
        self,
        mock_home: MagicMock,
        mock_sleep: MagicMock,
        watchdog: Watchdog,
        tmp_path: Path,
    ) -> None:
        mock_home.return_value = tmp_path

        call_count = 0

        def health_side_effect() -> bool:
            nonlocal call_count
            call_count += 1
            if call_count > 4:
                watchdog.running = False
                return True
            return False  # Fail first 4 times

        with (
            patch.object(watchdog, "check_health", side_effect=health_side_effect),
            patch.object(watchdog, "restart_daemon", return_value=True) as mock_restart,
        ):
            watchdog.run()

        # Should have attempted restart after 3 failures (threshold)
        mock_restart.assert_called()

    @patch("gobby.watchdog.time.sleep")
    @patch("gobby.watchdog.get_gobby_home")
    def test_run_restart_failure_logs_error(
        self,
        mock_home: MagicMock,
        mock_sleep: MagicMock,
        watchdog: Watchdog,
        tmp_path: Path,
    ) -> None:
        mock_home.return_value = tmp_path

        call_count = 0

        def health_side_effect() -> bool:
            nonlocal call_count
            call_count += 1
            if call_count > 4:
                watchdog.running = False
                return True
            return False

        with (
            patch.object(watchdog, "check_health", side_effect=health_side_effect),
            patch.object(watchdog, "restart_daemon", return_value=False) as mock_restart,
        ):
            watchdog.run()

        mock_restart.assert_called()

    @patch("gobby.watchdog.time.sleep")
    @patch("gobby.watchdog.get_gobby_home")
    def test_run_interruptible_sleep(
        self,
        mock_home: MagicMock,
        mock_sleep: MagicMock,
        watchdog: Watchdog,
        tmp_path: Path,
    ) -> None:
        """Verify the sleep loop breaks when running is set to False."""
        mock_home.return_value = tmp_path

        sleep_count = 0

        def sleep_side_effect(duration: float) -> None:
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count >= 2:
                watchdog.running = False

        mock_sleep.side_effect = sleep_side_effect

        with patch.object(watchdog, "check_health", return_value=True):
            watchdog.run()

    @patch("gobby.watchdog.time.sleep")
    @patch("gobby.watchdog.get_gobby_home")
    def test_run_health_check_resets_failures(
        self,
        mock_home: MagicMock,
        mock_sleep: MagicMock,
        watchdog: Watchdog,
        tmp_path: Path,
    ) -> None:
        mock_home.return_value = tmp_path

        results = [False, False, True]  # 2 failures then success
        idx = 0

        def health_side_effect() -> bool:
            nonlocal idx
            if idx >= len(results):
                watchdog.running = False
                return True
            r = results[idx]
            idx += 1
            return r

        with patch.object(watchdog, "check_health", side_effect=health_side_effect):
            watchdog.run()

        assert watchdog.consecutive_failures == 0


# ---------------------------------------------------------------------------
# main() entry point
# ---------------------------------------------------------------------------


class TestMain:
    @patch("gobby.watchdog.Watchdog")
    @patch("gobby.watchdog.load_config")
    @patch("gobby.watchdog.get_gobby_home")
    @patch("sys.argv", ["watchdog", "--port", "60887"])
    def test_main_basic(
        self,
        mock_home: MagicMock,
        mock_load_config: MagicMock,
        mock_watchdog_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        from gobby.watchdog import main

        mock_home.return_value = tmp_path
        (tmp_path / "logs").mkdir()

        mock_config = MagicMock()
        mock_config.logging.watchdog = str(tmp_path / "logs" / "watchdog.log")
        mock_config.watchdog = WatchdogConfig()
        mock_load_config.return_value = mock_config

        mock_wd_instance = MagicMock()
        mock_watchdog_cls.return_value = mock_wd_instance

        main()

        mock_watchdog_cls.assert_called_once_with(
            daemon_port=60887,
            config=mock_config.watchdog,
            verbose=False,
        )
        mock_wd_instance.run.assert_called_once()

    @patch("gobby.watchdog.Watchdog")
    @patch("gobby.watchdog.load_config")
    @patch("gobby.watchdog.get_gobby_home")
    @patch("sys.argv", ["watchdog", "--port", "60887", "--verbose"])
    def test_main_verbose(
        self,
        mock_home: MagicMock,
        mock_load_config: MagicMock,
        mock_watchdog_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        from gobby.watchdog import main

        mock_home.return_value = tmp_path
        (tmp_path / "logs").mkdir()

        mock_config = MagicMock()
        mock_config.logging.watchdog = str(tmp_path / "logs" / "watchdog.log")
        mock_config.watchdog = WatchdogConfig()
        mock_load_config.return_value = mock_config

        mock_wd_instance = MagicMock()
        mock_watchdog_cls.return_value = mock_wd_instance

        main()

        mock_watchdog_cls.assert_called_once_with(
            daemon_port=60887,
            config=mock_config.watchdog,
            verbose=True,
        )

    @patch("gobby.watchdog.Watchdog")
    @patch("gobby.watchdog.load_config", side_effect=Exception("No config"))
    @patch("gobby.watchdog.get_gobby_home")
    @patch("sys.argv", ["watchdog", "--port", "60887"])
    def test_main_config_load_failure_uses_defaults(
        self,
        mock_home: MagicMock,
        mock_load_config: MagicMock,
        mock_watchdog_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        from gobby.watchdog import main

        mock_home.return_value = tmp_path

        mock_wd_instance = MagicMock()
        mock_watchdog_cls.return_value = mock_wd_instance

        main()

        # Should use default WatchdogConfig when load_config fails
        call_kwargs = mock_watchdog_cls.call_args[1]
        assert isinstance(call_kwargs["config"], WatchdogConfig)
        mock_wd_instance.run.assert_called_once()
