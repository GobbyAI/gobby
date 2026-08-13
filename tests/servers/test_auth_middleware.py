"""AuthMiddleware behavior for required-by-default route authentication.

Machine-local tooling routes require configured credentials when UI auth is
enabled. Only explicitly public routes bypass middleware authentication.
"""

import asyncio
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, ParamSpec, TypeVar, cast
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.servers.auth_service import AuthService
from gobby.servers.middleware.auth import AuthMiddleware
from gobby.storage.auth import AuthStore, hash_token
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secrets import SecretStore
from tests.fixtures.postgres import TEST_USER_ID

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

_P = ParamSpec("_P")
_T = TypeVar("_T")


async def _run_db(func: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs) -> _T:
    return await asyncio.to_thread(func, *args, **kwargs)


@pytest.fixture
def auth_client() -> tuple[TestClient, MagicMock]:
    """App with AuthMiddleware and a catch-all route that returns 200."""
    app = FastAPI()
    auth_service = MagicMock()
    auth_service.is_request_authenticated.return_value = False
    app.add_middleware(
        AuthMiddleware,
        server=cast(
            "HTTPServer",
            SimpleNamespace(auth_service=auth_service, run_db=_run_db),
        ),
    )

    @app.get("/{path:path}")
    @app.post("/{path:path}")
    async def catch_all(path: str) -> dict[str, str]:
        return {"path": path}

    return TestClient(app), auth_service


PROTECTED_PATHS = [
    "/api/llm/generate",
    "/api/llm/vision/extract",
    "/api/embeddings",
    "/api/voice/transcribe",
    "/api/workflows/variables/set",
    "/api/workflows/variables/get",
    "/api/mcp/tools/call",
    "/api/mcp/servers",
    "/api/sessions/session-id/transcript",
    "/api/sessions/session-id/changes",
    "/api/sessions/session-id/expire",
    "/api/sessions/bulk-move",
    "/api/sessions/session-id/rename",
    "/api/sessions/session-id/stop",
    "/api/sessions/statusline",
    "/api/admin/status",
    "/api/admin/metrics",
    "/api/admin/config",
    "/api/tasks",
    "/api/config",
    "/api/agents",
    "/api/sessions/register",
    "/api/sessions/find_current",
    "/api/sessions/update_status",
    "/api/authentic",
    "/api/mcpx",
]

PUBLIC_PATHS = [
    "/",
    "/api/health",
    "/api/admin/health",
    "/api/admin/startup-progress",
    "/api/auth/status",
    "/api/comms/webhooks/test",
    "/api/github/webhooks/test",
    "/assets/app.js",
    "/favicon.ico",
    "/logo.png",
]


@pytest.mark.parametrize("path", PUBLIC_PATHS)
def test_public_routes_bypass_required_auth(
    auth_client: tuple[TestClient, MagicMock], path: str
) -> None:
    client, auth_service = auth_client

    response = client.get(path)

    assert response.status_code == 200, path
    auth_service.is_request_authenticated.assert_not_called()


@pytest.mark.parametrize("path", PROTECTED_PATHS)
def test_protected_routes_require_auth_when_enabled(
    auth_client: tuple[TestClient, MagicMock], path: str
) -> None:
    """Data-plane and UI API routes require credentials in required mode."""
    client, _auth_service = auth_client

    response = client.get(path)

    assert response.status_code == 401, path
    assert response.json() == {
        "error": (
            "Authentication required. CLI clients need ~/.gobby/local_cli_token "
            "(run 'gobby install' or 'gobby auth token --rotate'). Browsers: log in."
        )
    }


@pytest.mark.asyncio
async def test_repeated_requests_reuse_cached_credentials_without_secret_store(
    hub_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_token = "old-local-token"
    new_token = "new-local-token"
    auth_store = AuthStore(hub_db)
    auth_store.set_local_api_token_hash(hash_token(old_token))

    original_get = AuthStore.get_local_api_token_hash
    credential_lookup_threads: list[int] = []

    def tracked_get(store: AuthStore) -> str | None:
        credential_lookup_threads.append(threading.get_ident())
        return original_get(store)

    monkeypatch.setattr(AuthStore, "get_local_api_token_hash", tracked_get)
    secret_store_init = MagicMock(side_effect=AssertionError("SecretStore constructed"))
    monkeypatch.setattr(SecretStore, "__init__", secret_store_init)

    auth_service = AuthService(
        lambda: hub_db,
        token_file=tmp_path / "missing-token",
    )
    server = cast(
        "HTTPServer",
        SimpleNamespace(auth_service=auth_service, run_db=_run_db),
    )
    app = FastAPI()
    app.add_middleware(AuthMiddleware, server=server)

    @app.get("/api/tasks")
    async def protected() -> dict[str, bool]:
        return {"ok": True}

    event_loop_thread = threading.get_ident()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get(
            "/api/tasks",
            headers={"Authorization": f"Bearer {old_token}"},
        )
        second = await client.get(
            "/api/tasks",
            headers={"Authorization": f"Bearer {old_token}"},
        )

        auth_store.set_local_api_token_hash(hash_token(new_token))
        auth_service._last_refresh -= auth_service.MIN_REFRESH_INTERVAL
        after_change = await client.get(
            "/api/tasks",
            headers={"Authorization": f"Bearer {new_token}"},
        )

    assert [first.status_code, second.status_code, after_change.status_code] == [200, 200, 200]
    assert len(credential_lookup_threads) == 2
    assert all(thread_id != event_loop_thread for thread_id in credential_lookup_threads)
    secret_store_init.assert_not_called()


@pytest.mark.asyncio
async def test_session_cookie_validation_runs_off_event_loop(
    hub_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_token, _expires_at = AuthStore(hub_db).create_session(TEST_USER_ID)
    original_validate = AuthStore.validate_session
    validation_threads: list[int] = []

    def tracked_validate(store: AuthStore, token: str) -> bool:
        validation_threads.append(threading.get_ident())
        return original_validate(store, token)

    monkeypatch.setattr(AuthStore, "validate_session", tracked_validate)
    auth_service = AuthService(
        lambda: hub_db,
        token_file=tmp_path / "missing-token",
    )
    server = cast(
        "HTTPServer",
        SimpleNamespace(auth_service=auth_service, run_db=_run_db),
    )
    app = FastAPI()
    app.add_middleware(AuthMiddleware, server=server)

    @app.get("/api/tasks")
    async def protected() -> dict[str, bool]:
        return {"ok": True}

    event_loop_thread = threading.get_ident()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.cookies.set("gobby_session", session_token)
        response = await client.get("/api/tasks")

    assert response.status_code == 200
    assert validation_threads
    assert all(thread_id != event_loop_thread for thread_id in validation_threads)


@pytest.mark.asyncio
async def test_concurrent_protected_requests_do_not_block_event_loop() -> None:
    request_count = 3
    started_count = 0
    started_lock = threading.Lock()
    workers_started = asyncio.Event()
    release_auth = threading.Event()
    auth_threads: list[int] = []
    auth_service = MagicMock(enabled=True)
    event_loop = asyncio.get_running_loop()

    def blocking_authentication(_request: object) -> bool:
        nonlocal started_count
        auth_threads.append(threading.get_ident())
        with started_lock:
            started_count += 1
            if started_count == request_count:
                event_loop.call_soon_threadsafe(workers_started.set)
        if not release_auth.wait(timeout=1):
            raise AssertionError("event loop did not progress while authentication was blocked")
        return True

    auth_service.is_request_authenticated.side_effect = blocking_authentication
    server = cast(
        "HTTPServer",
        SimpleNamespace(auth_service=auth_service, run_db=_run_db),
    )
    app = FastAPI()
    app.add_middleware(AuthMiddleware, server=server)

    @app.get("/api/tasks")
    async def protected() -> dict[str, bool]:
        return {"ok": True}

    async def release_after_workers_start() -> None:
        await workers_started.wait()
        release_auth.set()

    event_loop_thread = threading.get_ident()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = await asyncio.gather(
            *(client.get("/api/tasks") for _ in range(request_count)),
            release_after_workers_start(),
        )

    http_responses = cast(list[httpx.Response], responses[:-1])
    assert [response.status_code for response in http_responses] == [200, 200, 200]
    assert len(auth_threads) == request_count
    assert all(thread_id != event_loop_thread for thread_id in auth_threads)
