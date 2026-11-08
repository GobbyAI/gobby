"""A machine-wide bearer may not act as a session; session tokens bind one session."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from starlette.requests import Request

from gobby.servers.auth_service import AuthService
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.local_token import derive_managed_signing_key, issue_session_api_token

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"
FRONT_DOOR_SECRET = "session-binding-front-door-secret"
API_KEY = "session-binding-machine-key"
MACHINE_HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "X-Gobby-User-Id": "user-1",
    "X-Gobby-Machine-Id": LOCAL_MACHINE_ID,
    "X-Gobby-Key-Id": "key-1",
    "X-Gobby-Front-Door": FRONT_DOOR_SECRET,
}


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _request(headers: dict[str, str], *, method: str = "GET", path: str = "/") -> Request:
    raw_headers = [(key.lower().encode(), value.encode()) for key, value in headers.items()]
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": raw_headers,
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }
    )


def _service(database: Any, tmp_path: Path) -> AuthService:
    bootstrap_file = tmp_path / "bootstrap.yaml"
    bootstrap_file.write_text(json.dumps({"api_key": API_KEY}))
    service = AuthService(
        lambda: database,
        bootstrap_file=bootstrap_file,
        break_glass_file=tmp_path / "absent-break-glass",
    )
    service.bind_runtime(
        grant_service=None,
        local_machine_id=LOCAL_MACHINE_ID,
        clock=None,
        front_door_secret=FRONT_DOOR_SECRET,
    )
    return service


@pytest.mark.parametrize(
    "selector",
    [
        {"X-Gobby-Session-Id": "session-123"},
        {"X-Gobby-Agent-Run-Id": "run-123"},
        {"X-Gobby-Managed-Execution-Id": "execution-123"},
        {"X-Gobby-Terminal-Context": '{"parent_pid": 4242}'},
    ],
)
@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/mcp/tools/call"),
        ("POST", "/api/hooks/execute"),
        ("POST", "/api/embeddings"),
        ("POST", "/api/sessions/session-123/acp/close"),
    ],
)
def test_machine_bearer_refuses_session_selection(
    tmp_path: Path, selector: dict[str, str], method: str, path: str
) -> None:
    service = _service(MagicMock(spec=HubDatabase), tmp_path)

    decision = service.authenticate(_request(MACHINE_HEADERS | selector, method=method, path=path))

    assert not decision.allowed
    assert decision.code == "session_requires_bound_credential"
    assert decision.status_code == 403
    rendered = repr(decision)
    assert API_KEY not in rendered
    assert FRONT_DOOR_SECRET not in rendered


def test_machine_bearer_without_session_selector_stays_operator(tmp_path: Path) -> None:
    service = _service(MagicMock(spec=HubDatabase), tmp_path)

    decision = service.authenticate(
        _request(MACHINE_HEADERS, method="GET", path="/api/mcp/servers")
    )

    assert decision.allowed
    assert decision.user_id == "user-1"


@pytest.fixture
def live_session(session_manager: Any, sample_project: dict[str, Any]) -> Any:
    return session_manager.register(
        external_id="session-bound-live",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=sample_project["id"],
    )


def _session_headers(token: str, session_id: str, project_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Gobby-Session-Id": session_id,
        "X-Gobby-Caller-Project-Id": project_id,
    }


def test_session_token_admits_its_own_session(
    temp_db: HubDatabase, tmp_path: Path, live_session: Any, sample_project: dict[str, Any]
) -> None:
    service = _service(temp_db, tmp_path)
    token = issue_session_api_token(
        derive_managed_signing_key(API_KEY),
        session_id=live_session.id,
        project_id=sample_project["id"],
        machine_id=LOCAL_MACHINE_ID,
    )
    headers = _session_headers(token, live_session.id, sample_project["id"])

    assert service.is_request_authenticated(
        _request(headers, method="POST", path="/api/mcp/tools/call")
    )
    assert service.is_request_authenticated(
        _request(headers, method="POST", path="/api/hooks/execute")
    )


def test_session_token_refuses_other_session_and_run_selectors(
    temp_db: HubDatabase, tmp_path: Path, live_session: Any, sample_project: dict[str, Any]
) -> None:
    service = _service(temp_db, tmp_path)
    token = issue_session_api_token(
        derive_managed_signing_key(API_KEY),
        session_id=live_session.id,
        project_id=sample_project["id"],
        machine_id=LOCAL_MACHINE_ID,
    )
    headers = _session_headers(token, live_session.id, sample_project["id"])

    foreign = headers | {"X-Gobby-Session-Id": "operator-session"}
    assert not service.is_request_authenticated(
        _request(foreign, method="POST", path="/api/mcp/tools/call")
    )
    with_run = headers | {"X-Gobby-Agent-Run-Id": "run-123"}
    assert not service.is_request_authenticated(
        _request(with_run, method="POST", path="/api/mcp/tools/call")
    )


def test_session_token_refused_after_session_ends(
    temp_db: HubDatabase, tmp_path: Path, live_session: Any, sample_project: dict[str, Any]
) -> None:
    service = _service(temp_db, tmp_path)
    token = issue_session_api_token(
        derive_managed_signing_key(API_KEY),
        session_id=live_session.id,
        project_id=sample_project["id"],
        machine_id=LOCAL_MACHINE_ID,
    )
    headers = _session_headers(token, live_session.id, sample_project["id"])
    temp_db.execute("UPDATE sessions SET status = %s WHERE id = %s", ("expired", live_session.id))

    assert not service.is_request_authenticated(
        _request(headers, method="POST", path="/api/mcp/tools/call")
    )


def test_expired_session_token_is_refused(
    temp_db: HubDatabase, tmp_path: Path, live_session: Any, sample_project: dict[str, Any]
) -> None:
    service = _service(temp_db, tmp_path)
    token = issue_session_api_token(
        derive_managed_signing_key(API_KEY),
        session_id=live_session.id,
        project_id=sample_project["id"],
        machine_id=LOCAL_MACHINE_ID,
        ttl_seconds=-1,
    )
    headers = _session_headers(token, live_session.id, sample_project["id"])

    assert not service.is_request_authenticated(
        _request(headers, method="POST", path="/api/mcp/tools/call")
    )
