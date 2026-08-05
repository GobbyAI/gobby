"""Active-runtime lease status and cooperative handoff routes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from gobby.daemon_lease import DaemonLeaseStatus
from gobby.servers.http import HTTPServer
from gobby.servers.routes.admin._lease import register_lease_routes


@dataclass
class FakeAgentRunner:
    running: int

    def get_running_agents_count(self) -> int:
        return self.running


class FakeLease:
    def status(self) -> DaemonLeaseStatus:
        return DaemonLeaseStatus(
            held=True,
            owner_pid=123,
            owner_application_name="gobby-lease-v1:machine:instance",
            heartbeat_age_seconds=0.1,
        )


class FakeRunner:
    def __init__(self, running_agents: int) -> None:
        self.agent_runner = FakeAgentRunner(running_agents)
        self.daemon_lease = FakeLease()
        self.shutdown_requested = False

    def request_shutdown(self) -> None:
        self.shutdown_requested = True


class FakeServer:
    def __init__(self, runner: FakeRunner) -> None:
        self.runner = runner

    def get_runner(self) -> FakeRunner:
        return self.runner

    async def run_db(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)


def _client(running_agents: int) -> tuple[TestClient, FakeRunner]:
    runner = FakeRunner(running_agents)
    router = APIRouter(prefix="/api/admin")
    register_lease_routes(router, cast(HTTPServer, FakeServer(runner)))
    app = FastAPI()
    app.include_router(router)
    return TestClient(app), runner


def test_active_lease_status_reports_holder() -> None:
    client, _runner = _client(0)

    response = client.get("/api/admin/lease/status")

    assert response.status_code == 200
    assert response.json()["mode"] == "active"
    assert response.json()["owner_pid"] == 123


def test_handoff_refuses_while_local_agents_are_running() -> None:
    client, runner = _client(2)

    response = client.post("/api/admin/lease/handoff")

    assert response.status_code == 409
    assert response.json()["detail"]["blockers"] == {"active_agent_runs": 2}
    assert runner.shutdown_requested is False


def test_handoff_requests_shutdown_after_quiescence() -> None:
    client, runner = _client(0)

    response = client.post("/api/admin/lease/handoff")

    assert response.status_code == 200
    assert response.json() == {"handoff": "accepted"}
    assert runner.shutdown_requested is True
