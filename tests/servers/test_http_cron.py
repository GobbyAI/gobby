"""Tests for HTTP cron job endpoints."""

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from gobby.app_context import ServiceContainer
from gobby.scheduler.scheduler import CronRunRejected
from gobby.servers.http import HTTPServer
from gobby.storage.cron import CronJobStorage, SystemRowProtected
from gobby.storage.cron_models import CronJob, CronRun, CronRunChild
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit

PROJECT_ID = "00000000-0000-0000-0000-000000000000"
UNKNOWN_PROJECT_ID = "ffffffff-ffff-4fff-8fff-ffffffffffff"


def _make_job(**overrides: object) -> CronJob:
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
def session_storage(temp_db: HubDatabase) -> SessionManager:
    return SessionManager(temp_db)


@pytest.fixture
def cron_storage() -> MagicMock:
    return MagicMock(spec=CronJobStorage)


@pytest.fixture
def cron_scheduler() -> MagicMock:
    mock = MagicMock()
    mock.run_now = AsyncMock()
    return mock


@pytest.fixture
def http_server(
    session_storage: SessionManager,
    cron_storage: MagicMock,
    cron_scheduler: MagicMock,
) -> HTTPServer:
    services = ServiceContainer(
        config=None,
        database=session_storage.db,
        session_manager=session_storage,
        task_manager=MagicMock(),
        cron_storage=cron_storage,
        cron_scheduler=cron_scheduler,
    )
    return HTTPServer(services=services, port=60888, test_mode=True, auth_mode="disabled")


@pytest.fixture
def client(http_server: HTTPServer) -> Iterator[TestClient]:
    with patch("gobby.servers.app_factory.HookManager") as MockHM:
        mock_instance = MockHM.return_value
        mock_instance._stop_registry = MagicMock()
        mock_instance.shutdown = MagicMock()
        mock_instance.shutdown_async = AsyncMock()
        with TestClient(http_server.app) as client:
            yield client


class TestCronListJobs:
    def test_list_jobs(self, client, cron_storage) -> None:
        cron_storage.list_jobs.return_value = [_make_job(), _make_job(id="cj-def456")]
        resp = client.get("/api/cron/jobs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2

    def test_list_jobs_with_filter(self, client, cron_storage) -> None:
        cron_storage.list_jobs.return_value = []
        resp = client.get("/api/cron/jobs?enabled=true")
        assert resp.status_code == 200
        cron_storage.list_jobs.assert_called_once_with(project_id=None, enabled=True)

    def test_list_jobs_filters_removed_automation_rows(self, client, cron_storage) -> None:
        cron_storage.list_jobs.return_value = [
            _make_job(name="User Job"),
            _make_job(id="cj-system", name="gobby:dispatcher"),
            _make_job(id="cj-heartbeat", name="gobby:pipeline-heartbeat"),
        ]

        resp = client.get("/api/cron/jobs")

        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["jobs"][0]["name"] == "User Job"


class TestCronCreateJob:
    def test_create_job_uses_current_project(
        self,
        client,
        cron_storage,
        http_server,
        project_storage: LocalProjectManager,
    ) -> None:
        project = project_storage.create(name="current-project")
        http_server.resolve_project_id = MagicMock(return_value=project.id)
        cron_storage.create_job.return_value = _make_job(project_id=project.id)
        resp = client.post(
            "/api/cron/jobs",
            json={
                "name": "Test",
                "action_type": "shell",
                "action_config": {"command": "echo"},
                "cron_expr": "0 7 * * *",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["job"]["name"] == "Test Job"
        assert cron_storage.create_job.call_args.kwargs["project_id"] == project.id

    def test_create_job_supports_explicit_project(
        self,
        client,
        cron_storage,
        project_storage: LocalProjectManager,
    ) -> None:
        project = project_storage.create(name="explicit-project")
        cron_storage.create_job.return_value = _make_job(project_id=project.id)

        resp = client.post(
            "/api/cron/jobs",
            json={
                "name": "Test",
                "project_id": project.id,
                "action_type": "shell",
                "action_config": {"command": "echo"},
                "cron_expr": "0 7 * * *",
            },
        )

        assert resp.status_code == 200
        assert cron_storage.create_job.call_args.kwargs["project_id"] == project.id

    def test_create_job_rejects_unknown_project(self, client, cron_storage) -> None:
        resp = client.post(
            "/api/cron/jobs",
            json={
                "name": "Test",
                "project_id": UNKNOWN_PROJECT_ID,
                "action_type": "shell",
                "action_config": {"command": "echo"},
                "cron_expr": "0 7 * * *",
            },
        )

        assert resp.status_code == 404
        assert resp.json()["detail"] == f"Project not found: {UNKNOWN_PROJECT_ID}"
        cron_storage.create_job.assert_not_called()

    def test_create_job_rejects_missing_project_context(
        self, client, cron_storage, http_server
    ) -> None:
        http_server.resolve_project_id = MagicMock(
            side_effect=ValueError("No project ID provided or detected")
        )

        resp = client.post(
            "/api/cron/jobs",
            json={
                "name": "Test",
                "action_type": "shell",
                "action_config": {"command": "echo"},
                "cron_expr": "0 7 * * *",
            },
        )

        assert resp.status_code == 400
        assert resp.json()["detail"] == "No project ID provided or detected"
        cron_storage.create_job.assert_not_called()

    @pytest.mark.parametrize(
        "schedule",
        [
            {"schedule_type": "cron"},
            {"schedule_type": "interval"},
            {"schedule_type": "once"},
        ],
    )
    def test_create_rejects_missing_schedule_field(self, client, cron_storage, schedule) -> None:
        resp = client.post(
            "/api/cron/jobs",
            json={"name": "Test", "action_type": "shell", **schedule},
        )

        assert resp.status_code == 422
        cron_storage.create_job.assert_not_called()


class TestCronGetJob:
    def test_get_job(self, client, cron_storage) -> None:
        cron_storage.get_job.return_value = _make_job()
        resp = client.get("/api/cron/jobs/cj-abc123")
        assert resp.status_code == 200
        assert resp.json()["job"]["id"] == "cj-abc123"

    def test_get_job_not_found(self, client, cron_storage) -> None:
        cron_storage.get_job.return_value = None
        resp = client.get("/api/cron/jobs/cj-nonexistent")
        assert resp.status_code == 404


class TestCronUpdateJob:
    def test_update_job(self, client, cron_storage) -> None:
        cron_storage.update_job.return_value = _make_job(name="Updated")
        resp = client.patch("/api/cron/jobs/cj-abc123", json={"name": "Updated"})
        assert resp.status_code == 200
        assert resp.json()["job"]["name"] == "Updated"

    def test_update_no_fields(self, client, cron_storage) -> None:
        resp = client.patch("/api/cron/jobs/cj-abc123", json={})
        assert resp.status_code == 400

    def test_update_not_found(self, client, cron_storage) -> None:
        cron_storage.update_job.return_value = None
        resp = client.patch("/api/cron/jobs/cj-nonexistent", json={"name": "X"})
        assert resp.status_code == 404

    @pytest.mark.parametrize(
        "payload",
        [
            {"schedule_type": "cron"},
            {"schedule_type": "interval"},
            {"schedule_type": "once"},
            {"schedule_type": "invalid"},
            {"action_type": "invalid"},
        ],
    )
    def test_update_rejects_invalid_schedule_or_action(self, client, cron_storage, payload) -> None:
        resp = client.patch("/api/cron/jobs/cj-abc123", json=payload)

        assert resp.status_code == 422
        cron_storage.update_job.assert_not_called()


class TestCronDeleteJob:
    def test_delete_job(self, client, cron_storage) -> None:
        cron_storage.delete_job.return_value = True
        resp = client.delete("/api/cron/jobs/cj-abc123")
        assert resp.status_code == 200

    def test_delete_not_found(self, client, cron_storage) -> None:
        cron_storage.delete_job.return_value = False
        resp = client.delete("/api/cron/jobs/cj-nonexistent")
        assert resp.status_code == 404


class TestCronToggleJob:
    def test_toggle_job(self, client, cron_storage) -> None:
        cron_storage.toggle_job.return_value = _make_job(enabled=False)
        resp = client.post("/api/cron/jobs/cj-abc123/toggle")
        assert resp.status_code == 200
        assert resp.json()["job"]["enabled"] is False

    def test_toggle_not_found(self, client, cron_storage) -> None:
        cron_storage.toggle_job.return_value = None
        resp = client.post("/api/cron/jobs/cj-nonexistent/toggle")
        assert resp.status_code == 404


@pytest.mark.parametrize(
    ("storage_method", "http_method", "path", "payload"),
    [
        ("update_job", "PATCH", "/api/cron/jobs/cj-system", {"name": "Updated"}),
        ("delete_job", "DELETE", "/api/cron/jobs/cj-system", None),
        ("toggle_job", "POST", "/api/cron/jobs/cj-system/toggle", None),
    ],
)
def test_system_row_protection_returns_403(
    client,
    cron_storage,
    storage_method,
    http_method,
    path,
    payload,
) -> None:
    internal_message = "internal system-row mutation instructions"
    getattr(cron_storage, storage_method).side_effect = SystemRowProtected(internal_message)

    resp = client.request(http_method, path, json=payload)

    assert resp.status_code == 403
    assert resp.json()["detail"] == "System cron job is protected"
    assert internal_message not in resp.text


class TestCronRunNow:
    def test_run_now(self, client, cron_scheduler) -> None:
        cron_scheduler.run_now.return_value = _make_run()
        resp = client.post("/api/cron/jobs/cj-abc123/run")
        assert resp.status_code == 200
        assert resp.json()["run"]["id"] == "cr-run123"

    def test_run_now_not_found(self, client, cron_scheduler, cron_storage) -> None:
        cron_scheduler.run_now.return_value = None
        cron_storage.get_job.return_value = None
        resp = client.post("/api/cron/jobs/cj-nonexistent/run")
        assert resp.status_code == 404

    def test_run_now_active_collision(self, client, cron_scheduler, cron_storage) -> None:
        cron_scheduler.run_now.return_value = None
        cron_storage.get_job.return_value = _make_job()
        resp = client.post("/api/cron/jobs/cj-abc123/run")
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "cron_job_already_running"

    @pytest.mark.parametrize(
        ("error", "status_code"),
        [
            (
                CronRunRejected(
                    "cron_job_already_running",
                    "Cron job already has a running run: cj-abc123",
                ),
                409,
            ),
            (
                CronRunRejected(
                    "cron_max_concurrent_jobs",
                    "Cron scheduler is at max concurrency (1/1)",
                ),
                429,
            ),
        ],
    )
    def test_run_now_rejections(
        self,
        client,
        cron_scheduler,
        error: CronRunRejected,
        status_code: int,
    ) -> None:
        cron_scheduler.run_now.side_effect = error
        resp = client.post("/api/cron/jobs/cj-abc123/run")
        assert resp.status_code == status_code
        assert resp.json()["detail"]["code"] == error.code

    def test_run_now_scheduler_unavailable_does_not_create_run(
        self,
        client,
        http_server,
        cron_storage,
    ) -> None:
        http_server.services.cron_scheduler = None

        resp = client.post("/api/cron/jobs/cj-abc123/run")

        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "cron_scheduler_unavailable"
        cron_storage.create_run.assert_not_called()


class TestCronListRuns:
    def test_list_runs(self, client, cron_storage) -> None:
        cron_storage.get_job.return_value = _make_job()
        cron_storage.list_runs.return_value = [_make_run()]
        resp = client.get("/api/cron/jobs/cj-abc123/runs")
        assert resp.status_code == 200
        assert resp.json()["count"] == 1
        assert resp.json()["runs"][0]["child"] is None

    def test_list_runs_includes_child(self, client, cron_storage) -> None:
        cron_storage.get_job.return_value = _make_job()
        cron_storage.list_runs.return_value = [
            _make_run(
                status="dispatched",
                pipeline_execution_id="pe-child",
                child=CronRunChild(
                    type="pipeline_execution",
                    id="pe-child",
                    status="waiting_approval",
                    terminal=False,
                ),
            )
        ]
        resp = client.get("/api/cron/jobs/cj-abc123/runs")
        assert resp.status_code == 200
        assert resp.json()["runs"][0]["child"] == {
            "type": "pipeline_execution",
            "id": "pe-child",
            "status": "waiting_approval",
            "terminal": False,
            "missing": False,
        }

    def test_list_runs_empty(self, client, cron_storage) -> None:
        cron_storage.list_runs.return_value = []
        resp = client.get("/api/cron/jobs/cj-abc123/runs")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0


class TestCronGetRun:
    def test_get_run(self, client, cron_storage) -> None:
        cron_storage.get_run.return_value = _make_run()
        resp = client.get("/api/cron/runs/cr-run123")
        assert resp.status_code == 200
        assert resp.json()["run"]["status"] == "completed"
        assert resp.json()["run"]["child"] is None

    def test_get_run_not_found(self, client, cron_storage) -> None:
        cron_storage.get_run.return_value = None
        resp = client.get("/api/cron/runs/cr-nonexistent")
        assert resp.status_code == 404
