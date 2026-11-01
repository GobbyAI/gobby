"""Host and Origin policy for the MCP HTTP route when a proxy forwards to loopback."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import HTTPConnection

from gobby.app_context import ServiceContainer
from gobby.config.app import DaemonConfig
from gobby.config.bootstrap import BootstrapConfig
from gobby.servers.app_factory import _mcp_transport_security
from gobby.servers.auth_service import AuthService
from gobby.servers.grant_auth import AuthDecision
from gobby.servers.http import HTTPServer
from gobby.storage.sessions import SessionManager
from tests.servers.conftest import TEST_API_KEY

pytestmark = pytest.mark.unit

TAILNET_HOST = "mbp.tail4125a0.ts.net"
LOOPBACK_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
LOOPBACK_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
MCP_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "host-allowlist-test", "version": "0"},
    },
}


def test_loopback_bind_without_extra_hosts_keeps_sdk_loopback_policy() -> None:
    settings = _mcp_transport_security("127.0.0.1", [])

    assert settings is not None
    assert settings.enable_dns_rebinding_protection is True
    assert settings.allowed_hosts == LOOPBACK_HOSTS
    assert settings.allowed_origins == LOOPBACK_ORIGINS


def test_configured_hosts_extend_loopback_policy_with_https_origins() -> None:
    settings = _mcp_transport_security("localhost", [TAILNET_HOST, f"{TAILNET_HOST}:8443"])

    assert settings is not None
    assert settings.enable_dns_rebinding_protection is True
    assert settings.allowed_hosts == [*LOOPBACK_HOSTS, TAILNET_HOST, f"{TAILNET_HOST}:8443"]
    assert settings.allowed_origins == [
        *LOOPBACK_ORIGINS,
        f"https://{TAILNET_HOST}",
        f"https://{TAILNET_HOST}:8443",
    ]


@pytest.mark.parametrize("bind_host", ["0.0.0.0", "100.100.1.2", "::"])
def test_non_loopback_bind_keeps_sdk_default_without_host_check(bind_host: str) -> None:
    assert _mcp_transport_security(bind_host, [TAILNET_HOST]) is None


def _bearer_gate(_service: AuthService, request: HTTPConnection) -> AuthDecision:
    """Boundary fake for credential verification: admits only the test bearer.

    Token verification has its own suites; these tests pin the Host and Origin policy
    that runs behind it.
    """
    if request.headers.get("Authorization") == f"Bearer {TEST_API_KEY}":
        return AuthDecision(allowed=True, break_glass=True)
    return AuthDecision(allowed=False, code="missing_auth", status_code=401)


def _post_initialize(
    session_storage: SessionManager,
    allowed_hosts: list[str],
    host: str,
    *,
    bearer: bool = True,
    origin: str | None = None,
) -> httpx.Response:
    services = ServiceContainer(
        database=session_storage.db,
        session_manager=session_storage,
        task_manager=MagicMock(),
        mcp_manager=MagicMock(),
    )
    server = HTTPServer(
        services=services,
        startup_config=DaemonConfig(mcp_allowed_hosts=allowed_hosts),
        port=60887,
        test_mode=True,
        bootstrap_config=BootstrapConfig(),
    )
    headers = {"Host": host, "Accept": "application/json, text/event-stream"}
    if bearer:
        headers["Authorization"] = f"Bearer {TEST_API_KEY}"
    if origin is not None:
        headers["Origin"] = origin

    with (
        patch("gobby.servers.app_factory.HookManager") as hook_manager_cls,
        patch.object(AuthService, "authenticate", _bearer_gate),
    ):
        hook_manager = hook_manager_cls.return_value
        hook_manager._stop_registry = MagicMock()
        hook_manager.shutdown = MagicMock()
        hook_manager.shutdown_async = AsyncMock()
        with TestClient(server.app) as client:
            return client.post("/mcp", json=MCP_INITIALIZE, headers=headers)


def test_listed_host_with_bearer_reaches_mcp(session_storage: SessionManager) -> None:
    response = _post_initialize(session_storage, [TAILNET_HOST], TAILNET_HOST)

    assert response.status_code == 200


def test_unlisted_host_with_bearer_is_rejected_421(session_storage: SessionManager) -> None:
    response = _post_initialize(session_storage, [TAILNET_HOST], "evil.example.com")

    assert response.status_code == 421
    assert response.text == "Invalid Host header"


def test_empty_allowlist_keeps_loopback_and_rejects_tailnet_host(
    session_storage: SessionManager,
) -> None:
    tailnet = _post_initialize(session_storage, [], TAILNET_HOST)
    loopback = _post_initialize(session_storage, [], "127.0.0.1:60887")

    assert tailnet.status_code == 421
    assert loopback.status_code == 200


def test_loopback_host_still_accepted_when_hosts_configured(
    session_storage: SessionManager,
) -> None:
    response = _post_initialize(session_storage, [TAILNET_HOST], "localhost:60887")

    assert response.status_code == 200


def test_listed_host_without_bearer_is_401(session_storage: SessionManager) -> None:
    response = _post_initialize(session_storage, [TAILNET_HOST], TAILNET_HOST, bearer=False)

    assert response.status_code == 401


def test_port_wildcard_entry_accepts_any_port(session_storage: SessionManager) -> None:
    response = _post_initialize(session_storage, [f"{TAILNET_HOST}:*"], f"{TAILNET_HOST}:8443")

    assert response.status_code == 200


def test_configured_host_origin_must_be_its_https_origin(
    session_storage: SessionManager,
) -> None:
    same_origin = _post_initialize(
        session_storage,
        [TAILNET_HOST],
        TAILNET_HOST,
        origin=f"https://{TAILNET_HOST}",
    )
    foreign_origin = _post_initialize(
        session_storage,
        [TAILNET_HOST],
        TAILNET_HOST,
        origin="https://evil.example.com",
    )

    assert same_origin.status_code == 200
    assert foreign_origin.status_code == 403


@pytest.mark.parametrize(
    "entry",
    [
        "https://mbp.tail4125a0.ts.net",
        "mbp.tail4125a0.ts.net/mcp",
        "*",
        "*.tail4125a0.ts.net",
        " mbp.tail4125a0.ts.net",
        "MBP.tail4125a0.ts.net",
        "mbp.tail4125a0.ts.net:*:*",
        "",
    ],
)
def test_mcp_allowed_hosts_rejects_values_that_are_not_host_headers(entry: str) -> None:
    with pytest.raises(ValidationError):
        DaemonConfig(mcp_allowed_hosts=[entry])


def test_mcp_allowed_hosts_accepts_host_port_and_port_wildcard_forms() -> None:
    entries = [TAILNET_HOST, f"{TAILNET_HOST}:8443", f"{TAILNET_HOST}:*", "[::1]:8443"]

    assert DaemonConfig(mcp_allowed_hosts=entries).mcp_allowed_hosts == entries


def test_mcp_allowed_hosts_defaults_to_empty() -> None:
    assert DaemonConfig().mcp_allowed_hosts == []
