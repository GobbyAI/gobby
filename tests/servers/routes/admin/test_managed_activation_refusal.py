"""Daemon activation routes refuse run-scoped agent credentials and admit the operator."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import APIRouter, FastAPI
from starlette.requests import HTTPConnection
from starlette.testclient import TestClient

from gobby.releases import activation
from gobby.servers.auth_service import AuthService
from gobby.servers.http import HTTPServer
from gobby.servers.routes.admin import _lifecycle
from gobby.servers.routes.admin._lifecycle import register_lifecycle_routes
from gobby.utils.local_token import AgentApiTokenClaims

pytestmark = pytest.mark.unit

AGENT_HEADERS = {"Authorization": "Bearer gobby-agent-v1.synthetic"}
CLAIMS = AgentApiTokenClaims(
    "synthetic-session",
    "synthetic-project",
    "synthetic-machine",
    1,
    9999999999,
    agent_run_id="synthetic-run",
)


HUB_DATABASE = object()


class FakeServer:
    def __init__(self, auth_service: AuthService) -> None:
        self.auth_service = auth_service
        self.services = SimpleNamespace(database=HUB_DATABASE)

    def get_runner(self) -> None:
        return None

    async def run_db(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)


@pytest.fixture
def stops(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    monkeypatch.setattr(_lifecycle, "_restart_lock", None)
    monkeypatch.setattr(_lifecycle, "require_machine_id", lambda: "machine")
    monkeypatch.setattr(_lifecycle, "_prepare_handoff_shutdown", AsyncMock())
    monkeypatch.setattr(_lifecycle, "_restart_start_refusal", lambda: None)
    monkeypatch.setattr("gobby.runner_maintenance.write_shutdown_source", MagicMock())
    request = MagicMock(return_value=True)
    monkeypatch.setattr(_lifecycle, "_request_runner_shutdown", request)
    return request


def _client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    break_glass: bool,
    holds: bool = False,
    verified: bool = True,
) -> TestClient:
    def claims_for(_self: AuthService, _request: HTTPConnection, _token: str) -> object:
        return CLAIMS if verified else None

    def admits(_self: AuthService, _request: HTTPConnection) -> bool:
        return break_glass

    monkeypatch.setattr(AuthService, "_verified_agent_claims_for_token", claims_for)
    monkeypatch.setattr(AuthService, "break_glass_admits", admits)
    monkeypatch.setattr(activation, "session_has_capability", lambda _db, _sid, _cap: holds)
    auth = AuthService(MagicMock())
    router = APIRouter(prefix="/api/admin")
    register_lifecycle_routes(router, cast(HTTPServer, FakeServer(auth)))
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.mark.parametrize("path", ["/api/admin/restart", "/api/admin/shutdown"])
def test_agent_bearer_is_refused_before_any_stop(
    monkeypatch: pytest.MonkeyPatch, stops: MagicMock, path: str
) -> None:
    response = _client(monkeypatch, break_glass=False).post(path, headers=AGENT_HEADERS)

    assert response.status_code == 403
    assert "managed_activation_refused" in response.json()["detail"]
    stops.assert_not_called()


@pytest.mark.parametrize("path", ["/api/admin/restart", "/api/admin/shutdown"])
def test_operator_credential_still_activates(
    monkeypatch: pytest.MonkeyPatch, stops: MagicMock, path: str
) -> None:
    response = _client(monkeypatch, break_glass=True).post(path)

    assert response.status_code == 200
    stops.assert_called_once()


RESTART_INTENT_PATHS = ["/api/admin/restart", "/api/admin/shutdown?restart_supervisor=true"]


@pytest.mark.parametrize("path", RESTART_INTENT_PATHS)
def test_verified_holder_is_admitted_for_restart_intent(
    monkeypatch: pytest.MonkeyPatch, stops: MagicMock, path: str
) -> None:
    response = _client(monkeypatch, break_glass=False, holds=True).post(path, headers=AGENT_HEADERS)

    assert response.status_code == 200
    stops.assert_called_once()


def test_verified_holder_is_refused_for_plain_shutdown(
    monkeypatch: pytest.MonkeyPatch, stops: MagicMock
) -> None:
    response = _client(monkeypatch, break_glass=False, holds=True).post(
        "/api/admin/shutdown", headers=AGENT_HEADERS
    )

    assert response.status_code == 403
    assert "managed_activation_refused" in response.json()["detail"]
    stops.assert_not_called()


@pytest.mark.parametrize("path", RESTART_INTENT_PATHS)
def test_managed_definition_without_the_capability_is_refused(
    monkeypatch: pytest.MonkeyPatch, stops: MagicMock, path: str
) -> None:
    response = _client(monkeypatch, break_glass=False, holds=False).post(
        path, headers=AGENT_HEADERS
    )

    assert response.status_code == 403
    stops.assert_not_called()


@pytest.mark.parametrize("path", RESTART_INTENT_PATHS)
def test_rejected_managed_credential_is_refused_even_for_a_holder(
    monkeypatch: pytest.MonkeyPatch, stops: MagicMock, path: str
) -> None:
    response = _client(monkeypatch, break_glass=False, holds=True, verified=False).post(
        path, headers=AGENT_HEADERS
    )

    assert response.status_code == 403
    assert "rejected" in response.json()["detail"]
    stops.assert_not_called()
