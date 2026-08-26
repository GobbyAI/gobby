"""Restart-protected cron run reporting for the CLI stop/restart gate (#21021)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from gobby.servers.http import HTTPServer
from gobby.servers.routes.admin._lifecycle import register_lifecycle_routes

pytestmark = pytest.mark.unit

PROTECTED_RUN = {
    "run_id": "run-1",
    "job_id": "job-1",
    "job_name": "gobby:memory-dream",
    "started_at": "2026-08-26T07:00:00+00:00",
    "elapsed_seconds": 3725.0,
    "remaining_seconds": 12475.0,
}


@dataclass
class FakeScheduler:
    runs: list[dict[str, Any]] = field(default_factory=list)
    calls: int = 0

    def list_protected_runs(self) -> list[dict[str, Any]]:
        self.calls += 1
        return list(self.runs)


@dataclass
class FakeRunner:
    cron_scheduler: FakeScheduler | None


class FakeServer:
    def __init__(self, runner: FakeRunner | None) -> None:
        self._runner = runner

    def get_runner(self) -> FakeRunner | None:
        return self._runner

    async def run_db(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)


def _client(runner: FakeRunner | None) -> TestClient:
    router = APIRouter(prefix="/api/admin")
    register_lifecycle_routes(router, cast(HTTPServer, FakeServer(runner)))
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_protected_runs_reports_scheduler_leases() -> None:
    scheduler = FakeScheduler(runs=[PROTECTED_RUN])
    client = _client(FakeRunner(scheduler))

    response = client.get("/api/admin/cron/protected-runs")

    assert response.status_code == 200
    assert response.json() == {"runs": [PROTECTED_RUN]}
    assert scheduler.calls == 1


def test_protected_runs_is_empty_without_a_scheduler() -> None:
    client = _client(FakeRunner(cron_scheduler=None))

    response = client.get("/api/admin/cron/protected-runs")

    assert response.status_code == 200
    assert response.json() == {"runs": []}


def test_protected_runs_is_unavailable_without_a_runner() -> None:
    client = _client(None)

    response = client.get("/api/admin/cron/protected-runs")

    assert response.status_code == 503
    assert response.json() == {"detail": "runner unavailable"}
