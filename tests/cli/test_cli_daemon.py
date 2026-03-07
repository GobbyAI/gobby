"""Comprehensive tests for the CLI daemon module.

Tests the start, stop, restart, and status commands with various
argument combinations and error scenarios using Click's CliRunner.
"""

import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import psutil
import pytest
from click.testing import CliRunner

from gobby.cli import cli

pytestmark = pytest.mark.unit


class TestStartCommand:
    """Tests for the 'start' command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    def test_start_help(self, runner: CliRunner) -> None:
        """Test start --help displays help text."""
        result = runner.invoke(cli, ["start", "--help"])
        assert result.exit_code == 0
        assert "Start the Gobby daemon" in result.output
        assert "--verbose" in result.output

    @patch("gobby.cli.daemon.fetch_rich_status")
    @patch("gobby.cli.daemon.httpx.get")
    @patch("gobby.cli.daemon.subprocess.Popen")
    @patch("gobby.cli.daemon.wait_for_port_available")
    @patch("gobby.cli.daemon.is_port_available")
    @patch("gobby.cli.daemon.kill_all_gobby_daemons")
    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.daemon.time.sleep")
    @patch("gobby.cli.load_config")
    def test_start_success(
        self,
        mock_load_config: MagicMock,
        mock_sleep: MagicMock,
        mock_init_storage: MagicMock,
        mock_kill_daemons: MagicMock,
        mock_is_port_available: MagicMock,
        mock_wait_port: MagicMock,
        mock_popen: MagicMock,
        mock_httpx_get: MagicMock,
        mock_fetch_status: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """Test successful daemon start."""
        mock_load_config.return_value = mock_daemon_config
        mock_kill_daemons.return_value = 0
        mock_is_port_available.return_value = True
        mock_fetch_status.return_value = {}

        # Mock process
        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.poll.return_value = None  # Process is running
        mock_popen.return_value = mock_process

        # Mock successful health check
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_httpx_get.return_value = mock_response

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
        ):
            # Create necessary directories within temp_dir by setting HOME
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            result = runner.invoke(cli, ["start"], env={"HOME": str(temp_dir)})

            assert result.exit_code == 0
            assert "Initializing local storage" in result.output
            mock_init_storage.assert_called_once()
            mock_popen.assert_called_once()

    @patch("gobby.cli.daemon.fetch_rich_status")
    @patch("gobby.cli.daemon.httpx.get")
    @patch("gobby.cli.daemon.subprocess.Popen")
    @patch("gobby.cli.daemon.wait_for_port_available")
    @patch("gobby.cli.daemon.is_port_available")
    @patch("gobby.cli.daemon.kill_all_gobby_daemons")
    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.daemon.time.sleep")
    @patch("gobby.cli.load_config")
    def test_start_with_verbose_flag(
        self,
        mock_load_config: MagicMock,
        mock_sleep: MagicMock,
        mock_init_storage: MagicMock,
        mock_kill_daemons: MagicMock,
        mock_is_port_available: MagicMock,
        mock_wait_port: MagicMock,
        mock_popen: MagicMock,
        mock_httpx_get: MagicMock,
        mock_fetch_status: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """Test start with --verbose flag adds verbose argument to command."""
        mock_load_config.return_value = mock_daemon_config
        mock_kill_daemons.return_value = 0
        mock_is_port_available.return_value = True
        mock_fetch_status.return_value = {}

        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_httpx_get.return_value = mock_response

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
        ):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            result = runner.invoke(cli, ["start", "--verbose"])

            assert result.exit_code == 0
            # Check that --verbose was passed to the subprocess command
            call_args = mock_popen.call_args
            cmd = call_args[0][0]
            assert "--verbose" in cmd

    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.load_config")
    def test_start_daemon_already_running(
        self,
        mock_load_config: MagicMock,
        mock_init_storage: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """Test start when daemon is already running."""
        mock_load_config.return_value = mock_daemon_config

        mock_proc = MagicMock()
        mock_proc.cmdline.return_value = ["python", "-m", "gobby.runner"]

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
            patch("gobby.cli.daemon._is_process_alive", return_value=True),
            patch("gobby.cli.daemon.psutil.Process", return_value=mock_proc),
        ):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            # Create PID file with current process PID (guaranteed to be running)
            pid_file = gobby_dir / "gobby.pid"
            pid_file.write_text(str(os.getpid()))

            result = runner.invoke(cli, ["start"])

            assert result.exit_code == 1
            assert "already running" in result.output

    @patch("gobby.cli.daemon.kill_all_gobby_daemons")
    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.load_config")
    def test_start_removes_stale_pid_file(
        self,
        mock_load_config: MagicMock,
        mock_init_storage: MagicMock,
        mock_kill_daemons: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """Test start removes stale PID file when process not running."""
        mock_load_config.return_value = mock_daemon_config
        mock_kill_daemons.return_value = 0

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
        ):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            # Create PID file with a non-existent PID
            pid_file = gobby_dir / "gobby.pid"
            pid_file.write_text("99999999")

            # The test will proceed to try starting the daemon after removing
            # stale PID - mock the remaining calls to prevent actual daemon start
            with (
                patch("gobby.cli.daemon.is_port_available", return_value=True),
                patch("gobby.cli.daemon.subprocess.Popen") as mock_popen,
                patch("gobby.cli.daemon.httpx.get") as mock_httpx_get,
                patch("gobby.cli.daemon.fetch_rich_status", return_value={}),
                patch("gobby.cli.daemon.time.sleep"),
            ):
                mock_process = MagicMock()
                mock_process.pid = 12345
                mock_process.poll.return_value = None
                mock_popen.return_value = mock_process

                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_httpx_get.return_value = mock_response

                result = runner.invoke(cli, ["start"])

                assert "Removing stale PID file" in result.output

    @patch("gobby.cli.daemon.wait_for_port_available")
    @patch("gobby.cli.daemon.is_port_available")
    @patch("gobby.cli.daemon.kill_all_gobby_daemons")
    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.load_config")
    def test_start_http_port_in_use_timeout(
        self,
        mock_load_config: MagicMock,
        mock_init_storage: MagicMock,
        mock_kill_daemons: MagicMock,
        mock_is_port_available: MagicMock,
        mock_wait_port: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """Test start fails when HTTP port never becomes available."""
        mock_load_config.return_value = mock_daemon_config
        mock_kill_daemons.return_value = 0
        mock_is_port_available.return_value = False
        mock_wait_port.return_value = False  # Port never available

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
        ):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            result = runner.invoke(cli, ["start"])

            assert result.exit_code == 1
            assert "Port" in result.output and "still in use" in result.output

    @patch("gobby.cli.daemon.wait_for_port_available")
    @patch("gobby.cli.daemon.is_port_available")
    @patch("gobby.cli.daemon.kill_all_gobby_daemons")
    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.load_config")
    def test_start_websocket_port_in_use_timeout(
        self,
        mock_load_config: MagicMock,
        mock_init_storage: MagicMock,
        mock_kill_daemons: MagicMock,
        mock_is_port_available: MagicMock,
        mock_wait_port: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """Test start fails when WebSocket port never becomes available."""
        mock_load_config.return_value = mock_daemon_config
        mock_kill_daemons.return_value = 0

        # HTTP port available, WS port not
        def port_available_side_effect(port):
            return port == mock_daemon_config.daemon_port

        mock_is_port_available.side_effect = port_available_side_effect
        mock_wait_port.return_value = False

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
        ):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            result = runner.invoke(cli, ["start"])

            assert result.exit_code == 1
            assert "Port" in result.output and "still in use" in result.output

    @patch("gobby.cli.daemon.httpx.get")
    @patch("gobby.cli.daemon.subprocess.Popen")
    @patch("gobby.cli.daemon.is_port_available")
    @patch("gobby.cli.daemon.kill_all_gobby_daemons")
    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.daemon.time.sleep")
    @patch("gobby.cli.load_config")
    def test_start_process_exits_immediately(
        self,
        mock_load_config: MagicMock,
        mock_sleep: MagicMock,
        mock_init_storage: MagicMock,
        mock_kill_daemons: MagicMock,
        mock_is_port_available: MagicMock,
        mock_popen: MagicMock,
        mock_httpx_get: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """Test start handles process that exits immediately."""
        mock_load_config.return_value = mock_daemon_config
        mock_kill_daemons.return_value = 0
        mock_is_port_available.return_value = True

        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.poll.return_value = 1  # Process exited
        mock_popen.return_value = mock_process

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
        ):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            result = runner.invoke(cli, ["start"])

            assert result.exit_code == 1
            assert "Process exited immediately" in result.output

    @pytest.mark.skip(
        reason="Flaky: FD leak from earlier tests causes OSError in isolated_filesystem"
    )
    @patch("gobby.cli.daemon.httpx.get")
    @patch("gobby.cli.daemon.subprocess.Popen")
    @patch("gobby.cli.daemon.is_port_available")
    @patch("gobby.cli.daemon.kill_all_gobby_daemons")
    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.daemon.time.sleep")
    @patch("gobby.cli.load_config")
    def test_start_health_check_fails(
        self,
        mock_load_config: MagicMock,
        mock_sleep: MagicMock,
        mock_init_storage: MagicMock,
        mock_kill_daemons: MagicMock,
        mock_is_port_available: MagicMock,
        mock_popen: MagicMock,
        mock_httpx_get: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """Test start continues with warning when health check fails."""
        mock_load_config.return_value = mock_daemon_config
        mock_kill_daemons.return_value = 0
        mock_is_port_available.return_value = True

        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        # Health check always fails
        mock_httpx_get.side_effect = httpx.ConnectError("Connection refused")

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
        ):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            result = runner.invoke(cli, ["start"])

            assert result.exit_code == 0
            assert "Warning: Daemon started but health check failed" in result.output

    @patch("gobby.cli.daemon.kill_all_gobby_daemons")
    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.daemon.time.sleep")
    @patch("gobby.cli.load_config")
    def test_start_kills_existing_processes(
        self,
        mock_load_config: MagicMock,
        mock_sleep: MagicMock,
        mock_init_storage: MagicMock,
        mock_kill_daemons: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """Test start kills existing gobby daemon processes."""
        mock_load_config.return_value = mock_daemon_config
        mock_kill_daemons.return_value = 2  # Two processes killed

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
        ):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            with (
                patch("gobby.cli.daemon.is_port_available", return_value=True),
                patch("gobby.cli.daemon.subprocess.Popen") as mock_popen,
                patch("gobby.cli.daemon.httpx.get") as mock_httpx_get,
                patch("gobby.cli.daemon.fetch_rich_status", return_value={}),
            ):
                mock_process = MagicMock()
                mock_process.pid = 12345
                mock_process.poll.return_value = None
                mock_popen.return_value = mock_process

                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_httpx_get.return_value = mock_response

                result = runner.invoke(cli, ["start"])

                assert "Stopped 2 existing process(es)" in result.output


class TestStopCommand:
    """Tests for the 'stop' command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    def test_stop_help(self, runner: CliRunner) -> None:
        """Test stop --help displays help text."""
        result = runner.invoke(cli, ["stop", "--help"])
        assert result.exit_code == 0
        assert "Stop the Gobby daemon" in result.output

    @patch("gobby.cli.daemon.stop_daemon_util")
    @patch("gobby.cli.load_config")
    def test_stop_success(
        self,
        mock_load_config: MagicMock,
        mock_stop_daemon: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test successful daemon stop."""
        mock_load_config.return_value = MagicMock()
        mock_stop_daemon.return_value = True

        result = runner.invoke(cli, ["stop"])

        assert result.exit_code == 0
        mock_stop_daemon.assert_called_once_with(quiet=False)

    @patch("gobby.cli.daemon.stop_daemon_util")
    @patch("gobby.cli.load_config")
    def test_stop_failure(
        self,
        mock_load_config: MagicMock,
        mock_stop_daemon: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test stop command fails when stop_daemon returns False."""
        mock_load_config.return_value = MagicMock()
        mock_stop_daemon.return_value = False

        result = runner.invoke(cli, ["stop"])

        assert result.exit_code == 1
        mock_stop_daemon.assert_called_once_with(quiet=False)


class TestRestartCommand:
    """Tests for the 'restart' command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    def test_restart_help(self, runner: CliRunner) -> None:
        """Test restart --help displays help text."""
        result = runner.invoke(cli, ["restart", "--help"])
        assert result.exit_code == 0
        assert "Restart the Gobby daemon" in result.output
        assert "--verbose" in result.output

    @patch("gobby.cli.daemon.fetch_rich_status")
    @patch("gobby.cli.daemon.httpx.get")
    @patch("gobby.cli.daemon.subprocess.Popen")
    @patch("gobby.cli.daemon.is_port_available")
    @patch("gobby.cli.daemon.kill_all_gobby_daemons")
    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.daemon.stop_daemon_util")
    @patch("gobby.cli.daemon.setup_logging")
    @patch("gobby.cli.daemon.time.sleep")
    @patch("gobby.cli.load_config")
    def test_restart_success(
        self,
        mock_load_config: MagicMock,
        mock_sleep: MagicMock,
        mock_setup_logging: MagicMock,
        mock_stop_daemon: MagicMock,
        mock_init_storage: MagicMock,
        mock_kill_daemons: MagicMock,
        mock_is_port_available: MagicMock,
        mock_popen: MagicMock,
        mock_httpx_get: MagicMock,
        mock_fetch_status: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """Test successful daemon restart."""
        mock_load_config.return_value = mock_daemon_config
        mock_stop_daemon.return_value = True
        mock_kill_daemons.return_value = 0
        mock_is_port_available.return_value = True
        mock_fetch_status.return_value = {}

        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_httpx_get.return_value = mock_response

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
        ):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            result = runner.invoke(cli, ["restart"])

            assert result.exit_code == 0
            assert "Restarting Gobby daemon" in result.output
            mock_stop_daemon.assert_called_once()
            mock_setup_logging.assert_called_once_with(False)

    @patch("gobby.cli.daemon.stop_daemon_util")
    @patch("gobby.cli.daemon.setup_logging")
    @patch("gobby.cli.load_config")
    def test_restart_stop_fails(
        self,
        mock_load_config: MagicMock,
        mock_setup_logging: MagicMock,
        mock_stop_daemon: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test restart aborts when stop fails."""
        mock_load_config.return_value = MagicMock()
        mock_stop_daemon.return_value = False

        result = runner.invoke(cli, ["restart"])

        assert result.exit_code == 1
        assert "Failed to stop daemon" in result.output

    @patch("gobby.cli.daemon.fetch_rich_status")
    @patch("gobby.cli.daemon.httpx.get")
    @patch("gobby.cli.daemon.subprocess.Popen")
    @patch("gobby.cli.daemon.is_port_available")
    @patch("gobby.cli.daemon.kill_all_gobby_daemons")
    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.daemon.stop_daemon_util")
    @patch("gobby.cli.daemon.setup_logging")
    @patch("gobby.cli.daemon.time.sleep")
    @patch("gobby.cli.load_config")
    def test_restart_with_verbose(
        self,
        mock_load_config: MagicMock,
        mock_sleep: MagicMock,
        mock_setup_logging: MagicMock,
        mock_stop_daemon: MagicMock,
        mock_init_storage: MagicMock,
        mock_kill_daemons: MagicMock,
        mock_is_port_available: MagicMock,
        mock_popen: MagicMock,
        mock_httpx_get: MagicMock,
        mock_fetch_status: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """Test restart with --verbose flag."""
        mock_load_config.return_value = mock_daemon_config
        mock_stop_daemon.return_value = True
        mock_kill_daemons.return_value = 0
        mock_is_port_available.return_value = True
        mock_fetch_status.return_value = {}

        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_httpx_get.return_value = mock_response

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
        ):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            result = runner.invoke(cli, ["restart", "--verbose"])

            assert result.exit_code == 0
            mock_setup_logging.assert_called_once_with(True)


class TestStatusCommand:
    """Tests for the 'status' command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    def test_status_help(self, runner: CliRunner) -> None:
        """Test status --help displays help text."""
        result = runner.invoke(cli, ["status", "--help"])
        assert result.exit_code == 0
        assert "Show Gobby daemon status" in result.output

    @patch("gobby.cli.daemon.get_gobby_home")
    @patch("gobby.cli.load_config")
    def test_status_no_pid_file(
        self,
        mock_load_config: MagicMock,
        mock_get_gobby_home: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """Test status when no PID file exists."""
        mock_load_config.return_value = mock_daemon_config

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            # Create gobby dir without PID file
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            mock_get_gobby_home.return_value = gobby_dir

            result = runner.invoke(cli, ["status"])

            assert result.exit_code == 0
            assert "Stopped" in result.output

    @patch("gobby.cli.daemon.get_gobby_home")
    @patch("gobby.cli.load_config")
    def test_status_invalid_pid_file(
        self,
        mock_load_config: MagicMock,
        mock_get_gobby_home: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """Test status with invalid PID file content."""
        mock_load_config.return_value = mock_daemon_config

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            mock_get_gobby_home.return_value = gobby_dir

            # Create invalid PID file
            pid_file = gobby_dir / "gobby.pid"
            pid_file.write_text("not-a-number")

            result = runner.invoke(cli, ["status"])

            assert result.exit_code == 0
            assert "Stopped" in result.output

    @patch("gobby.cli.daemon.get_gobby_home")
    @patch("gobby.cli.load_config")
    def test_status_stale_pid_file(
        self,
        mock_load_config: MagicMock,
        mock_get_gobby_home: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """Test status with stale PID file (process not running)."""
        mock_load_config.return_value = mock_daemon_config

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)
            mock_get_gobby_home.return_value = gobby_dir

            # Create PID file with non-existent process
            pid_file = gobby_dir / "gobby.pid"
            pid_file.write_text("99999999")

            result = runner.invoke(cli, ["status"])

            assert result.exit_code == 0
            assert "Stopped" in result.output
            assert "Stale PID file found" in result.output

    @patch("gobby.cli.daemon.get_gobby_home")
    @patch("gobby.cli.daemon.fetch_rich_status")
    @patch("gobby.cli.daemon.psutil.Process")
    @patch("gobby.cli.load_config")
    def test_status_daemon_running(
        self,
        mock_load_config: MagicMock,
        mock_psutil_process: MagicMock,
        mock_fetch_status: MagicMock,
        mock_get_gobby_home: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """Test status when daemon is running."""
        mock_load_config.return_value = mock_daemon_config
        mock_fetch_status.return_value = {
            "mcp_total": 5,
            "mcp_connected": 3,
            "sessions_active": 2,
        }

        # Mock psutil.Process
        mock_proc = MagicMock()
        mock_proc.create_time.return_value = time.time() - 3600  # 1 hour ago
        mock_psutil_process.return_value = mock_proc

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)
            mock_get_gobby_home.return_value = gobby_dir

            # Create PID file with current process PID
            pid_file = gobby_dir / "gobby.pid"
            pid_file.write_text(str(os.getpid()))

            result = runner.invoke(cli, ["status"])

            assert result.exit_code == 0
            assert "Running" in result.output
            mock_fetch_status.assert_called_once()

    @patch("gobby.cli.daemon.fetch_rich_status")
    @patch("gobby.cli.daemon.psutil.Process")
    @patch("gobby.cli.load_config")
    def test_status_psutil_error(
        self,
        mock_load_config: MagicMock,
        mock_psutil_process: MagicMock,
        mock_fetch_status: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """Test status handles psutil errors gracefully."""
        mock_load_config.return_value = mock_daemon_config
        mock_fetch_status.return_value = {}
        mock_psutil_process.side_effect = psutil.NoSuchProcess(pid=12345)

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
        ):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            # Create PID file with current process PID
            pid_file = gobby_dir / "gobby.pid"
            pid_file.write_text(str(os.getpid()))

            result = runner.invoke(cli, ["status"])

            # Should still work, just without uptime info
            assert result.exit_code == 0
            assert "Running" in result.output


class TestDaemonCommandsIntegration:
    """Integration tests for daemon commands."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    @pytest.fixture
    def clean_pid_file(self, temp_dir: Path):
        """Ensure temp PID file location is clean (does NOT touch real PID file)."""
        pid_file = temp_dir / ".gobby" / "gobby.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        if pid_file.exists():
            pid_file.unlink()
        yield pid_file
        if pid_file.exists():
            pid_file.unlink()

    @patch("gobby.cli.daemon.fetch_rich_status")
    @patch("gobby.cli.daemon.format_status_message")
    @patch("gobby.cli.daemon.httpx.get")
    @patch("gobby.cli.daemon.subprocess.Popen")
    @patch("gobby.cli.daemon.is_port_available")
    @patch("gobby.cli.daemon.kill_all_gobby_daemons")
    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.daemon.time.sleep")
    @patch("gobby.cli.load_config")
    def test_start_displays_status_message(
        self,
        mock_load_config: MagicMock,
        mock_sleep: MagicMock,
        mock_init_storage: MagicMock,
        mock_kill_daemons: MagicMock,
        mock_is_port_available: MagicMock,
        mock_popen: MagicMock,
        mock_httpx_get: MagicMock,
        mock_format_status: MagicMock,
        mock_fetch_status: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
        clean_pid_file,
    ) -> None:
        """Test that start command displays status message."""
        mock_load_config.return_value = mock_daemon_config
        mock_kill_daemons.return_value = 0
        mock_is_port_available.return_value = True
        mock_fetch_status.return_value = {}
        mock_format_status.return_value = "STATUS MESSAGE"

        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_httpx_get.return_value = mock_response

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
        ):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            result = runner.invoke(cli, ["start"])

            assert result.exit_code == 0
            assert "STATUS MESSAGE" in result.output
            mock_format_status.assert_called()

    def test_cli_has_all_daemon_commands(self, runner: CliRunner) -> None:
        """Test that CLI has all daemon management commands."""
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "start" in result.output
        assert "stop" in result.output
        assert "restart" in result.output
        assert "status" in result.output


class TestEdgeCases:
    """Test edge cases and error handling."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    @pytest.fixture
    def clean_pid_file(self, temp_dir: Path):
        """Ensure temp PID file location is clean (does NOT touch real PID file)."""
        pid_file = temp_dir / ".gobby" / "gobby.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        if pid_file.exists():
            pid_file.unlink()
        yield pid_file
        if pid_file.exists():
            pid_file.unlink()

    @pytest.mark.skip(
        reason="Flaky: FD leak from earlier tests causes OSError in isolated_filesystem"
    )
    @patch("gobby.cli.daemon.fetch_rich_status")
    @patch("gobby.cli.daemon.httpx.get")
    @patch("gobby.cli.daemon.subprocess.Popen")
    @patch("gobby.cli.daemon.is_port_available")
    @patch("gobby.cli.daemon.kill_all_gobby_daemons")
    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.daemon.time.sleep")
    @patch("gobby.cli.load_config")
    def test_start_health_check_timeout(
        self,
        mock_load_config: MagicMock,
        mock_sleep: MagicMock,
        mock_init_storage: MagicMock,
        mock_kill_daemons: MagicMock,
        mock_is_port_available: MagicMock,
        mock_popen: MagicMock,
        mock_httpx_get: MagicMock,
        mock_fetch_status: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
        clean_pid_file,
    ) -> None:
        """Test start handles health check timeout gracefully."""
        mock_load_config.return_value = mock_daemon_config
        mock_kill_daemons.return_value = 0
        mock_is_port_available.return_value = True
        mock_fetch_status.return_value = {}

        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        # Simulate timeout
        mock_httpx_get.side_effect = httpx.TimeoutException("Timeout")

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
        ):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            result = runner.invoke(cli, ["start"])

            # Should still succeed but with warning
            assert result.exit_code == 0
            assert "health check failed" in result.output

    @patch("gobby.cli.daemon.fetch_rich_status")
    @patch("gobby.cli.daemon.httpx.get")
    @patch("gobby.cli.daemon.subprocess.Popen")
    @patch("gobby.cli.daemon.is_port_available")
    @patch("gobby.cli.daemon.kill_all_gobby_daemons")
    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.daemon.time.sleep")
    @patch("gobby.cli.load_config")
    def test_start_health_check_non_200_response(
        self,
        mock_load_config: MagicMock,
        mock_sleep: MagicMock,
        mock_init_storage: MagicMock,
        mock_kill_daemons: MagicMock,
        mock_is_port_available: MagicMock,
        mock_popen: MagicMock,
        mock_httpx_get: MagicMock,
        mock_fetch_status: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
        clean_pid_file,
    ) -> None:
        """Test start retries when health check returns non-200."""
        mock_load_config.return_value = mock_daemon_config
        mock_kill_daemons.return_value = 0
        mock_is_port_available.return_value = True
        mock_fetch_status.return_value = {}

        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        # First few calls fail, then succeed
        responses = []
        for _ in range(5):
            bad_response = MagicMock()
            bad_response.status_code = 500
            responses.append(bad_response)
        good_response = MagicMock()
        good_response.status_code = 200
        responses.append(good_response)

        mock_httpx_get.side_effect = responses

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
        ):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            result = runner.invoke(cli, ["start"])

            assert result.exit_code == 0

    @patch("gobby.cli.daemon.subprocess.Popen")
    @patch("gobby.cli.daemon.is_port_available")
    @patch("gobby.cli.daemon.kill_all_gobby_daemons")
    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.load_config")
    def test_start_popen_exception(
        self,
        mock_load_config: MagicMock,
        mock_init_storage: MagicMock,
        mock_kill_daemons: MagicMock,
        mock_is_port_available: MagicMock,
        mock_popen: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
        clean_pid_file,
    ) -> None:
        """Test start handles Popen exception."""
        mock_load_config.return_value = mock_daemon_config
        mock_kill_daemons.return_value = 0
        mock_is_port_available.return_value = True
        mock_popen.side_effect = OSError("Cannot execute")

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
        ):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            result = runner.invoke(cli, ["start"])

            assert result.exit_code == 1
            assert "Error starting daemon" in result.output

    @patch("gobby.cli.daemon.format_status_message")
    @patch("gobby.cli.daemon.fetch_rich_status")
    @patch("gobby.cli.daemon.psutil.Process")
    @patch("gobby.cli.load_config")
    def test_status_with_rich_data(
        self,
        mock_load_config: MagicMock,
        mock_psutil_process: MagicMock,
        mock_fetch_status: MagicMock,
        mock_format_status: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """Test status command with rich daemon data."""
        mock_load_config.return_value = mock_daemon_config
        mock_format_status.return_value = "FULL STATUS"

        # Rich status data
        mock_fetch_status.return_value = {
            "memory_mb": 128.5,
            "cpu_percent": 2.5,
            "mcp_total": 10,
            "mcp_connected": 8,
            "mcp_tools_cached": 50,
            "sessions_active": 3,
            "tasks_open": 5,
            "tasks_in_progress": 2,
        }

        mock_proc = MagicMock()
        mock_proc.create_time.return_value = time.time() - 7200  # 2 hours ago
        mock_psutil_process.return_value = mock_proc

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
        ):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            pid_file = gobby_dir / "gobby.pid"
            pid_file.write_text(str(os.getpid()))

            result = runner.invoke(cli, ["status"])

            assert result.exit_code == 0
            mock_fetch_status.assert_called_once_with(mock_daemon_config.daemon_port, timeout=2.0)


class TestCommandBuilding:
    """Test the command building for subprocess."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    @pytest.fixture
    def clean_pid_file(self, temp_dir: Path):
        """Ensure temp PID file location is clean (does NOT touch real PID file)."""
        pid_file = temp_dir / ".gobby" / "gobby.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        if pid_file.exists():
            pid_file.unlink()
        yield pid_file
        if pid_file.exists():
            pid_file.unlink()

    @patch("gobby.cli.daemon.fetch_rich_status")
    @patch("gobby.cli.daemon.httpx.get")
    @patch("gobby.cli.daemon.subprocess.Popen")
    @patch("gobby.cli.daemon.is_port_available")
    @patch("gobby.cli.daemon.kill_all_gobby_daemons")
    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.daemon.time.sleep")
    @patch("gobby.cli.load_config")
    def test_start_command_uses_correct_module(
        self,
        mock_load_config: MagicMock,
        mock_sleep: MagicMock,
        mock_init_storage: MagicMock,
        mock_kill_daemons: MagicMock,
        mock_is_port_available: MagicMock,
        mock_popen: MagicMock,
        mock_httpx_get: MagicMock,
        mock_fetch_status: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
        clean_pid_file,
    ) -> None:
        """Test that start command builds correct subprocess command."""
        mock_load_config.return_value = mock_daemon_config
        mock_kill_daemons.return_value = 0
        mock_is_port_available.return_value = True
        mock_fetch_status.return_value = {}

        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_httpx_get.return_value = mock_response

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
        ):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            runner.invoke(cli, ["start"])

            call_args = mock_popen.call_args
            cmd = call_args[0][0]

            # Check command structure
            assert cmd[0] == sys.executable
            assert "-m" in cmd
            assert "gobby.runner" in cmd

    @patch("gobby.cli.daemon.fetch_rich_status")
    @patch("gobby.cli.daemon.httpx.get")
    @patch("gobby.cli.daemon.subprocess.Popen")
    @patch("gobby.cli.daemon.is_port_available")
    @patch("gobby.cli.daemon.kill_all_gobby_daemons")
    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.daemon.time.sleep")
    @patch("gobby.cli.load_config")
    def test_start_subprocess_options(
        self,
        mock_load_config: MagicMock,
        mock_sleep: MagicMock,
        mock_init_storage: MagicMock,
        mock_kill_daemons: MagicMock,
        mock_is_port_available: MagicMock,
        mock_popen: MagicMock,
        mock_httpx_get: MagicMock,
        mock_fetch_status: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
        clean_pid_file,
    ) -> None:
        """Test that start command uses correct subprocess options."""
        mock_load_config.return_value = mock_daemon_config
        mock_kill_daemons.return_value = 0
        mock_is_port_available.return_value = True
        mock_fetch_status.return_value = {}

        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_httpx_get.return_value = mock_response

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
        ):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            runner.invoke(cli, ["start"])

            call_kwargs = mock_popen.call_args[1]

            # Check subprocess options
            assert call_kwargs["stdin"] == subprocess.DEVNULL
            assert call_kwargs["start_new_session"] is True
            assert "env" in call_kwargs
