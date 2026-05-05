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

pytestmark = pytest.mark.unit


class TestFormatUptime:
    """Tests for format_uptime function."""

    def test_seconds_only(self) -> None:
        """Test formatting seconds only."""
        assert format_uptime(45) == "45s"
        assert format_uptime(1) == "1s"
        assert format_uptime(0) == "0s"

    def test_minutes_and_seconds(self) -> None:
        """Test formatting minutes and seconds."""
        assert format_uptime(90) == "1m 30s"
        assert format_uptime(125) == "2m 5s"
        assert format_uptime(60) == "1m"

    def test_hours_minutes_seconds(self) -> None:
        """Test formatting hours, minutes, and seconds."""
        assert format_uptime(3661) == "1h 1m 1s"
        assert format_uptime(7200) == "2h"
        assert format_uptime(3720) == "1h 2m"

    def test_hours_and_seconds_no_minutes(self) -> None:
        """Test hours with seconds but no minutes."""
        assert format_uptime(3605) == "1h 5s"


class TestIsPortAvailable:
    """Tests for is_port_available function."""

    def test_available_port(self) -> None:
        """Test checking an available port."""
        # Port 0 always finds an available port
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("localhost", 0))
        port = sock.getsockname()[1]
        sock.close()

        # After closing, the port should be available
        assert is_port_available(port) is True

    def test_unavailable_port(self) -> None:
        """Test that is_port_available returns False when port is used."""
        import socket

        # Create a socket and bind it to a random port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # MacOS/BSD needs SO_REUSEADDR to be FALSE for pure exclusivity in some cases,
        # but standard behavior is if we bind AND listen, it should be unavailable.
        # However, is_port_available uses SO_REUSEADDR.
        # If we want to ensure it Returns False, we must ensure is_port_available's bind fails.
        # If is_port_available uses SO_REUSEADDR, it CAN bind to a port that is TIME_WAIT,
        # but typically NOT one that is LISTEN.
        sock.bind(("localhost", 0))
        sock.listen(1)
        port = sock.getsockname()[1]

        try:
            assert is_port_available(port) is False
        finally:
            sock.close()


class TestWaitForPortAvailable:
    """Tests for wait_for_port_available function."""

    def test_port_immediately_available(self) -> None:
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

    def test_port_never_available_timeout(self) -> None:
        """Test that wait_for_port_available returns False on timeout."""
        import socket

        # Bind a port and keep it busy
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("localhost", 0))
        sock.listen(1)
        port = sock.getsockname()[1]

        try:
            # Should return False after timeout
            assert wait_for_port_available(port, timeout=0.1) is False
        finally:
            sock.close()


class TestCLIDetection:
    """Tests for CLI detection functions."""

    @patch("shutil.which")
    def test_is_claude_code_installed_found(self, mock_which: MagicMock) -> None:
        """Test Claude Code detection when installed."""
        mock_which.return_value = "/usr/local/bin/claude"
        assert _is_claude_code_installed() is True
        mock_which.assert_called_once_with("claude")

    @patch("shutil.which")
    def test_is_claude_code_installed_not_found(self, mock_which: MagicMock) -> None:
        """Test Claude Code detection when not installed."""
        mock_which.return_value = None
        assert _is_claude_code_installed() is False

    @patch("shutil.which")
    def test_is_gemini_cli_installed_found(self, mock_which: MagicMock) -> None:
        """Test Gemini CLI detection when installed."""
        mock_which.return_value = "/usr/local/bin/gemini"
        assert _is_gemini_cli_installed() is True
        mock_which.assert_called_once_with("gemini")

    @patch("shutil.which")
    def test_is_gemini_cli_installed_not_found(self, mock_which: MagicMock) -> None:
        """Test Gemini CLI detection when not installed."""
        mock_which.return_value = None
        assert _is_gemini_cli_installed() is False

    @patch("shutil.which")
    def test_is_codex_cli_installed_found(self, mock_which: MagicMock) -> None:
        """Test Codex CLI detection when installed."""
        mock_which.return_value = "/usr/local/bin/codex"
        assert _is_codex_cli_installed() is True
        mock_which.assert_called_once_with("codex")

    @patch("shutil.which")
    def test_is_codex_cli_installed_not_found(self, mock_which: MagicMock) -> None:
        """Test Codex CLI detection when not installed."""
        mock_which.return_value = None
        assert _is_codex_cli_installed() is False


class TestCLICommands:
    """Tests for CLI commands using Click's test runner."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    def test_cli_help(self, runner: CliRunner) -> None:
        """Test --help displays help text."""
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "Gobby" in result.output

    def test_start_help(self, runner: CliRunner) -> None:
        """Test start --help displays help."""
        result = runner.invoke(cli, ["start", "--help"])
        assert result.exit_code == 0
        assert "Start the Gobby daemon" in result.output

    def test_stop_help(self, runner: CliRunner) -> None:
        """Test stop --help displays help."""
        result = runner.invoke(cli, ["stop", "--help"])
        assert result.exit_code == 0
        assert "Stop the Gobby daemon" in result.output

    def test_status_help(self, runner: CliRunner) -> None:
        """Test status --help displays help."""
        result = runner.invoke(cli, ["status", "--help"])
        assert result.exit_code == 0
        assert "operational health dashboard" in result.output

    def test_restart_help(self, runner: CliRunner) -> None:
        """Test restart --help displays help."""
        result = runner.invoke(cli, ["restart", "--help"])
        assert result.exit_code == 0
        assert "Restart the Gobby daemon" in result.output

    def test_init_help(self, runner: CliRunner) -> None:
        """Test init --help displays help."""
        result = runner.invoke(cli, ["init", "--help"])
        assert result.exit_code == 0
        assert "Initialize a new Gobby project" in result.output

    def test_install_help(self, runner: CliRunner) -> None:
        """Test install --help displays help."""
        result = runner.invoke(cli, ["install", "--help"])
        assert result.exit_code == 0
        assert "Install Gobby hooks" in result.output

    def test_uninstall_help(self, runner: CliRunner) -> None:
        """Test uninstall --help displays help."""
        result = runner.invoke(cli, ["uninstall", "--help"])
        assert result.exit_code == 0
        assert "Uninstall Gobby hooks" in result.output

    def test_mcp_server_help(self, runner: CliRunner) -> None:
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

    @patch("gobby.cli.daemon.get_service_status", return_value={"installed": False})
    @patch("gobby.cli.load_config")
    @patch("gobby.cli.daemon.get_gobby_home")
    def test_status_no_pid_file(
        self,
        mock_get_home: MagicMock,
        mock_load_config: MagicMock,
        mock_get_service_status: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test status when no PID file exists."""
        mock_config = MagicMock()
        mock_config.logging.client = str(temp_dir / "logs" / "client.log")
        mock_load_config.return_value = mock_config

        # Mock get_gobby_home to return temp_dir (which has no gobby.pid)
        mock_get_home.return_value = temp_dir

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
    ) -> None:
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
    ) -> None:
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
    @patch("gobby.cli.install._is_qwen_cli_installed")
    @patch("gobby.cli.install._is_codex_cli_installed")
    @patch("gobby.cli.install._is_droid_cli_installed")
    @patch("gobby.cli.load_config")
    def test_install_no_clis_detected(
        self,
        mock_load_config: MagicMock,
        mock_droid: MagicMock,
        mock_codex: MagicMock,
        mock_qwen: MagicMock,
        mock_gemini: MagicMock,
        mock_claude: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test install when no CLIs are detected."""
        mock_load_config.return_value = MagicMock()
        mock_claude.return_value = False
        mock_gemini.return_value = False
        mock_qwen.return_value = False
        mock_codex.return_value = False
        mock_droid.return_value = False

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

    @patch("gobby.cli.install.Path.home")
    @patch("gobby.cli.load_config")
    def test_uninstall_no_hooks_found(
        self,
        mock_load_config: MagicMock,
        mock_path_home: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test uninstall when no hooks are found."""
        mock_load_config.return_value = MagicMock()
        # Mock home directory to prevent finding actual hooks on dev machine
        mock_path_home.return_value = temp_dir

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            # Confirm the uninstall
            result = runner.invoke(cli, ["uninstall", "--yes"])

            assert result.exit_code == 0
            assert "No Gobby hooks found" in result.output
