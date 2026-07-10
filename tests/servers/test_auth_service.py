"""Tests for the daemon's shared HTTP and WebSocket authentication service."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from starlette.requests import Request

import gobby.servers.auth_service as auth_service_module
import gobby.servers.http as http_module
from gobby.app_context import ServiceContainer
from gobby.config.app import DaemonConfig
from gobby.servers.auth_service import AuthService
from gobby.storage.auth import LOCAL_API_TOKEN_HASH_KEY, AuthStore, hash_token
from gobby.storage.config_store import ConfigStore
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


def _request(headers: dict[str, str]) -> Request:
    raw_headers = [(key.lower().encode(), value.encode()) for key, value in headers.items()]
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": raw_headers,
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }
    )


def _set_api_token(db: HubDatabase, token: str) -> None:
    ConfigStore(db).set(LOCAL_API_TOKEN_HASH_KEY, hash_token(token), source="system")


def _password_hash(password: str, salt: bytes = b"auth-service-test") -> str:
    derived = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    )
    return "$".join(
        (
            "scrypt",
            str(2**14),
            "8",
            "1",
            base64.b64encode(salt).decode(),
            base64.b64encode(derived).decode(),
        )
    )


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


def test_phase_default_ignores_config(temp_db: HubDatabase) -> None:
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
    explicit_server = http_module.HTTPServer(services, auth_mode="required")

    assert isinstance(default_server.auth_service, AuthService)
    assert default_server.auth_service.enabled is False
    assert explicit_server.auth_service.enabled is True


def test_verify_password_uses_scrypt_hash(temp_db: HubDatabase, tmp_path: Path) -> None:
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
