"""Tests for cron CLI commands."""

import json
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli import cli
from gobby.storage.cron_models import CronJob, CronRun

pytestmark = pytest.mark.unit

PROJECT_ID = "00000000-0000-0000-0000-000000000000"


def _make_job(**overrides: object) -> CronJob:
    """Create a CronJob with sensible defaults."""
    defaults = {
        "id": "cj-abc123",
        "project_id": PROJECT_ID,
        "name": "Test Job",
        "schedule_type": "cron",
        "cron_expr": "0 7 * * *",
        "interval_seconds": None,
        "run_at": None,
        "timezone": "UTC",
        "action_type": "shell",
        "action_config": {"command": "echo", "args": ["hello"]},
        "enabled": True,
        "next_run_at": "2026-02-11T07:00:00+00:00",
        "last_run_at": None,
        "last_status": None,
        "consecutive_failures": 0,
        "description": None,
        "created_at": "2026-02-10T00:00:00+00:00",
        "updated_at": "2026-02-10T00:00:00+00:00",
    }
    defaults.update(overrides)
    return CronJob(**defaults)


def _make_run(**overrides: object) -> CronRun:
    """Create a CronRun with sensible defaults."""
    defaults = {
        "id": "cr-run123",
        "cron_job_id": "cj-abc123",
        "triggered_at": "2026-02-10T07:00:00+00:00",
        "started_at": "2026-02-10T07:00:01+00:00",
        "completed_at": "2026-02-10T07:00:05+00:00",
        "status": "completed",
        "output": "hello",
        "error": None,
        "agent_run_id": None,
        "pipeline_execution_id": None,
        "created_at": "2026-02-10T07:00:00+00:00",
    }
    defaults.update(overrides)
    return CronRun(**defaults)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def mock_storage() -> Iterator[MagicMock]:
    """Create a mock cron storage with a mock db."""
    mock_db = MagicMock()
    mock_st = MagicMock()
    with patch("gobby.cli.cron.get_cron_storage", return_value=(mock_db, mock_st)):
        yield mock_st


class TestCronCommandRegistration:
    """Tests for cron CLI command registration."""

    def test_cron_command_exists(self, runner) -> None:
        result = runner.invoke(cli, ["cron", "--help"])
        assert result.exit_code == 0
        assert "cron" in result.output.lower()

    def test_cron_subcommands_exist(self, runner) -> None:
        result = runner.invoke(cli, ["cron", "--help"])
        assert result.exit_code == 0
        for cmd in ["list", "add", "run", "toggle", "runs", "remove", "edit"]:
            assert cmd in result.output


class TestCronList:
    """Tests for 'gobby cron list'."""

    def test_list_shows_jobs(self, runner, mock_storage) -> None:
        mock_storage.list_jobs.return_value = [
            _make_job(id="cj-001", name="Email Check"),
            _make_job(id="cj-002", name="DB Backup", enabled=False),
        ]
        result = runner.invoke(cli, ["cron", "list"])
        assert result.exit_code == 0
        assert "Email Check" in result.output
        assert "DB Backup" in result.output

    def test_list_empty(self, runner, mock_storage) -> None:
        mock_storage.list_jobs.return_value = []
        result = runner.invoke(cli, ["cron", "list"])
        assert result.exit_code == 0
        assert "no cron jobs" in result.output.lower()

    def test_list_json_format(self, runner, mock_storage) -> None:
        mock_storage.list_jobs.return_value = [_make_job()]
        result = runner.invoke(cli, ["cron", "list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["name"] == "Test Job"

    def test_list_enabled_filter(self, runner, mock_storage) -> None:
        mock_storage.list_jobs.return_value = []
        result = runner.invoke(cli, ["cron", "list", "--enabled"])
        assert result.exit_code == 0
        mock_storage.list_jobs.assert_called_once_with(project_id=None, enabled=True)


class TestCronAdd:
    """Tests for 'gobby cron add'."""

    def test_add_cron_job(self, runner, mock_storage) -> None:
        mock_storage.create_job.return_value = _make_job()
        result = runner.invoke(
            cli,
            [
                "cron",
                "add",
                "--name",
                "Morning Check",
                "--schedule",
                "0 7 * * *",
                "--action-type",
                "shell",
                "--action-config",
                '{"command": "echo", "args": ["hello"]}',
            ],
        )
        assert result.exit_code == 0
        assert "cj-abc123" in result.output
        mock_storage.create_job.assert_called_once()

    def test_add_interval_job(self, runner, mock_storage) -> None:
        mock_storage.create_job.return_value = _make_job(
            schedule_type="interval", interval_seconds=300, cron_expr=None
        )
        result = runner.invoke(
            cli,
            [
                "cron",
                "add",
                "--name",
                "Periodic Check",
                "--schedule",
                "300s",
                "--action-type",
                "shell",
                "--action-config",
                '{"command": "echo"}',
            ],
        )
        assert result.exit_code == 0
        call_kwargs = mock_storage.create_job.call_args
        assert call_kwargs.kwargs["schedule_type"] == "interval"
        assert call_kwargs.kwargs["interval_seconds"] == 300

    def test_add_json_output(self, runner, mock_storage) -> None:
        mock_storage.create_job.return_value = _make_job()
        result = runner.invoke(
            cli,
            [
                "cron",
                "add",
                "--name",
                "Test",
                "--schedule",
                "0 * * * *",
                "--action-type",
                "shell",
                "--action-config",
                '{"command": "echo"}',
                "--json",
            ],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["id"] == "cj-abc123"

    def test_add_invalid_json(self, runner, mock_storage) -> None:
        result = runner.invoke(
            cli,
            [
                "cron",
                "add",
                "--name",
                "Bad",
                "--schedule",
                "0 * * * *",
                "--action-type",
                "shell",
                "--action-config",
                "not-json",
            ],
        )
        assert result.exit_code != 0


class TestCronRun:
    """Tests for 'gobby cron run'."""

    def test_run_triggers_execution(self, runner, mock_storage) -> None:
        mock_storage.get_job.return_value = _make_job()
        mock_storage.create_run.return_value = _make_run()
        result = runner.invoke(cli, ["cron", "run", "cj-abc123"])
        assert result.exit_code == 0
        assert "cr-run123" in result.output

    def test_run_not_found(self, runner, mock_storage) -> None:
        mock_storage.get_job.return_value = None
        result = runner.invoke(cli, ["cron", "run", "cj-nonexistent"])
        assert result.exit_code != 0

    def test_run_json_output(self, runner, mock_storage) -> None:
        mock_storage.get_job.return_value = _make_job()
        mock_storage.create_run.return_value = _make_run()
        result = runner.invoke(cli, ["cron", "run", "cj-abc123", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["id"] == "cr-run123"


class TestCronToggle:
    """Tests for 'gobby cron toggle'."""

    def test_toggle_enables(self, runner, mock_storage) -> None:
        mock_storage.toggle_job.return_value = _make_job(enabled=True)
        result = runner.invoke(cli, ["cron", "toggle", "cj-abc123"])
        assert result.exit_code == 0
        assert "enabled" in result.output

    def test_toggle_disables(self, runner, mock_storage) -> None:
        mock_storage.toggle_job.return_value = _make_job(enabled=False)
        result = runner.invoke(cli, ["cron", "toggle", "cj-abc123"])
        assert result.exit_code == 0
        assert "disabled" in result.output

    def test_toggle_not_found(self, runner, mock_storage) -> None:
        mock_storage.toggle_job.return_value = None
        result = runner.invoke(cli, ["cron", "toggle", "cj-nonexistent"])
        assert result.exit_code != 0


class TestCronRuns:
    """Tests for 'gobby cron runs'."""

    def test_runs_shows_history(self, runner, mock_storage) -> None:
        mock_storage.get_job.return_value = _make_job()
        mock_storage.list_runs.return_value = [
            _make_run(id="cr-001", status="completed"),
            _make_run(id="cr-002", status="failed"),
        ]
        result = runner.invoke(cli, ["cron", "runs", "cj-abc123"])
        assert result.exit_code == 0
        assert "cr-001" in result.output
        assert "cr-002" in result.output

    def test_runs_empty(self, runner, mock_storage) -> None:
        mock_storage.get_job.return_value = _make_job()
        mock_storage.list_runs.return_value = []
        result = runner.invoke(cli, ["cron", "runs", "cj-abc123"])
        assert result.exit_code == 0
        assert "no runs" in result.output.lower()

    def test_runs_json_output(self, runner, mock_storage) -> None:
        mock_storage.get_job.return_value = _make_job()
        mock_storage.list_runs.return_value = [_make_run()]
        result = runner.invoke(cli, ["cron", "runs", "cj-abc123", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 1

    def test_runs_not_found(self, runner, mock_storage) -> None:
        mock_storage.get_job.return_value = None
        result = runner.invoke(cli, ["cron", "runs", "cj-nonexistent"])
        assert result.exit_code != 0

    def test_runs_respects_limit(self, runner, mock_storage) -> None:
        mock_storage.get_job.return_value = _make_job()
        mock_storage.list_runs.return_value = []
        runner.invoke(cli, ["cron", "runs", "cj-abc123", "--limit", "5"])
        mock_storage.list_runs.assert_called_once_with("cj-abc123", limit=5)
        assert mock_storage.list_runs.call_count == 1
        assert mock_storage.list_runs.call_args is not None


class TestCronRemove:
    """Tests for 'gobby cron remove'."""

    def test_remove_deletes_job(self, runner, mock_storage) -> None:
        mock_storage.delete_job.return_value = True
        result = runner.invoke(cli, ["cron", "remove", "cj-abc123", "--yes"])
        assert result.exit_code == 0
        assert "removed" in result.output.lower()

    def test_remove_not_found(self, runner, mock_storage) -> None:
        mock_storage.delete_job.return_value = False
        result = runner.invoke(cli, ["cron", "remove", "cj-nonexistent", "--yes"])
        assert result.exit_code != 0


class TestCronEdit:
    """Tests for 'gobby cron edit'."""

    def test_edit_name(self, runner, mock_storage) -> None:
        mock_storage.get_job.return_value = _make_job()
        mock_storage.update_job.return_value = _make_job(name="New Name")
        result = runner.invoke(cli, ["cron", "edit", "cj-abc123", "--name", "New Name"])
        assert result.exit_code == 0
        assert "New Name" in result.output
        mock_storage.update_job.assert_called_once()

    def test_edit_schedule(self, runner, mock_storage) -> None:
        mock_storage.get_job.return_value = _make_job()
        mock_storage.update_job.return_value = _make_job(cron_expr="30 8 * * *")
        result = runner.invoke(cli, ["cron", "edit", "cj-abc123", "--schedule", "30 8 * * *"])
        assert result.exit_code == 0
        call_kwargs = mock_storage.update_job.call_args
        assert call_kwargs.kwargs["cron_expr"] == "30 8 * * *"

    def test_edit_enabled(self, runner, mock_storage) -> None:
        mock_storage.get_job.return_value = _make_job()
        mock_storage.update_job.return_value = _make_job(enabled=False)
        result = runner.invoke(cli, ["cron", "edit", "cj-abc123", "--disabled"])
        assert result.exit_code == 0
        call_kwargs = mock_storage.update_job.call_args
        assert call_kwargs.kwargs["enabled"] is False

    def test_edit_action_config(self, runner, mock_storage) -> None:
        mock_storage.get_job.return_value = _make_job()
        new_config = {"command": "ls", "args": ["-la"]}
        mock_storage.update_job.return_value = _make_job(action_config=new_config)
        result = runner.invoke(
            cli,
            ["cron", "edit", "cj-abc123", "--action-config", json.dumps(new_config)],
        )
        assert result.exit_code == 0

    def test_edit_no_changes(self, runner, mock_storage) -> None:
        mock_storage.get_job.return_value = _make_job()
        result = runner.invoke(cli, ["cron", "edit", "cj-abc123"])
        assert result.exit_code != 0

    def test_edit_not_found(self, runner, mock_storage) -> None:
        mock_storage.get_job.return_value = None
        result = runner.invoke(cli, ["cron", "edit", "cj-nonexistent", "--name", "X"])
        assert result.exit_code != 0

    def test_edit_json_output(self, runner, mock_storage) -> None:
        mock_storage.get_job.return_value = _make_job()
        mock_storage.update_job.return_value = _make_job(name="Updated")
        result = runner.invoke(cli, ["cron", "edit", "cj-abc123", "--name", "Updated", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["name"] == "Updated"
