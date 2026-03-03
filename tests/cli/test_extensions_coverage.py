"""Tests for cli/extensions.py -- targeting uncovered lines.

Covers: hooks run, hooks status, hooks disable, hooks enable, _indent helper.
Lines targeted: 159-256, 267-366, 376-437
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.extensions import _indent, hooks

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# =============================================================================
# _indent helper
# =============================================================================


class TestIndentHelper:
    def test_indent_basic(self) -> None:
        assert _indent("a\nb", 4) == "    a\n    b"

    def test_indent_single_line(self) -> None:
        assert _indent("hello", 2) == "  hello"

    def test_indent_strips_outer_whitespace(self) -> None:
        assert _indent("  a\n  b  \n", 3) == "   a\n     b"


# =============================================================================
# hooks run  (VerificationRunner imported inside the function)
# =============================================================================


class TestHooksRun:
    """Tests for hooks run command (lines 137-256)."""

    @patch("gobby.hooks.verification_runner.VerificationRunner")
    def test_hooks_run_dry_run_with_commands(
        self, mock_vr_cls: MagicMock, runner: CliRunner
    ) -> None:
        mock_runner = MagicMock()
        stage_config = MagicMock()
        stage_config.run = ["lint", "format"]
        mock_runner.get_stage_config.return_value = stage_config
        mock_runner.verification_config = MagicMock()
        mock_runner.verification_config.get_command.side_effect = lambda n: f"cmd-{n}"
        mock_vr_cls.from_project.return_value = mock_runner

        result = runner.invoke(hooks, ["run", "pre-commit", "--dry-run"])
        assert result.exit_code == 0
        assert "Would run for 'pre-commit'" in result.output
        assert "lint: cmd-lint" in result.output
        assert "format: cmd-format" in result.output

    @patch("gobby.hooks.verification_runner.VerificationRunner")
    def test_hooks_run_dry_run_no_stage_config(
        self, mock_vr_cls: MagicMock, runner: CliRunner
    ) -> None:
        mock_runner = MagicMock()
        mock_runner.get_stage_config.return_value = None
        mock_vr_cls.from_project.return_value = mock_runner

        result = runner.invoke(hooks, ["run", "pre-push", "--dry-run"])
        assert result.exit_code == 0
        assert "No commands configured for 'pre-push'" in result.output

    @patch("gobby.hooks.verification_runner.VerificationRunner")
    def test_hooks_run_dry_run_empty_run_list(
        self, mock_vr_cls: MagicMock, runner: CliRunner
    ) -> None:
        mock_runner = MagicMock()
        stage_config = MagicMock()
        stage_config.run = []
        mock_runner.get_stage_config.return_value = stage_config
        mock_vr_cls.from_project.return_value = mock_runner

        result = runner.invoke(hooks, ["run", "pre-merge", "--dry-run"])
        assert result.exit_code == 0
        assert "No commands configured" in result.output

    @patch("gobby.hooks.verification_runner.VerificationRunner")
    def test_hooks_run_dry_run_no_verification_config(
        self, mock_vr_cls: MagicMock, runner: CliRunner
    ) -> None:
        mock_runner = MagicMock()
        stage_config = MagicMock()
        stage_config.run = ["lint"]
        mock_runner.get_stage_config.return_value = stage_config
        mock_runner.verification_config = None
        mock_vr_cls.from_project.return_value = mock_runner

        result = runner.invoke(hooks, ["run", "pre-commit", "--dry-run"])
        assert result.exit_code == 0
        assert "No verification commands defined" in result.output

    @patch("gobby.hooks.verification_runner.VerificationRunner")
    def test_hooks_run_dry_run_undefined_command(
        self, mock_vr_cls: MagicMock, runner: CliRunner
    ) -> None:
        mock_runner = MagicMock()
        stage_config = MagicMock()
        stage_config.run = ["missing"]
        mock_runner.get_stage_config.return_value = stage_config
        mock_runner.verification_config = MagicMock()
        mock_runner.verification_config.get_command.return_value = None
        mock_vr_cls.from_project.return_value = mock_runner

        result = runner.invoke(hooks, ["run", "pre-commit", "--dry-run"])
        assert result.exit_code == 0
        assert "missing: (not defined)" in result.output

    @patch("gobby.hooks.verification_runner.VerificationRunner")
    def test_hooks_run_success(self, mock_vr_cls: MagicMock, runner: CliRunner) -> None:
        mock_runner = MagicMock()
        cmd_result = MagicMock()
        cmd_result.success = True
        cmd_result.name = "lint"
        cmd_result.duration_ms = 42
        cmd_result.skipped = False
        cmd_result.stdout = ""
        cmd_result.stderr = ""
        cmd_result.error = None

        stage_result = MagicMock()
        stage_result.success = True
        stage_result.skipped = False
        stage_result.results = [cmd_result]
        stage_result.passed_count = 1
        stage_result.failed_count = 0
        stage_result.skipped_count = 0

        mock_runner.run_stage.return_value = stage_result
        mock_vr_cls.from_project.return_value = mock_runner

        result = runner.invoke(hooks, ["run", "pre-commit"])
        assert result.exit_code == 0
        assert "lint" in result.output
        assert "Passed: 1" in result.output

    @patch("gobby.hooks.verification_runner.VerificationRunner")
    def test_hooks_run_failure(self, mock_vr_cls: MagicMock, runner: CliRunner) -> None:
        mock_runner = MagicMock()
        cmd_result = MagicMock()
        cmd_result.success = False
        cmd_result.name = "lint"
        cmd_result.duration_ms = 100
        cmd_result.skipped = False
        cmd_result.stdout = ""
        cmd_result.stderr = "Error on line 5\nMore details"
        cmd_result.error = "exit code 1"

        stage_result = MagicMock()
        stage_result.success = False
        stage_result.skipped = False
        stage_result.results = [cmd_result]
        stage_result.passed_count = 0
        stage_result.failed_count = 1
        stage_result.skipped_count = 0

        mock_runner.run_stage.return_value = stage_result
        mock_vr_cls.from_project.return_value = mock_runner

        result = runner.invoke(hooks, ["run", "pre-commit"])
        assert result.exit_code == 1
        assert "Error: exit code 1" in result.output
        assert "Error on line 5" in result.output

    @patch("gobby.hooks.verification_runner.VerificationRunner")
    def test_hooks_run_failure_verbose(self, mock_vr_cls: MagicMock, runner: CliRunner) -> None:
        mock_runner = MagicMock()
        cmd_result = MagicMock()
        cmd_result.success = False
        cmd_result.name = "lint"
        cmd_result.duration_ms = 100
        cmd_result.skipped = False
        cmd_result.stdout = "stdout output"
        cmd_result.stderr = "stderr detail"
        cmd_result.error = None

        stage_result = MagicMock()
        stage_result.success = False
        stage_result.skipped = False
        stage_result.results = [cmd_result]
        stage_result.passed_count = 0
        stage_result.failed_count = 1
        stage_result.skipped_count = 0

        mock_runner.run_stage.return_value = stage_result
        mock_vr_cls.from_project.return_value = mock_runner

        result = runner.invoke(hooks, ["run", "pre-commit", "--verbose"])
        assert result.exit_code == 1
        assert "stderr:" in result.output
        assert "stderr detail" in result.output
        assert "stdout:" in result.output
        assert "stdout output" in result.output

    @patch("gobby.hooks.verification_runner.VerificationRunner")
    def test_hooks_run_skipped_stage(self, mock_vr_cls: MagicMock, runner: CliRunner) -> None:
        mock_runner = MagicMock()
        stage_result = MagicMock()
        stage_result.success = True
        stage_result.skipped = True
        stage_result.skip_reason = "No changes to check"
        stage_result.results = []

        mock_runner.run_stage.return_value = stage_result
        mock_vr_cls.from_project.return_value = mock_runner

        result = runner.invoke(hooks, ["run", "pre-commit", "--verbose"])
        assert result.exit_code == 0
        assert "Skipped: No changes to check" in result.output

    @patch("gobby.hooks.verification_runner.VerificationRunner")
    def test_hooks_run_skipped_command(self, mock_vr_cls: MagicMock, runner: CliRunner) -> None:
        mock_runner = MagicMock()
        cmd_result = MagicMock()
        cmd_result.success = True
        cmd_result.skipped = True
        cmd_result.skip_reason = "no files changed"
        cmd_result.name = "lint"
        cmd_result.stdout = ""

        stage_result = MagicMock()
        stage_result.success = True
        stage_result.skipped = False
        stage_result.results = [cmd_result]
        stage_result.passed_count = 0
        stage_result.failed_count = 0
        stage_result.skipped_count = 1

        mock_runner.run_stage.return_value = stage_result
        mock_vr_cls.from_project.return_value = mock_runner

        result = runner.invoke(hooks, ["run", "pre-commit"])
        assert result.exit_code == 0
        assert "lint: skipped" in result.output
        assert "no files changed" in result.output

    @patch("gobby.hooks.verification_runner.VerificationRunner")
    def test_hooks_run_json_output(self, mock_vr_cls: MagicMock, runner: CliRunner) -> None:
        mock_runner = MagicMock()
        cmd_result = MagicMock()
        cmd_result.name = "lint"
        cmd_result.command = "ruff check src/"
        cmd_result.success = True
        cmd_result.exit_code = 0
        cmd_result.duration_ms = 50
        cmd_result.skipped = False
        cmd_result.skip_reason = None
        cmd_result.error = None
        cmd_result.stdout = None
        cmd_result.stderr = None

        stage_result = MagicMock()
        stage_result.stage = "pre-commit"
        stage_result.success = True
        stage_result.skipped = False
        stage_result.skip_reason = None
        stage_result.results = [cmd_result]

        mock_runner.run_stage.return_value = stage_result
        mock_vr_cls.from_project.return_value = mock_runner

        result = runner.invoke(hooks, ["run", "pre-commit", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["stage"] == "pre-commit"
        assert data["success"] is True
        assert len(data["results"]) == 1

    @patch("gobby.hooks.verification_runner.VerificationRunner")
    def test_hooks_run_json_verbose_includes_output(
        self, mock_vr_cls: MagicMock, runner: CliRunner
    ) -> None:
        mock_runner = MagicMock()
        cmd_result = MagicMock()
        cmd_result.name = "lint"
        cmd_result.command = "ruff check"
        cmd_result.success = True
        cmd_result.exit_code = 0
        cmd_result.duration_ms = 50
        cmd_result.skipped = False
        cmd_result.skip_reason = None
        cmd_result.error = None
        cmd_result.stdout = "all good"
        cmd_result.stderr = ""

        stage_result = MagicMock()
        stage_result.stage = "pre-commit"
        stage_result.success = True
        stage_result.skipped = False
        stage_result.skip_reason = None
        stage_result.results = [cmd_result]

        mock_runner.run_stage.return_value = stage_result
        mock_vr_cls.from_project.return_value = mock_runner

        result = runner.invoke(hooks, ["run", "pre-commit", "--json", "--verbose"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["results"][0]["stdout"] == "all good"


# =============================================================================
# hooks status (imports from gobby.utils.project_context inside function)
# =============================================================================


class TestHooksStatus:
    """Tests for hooks status command (lines 259-366)."""

    @patch("gobby.utils.project_context.get_hooks_config")
    @patch("gobby.utils.project_context.get_verification_config")
    def test_hooks_status_json(
        self,
        mock_verif: MagicMock,
        mock_hooks: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        mock_verif.return_value = None
        mock_hooks.return_value = None

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("pathlib.Path.cwd", return_value=tmp_path),
        ):
            result = runner.invoke(hooks, ["status", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "global_installed" in data
        assert "hooks_disabled" in data

    @patch("gobby.utils.project_context.get_hooks_config")
    @patch("gobby.utils.project_context.get_verification_config")
    def test_hooks_status_human_readable(
        self,
        mock_verif: MagicMock,
        mock_hooks: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        mock_verif_config = MagicMock()
        mock_verif_config.all_commands.return_value = {"lint": "ruff check src/"}
        mock_verif.return_value = mock_verif_config

        mock_hooks_config = MagicMock()
        stage = MagicMock()
        stage.run = ["lint"]
        stage.enabled = True
        stage.fail_fast = True
        stage.timeout = 60
        mock_hooks_config.pre_commit = stage

        empty_stage = MagicMock()
        empty_stage.run = []
        mock_hooks_config.pre_push = empty_stage
        mock_hooks_config.pre_merge = empty_stage
        mock_hooks.return_value = mock_hooks_config

        # Create dispatcher so global_installed=True
        hooks_dir = tmp_path / ".gobby" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "hook_dispatcher.py").write_text("x")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("pathlib.Path.cwd", return_value=tmp_path),
            patch.dict(os.environ, {}, clear=False),
        ):
            result = runner.invoke(hooks, ["status"])

        assert result.exit_code == 0
        assert "Global Hooks:" in result.output
        assert "Verification Commands:" in result.output
        assert "lint: ruff check src/" in result.output
        assert "Hook Stages:" in result.output

    @patch("gobby.utils.project_context.get_hooks_config")
    @patch("gobby.utils.project_context.get_verification_config")
    def test_hooks_status_no_hooks_config(
        self,
        mock_verif: MagicMock,
        mock_hooks: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        mock_verif.return_value = None
        mock_hooks.return_value = None

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("pathlib.Path.cwd", return_value=tmp_path),
        ):
            result = runner.invoke(hooks, ["status"])

        assert result.exit_code == 0
        assert "(none configured)" in result.output


# =============================================================================
# hooks disable / enable (import Path inside function body)
# =============================================================================


class TestHooksDisableEnable:
    """Tests for hooks disable/enable commands (lines 376-437)."""

    def test_hooks_disable_success(self, runner: CliRunner, tmp_path: Path) -> None:
        project_json = tmp_path / ".gobby" / "project.json"
        project_json.parent.mkdir(parents=True)
        project_json.write_text('{"name": "test"}')

        with patch("pathlib.Path.cwd", return_value=tmp_path):
            result = runner.invoke(hooks, ["disable"])

        assert result.exit_code == 0
        assert "Hooks disabled" in result.output
        data = json.loads(project_json.read_text())
        assert data["hooks_disabled"] is True

    def test_hooks_disable_no_project(self, runner: CliRunner, tmp_path: Path) -> None:
        with patch("pathlib.Path.cwd", return_value=tmp_path):
            result = runner.invoke(hooks, ["disable"])

        assert result.exit_code == 1
        assert "No .gobby/project.json found" in result.output

    def test_hooks_enable_success(self, runner: CliRunner, tmp_path: Path) -> None:
        project_json = tmp_path / ".gobby" / "project.json"
        project_json.parent.mkdir(parents=True)
        project_json.write_text('{"name": "test", "hooks_disabled": true}')

        with patch("pathlib.Path.cwd", return_value=tmp_path):
            result = runner.invoke(hooks, ["enable"])

        assert result.exit_code == 0
        assert "Hooks re-enabled" in result.output
        data = json.loads(project_json.read_text())
        assert "hooks_disabled" not in data

    def test_hooks_enable_no_project(self, runner: CliRunner, tmp_path: Path) -> None:
        with patch("pathlib.Path.cwd", return_value=tmp_path):
            result = runner.invoke(hooks, ["enable"])

        assert result.exit_code == 1
        assert "No .gobby/project.json found" in result.output

    def test_hooks_disable_bad_json(self, runner: CliRunner, tmp_path: Path) -> None:
        project_json = tmp_path / ".gobby" / "project.json"
        project_json.parent.mkdir(parents=True)
        project_json.write_text("not valid json{{{")

        with patch("pathlib.Path.cwd", return_value=tmp_path):
            result = runner.invoke(hooks, ["disable"])

        assert result.exit_code == 1
        assert "Failed to read project.json" in result.output

    def test_hooks_enable_bad_json(self, runner: CliRunner, tmp_path: Path) -> None:
        project_json = tmp_path / ".gobby" / "project.json"
        project_json.parent.mkdir(parents=True)
        project_json.write_text("not valid json{{{")

        with patch("pathlib.Path.cwd", return_value=tmp_path):
            result = runner.invoke(hooks, ["enable"])

        assert result.exit_code == 1
        assert "Failed to read project.json" in result.output
