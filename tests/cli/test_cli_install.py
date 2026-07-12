"""Comprehensive tests for the CLI install module.

Tests for install.py using Click's CliRunner to test all commands and options.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click import ClickException
from click.testing import CliRunner

from gobby.cli import cli
from gobby.cli.install import (
    _configure_secret_kek_posture,
    _ensure_daemon_config,
    _is_agy_cli_installed,
    _is_claude_code_installed,
    _is_codex_cli_installed,
    _is_droid_cli_installed,
    _is_qwen_cli_installed,
    _resolve_ide_settings_consent,
    uninstall,
)
from gobby.storage.auth import LOCAL_API_TOKEN_HASH_KEY, ensure_local_api_token, hash_token
from gobby.storage.config_store import ConfigStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secrets import (
    POSTURE_KEY_FILE,
    POSTURE_SCRYPT_PASSPHRASE,
    SECRET_KEK_PASSPHRASE_ENV,
)

pytestmark = pytest.mark.unit


def test_install_auth_mode_flag(tmp_path: Path) -> None:
    codex_result = {
        "success": True,
        "hooks_installed": [],
        "files_installed": [],
        "workflows_installed": [],
        "commands_installed": [],
        "plugins_installed": [],
        "config_updated": True,
        "mcp_configured": True,
    }

    with (
        patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install")),
        patch(
            "gobby.cli.install._ensure_daemon_config",
            return_value={"created": False, "path": tmp_path / "bootstrap.yaml"},
        ),
        patch("gobby.cli.install.update_bootstrap_yaml") as update_bootstrap,
        patch("gobby.cli.install.load_full_config_from_db", side_effect=FileNotFoundError),
        patch("gobby.cli.install.install_codex", return_value=codex_result),
    ):
        result = CliRunner().invoke(
            cli,
            ["install", "--codex", "--no-interactive", "--auth-mode", "disabled"],
        )

    assert result.exit_code == 0
    update_bootstrap.assert_called_once()
    path, updater = update_bootstrap.call_args.args
    bootstrap: dict[str, str] = {}
    updater(bootstrap)
    assert path == tmp_path / "bootstrap.yaml"
    assert bootstrap["auth_mode"] == "disabled"


class _SecretKekStore:
    def __init__(self, posture: str = POSTURE_KEY_FILE) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.posture = posture
        self.kek_passphrase: str | None = None

    def current_kek_posture(self) -> str:
        return self.posture

    def set_kek_posture(self, posture: str, *, passphrase: str | None = None) -> None:
        self.calls.append((posture, passphrase))
        self.posture = posture


class TestSecretKekPostureInstall:
    def test_key_file_posture_rewraps_existing_passphrase_store(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store = _SecretKekStore(POSTURE_SCRYPT_PASSPHRASE)
        monkeypatch.setenv(SECRET_KEK_PASSPHRASE_ENV, "current horse")

        _configure_secret_kek_posture(store, "key-file", no_interactive=True)

        assert store.kek_passphrase == "current horse"
        assert store.calls == [(POSTURE_KEY_FILE, None)]

    def test_passphrase_posture_uses_env_in_non_interactive(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store = _SecretKekStore()
        monkeypatch.setenv(SECRET_KEK_PASSPHRASE_ENV, "correct horse")

        _configure_secret_kek_posture(store, "passphrase", no_interactive=True)

        assert store.calls == [(POSTURE_SCRYPT_PASSPHRASE, "correct horse")]

    def test_passphrase_posture_requires_env_in_non_interactive(self) -> None:
        with pytest.raises(ClickException, match=SECRET_KEK_PASSPHRASE_ENV):
            _configure_secret_kek_posture(MagicMock(), "passphrase", no_interactive=True)


@pytest.fixture(autouse=True)
def _mock_ext_services_and_prompts():
    """Prevent real Docker service installers and interactive API-key prompts."""
    with (
        patch("gobby.cli.install.run_daemon_setup"),
        patch("gobby.cli.install._run_qdrant_install"),
        patch("gobby.cli.install._run_falkordb_install"),
        patch("gobby.cli.install._run_install_preflight", return_value=([], [])),
        patch("gobby.cli.install._maybe_start_daemon_after_install"),
        patch("gobby.storage.hub.runtime.open_runtime_hub_database", return_value=MagicMock()),
        patch("gobby.cli.install.SecretStore"),
        patch("gobby.cli.install.ConfigStore"),
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


@pytest.fixture(autouse=True)
def _mock_agy_detector() -> None:
    """Keep AGY detection deterministic unless a test overrides it."""
    with patch("gobby.cli.install._is_agy_cli_installed", return_value=False):
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
        assert content["postgres_pool"] == {
            "min_size": 2,
            "max_size": 20,
            "acquire_timeout_seconds": 5.0,
            "open_timeout_seconds": 30.0,
        }


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
    def test_is_agy_cli_installed_true(self, mock_which: MagicMock) -> None:
        """Test AGY CLI detection when installed."""
        mock_which.return_value = "/usr/local/bin/agy"
        assert _is_agy_cli_installed() is True
        mock_which.assert_called_once_with("agy")

    @patch("shutil.which")
    def test_is_agy_cli_installed_false(self, mock_which: MagicMock) -> None:
        """Test AGY CLI detection when not installed."""
        mock_which.return_value = None
        assert _is_agy_cli_installed() is False

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
        retired_flag = "--" + "gemini"
        assert retired_flag not in result.output
        assert "--agy" in result.output
        assert "--qwen" in result.output
        assert "--codex" in result.output
        assert "--droid" in result.output
        assert "--hooks" in result.output
        assert "--all" in result.output
        assert "--embedding-api-key" not in result.output
        assert "--embedding-provider" in result.output
        assert "LM Studio-compatible defaults" in result.output
        assert "openai-compatible uses generic OpenAI-" in result.output
        assert "compatible embedding APIs" in result.output
        assert "--ide-settings" in result.output
        assert "--no-ide-settings" in result.output

    def test_install_ide_settings_option_is_tri_state(self) -> None:
        install_command = cli.commands["install"]
        ide_option = next(
            parameter
            for parameter in install_command.params
            if parameter.name == "ide_settings_flag"
        )

        assert ide_option.default is None

    @pytest.mark.parametrize("explicit_value", [True, False])
    def test_explicit_ide_settings_choice_skips_detection(self, explicit_value: bool) -> None:
        with (
            patch(
                "gobby.cli.installers.ide_config."
                "find_vscode_family_ides_needing_terminal_integration"
            ) as mock_detect,
            patch("gobby.cli.install.click.confirm") as mock_confirm,
        ):
            result = _resolve_ide_settings_consent(explicit_value, no_interactive=False)

        assert result is explicit_value
        mock_detect.assert_not_called()
        mock_confirm.assert_not_called()

    def test_no_interactive_skips_unspecified_ide_settings(self) -> None:
        with patch(
            "gobby.cli.installers.ide_config.find_vscode_family_ides_needing_terminal_integration"
        ) as mock_detect:
            result = _resolve_ide_settings_consent(None, no_interactive=True)

        assert result is False
        mock_detect.assert_not_called()

    @pytest.mark.parametrize(("confirmed", "expected"), [(True, True), (False, False)])
    def test_interactive_ide_settings_prompt(
        self,
        confirmed: bool,
        expected: bool,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with (
            patch(
                "gobby.cli.installers.ide_config."
                "find_vscode_family_ides_needing_terminal_integration",
                return_value=["Cursor", "Antigravity"],
            ),
            patch("gobby.cli.install.click.confirm", return_value=confirmed) as mock_confirm,
        ):
            result = _resolve_ide_settings_consent(None, no_interactive=False)

        assert result is expected
        assert "Cursor, Antigravity" in capsys.readouterr().out
        mock_confirm.assert_called_once_with(
            "Configure detected VS Code-family IDE terminals to use tmux and Gobby session titles?",
            default=True,
        )

    def test_already_configured_ides_do_not_prompt(self) -> None:
        with (
            patch(
                "gobby.cli.installers.ide_config."
                "find_vscode_family_ides_needing_terminal_integration",
                return_value=[],
            ),
            patch("gobby.cli.install.click.confirm") as mock_confirm,
        ):
            result = _resolve_ide_settings_consent(None, no_interactive=False)

        assert result is False
        mock_confirm.assert_not_called()

    @pytest.mark.parametrize("embedding_dim", ["0", "-1"])
    def test_install_rejects_non_positive_embedding_dim(
        self, runner: CliRunner, embedding_dim: str
    ) -> None:
        """--embedding-dim must be rejected by Click before install orchestration runs."""
        result = runner.invoke(cli, ["install", "--embedding-dim", embedding_dim])

        assert result.exit_code == 2
        assert "Invalid value for '--embedding-dim'" in result.output

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
            result = runner.invoke(cli, ["install", "--qwen", "--no-interactive"])

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
            result = runner.invoke(cli, ["install", "--droid", "--no-interactive"])

        assert result.exit_code == 0
        assert "Droid CLI" in result.output
        assert "Installed 1 hooks" in result.output
        mock_install_droid.assert_called_once()

    @patch("gobby.cli.install._ensure_daemon_config")
    @patch("gobby.cli.install.install_agy")
    @patch("gobby.cli.load_full_config_from_db")
    def test_install_agy_only_flag(
        self,
        mock_load_config: MagicMock,
        mock_install_agy: MagicMock,
        mock_ensure_config: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test install with --agy flag only."""
        mock_load_config.return_value = MagicMock()
        mock_ensure_config.return_value = {"created": False, "path": "/test/config.yaml"}
        mock_install_agy.return_value = {
            "success": True,
            "hooks_installed": ["PreInvocation"],
            "workflows_installed": [],
            "commands_installed": [],
            "plugins_installed": [],
            "mcp_configured": True,
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["install", "--agy", "--no-interactive"])

        assert result.exit_code == 0
        assert "AGY CLI" in result.output
        assert "Installed 1 hooks" in result.output
        mock_install_agy.assert_called_once()

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

    def test_install_provisions_api_token(
        self,
        runner: CliRunner,
        temp_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        gobby_home = temp_dir / "gobby-home"
        monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
        config_store = MagicMock()
        config_store.get.return_value = None
        codex_result = {
            "success": True,
            "hooks_installed": [],
            "files_installed": [],
            "workflows_installed": [],
            "commands_installed": [],
            "plugins_installed": [],
            "config_updated": True,
            "mcp_configured": True,
        }

        with (
            patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install")),
            patch(
                "gobby.cli.install._ensure_daemon_config",
                return_value={"created": False, "path": "/test/config.yaml"},
            ),
            patch("gobby.cli.install.load_full_config_from_db"),
            patch("gobby.cli.install.ConfigStore", return_value=config_store),
            patch("gobby.cli.install.install_codex", return_value=codex_result),
        ):
            with runner.isolated_filesystem(temp_dir=str(temp_dir)):
                result = runner.invoke(cli, ["install", "--codex", "--no-interactive"])

        token_path = gobby_home / "local_cli_token"
        assert result.exit_code == 0
        assert token_path.exists()
        token = token_path.read_text().strip()
        config_store.set.assert_called_once_with(
            LOCAL_API_TOKEN_HASH_KEY,
            hash_token(token),
            source="system",
        )
        assert token_path.stat().st_mode & 0o777 == 0o600

    def test_install_db_unreachable_writes_file_only(
        self,
        runner: CliRunner,
        temp_dir: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        gobby_home = temp_dir / "gobby-home"
        monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
        codex_result = {
            "success": True,
            "hooks_installed": [],
            "files_installed": [],
            "workflows_installed": [],
            "commands_installed": [],
            "plugins_installed": [],
            "config_updated": True,
            "mcp_configured": True,
        }

        with (
            patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install")),
            patch(
                "gobby.cli.install._ensure_daemon_config",
                return_value={"created": False, "path": "/test/config.yaml"},
            ),
            patch("gobby.cli.install.load_full_config_from_db", side_effect=FileNotFoundError),
            patch("gobby.cli.install.install_codex", return_value=codex_result),
        ):
            with runner.isolated_filesystem(temp_dir=str(temp_dir)):
                result = runner.invoke(cli, ["install", "--codex", "--no-interactive"])

        token_path = gobby_home / "local_cli_token"
        assert result.exit_code == 0
        original_token = token_path.read_text().strip()
        config_store = ConfigStore(temp_db)
        assert config_store.get(LOCAL_API_TOKEN_HASH_KEY) is None

        assert ensure_local_api_token(config_store) == original_token
        assert token_path.read_text().strip() == original_token
        assert config_store.get(LOCAL_API_TOKEN_HASH_KEY) == hash_token(original_token)


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
        retired_flag = "--" + "gemini"
        assert retired_flag not in result.output
        assert "--agy" in result.output
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

    @patch("gobby.cli.install.uninstall_agy")
    @patch("gobby.cli.load_full_config_from_db")
    def test_uninstall_agy_only_flag(
        self,
        mock_load_config: MagicMock,
        mock_uninstall_agy: MagicMock,
        runner: CliRunner,
        temp_dir: Path,
    ) -> None:
        """Test uninstall with --agy flag only."""
        mock_load_config.return_value = MagicMock()
        mock_uninstall_agy.return_value = {
            "success": True,
            "hooks_removed": ["PreInvocation"],
            "files_removed": [],
        }

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            result = runner.invoke(cli, ["uninstall", "--agy", "--yes"])

        assert result.exit_code == 0
        assert "AGY CLI" in result.output
        assert "Removed 1 hooks" in result.output
        mock_uninstall_agy.assert_called_once()

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


class TestInstallCommandDirectInvocation:
    """Tests for directly invoking install/uninstall Click commands."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

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


class TestUninstallFullOutput:
    """Tests for uninstall command full output paths."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()


class TestInstallWithCodexAllDetected:
    """Tests for install --all with codex detected."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()
