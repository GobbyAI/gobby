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
    _is_copilot_cli_installed,
    _is_cursor_installed,
    _is_gemini_cli_installed,
    _is_windsurf_installed,
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
            # Just check it doesn't raise
            _echo_install_details(result)

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

    def test_echo_install_details_mcp_already(self) -> None:
        result: dict[str, Any] = {
            "hooks_installed": [],
            "mcp_already_configured": True,
        }
        _echo_install_details(result, mcp_config_path="~/.claude.json")

    def test_echo_uninstall_details_with_hooks(self) -> None:
        result: dict[str, Any] = {
            "hooks_removed": ["hook1", "hook2"],
            "files_removed": ["file1"],
        }
        _echo_uninstall_details(result)

    def test_echo_uninstall_details_empty(self) -> None:
        result: dict[str, Any] = {
            "hooks_removed": [],
            "files_removed": [],
        }
        _echo_uninstall_details(result)


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------
class TestDetectionHelpers:
    @patch("gobby.cli.install.shutil.which", return_value="/usr/bin/claude")
    def test_claude_installed(self, _mock_which: MagicMock) -> None:
        assert _is_claude_code_installed() is True

    @patch("gobby.cli.install.shutil.which", return_value=None)
    def test_claude_not_installed(self, _mock_which: MagicMock) -> None:
        assert _is_claude_code_installed() is False

    @patch("gobby.cli.install.shutil.which", return_value="/usr/bin/gemini")
    def test_gemini_installed(self, _mock_which: MagicMock) -> None:
        assert _is_gemini_cli_installed() is True

    @patch("gobby.cli.install.shutil.which", return_value=None)
    def test_gemini_not_installed(self, _mock_which: MagicMock) -> None:
        assert _is_gemini_cli_installed() is False

    @patch("gobby.cli.install.shutil.which", return_value="/usr/bin/codex")
    def test_codex_installed(self, _mock_which: MagicMock) -> None:
        assert _is_codex_cli_installed() is True

    @patch("gobby.cli.install.shutil.which", return_value=None)
    def test_codex_not_installed(self, _mock_which: MagicMock) -> None:
        assert _is_codex_cli_installed() is False

    @patch("gobby.cli.install.shutil.which", return_value=None)
    def test_copilot_no_gh(self, _mock_which: MagicMock) -> None:
        assert _is_copilot_cli_installed() is False

    @patch("gobby.cli.install.shutil.which", return_value="/usr/bin/gh")
    def test_copilot_with_gh(self, _mock_which: MagicMock) -> None:
        assert _is_copilot_cli_installed() is True

    @patch("gobby.cli.install.sys.platform", "darwin")
    def test_cursor_darwin(self) -> None:
        with patch("gobby.cli.install.Path.exists", return_value=True):
            assert _is_cursor_installed() is True

    @patch("gobby.cli.install.sys.platform", "darwin")
    def test_cursor_darwin_not_found(self) -> None:
        with patch("gobby.cli.install.Path.exists", return_value=False):
            assert _is_cursor_installed() is False

    @patch("gobby.cli.install.sys.platform", "darwin")
    def test_windsurf_darwin(self) -> None:
        with patch("gobby.cli.install.Path.exists", return_value=True):
            assert _is_windsurf_installed() is True

    @patch("gobby.cli.install.sys.platform", "darwin")
    def test_windsurf_darwin_not_found(self) -> None:
        with patch("gobby.cli.install.Path.exists", return_value=False):
            assert _is_windsurf_installed() is False


# ---------------------------------------------------------------------------
# install command — --claude only
# ---------------------------------------------------------------------------
class TestInstallCommand:
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
    @patch("gobby.cli.install.install_cursor")
    def test_install_cursor(
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
        }
        result = runner.invoke(install, ["--cursor"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Cursor" in result.output

    @patch("gobby.cli.install.run_daemon_setup")
    @patch(
        "gobby.cli.install._ensure_daemon_config", return_value={"created": False, "path": "/fake"}
    )
    @patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.cli.install.install_windsurf")
    def test_install_windsurf(
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
        }
        result = runner.invoke(install, ["--windsurf"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Windsurf" in result.output

    @patch("gobby.cli.install.run_daemon_setup")
    @patch(
        "gobby.cli.install._ensure_daemon_config", return_value={"created": False, "path": "/fake"}
    )
    @patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.cli.install.install_copilot")
    def test_install_copilot(
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
        }
        result = runner.invoke(install, ["--copilot"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Copilot" in result.output

    @patch("gobby.cli.install.run_daemon_setup")
    @patch(
        "gobby.cli.install._ensure_daemon_config", return_value={"created": False, "path": "/fake"}
    )
    @patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.cli.install.install_copilot")
    def test_install_copilot_skipped(
        self,
        mock_install: MagicMock,
        _install_dir: MagicMock,
        _config: MagicMock,
        _setup: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_install.return_value = {
            "success": True,
            "skipped": True,
            "skip_reason": "Not configured",
            "hooks_installed": [],
        }
        result = runner.invoke(install, ["--copilot"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Skipped" in result.output

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

    @patch("gobby.cli.install.run_daemon_setup")
    @patch(
        "gobby.cli.install._ensure_daemon_config", return_value={"created": False, "path": "/fake"}
    )
    @patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.cli.install.install_neo4j")
    def test_install_neo4j(
        self,
        mock_install: MagicMock,
        _install_dir: MagicMock,
        _config: MagicMock,
        _setup: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_install.return_value = {
            "success": True,
            "neo4j_url": "http://localhost:7474",
            "bolt_url": "bolt://localhost:7687",
            "compose_file": "/path/to/compose.yml",
        }
        result = runner.invoke(install, ["--neo4j"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Neo4j" in result.output

    @patch("gobby.cli.install.run_daemon_setup")
    @patch(
        "gobby.cli.install._ensure_daemon_config", return_value={"created": False, "path": "/fake"}
    )
    @patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.cli.install.install_antigravity")
    def test_install_antigravity(
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
        }
        result = runner.invoke(install, ["--antigravity"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Antigravity" in result.output

    @patch("gobby.cli.install.run_daemon_setup")
    @patch(
        "gobby.cli.install._ensure_daemon_config", return_value={"created": False, "path": "/fake"}
    )
    @patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/src/install"))
    @patch("gobby.cli.install._is_claude_code_installed", return_value=False)
    @patch("gobby.cli.install._is_gemini_cli_installed", return_value=False)
    @patch("gobby.cli.install._is_codex_cli_installed", return_value=False)
    @patch("gobby.cli.install._is_cursor_installed", return_value=False)
    @patch("gobby.cli.install._is_windsurf_installed", return_value=False)
    @patch("gobby.cli.install._is_copilot_cli_installed", return_value=False)
    def test_install_all_no_clis_detected(
        self,
        *mocks: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        result = runner.invoke(install, ["-C", str(tmp_path)], catch_exceptions=False)
        assert result.exit_code == 1
        assert "No supported" in result.output

    @patch("gobby.cli.install.run_daemon_setup")
    @patch(
        "gobby.cli.install._ensure_daemon_config", return_value={"created": False, "path": "/fake"}
    )
    @patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.cli.install._is_codex_cli_installed", return_value=True)
    @patch("gobby.cli.install.install_codex_notify")
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

    @patch("gobby.cli.install.run_daemon_setup")
    @patch(
        "gobby.cli.install._ensure_daemon_config", return_value={"created": False, "path": "/fake"}
    )
    @patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.cli.install._is_codex_cli_installed", return_value=False)
    def test_install_codex_not_detected(
        self,
        _codex: MagicMock,
        _install_dir: MagicMock,
        _config: MagicMock,
        _setup: MagicMock,
        runner: CliRunner,
    ) -> None:
        result = runner.invoke(install, ["--codex"], catch_exceptions=False)
        assert result.exit_code == 1
        assert "not detected" in result.output.lower()


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

    @patch("gobby.cli.install.uninstall_codex_notify")
    def test_uninstall_codex(self, mock_uninstall: MagicMock, runner: CliRunner) -> None:
        mock_uninstall.return_value = {
            "success": True,
            "files_removed": ["codex_file"],
            "config_updated": True,
        }
        result = runner.invoke(uninstall, ["--codex", "--yes"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Codex" in result.output

    @patch("gobby.cli.install.uninstall_cursor")
    def test_uninstall_cursor(self, mock_uninstall: MagicMock, runner: CliRunner) -> None:
        mock_uninstall.return_value = {
            "success": True,
            "hooks_removed": [],
            "files_removed": [],
        }
        result = runner.invoke(uninstall, ["--cursor", "--yes"], catch_exceptions=False)
        assert result.exit_code == 0

    @patch("gobby.cli.install.uninstall_windsurf")
    def test_uninstall_windsurf(self, mock_uninstall: MagicMock, runner: CliRunner) -> None:
        mock_uninstall.return_value = {
            "success": True,
            "hooks_removed": ["hook1"],
            "files_removed": [],
        }
        result = runner.invoke(uninstall, ["--windsurf", "--yes"], catch_exceptions=False)
        assert result.exit_code == 0

    @patch("gobby.cli.install.uninstall_copilot")
    def test_uninstall_copilot(self, mock_uninstall: MagicMock, runner: CliRunner) -> None:
        mock_uninstall.return_value = {
            "success": True,
            "hooks_removed": [],
            "files_removed": [],
        }
        result = runner.invoke(uninstall, ["--copilot", "--yes"], catch_exceptions=False)
        assert result.exit_code == 0

    @patch("gobby.cli.install.uninstall_neo4j")
    def test_uninstall_neo4j(self, mock_uninstall: MagicMock, runner: CliRunner) -> None:
        mock_uninstall.return_value = {
            "success": True,
            "already_uninstalled": False,
            "volumes_removed": True,
        }
        result = runner.invoke(uninstall, ["--neo4j", "--volumes", "--yes"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Neo4j" in result.output

    def test_uninstall_all_nothing_found(self, runner: CliRunner, tmp_path: Path) -> None:
        """When --all is used but no CLI hooks are detected."""
        # Use a clean tmp_path as home so no settings.json files are found
        with patch("gobby.cli.install.Path.home", return_value=tmp_path):
            result = runner.invoke(uninstall, ["--all", "--yes"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "No Gobby hooks found" in result.output
