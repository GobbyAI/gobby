"""HTTP server lifespan and middleware behavior tests."""

import asyncio
import hashlib
import hmac
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import ParamSpec, TypeVar, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse, Response

from gobby.app_context import ServiceContainer
from gobby.config.app import DaemonConfig
from gobby.servers.auth_service import AuthService
from gobby.servers.http import HTTPServer
from gobby.servers.middleware.auth import AuthMiddleware
from gobby.storage.auth import AuthStore, hash_token
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.unit

_P = ParamSpec("_P")
_T = TypeVar("_T")


async def _run_db(func: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs) -> _T:
    return await asyncio.to_thread(func, *args, **kwargs)


def _required_auth_middleware_app(
    *,
    bind_host: str = "localhost",
) -> FastAPI:
    from gobby.servers.grant_auth import AuthDecision

    auth_service = MagicMock()

    def authenticate(request: Request) -> AuthDecision:
        if request.headers.get("Authorization") == "Bearer shared-token":
            return AuthDecision(allowed=True)
        return AuthDecision(
            allowed=False,
            code="missing_auth",
            message=(
                "Authentication required. CLI clients need ~/.gobby/local_cli_token "
                "(run 'gobby install' or 'gobby auth token --rotate'). Browsers: log in."
            ),
        )

    auth_service.authenticate.side_effect = authenticate
    server = cast(
        HTTPServer,
        SimpleNamespace(
            auth_service=auth_service,
            services=SimpleNamespace(config=SimpleNamespace(bind_host=bind_host)),
            run_db=_run_db,
        ),
    )
    app = FastAPI()
    app.add_middleware(AuthMiddleware, server=server)
    return app


def _required_auth_app(
    *,
    bind_host: str = "localhost",
) -> FastAPI:
    app = _required_auth_middleware_app(bind_host=bind_host)

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    async def echo_path(path: str) -> dict[str, str]:
        return {"path": path}

    return app


def test_non_loopback_hooks_require_auth() -> None:
    client = TestClient(_required_auth_app(bind_host="0.0.0.0"))

    response = client.post("/api/hooks/execute")

    assert response.status_code == 401


def test_non_loopback_hooks_accept_shared_token() -> None:
    client = TestClient(_required_auth_app(bind_host="0.0.0.0"))

    response = client.post(
        "/api/hooks/execute",
        headers={"Authorization": "Bearer shared-token"},
    )

    assert response.status_code == 200


def test_loopback_hooks_require_auth() -> None:
    client = TestClient(_required_auth_app(bind_host="127.0.0.1"))

    response = client.post("/api/hooks/execute")

    assert response.status_code == 401


def test_required_by_default(temp_db: HubDatabase) -> None:
    def services() -> ServiceContainer:
        return ServiceContainer(
            database=temp_db,
            session_manager=MagicMock(),
            task_manager=MagicMock(),
            text_generation_service=MagicMock(),
            tool_chat_service=MagicMock(),
            llm_service=MagicMock(),
        )

    server = HTTPServer(services())

    assert isinstance(server.auth_service, AuthService)
    assert type(server.auth_service) is AuthService


def test_cors_wraps_auth_rejections_and_protected_preflights(temp_db: HubDatabase) -> None:
    origin = "https://app.example.test"
    services = ServiceContainer(
        database=temp_db,
        session_manager=MagicMock(),
        task_manager=MagicMock(),
    )
    server = HTTPServer(services, startup_config=DaemonConfig(cors_origins=[origin]))
    client = TestClient(server.app)

    rejected = client.get("/api/tasks", headers={"Origin": origin})
    preflight = client.options(
        "/api/tasks",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
        },
    )

    assert rejected.status_code == 401
    assert rejected.headers["access-control-allow-origin"] == origin
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == origin


def test_public_prefix_matrix() -> None:
    client = TestClient(_required_auth_app())
    public_paths = (
        "/",
        "/api/auth/status",
        "/api/health",
        "/api/admin/startup-progress",
        "/api/comms/webhooks/slack",
        "/api/github/webhooks/triage/project",
        "/assets/index.js",
        "/favicon.ico",
        "/logo.png",
    )
    protected_paths = (
        "/api/health/details",
        "/api/admin/startup-progress/details",
        "/api/hooks/session-start",
        "/api/sessions/register",
        "/api/sessions/find_current",
        "/api/sessions/update_status",
        "/api/sessions/current",
        "/api/sessions/session-id/transcript",
        "/api/sessions/session-id/changes",
        "/api/sessions/session-id/expire",
        "/api/sessions/bulk-move",
        "/api/sessions/session-id/rename",
        "/api/sessions/session-id/stop",
        "/api/sessions/statusline",
        "/api/mcp",
        "/api/mcp/tools/call",
        "/api/mcp/servers",
        "/api/admin/status",
        "/api/admin/metrics",
        "/api/admin/config",
        "/mcp",
        "/memory",
    )

    for path in public_paths:
        assert client.get(path).status_code == 200, path
    for path in protected_paths:
        response = client.get(path)
        assert response.status_code == 401, path
        assert "gobby auth token --rotate" in response.json()["error"]


def test_public_webhooks_signature_gated() -> None:
    secret = b"webhook-secret"
    app = _required_auth_middleware_app()

    @app.post("/api/comms/webhooks/signed")
    async def comms_webhook(request: Request) -> Response:
        body = await request.body()
        expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(request.headers.get("x-comms-signature", ""), expected):
            return JSONResponse({}, status_code=401)
        return JSONResponse({"accepted": True})

    @app.post("/api/github/webhooks/signed")
    async def github_webhook(request: Request) -> Response:
        body = await request.body()
        expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(request.headers.get("x-hub-signature-256", ""), expected):
            return JSONResponse({}, status_code=401)
        return JSONResponse({"accepted": True})

    client = TestClient(app)
    body = b'{"event":"ping"}'
    comms_signature = hmac.new(secret, body, hashlib.sha256).hexdigest()
    github_signature = "sha256=" + comms_signature

    assert client.post("/api/comms/webhooks/signed", content=body).status_code == 401
    assert (
        client.post(
            "/api/comms/webhooks/signed",
            content=body,
            headers={"X-Comms-Signature": comms_signature},
        ).status_code
        == 200
    )
    assert client.post("/api/github/webhooks/signed", content=body).status_code == 401
    assert (
        client.post(
            "/api/github/webhooks/signed",
            content=body,
            headers={"X-Hub-Signature-256": github_signature},
        ).status_code
        == 200
    )


def test_bearer_and_alias_accepted(temp_db: HubDatabase, tmp_path: Path) -> None:
    token = "local-cli-token"
    auth_store = AuthStore(temp_db)
    auth_store.set_local_api_token_hash(hash_token(token))
    session_token, _ = auth_store.create_session(TEST_USER_ID)
    server = cast(
        HTTPServer,
        SimpleNamespace(
            auth_service=AuthService(
                lambda: temp_db,
                token_file=tmp_path / "missing",
            ),
            run_db=_run_db,
        ),
    )
    app = FastAPI()
    app.add_middleware(AuthMiddleware, server=server)

    @app.get("/api/tasks")
    async def protected() -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(app)

    bearer = client.get("/api/tasks", headers={"Authorization": f"Bearer {token}"})
    alias = client.get("/api/tasks", headers={"X-Gobby-Local-Token": token})
    client.cookies.set("gobby_session", session_token)
    cookie = client.get("/api/tasks")

    assert bearer.status_code == 200
    assert alias.status_code == 200
    assert cookie.status_code == 200


class TestLifespan:
    """Tests for FastAPI lifespan management."""

    def test_lifespan_does_not_start_web_chat_runtime(
        self,
        session_storage: SessionManager,
    ) -> None:
        """Subprocess startup is owned by the runner after the HTTP bind."""
        runtime_manager = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
        services = ServiceContainer(
            database=session_storage.db,
            session_manager=session_storage,
            task_manager=MagicMock(),
            web_chat_runtime_manager=runtime_manager,
        )
        server = HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
            startup_config=DaemonConfig(),
        )

        assert server._running is False
        with TestClient(server.app):
            assert server._running is True
            runtime_manager.start.assert_not_awaited()

        assert server._running is False
        runtime_manager.stop.assert_awaited_once_with()

    def test_lifespan_sets_running_flag(self, session_storage: SessionManager) -> None:
        """Test that lifespan sets _running flag."""
        services = ServiceContainer(
            database=session_storage.db,
            session_manager=session_storage,
            task_manager=MagicMock(),
        )
        server = HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
            startup_config=DaemonConfig(),
        )

        assert server._running is False

        with TestClient(server.app):
            assert server._running is True

    def test_lifespan_initializes_hook_manager(self, session_storage: SessionManager) -> None:
        """Test that lifespan initializes HookManager."""
        mock_config = MagicMock()
        mock_config.logging.dir = "/tmp"
        mock_config.logging.max_size_mb = 10
        mock_config.logging.backup_count = 3
        mock_config.workflow.timeout = 30
        mock_config.workflow.enabled = True

        memory_manager = MagicMock()
        services = ServiceContainer(
            database=session_storage.db,
            session_manager=session_storage,
            task_manager=MagicMock(),
            memory_manager=memory_manager,
        )

        server = HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
            startup_config=DaemonConfig(),
        )

        with patch("gobby.servers.app_factory.HookManager") as MockHM:
            MockHM.return_value.shutdown_async = AsyncMock()
            with TestClient(server.app):
                MockHM.assert_called_once()
                hook_manager_kwargs = MockHM.call_args.kwargs
                assert hook_manager_kwargs["database"] is session_storage.db
                assert hook_manager_kwargs["session_manager"] is session_storage
                assert hook_manager_kwargs["memory_manager"] is memory_manager

    def test_lifespan_rejects_non_awaitable_hook_manager_shutdown(
        self,
        session_storage: SessionManager,
    ) -> None:
        """HookManager shutdown_async must preserve its async contract."""
        services = ServiceContainer(
            database=session_storage.db,
            session_manager=session_storage,
            task_manager=MagicMock(),
        )
        server = HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
            startup_config=DaemonConfig(),
        )

        with patch("gobby.servers.app_factory.HookManager") as MockHM:
            MockHM.return_value.shutdown_async = MagicMock(return_value=None)
            with pytest.raises(RuntimeError, match="shutdown_async"):
                with TestClient(server.app):
                    pass

    def test_lifespan_cleans_up_voice_resources(self, session_storage: SessionManager) -> None:
        """Test that lifespan uses the explicit voice cleanup hook on shutdown."""
        services = ServiceContainer(
            database=session_storage.db,
            session_manager=session_storage,
            task_manager=MagicMock(),
        )
        server = HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
            startup_config=DaemonConfig(),
        )
        mock_ws_server = MagicMock()
        mock_ws_server.cleanup_voice = AsyncMock()
        server.websocket_server = mock_ws_server

        with TestClient(server.app):
            pass

        mock_ws_server.cleanup_voice.assert_awaited_once()
        assert mock_ws_server.cleanup_voice.await_count == 1
        assert mock_ws_server.cleanup_voice.await_args is not None

    def test_lifespan_logs_vision_cleanup_failure(
        self,
        session_storage: SessionManager,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Vision cleanup failures should not abort the rest of shutdown."""
        services = ServiceContainer(
            database=session_storage.db,
            session_manager=session_storage,
            task_manager=MagicMock(),
        )
        server = HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
            startup_config=DaemonConfig(),
        )

        with (
            patch(
                "gobby.servers.routes.llm.stop_vision_temp_cleanup_task",
                new=AsyncMock(side_effect=RuntimeError("cleanup failed")),
            ),
            caplog.at_level("WARNING", logger="gobby.servers.app_factory"),
        ):
            with TestClient(server.app):
                pass

        assert "Failed to stop vision temp cleanup task: cleanup failed" in caplog.text
