import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.config.features import HooksConfig, HookStageConfig, ProjectVerificationConfig
from gobby.hooks.verification_runner import (
    StageResult,
    VerificationResult,
    VerificationRunner,
    run_command,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_verification_config():
    config = MagicMock(spec=ProjectVerificationConfig)
    config.get_command.side_effect = (
        lambda name: f"echo {name}" if name in ["lint", "test"] else None
    )
    return config


@pytest.fixture
def mock_hooks_config():
    config = MagicMock(spec=HooksConfig)

    # Setup stage configs
    pre_commit = MagicMock(spec=HookStageConfig)
    pre_commit.enabled = True
    pre_commit.run = ["lint", "test"]
    pre_commit.timeout = 10
    pre_commit.fail_fast = False

    pre_push = MagicMock(spec=HookStageConfig)
    pre_push.enabled = True
    pre_push.run = ["test"]
    pre_push.timeout = 30

    disabled_stage = MagicMock(spec=HookStageConfig)
    disabled_stage.enabled = False

    empty_stage = MagicMock(spec=HookStageConfig)
    empty_stage.enabled = True
    empty_stage.run = []

    def get_stage(name):
        stages = {
            "pre-commit": pre_commit,
            "pre-push": pre_push,
            "disabled": disabled_stage,
            "empty": empty_stage,
        }
        return stages.get(name, MagicMock(enabled=True, run=[]))

    config.get_stage.side_effect = get_stage
    return config


class TestRunCommand:
    @patch("subprocess.run")
    def test_run_command_success(self, mock_run) -> None:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Success output",
            stderr="",
        )

        result = run_command("test-cmd", "echo test", Path("/tmp"))

        assert result.name == "test-cmd"
        assert result.command == "echo test"
        assert result.success is True
        assert result.exit_code == 0
        assert result.stdout == "Success output"
        assert result.duration_ms >= 0

    @patch("subprocess.run")
    def test_run_command_failure(self, mock_run) -> None:
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Error output",
        )

        result = run_command("test-cmd", "invalid", Path("/tmp"))

        assert result.success is False
        assert result.exit_code == 1
        assert result.stderr == "Error output"

    @patch("subprocess.run")
    def test_run_command_timeout(self, mock_run) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="long-cmd", timeout=1)

        result = run_command("timeout-cmd", "sleep 2", Path("/tmp"), timeout=1)

        assert result.success is False
        assert "timed out" in result.error

    @patch("subprocess.run")
    def test_run_command_exception(self, mock_run) -> None:
        mock_run.side_effect = OSError("System error")

        result = run_command("error-cmd", "broken", Path("/tmp"))

        assert result.success is False
        assert "System error" in result.error


class TestStageResult:
    def test_counts(self) -> None:
        results = [
            VerificationResult("p1", "c1", success=True),
            VerificationResult("p2", "c2", success=True),
            VerificationResult("f1", "c3", success=False),
            VerificationResult("s1", "c4", success=False, skipped=True),
        ]
        stage_result = StageResult("test", success=False, results=results)

        assert stage_result.passed_count == 2
        assert stage_result.failed_count == 1  # Skipped doesn't count as failed
        assert stage_result.skipped_count == 1


class TestVerificationRunner:
    def test_init_defaults(self) -> None:
        with patch("pathlib.Path.cwd", return_value=Path("/tmp")):
            runner = VerificationRunner()
            assert runner.cwd == Path("/tmp")
            assert runner.verification_config is None
            assert runner.hooks_config is None

    @patch("gobby.hooks.verification_runner.get_verification_config")
    @patch("gobby.hooks.verification_runner.get_hooks_config")
    def test_from_project(self, mock_get_hooks, mock_get_verif) -> None:
        runner = VerificationRunner.from_project(Path("/test/project"))

        assert runner.cwd == Path("/test/project")
        mock_get_verif.assert_called_with(Path("/test/project"))
        mock_get_hooks.assert_called_with(Path("/test/project"))

    def test_run_stage_no_hooks_config(self) -> None:
        runner = VerificationRunner()
        result = runner.run_stage("pre-commit")

        assert result.skipped is True
        assert "No hooks configured" in result.skip_reason

    def test_run_stage_disabled(self, mock_hooks_config) -> None:
        runner = VerificationRunner(hooks_config=mock_hooks_config)
        result = runner.run_stage("disabled")

        assert result.skipped is True
        assert "disabled" in result.skip_reason

    def test_run_stage_empty(self, mock_hooks_config) -> None:
        runner = VerificationRunner(hooks_config=mock_hooks_config)
        result = runner.run_stage("empty")

        assert result.skipped is True
        assert "No commands" in result.skip_reason

    def test_run_stage_no_verification_config(self, mock_hooks_config) -> None:
        runner = VerificationRunner(hooks_config=mock_hooks_config)
        result = runner.run_stage("pre-commit")

        assert result.skipped is False
        assert result.success is False
        assert [item.name for item in result.results] == ["lint", "test"]
        assert result.failed_count == 2
        assert all("not defined" in (item.error or "") for item in result.results)

    @patch("gobby.hooks.verification_runner.run_command")
    def test_run_stage_success(
        self, mock_run_cmd, mock_verification_config, mock_hooks_config
    ) -> None:
        runner = VerificationRunner(
            verification_config=mock_verification_config, hooks_config=mock_hooks_config
        )

        mock_run_cmd.return_value = VerificationResult("cmd", "echo", success=True)

        result = runner.run_stage("pre-commit")

        assert result.success is True
        assert len(result.results) == 2
        assert mock_run_cmd.call_count == 2

    def test_run_stage_undefined_command(self, mock_verification_config, mock_hooks_config) -> None:
        # Add unknown command
        mock_hooks_config.get_stage("pre-commit").run = ["unknown"]

        runner = VerificationRunner(
            verification_config=mock_verification_config, hooks_config=mock_hooks_config
        )

        result = runner.run_stage("pre-commit")

        assert result.success is False
        assert len(result.results) == 1
        assert result.results[0].success is False
        assert result.results[0].skipped is False
        assert result.results[0].error == "Command 'unknown' not defined in verification config"
        assert result.failed_count == 1

    @patch("gobby.hooks.verification_runner.run_command")
    def test_run_stage_fail_fast(
        self, mock_run_cmd, mock_verification_config, mock_hooks_config
    ) -> None:
        stage_config = mock_hooks_config.get_stage("pre-commit")
        stage_config.fail_fast = True

        runner = VerificationRunner(
            verification_config=mock_verification_config, hooks_config=mock_hooks_config
        )

        # First command fails
        mock_run_cmd.side_effect = [
            VerificationResult("lint", "echo lint", success=False),
            VerificationResult("test", "echo test", success=True),
        ]

        result = runner.run_stage("pre-commit")

        assert result.success is False
        assert len(result.results) == 1  # Should stop after first failure
        assert result.results[0].name == "lint"

    def test_get_stage_config(self, mock_hooks_config) -> None:
        runner = VerificationRunner(hooks_config=mock_hooks_config)
        config = runner.get_stage_config("pre-commit")
        assert config is not None

        runner_no_config = VerificationRunner()
        assert runner_no_config.get_stage_config("pre-commit") is None
