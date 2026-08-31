"""HTTP contract for the session-feedback review routes."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.app_context import ServiceContainer
from gobby.feedback.storage import FeedbackReviewRun
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

    response = _client(service).post("/feedback/review", json={"dry_run": True})

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["run_id"] == "run-1"
    service.run_review.assert_awaited_once_with(dry_run=True)


def test_review_post_failure_maps_to_500_with_detail() -> None:
    service = MagicMock()
    service.run_review = AsyncMock(side_effect=RuntimeError("provider unavailable"))

    response = _client(service).post("/feedback/review", json={})

    assert response.status_code == 500
    assert response.json()["detail"] == "provider unavailable"


def test_service_container_declares_the_attribute_the_routes_read() -> None:
    """The routes resolve server.services.feedback_review_service, so the
    real ServiceContainer must declare that field (regression: #21309)."""
    field_names = {field.name for field in dataclasses.fields(ServiceContainer)}
    assert "feedback_review_service" in field_names


def test_routes_return_503_when_service_unavailable() -> None:
    client = _client(None)

    assert client.post("/feedback/review", json={}).status_code == 503
    assert client.get("/feedback/review/latest").status_code == 503
    assert client.get("/feedback/review/run-1").status_code == 503


def test_latest_returns_run_payload_and_404_when_empty() -> None:
    service = MagicMock()
    service.store.latest_run.return_value = _run()

    response = _client(service).get("/feedback/review/latest")
    assert response.status_code == 200
    run = response.json()["run"]
    assert run["id"] == "run-1"
    assert run["digest_md"] == "# Digest"

    service.store.latest_run.return_value = None
    assert _client(service).get("/feedback/review/latest").status_code == 404


def test_run_by_id_returns_run_and_404_for_unknown() -> None:
    service = MagicMock()
    service.store.get_run.return_value = _run("run-9")

    response = _client(service).get("/feedback/review/run-9")
    assert response.status_code == 200
    assert response.json()["run"]["id"] == "run-9"
    service.store.get_run.assert_called_once_with("run-9")

    service.store.get_run.return_value = None
    assert _client(service).get("/feedback/review/missing").status_code == 404
