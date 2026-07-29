"""Tests for the daemon's shared HTTP and WebSocket authentication service."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from starlette.requests import Request

import gobby.servers.auth_service as auth_service_module
import gobby.servers.http as http_module
from gobby.app_context import ServiceContainer
from gobby.config.app import DaemonConfig
from gobby.config.ui import AuthConfig
from gobby.runner_init import storage as storage_module
from gobby.servers.auth_service import AuthService
from gobby.storage.auth import (
    LOCAL_API_TOKEN_HASH_KEY,
    AuthStore,
    hash_password,
    hash_token,
    verify_password_hash,
)
from gobby.storage.config_store import ConfigStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secrets import SecretStore
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


def test_legacy_password_migration(temp_db: HubDatabase, tmp_path: Path) -> None:
    config_store = ConfigStore(temp_db)
    secret_store = SecretStore(temp_db)
    config_store.set("auth.username", "legacy-user")
    config_store.set_secret("auth.password", "legacy-password", secret_store)

    migrated = storage_module._migrate_legacy_auth_password(config_store, secret_store)

    assert migrated is True
    assert config_store.get("auth.password") is None
    assert secret_store.get("password") is None
    password_hash = config_store.get("auth.password_hash")
    assert isinstance(password_hash, str)
    assert password_hash.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    service = AuthService(lambda: temp_db, "required", token_file=tmp_path / "missing")
    assert service.verify_password("legacy-user", "legacy-password") is True


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
) -> None:
    token_file = tmp_path / "local_cli_token"
    token_file.write_text("operator-token")
    _set_api_token(temp_db, "operator-token")
    service = AuthService(lambda: temp_db, mode="required", token_file=token_file)
    token = issue_agent_api_token(
        "operator-token",
        agent_run_id="run-123",
        session_id="session-123",
        project_id="project-123",
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Gobby-Agent-Run-Id": "run-123",
        "X-Gobby-Session-Id": "session-123",
        "X-Gobby-Project-Id": "project-123",
    }

    assert service.is_request_authenticated(
        _request(headers, method="POST", path="/api/mcp/tools/call")
    )
    assert service.is_request_authenticated(
        _request(headers, method="POST", path="/api/code-index/codewiki/refresh")
    )
    assert service.is_request_authenticated(
        _request(
            {key: value for key, value in headers.items() if key != "X-Gobby-Agent-Run-Id"},
            method="POST",
            path="/api/hooks/execute",
        )
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


def test_dead_run_server_removed() -> None:
    assert not hasattr(http_module, "run_server")
