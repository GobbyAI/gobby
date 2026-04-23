"""HTTP server lifespan and middleware behavior tests."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from gobby.app_context import ServiceContainer
from gobby.servers.http import HTTPServer
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


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

        services = ServiceContainer(
            config=mock_config,
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
            with TestClient(server.app):
                MockHM.assert_called_once()

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
