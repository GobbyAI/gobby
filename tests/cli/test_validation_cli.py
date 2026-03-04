"""Tests for validation CLI commands.

TDD Red Phase: These tests define expected behavior for validation CLI
commands that don't yet exist or need extension.

Task: gt-34841b
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli import cli

pytestmark = pytest.mark.unit


@pytest.mark.unit
class TestValidateCommandWithNewFlags:
    """Tests for gobby tasks validate with enhanced validation flags."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    @pytest.fixture
    def mock_task(self):
        """Create a mock task."""
        task = MagicMock()
        task.id = "gt-test123"
        task.seq_num = 1
        task.title = "Test task"
        task.description = "Test description"
        task.status = "in_progress"
        task.project_id = "proj-123"
        task.validation_criteria = "Tests pass"
        task.validation_fail_count = 0
        return task

    def test_validate_help_shows_max_iterations_flag(self, runner: CliRunner) -> None:
        """Test that validate --help shows --max-iterations flag."""
        result = runner.invoke(cli, ["tasks", "validate", "--help"])
        assert result.exit_code == 0
        assert "--max-iterations" in result.output

    def test_validate_help_shows_history_flag(self, runner: CliRunner) -> None:
        """Test that validate --help shows --history flag."""
        result = runner.invoke(cli, ["tasks", "validate", "--help"])
        assert result.exit_code == 0
        assert "--history" in result.output

    def test_validate_help_shows_recurring_flag(self, runner: CliRunner) -> None:
        """Test that validate --help shows --recurring flag."""
        result = runner.invoke(cli, ["tasks", "validate", "--help"])
        assert result.exit_code == 0
        assert "--recurring" in result.output

    @patch("gobby.cli.tasks.ai.get_task_manager")
    @patch("gobby.cli.tasks.ai.resolve_task_id")
    @patch("gobby.config.app.load_config")
    def test_validate_with_max_iterations(
        self,
        mock_load_config: MagicMock,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test validate with --max-iterations flag."""
        mock_resolve.return_value = mock_task
        mock_manager = MagicMock()
        mock_manager.list_tasks.return_value = []  # No children
        mock_get_manager.return_value = mock_manager
        mock_load_config.return_value = MagicMock()

        result = runner.invoke(
            cli,
            [
                "tasks",
                "validate",
                "gt-test123",
                "--max-iterations",
                "5",
                "--summary",
                "test changes",
            ],
        )

        # Command should accept the flag (even if validation is mocked)
        # We're testing the CLI accepts the flag, not the implementation
        # Exit code 2 means Click rejected the flag as unrecognized
        assert result.exit_code != 2, f"Flag --max-iterations was rejected: {result.output}"

    @patch("gobby.cli.tasks.ai.get_task_manager")
    @patch("gobby.cli.tasks.ai.resolve_task_id")
    def test_validate_with_history_flag_shows_history(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test validate --history shows validation history."""
        mock_resolve.return_value = mock_task
        mock_manager = MagicMock()
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(
            cli,
            ["tasks", "validate", "gt-test123", "--history"],
        )

        # Should show history output, not require --summary
        assert result.exit_code == 0
        # History output should contain iteration or history info
        assert "history" in result.output.lower() or "iteration" in result.output.lower()

    @patch("gobby.cli.tasks.ai.get_task_manager")
    @patch("gobby.cli.tasks.ai.resolve_task_id")
    def test_validate_with_recurring_flag_shows_recurring_issues(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_task: MagicMock,
    ) -> None:
        """Test validate --recurring shows recurring issues."""
        mock_resolve.return_value = mock_task
        mock_manager = MagicMock()
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(
            cli,
            ["tasks", "validate", "gt-test123", "--recurring"],
        )

        assert result.exit_code == 0
        # Should show recurring issues info
        assert "recurring" in result.output.lower() or "no recurring" in result.output.lower()


class TestDeEscalateCommand:
    """Tests for gobby tasks de-escalate command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    def test_de_escalate_command_exists(self, runner: CliRunner) -> None:
        """Test that de-escalate command exists."""
        result = runner.invoke(cli, ["tasks", "de-escalate", "--help"])
        assert result.exit_code == 0
        assert "de-escalate" in result.output.lower() or "Return" in result.output

    def test_de_escalate_requires_task_id(self, runner: CliRunner) -> None:
        """Test that de-escalate requires a task ID."""
        result = runner.invoke(cli, ["tasks", "de-escalate"])
        # Should fail with missing argument
        assert result.exit_code != 0

    def test_de_escalate_requires_reason(self, runner: CliRunner) -> None:
        """Test that de-escalate requires a reason."""
        result = runner.invoke(cli, ["tasks", "de-escalate", "gt-test123"])
        # --reason is required=True in Click, so omitting it should fail with exit code 2
        assert (
            result.exit_code == 2
        ), f"Expected exit code 2 for missing required --reason, got {result.exit_code}"

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.resolve_task_id")
    def test_de_escalate_with_valid_args(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test de-escalate with valid arguments."""
        mock_task = MagicMock()
        mock_task.id = "gt-test123"
        mock_task.status = "escalated"
        mock_resolve.return_value = mock_task

        mock_manager = MagicMock()
        mock_manager.update_task.return_value = mock_task
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(
            cli,
            ["tasks", "de-escalate", "gt-test123", "--reason", "Fixed manually"],
        )

        assert result.exit_code == 0

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.resolve_task_id")
    def test_de_escalate_non_escalated_task_fails(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test de-escalate fails for non-escalated task."""
        mock_task = MagicMock()
        mock_task.id = "gt-test123"
        mock_task.status = "open"  # Not escalated
        mock_resolve.return_value = mock_task

        mock_manager = MagicMock()
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(
            cli,
            ["tasks", "de-escalate", "gt-test123", "--reason", "Some reason"],
        )

        # CLI prints error to stderr but returns exit code 0; check for error message
        assert (
            "not escalated" in result.output.lower()
        ), f"Expected 'not escalated' message for non-escalated task, got: {result.output}"

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.resolve_task_id")
    def test_de_escalate_with_reset_validation_flag(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test de-escalate with --reset-validation flag."""
        mock_task = MagicMock()
        mock_task.id = "gt-test123"
        mock_task.status = "escalated"
        mock_resolve.return_value = mock_task

        mock_manager = MagicMock()
        mock_manager.update_task.return_value = mock_task
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(
            cli,
            ["tasks", "de-escalate", "gt-test123", "--reason", "Fixed", "--reset-validation"],
        )

        # With valid args and an escalated task, command should succeed
        assert (
            result.exit_code == 0
        ), f"Expected exit code 0 for valid de-escalate command, got {result.exit_code}: {result.output}"


class TestValidationHistoryCommand:
    """Tests for gobby tasks validation-history command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    def test_validation_history_command_exists(self, runner: CliRunner) -> None:
        """Test that validation-history command exists."""
        result = runner.invoke(cli, ["tasks", "validation-history", "--help"])
        assert result.exit_code == 0

    def test_validation_history_requires_task_id(self, runner: CliRunner) -> None:
        """Test that validation-history requires a task ID."""
        result = runner.invoke(cli, ["tasks", "validation-history"])
        # Should fail with missing argument
        assert result.exit_code != 0

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.resolve_task_id")
    def test_validation_history_shows_iterations(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test validation-history shows iteration data."""
        mock_task = MagicMock()
        mock_task.id = "gt-test123"
        mock_resolve.return_value = mock_task

        mock_manager = MagicMock()
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(
            cli,
            ["tasks", "validation-history", "gt-test123"],
        )

        assert result.exit_code == 0

    def test_validation_history_clear_flag_exists(self, runner: CliRunner) -> None:
        """Test that validation-history --clear flag exists."""
        result = runner.invoke(cli, ["tasks", "validation-history", "--help"])
        assert "--clear" in result.output

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.resolve_task_id")
    def test_validation_history_clear_removes_history(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test validation-history --clear removes history."""
        mock_task = MagicMock()
        mock_task.id = "gt-test123"
        mock_resolve.return_value = mock_task

        mock_manager = MagicMock()
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(
            cli,
            ["tasks", "validation-history", "gt-test123", "--clear"],
        )

        assert result.exit_code == 0
        assert "cleared" in result.output.lower()

    @patch("gobby.cli.tasks.crud.get_task_manager")
    @patch("gobby.cli.tasks.crud.resolve_task_id")
    def test_validation_history_json_output(
        self,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test validation-history --json outputs JSON."""
        mock_task = MagicMock()
        mock_task.id = "gt-test123"
        mock_resolve.return_value = mock_task

        mock_manager = MagicMock()
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(
            cli,
            ["tasks", "validation-history", "gt-test123", "--json"],
        )

        assert (
            result.exit_code == 0
        ), f"Expected exit code 0 for --json output, got {result.exit_code}: {result.output}"

        # Output should be valid JSON with expected structure
        try:
            data = json.loads(result.output)
        except json.JSONDecodeError as e:
            pytest.fail(f"Output is not valid JSON: {e}\nOutput was: {result.output}")

        # Verify top-level keys exist
        assert (
            "task_id" in data
        ), f"Expected 'task_id' key in JSON output, got keys: {list(data.keys())}"
        assert (
            "iterations" in data
        ), f"Expected 'iterations' key in JSON output, got keys: {list(data.keys())}"

        # Verify types
        assert isinstance(
            data["task_id"], str
        ), f"Expected 'task_id' to be a string, got {type(data['task_id']).__name__}"
        assert isinstance(
            data["iterations"], list
        ), f"Expected 'iterations' to be a list, got {type(data['iterations']).__name__}"


class TestListTasksEscalatedFilter:
    """Tests for gobby tasks list --status escalated filter."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    def test_list_accepts_escalated_status(self, runner: CliRunner) -> None:
        """Test that list command accepts --status escalated."""
        result = runner.invoke(cli, ["tasks", "list", "--help"])
        assert result.exit_code == 0
        # Help should mention status filter
        assert "--status" in result.output

    @patch("gobby.cli.tasks.crud.get_task_manager")
    def test_list_with_escalated_status_filter(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test list --status escalated filters correctly."""
        mock_task = MagicMock()
        mock_task.id = "gt-test123"
        mock_task.seq_num = 1
        mock_task.title = "Escalated task"
        mock_task.status = "escalated"
        mock_task.priority = 2
        mock_task.task_type = "task"
        mock_task.parent_task_id = None
        mock_task.to_dict.return_value = {
            "id": "gt-test123",
            "title": "Escalated task",
            "status": "escalated",
        }

        mock_manager = MagicMock()
        mock_manager.list_tasks.return_value = [mock_task]
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(
            cli,
            ["tasks", "list", "--status", "escalated"],
        )

        assert result.exit_code == 0
        # Should have called list_tasks with status filter
        mock_manager.list_tasks.assert_called()
        call_kwargs = mock_manager.list_tasks.call_args.kwargs
        assert call_kwargs.get("status") == "escalated"

    @patch("gobby.cli.tasks.crud.get_task_manager")
    def test_list_escalated_shows_escalation_reason(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test that escalated tasks show escalation reason."""
        mock_task = MagicMock()
        mock_task.id = "gt-test123"
        mock_task.seq_num = 1
        mock_task.title = "Escalated task"
        mock_task.status = "escalated"
        mock_task.escalation_reason = "recurring_issues"
        mock_task.escalated_at = "2025-01-01T00:00:00Z"
        mock_task.priority = 2
        mock_task.task_type = "task"
        mock_task.parent_task_id = None
        mock_task.to_dict.return_value = {
            "id": "gt-test123",
            "title": "Escalated task",
            "status": "escalated",
            "escalation_reason": "recurring_issues",
        }

        mock_manager = MagicMock()
        mock_manager.list_tasks.return_value = [mock_task]
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(
            cli,
            ["tasks", "list", "--status", "escalated"],
        )

        assert result.exit_code == 0
        # Escalated tasks should display their escalation reason
        assert "escalat" in result.output.lower() or "recurring" in result.output.lower()


class TestValidateFlagCombinations:
    """Tests for validate command flag combinations."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    def test_history_flag_does_not_require_summary(self, runner: CliRunner) -> None:
        """Test that --history bypasses --summary requirement.

        When using --history, we're viewing validation history data,
        not running a new validation, so --summary is not required.
        """
        result = runner.invoke(
            cli,
            ["tasks", "validate", "gt-test123", "--history"],
        )
        # Should not ask for --summary when just viewing history
        assert "summary" not in result.output.lower() or result.exit_code != 2

    @patch("gobby.cli.tasks.ai.get_task_manager")
    @patch("gobby.cli.tasks.ai.resolve_task_id")
    @patch("gobby.config.app.load_config")
    def test_all_flags_together(
        self,
        mock_load_config: MagicMock,
        mock_resolve: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test using multiple flags together."""
        mock_task = MagicMock()
        mock_task.id = "gt-test123"
        mock_task.title = "Test"
        mock_task.status = "in_progress"
        mock_task.validation_fail_count = 0
        mock_resolve.return_value = mock_task

        mock_manager = MagicMock()
        mock_manager.list_tasks.return_value = []
        mock_get_manager.return_value = mock_manager
        mock_load_config.return_value = MagicMock()

        result = runner.invoke(
            cli,
            [
                "tasks",
                "validate",
                "gt-test123",
                "--max-iterations",
                "3",
                "--summary",
                "test changes",
            ],
        )

        # All flags should be accepted without error
        assert result.exit_code != 2  # 2 is Click's usage error


class TestValidateTaskNotFound:
    """Tests for validation commands with non-existent tasks."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    @patch("gobby.cli.tasks.ai.get_task_manager")
    def test_validate_history_task_not_found(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test validate --history with non-existent task."""
        mock_manager = MagicMock()
        mock_manager.get_task.side_effect = ValueError("not found")
        mock_manager.find_tasks_by_prefix.return_value = []
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(
            cli,
            ["tasks", "validate", "gt-nonexistent", "--history"],
        )

        # Should handle gracefully - resolve_task_id prints "not found"
        assert "not found" in result.output.lower()

    @patch("gobby.cli.tasks.crud.get_task_manager")
    def test_validation_history_task_not_found(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test validation-history with non-existent task."""
        mock_manager = MagicMock()
        mock_manager.get_task.side_effect = ValueError("not found")
        mock_manager.find_tasks_by_prefix.return_value = []
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(
            cli,
            ["tasks", "validation-history", "gt-nonexistent"],
        )

        assert "not found" in result.output.lower()

    @patch("gobby.cli.tasks.crud.get_task_manager")
    def test_de_escalate_task_not_found(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test de-escalate with non-existent task."""
        mock_manager = MagicMock()
        mock_manager.get_task.side_effect = ValueError("not found")
        mock_manager.find_tasks_by_prefix.return_value = []
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(
            cli,
            ["tasks", "de-escalate", "gt-nonexistent", "--reason", "test"],
        )

        assert "not found" in result.output.lower()
