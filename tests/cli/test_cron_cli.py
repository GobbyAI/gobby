"""Tests for cron CLI commands."""

import json
import time
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from gobby.cli import cli
from gobby.storage.cron import SystemRowProtected
from gobby.storage.cron_models import CronJob, CronRun

pytestmark = pytest.mark.unit

PROJECT_ID = "00000000-0000-0000-0000-000000000000"
JOB_ID = "11111111-1111-1111-1111-111111111111"


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


def _make_daemon_response(
    status_code: int = 200,
    payload: dict[str, object] | None = None,
    text: str = "",
) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload or {"status": "success", "run": _make_run().to_dict()}
    response.text = text
    return response


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def mock_storage() -> Iterator[MagicMock]:
    """Create a mock cron storage with a mock db."""
    mock_db = MagicMock()
    mock_st = MagicMock()
    # Non-uuid job refs (the "cj-abc123" fixtures) resolve by name first.
    jobs_by_name = {
        "cj-abc123": _make_job(),
        "Test Job": _make_job(id=JOB_ID),
    }
    mock_st.get_job_by_name.side_effect = jobs_by_name.get
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
        for cmd in ["list", "add", "run", "toggle", "park", "wake", "runs", "remove", "edit"]:
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
        mock_storage.list_jobs.assert_called_once_with(
            project_id=None,
            enabled=True,
            exclude_removed_automation=True,
        )

    def test_list_filters_removed_automation_rows(self, runner, mock_storage) -> None:
        mock_storage.list_jobs.return_value = [_make_job(name="User Job")]
        result = runner.invoke(cli, ["cron", "list"])
        assert result.exit_code == 0
        assert "User Job" in result.output
        mock_storage.list_jobs.assert_called_once_with(
            project_id=None,
            enabled=None,
            exclude_removed_automation=True,
        )


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
        # No --timezone: storage resolves the host zone rather than forcing UTC.
        assert mock_storage.create_job.call_args.kwargs["timezone"] is None

    def test_add_uses_an_explicit_timezone_when_given(
        self, runner: CliRunner, mock_storage: MagicMock
    ) -> None:
        mock_storage.create_job.return_value = _make_job(timezone="Europe/Berlin")
        result = runner.invoke(
            cli,
            [
                "cron",
                "add",
                "--name",
                "Berlin Check",
                "--schedule",
                "0 7 * * *",
                "--action-type",
                "shell",
                "--action-config",
                '{"command": "echo"}',
                "--timezone",
                "Europe/Berlin",
            ],
        )

        assert result.exit_code == 0
        assert mock_storage.create_job.call_args.kwargs["timezone"] == "Europe/Berlin"
        assert "0 7 * * * Europe/Berlin" in result.output

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

    def test_add_invalid_schedule(self, runner, mock_storage) -> None:
        result = runner.invoke(
            cli,
            [
                "cron",
                "add",
                "--name",
                "Bad Schedule",
                "--schedule",
                "not a cron",
                "--action-type",
                "shell",
                "--action-config",
                '{"command": "echo"}',
            ],
        )
        assert result.exit_code != 0
        assert "Invalid cron schedule: not a cron" in result.output
        mock_storage.create_job.assert_not_called()


class TestCronRun:
    """Tests for 'gobby cron run'."""

    def test_run_triggers_execution(self, runner, mock_storage) -> None:
        # `cron run` obtains its client via gobby.cli.cron.get_daemon_client,
        # so patch that seam (patching DaemonClient would leak real HTTP calls
        # to any locally running daemon).
        with patch("gobby.cli.cron.get_daemon_client") as get_client:
            client = get_client.return_value
            client.call_http_api.return_value = _make_daemon_response()

            result = runner.invoke(cli, ["cron", "run", "cj-abc123"])

        assert result.exit_code == 0
        assert "cr-run123" in result.output
        client.call_http_api.assert_called_once_with(
            "/api/cron/jobs/cj-abc123/run",
            method="POST",
        )
        mock_storage.get_job.assert_not_called()
        mock_storage.create_run.assert_not_called()

    def test_run_resolves_job_name(self, runner, mock_storage) -> None:
        with patch("gobby.cli.cron.get_daemon_client") as get_client:
            client = get_client.return_value
            client.call_http_api.return_value = _make_daemon_response()

            result = runner.invoke(cli, ["cron", "run", "Test Job"])

        assert result.exit_code == 0
        mock_storage.get_job_by_name.assert_called_once_with("Test Job")
        client.call_http_api.assert_called_once_with(
            f"/api/cron/jobs/{JOB_ID}/run",
            method="POST",
        )

    def test_run_accepts_job_uuid(self, runner, mock_storage) -> None:
        with patch("gobby.cli.cron.get_daemon_client") as get_client:
            client = get_client.return_value
            client.call_http_api.return_value = _make_daemon_response()

            result = runner.invoke(cli, ["cron", "run", JOB_ID])

        assert result.exit_code == 0
        mock_storage.get_job_by_name.assert_not_called()
        client.call_http_api.assert_called_once_with(
            f"/api/cron/jobs/{JOB_ID}/run",
            method="POST",
        )

    def test_run_not_found(self, runner, mock_storage) -> None:
        response = _make_daemon_response(
            status_code=404,
            payload={"detail": "Cron job not found: cj-nonexistent"},
        )
        with patch("gobby.cli.cron.get_daemon_client") as get_client:
            get_client.return_value.call_http_api.return_value = response
            result = runner.invoke(cli, ["cron", "run", "cj-nonexistent"])

        assert result.exit_code != 0
        assert "Job not found: cj-nonexistent" in result.output
        mock_storage.create_run.assert_not_called()

    def test_run_json_output(self, runner, mock_storage) -> None:
        with patch("gobby.cli.cron.get_daemon_client") as get_client:
            get_client.return_value.call_http_api.return_value = _make_daemon_response()
            result = runner.invoke(cli, ["cron", "run", "cj-abc123", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["id"] == "cr-run123"
        mock_storage.create_run.assert_not_called()

    @pytest.mark.parametrize(
        ("status_code", "detail", "expected"),
        [
            (
                409,
                {
                    "code": "cron_job_already_running",
                    "message": "Cron job already has a running run: cj-abc123",
                },
                "Cron job already has a running run",
            ),
            (
                429,
                {
                    "code": "cron_max_concurrent_jobs",
                    "message": "Cron scheduler is at max concurrency (1/1)",
                },
                "Cron scheduler is at max concurrency",
            ),
            (
                503,
                {
                    "code": "cron_scheduler_unavailable",
                    "message": "Cron scheduler is not available",
                },
                "Cron scheduler is not available",
            ),
        ],
    )
    def test_run_daemon_rejection(
        self,
        runner,
        mock_storage,
        status_code: int,
        detail: dict[str, str],
        expected: str,
    ) -> None:
        with patch("gobby.cli.cron.get_daemon_client") as get_client:
            get_client.return_value.call_http_api.return_value = _make_daemon_response(
                status_code=status_code,
                payload={"detail": detail},
            )
            result = runner.invoke(cli, ["cron", "run", "cj-abc123"])

        assert result.exit_code != 0
        assert expected in result.output
        mock_storage.create_run.assert_not_called()

    def test_run_daemon_unavailable(self, runner, mock_storage) -> None:
        with patch("gobby.cli.cron.get_daemon_client") as get_client:
            get_client.return_value.call_http_api.side_effect = httpx.ConnectError(
                "connection refused"
            )
            result = runner.invoke(cli, ["cron", "run", "cj-abc123"])

        assert result.exit_code != 0
        assert "Daemon unavailable" in result.output
        mock_storage.create_run.assert_not_called()


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

    def test_toggle_resolves_job_name(self, runner, mock_storage) -> None:
        mock_storage.toggle_job.return_value = _make_job(id=JOB_ID)

        result = runner.invoke(cli, ["cron", "toggle", "Test Job"])

        assert result.exit_code == 0
        mock_storage.toggle_job.assert_called_once_with(JOB_ID)

    def test_toggle_not_found(self, runner, mock_storage) -> None:
        mock_storage.toggle_job.return_value = None
        result = runner.invoke(cli, ["cron", "toggle", "cj-nonexistent"])
        assert result.exit_code != 0

    def test_toggle_system_job_names_operator_commands(self, runner, mock_storage) -> None:
        message = (
            "Cron row cj-abc123 is system-managed; use `gobby cron park <id>` "
            "or `gobby cron wake <id>`."
        )
        mock_storage.toggle_job.side_effect = SystemRowProtected(message)

        result = runner.invoke(cli, ["cron", "toggle", "cj-abc123"])

        assert result.exit_code == 1
        assert message in result.output
        assert result.exception is not None
        assert not isinstance(result.exception, SystemRowProtected)

    def test_toggle_system_job_json_error_is_structured(self, runner, mock_storage) -> None:
        message = (
            "Cron row cj-abc123 is system-managed; use `gobby cron park <id>` "
            "or `gobby cron wake <id>`."
        )
        mock_storage.toggle_job.side_effect = SystemRowProtected(message)

        result = runner.invoke(cli, ["cron", "toggle", "cj-abc123", "--json"])

        assert result.exit_code == 1
        assert json.loads(result.output) == {"success": False, "error": message}


class TestCronParkWake:
    """Tests for system-managed cron scheduling controls."""

    def test_park_system_job(self, runner, mock_storage) -> None:
        mock_storage.park_system_job.return_value = _make_job(
            id=JOB_ID,
            name="gobby:memory-dream",
            is_system=True,
            next_run_at=None,
        )
        mock_storage.get_job_by_name.side_effect = None
        mock_storage.get_job_by_name.return_value = _make_job(
            id=JOB_ID,
            name="gobby:memory-dream",
            is_system=True,
        )

        result = runner.invoke(cli, ["cron", "park", "gobby:memory-dream"])

        assert result.exit_code == 0
        mock_storage.park_system_job.assert_called_once_with(JOB_ID)
        assert f"Parked system cron job {JOB_ID} (gobby:memory-dream)" in result.output

    def test_wake_system_job_json(self, runner, mock_storage) -> None:
        job = _make_job(
            id=JOB_ID,
            name="gobby:memory-dream",
            is_system=True,
        )
        mock_storage.wake_system_job.return_value = job

        result = runner.invoke(cli, ["cron", "wake", JOB_ID, "--json"])

        assert result.exit_code == 0
        mock_storage.wake_system_job.assert_called_once_with(JOB_ID)
        assert json.loads(result.output)["id"] == JOB_ID

    @pytest.mark.parametrize("command", ["park", "wake"])
    def test_system_job_command_rejects_non_system_row(
        self, runner, mock_storage, command: str
    ) -> None:
        storage_method = getattr(mock_storage, f"{command}_system_job")
        storage_method.side_effect = SystemRowProtected(
            f"Cron row {JOB_ID} is non-system; {command}_system_job is reserved."
        )

        result = runner.invoke(cli, ["cron", command, JOB_ID])

        assert result.exit_code == 1
        assert "non-system" in result.output
        assert result.exception is not None
        assert not isinstance(result.exception, SystemRowProtected)

    @pytest.mark.parametrize("command", ["park", "wake"])
    def test_system_job_command_not_found(self, runner, mock_storage, command: str) -> None:
        storage_method = getattr(mock_storage, f"{command}_system_job")
        storage_method.return_value = None

        result = runner.invoke(cli, ["cron", command, JOB_ID])

        assert result.exit_code == 1
        assert f"Job not found: {JOB_ID}" in result.output


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

    def test_runs_presents_trigger_times_in_local_zone(
        self,
        runner: CliRunner,
        mock_storage: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Runs are stored UTC and rendered in the reader's local zone."""
        monkeypatch.setenv("TZ", "America/Chicago")
        time.tzset()
        try:
            mock_storage.get_job.return_value = _make_job()
            mock_storage.list_runs.return_value = [_make_run(id="cr-001")]

            result = runner.invoke(cli, ["cron", "runs", "cj-abc123"])
        finally:
            monkeypatch.undo()
            time.tzset()

        assert result.exit_code == 0
        assert "2026-02-10T01:00:00-06:00" in result.output
        assert "2026-02-10T07:00:00+00:00" not in result.output

    def test_runs_empty(self, runner, mock_storage) -> None:
        mock_storage.get_job.return_value = _make_job()
        mock_storage.list_runs.return_value = []
        result = runner.invoke(cli, ["cron", "runs", "cj-abc123"])
        assert result.exit_code == 0
        assert "no runs" in result.output.lower()

    def test_runs_resolves_job_name(self, runner, mock_storage) -> None:
        mock_storage.get_job.return_value = _make_job(id=JOB_ID)
        mock_storage.list_runs.return_value = []

        result = runner.invoke(cli, ["cron", "runs", "Test Job"])

        assert result.exit_code == 0
        mock_storage.get_job.assert_called_once_with(JOB_ID)
        mock_storage.list_runs.assert_called_once_with(JOB_ID, limit=20)

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

    def test_remove_resolves_job_name(self, runner, mock_storage) -> None:
        mock_storage.delete_job.return_value = True

        result = runner.invoke(cli, ["cron", "remove", "Test Job", "--yes"])

        assert result.exit_code == 0
        mock_storage.delete_job.assert_called_once_with(JOB_ID)

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

    def test_edit_resolves_job_name(self, runner, mock_storage) -> None:
        mock_storage.get_job.return_value = _make_job(id=JOB_ID)
        mock_storage.update_job.return_value = _make_job(id=JOB_ID, description="Updated")

        result = runner.invoke(cli, ["cron", "edit", "Test Job", "--description", "Updated"])

        assert result.exit_code == 0
        mock_storage.get_job.assert_called_once_with(JOB_ID)
        mock_storage.update_job.assert_called_once_with(JOB_ID, description="Updated")

    def test_edit_schedule(self, runner, mock_storage) -> None:
        mock_storage.get_job.return_value = _make_job()
        mock_storage.update_job.return_value = _make_job(cron_expr="30 8 * * *")
        result = runner.invoke(cli, ["cron", "edit", "cj-abc123", "--schedule", "30 8 * * *"])
        assert result.exit_code == 0
        call_kwargs = mock_storage.update_job.call_args
        assert call_kwargs.kwargs["cron_expr"] == "30 8 * * *"

    def test_edit_interval_schedule_uses_shared_parser(self, runner, mock_storage) -> None:
        mock_storage.get_job.return_value = _make_job()
        mock_storage.update_job.return_value = _make_job(
            schedule_type="interval", interval_seconds=300, cron_expr=None
        )
        result = runner.invoke(cli, ["cron", "edit", "cj-abc123", "--schedule", "5m"])
        assert result.exit_code == 0
        call_kwargs = mock_storage.update_job.call_args
        assert call_kwargs.kwargs["schedule_type"] == "interval"
        assert call_kwargs.kwargs["cron_expr"] is None
        assert call_kwargs.kwargs["interval_seconds"] == 300

    def test_edit_invalid_schedule(self, runner: CliRunner, mock_storage: MagicMock) -> None:
        mock_storage.get_job.return_value = _make_job()
        result = runner.invoke(cli, ["cron", "edit", "cj-abc123", "--schedule", "not a cron"])
        assert result.exit_code != 0
        assert "Invalid cron schedule: not a cron" in result.output
        mock_storage.update_job.assert_not_called()

    def test_edit_enabled(self, runner: CliRunner, mock_storage: MagicMock) -> None:
        mock_storage.get_job.return_value = _make_job()
        mock_storage.update_job.return_value = _make_job(enabled=False)
        result = runner.invoke(cli, ["cron", "edit", "cj-abc123", "--disabled"])
        assert result.exit_code == 0
        call_kwargs = mock_storage.update_job.call_args
        assert call_kwargs.kwargs["enabled"] is False

    def test_edit_action_config(self, runner: CliRunner, mock_storage: MagicMock) -> None:
        mock_storage.get_job.return_value = _make_job()
        new_config = {"command": "ls", "args": ["-la"]}
        mock_storage.update_job.return_value = _make_job(action_config=new_config)
        result = runner.invoke(
            cli,
            ["cron", "edit", "cj-abc123", "--action-config", json.dumps(new_config)],
        )
        assert result.exit_code == 0

    def test_edit_no_changes(self, runner: CliRunner, mock_storage: MagicMock) -> None:
        mock_storage.get_job.return_value = _make_job()
        result = runner.invoke(cli, ["cron", "edit", "cj-abc123"])
        assert result.exit_code != 0

    def test_edit_not_found(self, runner: CliRunner, mock_storage: MagicMock) -> None:
        mock_storage.get_job.return_value = None
        result = runner.invoke(cli, ["cron", "edit", "cj-nonexistent", "--name", "X"])
        assert result.exit_code != 0

    def test_edit_json_output(self, runner: CliRunner, mock_storage: MagicMock) -> None:
        mock_storage.get_job.return_value = _make_job()
        mock_storage.update_job.return_value = _make_job(name="Updated")
        result = runner.invoke(cli, ["cron", "edit", "cj-abc123", "--name", "Updated", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["name"] == "Updated"
