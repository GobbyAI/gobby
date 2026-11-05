"""The daemon admits a signed restart only for a verified, live, activating seat, and only once."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from gobby.releases import activation
from gobby.runtime_grants.signed_request import (
    SIGNED_MAC_HEADER,
    SIGNED_NONCE_HEADER,
    SIGNED_PAYLOAD_HEADER,
    derive_signed_request_key,
    signed_request_mac,
    signed_request_nonces,
)
from gobby.servers.auth_service import AuthService
from gobby.servers.middleware import auth as middleware_auth
from gobby.servers.middleware.auth import AuthMiddleware
from gobby.servers.routes.admin import _lifecycle as lifecycle_routes
from gobby.servers.routes.signed_request import create_signed_request_router
from gobby.utils.local_token import derive_managed_signing_key, issue_agent_api_token

pytestmark = pytest.mark.unit

MACHINE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SIGNING_KEY = derive_managed_signing_key("operator-token")
TOKEN = issue_agent_api_token(
    SIGNING_KEY,
    agent_run_id="exec-1",
    session_id="session-1",
    project_id="project-1",
    machine_id=MACHINE_ID,
    timeout_seconds=600,
)
_VERSION, PAYLOAD, SIGNATURE_TEXT = TOKEN.split(".")
SIGNATURE = base64.urlsafe_b64decode(SIGNATURE_TEXT + "=" * (-len(SIGNATURE_TEXT) % 4))
DERIVED_KEY = derive_signed_request_key(SIGNATURE)
RESTART = "/api/admin/restart"
PROTECTED_RUNS = "/api/admin/cron/protected-runs"


def _signed_headers(
    nonce: str,
    *,
    operation: str = "restart",
    method: str = "POST",
    path: str = RESTART,
    query: tuple[tuple[str, str], ...] = (),
    body: bytes = b"",
    payload: str = PAYLOAD,
) -> dict[str, str]:
    mac = signed_request_mac(
        DERIVED_KEY,
        operation=operation,
        method=method,
        path=path,
        query=query,
        body=body,
        nonce=nonce,
        encoded_payload=payload,
    )
    return {
        SIGNED_PAYLOAD_HEADER: payload,
        SIGNED_NONCE_HEADER: nonce,
        SIGNED_MAC_HEADER: mac,
    }


def _fresh_nonce() -> str:
    nonce = signed_request_nonces.issue(PAYLOAD)
    assert nonce is not None
    return nonce


@pytest.fixture(autouse=True)
def _isolated_nonces() -> None:
    signed_request_nonces.reset()


class FakeServer:
    """The server surface the middleware and the lifecycle routes read."""

    def __init__(self, auth: AuthService) -> None:
        self.auth_service = auth
        self.services = SimpleNamespace(database=object(), http_admission_closed=False)
        self._background_tasks: set[Any] = set()

    async def run_db(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    def get_runner(self) -> SimpleNamespace:
        return SimpleNamespace(cron_scheduler=None)


@pytest.fixture
def stops(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    monkeypatch.setattr(lifecycle_routes, "_restart_lock", None)
    monkeypatch.setattr(lifecycle_routes, "require_machine_id", lambda: "machine")
    monkeypatch.setattr(lifecycle_routes, "_prepare_handoff_shutdown", AsyncMock())
    monkeypatch.setattr(lifecycle_routes, "_restart_start_refusal", lambda: None)
    monkeypatch.setattr("gobby.runner_maintenance.write_shutdown_source", MagicMock())
    request = MagicMock(return_value=True)
    monkeypatch.setattr(lifecycle_routes, "_request_runner_shutdown", request)
    return request


@pytest.fixture
def holds(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Whether the installed definition holds activate_daemon, and every session checked."""
    state: dict[str, Any] = {"value": True, "checked": []}

    def session_has_capability(_db: object, session_id: str, _capability: str) -> bool:
        state["checked"].append(session_id)
        return bool(state["value"])

    monkeypatch.setattr(activation, "session_has_capability", session_has_capability)
    return state


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch,
    stops: MagicMock,
    holds: dict[str, Any],
) -> TestClient:
    monkeypatch.setattr(AuthService, "managed_signing_key", lambda _self: SIGNING_KEY)
    monkeypatch.setattr(AuthService, "_managed_capability_is_live", lambda _self, _claims: True)
    monkeypatch.setattr(middleware_auth, "require_hub_database", lambda _db: object())
    server = FakeServer(AuthService(MagicMock()))
    router = APIRouter(prefix="/api/admin")
    lifecycle_routes.register_lifecycle_routes(router, cast(Any, server))
    app = FastAPI()
    app.add_middleware(AuthMiddleware, server=cast(Any, server))
    app.include_router(router)
    app.include_router(create_signed_request_router())
    return TestClient(app)


def test_signed_restart_by_an_activating_seat_stops_the_daemon_once(
    client: TestClient, stops: MagicMock
) -> None:
    response = client.post(RESTART, headers=_signed_headers(_fresh_nonce()))

    assert response.status_code == 200
    stops.assert_called_once()


def test_capability_check_reads_the_session_from_the_verified_payload(
    client: TestClient, holds: dict[str, Any]
) -> None:
    client.post(
        RESTART,
        headers={
            **_signed_headers(_fresh_nonce()),
            "X-Gobby-Session-Id": "forged-session",
        },
    )

    assert holds["checked"] == ["session-1"]


def test_signed_restart_by_a_non_activating_seat_is_refused_before_any_stop(
    client: TestClient, holds: dict[str, Any], stops: MagicMock
) -> None:
    holds["value"] = False

    response = client.post(RESTART, headers=_signed_headers(_fresh_nonce()))

    assert response.status_code == 403
    assert "managed_activation_refused" in response.text
    stops.assert_not_called()


def test_restart_without_signed_headers_is_refused_as_missing_auth(
    client: TestClient, stops: MagicMock
) -> None:
    response = client.post(RESTART)

    assert response.status_code == 401
    assert response.json()["code"] == "missing_auth"
    stops.assert_not_called()


def test_a_replayed_nonce_is_refused(client: TestClient, stops: MagicMock) -> None:
    headers = _signed_headers(_fresh_nonce())
    first = client.post(RESTART, headers=headers)
    second = client.post(RESTART, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 401
    assert second.json()["code"] == "signed_request_rejected"
    stops.assert_called_once()


def test_a_tampered_force_flag_is_refused(client: TestClient, stops: MagicMock) -> None:
    headers = _signed_headers(_fresh_nonce())

    response = client.post(f"{RESTART}?force=true&terminals=false", headers=headers)

    assert response.status_code == 401
    assert response.json()["code"] == "signed_request_rejected"
    stops.assert_not_called()


def test_a_protected_runs_signature_cannot_authorize_a_restart(
    client: TestClient, stops: MagicMock
) -> None:
    headers = _signed_headers(
        _fresh_nonce(),
        operation="protected-runs",
        method="GET",
        path=PROTECTED_RUNS,
        query=(),
    )

    response = client.post(RESTART, headers=headers)

    assert response.status_code == 401
    stops.assert_not_called()


def test_a_bad_mac_does_not_consume_the_nonce(client: TestClient, stops: MagicMock) -> None:
    nonce = _fresh_nonce()
    forged = {**_signed_headers(nonce), SIGNED_MAC_HEADER: "0" * 64}

    refused = client.post(RESTART, headers=forged)
    admitted = client.post(RESTART, headers=_signed_headers(nonce))

    assert refused.status_code == 401
    assert admitted.status_code == 200
    stops.assert_called_once()


def test_a_run_that_is_no_longer_live_is_refused(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, stops: MagicMock
) -> None:
    monkeypatch.setattr(AuthService, "_managed_capability_is_live", lambda _self, _claims: False)

    response = client.post(RESTART, headers=_signed_headers(_fresh_nonce()))

    assert response.status_code == 401
    stops.assert_not_called()


def test_signed_protected_runs_preflight_is_admitted_without_a_bearer(client: TestClient) -> None:
    headers = _signed_headers(
        _fresh_nonce(),
        operation="protected-runs",
        method="GET",
        path=PROTECTED_RUNS,
        query=(),
    )

    response = client.get(PROTECTED_RUNS, headers=headers)

    assert response.status_code == 200
    assert response.json() == {"runs": []}


def test_nonce_route_is_reachable_without_credentials(client: TestClient) -> None:
    response = client.post("/api/runtime/signed-request/nonce", json={"payload": PAYLOAD})

    assert response.status_code == 200
    assert isinstance(response.json()["nonce"], str)
    assert response.json()["nonce"]


def test_nonce_route_issues_a_distinct_nonce_per_request(client: TestClient) -> None:
    first = client.post("/api/runtime/signed-request/nonce", json={"payload": PAYLOAD})
    second = client.post("/api/runtime/signed-request/nonce", json={"payload": PAYLOAD})

    assert first.json()["nonce"] != second.json()["nonce"]


def test_nonce_route_refuses_a_payload_that_is_not_a_capability_payload(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/runtime/signed-request/nonce", json={"payload": "not-a-capability"}
    )

    assert response.status_code == 400


def test_nonce_route_refuses_when_the_store_is_full(client: TestClient) -> None:
    for _ in range(256):
        _fresh_nonce()

    response = client.post("/api/runtime/signed-request/nonce", json={"payload": PAYLOAD})

    assert response.status_code == 503
