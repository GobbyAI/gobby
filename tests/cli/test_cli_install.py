"""Comprehensive tests for the CLI install module.

Tests for install.py using Click's CliRunner to test all commands and options.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from gobby.cli import cli
from gobby.cli.install import (
    _ensure_daemon_config,
    _is_claude_code_installed,
    _is_codex_cli_installed,
    _is_droid_cli_installed,
    _is_gemini_cli_installed,
    _is_qwen_cli_installed,
    install,
    uninstall,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _mock_ext_services_and_prompts():
    """Prevent real Docker service installers and interactive API-key prompts."""
    with (
        patch("gobby.cli.install._run_qdrant_install"),
        patch("gobby.cli.install._run_falkordb_install"),
        patch("gobby.storage.hub.runtime.open_runtime_hub_database", return_value=MagicMock()),
        patch("gobby.cli.install.SecretStore"),
        patch(
            "gobby.cli._install_prompts._prompt_api_keys",
            return_value={"stored": 0, "already_configured": 0, "env_found": 0},
        ),
    ):
        yield


@pytest.fixture(autouse=True)
def _mock_qwen_detector() -> None:
    """Keep Qwen detection deterministic unless a test overrides it."""
    with patch("gobby.cli.install._is_qwen_cli_installed", return_value=False):
        yield


@pytest.fixture(autouse=True)
def _mock_droid_detector() -> None:
    """Keep Droid detection deterministic unless a test overrides it."""
    with patch("gobby.cli.install._is_droid_cli_installed", return_value=False):
        yield


class TestEnsureDaemonConfig:
    """Tests for _ensure_daemon_config function."""

    def test_bootstrap_already_exists(self, temp_dir: Path) -> None:
        """Test when bootstrap file already exists."""
        bootstrap_path = temp_dir / ".gobby" / "bootstrap.yaml"
        bootstrap_path.parent.mkdir(parents=True, exist_ok=True)
        bootstrap_path.write_text("daemon_port: 60887\n")
        bootstrap_path.chmod(0o600)

        with patch.object(Path, "expanduser", return_value=bootstrap_path):
            result = _ensure_daemon_config()

        assert result["created"] is False
        assert result["path"] == str(bootstrap_path)
        assert "source" not in result

    def test_bootstrap_created_from_shared_template(self, temp_dir: Path) -> None:
        """Test creating bootstrap from shared template."""
        bootstrap_path = temp_dir / ".gobby" / "bootstrap.yaml"
        shared_bootstrap = temp_dir / "install" / "shared" / "config" / "bootstrap.yaml"
        shared_bootstrap.parent.mkdir(parents=True, exist_ok=True)
        shared_bootstrap.write_text("daemon_port: 60887\nbind_host: localhost\n")
        shared_bootstrap.chmod(0o600)

        with (
            patch.object(Path, "expanduser", return_value=bootstrap_path),
            patch(
                "gobby.cli.install_setup.get_install_dir",
                return_value=temp_dir / "install",
            ),
        ):
            result = _ensure_daemon_config()

        assert result["created"] is True
        assert result["path"] == str(bootstrap_path)
        assert result["source"] == "shared"
        assert bootstrap_path.exists()
        assert "daemon_port: 60887" in bootstrap_path.read_text()
        # Check permissions
        assert (bootstrap_path.stat().st_mode & 0o777) == 0o600

    def test_bootstrap_generated_as_fallback(self, temp_dir: Path) -> None:
        """Test generating bootstrap from defaults when no template exists."""
        bootstrap_path = temp_dir / ".gobby" / "bootstrap.yaml"
        install_dir = temp_dir / "install"
        install_dir.mkdir(parents=True, exist_ok=True)
        # No shared bootstrap template

        # Set up the parent directory so mkdir works
        bootstrap_path.parent.mkdir(parents=True, exist_ok=True)

        with (
            patch.object(Path, "expanduser", return_value=bootstrap_path),
            patch(
                "gobby.cli.install_setup.get_install_dir",
                return_value=install_dir,
            ),
        ):
            result = _ensure_daemon_config()

        assert result["created"] is True
        assert result["source"] == "generated"
        assert bootstrap_path.exists()
        content = yaml.safe_load(bootstrap_path.read_text())
        assert content["daemon_port"] == 60887
        assert content["bind_host"] == "localhost"


class TestCLIDetectionFunctions:
    """Tests for CLI detection helper functions."""

    @patch("shutil.which")
    def test_is_claude_code_installed_true(self, mock_which: MagicMock) -> None:
        """Test Claude Code detection when installed."""
        mock_which.return_value = "/usr/local/bin/claude"
        assert _is_claude_code_installed() is True
        mock_which.assert_called_once_with("claude")

    @patch("shutil.which")
    def test_is_claude_code_installed_false(self, mock_which: MagicMock) -> None:
        """Test Claude Code detection when not installed."""
        mock_which.return_value = None
        assert _is_claude_code_installed() is False

    @patch("shutil.which")
    def test_is_gemini_cli_installed_true(self, mock_which: MagicMock) -> None:
        """Test Gemini CLI detection when installed."""
        mock_which.return_value = "/usr/local/bin/gemini"
        assert _is_gemini_cli_installed() is True
        mock_which.assert_called_once_with("gemini")

    @patch("shutil.which")
    def test_is_gemini_cli_installed_false(self, mock_which: MagicMock) -> None:
        """Test Gemini CLI detection when not installed."""
        mock_which.return_value = None
        assert _is_gemini_cli_installed() is False

    @patch("shutil.which")
    def test_is_qwen_cli_installed_true(self, mock_which: MagicMock) -> None:
        """Test Qwen CLI detection when installed."""
        mock_which.return_value = "/usr/local/bin/qwen"
        assert _is_qwen_cli_installed() is True
        mock_which.assert_called_once_with("qwen")

    @patch("shutil.which")
    def test_is_qwen_cli_installed_false(self, mock_which: MagicMock) -> None:
        """Test Qwen CLI detection when not installed."""
        mock_which.return_value = None
        assert _is_qwen_cli_installed() is False

    @patch("shutil.which")
    def test_is_codex_cli_installed_true(self, mock_which: MagicMock) -> None:
        """Test Codex CLI detection when installed."""
        mock_which.return_value = "/usr/local/bin/codex"
        assert _is_codex_cli_installed() is True
        mock_which.assert_called_once_with("codex")

    @patch("shutil.which")
    def test_is_codex_cli_installed_false(self, mock_which: MagicMock) -> None:
        """Test Codex CLI detection when not installed."""
        mock_which.return_value = None
        assert _is_codex_cli_installed() is False

    @patch("shutil.which")
    def test_is_droid_cli_installed_true(self, mock_which: MagicMock) -> None:
        """Test Droid CLI detection when installed."""
        mock_which.return_value = "/usr/local/bin/droid"
        assert _is_droid_cli_installed() is True
        mock_which.assert_called_once_with("droid")

    @patch("shutil.which")
    def test_is_droid_cli_installed_false(self, mock_which: MagicMock) -> None:
        """Test Droid CLI detection when not installed."""
        mock_which.return_value = None
        assert _is_droid_cli_installed() is False


class TestInstallCommand:
    """Tests for the install CLI command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    def test_install_help(self, runner: CliRunner) -> None:
        """Test install --help displays help text."""
        result = runner.invoke(cli, ["install", "--help"])
        assert result.exit_code == 0
        assert "Install Gobby hooks" in result.output
        assert "--claude" in result.output
        assert "--gemini" in result.output
        assert "--qwen" in result.output
        assert "--codex" in result.output
        assert "--droid" in result.output
        assert "--hooks" in result.output
        assert "--all" in result.output
        assert "--embedding-provider" in result.output
        assert "LM Studio-compatible defaults" in result.output
        assert "openai-compatible uses generic OpenAI-" in result.output
        assert "compatible embedding APIs" in result.output

    @pytest.mark.parametrize("embedding_dim", ["0", "-1"])
    def test_install_rejects_non_positive_embedding_dim(
        self, runner: CliRunner, embedding_dim: str
    ) -> None:
        """--embedding-dim must be rejected by Click before install orchestration runs."""
        result = runner.invoke(cli, ["install", "--embedding-dim", embedding_dim])

        assert result.exit_code == 2
        assert "Invalid value for '--embedding-dim'" in result.output

    @patch("gobby.cli.install._is_claude_code_installed")
    @patch("gobby.cli.install._is_gemini_cli_installed")
    @patch("gobby.cli.install._is_codex_cli_installed")
    @patch("gobby.cli.install.load_full_config_from_db")
    def test_install_no_clis_detected_no_git(
        self,
        mock_load_config: MagicMock,
        mock_codex: MagicMock,
        mock_gemini: MagicMock,
        mock_claude: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test install when no CLIs are detected and no git repo."""
        mock_load_config.return_value = MagicMock()
        mock_claude.return_value = False
        mock_gemini.return_value = False
        mock_codex.return_value = False

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["install"])

        assert result.exit_code == 1
        assert "No supported AI coding CLIs detected" in result.output
        assert "Claude Code" in result.output
        assert "Gemini CLI" in result.output
        assert "Qwen CLI" in result.output
        assert "Codex CLI" in result.output
        assert "Droid CLI" in result.output

    @patch("gobby.cli.install._ensure_daemon_config")
    @patch("gobby.cli.install.install_claude")
    @patch("gobby.cli.install._is_claude_code_installed")
    @patch("gobby.cli.install._is_gemini_cli_installed")
    @patch("gobby.cli.install._is_codex_cli_installed")
    @patch("gobby.cli.load_full_config_from_db")
    def test_install_claude_only_flag(
        self,
        mock_load_config: MagicMock,
        mock_codex: MagicMock,
        mock_gemini: MagicMock,
        mock_claude: MagicMock,
        mock_install_claude: MagicMock,
        mock_ensure_config: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test install with --claude flag only."""
        mock_load_config.return_value = MagicMock()
        mock_codex.return_value = False
        mock_ensure_config.return_value = {"created": False, "path": "/test/config.yaml"}
        mock_install_claude.return_value = {
            "success": True,
            "hooks_installed": ["SessionStart", "SessionEnd"],
            "workflows_installed": [],
            "commands_installed": [],
            "plugins_installed": [],
            "mcp_configured": True,
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["install", "--claude"])

        assert result.exit_code == 0
        assert "Claude Code" in result.output
        assert "Installed 2 hooks" in result.output
        assert "Installation completed successfully" in result.output
        mock_install_claude.assert_called_once()

    @patch("gobby.cli.install._ensure_daemon_config")
    @patch("gobby.cli.install.install_gemini")
    @patch("gobby.cli.install._is_claude_code_installed")
    @patch("gobby.cli.install._is_gemini_cli_installed")
    @patch("gobby.cli.install._is_codex_cli_installed")
    @patch("gobby.cli.load_full_config_from_db")
    def test_install_gemini_only_flag(
        self,
        mock_load_config: MagicMock,
        mock_codex: MagicMock,
        mock_gemini: MagicMock,
        mock_claude: MagicMock,
        mock_install_gemini: MagicMock,
        mock_ensure_config: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test install with --gemini flag only."""
        mock_load_config.return_value = MagicMock()
        mock_codex.return_value = False
        mock_ensure_config.return_value = {"created": False, "path": "/test/config.yaml"}
        mock_install_gemini.return_value = {
            "success": True,
            "hooks_installed": ["SessionStart"],
            "workflows_installed": ["workflow1"],
            "commands_installed": ["cmd1"],
            "plugins_installed": ["plugin1"],
            "mcp_configured": False,
            "mcp_already_configured": True,
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["install", "--gemini"])

        assert result.exit_code == 0
        assert "Gemini CLI" in result.output
        assert "Installed 1 hooks" in result.output
        assert "Installed 1 workflows" in result.output
        assert "Installed 1 skills/commands" in result.output
        assert "Installed 1 plugins" in result.output
        assert "MCP server already configured" in result.output
        mock_install_gemini.assert_called_once()

    @patch("gobby.cli.install._ensure_daemon_config")
    @patch("gobby.cli.install.install_qwen")
    @patch("gobby.cli.load_full_config_from_db")
    def test_install_qwen_only_flag(
        self,
        mock_load_config: MagicMock,
        mock_install_qwen: MagicMock,
        mock_ensure_config: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test install with --qwen flag only."""
        mock_load_config.return_value = MagicMock()
        mock_ensure_config.return_value = {"created": False, "path": "/test/config.yaml"}
        mock_install_qwen.return_value = {
            "success": True,
            "hooks_installed": ["SessionStart"],
            "workflows_installed": [],
            "commands_installed": ["qwen-cmd"],
            "plugins_installed": ["plugin1"],
            "mcp_configured": True,
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["install", "--qwen"])

        assert result.exit_code == 0
        assert "Qwen CLI" in result.output
        assert "Installed 1 hooks" in result.output
        assert "Installed 1 skills/commands" in result.output
        assert "Installed 1 plugins" in result.output
        mock_install_qwen.assert_called_once()

    @patch("gobby.cli.install._ensure_daemon_config")
    @patch("gobby.cli.install.install_droid")
    @patch("gobby.cli.load_full_config_from_db")
    def test_install_droid_only_flag(
        self,
        mock_load_config: MagicMock,
        mock_install_droid: MagicMock,
        mock_ensure_config: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test install with --droid flag only."""
        mock_load_config.return_value = MagicMock()
        mock_ensure_config.return_value = {"created": False, "path": "/test/config.yaml"}
        mock_install_droid.return_value = {
            "success": True,
            "hooks_installed": ["SessionStart"],
            "workflows_installed": [],
            "commands_installed": [],
            "plugins_installed": [],
            "mcp_configured": True,
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["install", "--droid"])

        assert result.exit_code == 0
        assert "Droid CLI" in result.output
        assert "Installed 1 hooks" in result.output
        mock_install_droid.assert_called_once()

    @patch("gobby.cli.install._ensure_daemon_config")
    @patch("gobby.cli.install.install_codex")
    @patch("gobby.cli.install._is_claude_code_installed")
    @patch("gobby.cli.install._is_gemini_cli_installed")
    @patch("gobby.cli.install._is_codex_cli_installed")
    @patch("gobby.cli.load_full_config_from_db")
    def test_install_codex_only_flag_codex_detected(
        self,
        mock_load_config: MagicMock,
        mock_codex_detected: MagicMock,
        mock_gemini: MagicMock,
        mock_claude: MagicMock,
        mock_install_codex: MagicMock,
        mock_ensure_config: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test install with --codex flag when Codex is detected."""
        mock_load_config.return_value = MagicMock()
        mock_codex_detected.return_value = True
        mock_ensure_config.return_value = {"created": False, "path": "/test/config.yaml"}
        mock_install_codex.return_value = {
            "success": True,
            "hooks_installed": [],
            "files_installed": ["/home/user/.gobby/hooks/codex/hook_dispatcher.py"],
            "workflows_installed": [],
            "commands_installed": [],
            "plugins_installed": [],
            "config_updated": True,
            "mcp_configured": True,
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["install", "--codex"])

        assert result.exit_code == 0
        assert "Codex" in result.output
        assert "Installed" in result.output
        mock_install_codex.assert_called_once()

    @patch("gobby.cli.install._ensure_daemon_config")
    @patch("gobby.cli.install.install_codex")
    @patch("gobby.cli.install._is_claude_code_installed")
    @patch("gobby.cli.install._is_gemini_cli_installed")
    @patch("gobby.cli.install._is_codex_cli_installed")
    @patch("gobby.cli.load_full_config_from_db")
    def test_install_codex_only_flag_codex_not_detected(
        self,
        mock_load_config: MagicMock,
        mock_codex_detected: MagicMock,
        mock_gemini: MagicMock,
        mock_claude: MagicMock,
        mock_install_codex: MagicMock,
        mock_ensure_config: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test install with --codex flag proceeds even when Codex is not in PATH."""
        mock_load_config.return_value = MagicMock()
        mock_codex_detected.return_value = False
        mock_ensure_config.return_value = {"created": False, "path": "/test/config.yaml"}
        mock_install_codex.return_value = {
            "success": True,
            "hooks_installed": [],
            "files_installed": [],
            "workflows_installed": [],
            "commands_installed": [],
            "plugins_installed": [],
            "config_updated": True,
            "mcp_configured": True,
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["install", "--codex"])

        assert result.exit_code == 0
        assert "Codex" in result.output
        mock_install_codex.assert_called_once()

    def test_codex_install_skips_embedding(
        self,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Targeted Codex install does not run embedding or Docker setup."""
        codex_result = {
            "success": True,
            "hooks_installed": [],
            "files_installed": ["/home/user/.gobby/hooks/codex/hook_dispatcher.py"],
            "workflows_installed": [],
            "commands_installed": [],
            "plugins_installed": [],
            "config_updated": True,
            "mcp_configured": True,
        }
        with (
            patch("gobby.cli.install.run_daemon_setup"),
            patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install")),
            patch(
                "gobby.cli.install._ensure_daemon_config",
                return_value={"created": False, "path": "/test/config.yaml"},
            ),
            patch("gobby.cli.install.load_full_config_from_db", side_effect=FileNotFoundError),
            patch("gobby.cli.install.install_codex", return_value=codex_result) as mock_codex,
            patch("gobby.cli.install._run_embedding_install") as mock_embedding,
            patch("gobby.cli.install._run_qdrant_install") as mock_qdrant,
            patch("gobby.cli.install._run_falkordb_install") as mock_falkordb,
        ):
            with runner.isolated_filesystem(temp_dir=str(temp_dir)):
                result = runner.invoke(cli, ["install", "--codex", "--no-interactive"])

        assert result.exit_code == 0
        assert "Codex" in result.output
        assert "Embedding Provider" not in result.output
        mock_codex.assert_called_once()
        mock_embedding.assert_not_called()
        mock_qdrant.assert_not_called()
        mock_falkordb.assert_not_called()

    @patch("gobby.cli.install._ensure_daemon_config")
    @patch("gobby.cli.install.install_git_hooks")
    @patch("gobby.cli.install._is_claude_code_installed")
    @patch("gobby.cli.install._is_gemini_cli_installed")
    @patch("gobby.cli.install._is_codex_cli_installed")
    @patch("gobby.cli.load_full_config_from_db")
    def test_install_hooks_only_flag(
        self,
        mock_load_config: MagicMock,
        mock_codex: MagicMock,
        mock_gemini: MagicMock,
        mock_claude: MagicMock,
        mock_install_git_hooks: MagicMock,
        mock_ensure_config: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test install with --hooks flag only."""
        mock_load_config.return_value = MagicMock()
        mock_codex.return_value = False
        mock_ensure_config.return_value = {"created": False, "path": "/test/config.yaml"}
        mock_install_git_hooks.return_value = {
            "success": True,
            "installed": ["pre-commit", "post-merge", "post-checkout"],
            "skipped": [],
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["install", "--hooks"])

        assert result.exit_code == 0
        assert "Git Hooks" in result.output
        assert "pre-commit" in result.output
        assert "post-merge" in result.output
        assert "post-checkout" in result.output
        mock_install_git_hooks.assert_called_once()

    @patch("gobby.cli.install._ensure_daemon_config")
    @patch("gobby.cli.install.install_git_hooks")
    @patch("gobby.cli.install._is_claude_code_installed")
    @patch("gobby.cli.install._is_gemini_cli_installed")
    @patch("gobby.cli.install._is_codex_cli_installed")
    @patch("gobby.cli.load_full_config_from_db")
    def test_install_hooks_with_skipped(
        self,
        mock_load_config: MagicMock,
        mock_codex: MagicMock,
        mock_gemini: MagicMock,
        mock_claude: MagicMock,
        mock_install_git_hooks: MagicMock,
        mock_ensure_config: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test install --hooks with some skipped hooks."""
        mock_load_config.return_value = MagicMock()
        mock_codex.return_value = False
        mock_ensure_config.return_value = {"created": False, "path": "/test/config.yaml"}
        mock_install_git_hooks.return_value = {
            "success": True,
            "installed": ["pre-commit"],
            "skipped": ["post-merge (already installed)"],
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["install", "--hooks"])

        assert result.exit_code == 0
        assert "Installed git hooks" in result.output
        assert "Skipped" in result.output
        assert "post-merge (already installed)" in result.output

    @patch("gobby.cli.install._ensure_daemon_config")
    @patch("gobby.cli.install.install_git_hooks")
    @patch("gobby.cli.install._is_claude_code_installed")
    @patch("gobby.cli.install._is_gemini_cli_installed")
    @patch("gobby.cli.install._is_codex_cli_installed")
    @patch("gobby.cli.load_full_config_from_db")
    def test_install_hooks_failure(
        self,
        mock_load_config: MagicMock,
        mock_codex: MagicMock,
        mock_gemini: MagicMock,
        mock_claude: MagicMock,
        mock_install_git_hooks: MagicMock,
        mock_ensure_config: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test install --hooks when git hooks installation fails."""
        mock_load_config.return_value = MagicMock()
        mock_codex.return_value = False
        mock_ensure_config.return_value = {"created": False, "path": "/test/config.yaml"}
        mock_install_git_hooks.return_value = {
            "success": False,
            "error": "Not a git repository",
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["install", "--hooks"])

        assert result.exit_code == 1
        assert "Not a git repository" in result.output
        assert "Some installations failed" in result.output

    @patch("gobby.cli.install._ensure_daemon_config")
    @patch("gobby.cli.install.install_claude")
    @patch("gobby.cli.install.install_gemini")
    @patch("gobby.cli.install.install_git_hooks")
    @patch("gobby.cli.install._is_claude_code_installed")
    @patch("gobby.cli.install._is_gemini_cli_installed")
    @patch("gobby.cli.install._is_codex_cli_installed")
    @patch("gobby.cli.load_full_config_from_db")
    def test_install_all_flag_auto_detect(
        self,
        mock_load_config: MagicMock,
        mock_codex: MagicMock,
        mock_gemini: MagicMock,
        mock_claude: MagicMock,
        mock_install_git_hooks: MagicMock,
        mock_install_gemini: MagicMock,
        mock_install_claude: MagicMock,
        mock_ensure_config: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test install with --all flag auto-detects CLIs."""
        mock_load_config.return_value = MagicMock()
        mock_claude.return_value = True
        mock_gemini.return_value = True
        mock_codex.return_value = False
        mock_ensure_config.return_value = {"created": True, "path": "/test/config.yaml"}
        mock_install_claude.return_value = {
            "success": True,
            "hooks_installed": ["SessionStart"],
            "workflows_installed": [],
            "commands_installed": [],
            "plugins_installed": [],
        }
        mock_install_gemini.return_value = {
            "success": True,
            "hooks_installed": ["SessionStart"],
            "workflows_installed": [],
            "commands_installed": [],
            "plugins_installed": [],
        }
        mock_install_git_hooks.return_value = {
            "success": True,
            "installed": ["pre-commit"],
            "skipped": [],
        }

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.install.run_daemon_setup"),
        ):
            # Create .git directory to trigger git hooks install
            Path(".git").mkdir()
            result = runner.invoke(cli, ["install", "--all"])

        assert result.exit_code == 0
        assert "Claude Code" in result.output
        assert "Gemini CLI" in result.output
        assert "Git Hooks" in result.output
        mock_install_claude.assert_called_once()
        mock_install_gemini.assert_called_once()
        mock_install_git_hooks.assert_called_once()

    @patch("gobby.cli.install._ensure_daemon_config")
    @patch("gobby.cli.install.install_claude")
    @patch("gobby.cli.install._is_claude_code_installed")
    @patch("gobby.cli.install._is_gemini_cli_installed")
    @patch("gobby.cli.install._is_codex_cli_installed")
    @patch("gobby.cli.load_full_config_from_db")
    def test_install_default_acts_like_all(
        self,
        mock_load_config: MagicMock,
        mock_codex: MagicMock,
        mock_gemini: MagicMock,
        mock_claude: MagicMock,
        mock_install_claude: MagicMock,
        mock_ensure_config: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test install with no flags acts like --all."""
        mock_load_config.return_value = MagicMock()
        mock_claude.return_value = True
        mock_gemini.return_value = False
        mock_codex.return_value = False
        mock_ensure_config.return_value = {"created": False, "path": "/test/config.yaml"}
        mock_install_claude.return_value = {
            "success": True,
            "hooks_installed": ["SessionStart"],
            "workflows_installed": [],
            "commands_installed": [],
            "plugins_installed": [],
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["install"])

        assert result.exit_code == 0
        mock_install_claude.assert_called_once()
        assert mock_install_claude.call_count == 1
        assert mock_install_claude.call_args is not None

    @patch("gobby.cli.install._ensure_daemon_config")
    @patch("gobby.cli.install.install_claude")
    @patch("gobby.cli.install._is_claude_code_installed")
    @patch("gobby.cli.install._is_gemini_cli_installed")
    @patch("gobby.cli.install._is_codex_cli_installed")
    @patch("gobby.cli.load_full_config_from_db")
    def test_install_claude_failure(
        self,
        mock_load_config: MagicMock,
        mock_codex: MagicMock,
        mock_gemini: MagicMock,
        mock_claude: MagicMock,
        mock_install_claude: MagicMock,
        mock_ensure_config: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test install when Claude installation fails."""
        mock_load_config.return_value = MagicMock()
        mock_codex.return_value = False
        mock_ensure_config.return_value = {"created": False, "path": "/test/config.yaml"}
        mock_install_claude.return_value = {
            "success": False,
            "error": "Missing source files",
            "hooks_installed": [],
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["install", "--claude"])

        assert result.exit_code == 1
        assert "Failed: Missing source files" in result.output
        assert "Some installations failed" in result.output

    @patch("gobby.cli.install._ensure_daemon_config")
    @patch("gobby.cli.install.install_claude")
    @patch("gobby.cli.install._is_claude_code_installed")
    @patch("gobby.cli.install._is_gemini_cli_installed")
    @patch("gobby.cli.install._is_codex_cli_installed")
    @patch("gobby.cli.install.get_install_dir")
    @patch("gobby.cli.load_full_config_from_db")
    def test_install_shows_dev_mode(
        self,
        mock_load_config: MagicMock,
        mock_get_install_dir: MagicMock,
        mock_codex: MagicMock,
        mock_gemini: MagicMock,
        mock_claude: MagicMock,
        mock_install_claude: MagicMock,
        mock_ensure_config: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test install shows development mode when using source directory."""
        mock_load_config.return_value = MagicMock()
        mock_codex.return_value = False
        mock_get_install_dir.return_value = Path("/home/user/project/src/gobby/install")
        mock_ensure_config.return_value = {"created": False, "path": "/test/config.yaml"}
        mock_install_claude.return_value = {
            "success": True,
            "hooks_installed": ["SessionStart"],
            "workflows_installed": [],
            "commands_installed": [],
            "plugins_installed": [],
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["install", "--claude"])

        assert result.exit_code == 0
        assert "Development" in result.output

    @patch("gobby.cli.install._ensure_daemon_config")
    @patch("gobby.cli.install.install_claude")
    @patch("gobby.cli.install._is_claude_code_installed")
    @patch("gobby.cli.install._is_gemini_cli_installed")
    @patch("gobby.cli.install._is_codex_cli_installed")
    @patch("gobby.cli.load_full_config_from_db")
    def test_install_shows_created_config(
        self,
        mock_load_config: MagicMock,
        mock_codex: MagicMock,
        mock_gemini: MagicMock,
        mock_claude: MagicMock,
        mock_install_claude: MagicMock,
        mock_ensure_config: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test install shows when daemon config was created."""
        mock_load_config.return_value = MagicMock()
        mock_codex.return_value = False
        mock_ensure_config.return_value = {"created": True, "path": "/home/user/.gobby/config.yaml"}
        mock_install_claude.return_value = {
            "success": True,
            "hooks_installed": ["SessionStart"],
            "workflows_installed": [],
            "commands_installed": [],
            "plugins_installed": [],
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["install", "--claude"])

        assert result.exit_code == 0
        assert "Created daemon config" in result.output

    @patch("gobby.cli.install._ensure_daemon_config")
    @patch("gobby.cli.install.install_codex")
    @patch("gobby.cli.install._is_claude_code_installed")
    @patch("gobby.cli.install._is_gemini_cli_installed")
    @patch("gobby.cli.install._is_codex_cli_installed")
    @patch("gobby.cli.load_full_config_from_db")
    def test_install_codex_config_already_configured(
        self,
        mock_load_config: MagicMock,
        mock_codex_detected: MagicMock,
        mock_gemini: MagicMock,
        mock_claude: MagicMock,
        mock_install_codex: MagicMock,
        mock_ensure_config: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test install codex when config was already configured."""
        mock_load_config.return_value = MagicMock()
        mock_codex_detected.return_value = True
        mock_ensure_config.return_value = {"created": False, "path": "/test/config.yaml"}
        mock_install_codex.return_value = {
            "success": True,
            "hooks_installed": [],
            "files_installed": ["/home/user/.gobby/hooks/codex/hook_dispatcher.py"],
            "workflows_installed": [],
            "commands_installed": [],
            "plugins_installed": [],
            "config_updated": False,  # Already configured
            "mcp_configured": False,
            "mcp_already_configured": True,
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["install", "--codex"])

        assert result.exit_code == 0
        assert "Codex" in result.output
        assert "Configuration:" in result.output


class TestUninstallCommand:
    """Tests for the uninstall CLI command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    def test_uninstall_help(self, runner: CliRunner) -> None:
        """Test uninstall --help displays help text."""
        result = runner.invoke(cli, ["uninstall", "--help"])
        assert result.exit_code == 0
        assert "Uninstall Gobby hooks" in result.output
        assert "--claude" in result.output
        assert "--gemini" in result.output
        assert "--qwen" in result.output
        assert "--codex" in result.output
        assert "--all" in result.output
        assert "--yes" in result.output or "-y" in result.output

    @patch("gobby.cli.load_full_config_from_db")
    def test_uninstall_no_hooks_found(
        self,
        mock_load_config: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test uninstall when no hooks are found."""
        mock_load_config.return_value = MagicMock()

        fake_home = temp_dir / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["uninstall", "--yes"])

        assert result.exit_code == 0
        assert "No Gobby hooks found" in result.output

    @patch("gobby.cli.install.uninstall_claude")
    @patch("gobby.cli.load_full_config_from_db")
    def test_uninstall_claude_only_flag(
        self,
        mock_load_config: MagicMock,
        mock_uninstall_claude: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test uninstall with --claude flag only."""
        mock_load_config.return_value = MagicMock()
        mock_uninstall_claude.return_value = {
            "success": True,
            "hooks_removed": ["SessionStart", "SessionEnd"],
            "files_removed": ["hook_dispatcher.py"],
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            # Create .claude directory so it's detected
            Path(".claude").mkdir()
            Path(".claude/settings.json").write_text("{}")

            result = runner.invoke(cli, ["uninstall", "--claude", "--yes"])

        assert result.exit_code == 0
        assert "Claude Code" in result.output
        assert "Removed 2 hooks" in result.output
        assert "Removed 1 files" in result.output

        mock_uninstall_claude.assert_called_once()

    @patch("gobby.cli.install.uninstall_gemini")
    @patch("gobby.cli.load_full_config_from_db")
    def test_uninstall_gemini_only_flag(
        self,
        mock_load_config: MagicMock,
        mock_uninstall_gemini: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test uninstall with --gemini flag only."""
        mock_load_config.return_value = MagicMock()
        mock_uninstall_gemini.return_value = {
            "success": True,
            "hooks_removed": ["SessionStart"],
            "files_removed": ["hook_dispatcher.py"],
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            # Create .gemini directory so it's detected
            Path(".gemini").mkdir()
            Path(".gemini/settings.json").write_text("{}")

            result = runner.invoke(cli, ["uninstall", "--gemini", "--yes"])

        assert result.exit_code == 0
        assert "Gemini CLI" in result.output
        assert "Removed 1 hooks" in result.output
        mock_uninstall_gemini.assert_called_once()

    @patch("gobby.cli.install.uninstall_codex")
    @patch("gobby.cli.load_full_config_from_db")
    def test_uninstall_codex_only_flag(
        self,
        mock_load_config: MagicMock,
        mock_uninstall_codex: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test uninstall with --codex flag only."""
        mock_load_config.return_value = MagicMock()
        mock_uninstall_codex.return_value = {
            "success": True,
            "hooks_removed": [],
            "files_removed": ["/home/user/.gobby/hooks/codex/hook_dispatcher.py"],
            "config_updated": True,
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["uninstall", "--codex", "--yes"])

        assert result.exit_code == 0
        assert "Codex" in result.output
        assert "Removed 1 files" in result.output
        mock_uninstall_codex.assert_called_once()

    @patch("gobby.cli.install.uninstall_qwen")
    @patch("gobby.cli.load_full_config_from_db")
    def test_uninstall_qwen_only_flag(
        self,
        mock_load_config: MagicMock,
        mock_uninstall_qwen: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test uninstall with --qwen flag only."""
        mock_load_config.return_value = MagicMock()
        mock_uninstall_qwen.return_value = {
            "success": True,
            "hooks_removed": ["SessionStart"],
            "files_removed": ["hook_dispatcher.py"],
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            Path(".qwen").mkdir()
            Path(".qwen/settings.json").write_text("{}")

            result = runner.invoke(cli, ["uninstall", "--qwen", "--yes"])

        assert result.exit_code == 0
        assert "Qwen CLI" in result.output
        assert "Removed 1 hooks" in result.output
        mock_uninstall_qwen.assert_called_once()

    @patch("gobby.cli.install.uninstall_droid")
    @patch("gobby.cli.load_full_config_from_db")
    def test_uninstall_droid_only_flag(
        self,
        mock_load_config: MagicMock,
        mock_uninstall_droid: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test uninstall with --droid flag only."""
        mock_load_config.return_value = MagicMock()
        mock_uninstall_droid.return_value = {
            "success": True,
            "hooks_removed": ["SessionStart"],
            "files_removed": [],
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["uninstall", "--droid", "--yes"])

        assert result.exit_code == 0
        assert "Droid CLI" in result.output
        assert "Removed 1 hooks" in result.output
        mock_uninstall_droid.assert_called_once()

    @patch("gobby.cli.install.uninstall_claude")
    @patch("gobby.cli.load_full_config_from_db")
    def test_uninstall_claude_failure(
        self,
        mock_load_config: MagicMock,
        mock_uninstall_claude: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test uninstall when Claude uninstallation fails."""
        mock_load_config.return_value = MagicMock()
        mock_uninstall_claude.return_value = {
            "success": False,
            "error": "Settings file not found",
            "hooks_removed": [],
            "files_removed": [],
            "skills_removed": [],
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["uninstall", "--claude", "--yes"])

        assert result.exit_code == 1
        assert "Failed: Settings file not found" in result.output
        assert "Some uninstallations failed" in result.output

    @patch("gobby.cli.install.uninstall_claude")
    @patch("gobby.cli.load_full_config_from_db")
    def test_uninstall_no_hooks_to_remove(
        self,
        mock_load_config: MagicMock,
        mock_uninstall_claude: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test uninstall when no hooks were found to remove."""
        mock_load_config.return_value = MagicMock()
        mock_uninstall_claude.return_value = {
            "success": True,
            "hooks_removed": [],
            "files_removed": [],
            "skills_removed": [],
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["uninstall", "--claude", "--yes"])

        assert result.exit_code == 0
        assert "(no hooks found to remove)" in result.output

    @patch("gobby.cli.install.uninstall_codex")
    @patch("gobby.cli.load_full_config_from_db")
    def test_uninstall_codex_no_integration_found(
        self,
        mock_load_config: MagicMock,
        mock_uninstall_codex: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test uninstall codex when no integration was found."""
        mock_load_config.return_value = MagicMock()
        mock_uninstall_codex.return_value = {
            "success": True,
            "hooks_removed": [],
            "files_removed": [],
            "config_updated": False,
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["uninstall", "--codex", "--yes"])

        assert result.exit_code == 0
        assert "(no hooks found to remove)" in result.output

    @patch("gobby.cli.install.uninstall_codex")
    @patch("gobby.cli.install.uninstall_claude")
    @patch("gobby.cli.install.uninstall_gemini")
    @patch("gobby.cli.load_full_config_from_db")
    def test_uninstall_all_auto_detect(
        self,
        mock_load_config: MagicMock,
        mock_uninstall_gemini: MagicMock,
        mock_uninstall_claude: MagicMock,
        _mock_uninstall_codex: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test uninstall with --all auto-detects installed CLIs."""
        mock_load_config.return_value = MagicMock()
        mock_uninstall_claude.return_value = {
            "success": True,
            "hooks_removed": ["SessionStart"],
            "files_removed": [],
            "skills_removed": [],
        }
        mock_uninstall_gemini.return_value = {
            "success": True,
            "hooks_removed": ["SessionStart"],
            "files_removed": [],
        }
        _mock_uninstall_codex.return_value = {
            "success": True,
            "files_removed": [],
            "config_updated": False,
        }

        fake_home = temp_dir / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            # Create both .claude and .gemini directories under fake home
            (fake_home / ".claude").mkdir()
            (fake_home / ".claude" / "settings.json").write_text("{}")
            (fake_home / ".gemini").mkdir()
            (fake_home / ".gemini" / "settings.json").write_text("{}")

            result = runner.invoke(cli, ["uninstall", "--all", "--yes"])

        assert result.exit_code == 0
        assert "Claude Code" in result.output
        assert "Gemini CLI" in result.output
        mock_uninstall_claude.assert_called_once()
        mock_uninstall_gemini.assert_called_once()

    @patch("gobby.cli.load_full_config_from_db")
    def test_uninstall_requires_confirmation(
        self,
        mock_load_config: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test uninstall requires confirmation without --yes."""
        mock_load_config.return_value = MagicMock()

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            # Create .claude directory
            Path(".claude").mkdir()
            Path(".claude/settings.json").write_text("{}")

            # Without --yes, should prompt and abort
            result = runner.invoke(cli, ["uninstall", "--claude"], input="n\n")

        assert result.exit_code == 1
        assert "Aborted" in result.output

    @patch("gobby.cli.install.uninstall_claude")
    @patch("gobby.cli.load_full_config_from_db")
    def test_uninstall_confirms_with_yes_input(
        self,
        mock_load_config: MagicMock,
        mock_uninstall_claude: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test uninstall proceeds when user confirms with 'y'."""
        mock_load_config.return_value = MagicMock()
        mock_uninstall_claude.return_value = {
            "success": True,
            "hooks_removed": ["SessionStart"],
            "files_removed": [],
            "skills_removed": [],
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            Path(".claude").mkdir()
            Path(".claude/settings.json").write_text("{}")

            result = runner.invoke(cli, ["uninstall", "--claude"], input="y\n")

        assert result.exit_code == 0
        mock_uninstall_claude.assert_called_once()

    @patch("gobby.cli.install.uninstall_codex")
    @patch("gobby.cli.install.uninstall_claude")
    @patch("gobby.cli.install.uninstall_gemini")
    @patch("gobby.cli.load_full_config_from_db")
    def test_uninstall_default_acts_like_all(
        self,
        mock_load_config: MagicMock,
        mock_uninstall_gemini: MagicMock,
        mock_uninstall_claude: MagicMock,
        _mock_uninstall_codex: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test uninstall with no flags acts like --all."""
        mock_load_config.return_value = MagicMock()
        mock_uninstall_claude.return_value = {
            "success": True,
            "hooks_removed": ["SessionStart"],
            "files_removed": [],
            "skills_removed": [],
        }
        mock_uninstall_gemini.return_value = {
            "success": True,
            "hooks_removed": ["SessionStart"],
            "files_removed": [],
        }
        _mock_uninstall_codex.return_value = {
            "success": True,
            "hooks_removed": [],
            "files_removed": [],
            "config_updated": False,
        }

        fake_home = temp_dir / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            # Create both directories under fake home
            (fake_home / ".claude").mkdir()
            (fake_home / ".claude" / "settings.json").write_text("{}")
            (fake_home / ".gemini").mkdir()
            (fake_home / ".gemini" / "settings.json").write_text("{}")

            result = runner.invoke(cli, ["uninstall", "--yes"])

        assert result.exit_code == 0
        mock_uninstall_claude.assert_called_once()
        assert mock_uninstall_claude.call_count == 1
        assert mock_uninstall_claude.call_args is not None
        mock_uninstall_gemini.assert_called_once()
        assert mock_uninstall_gemini.call_count == 1
        assert mock_uninstall_gemini.call_args is not None


class TestInstallCommandDirectInvocation:
    """Tests for directly invoking install/uninstall Click commands."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    @patch("gobby.cli.install._ensure_daemon_config")
    @patch("gobby.cli.install.install_claude")
    @patch("gobby.cli.install._is_claude_code_installed")
    @patch("gobby.cli.install._is_gemini_cli_installed")
    @patch("gobby.cli.install._is_codex_cli_installed")
    @patch("gobby.cli.install.get_install_dir")
    def test_invoke_install_directly(
        self,
        mock_get_install_dir: MagicMock,
        mock_codex: MagicMock,
        mock_gemini: MagicMock,
        mock_claude: MagicMock,
        mock_install_claude: MagicMock,
        mock_ensure_config: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test invoking the install command directly."""
        mock_codex.return_value = False
        mock_get_install_dir.return_value = temp_dir / "install"
        mock_ensure_config.return_value = {"created": False, "path": "/test/config.yaml"}
        mock_install_claude.return_value = {
            "success": True,
            "hooks_installed": ["SessionStart"],
            "workflows_installed": [],
            "commands_installed": [],
            "plugins_installed": [],
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(install, ["--claude"])

        assert result.exit_code == 0
        assert mock_install_claude.call_count == 1

    @patch("gobby.cli.install.uninstall_claude")
    def test_invoke_uninstall_directly(
        self,
        mock_uninstall_claude: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test invoking the uninstall command directly."""
        mock_uninstall_claude.return_value = {
            "success": True,
            "hooks_removed": ["SessionStart"],
            "files_removed": [],
            "skills_removed": [],
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            Path(".claude").mkdir()
            Path(".claude/settings.json").write_text("{}")

            result = runner.invoke(uninstall, ["--claude", "--yes"])

        assert result.exit_code == 0


class TestInstallEdgeCases:
    """Tests for edge cases in install command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    @patch("gobby.cli.install._ensure_daemon_config")
    @patch("gobby.cli.install.install_claude")
    @patch("gobby.cli.install.install_gemini")
    @patch("gobby.cli.install._is_claude_code_installed")
    @patch("gobby.cli.install._is_gemini_cli_installed")
    @patch("gobby.cli.install._is_codex_cli_installed")
    @patch("gobby.cli.load_full_config_from_db")
    def test_install_multiple_flags(
        self,
        mock_load_config: MagicMock,
        mock_codex: MagicMock,
        mock_gemini: MagicMock,
        mock_claude: MagicMock,
        mock_install_gemini: MagicMock,
        mock_install_claude: MagicMock,
        mock_ensure_config: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test install with multiple CLI flags."""
        mock_load_config.return_value = MagicMock()
        mock_codex.return_value = False
        mock_ensure_config.return_value = {"created": False, "path": "/test/config.yaml"}
        mock_install_claude.return_value = {
            "success": True,
            "hooks_installed": ["SessionStart"],
            "workflows_installed": [],
            "commands_installed": [],
            "plugins_installed": [],
        }
        mock_install_gemini.return_value = {
            "success": True,
            "hooks_installed": ["SessionStart"],
            "workflows_installed": [],
            "commands_installed": [],
            "plugins_installed": [],
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["install", "--claude", "--gemini"])

        assert result.exit_code == 0
        assert "Claude Code" in result.output
        assert "Gemini CLI" in result.output
        mock_install_claude.assert_called_once()
        mock_install_gemini.assert_called_once()

    @patch("gobby.cli.install._ensure_daemon_config")
    @patch("gobby.cli.install.install_claude")
    @patch("gobby.cli.install.install_git_hooks")
    @patch("gobby.cli.install._is_claude_code_installed")
    @patch("gobby.cli.install._is_gemini_cli_installed")
    @patch("gobby.cli.install._is_codex_cli_installed")
    @patch("gobby.cli.load_full_config_from_db")
    def test_install_cli_and_hooks_together(
        self,
        mock_load_config: MagicMock,
        mock_codex: MagicMock,
        mock_gemini: MagicMock,
        mock_claude: MagicMock,
        mock_install_git_hooks: MagicMock,
        mock_install_claude: MagicMock,
        mock_ensure_config: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test install with both CLI and git hooks flags."""
        mock_load_config.return_value = MagicMock()
        mock_codex.return_value = False
        mock_ensure_config.return_value = {"created": False, "path": "/test/config.yaml"}
        mock_install_claude.return_value = {
            "success": True,
            "hooks_installed": ["SessionStart"],
            "workflows_installed": [],
            "commands_installed": [],
            "plugins_installed": [],
        }
        mock_install_git_hooks.return_value = {
            "success": True,
            "installed": ["pre-commit"],
            "skipped": [],
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["install", "--claude", "--hooks"])

        assert result.exit_code == 0
        assert "Claude Code" in result.output
        assert "Git Hooks" in result.output
        mock_install_claude.assert_called_once()
        mock_install_git_hooks.assert_called_once()

    @patch("gobby.cli.install._ensure_daemon_config")
    @patch("gobby.cli.install.install_git_hooks")
    @patch("gobby.cli.install._is_claude_code_installed")
    @patch("gobby.cli.install._is_gemini_cli_installed")
    @patch("gobby.cli.install._is_codex_cli_installed")
    @patch("gobby.cli.load_full_config_from_db")
    def test_install_hooks_empty_result(
        self,
        mock_load_config: MagicMock,
        mock_codex: MagicMock,
        mock_gemini: MagicMock,
        mock_claude: MagicMock,
        mock_install_git_hooks: MagicMock,
        mock_ensure_config: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test install --hooks when no hooks are installed or skipped."""
        mock_load_config.return_value = MagicMock()
        mock_codex.return_value = False
        mock_ensure_config.return_value = {"created": False, "path": "/test/config.yaml"}
        mock_install_git_hooks.return_value = {
            "success": True,
            "installed": [],
            "skipped": [],
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["install", "--hooks"])

        assert result.exit_code == 0
        assert "No hooks to install" in result.output


class TestUninstallEdgeCases:
    """Tests for edge cases in uninstall command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    @patch("gobby.cli.install.uninstall_codex")
    @patch("gobby.cli.load_full_config_from_db")
    def test_uninstall_codex_checks_home_path(
        self,
        mock_load_config: MagicMock,
        mock_uninstall_codex: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test uninstall --all checks codex notify in home directory."""
        mock_load_config.return_value = MagicMock()
        mock_uninstall_codex.return_value = {
            "success": True,
            "hooks_removed": [],
            "files_removed": [str(temp_dir / ".gobby/hooks/codex/hook_dispatcher.py")],
            "config_updated": True,
        }

        # Create the codex hooks.json in the fake home directory
        fake_home = temp_dir / "home"
        fake_home.mkdir()
        codex_dir = fake_home / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "hooks.json").write_text("{}")

        # Monkeypatch Path.home() to return our fake home
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["uninstall", "--all", "--yes"])

        assert result.exit_code == 0
        assert "Codex" in result.output
        mock_uninstall_codex.assert_called_once()

    @patch("gobby.cli.install.uninstall_gemini")
    @patch("gobby.cli.load_full_config_from_db")
    def test_uninstall_gemini_failure(
        self,
        mock_load_config: MagicMock,
        mock_uninstall_gemini: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test uninstall when Gemini uninstallation fails."""
        mock_load_config.return_value = MagicMock()
        mock_uninstall_gemini.return_value = {
            "success": False,
            "error": "Permission denied",
            "hooks_removed": [],
            "files_removed": [],
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["uninstall", "--gemini", "--yes"])

        assert result.exit_code == 1
        assert "Failed: Permission denied" in result.output

    @patch("gobby.cli.install.uninstall_codex")
    @patch("gobby.cli.load_full_config_from_db")
    def test_uninstall_codex_failure(
        self,
        mock_load_config: MagicMock,
        mock_uninstall_codex: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test uninstall when Codex uninstallation fails."""
        mock_load_config.return_value = MagicMock()
        mock_uninstall_codex.return_value = {
            "success": False,
            "error": "Failed to update Codex config",
            "files_removed": [],
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["uninstall", "--codex", "--yes"])

        assert result.exit_code == 1
        assert "Failed: Failed to update Codex config" in result.output


class TestInstallFullOutput:
    """Tests for install command full output paths with skills, workflows, commands, plugins."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    @patch("gobby.cli.install._ensure_daemon_config")
    @patch("gobby.cli.install.install_claude")
    @patch("gobby.cli.install._is_claude_code_installed")
    @patch("gobby.cli.install._is_gemini_cli_installed")
    @patch("gobby.cli.install._is_codex_cli_installed")
    @patch("gobby.cli.load_full_config_from_db")
    def test_install_claude_with_all_content_types(
        self,
        mock_load_config: MagicMock,
        mock_codex: MagicMock,
        mock_gemini: MagicMock,
        mock_claude: MagicMock,
        mock_install_claude: MagicMock,
        mock_ensure_config: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test install Claude with skills, workflows, commands, and plugins."""
        mock_load_config.return_value = MagicMock()
        mock_codex.return_value = False
        mock_ensure_config.return_value = {"created": False, "path": "/test/config.yaml"}
        mock_install_claude.return_value = {
            "success": True,
            "hooks_installed": ["SessionStart", "SessionEnd", "PreToolUse"],
            "workflows_installed": ["plan-execute", "test-driven"],
            "commands_installed": ["validate", "sync"],
            "plugins_installed": ["task-hooks", "session-tracker"],
            "mcp_configured": True,
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["install", "--claude"])

        assert result.exit_code == 0
        assert "Installed 3 hooks" in result.output
        assert "SessionStart" in result.output
        assert "SessionEnd" in result.output

        assert "Installed 2 workflows" in result.output
        assert "plan-execute" in result.output
        assert "Installed 2 skills/commands" in result.output
        assert "validate" in result.output
        assert "Installed 2 plugins" in result.output
        assert "task-hooks" in result.output
        assert "Configured MCP server" in result.output

    @patch("gobby.cli.install._ensure_daemon_config")
    @patch("gobby.cli.install.install_gemini")
    @patch("gobby.cli.install._is_claude_code_installed")
    @patch("gobby.cli.install._is_gemini_cli_installed")
    @patch("gobby.cli.install._is_codex_cli_installed")
    @patch("gobby.cli.load_full_config_from_db")
    def test_install_gemini_with_all_content_types(
        self,
        mock_load_config: MagicMock,
        mock_codex: MagicMock,
        mock_gemini: MagicMock,
        mock_claude: MagicMock,
        mock_install_gemini: MagicMock,
        mock_ensure_config: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test install Gemini with skills, workflows, commands, and plugins."""
        mock_load_config.return_value = MagicMock()
        mock_codex.return_value = False
        mock_ensure_config.return_value = {"created": False, "path": "/test/config.yaml"}
        mock_install_gemini.return_value = {
            "success": True,
            "hooks_installed": ["SessionStart", "BeforeAgent"],
            "workflows_installed": ["gemini-workflow"],
            "commands_installed": ["gemini-cmd"],
            "plugins_installed": ["gemini-plugin"],
            "mcp_configured": True,
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["install", "--gemini"])

        assert result.exit_code == 0
        assert "Gemini CLI" in result.output
        assert "Installed 2 hooks" in result.output

        assert "Installed 1 workflows" in result.output
        assert "Installed 1 skills/commands" in result.output
        assert "Installed 1 plugins" in result.output
        assert "Configured MCP server: ~/.gemini/settings.json" in result.output

    @patch("gobby.cli.install._ensure_daemon_config")
    @patch("gobby.cli.install.install_gemini")
    @patch("gobby.cli.install._is_claude_code_installed")
    @patch("gobby.cli.install._is_gemini_cli_installed")
    @patch("gobby.cli.install._is_codex_cli_installed")
    @patch("gobby.cli.load_full_config_from_db")
    def test_install_gemini_failure(
        self,
        mock_load_config: MagicMock,
        mock_codex: MagicMock,
        mock_gemini: MagicMock,
        mock_claude: MagicMock,
        mock_install_gemini: MagicMock,
        mock_ensure_config: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test install when Gemini installation fails."""
        mock_load_config.return_value = MagicMock()
        mock_codex.return_value = False
        mock_ensure_config.return_value = {"created": False, "path": "/test/config.yaml"}
        mock_install_gemini.return_value = {
            "success": False,
            "error": "Missing hooks template",
            "hooks_installed": [],
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["install", "--gemini"])

        assert result.exit_code == 1
        assert "Gemini CLI" in result.output
        assert "Failed: Missing hooks template" in result.output
        assert "Some installations failed" in result.output

    @patch("gobby.cli.install._ensure_daemon_config")
    @patch("gobby.cli.install.install_codex")
    @patch("gobby.cli.install._is_claude_code_installed")
    @patch("gobby.cli.install._is_gemini_cli_installed")
    @patch("gobby.cli.install._is_codex_cli_installed")
    @patch("gobby.cli.load_full_config_from_db")
    def test_install_codex_with_all_content_types(
        self,
        mock_load_config: MagicMock,
        mock_codex_detected: MagicMock,
        mock_gemini: MagicMock,
        mock_claude: MagicMock,
        mock_install_codex: MagicMock,
        mock_ensure_config: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test install Codex with skills, workflows, commands, and plugins."""
        mock_load_config.return_value = MagicMock()
        mock_codex_detected.return_value = True
        mock_ensure_config.return_value = {"created": False, "path": "/test/config.yaml"}
        mock_install_codex.return_value = {
            "success": True,
            "hooks_installed": [],
            "files_installed": ["/home/user/.gobby/hooks/codex/hook_dispatcher.py"],
            "workflows_installed": ["codex-workflow"],
            "commands_installed": ["codex-cmd"],
            "plugins_installed": ["codex-plugin"],
            "config_updated": True,
            "mcp_configured": True,
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["install", "--codex"])

        assert result.exit_code == 0
        assert "Codex" in result.output
        assert "Installed 0 hooks" in result.output

        assert "Installed 1 workflows" in result.output
        assert "Installed 1 skills/commands" in result.output
        assert "Installed 1 plugins" in result.output

    @patch("gobby.cli.install._ensure_daemon_config")
    @patch("gobby.cli.install.install_codex")
    @patch("gobby.cli.install._is_claude_code_installed")
    @patch("gobby.cli.install._is_gemini_cli_installed")
    @patch("gobby.cli.install._is_codex_cli_installed")
    @patch("gobby.cli.load_full_config_from_db")
    def test_install_codex_failure(
        self,
        mock_load_config: MagicMock,
        mock_codex_detected: MagicMock,
        mock_gemini: MagicMock,
        mock_claude: MagicMock,
        mock_install_codex: MagicMock,
        mock_ensure_config: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test install when Codex installation fails."""
        mock_load_config.return_value = MagicMock()
        mock_codex_detected.return_value = True
        mock_ensure_config.return_value = {"created": False, "path": "/test/config.yaml"}
        mock_install_codex.return_value = {
            "success": False,
            "error": "Missing source file",
            "files_installed": [],
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["install", "--codex"])

        assert result.exit_code == 1
        assert "Codex" in result.output
        assert "Failed: Missing source file" in result.output
        assert "Some installations failed" in result.output


class TestUninstallFullOutput:
    """Tests for uninstall command full output paths."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    @patch("gobby.cli.install.uninstall_gemini")
    @patch("gobby.cli.load_full_config_from_db")
    def test_uninstall_gemini_with_files_removed(
        self,
        mock_load_config: MagicMock,
        mock_uninstall_gemini: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test uninstall Gemini with files removed."""
        mock_load_config.return_value = MagicMock()
        mock_uninstall_gemini.return_value = {
            "success": True,
            "hooks_removed": ["SessionStart", "BeforeAgent"],
            "files_removed": ["hook_dispatcher.py", "validate_settings.py"],
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            Path(".gemini").mkdir()
            Path(".gemini/settings.json").write_text("{}")

            result = runner.invoke(cli, ["uninstall", "--gemini", "--yes"])

        assert result.exit_code == 0
        assert "Gemini CLI" in result.output
        assert "Removed 2 hooks" in result.output
        assert "SessionStart" in result.output
        assert "Removed 2 files" in result.output

    @patch("gobby.cli.install.uninstall_gemini")
    @patch("gobby.cli.load_full_config_from_db")
    def test_uninstall_gemini_no_hooks_found(
        self,
        mock_load_config: MagicMock,
        mock_uninstall_gemini: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test uninstall Gemini when no hooks found."""
        mock_load_config.return_value = MagicMock()
        mock_uninstall_gemini.return_value = {
            "success": True,
            "hooks_removed": [],
            "files_removed": [],
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            Path(".gemini").mkdir()
            Path(".gemini/settings.json").write_text("{}")

            result = runner.invoke(cli, ["uninstall", "--gemini", "--yes"])

        assert result.exit_code == 0
        assert "Gemini CLI" in result.output
        assert "(no hooks found to remove)" in result.output


class TestInstallWithCodexAllDetected:
    """Tests for install --all with codex detected."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    @patch("gobby.cli.install._ensure_daemon_config")
    @patch("gobby.cli.install.install_codex")
    @patch("gobby.cli.install.install_git_hooks")
    @patch("gobby.cli.install._is_claude_code_installed")
    @patch("gobby.cli.install._is_gemini_cli_installed")
    @patch("gobby.cli.install._is_codex_cli_installed")
    @patch("gobby.cli.load_full_config_from_db")
    def test_install_all_with_codex_detected(
        self,
        mock_load_config: MagicMock,
        mock_codex: MagicMock,
        mock_gemini: MagicMock,
        mock_claude: MagicMock,
        mock_install_git_hooks: MagicMock,
        mock_install_codex: MagicMock,
        mock_ensure_config: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test install --all when codex is detected."""
        mock_load_config.return_value = MagicMock()
        mock_claude.return_value = False
        mock_gemini.return_value = False
        mock_codex.return_value = True
        mock_ensure_config.return_value = {"created": False, "path": "/test/config.yaml"}
        mock_install_codex.return_value = {
            "success": True,
            "hooks_installed": [],
            "files_installed": ["/home/user/.gobby/hooks/codex/hook_dispatcher.py"],
            "skills_installed": [],
            "workflows_installed": [],
            "commands_installed": [],
            "plugins_installed": [],
            "config_updated": True,
            "mcp_configured": True,
        }
        mock_install_git_hooks.return_value = {
            "success": True,
            "installed": ["pre-commit"],
            "skipped": [],
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            # Create .git directory to trigger git hooks install
            Path(".git").mkdir()
            result = runner.invoke(cli, ["install", "--all"])

        assert result.exit_code == 0
        assert "Codex" in result.output
        assert "Git Hooks" in result.output
        mock_install_codex.assert_called_once()
        mock_install_git_hooks.assert_called_once()
