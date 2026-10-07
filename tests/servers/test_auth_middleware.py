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

import gobby.servers.middleware.auth as auth_middleware
from gobby.servers.auth_service import AuthService
from gobby.servers.grant_auth import AuthDecision
from gobby.servers.lease_fence import EffectFence
from gobby.servers.middleware.auth import AuthMiddleware
from gobby.storage.auth import AuthStore
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
    auth_service.authenticate.return_value = AuthDecision(allowed=False)
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


def test_production_middleware_does_not_import_test_admit_barrier() -> None:
    source = Path(auth_middleware.__file__).read_text(encoding="utf-8")
    assert "await_test_admit_barrier" not in source
    assert "GOBBY_TEST_PROTECT" not in source


PROTECTED_PATHS = [
    "/api/llm/generate",
    "/api/llm/vision/extract",
    "/api/embeddings",
    "/api/voice/transcribe",
    "/api/sessions/session-id/variables/set",
    "/api/sessions/session-id/variables/get",
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
    "/api/admin/startup-progress",
    "/api/auth/status",
    "/api/comms/webhooks/test",
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
    auth_service.authenticate.assert_not_called()


def test_stop_grace_keeps_protected_hook_admission_open() -> None:
    app = FastAPI()
    auth_service = MagicMock()
    auth_service.authenticate.return_value = AuthDecision(allowed=True, status_code=200)
    server = cast(
        "HTTPServer",
        SimpleNamespace(
            auth_service=auth_service,
            run_db=_run_db,
            services=SimpleNamespace(
                shutdown_in_progress=True,
                http_admission_closed=False,
            ),
        ),
    )
    app.add_middleware(AuthMiddleware, server=server)

    @app.post("/api/hooks/execute")
    async def protected() -> dict[str, bool]:
        return {"accepted": True}

    response = TestClient(app).post("/api/hooks/execute")

    assert response.status_code == 200
    assert response.json() == {"accepted": True}
    auth_service.authenticate.assert_called_once()


def test_closed_http_admission_rejects_hook_before_database_submission() -> None:
    app = FastAPI()
    auth_service = MagicMock()
    run_db = MagicMock(side_effect=AssertionError("database executor must not be used"))
    server = cast(
        "HTTPServer",
        SimpleNamespace(
            auth_service=auth_service,
            run_db=run_db,
            services=SimpleNamespace(
                shutdown_in_progress=True,
                http_admission_closed=True,
            ),
        ),
    )
    app.add_middleware(AuthMiddleware, server=server)

    @app.post("/api/hooks/execute")
    async def protected() -> dict[str, bool]:
        raise AssertionError("protected handler must not run after admission closes")

    @app.get("/api/health")
    async def health() -> dict[str, bool]:
        return {"healthy": True}

    client = TestClient(app)
    response = client.post("/api/hooks/execute")
    health_response = client.get("/api/health")

    assert response.status_code == 503
    assert response.json() == {
        "error": "Daemon is shutting down",
        "code": "shutdown_in_progress",
    }
    assert health_response.status_code == 200
    run_db.assert_not_called()
    auth_service.authenticate.assert_not_called()


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
            "Authentication required. CLI clients need an API key in bootstrap.yaml "
            "and gdaemon (run 'gobby auth login'). Browsers: log in."
        )
    }


@pytest.mark.parametrize(
    "code",
    (
        "stale_epoch",
        "revoked",
        "missing_grant",
        "forged_identity",
        "lease_not_held",
        "capability_expired",
        "run_inactive",
        "identity_mismatch",
    ),
)
def test_grant_rejection_omits_login_guidance(
    auth_client: tuple[TestClient, MagicMock], code: str
) -> None:
    client, auth_service = auth_client
    auth_service.authenticate.return_value = AuthDecision(allowed=False, code=code, status_code=401)
    response = client.get("/api/tasks")
    body = response.json()
    assert "API key" not in body["error"]
    assert "log in" not in body["error"].casefold()
    assert body["error"] == "Request rejected"
    assert body["code"] == code


@pytest.mark.parametrize("code", ("missing_auth", "invalid_token", "session_invalid"))
def test_unrecognised_credentials_keep_login_guidance(
    auth_client: tuple[TestClient, MagicMock], code: str
) -> None:
    client, auth_service = auth_client
    auth_service.authenticate.return_value = AuthDecision(allowed=False, code=code, status_code=401)
    response = client.get("/api/tasks")
    body = response.json()
    assert "API key" in body["error"]
    assert "bootstrap.yaml" in body["error"]
    assert body["code"] == code


@pytest.mark.asyncio
async def test_repeated_front_door_identity_needs_no_database_or_secret_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_getter = MagicMock(side_effect=AssertionError("database consulted"))
    secret_store_init = MagicMock(side_effect=AssertionError("SecretStore constructed"))
    monkeypatch.setattr(SecretStore, "__init__", secret_store_init)
    auth_service = AuthService(
        database_getter,
        bootstrap_file=tmp_path / "bootstrap.yaml",
        break_glass_file=tmp_path / "absent-break-glass",
    )

    def bind(secret: str) -> None:
        auth_service.bind_runtime(
            grant_service=None,
            lease_live=None,
            local_machine_id="machine-1",
            effect_fence=None,
            clock=None,
            front_door_secret=secret,
        )

    bind("first-secret")
    headers = {
        "X-Gobby-Front-Door": "first-secret",
        "X-Gobby-User-Id": "user-1",
        "X-Gobby-Machine-Id": "machine-1",
        "X-Gobby-Key-Id": "key-1",
    }
    server = cast("HTTPServer", SimpleNamespace(auth_service=auth_service, run_db=_run_db))
    app = FastAPI()
    app.add_middleware(AuthMiddleware, server=server)

    @app.get("/api/tasks")
    async def protected() -> dict[str, bool]:
        return {"ok": True}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.get("/api/tasks", headers=headers)
        second = await client.get("/api/tasks", headers=headers)
        bind("second-secret")
        stale = await client.get("/api/tasks", headers=headers)
        fresh = await client.get(
            "/api/tasks", headers={**headers, "X-Gobby-Front-Door": "second-secret"}
        )
    assert [first.status_code, second.status_code, stale.status_code, fresh.status_code] == [
        200,
        200,
        401,
        200,
    ]
    database_getter.assert_not_called()
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
        bootstrap_file=tmp_path / "bootstrap.yaml",
        break_glass_file=tmp_path / "absent-break-glass",
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

    def blocking_authentication(_request: object) -> AuthDecision:
        nonlocal started_count
        auth_threads.append(threading.get_ident())
        with started_lock:
            started_count += 1
            if started_count == request_count:
                event_loop.call_soon_threadsafe(workers_started.set)
        if not release_auth.wait(timeout=1):
            raise AssertionError("event loop did not progress while authentication was blocked")
        return AuthDecision(allowed=True, status_code=200)

    auth_service.authenticate.side_effect = blocking_authentication
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


@pytest.mark.asyncio
async def test_effectful_handler_holds_admission_across_call_next() -> None:
    fence = EffectFence()
    in_flight_during_handler: list[int] = []
    auth_service = MagicMock()
    auth_service.authenticate.return_value = AuthDecision(allowed=True, status_code=200)
    auth_service.effect_fence = fence
    server = cast(
        "HTTPServer",
        SimpleNamespace(auth_service=auth_service, run_db=_run_db),
    )
    app = FastAPI()
    app.add_middleware(AuthMiddleware, server=server)

    @app.post("/api/embeddings")
    async def protected() -> dict[str, bool]:
        in_flight_during_handler.append(fence.in_flight)
        return {"ok": True}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/embeddings")

    assert response.status_code == 200
    assert in_flight_during_handler == [1]
    assert fence.in_flight == 0


@pytest.mark.asyncio
async def test_drained_fence_rejects_effectful_handler() -> None:
    fence = EffectFence()
    fence.drain(timeout=0.1)
    auth_service = MagicMock()
    auth_service.authenticate.return_value = AuthDecision(allowed=True, status_code=200)
    auth_service.effect_fence = fence
    server = cast(
        "HTTPServer",
        SimpleNamespace(auth_service=auth_service, run_db=_run_db),
    )
    app = FastAPI()
    app.add_middleware(AuthMiddleware, server=server)

    @app.post("/api/embeddings")
    async def protected() -> dict[str, bool]:
        raise AssertionError("handler must not run after drain")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/embeddings")

    assert response.status_code == 409
    assert response.json()["code"] == "lease_not_held"
