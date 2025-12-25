"""Tests for the CLI module."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli import cli
from gobby.cli.install import (
    _is_claude_code_installed,
    _is_codex_cli_installed,
    _is_gemini_cli_installed,
)
from gobby.cli.utils import (
    format_uptime,
    is_port_available,
    wait_for_port_available,
)


class TestFormatUptime:
    """Tests for format_uptime function."""

    def test_seconds_only(self):
        """Test formatting seconds only."""
        assert format_uptime(45) == "45s"
        assert format_uptime(1) == "1s"
        assert format_uptime(0) == "0s"

    def test_minutes_and_seconds(self):
        """Test formatting minutes and seconds."""
        assert format_uptime(90) == "1m 30s"
        assert format_uptime(125) == "2m 5s"
        assert format_uptime(60) == "1m"

    def test_hours_minutes_seconds(self):
        """Test formatting hours, minutes, and seconds."""
        assert format_uptime(3661) == "1h 1m 1s"
        assert format_uptime(7200) == "2h"
        assert format_uptime(3720) == "1h 2m"

    def test_hours_and_seconds_no_minutes(self):
        """Test hours with seconds but no minutes."""
        assert format_uptime(3605) == "1h 5s"


class TestIsPortAvailable:
    """Tests for is_port_available function."""

    def test_available_port(self):
        """Test checking an available port."""
        # Port 0 always finds an available port
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("localhost", 0))
        port = sock.getsockname()[1]
        sock.close()

        # After closing, the port should be available
        assert is_port_available(port) is True

    def test_unavailable_port(self):
        """Test checking an unavailable port."""
        import socket

        # Bind to a port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("localhost", 0))
        port = sock.getsockname()[1]

        try:
            # Port is now in use
            assert is_port_available(port) is False
        finally:
            sock.close()


class TestWaitForPortAvailable:
    """Tests for wait_for_port_available function."""

    def test_port_immediately_available(self):
        """Test waiting for a port that's already available."""
        # Use port 0 to get an available port
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("localhost", 0))
        port = sock.getsockname()[1]
        sock.close()

        # Should return True immediately
        result = wait_for_port_available(port, timeout=0.5)
        assert result is True

    def test_port_never_available_timeout(self):
        """Test timeout when port never becomes available."""
        import socket

        # Bind to a port and keep it bound
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("localhost", 0))
        port = sock.getsockname()[1]

        try:
            # Should timeout and return False
            result = wait_for_port_available(port, timeout=0.3)
            assert result is False
        finally:
            sock.close()


class TestCLIDetection:
    """Tests for CLI detection functions."""

    @patch("shutil.which")
    def test_is_claude_code_installed_found(self, mock_which: MagicMock):
        """Test Claude Code detection when installed."""
        mock_which.return_value = "/usr/local/bin/claude"
        assert _is_claude_code_installed() is True
        mock_which.assert_called_once_with("claude")

    @patch("shutil.which")
    def test_is_claude_code_installed_not_found(self, mock_which: MagicMock):
        """Test Claude Code detection when not installed."""
        mock_which.return_value = None
        assert _is_claude_code_installed() is False

    @patch("shutil.which")
    def test_is_gemini_cli_installed_found(self, mock_which: MagicMock):
        """Test Gemini CLI detection when installed."""
        mock_which.return_value = "/usr/local/bin/gemini"
        assert _is_gemini_cli_installed() is True
        mock_which.assert_called_once_with("gemini")

    @patch("shutil.which")
    def test_is_gemini_cli_installed_not_found(self, mock_which: MagicMock):
        """Test Gemini CLI detection when not installed."""
        mock_which.return_value = None
        assert _is_gemini_cli_installed() is False

    @patch("shutil.which")
    def test_is_codex_cli_installed_found(self, mock_which: MagicMock):
        """Test Codex CLI detection when installed."""
        mock_which.return_value = "/usr/local/bin/codex"
        assert _is_codex_cli_installed() is True
        mock_which.assert_called_once_with("codex")

    @patch("shutil.which")
    def test_is_codex_cli_installed_not_found(self, mock_which: MagicMock):
        """Test Codex CLI detection when not installed."""
        mock_which.return_value = None
        assert _is_codex_cli_installed() is False


class TestCLICommands:
    """Tests for CLI commands using Click's test runner."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    def test_cli_help(self, runner: CliRunner):
        """Test --help displays help text."""
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "Gobby" in result.output

    def test_start_help(self, runner: CliRunner):
        """Test start --help displays help."""
        result = runner.invoke(cli, ["start", "--help"])
        assert result.exit_code == 0
        assert "Start the Gobby daemon" in result.output

    def test_stop_help(self, runner: CliRunner):
        """Test stop --help displays help."""
        result = runner.invoke(cli, ["stop", "--help"])
        assert result.exit_code == 0
        assert "Stop the Gobby daemon" in result.output

    def test_status_help(self, runner: CliRunner):
        """Test status --help displays help."""
        result = runner.invoke(cli, ["status", "--help"])
        assert result.exit_code == 0
        assert "Show Gobby daemon status" in result.output

    def test_restart_help(self, runner: CliRunner):
        """Test restart --help displays help."""
        result = runner.invoke(cli, ["restart", "--help"])
        assert result.exit_code == 0
        assert "Restart the Gobby daemon" in result.output

    def test_init_help(self, runner: CliRunner):
        """Test init --help displays help."""
        result = runner.invoke(cli, ["init", "--help"])
        assert result.exit_code == 0
        assert "Initialize a new Gobby project" in result.output

    def test_install_help(self, runner: CliRunner):
        """Test install --help displays help."""
        result = runner.invoke(cli, ["install", "--help"])
        assert result.exit_code == 0
        assert "Install Gobby hooks" in result.output

    def test_uninstall_help(self, runner: CliRunner):
        """Test uninstall --help displays help."""
        result = runner.invoke(cli, ["uninstall", "--help"])
        assert result.exit_code == 0
        assert "Uninstall Gobby hooks" in result.output

    def test_mcp_server_help(self, runner: CliRunner):
        """Test mcp-server --help displays help."""
        result = runner.invoke(cli, ["mcp-server", "--help"])
        assert result.exit_code == 0
        assert "stdio MCP server" in result.output


class TestStatusCommand:
    """Tests for the status command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    @patch("gobby.cli.load_config")
    def test_status_no_pid_file(
        self, mock_load_config: MagicMock, runner: CliRunner, temp_dir: Path
    ):
        """Test status when no PID file exists."""
        mock_config = MagicMock()
        mock_config.logging.client = str(temp_dir / "logs" / "client.log")
        mock_load_config.return_value = mock_config

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            # Ensure no PID file exists
            pid_file = Path.home() / ".gobby" / "gobby.pid"
            if pid_file.exists():
                pid_file.unlink()

            result = runner.invoke(cli, ["status"])

            # Should indicate daemon is not running
            assert result.exit_code == 0
            assert "not running" in result.output.lower() or "stopped" in result.output.lower()


class TestInitCommand:
    """Tests for the init command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    @patch("gobby.cli.init.initialize_project")
    @patch("gobby.cli.load_config")
    def test_init_new_project(
        self,
        mock_load_config: MagicMock,
        mock_init: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ):
        """Test initializing a new project."""
        mock_load_config.return_value = MagicMock()

        # Mock successful initialization
        mock_result = MagicMock()
        mock_result.already_existed = False
        mock_result.project_name = "test-project"
        mock_result.project_id = "test-uuid-123"
        mock_init.return_value = mock_result

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["init"])

            assert result.exit_code == 0
            assert "test-project" in result.output
            assert "test-uuid-123" in result.output

    @patch("gobby.cli.init.initialize_project")
    @patch("gobby.cli.load_config")
    def test_init_existing_project(
        self,
        mock_load_config: MagicMock,
        mock_init: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ):
        """Test initializing when project already exists."""
        mock_load_config.return_value = MagicMock()

        mock_result = MagicMock()
        mock_result.already_existed = True
        mock_result.project_name = "existing-project"
        mock_result.project_id = "existing-uuid"
        mock_init.return_value = mock_result

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["init"])

            assert result.exit_code == 0
            assert "already initialized" in result.output.lower()


class TestInstallCommand:
    """Tests for the install command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    @patch("gobby.cli.install._is_claude_code_installed")
    @patch("gobby.cli.install._is_gemini_cli_installed")
    @patch("gobby.cli.install._is_codex_cli_installed")
    @patch("gobby.cli.load_config")
    def test_install_no_clis_detected(
        self,
        mock_load_config: MagicMock,
        mock_codex: MagicMock,
        mock_gemini: MagicMock,
        mock_claude: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ):
        """Test install when no CLIs are detected."""
        mock_load_config.return_value = MagicMock()
        mock_claude.return_value = False
        mock_gemini.return_value = False
        mock_codex.return_value = False

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["install"])

            assert result.exit_code == 1
            assert "No supported AI coding CLIs detected" in result.output


class TestUninstallCommand:
    """Tests for the uninstall command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    @patch("gobby.cli.load_config")
    def test_uninstall_no_hooks_found(
        self,
        mock_load_config: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ):
        """Test uninstall when no hooks are found."""
        mock_load_config.return_value = MagicMock()

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            # Confirm the uninstall
            result = runner.invoke(cli, ["uninstall", "--yes"])

            assert result.exit_code == 0
            assert "No Gobby hooks found" in result.output
