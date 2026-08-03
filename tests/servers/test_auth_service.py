"""Tests for the daemon's shared HTTP and WebSocket authentication service."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

import pytest

if TYPE_CHECKING:
    from gobby.mcp_proxy.manager import MCPClientManager
    from gobby.storage.sessions import SessionManager
from starlette.requests import Request

import gobby.servers.auth_service as auth_service_module
import gobby.servers.http as http_module
from gobby.app_context import ServiceContainer
from gobby.config.app import DaemonConfig
from gobby.config.ui import AuthConfig
from gobby.servers.auth_service import AuthService
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.auth import (
    LOCAL_API_TOKEN_HASH_KEY,
    AuthStore,
    hash_password,
    hash_token,
    verify_password_hash,
)
from gobby.storage.config_store import ConfigStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.local_token import issue_agent_api_token

pytestmark = pytest.mark.unit


def _request(
    headers: dict[str, str],
    *,
    method: str = "GET",
    path: str = "/",
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
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }
    )


def _set_api_token(db: HubDatabase, token: str) -> None:
    ConfigStore(db).set(LOCAL_API_TOKEN_HASH_KEY, hash_token(token), source="system")


@pytest.fixture
def live_agent_run(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> AgentRun:
    """A pending agent run backing the per-request liveness check."""
    session = session_manager.register(
        external_id="auth-service-agent",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="claude",
        project_id=sample_project["id"],
    )
    return LocalAgentRunManager(temp_db).create(
        parent_session_id=session.id,
        provider="claude",
        prompt="auth service capability",
    )


def _password_hash(password: str, salt: bytes = b"auth-service-test") -> str:
    return hash_password(password, salt=salt)


def test_password_hash_is_salted_argon2id() -> None:
    first_hash = hash_password("correct-password")
    second_hash = hash_password("correct-password")

    assert first_hash.startswith("$argon2id$v=19$")
    assert second_hash.startswith("$argon2id$v=19$")
    assert first_hash != second_hash
    assert verify_password_hash("correct-password", first_hash) is True
    assert verify_password_hash("wrong-password", first_hash) is False


def test_auth_config_ignores_removed_credentials() -> None:
    config = AuthConfig.model_validate(
        {
            "username": "admin",
            "password": "legacy-password",
            "session_secret": "legacy-secret",
            "api_token_hash": "hash",
            "password_hash": "hash",
        }
    )

    assert config.username == "admin"
    assert set(type(config).model_fields) == {"username"}


def test_verify_bearer_rotation_refresh(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    monkeypatch.setattr(auth_service_module.time, "monotonic", lambda: clock[0])
    token_file = tmp_path / "local_cli_token"
    token_file.write_text("old-token")
    _set_api_token(temp_db, "old-token")
    service = AuthService(lambda: temp_db, mode="required", token_file=token_file)

    assert service.verify_bearer("old-token") is True

    token_file.write_text("new-token")
    _set_api_token(temp_db, "new-token")
    assert service.verify_bearer("new-token") is False
    assert service.verify_bearer("old-token") is True

    clock[0] += service.MIN_REFRESH_INTERVAL
    assert service.verify_bearer("old-token") is False
    assert service.verify_bearer("new-token") is True


def test_is_request_authenticated_precedence(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "local_cli_token"
    token_file.write_text("api-token")
    _set_api_token(temp_db, "api-token")
    session_token, _ = AuthStore(temp_db).create_session()
    service = AuthService(lambda: temp_db, mode="required", token_file=token_file)

    assert service.is_request_authenticated(
        _request(
            {
                "Authorization": "Bearer api-token",
                "X-Gobby-Local-Token": "wrong-token",
                "Cookie": "gobby_session=wrong-session",
            }
        )
    )
    assert not service.is_request_authenticated(
        _request(
            {
                "Authorization": "Bearer wrong-token",
                "X-Gobby-Local-Token": "api-token",
                "Cookie": f"gobby_session={session_token}",
            }
        )
    )
    assert service.is_request_authenticated(
        _request(
            {
                "X-Gobby-Local-Token": "api-token",
                "Cookie": "gobby_session=wrong-session",
            }
        )
    )
    assert not service.is_request_authenticated(
        _request(
            {
                "X-Gobby-Local-Token": "wrong-token",
                "Cookie": f"gobby_session={session_token}",
            }
        )
    )
    assert service.is_request_authenticated(_request({"Cookie": f"gobby_session={session_token}"}))


def test_agent_bearer_is_bound_to_run_identity_and_routes(
    temp_db: HubDatabase,
    tmp_path: Path,
    live_agent_run: AgentRun,
) -> None:
    token_file = tmp_path / "local_cli_token"
    token_file.write_text("operator-token")
    _set_api_token(temp_db, "operator-token")
    service = AuthService(lambda: temp_db, mode="required", token_file=token_file)
    token = issue_agent_api_token(
        "operator-token",
        agent_run_id=live_agent_run.id,
        session_id="session-123",
        project_id="project-123",
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Gobby-Agent-Run-Id": live_agent_run.id,
        "X-Gobby-Session-Id": "session-123",
        "X-Gobby-Project-Id": "project-123",
    }

    assert service.is_request_authenticated(
        _request(headers, method="POST", path="/api/mcp/tools/call")
    )
    assert service.is_request_authenticated(
        _request(headers, method="POST", path="/api/code-index/codewiki/refresh")
    )

    assert not service.is_request_authenticated(
        _request(
            headers | {"X-Gobby-Session-Id": "operator-session"},
            method="POST",
            path="/api/mcp/tools/call",
        )
    )
    assert not service.is_request_authenticated(
        _request(headers, method="POST", path="/api/mcp/servers")
    )
    assert not service.is_request_authenticated(
        _request(headers, method="GET", path="/api/configuration/secrets")
    )


def test_local_token_refreshes_after_rotation(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [200.0]
    monkeypatch.setattr(auth_service_module.time, "monotonic", lambda: clock[0])
    token_file = tmp_path / "local_cli_token"
    token_file.write_text("old-token")
    _set_api_token(temp_db, "old-token")
    service = AuthService(lambda: temp_db, mode="required", token_file=token_file)

    assert service.local_token() == "old-token"

    token_file.write_text("new-token")
    _set_api_token(temp_db, "new-token")
    assert service.local_token() == "old-token"

    clock[0] += service.MIN_REFRESH_INTERVAL
    assert service.local_token() == "new-token"


def test_server_auth_mode_uses_config_then_explicit_override(temp_db: HubDatabase) -> None:
    services = ServiceContainer(
        config=DaemonConfig(auth_mode="required"),
        database=temp_db,
        session_manager=MagicMock(),
        task_manager=MagicMock(),
        text_generation_service=MagicMock(),
        tool_chat_service=MagicMock(),
        llm_service=MagicMock(),
    )

    default_server = http_module.HTTPServer(services)
    explicit_server = http_module.HTTPServer(services, auth_mode="disabled")

    assert isinstance(default_server.auth_service, AuthService)
    assert default_server.auth_service.enabled is True
    assert explicit_server.auth_service.enabled is False


def test_verify_password_uses_argon2id_hash(temp_db: HubDatabase, tmp_path: Path) -> None:
    config_store = ConfigStore(temp_db)
    config_store.set("auth.username", "operator")
    config_store.set("auth.password_hash", _password_hash("correct-password"))
    service = AuthService(
        lambda: temp_db,
        mode="required",
        token_file=tmp_path / "missing-local-token",
    )

    assert service.verify_password("operator", "correct-password") is True
    assert service.verify_password("intruder", "correct-password") is False
    assert service.verify_password("operator", "wrong-password") is False


@pytest.mark.asyncio
async def test_session_and_ws_verifiers(temp_db: HubDatabase, tmp_path: Path) -> None:
    token_file = tmp_path / "local_cli_token"
    token_file.write_text("api-token")
    _set_api_token(temp_db, "api-token")
    session_token, _ = AuthStore(temp_db).create_session()
    service = AuthService(lambda: temp_db, mode="required", token_file=token_file)

    assert service.validate_session(session_token) is True
    assert service.validate_session("wrong-session") is False
    assert await service.verify_ws_token("api-token") == "local-cli"
    assert await service.verify_ws_token("wrong-token") is None


def test_agent_capability_matrix(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    live_agent_run: AgentRun,
) -> None:
    token_file = tmp_path / "local_cli_token"
    token_file.write_text("operator-token")
    _set_api_token(temp_db, "operator-token")
    service = AuthService(lambda: temp_db, mode="required", token_file=token_file)
    session_uuid = "11111111-2222-3333-4444-555555555555"
    token = issue_agent_api_token(
        "operator-token",
        agent_run_id=live_agent_run.id,
        session_id=session_uuid,
        project_id="project-123",
    )
    identity = {
        "Authorization": f"Bearer {token}",
        "X-Gobby-Agent-Run-Id": live_agent_run.id,
        "X-Gobby-Session-Id": session_uuid,
        "X-Gobby-Caller-Project-Id": "project-123",
    }

    # Cross-project targeting: the target header may differ from the caller
    # project bound into the claims.
    assert service.is_request_authenticated(
        _request(
            identity | {"X-Gobby-Project-Id": "other-project"},
            method="POST",
            path="/api/mcp/tools/call",
        )
    )

    # A "#N" self-ref in the session header authenticates via resolution.
    resolved: list[tuple[str, str | None]] = []

    def fake_resolve(db: HubDatabase, ref: str, project_id: str | None = None) -> str:
        resolved.append((ref, project_id))
        return session_uuid

    monkeypatch.setattr(auth_service_module, "resolve_session_reference", fake_resolve)
    assert service.is_request_authenticated(
        _request(
            identity | {"X-Gobby-Session-Id": "#7"},
            method="POST",
            path="/api/mcp/tools/call",
        )
    )
    assert resolved == [("#7", "project-123")]

    # A ref that resolves to a different session is rejected.
    monkeypatch.setattr(
        auth_service_module,
        "resolve_session_reference",
        lambda db, ref, project_id=None: "99999999-8888-7777-6666-555555555555",
    )
    assert not service.is_request_authenticated(
        _request(
            identity | {"X-Gobby-Session-Id": "#8"},
            method="POST",
            path="/api/mcp/tools/call",
        )
    )

    # Context-free read-only routes authenticate without identity headers
    # (the Rust binaries send none) ...
    bearer_only = {"Authorization": f"Bearer {token}"}
    assert service.is_request_authenticated(
        _request(bearer_only, method="GET", path="/api/comms/channels")
    )
    assert service.is_request_authenticated(
        _request(bearer_only, method="GET", path="/api/embeddings/status")
    )
    assert service.is_request_authenticated(
        _request(bearer_only, method="POST", path="/api/code-index/graph/rebuild")
    )
    # ... but a present-and-wrong identity header still rejects.
    assert not service.is_request_authenticated(
        _request(
            bearer_only | {"X-Gobby-Caller-Project-Id": "other-project"},
            method="GET",
            path="/api/comms/channels",
        )
    )

    # Context-bearing routes require the full caller identity.
    assert not service.is_request_authenticated(
        _request(bearer_only, method="POST", path="/api/workflows/variables/set")
    )
    assert service.is_request_authenticated(
        _request(identity, method="POST", path="/api/workflows/variables/set")
    )

    # Out-of-matrix routes stay rejected, whatever the headers.
    for method, path in (
        ("POST", "/api/agents/spawn"),
        ("DELETE", "/api/mcp/servers/github"),
        ("PUT", "/api/mcp/servers/github"),
        ("POST", "/api/pipelines/run"),
        ("GET", "/api/configuration/secrets"),
        ("POST", "/api/memories/graph/rebuild"),
    ):
        assert not service.is_request_authenticated(_request(identity, method=method, path=path))


def _agent_service_and_headers(
    temp_db: HubDatabase,
    tmp_path: Path,
    run_id: str,
    *,
    timeout_seconds: float | None = None,
    minted_at: float | None = None,
) -> tuple[AuthService, dict[str, str]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    token_file = tmp_path / "local_cli_token"
    token_file.write_text("operator-token")
    _set_api_token(temp_db, "operator-token")
    service = AuthService(lambda: temp_db, mode="required", token_file=token_file)

    def mint() -> str:
        return issue_agent_api_token(
            "operator-token",
            agent_run_id=run_id,
            session_id="session-123",
            project_id="project-123",
            timeout_seconds=timeout_seconds,
        )

    if minted_at is None:
        token = mint()
    else:
        real_time = time.time
        time.time = lambda: minted_at
        try:
            token = mint()
        finally:
            time.time = real_time
    return service, {
        "Authorization": f"Bearer {token}",
        "X-Gobby-Agent-Run-Id": run_id,
        "X-Gobby-Session-Id": "session-123",
        "X-Gobby-Project-Id": "project-123",
    }


def test_agent_token_expiry_rejected_on_both_paths(
    temp_db: HubDatabase,
    tmp_path: Path,
    live_agent_run: AgentRun,
) -> None:
    """Expired capabilities fail on the run-timeout and untimed-ceiling paths."""
    service, headers = _agent_service_and_headers(
        temp_db,
        tmp_path / "timed",
        live_agent_run.id,
        timeout_seconds=120,
        minted_at=time.time() - 300,
    )
    assert not service.is_request_authenticated(
        _request(headers, method="POST", path="/api/mcp/tools/call")
    )

    service, headers = _agent_service_and_headers(
        temp_db,
        tmp_path / "untimed",
        live_agent_run.id,
        minted_at=time.time() - (86400 + 60),
    )
    assert not service.is_request_authenticated(
        _request(headers, method="POST", path="/api/mcp/tools/call")
    )

    # A fresh untimed token from the same identity still authenticates.
    service, headers = _agent_service_and_headers(temp_db, tmp_path / "fresh", live_agent_run.id)
    assert service.is_request_authenticated(
        _request(headers, method="POST", path="/api/mcp/tools/call")
    )


def test_terminal_run_token_rejected(
    temp_db: HubDatabase,
    tmp_path: Path,
    live_agent_run: AgentRun,
) -> None:
    """Run-liveness is the real revocation: a dead run's token stops working."""
    service, headers = _agent_service_and_headers(temp_db, tmp_path, live_agent_run.id)
    request = _request(headers, method="POST", path="/api/mcp/tools/call")
    assert service.is_request_authenticated(request)

    LocalAgentRunManager(temp_db).complete(live_agent_run.id, result="done")
    assert not service.is_request_authenticated(request)


def test_hooks_route_requires_run_identity(
    temp_db: HubDatabase,
    tmp_path: Path,
    live_agent_run: AgentRun,
) -> None:
    """The hooks route lost its run-id exemption: ghook sends the header."""
    service, headers = _agent_service_and_headers(temp_db, tmp_path, live_agent_run.id)

    assert service.is_request_authenticated(
        _request(headers, method="POST", path="/api/hooks/execute")
    )
    assert not service.is_request_authenticated(
        _request(
            {key: value for key, value in headers.items() if key != "X-Gobby-Agent-Run-Id"},
            method="POST",
            path="/api/hooks/execute",
        )
    )
    assert not service.is_request_authenticated(
        _request(
            headers | {"X-Gobby-Agent-Run-Id": "99999999-8888-7777-6666-555555555555"},
            method="POST",
            path="/api/hooks/execute",
        )
    )


@pytest.mark.asyncio
async def test_agent_listing_redaction() -> None:
    from gobby.servers.routes.mcp.endpoints.server import list_mcp_servers

    config = SimpleNamespace(
        name="github",
        transport="stdio",
        project_id="project-123",
        description="external server",
        url=None,
        command="gh-mcp",
        args=[],
        env={"API_KEY": "raw-secret-value", "SAFE_REF": "$secret:github/api-key"},
        headers={"Authorization": "Bearer raw-header-secret", "X-Ref": "$secret:github/header"},
        enabled=True,
        requires_oauth=False,
        oauth_provider=None,
        connect_timeout=5.0,
    )
    mcp_manager = SimpleNamespace(
        server_configs=[config],
        health={},
        is_connected=lambda name: False,
    )

    result = await list_mcp_servers(
        internal_manager=None,
        mcp_manager=cast("MCPClientManager", mcp_manager),
    )

    assert result["success"] is True
    (entry,) = [item for item in result["servers"] if item["name"] == "github"]
    assert entry["env"] == {"SAFE_REF": "$secret:github/api-key"}
    assert entry["headers"] == {"X-Ref": "$secret:github/header"}
    serialized = json.dumps(result)
    assert "raw-secret-value" not in serialized
    assert "raw-header-secret" not in serialized


def test_dead_run_server_removed() -> None:
    assert not hasattr(http_module, "run_server")
