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
        neo4j_result = {
            "success": True,
            "neo4j_url": "http://localhost:7474",
            "bolt_url": "bolt://localhost:7687",
            "compose_file": "/path/to/compose.yml",
        }
        with (
            patch("gobby.cli.install.install_qdrant", return_value=qdrant_result),
            patch("gobby.cli.install.install_neo4j", return_value=neo4j_result),
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

    @patch("gobby.cli.install.run_daemon_setup")
    @patch(
        "gobby.cli.install._ensure_daemon_config", return_value={"created": False, "path": "/fake"}
    )
    @patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.cli.install._run_embedding_install", return_value="lmstudio")
    @patch("gobby.cli.install.install_embedding")
    @patch("gobby.cli.install.install_neo4j")
    @patch("gobby.cli.install.install_qdrant")
    @patch("gobby.cli.install.install_claude")
    def test_install_neo4j_default(
        self,
        mock_claude: MagicMock,
        mock_qdrant: MagicMock,
        mock_neo4j: MagicMock,
        _embed: MagicMock,
        _embed_wizard: MagicMock,
        _install_dir: MagicMock,
        _config: MagicMock,
        _setup: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Neo4j installs by default (no --neo4j flag needed)."""
        mock_claude.return_value = {
            "success": True,
            "hooks_installed": ["PreToolUse"],
            "mcp_configured": True,
        }
        mock_qdrant.return_value = {"success": True, "qdrant_url": "http://localhost:6333"}
        mock_neo4j.return_value = {
            "success": True,
            "neo4j_url": "http://localhost:7474",
            "bolt_url": "bolt://localhost:7687",
            "compose_file": "/path/to/compose.yml",
        }
        result = runner.invoke(install, ["--claude"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Neo4j" in result.output
        mock_neo4j.assert_called_once()

    @patch("gobby.cli.install.run_daemon_setup")
    @patch(
        "gobby.cli.install._ensure_daemon_config", return_value={"created": False, "path": "/fake"}
    )
    @patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.cli.install.install_neo4j")
    @patch("gobby.cli.install.install_qdrant")
    @patch("gobby.cli.install.install_claude")
    def test_install_no_ext_services(
        self,
        mock_claude: MagicMock,
        mock_qdrant: MagicMock,
        mock_neo4j: MagicMock,
        _install_dir: MagicMock,
        _config: MagicMock,
        _setup: MagicMock,
        runner: CliRunner,
    ) -> None:
        """--no-ext-services skips both Qdrant and Neo4j."""
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
        mock_neo4j.assert_not_called()
        assert mock_neo4j.call_count == 0
        assert not mock_neo4j.called

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
