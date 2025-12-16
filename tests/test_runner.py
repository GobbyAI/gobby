"""Tests for src/runner.py - Gobby Daemon Runner."""

import asyncio
import signal
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

from gobby.runner import GobbyRunner, run_gobby, main


class TestGobbyRunnerInit:
    """Tests for GobbyRunner initialization."""

    def test_init_creates_components(self, tmp_path):
        """Test that init creates all required components."""
        mock_config = MagicMock()
        mock_config.daemon_port = 8765
        mock_config.websocket = MagicMock()
        mock_config.websocket.enabled = True
        mock_config.websocket.port = 8766
        mock_config.websocket.ping_interval = 30
        mock_config.websocket.ping_timeout = 10

        with (
            patch("gobby.runner.setup_file_logging"),
            patch("gobby.runner.setup_mcp_logging"),
            patch("gobby.runner.load_config", return_value=mock_config),
            patch("gobby.runner.get_machine_id", return_value="test-machine"),
            patch("gobby.runner.LocalDatabase") as mock_db,
            patch("gobby.runner.run_migrations"),
            patch("gobby.runner.LocalSessionManager"),
            patch("gobby.runner.LocalMCPManager"),
            patch("gobby.runner.MCPClientManager"),
            patch("gobby.runner.HTTPServer") as mock_http,
            patch("gobby.runner.WebSocketServer") as mock_ws,
        ):
            runner = GobbyRunner(config_path=tmp_path / "config.yaml", verbose=True)

            assert runner.config == mock_config
            assert runner.verbose is True
            assert runner.machine_id == "test-machine"
            assert runner._shutdown_requested is False
            mock_http.assert_called_once()
            mock_ws.assert_called_once()

    def test_init_without_websocket(self, tmp_path):
        """Test init when WebSocket is disabled."""
        mock_config = MagicMock()
        mock_config.daemon_port = 8765
        mock_config.websocket = MagicMock()
        mock_config.websocket.enabled = False

        with (
            patch("gobby.runner.setup_file_logging"),
            patch("gobby.runner.setup_mcp_logging"),
            patch("gobby.runner.load_config", return_value=mock_config),
            patch("gobby.runner.get_machine_id", return_value="test-machine"),
            patch("gobby.runner.LocalDatabase"),
            patch("gobby.runner.run_migrations"),
            patch("gobby.runner.LocalSessionManager"),
            patch("gobby.runner.LocalMCPManager"),
            patch("gobby.runner.MCPClientManager"),
            patch("gobby.runner.HTTPServer"),
            patch("gobby.runner.WebSocketServer") as mock_ws,
        ):
            runner = GobbyRunner()

            assert runner.websocket_server is None
            mock_ws.assert_not_called()

    def test_init_websocket_none_config(self):
        """Test init when websocket config is None."""
        mock_config = MagicMock()
        mock_config.daemon_port = 8765
        mock_config.websocket = None

        with (
            patch("gobby.runner.setup_file_logging"),
            patch("gobby.runner.setup_mcp_logging"),
            patch("gobby.runner.load_config", return_value=mock_config),
            patch("gobby.runner.get_machine_id", return_value="test-machine"),
            patch("gobby.runner.LocalDatabase"),
            patch("gobby.runner.run_migrations"),
            patch("gobby.runner.LocalSessionManager"),
            patch("gobby.runner.LocalMCPManager"),
            patch("gobby.runner.MCPClientManager"),
            patch("gobby.runner.HTTPServer"),
            patch("gobby.runner.WebSocketServer") as mock_ws,
        ):
            runner = GobbyRunner()

            assert runner.websocket_server is None


class TestGobbyRunnerSignalHandlers:
    """Tests for signal handler setup."""

    def test_setup_signal_handlers(self):
        """Test that signal handlers are registered."""
        mock_config = MagicMock()
        mock_config.daemon_port = 8765
        mock_config.websocket = None

        with (
            patch("gobby.runner.setup_file_logging"),
            patch("gobby.runner.setup_mcp_logging"),
            patch("gobby.runner.load_config", return_value=mock_config),
            patch("gobby.runner.get_machine_id"),
            patch("gobby.runner.LocalDatabase"),
            patch("gobby.runner.run_migrations"),
            patch("gobby.runner.LocalSessionManager"),
            patch("gobby.runner.LocalMCPManager"),
            patch("gobby.runner.MCPClientManager"),
            patch("gobby.runner.HTTPServer"),
        ):
            runner = GobbyRunner()

            # Create mock loop
            mock_loop = MagicMock()

            with patch("asyncio.get_running_loop", return_value=mock_loop):
                runner._setup_signal_handlers()

            # Verify signal handlers were added
            assert mock_loop.add_signal_handler.call_count == 2
            calls = mock_loop.add_signal_handler.call_args_list
            signals_registered = [call[0][0] for call in calls]
            assert signal.SIGTERM in signals_registered
            assert signal.SIGINT in signals_registered


class TestGobbyRunnerRun:
    """Tests for the run method."""

    @pytest.mark.asyncio
    async def test_run_connects_mcp_servers(self):
        """Test that run connects to MCP servers."""
        mock_config = MagicMock()
        mock_config.daemon_port = 8765
        mock_config.websocket = None

        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock()
        mock_mcp_manager.disconnect_all = AsyncMock()

        with (
            patch("gobby.runner.setup_file_logging"),
            patch("gobby.runner.setup_mcp_logging"),
            patch("gobby.runner.load_config", return_value=mock_config),
            patch("gobby.runner.get_machine_id"),
            patch("gobby.runner.LocalDatabase"),
            patch("gobby.runner.run_migrations"),
            patch("gobby.runner.LocalSessionManager"),
            patch("gobby.runner.LocalMCPManager"),
            patch("gobby.runner.MCPClientManager", return_value=mock_mcp_manager),
            patch("gobby.runner.HTTPServer") as mock_http,
        ):
            runner = GobbyRunner()

            # Set shutdown flag immediately to exit loop
            runner._shutdown_requested = True

            mock_http_instance = MagicMock()
            mock_http_instance.app = MagicMock()
            mock_http_instance.port = 8765
            mock_http.return_value = mock_http_instance

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch.object(runner, "_setup_signal_handlers"):
                    await runner.run()

            mock_mcp_manager.connect_all.assert_called_once()
            mock_mcp_manager.disconnect_all.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_handles_mcp_timeout(self):
        """Test that run handles MCP connection timeout."""
        mock_config = MagicMock()
        mock_config.daemon_port = 8765
        mock_config.websocket = None

        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock(side_effect=asyncio.TimeoutError())
        mock_mcp_manager.disconnect_all = AsyncMock()

        with (
            patch("gobby.runner.setup_file_logging"),
            patch("gobby.runner.setup_mcp_logging"),
            patch("gobby.runner.load_config", return_value=mock_config),
            patch("gobby.runner.get_machine_id"),
            patch("gobby.runner.LocalDatabase"),
            patch("gobby.runner.run_migrations"),
            patch("gobby.runner.LocalSessionManager"),
            patch("gobby.runner.LocalMCPManager"),
            patch("gobby.runner.MCPClientManager", return_value=mock_mcp_manager),
            patch("gobby.runner.HTTPServer") as mock_http,
        ):
            runner = GobbyRunner()
            runner._shutdown_requested = True

            mock_http_instance = MagicMock()
            mock_http_instance.app = MagicMock()
            mock_http_instance.port = 8765
            mock_http.return_value = mock_http_instance

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch.object(runner, "_setup_signal_handlers"):
                    # Should not raise - timeout is handled gracefully
                    await runner.run()

    @pytest.mark.asyncio
    async def test_run_handles_mcp_connection_error(self):
        """Test that run handles MCP connection errors."""
        mock_config = MagicMock()
        mock_config.daemon_port = 8765
        mock_config.websocket = None

        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock(side_effect=Exception("Connection failed"))
        mock_mcp_manager.disconnect_all = AsyncMock()

        with (
            patch("gobby.runner.setup_file_logging"),
            patch("gobby.runner.setup_mcp_logging"),
            patch("gobby.runner.load_config", return_value=mock_config),
            patch("gobby.runner.get_machine_id"),
            patch("gobby.runner.LocalDatabase"),
            patch("gobby.runner.run_migrations"),
            patch("gobby.runner.LocalSessionManager"),
            patch("gobby.runner.LocalMCPManager"),
            patch("gobby.runner.MCPClientManager", return_value=mock_mcp_manager),
            patch("gobby.runner.HTTPServer") as mock_http,
        ):
            runner = GobbyRunner()
            runner._shutdown_requested = True

            mock_http_instance = MagicMock()
            mock_http_instance.app = MagicMock()
            mock_http_instance.port = 8765
            mock_http.return_value = mock_http_instance

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch.object(runner, "_setup_signal_handlers"):
                    # Should not raise - error is logged but doesn't crash
                    await runner.run()

    @pytest.mark.asyncio
    async def test_run_with_websocket_server(self):
        """Test run with WebSocket server enabled."""
        mock_config = MagicMock()
        mock_config.daemon_port = 8765
        mock_config.websocket = MagicMock()
        mock_config.websocket.enabled = True
        mock_config.websocket.port = 8766
        mock_config.websocket.ping_interval = 30
        mock_config.websocket.ping_timeout = 10

        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock()
        mock_mcp_manager.disconnect_all = AsyncMock()

        mock_ws_server = AsyncMock()
        mock_ws_server.start = AsyncMock()

        with (
            patch("gobby.runner.setup_file_logging"),
            patch("gobby.runner.setup_mcp_logging"),
            patch("gobby.runner.load_config", return_value=mock_config),
            patch("gobby.runner.get_machine_id"),
            patch("gobby.runner.LocalDatabase"),
            patch("gobby.runner.run_migrations"),
            patch("gobby.runner.LocalSessionManager"),
            patch("gobby.runner.LocalMCPManager"),
            patch("gobby.runner.MCPClientManager", return_value=mock_mcp_manager),
            patch("gobby.runner.HTTPServer") as mock_http,
            patch("gobby.runner.WebSocketServer", return_value=mock_ws_server),
        ):
            runner = GobbyRunner()
            runner._shutdown_requested = True

            mock_http_instance = MagicMock()
            mock_http_instance.app = MagicMock()
            mock_http_instance.port = 8765
            mock_http.return_value = mock_http_instance

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch.object(runner, "_setup_signal_handlers"):
                    await runner.run()

            # WebSocket server start should be called
            mock_ws_server.start.assert_called()

    @pytest.mark.asyncio
    async def test_run_passes_websocket_to_http(self):
        """Test that run passes WebSocket server reference to HTTP server."""
        mock_config = MagicMock()
        mock_config.daemon_port = 8765
        mock_config.websocket = MagicMock()
        mock_config.websocket.enabled = True
        mock_config.websocket.port = 8766
        mock_config.websocket.ping_interval = 30
        mock_config.websocket.ping_timeout = 10

        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock()
        mock_mcp_manager.disconnect_all = AsyncMock()

        mock_ws_server = AsyncMock()
        mock_ws_server.start = AsyncMock()

        with (
            patch("gobby.runner.setup_file_logging"),
            patch("gobby.runner.setup_mcp_logging"),
            patch("gobby.runner.load_config", return_value=mock_config),
            patch("gobby.runner.get_machine_id"),
            patch("gobby.runner.LocalDatabase"),
            patch("gobby.runner.run_migrations"),
            patch("gobby.runner.LocalSessionManager"),
            patch("gobby.runner.LocalMCPManager"),
            patch("gobby.runner.MCPClientManager", return_value=mock_mcp_manager),
            patch("gobby.runner.HTTPServer") as mock_http,
            patch("gobby.runner.WebSocketServer", return_value=mock_ws_server),
        ):
            runner = GobbyRunner()

            # Verify reference was passed
            # In our implementation, we set it on the http_server instance
            assert runner.http_server.websocket_server == mock_ws_server


class TestRunGobbyFunction:
    """Tests for run_gobby async function."""

    @pytest.mark.asyncio
    async def test_run_gobby_creates_runner(self):
        """Test that run_gobby creates and runs GobbyRunner."""
        with patch("gobby.runner.GobbyRunner") as mock_runner_cls:
            mock_runner = AsyncMock()
            mock_runner.run = AsyncMock()
            mock_runner_cls.return_value = mock_runner

            await run_gobby(config_path=Path("/tmp/config.yaml"), verbose=True)

            mock_runner_cls.assert_called_once_with(
                config_path=Path("/tmp/config.yaml"), verbose=True
            )
            mock_runner.run.assert_called_once()


class TestMainFunction:
    """Tests for main synchronous entry point."""

    def test_main_runs_asyncio(self):
        """Test that main runs the async runner."""
        with patch("asyncio.run") as mock_run:
            with patch("gobby.runner.run_gobby") as mock_run_gobby:
                main(config_path=Path("/tmp/config.yaml"), verbose=True)

            mock_run.assert_called_once()

    def test_main_handles_keyboard_interrupt(self):
        """Test that main handles KeyboardInterrupt gracefully."""
        with patch("asyncio.run", side_effect=KeyboardInterrupt()):
            with pytest.raises(SystemExit) as exc_info:
                main()

            assert exc_info.value.code == 0

    def test_main_handles_exception(self):
        """Test that main handles exceptions and exits with code 1."""
        with patch("asyncio.run", side_effect=Exception("Test error")):
            with pytest.raises(SystemExit) as exc_info:
                main()

            assert exc_info.value.code == 1
