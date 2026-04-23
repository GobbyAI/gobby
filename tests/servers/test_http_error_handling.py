"""HTTP server exception handler tests."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from gobby.app_context import ServiceContainer
from gobby.servers.http import HTTPServer
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


class TestExceptionHandlers:
    """Tests for exception handlers."""

    def test_global_exception_handler_logs_details(self, session_storage: SessionManager) -> None:
        """Test that global exception handler logs request details."""
        services = ServiceContainer(
            config=MagicMock(),
            database=session_storage.db,
            session_manager=session_storage,
            task_manager=MagicMock(),
        )
        server = HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
        )

        @server.app.get("/trigger_error")
        def trigger_error() -> None:
            raise RuntimeError("Test error")

        client = TestClient(server.app, raise_server_exceptions=False)

        with patch("gobby.servers.exception_handlers.logger") as mock_logger:
            response = client.get("/trigger_error")

            assert mock_logger.error.called

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "error"
        assert data["error_logged"] is True

    def test_global_exception_handler_includes_path(self, session_storage: SessionManager) -> None:
        """Test exception handler includes request path in logs."""
        services = ServiceContainer(
            config=MagicMock(),
            database=session_storage.db,
            session_manager=session_storage,
            task_manager=MagicMock(),
        )
        server = HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
        )

        @server.app.get("/custom/error/path")
        def trigger_error() -> None:
            raise ValueError("Custom error")

        client = TestClient(server.app, raise_server_exceptions=False)
        response = client.get("/custom/error/path")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "error"

    def test_global_exception_handler_downgrades_client_disconnect(
        self, session_storage: SessionManager
    ) -> None:
        """Client disconnects should not be logged as unhandled server errors."""
        services = ServiceContainer(
            config=MagicMock(),
            database=session_storage.db,
            session_manager=session_storage,
            task_manager=MagicMock(),
        )
        server = HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
        )

        @server.app.get("/disconnect")
        def trigger_disconnect() -> None:
            raise ClientDisconnect()

        client = TestClient(server.app, raise_server_exceptions=False)

        with patch("gobby.servers.exception_handlers.logger") as mock_logger:
            response = client.get("/disconnect")

        assert response.status_code == 200
        assert response.json() == {"status": "ok", "warning": "client_disconnected"}
        assert mock_logger.error.called is False
        assert mock_logger.debug.called is True
