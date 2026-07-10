"""HTTP server lifespan and middleware behavior tests."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.app_context import ServiceContainer
from gobby.servers.auth_service import AuthService
from gobby.servers.http import HTTPServer
from gobby.servers.middleware.auth import AuthMiddleware
from gobby.storage.auth import LOCAL_API_TOKEN_HASH_KEY, AuthStore, hash_token
from gobby.storage.config_store import ConfigStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


def test_bearer_and_alias_accepted(temp_db: HubDatabase, tmp_path: Path) -> None:
    token = "local-cli-token"
    ConfigStore(temp_db).set(LOCAL_API_TOKEN_HASH_KEY, hash_token(token), source="system")
    session_token, _ = AuthStore(temp_db).create_session()
    server = SimpleNamespace(
        auth_service=AuthService(lambda: temp_db, "required", token_file=tmp_path / "missing")
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

    def test_lifespan_sets_running_flag(self, session_storage: SessionManager) -> None:
        """Test that lifespan sets _running flag."""
        services = ServiceContainer(
            config=None,
            database=session_storage.db,
            session_manager=session_storage,
            task_manager=MagicMock(),
        )
        server = HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
        )

        assert server._running is False

        with TestClient(server.app):
            assert server._running is True

    def test_lifespan_initializes_hook_manager(self, session_storage: SessionManager) -> None:
        """Test that lifespan initializes HookManager."""
        mock_config = MagicMock()
        mock_config.logging.hook_manager = "/tmp/hooks.log"
        mock_config.logging.max_size_mb = 10
        mock_config.logging.backup_count = 3
        mock_config.workflow.timeout = 30
        mock_config.workflow.enabled = True

        memory_manager = MagicMock()
        services = ServiceContainer(
            config=mock_config,
            database=session_storage.db,
            session_manager=session_storage,
            task_manager=MagicMock(),
            memory_manager=memory_manager,
        )

        server = HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
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
            config=None,
            database=session_storage.db,
            session_manager=session_storage,
            task_manager=MagicMock(),
        )
        server = HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
        )

        with patch("gobby.servers.app_factory.HookManager") as MockHM:
            MockHM.return_value.shutdown_async = MagicMock(return_value=None)
            with pytest.raises(RuntimeError, match="shutdown_async"):
                with TestClient(server.app):
                    pass

    def test_lifespan_cleans_up_voice_resources(self, session_storage: SessionManager) -> None:
        """Test that lifespan uses the explicit voice cleanup hook on shutdown."""
        services = ServiceContainer(
            config=None,
            database=session_storage.db,
            session_manager=session_storage,
            task_manager=MagicMock(),
        )
        server = HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
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
            config=None,
            database=session_storage.db,
            session_manager=session_storage,
            task_manager=MagicMock(),
        )
        server = HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
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
