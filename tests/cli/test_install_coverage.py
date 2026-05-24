"""Tests for cli/install.py — targeting uncovered lines."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.install import (
    _echo_install_details,
    _echo_uninstall_details,
    _is_claude_code_installed,
    _is_codex_cli_installed,
    _is_gemini_cli_installed,
    install,
    uninstall,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


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

    @patch("gobby.cli._detectors.shutil.which", return_value="/usr/bin/gemini")
    def test_gemini_installed(self, _mock_which: MagicMock) -> None:
        assert _is_gemini_cli_installed() is True

    @patch("gobby.cli._detectors.shutil.which", return_value=None)
    def test_gemini_not_installed(self, _mock_which: MagicMock) -> None:
        assert _is_gemini_cli_installed() is False

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
        """Mock Docker service and local embeddings installers — tests here focus on CLI-specific hooks."""
        qdrant_result = {"success": True, "qdrant_url": "http://localhost:6333"}
        falkordb_result = {
            "success": True,
            "browser_url": "http://localhost:13000",
            "password_source": "reused",
            "password": None,
        }
        with (
            patch("gobby.cli.install.install_qdrant", return_value=qdrant_result),
            patch("gobby.cli.install.install_falkordb", return_value=falkordb_result),
        ):
            yield

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
    @patch("gobby.cli.install.install_gemini")
    def test_install_gemini_only(
        self,
        mock_install: MagicMock,
        _install_dir: MagicMock,
        _config: MagicMock,
        _setup: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_install.return_value = {
            "success": True,
            "hooks_installed": ["hook1"],
            "mcp_configured": True,
        }
        result = runner.invoke(install, ["--gemini"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Gemini CLI" in result.output

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
        }
        result = runner.invoke(install, ["--hooks"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "pre-commit" in result.output

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
        """Default install still configures embeddings and external services."""
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
            patch("gobby.cli.install._is_gemini_cli_installed", return_value=False),
            patch("gobby.cli.install._is_grok_cli_installed", return_value=False),
            patch("gobby.cli.install._is_agy_cli_installed", return_value=False),
            patch("gobby.cli.install._is_qwen_cli_installed", return_value=False),
            patch("gobby.cli.install._is_codex_cli_installed", return_value=False),
            patch("gobby.cli.install._is_droid_cli_installed", return_value=False),
            patch("gobby.cli.install.install_claude", return_value=claude_result),
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
        assert "Components to configure: claude, git-hooks" in result.output
        mock_hooks.assert_called_once()
        mock_embedding.assert_called_once()
        mock_qdrant.assert_called_once()
        mock_falkordb.assert_called_once()

    def test_install_all_no_ext_services_runs_embedding_only(
        self,
        runner: CliRunner,
    ) -> None:
        """--all --no-ext-services still runs embedding setup and skips Docker services."""
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
            patch("gobby.cli.install._is_gemini_cli_installed", return_value=False),
            patch("gobby.cli.install._is_grok_cli_installed", return_value=False),
            patch("gobby.cli.install._is_agy_cli_installed", return_value=False),
            patch("gobby.cli.install._is_qwen_cli_installed", return_value=False),
            patch("gobby.cli.install._is_codex_cli_installed", return_value=False),
            patch("gobby.cli.install._is_droid_cli_installed", return_value=False),
            patch("gobby.cli.install.install_claude", return_value=claude_result),
            patch("gobby.cli.install._run_git_hooks_install") as mock_hooks,
            patch(
                "gobby.cli.install._run_embedding_install", return_value="lmstudio"
            ) as mock_embedding,
            patch("gobby.cli.install._run_qdrant_install") as mock_qdrant,
            patch("gobby.cli.install._run_falkordb_install") as mock_falkordb,
        ):
            result = runner.invoke(
                install,
                ["--all", "--no-ext-services"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert "Gobby Hooks Installation" in result.output
        assert "Components to configure: claude, git-hooks" in result.output
        mock_hooks.assert_called_once()
        mock_embedding.assert_called_once()
        mock_qdrant.assert_not_called()
        mock_falkordb.assert_not_called()

    @patch("gobby.cli.install.run_daemon_setup")
    @patch(
        "gobby.cli.install._ensure_daemon_config", return_value={"created": False, "path": "/fake"}
    )
    @patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.cli.install.install_falkordb")
    @patch("gobby.cli.install.install_qdrant")
    @patch("gobby.cli.install.install_claude")
    def test_install_no_ext_services(
        self,
        mock_claude: MagicMock,
        mock_qdrant: MagicMock,
        mock_falkordb: MagicMock,
        _install_dir: MagicMock,
        _config: MagicMock,
        _setup: MagicMock,
        runner: CliRunner,
    ) -> None:
        """--no-ext-services skips both Qdrant and FalkorDB."""
        mock_claude.return_value = {
            "success": True,
            "hooks_installed": ["PreToolUse"],
            "mcp_configured": True,
        }
        result = runner.invoke(install, ["--claude", "--no-ext-services"], catch_exceptions=False)
        assert result.exit_code == 0
        mock_qdrant.assert_not_called()
        assert mock_qdrant.call_count == 0
        assert not mock_qdrant.called
        mock_falkordb.assert_not_called()
        assert mock_falkordb.call_count == 0
        assert not mock_falkordb.called

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
            patch("gobby.cli.install._is_gemini_cli_installed", return_value=False),
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

    @patch("gobby.cli.install.uninstall_gemini")
    def test_uninstall_gemini(self, mock_uninstall: MagicMock, runner: CliRunner) -> None:
        mock_uninstall.return_value = {
            "success": True,
            "hooks_removed": ["hook1"],
            "files_removed": [],
        }
        result = runner.invoke(uninstall, ["--gemini", "--yes"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Gemini" in result.output

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

    @patch("gobby.cli.install.uninstall_falkordb")
    def test_uninstall_falkordb(self, mock_uninstall: MagicMock, runner: CliRunner) -> None:
        mock_uninstall.return_value = {
            "success": True,
            "data_removed": True,
        }
        result = runner.invoke(
            uninstall,
            ["--falkordb", "--volumes", "--yes"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "FalkorDB" in result.output

    def test_uninstall_all_nothing_found(self, runner: CliRunner, tmp_path: Path) -> None:
        """When --all is used but no CLI hooks are detected."""
        # Use a clean tmp_path as home so no settings.json files are found
        with patch("gobby.cli.install.Path.home", return_value=tmp_path):
            result = runner.invoke(uninstall, ["--all", "--yes"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "No Gobby hooks found" in result.output
