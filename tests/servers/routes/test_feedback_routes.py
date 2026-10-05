"""HTTP contract for the session-feedback review routes."""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.app_context import ServiceContainer
from gobby.feedback.cron import FEEDBACK_REVIEW_CRON_JOB_NAME
from gobby.feedback.storage import FeedbackEntry, FeedbackReviewRun
from gobby.servers.routes.feedback import create_feedback_router

pytestmark = pytest.mark.unit

_T0 = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)


def _run(run_id: str = "run-1", *, digest: str | None = "# Digest") -> FeedbackReviewRun:
    return FeedbackReviewRun(
        id=run_id,
        status="completed",
        dry_run=False,
        window_start=_T0,
        window_end=_T0,
        rows_considered=2,
        findings={"clusters": []},
        actions={"filed": []},
        digest_md=digest,
        error=None,
        created_at=_T0,
        completed_at=_T0,
    )


def _client(service: object | None) -> TestClient:
    server = MagicMock()
    server.services = SimpleNamespace(feedback_review_service=service)
    app = FastAPI()
    app.include_router(create_feedback_router(server))
    return TestClient(app, raise_server_exceptions=False)


def test_review_post_runs_review_and_returns_result() -> None:
    service = MagicMock()
    service.run_review = AsyncMock(
        return_value={
            "status": "completed",
            "run_id": "run-1",
            "dry_run": True,
            "rows_considered": 2,
            "tasks_filed": 1,
            "deduplicated": 0,
        }
    )

    response = _client(service).post("/api/feedback/review", json={"dry_run": True})

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["run_id"] == "run-1"
    service.run_review.assert_awaited_once_with(dry_run=True)


def test_review_post_failure_maps_to_500_with_detail() -> None:
    service = MagicMock()
    service.run_review = AsyncMock(side_effect=RuntimeError("provider unavailable"))

    response = _client(service).post("/api/feedback/review", json={})

    assert response.status_code == 500
    assert response.json()["detail"] == "provider unavailable"


def test_service_container_declares_the_attribute_the_routes_read() -> None:
    """The routes resolve server.services.feedback_review_service, so the
    real ServiceContainer must declare that field (regression: #21309)."""
    field_names = {field.name for field in dataclasses.fields(ServiceContainer)}
    assert "feedback_review_service" in field_names


def test_routes_return_503_when_service_unavailable() -> None:
    client = _client(None)

    assert client.post("/api/feedback/review", json={}).status_code == 503
    assert client.get("/api/feedback/review/latest").status_code == 503
    assert client.get("/api/feedback/review/run-1").status_code == 503


def test_latest_returns_run_payload_and_404_when_empty() -> None:
    service = MagicMock()
    service.store.latest_run.return_value = _run()

    response = _client(service).get("/api/feedback/review/latest")
    assert response.status_code == 200
    run = response.json()["run"]
    assert run["id"] == "run-1"
    assert run["digest_md"] == "# Digest"

    service.store.latest_run.return_value = None
    assert _client(service).get("/api/feedback/review/latest").status_code == 404


def test_run_by_id_returns_run_and_404_for_unknown() -> None:
    service = MagicMock()
    service.store.get_run.return_value = _run("run-9")

    response = _client(service).get("/api/feedback/review/run-9")
    assert response.status_code == 200
    assert response.json()["run"]["id"] == "run-9"
    service.store.get_run.assert_called_once_with("run-9")

    service.store.get_run.return_value = None
    assert _client(service).get("/api/feedback/review/missing").status_code == 404


@pytest.mark.parametrize("reader", ["observations", "results"])
def test_evidence_routes_forward_bounds_and_reject_invalid_pages(reader: str) -> None:
    service = MagicMock()
    method = getattr(service.store, f"{reader}_page")
    method.return_value = {"run_id": "run-9", "next_offset": 3}
    client = _client(service)
    response = client.get(f"/api/feedback/review/run-9/{reader}?offset=2&limit=1")
    assert response.status_code == 200
    assert response.json()["next_offset"] == 3
    method.assert_called_once_with("run-9", offset=2, limit=1)
    method.side_effect = ValueError("offset must be nonnegative")
    invalid = client.get(f"/api/feedback/review/run-9/{reader}?offset=-1")
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "offset must be nonnegative"


def _entry(entry_id: str = "fb-1") -> FeedbackEntry:
    return FeedbackEntry(
        id=entry_id,
        session_id="session-1",
        source="survey",
        kind="bug",
        kind_other_label=None,
        evidence="evidence",
        impact="impact",
        frequency="always",
        suggestion=None,
        disposition="fixed",
        created_at=_T0,
        reviewed=False,
        review_run_id=None,
    )


def test_entries_forward_filters_and_reject_invalid_bounds() -> None:
    service = MagicMock()
    service.store.list_feedback.return_value = [_entry()]
    client = _client(service)

    response = client.get(
        "/api/feedback/entries?limit=5&unreviewed=true&kind=bug&frequency=always&disposition=fixed"
    )

    assert response.status_code == 200
    assert [entry["id"] for entry in response.json()["entries"]] == ["fb-1"]
    service.store.list_feedback.assert_called_once_with(
        limit=5, unreviewed=True, kind="bug", frequency="always", disposition="fixed"
    )
    unknown = client.get("/api/feedback/entries?frequency=sometimes")
    assert unknown.status_code == 422
    assert unknown.json()["detail"] == "frequency must be one of once, repeated, always"
    assert service.store.list_feedback.call_count == 1
    service.store.list_feedback.side_effect = ValueError("limit must be between 1 and 100")
    invalid = client.get("/api/feedback/entries?limit=0")
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "limit must be between 1 and 100"


def test_runs_list_recent_runs_newest_first() -> None:
    service = MagicMock()
    service.store.list_runs.return_value = [_run("run-2"), _run("run-1")]

    response = _client(service).get("/api/feedback/runs?limit=2")

    assert response.status_code == 200
    assert [run["id"] for run in response.json()["runs"]] == ["run-2", "run-1"]
    assert all("observations" not in run for run in response.json()["runs"])
    service.store.list_runs.assert_called_once_with(limit=2)


def test_status_reports_backlog_latest_run_and_schedule() -> None:
    service = MagicMock()
    service.store.backlog_count.return_value = 7
    service.store.list_runs.return_value = [_run("run-3")]
    job = SimpleNamespace(
        enabled=True,
        cron_expr="0 3 * * *",
        timezone="America/Chicago",
        next_run_at=_T0.isoformat(),
        last_status="completed",
    )
    cron_storage = MagicMock()
    cron_storage.get_job_by_name.return_value = job
    server = MagicMock()
    server.services = SimpleNamespace(feedback_review_service=service, cron_storage=cron_storage)
    app = FastAPI()
    app.include_router(create_feedback_router(server))
    client = TestClient(app, raise_server_exceptions=False)

    body = client.get("/api/feedback/status").json()

    assert body["backlog"] == 7
    assert body["latest_run"]["id"] == "run-3"
    assert "observations" not in body["latest_run"]
    assert body["schedule"] == {
        "enabled": True,
        "cron_expr": "0 3 * * *",
        "timezone": "America/Chicago",
        "next_run_at": _T0.isoformat(),
        "last_status": "completed",
    }
    cron_storage.get_job_by_name.assert_called_once_with(FEEDBACK_REVIEW_CRON_JOB_NAME)
    service.store.list_runs.assert_called_once_with(limit=1)

    cron_storage.get_job_by_name.return_value = None
    service.store.list_runs.return_value = []
    empty = client.get("/api/feedback/status").json()
    assert empty["schedule"] is None
    assert empty["latest_run"] is None


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class _ThreadRecordingStore:
    """Feedback store fake that records whether each call ran on the event loop."""

    def __init__(self) -> None:
        self.calls: dict[str, bool] = {}

    def _record(self, name: str) -> None:
        self.calls[name] = _on_event_loop()

    def list_feedback(self, **_filters: object) -> list[FeedbackEntry]:
        self._record("list_feedback")
        return [_entry()]

    def list_runs(self, *, limit: int) -> list[FeedbackReviewRun]:
        self._record("list_runs")
        return [_run()][:limit]

    def backlog_count(self) -> int:
        self._record("backlog_count")
        return 3

    def latest_run(self) -> FeedbackReviewRun:
        self._record("latest_run")
        return _run()

    def get_run(self, run_id: str) -> FeedbackReviewRun:
        self._record("get_run")
        return _run(run_id)

    def observations_page(self, run_id: str, *, offset: int, limit: int) -> dict[str, object]:
        self._record("observations_page")
        return {"success": True, "run_id": run_id, "offset": offset, "limit": limit}

    def results_page(self, run_id: str, *, offset: int, limit: int) -> dict[str, object]:
        self._record("results_page")
        return {"success": True, "run_id": run_id, "offset": offset, "limit": limit}

    def get_job_by_name(self, _name: str) -> None:
        self._record("get_job_by_name")


@pytest.mark.parametrize(
    ("path", "expected_calls"),
    [
        ("/api/feedback/entries", {"list_feedback"}),
        ("/api/feedback/runs", {"list_runs"}),
        ("/api/feedback/status", {"list_runs", "backlog_count", "get_job_by_name"}),
        ("/api/feedback/review/latest", {"latest_run"}),
        ("/api/feedback/review/run-1", {"get_run"}),
        ("/api/feedback/review/run-1/observations", {"observations_page"}),
        ("/api/feedback/review/run-1/results", {"results_page"}),
    ],
)
def test_storage_calls_run_off_the_event_loop(path: str, expected_calls: set[str]) -> None:
    store = _ThreadRecordingStore()
    server = MagicMock()
    server.services = SimpleNamespace(
        feedback_review_service=SimpleNamespace(store=store), cron_storage=store
    )
    app = FastAPI()
    app.include_router(create_feedback_router(server))
    client = TestClient(app, raise_server_exceptions=False)

    assert client.get(path).status_code == 200
    assert set(store.calls) == expected_calls
    assert not any(store.calls.values()), f"storage ran on the event loop: {store.calls}"
