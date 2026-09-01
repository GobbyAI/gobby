from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from gobby.servers.routes.sessions import changes as changes_routes
from gobby.servers.session_changes import SessionWorkspace
from gobby.storage.project_checkouts import CheckoutNotFoundError
from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

pytestmark = pytest.mark.unit


def _client_with_resolver(monkeypatch: pytest.MonkeyPatch, resolver: object) -> TestClient:
    monkeypatch.setattr(changes_routes, "resolve_session_workspace", resolver)
    app = FastAPI()
    router = APIRouter(prefix="/api/sessions")
    server = cast("HTTPServer", SimpleNamespace(session_manager=object(), task_manager=object()))
    changes_routes.register_changes_routes(router, server)
    app.include_router(router)
    return TestClient(app)


def _raise(exc: Exception) -> None:
    raise exc


def test_session_changes_without_local_checkout_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client_with_resolver(
        monkeypatch,
        lambda **_kwargs: _raise(CheckoutNotFoundError("no checkout for machine m project p")),
    )

    response = client.get("/api/sessions/session-1/changes")

    assert response.status_code == 404
    assert response.json()["detail"] == {
        "error": "CheckoutNotFoundError",
        "message": "No checkout for this session's project on this machine",
    }


def test_session_change_diff_for_foreign_machine_session_is_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mismatch = MachineOwnershipMismatchError(
        resource_kind="project_checkout",
        resource_id="proj-1",
        owner_machine_id="machine-owner",
        current_machine_id="machine-local",
    )
    client = _client_with_resolver(monkeypatch, lambda **_kwargs: _raise(mismatch))

    response = client.get("/api/sessions/session-1/changes/diff", params={"path": "src/app.py"})

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "error": "MachineOwnershipMismatchError",
        "message": str(mismatch),
    }


def test_session_change_diff_runtime_error_returns_empty_diff_with_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    workspace = SessionWorkspace(
        working_dir=str(tmp_path),
        base_ref="HEAD",
        isolation="none",
    )
    monkeypatch.setattr(
        changes_routes,
        "resolve_session_workspace",
        lambda **_kwargs: workspace,
    )
    monkeypatch.setattr(
        changes_routes,
        "compute_session_file_diff",
        AsyncMock(side_effect=RuntimeError("boom")),
    )

    app = FastAPI()
    router = APIRouter(prefix="/api/sessions")
    server = SimpleNamespace(session_manager=object(), task_manager=object())
    changes_routes.register_changes_routes(router, server)
    app.include_router(router)
    client = TestClient(app)
    caplog.set_level(logging.WARNING, logger="gobby.servers.routes.sessions.changes")

    response = client.get(
        "/api/sessions/session-1/changes/diff",
        params={"path": "src/app.py"},
    )

    assert response.status_code == 200
    assert response.json() == {"diff": "", "path": "src/app.py", "error": "boom"}
    assert "Failed to compute session file diff" in caplog.text
