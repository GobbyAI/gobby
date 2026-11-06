"""Daemon activation routes refuse run-scoped agent credentials and admit the operator."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import APIRouter, FastAPI
from starlette.requests import HTTPConnection
from starlette.testclient import TestClient

from gobby.servers.auth_service import AuthService
from gobby.servers.http import HTTPServer
from gobby.servers.routes.admin import _lifecycle
from gobby.servers.routes.admin._lifecycle import register_lifecycle_routes
from gobby.utils.local_token import AgentApiTokenClaims

pytestmark = pytest.mark.unit

AGENT_HEADERS = {"Authorization": "Bearer gobby-agent-v1.synthetic"}
CLAIMS = AgentApiTokenClaims(
    "synthetic-session", "synthetic-project", "synthetic-machine", 1, 9999999999
)


class FakeServer:
    def __init__(self, auth_service: AuthService) -> None:
        self.auth_service = auth_service

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


def _client(monkeypatch: pytest.MonkeyPatch, *, break_glass: bool) -> TestClient:
    def claims_for(_self: AuthService, _request: HTTPConnection, _token: str) -> object:
        return CLAIMS

    def admits(_self: AuthService, _request: HTTPConnection) -> bool:
        return break_glass

    monkeypatch.setattr(AuthService, "_verified_agent_claims_for_token", claims_for)
    monkeypatch.setattr(AuthService, "break_glass_admits", admits)
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
