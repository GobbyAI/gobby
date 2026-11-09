"""A machine-wide bearer may not act as a session; session tokens bind one session."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from starlette.requests import Request

from gobby.runtime_grants import encode_grant_header
from gobby.runtime_grants.launch import materialize_managed_launch
from gobby.runtime_grants.schema import GrantBundle, GrantPrincipal
from gobby.servers.auth_service import AuthService
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.break_glass import BREAK_GLASS_HEADER
from gobby.utils.local_token import (
    derive_managed_signing_key,
    issue_agent_api_token,
    issue_maintenance_api_token,
    issue_session_api_token,
    issue_tool_api_token,
    read_managed_api_token,
)
from tests.servers.test_auth_service import (
    _GRANT_NOW,
    _grant_service,
    _signed_presentation_grant,
)

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"
FRONT_DOOR_SECRET = "session-binding-front-door-secret"
API_KEY = "session-binding-machine-key"
GRANT_HEADER = "X-Gobby-Runtime-Grant"
MACHINE_HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "X-Gobby-User-Id": "user-1",
    "X-Gobby-Machine-Id": LOCAL_MACHINE_ID,
    "X-Gobby-Key-Id": "key-1",
    "X-Gobby-Front-Door": FRONT_DOOR_SECRET,
}
# The machine-wide bearer alone, as an unspawned caller presents it.
MACHINE_BEARER = {"Authorization": f"Bearer {API_KEY}"}
# A second live api_keys row, which gdaemon also resolves and injects identity for.
SECOND_KEY = "session-binding-second-key"


@pytest.mark.parametrize("kind", ["agent_run", "tool_chat", "maintenance", "session"])
@pytest.mark.parametrize("machine_derived", [True, False])
def test_only_the_daemon_key_can_sign_each_capability_kind(
    temp_db: HubDatabase,
    tmp_path: Path,
    live_session: Any,
    sample_project: dict[str, Any],
    kind: str,
    machine_derived: bool,
) -> None:
    service = _service(temp_db, tmp_path)
    key = derive_managed_signing_key(API_KEY) if machine_derived else service.managed_signing_key()
    assert key is not None
    identity = {
        "session_id": live_session.id,
        "project_id": sample_project["id"],
        "machine_id": LOCAL_MACHINE_ID,
    }
    headers: dict[str, str] = {}
    if kind == "agent_run":
        token = issue_agent_api_token(key, agent_run_id="synthetic-run", **identity)
        headers["X-Gobby-Agent-Run-Id"] = "synthetic-run"
    elif kind == "tool_chat":
        token = issue_tool_api_token(
            key, managed_execution_id="synthetic-execution", timeout_seconds=60, **identity
        )
        headers["X-Gobby-Managed-Execution-Id"] = "synthetic-execution"
    elif kind == "maintenance":
        token = issue_maintenance_api_token(
            key,
            execution_id=live_session.id,
            project_id=sample_project["id"],
            machine_id=LOCAL_MACHINE_ID,
            timeout_seconds=60,
        )
        headers["X-Gobby-Managed-Execution-Id"] = live_session.id
    else:
        token = issue_session_api_token(key, **identity)
    request = _request(
        _session_headers(token, live_session.id, sample_project["id"]) | headers,
        method="POST",
        path="/api/mcp/tools/call",
    )
    # Isolate signing authority from owner liveness, which has separate real-storage coverage.
    with patch.object(service, "_managed_capability_is_live", return_value=True):
        decision = service.authenticate(request)
    assert decision.allowed is (not machine_derived)
    if machine_derived:
        assert decision.code == "capability_invalid"
        assert token not in repr(decision)


def test_machine_key_cannot_forge_a_session_credential(
    temp_db: HubDatabase, tmp_path: Path, live_session: Any, sample_project: dict[str, Any]
) -> None:
    service = _service(temp_db, tmp_path)
    forged = issue_session_api_token(
        derive_managed_signing_key(API_KEY),
        session_id=live_session.id,
        project_id=sample_project["id"],
        machine_id=LOCAL_MACHINE_ID,
    )
    decision = service.authenticate(
        _request(
            _session_headers(forged, live_session.id, sample_project["id"]),
            method="POST",
            path="/api/mcp/tools/call",
        )
    )
    assert not decision.allowed
    assert decision.code == "capability_invalid"
    assert forged not in repr(decision)


def test_capability_signing_key_survives_restart_and_machine_key_rotation(tmp_path: Path) -> None:
    service = _service(None, tmp_path)
    key = service.managed_signing_key()
    assert isinstance(key, bytes) and len(key) == 32
    assert key != derive_managed_signing_key(API_KEY)
    key_file = tmp_path / "capability_signing_key"
    assert key_file.read_bytes() == key
    assert key_file.stat().st_mode & 0o777 == 0o600
    bootstrap = tmp_path / "bootstrap.yaml"
    bootstrap.write_text(json.dumps({"api_key": SECOND_KEY}))
    restarted = AuthService(lambda: cast(HubDatabase, None), bootstrap_file=bootstrap)
    assert restarted.managed_signing_key() == key


@pytest.mark.parametrize("kind", ["agent_run", "tool_chat", "maintenance"])
def test_managed_respawn_remints_after_authority_activation(
    temp_db: HubDatabase,
    tmp_path: Path,
    live_session: Any,
    sample_project: dict[str, Any],
    kind: str,
) -> None:
    service = _service(temp_db, tmp_path)
    key = service.managed_signing_key()
    assert key is not None
    golden = Path(__file__).parents[1] / "runtime_grants/golden/direct_datastores.json"
    grant = GrantBundle.model_validate_json(golden.read_bytes()).model_copy(
        update={
            "principal": GrantPrincipal.model_validate(
                {
                    "kind": kind,
                    "execution_id": live_session.id,
                    "session_id": live_session.id if kind != "maintenance" else None,
                    "project_id": sample_project["id"],
                    "machine_id": LOCAL_MACHINE_ID,
                }
            ),
        }
    )
    old = materialize_managed_launch(
        grant,
        dest_dir=tmp_path / "old-launch",
        signing_key=derive_managed_signing_key(API_KEY),
        deadline_seconds=60,
    )
    fresh = materialize_managed_launch(
        grant,
        dest_dir=tmp_path / "fresh-launch",
        signing_key=key,
        deadline_seconds=60,
    )
    old_token = read_managed_api_token(old.env)
    fresh_token = read_managed_api_token(fresh.env)
    assert old_token is not None and fresh_token is not None
    restarted = _service(temp_db, tmp_path)
    extra = {
        "X-Gobby-Agent-Run-Id"
        if kind == "agent_run"
        else "X-Gobby-Managed-Execution-Id": live_session.id,
    }

    def request(token: str) -> Request:
        return _request(
            _session_headers(token, live_session.id, sample_project["id"]) | extra,
            method="POST",
            path="/api/mcp/tools/call",
        )

    with patch.object(restarted, "_managed_capability_is_live", return_value=True):
        refused = restarted.authenticate(request(old_token))
        admitted = restarted.authenticate(request(fresh_token))
    assert not refused.allowed and refused.code == "capability_invalid"
    assert admitted.allowed, admitted


@pytest.mark.parametrize("unsafe", ["permissions", "symlink", "truncated"])
def test_capability_signing_key_refuses_unsafe_files(tmp_path: Path, unsafe: str) -> None:
    service = _service(None, tmp_path)
    key_file = tmp_path / "capability_signing_key"
    target = tmp_path / "other-key"
    target.write_bytes(b"x" * 32)
    target.chmod(0o600)
    if unsafe == "symlink":
        key_file.symlink_to(target)
    else:
        key_file.write_bytes(b"x" * (1 if unsafe == "truncated" else 32))
        key_file.chmod(0o644 if unsafe == "permissions" else 0o600)
    assert service.managed_signing_key() is None


SECOND_KEY_HEADERS = MACHINE_HEADERS | {"Authorization": f"Bearer {SECOND_KEY}"}
BREAK_GLASS_SECRET = "session-binding-break-glass-secret"
GRANT_ROUTES = ["/api/mcp/tools/call", "/api/sessions/foreign-session/acp/close"]


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _request(
    headers: dict[str, str],
    *,
    method: str = "GET",
    path: str = "/",
    client: tuple[str, int] = ("testclient", 50000),
) -> Request:
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
            "client": client,
            "server": ("testserver", 80),
        }
    )


def _service(database: Any, tmp_path: Path, *, break_glass_file: Path | None = None) -> AuthService:
    bootstrap_file = tmp_path / "bootstrap.yaml"
    bootstrap_file.write_text(json.dumps({"api_key": API_KEY}))
    service = AuthService(
        lambda: database,
        bootstrap_file=bootstrap_file,
        break_glass_file=break_glass_file or tmp_path / "absent-break-glass",
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

    decision = service.authenticate(_request(MACHINE_BEARER | selector, method=method, path=path))

    assert not decision.allowed
    assert decision.code == "session_requires_bound_credential"
    assert decision.status_code == 403
    rendered = repr(decision)
    assert API_KEY not in rendered
    assert FRONT_DOOR_SECRET not in rendered


@pytest.mark.parametrize(
    "selector",
    [
        {"X-Gobby-Session-Id": "session-123"},
        {"X-Gobby-Terminal-Context": '{"parent_pid": 4242}'},
    ],
)
def test_invalid_bearer_with_selector_keeps_its_unauthorized_answer(
    tmp_path: Path, selector: dict[str, str]
) -> None:
    service = _service(MagicMock(spec=HubDatabase), tmp_path)

    decision = service.authenticate(
        _request(
            {"Authorization": "Bearer invalid"} | selector,
            method="POST",
            path="/api/mcp/tools/call",
        )
    )

    assert not decision.allowed
    assert decision.code == "invalid_token"
    assert decision.status_code == 401


def test_machine_bearer_without_session_selector_stays_operator(tmp_path: Path) -> None:
    service = _service(MagicMock(spec=HubDatabase), tmp_path)

    decision = service.authenticate(
        _request(MACHINE_HEADERS, method="GET", path="/api/mcp/servers")
    )

    assert decision.allowed
    assert decision.user_id == "user-1"


@pytest.mark.parametrize(
    "headers",
    [MACHINE_HEADERS, SECOND_KEY_HEADERS],
    ids=["bootstrap-key", "second-api-key"],
)
@pytest.mark.parametrize(
    "selector",
    [
        {"X-Gobby-Session-Id": "session-123"},
        {"X-Gobby-Agent-Run-Id": "run-123"},
        {"X-Gobby-Managed-Execution-Id": "execution-123"},
        {"X-Gobby-Terminal-Context": '{"parent_pid": 4242}'},
    ],
)
def test_verified_front_door_refuses_session_selection(
    tmp_path: Path, headers: dict[str, str], selector: dict[str, str]
) -> None:
    # gdaemon injects front-door identity for any live api_keys row, so a verified front
    # door proves a machine key, not the operator. Any key it carries may not name a session.
    service = _service(MagicMock(spec=HubDatabase), tmp_path)

    decision = service.authenticate(
        _request(headers | selector, method="POST", path="/api/mcp/tools/call")
    )

    assert not decision.allowed
    assert decision.code == "session_requires_bound_credential"
    assert decision.status_code == 403
    rendered = repr(decision)
    assert API_KEY not in rendered
    assert SECOND_KEY not in rendered
    assert FRONT_DOOR_SECRET not in rendered


@pytest.mark.parametrize(
    "selector",
    [
        {"X-Gobby-Session-Id": "session-123"},
        {"X-Gobby-Terminal-Context": '{"parent_pid": 4242}'},
    ],
)
def test_break_glass_refuses_session_selection(tmp_path: Path, selector: dict[str, str]) -> None:
    # Break-glass is an operator escape for the daemon's own routes, not a session exemption.
    break_glass_file = tmp_path / "break-glass"
    break_glass_file.write_text(BREAK_GLASS_SECRET)
    break_glass_file.chmod(0o600)
    service = _service(MagicMock(spec=HubDatabase), tmp_path, break_glass_file=break_glass_file)

    decision = service.authenticate(
        _request(
            {BREAK_GLASS_HEADER: BREAK_GLASS_SECRET} | selector,
            method="POST",
            path="/api/mcp/tools/call",
            client=("127.0.0.1", 50000),
        )
    )

    assert not decision.allowed
    assert decision.code == "session_requires_bound_credential"
    assert decision.status_code == 403
    assert BREAK_GLASS_SECRET not in repr(decision)


def test_verified_front_door_without_selector_stays_operator(tmp_path: Path) -> None:
    service = _service(MagicMock(spec=HubDatabase), tmp_path)

    decision = service.authenticate(
        _request(MACHINE_HEADERS, method="GET", path="/api/mcp/servers")
    )

    assert decision.allowed
    assert decision.user_id == "user-1"


def _grant_bound_service(database: Any, tmp_path: Path) -> AuthService:
    service = _service(database, tmp_path)
    service.bind_runtime(
        grant_service=_grant_service(),
        local_machine_id=LOCAL_MACHINE_ID,
        clock=lambda: _GRANT_NOW + 10,
        front_door_secret=FRONT_DOOR_SECRET,
    )
    return service


def _operator_grant_headers() -> dict[str, str]:
    grant = _signed_presentation_grant(session_id="cli-session", machine_id=LOCAL_MACHINE_ID)
    return MACHINE_HEADERS | {GRANT_HEADER: encode_grant_header(grant)}


@pytest.mark.parametrize("grant", ["", "   ", "not-a-grant", "a.b.c"])
@pytest.mark.parametrize("path", GRANT_ROUTES)
def test_machine_bearer_with_invalid_grant_refuses_foreign_session(
    tmp_path: Path, grant: str, path: str
) -> None:
    service = _service(MagicMock(spec=HubDatabase), tmp_path)
    headers = MACHINE_BEARER | {"X-Gobby-Session-Id": "foreign-session", GRANT_HEADER: grant}

    decision = service.authenticate(_request(headers, method="POST", path=path))

    assert not decision.allowed
    assert decision.code == "session_requires_bound_credential"
    assert decision.status_code == 403
    rendered = repr(decision)
    assert API_KEY not in rendered
    assert FRONT_DOOR_SECRET not in rendered


@pytest.mark.parametrize("path", GRANT_ROUTES)
def test_machine_bearer_with_valid_grant_refuses_foreign_session(tmp_path: Path, path: str) -> None:
    service = _grant_bound_service(MagicMock(spec=HubDatabase), tmp_path)
    headers = _operator_grant_headers() | {"X-Gobby-Session-Id": "foreign-session"}

    decision = service.authenticate(_request(headers, method="POST", path=path))

    assert not decision.allowed
    assert decision.code == "session_requires_bound_credential"
    assert decision.status_code == 403


def test_machine_bearer_with_valid_grant_for_another_session_is_refused(tmp_path: Path) -> None:
    service = _grant_bound_service(MagicMock(spec=HubDatabase), tmp_path)
    headers = _operator_grant_headers() | {"X-Gobby-Session-Id": "foreign-session"}

    decision = service.authenticate(_request(headers, method="GET", path="/api/llm/status"))

    assert not decision.allowed
    assert decision.code == "session_requires_bound_credential"
    assert decision.status_code == 403


def test_machine_bearer_with_valid_grant_refuses_terminal_context_selector(
    tmp_path: Path,
) -> None:
    service = _grant_bound_service(MagicMock(spec=HubDatabase), tmp_path)
    headers = _operator_grant_headers() | {"X-Gobby-Terminal-Context": '{"parent_pid": 4242}'}

    decision = service.authenticate(_request(headers, method="GET", path="/api/llm/status"))

    assert not decision.allowed
    assert decision.code == "session_requires_bound_credential"
    assert decision.status_code == 403


def test_machine_bearer_with_valid_grant_for_its_own_session_is_refused(
    tmp_path: Path,
) -> None:
    service = _grant_bound_service(MagicMock(spec=HubDatabase), tmp_path)
    headers = _operator_grant_headers() | {"X-Gobby-Session-Id": "cli-session"}

    decision = service.authenticate(_request(headers, method="GET", path="/api/llm/status"))

    assert not decision.allowed
    assert decision.code == "session_requires_bound_credential"
    assert decision.status_code == 403


@pytest.mark.parametrize(
    ("session_id", "expected"),
    [("cli-session", True), ("foreign-session", False), (None, False)],
)
def test_session_credential_matches_only_its_own_interactive_grant(
    session_id: str | None, expected: bool
) -> None:
    from gobby.servers.grant_auth import bearer_matches_grant
    from gobby.utils.local_token import SESSION_TOKEN_KIND, AgentApiTokenClaims

    grant = _signed_presentation_grant(session_id=session_id, machine_id=LOCAL_MACHINE_ID)
    claims = AgentApiTokenClaims(
        session_id="cli-session",
        project_id="project-1",
        machine_id=LOCAL_MACHINE_ID,
        iat=0,
        exp=10**10,
        kind=SESSION_TOKEN_KIND,
    )

    matched = bearer_matches_grant(
        grant.principal, claims=claims, local_machine_id=LOCAL_MACHINE_ID
    )

    assert matched is expected


@pytest.mark.parametrize(
    ("session_id", "expected"),
    [("cli-session", False), (None, True)],
)
def test_machine_key_presents_only_session_less_interactive_grants(
    session_id: str | None, expected: bool
) -> None:
    """With no session claim (the machine key), a session-bound grant is a missing claim."""
    from gobby.servers.grant_auth import bearer_matches_grant

    grant = _signed_presentation_grant(session_id=session_id, machine_id=LOCAL_MACHINE_ID)

    matched = bearer_matches_grant(grant.principal, claims=None, local_machine_id=LOCAL_MACHINE_ID)

    assert matched is expected


def test_session_credential_presents_its_own_interactive_grant(
    temp_db: HubDatabase, tmp_path: Path, live_session: Any, sample_project: dict[str, Any]
) -> None:
    service = _grant_bound_service(temp_db, tmp_path)
    token = issue_session_api_token(
        cast(bytes, service.managed_signing_key()),
        session_id=live_session.id,
        project_id=sample_project["id"],
        machine_id=LOCAL_MACHINE_ID,
    )
    grant = _signed_presentation_grant(
        session_id=live_session.id,
        machine_id=LOCAL_MACHINE_ID,
        project_id=sample_project["id"],
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Gobby-Machine-Id": LOCAL_MACHINE_ID,
        "X-Gobby-Session-Id": live_session.id,
        "X-Gobby-Caller-Project-Id": sample_project["id"],
        GRANT_HEADER: encode_grant_header(grant),
    }

    decision = service.authenticate(_request(headers, method="GET", path="/api/llm/status"))

    assert decision.allowed


def test_foreign_session_credential_cannot_present_another_sessions_grant(
    temp_db: HubDatabase, tmp_path: Path, live_session: Any, sample_project: dict[str, Any]
) -> None:
    service = _grant_bound_service(temp_db, tmp_path)
    token = issue_session_api_token(
        cast(bytes, service.managed_signing_key()),
        session_id=live_session.id,
        project_id=sample_project["id"],
        machine_id=LOCAL_MACHINE_ID,
    )
    grant = _signed_presentation_grant(
        session_id="cli-session",
        machine_id=LOCAL_MACHINE_ID,
        project_id=sample_project["id"],
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Gobby-Machine-Id": LOCAL_MACHINE_ID,
        "X-Gobby-Session-Id": live_session.id,
        "X-Gobby-Caller-Project-Id": sample_project["id"],
        GRANT_HEADER: encode_grant_header(grant),
    }

    decision = service.authenticate(_request(headers, method="GET", path="/api/llm/status"))

    assert not decision.allowed
    assert decision.code == "forged_identity"
    assert decision.status_code == 401
    rendered = repr(decision)
    assert API_KEY not in rendered
    assert FRONT_DOOR_SECRET not in rendered


def test_operator_shell_grant_without_session_is_admitted_on_machine_bearer(
    tmp_path: Path,
) -> None:
    # An operator shell has no session credential and its interactive grant names no
    # session. It reaches the daemon through the verified front door with the machine key
    # and no selector, and the grant path admits it.
    service = _grant_bound_service(MagicMock(spec=HubDatabase), tmp_path)
    grant = _signed_presentation_grant(session_id=None, machine_id=LOCAL_MACHINE_ID)
    headers = MACHINE_HEADERS | {GRANT_HEADER: encode_grant_header(grant)}

    decision = service.authenticate(_request(headers, method="GET", path="/api/llm/status"))

    assert decision.allowed


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
        cast(bytes, service.managed_signing_key()),
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
        cast(bytes, service.managed_signing_key()),
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
        cast(bytes, service.managed_signing_key()),
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
        cast(bytes, service.managed_signing_key()),
        session_id=live_session.id,
        project_id=sample_project["id"],
        machine_id=LOCAL_MACHINE_ID,
        ttl_seconds=-1,
    )
    headers = _session_headers(token, live_session.id, sample_project["id"])

    assert not service.is_request_authenticated(
        _request(headers, method="POST", path="/api/mcp/tools/call")
    )
