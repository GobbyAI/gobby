"""Tests for cli/install.py — targeting uncovered lines."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli._install_state import (
    EmbeddingInstallState,
    InstallSectionState,
    InstallState,
    VoiceInstallState,
    empty_install_state,
)
from gobby.cli.install import (
    _echo_install_details,
    _echo_uninstall_details,
    _is_claude_code_installed,
    _is_codex_cli_installed,
    install,
    uninstall,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    monkeypatch.setattr(
        "gobby.cli.installers.ide_config.find_vscode_family_ides_needing_terminal_integration",
        lambda: [],
    )
    return CliRunner()


def _configured_install_state() -> InstallState:
    return InstallState(
        embedding=EmbeddingInstallState(
            configured=True,
            summary="disabled",
            provider="none",
            dim=0,
        ),
        voice=VoiceInstallState(configured=True, summary="disabled", enabled=False),
        qdrant=InstallSectionState(configured=True, summary="localhost:6333"),
        falkordb=InstallSectionState(configured=True, summary="localhost:16379"),
        has_existing_values=True,
    )


# ---------------------------------------------------------------------------
# _echo_install_details / _echo_uninstall_details
# ---------------------------------------------------------------------------
class TestEchoHelpers:
    def test_echo_install_details_basic(self) -> None:
        result: dict[str, Any] = {
            "hooks_installed": ["hook1", "hook2"],
        }
        runner = CliRunner()
        with runner.isolated_filesystem():
            _echo_install_details(result)
            assert result["hooks_installed"] == ["hook1", "hook2"]

    def test_echo_install_details_full(self) -> None:
        result: dict[str, Any] = {
            "hooks_installed": ["hook1"],
            "workflows_installed": ["wf1"],
            "agents_installed": ["agent1"],
            "commands_installed": ["cmd1"],
            "plugins_installed": ["plugin1"],
            "mcp_configured": True,
        }
        _echo_install_details(result, mcp_config_path="~/.claude.json", config_path="~/.config")
        assert result["mcp_configured"] is True
        assert result["plugins_installed"] == ["plugin1"]

    def test_echo_install_details_mcp_already(self) -> None:
        result: dict[str, Any] = {
            "hooks_installed": [],
            "mcp_already_configured": True,
        }
        _echo_install_details(result, mcp_config_path="~/.claude.json")
        assert result["mcp_already_configured"] is True

    def test_echo_uninstall_details_with_hooks(self) -> None:
        result: dict[str, Any] = {
            "hooks_removed": ["hook1", "hook2"],
            "files_removed": ["file1"],
        }
        _echo_uninstall_details(result)
        assert result["hooks_removed"] == ["hook1", "hook2"]
        assert result["files_removed"] == ["file1"]

    def test_echo_uninstall_details_empty(self) -> None:
        result: dict[str, Any] = {
            "hooks_removed": [],
            "files_removed": [],
        }
        _echo_uninstall_details(result)
        assert result == {"hooks_removed": [], "files_removed": []}


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------
class TestDetectionHelpers:
    @patch("gobby.cli._detectors.shutil.which", return_value="/usr/bin/claude")
    def test_claude_installed(self, _mock_which: MagicMock) -> None:
        assert _is_claude_code_installed() is True

    @patch("gobby.cli._detectors.shutil.which", return_value=None)
    def test_claude_not_installed(self, _mock_which: MagicMock) -> None:
        assert _is_claude_code_installed() is False

    @patch("gobby.cli._detectors.shutil.which", return_value="/usr/bin/codex")
    def test_codex_installed(self, _mock_which: MagicMock) -> None:
        assert _is_codex_cli_installed() is True

    @patch("gobby.cli._detectors.shutil.which", return_value=None)
    def test_codex_not_installed(self, _mock_which: MagicMock) -> None:
        assert _is_codex_cli_installed() is False


# ---------------------------------------------------------------------------
# install command — --claude only
# ---------------------------------------------------------------------------
class TestInstallCommand:
    @pytest.fixture(autouse=True)
    def _mock_docker_services(self) -> Any:
        """Mock the managed stack — tests here focus on CLI-specific hooks."""
        postgres_result = {"success": True, "database_url": "postgresql://localhost/gobby"}
        qdrant_result = {"success": True, "qdrant_url": "http://localhost:6333"}
        falkordb_result = {
            "success": True,
            "url": "redis://localhost:6379",
            "browser_url": "http://localhost:13000",
            "password_source": "reused",
            "password": None,
        }
        with (
            patch("gobby.cli._install_daemon._docker_daemon_available", return_value=True),
            patch("gobby.cli.install.install_postgres", return_value=postgres_result),
            patch("gobby.cli.install.install_qdrant", return_value=qdrant_result),
            patch("gobby.cli.install.install_falkordb", return_value=falkordb_result),
            patch("gobby.cli.install._resolve_ide_settings_consent", return_value=False),
        ):
            yield

    def test_install_config_only_skips_hooks_and_services(self, runner: CliRunner) -> None:
        with (
            patch(
                "gobby.cli.install._ensure_daemon_config",
                return_value={"created": False, "path": "/fake/bootstrap.yaml"},
            ),
            patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install")),
            patch("gobby.cli.install.run_daemon_setup") as mock_setup,
            patch("gobby.cli.install._should_initialize_project") as mock_should_init,
            patch("gobby.cli.install._resolve_ide_settings_consent") as mock_ide_consent,
            patch("gobby.cli.install.install_agy") as mock_agy,
            patch("gobby.cli.install.install_claude") as mock_claude,
            patch("gobby.cli.install.install_codex") as mock_codex,
            patch("gobby.cli.install.install_droid") as mock_droid,
            patch("gobby.cli.install.install_grok") as mock_grok,
            patch("gobby.cli.install.install_qwen") as mock_qwen,
            patch("gobby.cli.install.install_git_hooks") as mock_git_hooks,
            patch("gobby.cli.install._run_embedding_install") as mock_embedding,
            patch("gobby.cli.install._run_voice_install") as mock_voice,
            patch("gobby.cli.install._run_qdrant_install") as mock_qdrant,
            patch("gobby.cli.install._run_falkordb_install") as mock_falkordb,
            patch("gobby.cli.install._maybe_start_daemon_after_install") as mock_start,
        ):
            result = runner.invoke(install, ["--config-only"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "Configuration and database initialization complete." in result.output
        mock_setup.assert_called_once_with(Path.cwd(), configure_ide_settings=False)
        mock_should_init.assert_not_called()
        mock_ide_consent.assert_not_called()
        for skipped in (
            mock_agy,
            mock_claude,
            mock_codex,
            mock_droid,
            mock_grok,
            mock_qwen,
            mock_git_hooks,
            mock_embedding,
            mock_voice,
            mock_qdrant,
            mock_falkordb,
            mock_start,
        ):
            skipped.assert_not_called()

    @patch("gobby.cli.install.run_daemon_setup")
    @patch(
        "gobby.cli.install._ensure_daemon_config", return_value={"created": False, "path": "/fake"}
    )
    @patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.cli.install.install_claude")
    def test_install_claude_only(
        self,
        mock_install_claude: MagicMock,
        _install_dir: MagicMock,
        _config: MagicMock,
        _setup: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_install_claude.return_value = {
            "success": True,
            "hooks_installed": ["PreToolUse", "PostToolUse"],
            "mcp_configured": True,
        }
        result = runner.invoke(install, ["--claude"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Claude Code" in result.output
        assert "successfully" in result.output.lower()

    @patch("gobby.cli.install.run_daemon_setup")
    @patch(
        "gobby.cli.install._ensure_daemon_config", return_value={"created": True, "path": "/fake"}
    )
    @patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.cli.install.install_claude")
    def test_install_claude_failure(
        self,
        mock_install_claude: MagicMock,
        _install_dir: MagicMock,
        _config: MagicMock,
        _setup: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_install_claude.return_value = {
            "success": False,
            "error": "Something went wrong",
            "hooks_installed": [],
        }
        result = runner.invoke(install, ["--claude"], catch_exceptions=False)
        assert result.exit_code == 1
        assert "Something went wrong" in result.output

    @patch("gobby.cli.install.run_daemon_setup")
    @patch(
        "gobby.cli.install._ensure_daemon_config", return_value={"created": False, "path": "/fake"}
    )
    @patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.cli.install.install_git_hooks")
    def test_install_git_hooks(
        self,
        mock_install: MagicMock,
        _install_dir: MagicMock,
        _config: MagicMock,
        _setup: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_install.return_value = {
            "success": True,
            "installed": ["pre-commit", "post-merge"],
            "skipped": [],
            "wiki_setup": {
                "success": True,
                "gitignore_updated": True,
                "worktree_path": "/fake/repo-wiki",
                "branch": "wiki",
                "warnings": [],
                "tracked_files": [],
            },
        }
        result = runner.invoke(install, ["--hooks"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "pre-commit" in result.output
        assert "Wiki branch setup:" in result.output
        assert "/fake/repo-wiki" in result.output

    def test_install_claude_targeted_skips_embedding_and_services(
        self,
        runner: CliRunner,
    ) -> None:
        """Targeted CLI installs do not configure embeddings or Docker services."""
        claude_result = {
            "success": True,
            "hooks_installed": ["PreToolUse"],
            "mcp_configured": True,
        }
        with (
            patch("gobby.cli.install.run_daemon_setup"),
            patch(
                "gobby.cli.install._ensure_daemon_config",
                return_value={"created": False, "path": "/fake"},
            ),
            patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install")),
            patch("gobby.cli.install.install_claude", return_value=claude_result),
            patch("gobby.cli.install._run_embedding_install") as mock_embedding,
            patch("gobby.cli.install._run_qdrant_install") as mock_qdrant,
            patch("gobby.cli.install._run_falkordb_install") as mock_falkordb,
        ):
            result = runner.invoke(install, ["--claude"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "Embedding Provider" not in result.output
        mock_embedding.assert_not_called()
        mock_qdrant.assert_not_called()
        mock_falkordb.assert_not_called()

    def test_install_default_runs_embedding_and_services(
        self,
        runner: CliRunner,
    ) -> None:
        """Default install configures embeddings and the required managed stack."""
        claude_result = {
            "success": True,
            "hooks_installed": ["PreToolUse"],
            "mcp_configured": True,
        }
        with (
            patch("gobby.cli.install.run_daemon_setup"),
            patch(
                "gobby.cli.install._ensure_daemon_config",
                return_value={"created": False, "path": "/fake"},
            ),
            patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install")),
            patch("gobby.cli.install._is_claude_code_installed", return_value=True),
            patch("gobby.cli.install._is_grok_cli_installed", return_value=False),
            patch("gobby.cli.install._is_agy_cli_installed", return_value=False),
            patch("gobby.cli.install._is_qwen_cli_installed", return_value=False),
            patch("gobby.cli.install._is_codex_cli_installed", return_value=False),
            patch("gobby.cli.install._is_droid_cli_installed", return_value=False),
            patch("gobby.cli.install.install_claude", return_value=claude_result),
            patch("gobby.cli.install.prepare_install_state", return_value=empty_install_state()),
            patch("gobby.cli.install._run_git_hooks_install") as mock_hooks,
            patch(
                "gobby.cli.install._run_embedding_install", return_value="lmstudio"
            ) as mock_embedding,
            patch("gobby.cli.install._run_qdrant_install") as mock_qdrant,
            patch("gobby.cli.install._run_falkordb_install") as mock_falkordb,
        ):
            result = runner.invoke(install, [], catch_exceptions=False)

        assert result.exit_code == 0
        assert "Gobby Hooks Installation" in result.output
        assert (
            "Components to configure: claude, postgres, qdrant, falkordb, git-hooks"
            in result.output
        )
        mock_hooks.assert_called_once()
        mock_embedding.assert_called_once()
        mock_qdrant.assert_called_once()
        mock_falkordb.assert_called_once()

    def test_install_default_includes_agy_when_detected(
        self,
        runner: CliRunner,
    ) -> None:
        """Default install includes AGY when the agy binary is present."""
        agy_result = {
            "success": True,
            "hooks_installed": ["PreInvocation"],
            "mcp_configured": True,
        }
        with (
            patch("gobby.cli.install.run_daemon_setup"),
            patch(
                "gobby.cli.install._ensure_daemon_config",
                return_value={"created": False, "path": "/fake"},
            ),
            patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install")),
            patch("gobby.cli.install._is_claude_code_installed", return_value=False),
            patch("gobby.cli.install._is_grok_cli_installed", return_value=False),
            patch("gobby.cli.install._is_agy_cli_installed", return_value=True),
            patch("gobby.cli.install._is_qwen_cli_installed", return_value=False),
            patch("gobby.cli.install._is_codex_cli_installed", return_value=False),
            patch("gobby.cli.install._is_droid_cli_installed", return_value=False),
            patch("gobby.cli.install.install_agy", return_value=agy_result) as mock_agy,
            patch("gobby.cli.install.prepare_install_state", return_value=empty_install_state()),
            patch("gobby.cli.install._run_git_hooks_install") as mock_hooks,
            patch("gobby.cli.install._run_embedding_install", return_value="none"),
            patch("gobby.cli.install._maybe_start_daemon_after_install"),
        ):
            result = runner.invoke(install, [], catch_exceptions=False)

        assert result.exit_code == 0
        assert "Components to configure: agy, postgres, qdrant, falkordb" in result.output
        assert "AGY CLI" in result.output
        mock_agy.assert_called_once()
        mock_hooks.assert_not_called()

    def test_install_rejects_removed_no_ext_services(self, runner: CliRunner) -> None:
        result = runner.invoke(install, ["--all", "--no-ext-services"])

        assert result.exit_code == 2
        assert "No such option '--no-ext-services'" in result.output

    def test_repeat_noninteractive_install_preserves_optional_sections(
        self, runner: CliRunner
    ) -> None:
        """Configured sections perform no setup work when a repeat install keeps them."""
        with (
            patch("gobby.cli.install.run_daemon_setup"),
            patch(
                "gobby.cli.install._ensure_daemon_config",
                return_value={"created": False, "path": "/fake"},
            ),
            patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install")),
            patch.multiple(
                "gobby.cli.install",
                _is_claude_code_installed=MagicMock(return_value=True),
                _is_grok_cli_installed=MagicMock(return_value=False),
                _is_agy_cli_installed=MagicMock(return_value=False),
                _is_qwen_cli_installed=MagicMock(return_value=False),
                _is_codex_cli_installed=MagicMock(return_value=False),
                _is_droid_cli_installed=MagicMock(return_value=False),
            ),
            patch(
                "gobby.cli.install.install_claude",
                return_value={
                    "success": True,
                    "hooks_installed": [],
                    "mcp_configured": True,
                },
            ),
            patch(
                "gobby.cli.install.prepare_install_state",
                return_value=_configured_install_state(),
            ),
            patch("gobby.cli.install._run_git_hooks_install"),
            patch("gobby.cli.install._run_embedding_install") as embedding,
            patch("gobby.cli.install._run_voice_install") as voice,
            patch("gobby.cli.install._run_qdrant_install") as qdrant,
            patch("gobby.cli.install._run_falkordb_install") as falkordb,
            patch("gobby.cli.install._maybe_start_daemon_after_install"),
        ):
            result = runner.invoke(
                install,
                ["--all", "--no-interactive"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert "Change embedding provider/model/endpoint?" not in result.output
        assert "Change voice setting?" not in result.output
        embedding.assert_not_called()
        voice.assert_not_called()
        qdrant.assert_called_once()
        falkordb.assert_called_once()

    def test_repeat_interactive_install_prompts_for_each_optional_section(
        self, runner: CliRunner
    ) -> None:
        with (
            patch("gobby.cli.install.run_daemon_setup"),
            patch(
                "gobby.cli.install._ensure_daemon_config",
                return_value={"created": False, "path": "/fake"},
            ),
            patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install")),
            patch.multiple(
                "gobby.cli.install",
                _is_claude_code_installed=MagicMock(return_value=True),
                _is_grok_cli_installed=MagicMock(return_value=False),
                _is_agy_cli_installed=MagicMock(return_value=False),
                _is_qwen_cli_installed=MagicMock(return_value=False),
                _is_codex_cli_installed=MagicMock(return_value=False),
                _is_droid_cli_installed=MagicMock(return_value=False),
            ),
            patch(
                "gobby.cli.install.install_claude",
                return_value={
                    "success": True,
                    "hooks_installed": [],
                    "mcp_configured": True,
                },
            ),
            patch(
                "gobby.cli.install.prepare_install_state",
                return_value=_configured_install_state(),
            ),
            patch("gobby.cli.install._run_git_hooks_install"),
            patch("gobby.cli.install._run_embedding_install") as embedding,
            patch("gobby.cli.install._run_voice_install") as voice,
            patch("gobby.cli.install._run_qdrant_install") as qdrant,
            patch("gobby.cli.install._run_falkordb_install") as falkordb,
            patch("gobby.cli.install._maybe_start_daemon_after_install"),
        ):
            result = runner.invoke(
                install,
                ["--all"],
                input="n\nn\nn\nn\n",
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        for prompt in ("Change embedding provider/model/endpoint?", "Change voice setting?"):
            assert prompt in result.output
        embedding.assert_not_called()
        voice.assert_not_called()
        qdrant.assert_called_once()
        falkordb.assert_called_once()

    def test_install_help_omits_removed_no_ext_services(self, runner: CliRunner) -> None:
        result = runner.invoke(install, ["--help"])

        assert result.exit_code == 0
        assert "--no-ext-services" not in result.output

    def test_install_all_no_clis_detected(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        with (
            patch("gobby.cli.install.run_daemon_setup"),
            patch(
                "gobby.cli.install._ensure_daemon_config",
                return_value={"created": False, "path": "/fake"},
            ),
            patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/src/install")),
            patch("gobby.cli.install._is_claude_code_installed", return_value=False),
            patch("gobby.cli.install._is_grok_cli_installed", return_value=False),
            patch("gobby.cli.install._is_agy_cli_installed", return_value=False),
            patch("gobby.cli.install._is_qwen_cli_installed", return_value=False),
            patch("gobby.cli.install._is_codex_cli_installed", return_value=False),
            patch("gobby.cli.install._is_droid_cli_installed", return_value=False),
        ):
            result = runner.invoke(install, ["-C", str(tmp_path)], catch_exceptions=False)
        assert result.exit_code == 1
        assert "No supported" in result.output

    @patch("gobby.cli.install.run_daemon_setup")
    @patch(
        "gobby.cli.install._ensure_daemon_config", return_value={"created": False, "path": "/fake"}
    )
    @patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.cli.install._is_codex_cli_installed", return_value=True)
    @patch("gobby.cli.install.install_codex")
    def test_install_codex_success(
        self,
        mock_install: MagicMock,
        _codex_installed: MagicMock,
        _install_dir: MagicMock,
        _config: MagicMock,
        _setup: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_install.return_value = {
            "success": True,
            "hooks_installed": [],
            "files_installed": ["/path/to/file"],
            "config_updated": True,
            "workflows_installed": ["wf1"],
            "commands_installed": ["cmd1"],
            "plugins_installed": ["plugin1"],
            "mcp_configured": True,
        }
        result = runner.invoke(install, ["--codex"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Codex" in result.output


# ---------------------------------------------------------------------------
# uninstall command
# ---------------------------------------------------------------------------
class TestUninstallCommand:
    @patch("gobby.cli.install.uninstall_claude")
    def test_uninstall_claude(self, mock_uninstall: MagicMock, runner: CliRunner) -> None:
        mock_uninstall.return_value = {
            "success": True,
            "hooks_removed": ["hook1"],
            "files_removed": ["file1"],
        }
        result = runner.invoke(uninstall, ["--claude", "--yes"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Claude Code" in result.output

    @patch("gobby.cli.install.uninstall_claude")
    def test_uninstall_claude_failure(self, mock_uninstall: MagicMock, runner: CliRunner) -> None:
        mock_uninstall.return_value = {
            "success": False,
            "error": "Permission denied",
        }
        result = runner.invoke(uninstall, ["--claude", "--yes"], catch_exceptions=False)
        assert result.exit_code == 1
        assert "Permission denied" in result.output

    @patch("gobby.cli.install.uninstall_codex")
    def test_uninstall_codex(self, mock_uninstall: MagicMock, runner: CliRunner) -> None:
        mock_uninstall.return_value = {
            "success": True,
            "hooks_removed": ["codex_hook"],
            "files_removed": ["codex_file"],
            "config_updated": True,
        }
        result = runner.invoke(uninstall, ["--codex", "--yes"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Codex" in result.output

    def test_uninstall_rejects_falkordb_target(self, runner: CliRunner) -> None:
        result = runner.invoke(uninstall, ["--falkordb", "--yes"])

        assert result.exit_code == 2
        assert "No such option '--falkordb'" in result.output

    def test_uninstall_all_nothing_found(self, runner: CliRunner, tmp_path: Path) -> None:
        """When --all is used but no CLI hooks are detected."""
        # Use a clean tmp_path as home so no settings.json files are found
        with patch("gobby.cli.install.Path.home", return_value=tmp_path):
            result = runner.invoke(uninstall, ["--all", "--yes"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "No Gobby hooks found" in result.output
