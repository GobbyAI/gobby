"""Loopback recovery admission remains independent of database and grants."""

from pathlib import Path

import pytest
from starlette.requests import Request

from gobby.servers.auth_service import AuthService
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


def _request(peer: str | None, value: str | None, *, grant_route: bool = False) -> Request:
    headers = [] if value is None else [(b"x-gobby-break-glass", value.encode())]
    return Request(
        {
            "type": "http",
            "method": "POST" if grant_route else "GET",
            "path": "/api/code-index/graph/clear" if grant_route else "/api/projects",
            "headers": headers,
            "client": (peer, 50000) if peer is not None else None,
            "query_string": b"",
        }
    )


@pytest.mark.parametrize(
    "peer,value,allowed",
    [
        ("127.0.0.1", "recovery-value", True),
        ("::1", "recovery-value", True),
        ("203.0.113.2", "recovery-value", False),
        (None, "recovery-value", False),
        ("127.0.0.1", "wrong", False),
        ("127.0.0.1", None, False),
    ],
)
def test_break_glass_admits_only_loopback_holder_without_database(
    tmp_path: Path, peer: str | None, value: str | None, allowed: bool
) -> None:
    path = tmp_path / "break_glass"
    path.touch(mode=0o600)
    path.write_text("recovery-value")
    database_calls: list[None] = []

    def broken_database() -> HubDatabase:
        database_calls.append(None)
        raise RuntimeError("database unavailable")

    service = AuthService(
        broken_database, bootstrap_file=tmp_path / "bootstrap.yaml", break_glass_file=path
    )
    request = _request(peer, value)
    if allowed:
        request.scope["headers"].append((b"authorization", b"Bearer stale-token"))
    decision = service.authenticate(request)
    assert decision.allowed is allowed
    if not allowed:
        assert decision.status_code == 401
    assert database_calls == []


def test_break_glass_still_requires_grant_on_grant_routes(tmp_path: Path) -> None:
    path = tmp_path / "break_glass"
    path.touch(mode=0o600)
    path.write_text("recovery-value")

    def broken_database() -> HubDatabase:
        raise RuntimeError("database must not be consulted")

    service = AuthService(
        broken_database, bootstrap_file=tmp_path / "bootstrap.yaml", break_glass_file=path
    )
    decision = service.authenticate(_request("127.0.0.1", "recovery-value", grant_route=True))
    assert decision.allowed is False
    assert decision.status_code == 401
    assert decision.code == "missing_grant"
