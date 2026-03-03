"""Tests for src/runner.py - Gobby Daemon Runner."""

import signal
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.runner import GobbyRunner, main, run_gobby

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_config():
    """Create a mock config with WebSocket disabled by default."""
    config = MagicMock()
    config.daemon_port = 60887
    config.websocket = None
    config.session_lifecycle = MagicMock()
    config.message_tracking = None
    config.memory_sync = MagicMock()
    config.memory_sync.enabled = False
    return config


@pytest.fixture
def mock_config_with_websocket():
    """Create a mock config with WebSocket enabled."""
    config = MagicMock()
    config.daemon_port = 60887
    config.websocket = MagicMock()
    config.websocket.enabled = True
    config.websocket.port = 60888
    config.websocket.ping_interval = 30
    config.websocket.ping_timeout = 10
    config.session_lifecycle = MagicMock()
    config.message_tracking = None
    config.memory_sync = MagicMock()
    config.memory_sync.enabled = False
    return config


def create_base_patches(
    mock_config=None,
    mock_mcp_manager=None,
    mock_http=None,
    mock_ws_server=None,
):
    """Create all standard patches needed for GobbyRunner tests.

    Args:
        mock_config: Optional config mock. If None, uses a default mock.
        mock_mcp_manager: Optional MCPClientManager mock.
        mock_http: Optional HTTPServer mock.
        mock_ws_server: Optional WebSocketServer mock.

    Returns a list of patch objects that should be used with ExitStack.
    """
    # Create default mocks if not provided
    if mock_mcp_manager is None:
        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock()
        mock_mcp_manager.disconnect_all = AsyncMock()

    if mock_http is None:
        mock_http = MagicMock()
        mock_http.app = MagicMock()
        mock_http.port = 60887

    patches = [
        patch("gobby.runner.setup_file_logging"),
        patch("gobby.runner.get_machine_id", return_value="test-machine"),
        patch("gobby.runner.LocalDatabase"),
        patch("gobby.runner.run_migrations"),
        patch("gobby.runner.LocalSessionManager"),
        patch("gobby.runner.LocalSessionMessageManager"),
        patch("gobby.runner.LocalTaskManager"),
        patch("gobby.runner.SessionTaskManager"),
        patch("gobby.runner.MCPClientManager", return_value=mock_mcp_manager),
        patch("gobby.runner.TaskSyncManager"),
        patch("gobby.runner.MemorySyncManager"),
        patch("gobby.runner.SessionMessageProcessor", return_value=AsyncMock()),
        patch("gobby.runner.TaskValidator"),
        patch("gobby.runner.SessionLifecycleManager", return_value=AsyncMock()),
        patch("gobby.runner.create_llm_service", return_value=None),
        patch("gobby.runner.MemoryManager", return_value=None),
        patch("gobby.runner.HTTPServer", return_value=mock_http),
        patch("gobby.storage.secrets.SecretStore"),
        patch("gobby.storage.config_store.ConfigStore"),
    ]

    # Add config patch
    if mock_config is not None:
        patches.insert(1, patch("gobby.runner.load_config", return_value=mock_config))
    else:
        patches.insert(1, patch("gobby.runner.load_config"))

    # Add WebSocketServer patch
    if mock_ws_server is not None:
        patches.append(patch("gobby.runner.WebSocketServer", return_value=mock_ws_server))
    else:
        patches.append(patch("gobby.runner.WebSocketServer"))

    return patches


class TestGobbyRunnerInit:
    """Tests for GobbyRunner initialization."""

    def test_init_creates_components(self, tmp_path, mock_config_with_websocket) -> None:
        """Test that init creates all required components."""
        patches = create_base_patches(mock_config=mock_config_with_websocket)

        # Create the config file so the existence check passes
        config_file = tmp_path / "config.yaml"
        config_file.write_text("")

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in patches]
            mock_http_cls = mocks[-4]
            mock_ws_cls = mocks[-1]

            runner = GobbyRunner(config_path=config_file, verbose=True)

            assert runner.config == mock_config_with_websocket
            assert runner.verbose is True
            assert runner.machine_id == "test-machine"
            assert runner._shutdown_requested is False
            mock_http_cls.assert_called_once()
            mock_ws_cls.assert_called_once()

    def test_init_without_websocket(self, mock_config) -> None:
        """Test init when WebSocket is disabled."""
        mock_config.websocket = MagicMock()
        mock_config.websocket.enabled = False

        patches = create_base_patches(mock_config)

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in patches]
            mock_ws_cls = mocks[-1]

            runner = GobbyRunner()

            assert runner.websocket_server is None
            mock_ws_cls.assert_not_called()

    def test_init_websocket_none_config(self, mock_config) -> None:
        """Test init when websocket config is None."""
        patches = create_base_patches(mock_config)

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            assert runner.websocket_server is None


class TestGobbyRunnerSignalHandlers:
    """Tests for signal handler setup."""

    def test_setup_signal_handlers(self) -> None:
        """Test that signal handlers are registered."""
        from gobby.runner_maintenance import setup_signal_handlers

        mock_loop = MagicMock()
        mock_callback = MagicMock()

        with patch("asyncio.get_running_loop", return_value=mock_loop):
            setup_signal_handlers(mock_callback)

        # Verify signal handlers were added
        assert mock_loop.add_signal_handler.call_count == 2
        calls = mock_loop.add_signal_handler.call_args_list
        signals_registered = [call[0][0] for call in calls]
        assert signal.SIGTERM in signals_registered
        assert signal.SIGINT in signals_registered


class TestGobbyRunnerRun:
    """Tests for the run method."""

    @pytest.mark.asyncio
    async def test_run_connects_mcp_servers(self, mock_config):
        """Test that run connects to MCP servers."""
        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock()
        mock_mcp_manager.disconnect_all = AsyncMock()

        patches = create_base_patches(
            mock_config=mock_config,
            mock_mcp_manager=mock_mcp_manager,
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            runner._shutdown_requested = True

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    await runner.run()

            mock_mcp_manager.connect_all.assert_called_once()
            mock_mcp_manager.disconnect_all.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_handles_mcp_timeout(self, mock_config):
        """Test that run handles MCP connection timeout."""
        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock(side_effect=TimeoutError())
        mock_mcp_manager.disconnect_all = AsyncMock()

        patches = create_base_patches(
            mock_config=mock_config,
            mock_mcp_manager=mock_mcp_manager,
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            runner._shutdown_requested = True

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    # Should not raise - timeout is handled gracefully
                    await runner.run()

    @pytest.mark.asyncio
    async def test_run_handles_mcp_connection_error(self, mock_config):
        """Test that run handles MCP connection errors."""
        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock(side_effect=Exception("Connection failed"))
        mock_mcp_manager.disconnect_all = AsyncMock()

        patches = create_base_patches(
            mock_config=mock_config,
            mock_mcp_manager=mock_mcp_manager,
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            runner._shutdown_requested = True

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    # Should not raise - error is logged but doesn't crash
                    await runner.run()

    @pytest.mark.asyncio
    async def test_run_with_websocket_server(self, mock_config_with_websocket):
        """Test run with WebSocket server enabled."""
        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock()
        mock_mcp_manager.disconnect_all = AsyncMock()

        mock_ws_server = AsyncMock()
        mock_ws_server.start = AsyncMock()

        patches = create_base_patches(
            mock_config=mock_config_with_websocket,
            mock_mcp_manager=mock_mcp_manager,
            mock_ws_server=mock_ws_server,
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            runner._shutdown_requested = True

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    await runner.run()

            # WebSocket server start should be called
            mock_ws_server.start.assert_called()

    @pytest.mark.asyncio
    async def test_run_passes_websocket_to_http(self, mock_config_with_websocket):
        """Test that run passes WebSocket server reference to HTTP server."""
        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock()
        mock_mcp_manager.disconnect_all = AsyncMock()

        mock_ws_server = AsyncMock()
        mock_ws_server.start = AsyncMock()

        mock_http = MagicMock()
        mock_http.app = MagicMock()
        mock_http.port = 60887

        patches = create_base_patches(
            mock_config=mock_config_with_websocket,
            mock_mcp_manager=mock_mcp_manager,
            mock_http=mock_http,
            mock_ws_server=mock_ws_server,
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

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

    def test_main_runs_asyncio(self) -> None:
        """Test that main runs the async runner."""
        with patch("asyncio.run") as mock_run:
            with patch("gobby.runner.run_gobby") as mock_run_gobby:
                mock_run_gobby.return_value = None
                main(config_path=Path("/tmp/config.yaml"), verbose=True)

            mock_run.assert_called_once()

    def test_main_handles_keyboard_interrupt(self) -> None:
        """Test that main handles KeyboardInterrupt gracefully."""
        with patch("asyncio.run", side_effect=KeyboardInterrupt()):
            with patch("gobby.runner.run_gobby") as mock_run_gobby:
                mock_run_gobby.return_value = None
                with pytest.raises(SystemExit) as exc_info:
                    main()

            assert exc_info.value.code == 0

    def test_main_handles_exception(self) -> None:
        """Test that main handles exceptions and exits with code 1."""
        with patch("asyncio.run", side_effect=Exception("Test error")):
            with patch("gobby.runner.run_gobby") as mock_run_gobby:
                mock_run_gobby.return_value = None
                with pytest.raises(SystemExit) as exc_info:
                    main()

            assert exc_info.value.code == 1


class TestGobbyRunnerInitialization:
    """Tests for component initialization during GobbyRunner.__init__."""

    def test_init_with_memory_manager(self) -> None:
        """Test that MemoryManager is initialized when memory config exists."""
        mock_config = MagicMock()
        mock_config.daemon_port = 60887
        mock_config.websocket = None
        mock_config.session_lifecycle = MagicMock()
        mock_config.message_tracking = None
        mock_config.memory_sync = MagicMock()
        mock_config.memory_sync.enabled = False
        mock_config.memory = MagicMock()  # Has memory config

        mock_memory_manager = MagicMock()

        patches = create_base_patches(mock_config=mock_config)
        # Replace MemoryManager patch to return our mock
        patches = [p for p in patches if "MemoryManager" not in str(p)]
        patches.append(patch("gobby.runner.MemoryManager", return_value=mock_memory_manager))

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            assert runner.memory_manager == mock_memory_manager

    def test_init_memory_manager_exception(self) -> None:
        """Test that MemoryManager initialization exception is handled."""
        mock_config = MagicMock()
        mock_config.daemon_port = 60887
        mock_config.websocket = None
        mock_config.session_lifecycle = MagicMock()
        mock_config.message_tracking = None
        mock_config.memory_sync = MagicMock()
        mock_config.memory_sync.enabled = False
        mock_config.memory = MagicMock()

        patches = create_base_patches(mock_config=mock_config)
        patches = [p for p in patches if "MemoryManager" not in str(p)]
        patches.append(
            patch("gobby.runner.MemoryManager", side_effect=Exception("Memory init error"))
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            # Should not raise - error is logged
            runner = GobbyRunner()
            assert runner.memory_manager is None

    def test_init_with_memory_sync_manager(self) -> None:
        """Test MemorySyncManager initialization when enabled."""
        mock_config = MagicMock()
        mock_config.daemon_port = 60887
        mock_config.websocket = None
        mock_config.session_lifecycle = MagicMock()
        mock_config.message_tracking = None
        mock_config.memory = MagicMock()
        mock_config.memory_sync = MagicMock()
        mock_config.memory_sync.enabled = True

        mock_memory_manager = MagicMock()
        mock_memory_manager.storage = MagicMock()
        mock_memory_sync_manager = MagicMock()
        mock_memory_sync_manager.import_sync.return_value = 0

        patches = create_base_patches(mock_config=mock_config)
        patches = [p for p in patches if "MemoryManager" not in str(p)]
        patches = [p for p in patches if "MemorySyncManager" not in str(p)]
        patches.append(patch("gobby.runner.MemoryManager", return_value=mock_memory_manager))
        patches.append(
            patch("gobby.runner.MemorySyncManager", return_value=mock_memory_sync_manager)
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            assert runner.memory_sync_manager == mock_memory_sync_manager
            mock_memory_sync_manager.import_sync.assert_called_once()
            mock_memory_sync_manager.export_sync.assert_called_once()

    def test_init_memory_sync_manager_exception(self) -> None:
        """Test MemorySyncManager initialization exception is handled."""
        mock_config = MagicMock()
        mock_config.daemon_port = 60887
        mock_config.websocket = None
        mock_config.session_lifecycle = MagicMock()
        mock_config.message_tracking = None
        mock_config.memory = MagicMock()
        mock_config.memory_sync = MagicMock()
        mock_config.memory_sync.enabled = True

        mock_memory_manager = MagicMock()
        mock_memory_manager.storage = MagicMock()

        patches = create_base_patches(mock_config=mock_config)
        patches = [p for p in patches if "MemoryManager" not in str(p)]
        patches = [p for p in patches if "MemorySyncManager" not in str(p)]
        patches.append(patch("gobby.runner.MemoryManager", return_value=mock_memory_manager))
        patches.append(
            patch("gobby.runner.MemorySyncManager", side_effect=Exception("Sync manager error"))
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            assert runner.memory_sync_manager is None

    def test_init_with_message_processor(self) -> None:
        """Test SessionMessageProcessor initialization when message_tracking enabled."""
        mock_config = MagicMock()
        mock_config.daemon_port = 60887
        mock_config.websocket = None
        mock_config.session_lifecycle = MagicMock()
        mock_config.memory_sync = MagicMock()
        mock_config.memory_sync.enabled = False
        mock_config.message_tracking = MagicMock()
        mock_config.message_tracking.enabled = True
        mock_config.message_tracking.poll_interval = 5.0

        mock_message_processor = AsyncMock()

        patches = create_base_patches(mock_config=mock_config)
        patches = [p for p in patches if "SessionMessageProcessor" not in str(p)]
        patches.append(
            patch("gobby.runner.SessionMessageProcessor", return_value=mock_message_processor)
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            assert runner.message_processor == mock_message_processor

    def test_init_with_task_validator(self) -> None:
        """Test TaskValidator initialization when LLM service and validation enabled."""
        mock_config = MagicMock()
        mock_config.daemon_port = 60887
        mock_config.websocket = None
        mock_config.session_lifecycle = MagicMock()
        mock_config.message_tracking = None
        mock_config.memory_sync = MagicMock()
        mock_config.memory_sync.enabled = False
        mock_config.gobby_tasks = MagicMock()
        mock_config.gobby_tasks.expansion = MagicMock()
        mock_config.gobby_tasks.expansion.enabled = False
        mock_config.gobby_tasks.validation = MagicMock()
        mock_config.gobby_tasks.validation.enabled = True

        mock_llm_service = MagicMock()
        mock_llm_service.enabled_providers = ["test"]
        mock_task_validator = MagicMock()

        patches = create_base_patches(mock_config=mock_config)
        patches = [p for p in patches if "create_llm_service" not in str(p)]
        patches = [p for p in patches if "TaskValidator" not in str(p)]
        patches.append(patch("gobby.runner.create_llm_service", return_value=mock_llm_service))
        patches.append(patch("gobby.runner.TaskValidator", return_value=mock_task_validator))

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            assert runner.task_validator == mock_task_validator

    def test_init_task_validator_exception(self) -> None:
        """Test TaskValidator initialization exception is handled."""
        mock_config = MagicMock()
        mock_config.daemon_port = 60887
        mock_config.websocket = None
        mock_config.session_lifecycle = MagicMock()
        mock_config.message_tracking = None
        mock_config.memory_sync = MagicMock()
        mock_config.memory_sync.enabled = False
        mock_config.gobby_tasks = MagicMock()
        mock_config.gobby_tasks.expansion = MagicMock()
        mock_config.gobby_tasks.expansion.enabled = False
        mock_config.gobby_tasks.validation = MagicMock()
        mock_config.gobby_tasks.validation.enabled = True

        mock_llm_service = MagicMock()
        mock_llm_service.enabled_providers = ["test"]

        patches = create_base_patches(mock_config=mock_config)
        patches = [p for p in patches if "create_llm_service" not in str(p)]
        patches = [p for p in patches if "TaskValidator" not in str(p)]
        patches.append(patch("gobby.runner.create_llm_service", return_value=mock_llm_service))
        patches.append(
            patch("gobby.runner.TaskValidator", side_effect=Exception("Validator error"))
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            assert runner.task_validator is None

    def test_init_agent_runner_exception(self) -> None:
        """Test AgentRunner initialization exception is handled."""
        mock_config = MagicMock()
        mock_config.daemon_port = 60887
        mock_config.websocket = None
        mock_config.session_lifecycle = MagicMock()
        mock_config.message_tracking = None
        mock_config.memory_sync = MagicMock()
        mock_config.memory_sync.enabled = False

        patches = create_base_patches(mock_config=mock_config)
        patches.append(
            patch("gobby.runner.AgentRunner", side_effect=Exception("Agent runner error"))
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            assert runner.agent_runner is None

    def test_init_llm_service_exception(self) -> None:
        """Test LLM service initialization exception is handled."""
        mock_config = MagicMock()
        mock_config.daemon_port = 60887
        mock_config.websocket = None
        mock_config.session_lifecycle = MagicMock()
        mock_config.message_tracking = None
        mock_config.memory_sync = MagicMock()
        mock_config.memory_sync.enabled = False

        patches = create_base_patches(mock_config=mock_config)
        patches = [p for p in patches if "create_llm_service" not in str(p)]
        patches.append(
            patch("gobby.runner.create_llm_service", side_effect=Exception("LLM init error"))
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            assert runner.llm_service is None


class TestAgentEventBroadcasting:
    """Tests for setup_agent_event_broadcasting function."""

    def test_setup_agent_event_broadcasting_with_websocket(
        self, mock_config_with_websocket
    ) -> None:
        """Test agent event broadcasting setup when WebSocket is enabled."""
        mock_ws_server = AsyncMock()
        mock_ws_server.start = AsyncMock()
        mock_ws_server.broadcast_agent_event = AsyncMock()

        mock_registry = MagicMock()
        mock_registry.add_event_callback = MagicMock()

        patches = create_base_patches(
            mock_config=mock_config_with_websocket,
            mock_ws_server=mock_ws_server,
        )
        # Patch at the broadcasting module (it's imported there)
        patches.append(
            patch(
                "gobby.agents.registry.get_running_agent_registry",
                return_value=mock_registry,
            )
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            GobbyRunner()  # Constructor triggers event broadcasting setup

            # Verify callback was registered
            mock_registry.add_event_callback.assert_called_once()

    def test_setup_agent_event_broadcasting_without_websocket(self, mock_config) -> None:
        """Test agent event broadcasting is skipped without WebSocket."""
        mock_registry = MagicMock()
        mock_registry.add_event_callback = MagicMock()

        patches = create_base_patches(mock_config=mock_config)
        # Patch at the broadcasting module (it's imported there)
        patches.append(
            patch(
                "gobby.agents.registry.get_running_agent_registry",
                return_value=mock_registry,
            )
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            GobbyRunner()  # Constructor runs without WebSocket

            # Callback should NOT be registered since no websocket
            mock_registry.add_event_callback.assert_not_called()

    def test_setup_agent_event_broadcasting_direct_call_without_websocket(self) -> None:
        """Test setup_agent_event_broadcasting does not crash when called with a mock server."""
        from gobby.runner_broadcasting import setup_agent_event_broadcasting

        mock_registry = MagicMock()
        mock_registry.add_event_callback = MagicMock()

        mock_ws_server = MagicMock()

        # Call the module-level function directly with a mock server
        with (
            patch(
                "gobby.agents.registry.get_running_agent_registry",
                return_value=mock_registry,
            ),
            patch("gobby.agents.pty_reader.get_pty_reader_manager"),
            patch("gobby.agents.tmux.get_tmux_output_reader"),
        ):
            setup_agent_event_broadcasting(mock_ws_server)

        # Registry callback should have been registered
        mock_registry.add_event_callback.assert_called_once()


class TestMetricsCleanupLoop:
    """Tests for metrics_cleanup_loop function."""

    @pytest.mark.asyncio
    async def test_metrics_cleanup_loop_runs_cleanup(self, mock_config):
        """Test that metrics cleanup loop runs cleanup."""
        import asyncio

        from gobby.runner_maintenance import metrics_cleanup_loop

        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock()
        mock_mcp_manager.disconnect_all = AsyncMock()

        patches = create_base_patches(
            mock_config=mock_config,
            mock_mcp_manager=mock_mcp_manager,
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            runner.metrics_manager.cleanup_old_metrics = MagicMock(return_value=5)

            shutdown_requested = False

            def is_shutdown():
                return shutdown_requested

            # Start the loop and cancel it after a short time
            task = asyncio.create_task(metrics_cleanup_loop(runner.metrics_manager, is_shutdown))

            # Give it a tiny bit of time then request shutdown
            await asyncio.sleep(0.01)
            shutdown_requested = True

            # Wait for the task to finish
            try:
                await asyncio.wait_for(task, timeout=1.0)
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_metrics_cleanup_loop_handles_exception(self, mock_config):
        """Test that metrics cleanup loop handles exceptions gracefully."""
        import asyncio

        from gobby.runner_maintenance import metrics_cleanup_loop

        patches = create_base_patches(mock_config=mock_config)

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            runner.metrics_manager.cleanup_old_metrics = MagicMock(
                side_effect=Exception("Cleanup error")
            )

            shutdown_requested = False

            def is_shutdown():
                return shutdown_requested

            # Start the loop
            task = asyncio.create_task(metrics_cleanup_loop(runner.metrics_manager, is_shutdown))

            # Request shutdown after a very brief moment
            await asyncio.sleep(0.01)
            shutdown_requested = True

            # Wait for the task
            try:
                await asyncio.wait_for(task, timeout=1.0)
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_metrics_cleanup_loop_cancelled(self, mock_config):
        """Test that metrics cleanup loop handles cancellation."""
        import asyncio

        from gobby.runner_maintenance import metrics_cleanup_loop

        patches = create_base_patches(mock_config=mock_config)

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            task = asyncio.create_task(metrics_cleanup_loop(runner.metrics_manager, lambda: False))
            await asyncio.sleep(0.01)
            task.cancel()

            try:
                await task
            except asyncio.CancelledError:
                pass


class TestGobbyRunnerShutdown:
    """Tests for shutdown handling in run method."""

    @pytest.mark.asyncio
    async def test_run_handles_http_server_shutdown_timeout(self, mock_config):
        """Test that run handles HTTP server shutdown timeout."""
        import asyncio

        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock()
        mock_mcp_manager.disconnect_all = AsyncMock()

        patches = create_base_patches(
            mock_config=mock_config,
            mock_mcp_manager=mock_mcp_manager,
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            runner._shutdown_requested = True

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = MagicMock()

                # Create a task that hangs to simulate timeout
                async def hanging_serve():
                    await asyncio.sleep(100)

                mock_server.serve = hanging_serve
                mock_server.should_exit = False
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    # Should complete without hanging due to timeout handling
                    # Timeout must exceed graceful_shutdown_timeout (15) + buffer (5) + cleanup
                    await asyncio.wait_for(runner.run(), timeout=25.0)

    @pytest.mark.asyncio
    async def test_run_handles_lifecycle_manager_shutdown_timeout(self, mock_config):
        """Test that run handles lifecycle manager shutdown timeout."""
        import asyncio

        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock()
        mock_mcp_manager.disconnect_all = AsyncMock()

        mock_lifecycle_manager = AsyncMock()
        mock_lifecycle_manager.start = AsyncMock()

        async def hanging_stop():
            await asyncio.sleep(100)

        mock_lifecycle_manager.stop = hanging_stop

        patches = create_base_patches(
            mock_config=mock_config,
            mock_mcp_manager=mock_mcp_manager,
        )
        patches = [p for p in patches if "SessionLifecycleManager" not in str(p)]
        patches.append(
            patch("gobby.runner.SessionLifecycleManager", return_value=mock_lifecycle_manager)
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            runner._shutdown_requested = True

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    await asyncio.wait_for(runner.run(), timeout=10.0)

    @pytest.mark.asyncio
    async def test_run_handles_message_processor_shutdown_timeout(self, mock_config):
        """Test that run handles message processor shutdown timeout."""
        import asyncio

        mock_config.message_tracking = MagicMock()
        mock_config.message_tracking.enabled = True
        mock_config.message_tracking.poll_interval = 5.0

        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock()
        mock_mcp_manager.disconnect_all = AsyncMock()

        mock_message_processor = AsyncMock()
        mock_message_processor.start = AsyncMock()

        async def hanging_stop():
            await asyncio.sleep(100)

        mock_message_processor.stop = hanging_stop

        patches = create_base_patches(
            mock_config=mock_config,
            mock_mcp_manager=mock_mcp_manager,
        )
        patches = [p for p in patches if "SessionMessageProcessor" not in str(p)]
        patches.append(
            patch("gobby.runner.SessionMessageProcessor", return_value=mock_message_processor)
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            runner._shutdown_requested = True

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    await asyncio.wait_for(runner.run(), timeout=10.0)

    @pytest.mark.asyncio
    async def test_run_handles_mcp_disconnect_timeout(self, mock_config):
        """Test that run handles MCP disconnect timeout."""
        import asyncio

        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock()

        async def hanging_disconnect():
            await asyncio.sleep(100)

        mock_mcp_manager.disconnect_all = hanging_disconnect

        patches = create_base_patches(
            mock_config=mock_config,
            mock_mcp_manager=mock_mcp_manager,
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            runner._shutdown_requested = True

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    await asyncio.wait_for(runner.run(), timeout=10.0)

    @pytest.mark.asyncio
    async def test_run_starts_message_processor(self, mock_config):
        """Test that run starts the message processor when enabled."""
        mock_config.message_tracking = MagicMock()
        mock_config.message_tracking.enabled = True
        mock_config.message_tracking.poll_interval = 5.0

        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock()
        mock_mcp_manager.disconnect_all = AsyncMock()

        mock_message_processor = AsyncMock()
        mock_message_processor.start = AsyncMock()
        mock_message_processor.stop = AsyncMock()

        patches = create_base_patches(
            mock_config=mock_config,
            mock_mcp_manager=mock_mcp_manager,
        )
        patches = [p for p in patches if "SessionMessageProcessor" not in str(p)]
        patches.append(
            patch("gobby.runner.SessionMessageProcessor", return_value=mock_message_processor)
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            runner._shutdown_requested = True

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    await runner.run()

            mock_message_processor.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_runs_startup_metrics_cleanup(self, mock_config):
        """Test that run performs startup metrics cleanup."""
        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock()
        mock_mcp_manager.disconnect_all = AsyncMock()

        patches = create_base_patches(
            mock_config=mock_config,
            mock_mcp_manager=mock_mcp_manager,
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            runner.metrics_manager.cleanup_old_metrics = MagicMock(return_value=10)
            runner._shutdown_requested = True

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    await runner.run()

            runner.metrics_manager.cleanup_old_metrics.assert_called()

    @pytest.mark.asyncio
    async def test_run_handles_startup_metrics_cleanup_error(self, mock_config):
        """Test that run handles startup metrics cleanup errors."""
        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock()
        mock_mcp_manager.disconnect_all = AsyncMock()

        patches = create_base_patches(
            mock_config=mock_config,
            mock_mcp_manager=mock_mcp_manager,
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            runner.metrics_manager.cleanup_old_metrics = MagicMock(
                side_effect=Exception("Cleanup failed")
            )
            runner._shutdown_requested = True

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    # Should not raise - error is logged
                    await runner.run()

    @pytest.mark.asyncio
    async def test_run_fatal_error_exits(self, mock_config):
        """Test that run exits on fatal error."""

        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock()
        mock_mcp_manager.disconnect_all = AsyncMock()

        patches = create_base_patches(
            mock_config=mock_config,
            mock_mcp_manager=mock_mcp_manager,
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            # Make signal handler setup raise an exception
            with (
                patch(
                    "gobby.runner_maintenance.setup_signal_handlers",
                    side_effect=Exception("Fatal error"),
                ),
                pytest.raises(SystemExit) as exc_info,
            ):
                await runner.run()

            assert exc_info.value.code == 1

    @pytest.mark.asyncio
    async def test_run_cancels_metrics_cleanup_task_on_shutdown(self, mock_config):
        """Test that metrics cleanup task is cancelled on shutdown."""

        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock()
        mock_mcp_manager.disconnect_all = AsyncMock()

        patches = create_base_patches(
            mock_config=mock_config,
            mock_mcp_manager=mock_mcp_manager,
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            runner._shutdown_requested = True

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    await runner.run()

            # The cleanup task should have been created and then cancelled
            # Since shutdown was immediate, task should be done or cancelled
            assert (
                runner._metrics_cleanup_task is None
                or runner._metrics_cleanup_task.done()
                or runner._metrics_cleanup_task.cancelled()
            )


class TestSignalHandlerBehavior:
    """Tests for signal handler behavior."""

    def test_signal_handler_invokes_shutdown_callback(self) -> None:
        """Test that the signal handler invokes the shutdown callback."""
        from gobby.runner_maintenance import setup_signal_handlers

        mock_loop = MagicMock()
        captured_handler = None

        def capture_handler(sig, handler):
            nonlocal captured_handler
            if sig == signal.SIGTERM:
                captured_handler = handler

        mock_loop.add_signal_handler = capture_handler

        shutdown_called = False

        def shutdown_callback():
            nonlocal shutdown_called
            shutdown_called = True

        with patch("asyncio.get_running_loop", return_value=mock_loop):
            setup_signal_handlers(shutdown_callback)

        # Verify handler was captured
        assert captured_handler is not None

        # Call the handler
        assert shutdown_called is False
        captured_handler()
        assert shutdown_called is True


class TestAgentEventBroadcastingCallback:
    """Tests for the broadcast_agent_event callback function."""

    @pytest.mark.asyncio
    async def test_broadcast_callback_invoked(self, mock_config_with_websocket):
        """Test that the broadcast callback is properly invoked."""
        import asyncio

        mock_ws_server = AsyncMock()
        mock_ws_server.start = AsyncMock()
        mock_ws_server.broadcast_agent_event = AsyncMock()

        mock_registry = MagicMock()
        captured_callback = None

        def capture_callback(callback):
            nonlocal captured_callback
            captured_callback = callback

        mock_registry.add_event_callback = capture_callback

        patches = create_base_patches(
            mock_config=mock_config_with_websocket,
            mock_ws_server=mock_ws_server,
        )
        patches.append(
            patch(
                "gobby.agents.registry.get_running_agent_registry",
                return_value=mock_registry,
            )
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            GobbyRunner()  # Constructor sets up agent event broadcasting

            # Verify callback was captured
            assert captured_callback is not None

            # Invoke the callback with test data
            captured_callback(
                "agent_started",
                "run-123",
                {
                    "parent_session_id": "sess-456",
                    "session_id": "sess-789",
                    "mode": "terminal",
                    "provider": "claude",
                    "pid": 12345,
                },
            )

            # Allow the async task to run
            await asyncio.sleep(0.01)

            # Verify broadcast was called
            mock_ws_server.broadcast_agent_event.assert_called_once()

    @pytest.mark.asyncio
    async def test_broadcast_callback_handles_exception(self, mock_config_with_websocket):
        """Test that the broadcast callback handles exceptions gracefully."""
        import asyncio

        mock_ws_server = AsyncMock()
        mock_ws_server.start = AsyncMock()
        mock_ws_server.broadcast_agent_event = AsyncMock(side_effect=Exception("Broadcast failed"))

        mock_registry = MagicMock()
        captured_callback = None

        def capture_callback(callback):
            nonlocal captured_callback
            captured_callback = callback

        mock_registry.add_event_callback = capture_callback

        patches = create_base_patches(
            mock_config=mock_config_with_websocket,
            mock_ws_server=mock_ws_server,
        )
        patches.append(
            patch(
                "gobby.agents.registry.get_running_agent_registry",
                return_value=mock_registry,
            )
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            GobbyRunner()  # Constructor sets up agent event broadcasting

            # Verify callback was captured
            assert captured_callback is not None

            # Invoke the callback - should not raise despite exception
            captured_callback(
                "agent_started",
                "run-123",
                {"parent_session_id": "sess-456"},
            )

            # Allow the async task to run and handle exception
            await asyncio.sleep(0.1)

    @pytest.mark.asyncio
    async def test_broadcast_callback_handles_cancelled_error(self, mock_config_with_websocket):
        """Test that the broadcast callback handles CancelledError gracefully."""
        import asyncio

        mock_ws_server = AsyncMock()
        mock_ws_server.start = AsyncMock()
        mock_ws_server.broadcast_agent_event = AsyncMock(side_effect=asyncio.CancelledError())

        mock_registry = MagicMock()
        captured_callback = None

        def capture_callback(callback):
            nonlocal captured_callback
            captured_callback = callback

        mock_registry.add_event_callback = capture_callback

        patches = create_base_patches(
            mock_config=mock_config_with_websocket,
            mock_ws_server=mock_ws_server,
        )
        patches.append(
            patch(
                "gobby.agents.registry.get_running_agent_registry",
                return_value=mock_registry,
            )
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            GobbyRunner()  # Constructor sets up agent event broadcasting

            # Verify callback was captured
            assert captured_callback is not None

            # Invoke the callback - should not raise
            captured_callback(
                "agent_started",
                "run-123",
                {},
            )

            # Allow the async task to run
            await asyncio.sleep(0.1)

    @pytest.mark.asyncio
    async def test_broadcast_callback_still_works_with_captured_reference(
        self, mock_config_with_websocket
    ):
        """Test callback uses the captured websocket_server reference from setup time.

        In the refactored architecture, setup_agent_event_broadcasting() captures
        the websocket_server reference at call time. Setting runner.websocket_server
        to None afterwards does NOT affect the captured closure.
        """
        import asyncio

        mock_ws_server = AsyncMock()
        mock_ws_server.start = AsyncMock()
        mock_ws_server.broadcast_agent_event = AsyncMock()

        mock_registry = MagicMock()
        captured_callback = None

        def capture_callback(callback):
            nonlocal captured_callback
            captured_callback = callback

        mock_registry.add_event_callback = capture_callback

        patches = create_base_patches(
            mock_config=mock_config_with_websocket,
            mock_ws_server=mock_ws_server,
        )
        patches.append(
            patch(
                "gobby.agents.registry.get_running_agent_registry",
                return_value=mock_registry,
            )
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            # Verify callback was captured
            assert captured_callback is not None

            # Even after setting runner.websocket_server to None, the callback
            # still holds the original reference (captured at setup time)
            runner.websocket_server = None

            # Invoke the callback
            captured_callback(
                "agent_started",
                "run-123",
                {"parent_session_id": "sess-456"},
            )

            # Allow async operations to complete
            await asyncio.sleep(0.01)

            # Broadcast IS called because the closure captured the original reference
            mock_ws_server.broadcast_agent_event.assert_called_once()


class TestMessageProcessorWebSocketIntegration:
    """Tests for message processor and WebSocket server integration."""

    def test_message_processor_gets_websocket_server(self, mock_config_with_websocket) -> None:
        """Test that message processor receives the WebSocket server reference."""
        mock_config_with_websocket.message_tracking = MagicMock()
        mock_config_with_websocket.message_tracking.enabled = True
        mock_config_with_websocket.message_tracking.poll_interval = 5.0

        mock_ws_server = AsyncMock()
        mock_ws_server.start = AsyncMock()

        mock_message_processor = MagicMock()

        patches = create_base_patches(
            mock_config=mock_config_with_websocket,
            mock_ws_server=mock_ws_server,
        )
        patches = [p for p in patches if "SessionMessageProcessor" not in str(p)]
        patches.append(
            patch("gobby.runner.SessionMessageProcessor", return_value=mock_message_processor)
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            # Verify parser manager initialization
            # assert runner.parser_manager is not None

            # Verify websocket server was passed to message processor
            assert runner.message_processor is not None
            assert runner.message_processor.websocket_server == mock_ws_server


class TestWebSocketServerShutdown:
    """Tests for WebSocket server shutdown handling."""

    @pytest.mark.asyncio
    async def test_run_with_websocket_shutdown(self, mock_config_with_websocket):
        """Test run properly shuts down WebSocket server."""
        import asyncio

        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock()
        mock_mcp_manager.disconnect_all = AsyncMock()

        mock_ws_server = AsyncMock()

        async def ws_start():
            # Simulate a running websocket server
            await asyncio.sleep(100)

        mock_ws_server.start = ws_start

        patches = create_base_patches(
            mock_config=mock_config_with_websocket,
            mock_mcp_manager=mock_mcp_manager,
            mock_ws_server=mock_ws_server,
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            runner._shutdown_requested = True

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    await asyncio.wait_for(runner.run(), timeout=10.0)

    @pytest.mark.asyncio
    async def test_run_websocket_shutdown_timeout(self, mock_config_with_websocket):
        """Test run handles WebSocket server shutdown timeout."""
        import asyncio

        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock()
        mock_mcp_manager.disconnect_all = AsyncMock()

        mock_ws_server = AsyncMock()

        async def ws_start_hang():
            try:
                await asyncio.sleep(1000)
            except asyncio.CancelledError:
                # Hang on cancellation to trigger timeout
                await asyncio.sleep(1000)

        mock_ws_server.start = ws_start_hang

        patches = create_base_patches(
            mock_config=mock_config_with_websocket,
            mock_mcp_manager=mock_mcp_manager,
            mock_ws_server=mock_ws_server,
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            runner._shutdown_requested = True

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    # Should complete without hanging due to timeout handling
                    await asyncio.wait_for(runner.run(), timeout=15.0)


class TestShutdownLoop:
    """Tests for the shutdown waiting loop."""

    @pytest.mark.asyncio
    async def test_run_waits_for_shutdown_signal(self, mock_config):
        """Test that run waits for shutdown signal in the main loop."""
        import asyncio

        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock()
        mock_mcp_manager.disconnect_all = AsyncMock()

        patches = create_base_patches(
            mock_config=mock_config,
            mock_mcp_manager=mock_mcp_manager,
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    # Create a task that will set shutdown flag after a short delay
                    async def trigger_shutdown():
                        await asyncio.sleep(0.1)
                        runner._shutdown_requested = True

                    shutdown_task = asyncio.create_task(trigger_shutdown())

                    # Run should wait until shutdown is triggered
                    await asyncio.wait_for(runner.run(), timeout=5.0)

                    await shutdown_task


class TestMetricsCleanupTaskShutdown:
    """Tests for metrics cleanup task shutdown behavior."""

    @pytest.mark.asyncio
    async def test_run_handles_metrics_cleanup_task_cancelled_error(self, mock_config):
        """Test run handles CancelledError from metrics cleanup task cancellation."""
        import asyncio

        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock()
        mock_mcp_manager.disconnect_all = AsyncMock()

        patches = create_base_patches(
            mock_config=mock_config,
            mock_mcp_manager=mock_mcp_manager,
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    # Create a delayed shutdown that gives time for metrics task to start
                    async def delayed_shutdown():
                        await asyncio.sleep(0.1)
                        runner._shutdown_requested = True

                    shutdown_task = asyncio.create_task(delayed_shutdown())

                    await asyncio.wait_for(runner.run(), timeout=10.0)
                    await shutdown_task


class TestMetricsCleanupLoopDetailed:
    """Detailed tests for the metrics cleanup loop."""

    @pytest.mark.asyncio
    async def test_metrics_cleanup_loop_performs_cleanup_after_sleep(self, mock_config):
        """Test that metrics cleanup loop performs cleanup after sleep interval."""
        import asyncio

        from gobby.runner_maintenance import metrics_cleanup_loop

        patches = create_base_patches(mock_config=mock_config)

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            cleanup_call_count = 0

            def mock_cleanup(retention_days: int = 30):
                nonlocal cleanup_call_count
                cleanup_call_count += 1
                return 5 if cleanup_call_count == 1 else 0

            runner.metrics_manager.cleanup_old_metrics = mock_cleanup

            shutdown_requested = False

            def is_shutdown():
                return shutdown_requested

            # Patch asyncio.sleep to complete immediately
            original_sleep = asyncio.sleep

            async def fast_sleep(seconds):
                nonlocal shutdown_requested
                if seconds > 1:  # Only intercept the 24-hour sleep
                    shutdown_requested = True  # Trigger shutdown after one iteration
                    return
                await original_sleep(seconds)

            with patch("asyncio.sleep", side_effect=fast_sleep):
                task = asyncio.create_task(
                    metrics_cleanup_loop(runner.metrics_manager, is_shutdown)
                )
                await asyncio.wait_for(task, timeout=2.0)

            # Cleanup should have been called once
            assert cleanup_call_count == 1

    @pytest.mark.asyncio
    async def test_metrics_cleanup_loop_logs_deleted_entries(self, mock_config):
        """Test that metrics cleanup loop logs when entries are deleted."""
        import asyncio

        from gobby.runner_maintenance import metrics_cleanup_loop

        patches = create_base_patches(mock_config=mock_config)

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            runner.metrics_manager.cleanup_old_metrics = MagicMock(return_value=10)

            shutdown_requested = False

            def is_shutdown():
                return shutdown_requested

            original_sleep = asyncio.sleep

            async def fast_sleep(seconds):
                nonlocal shutdown_requested
                if seconds > 1:
                    shutdown_requested = True
                    return
                await original_sleep(seconds)

            with patch("asyncio.sleep", side_effect=fast_sleep):
                task = asyncio.create_task(
                    metrics_cleanup_loop(runner.metrics_manager, is_shutdown)
                )
                await asyncio.wait_for(task, timeout=2.0)

    @pytest.mark.asyncio
    async def test_metrics_cleanup_loop_continues_on_error(self, mock_config):
        """Test that metrics cleanup loop continues after an error."""
        import asyncio

        from gobby.runner_maintenance import metrics_cleanup_loop

        patches = create_base_patches(mock_config=mock_config)

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            call_count = 0

            def mock_cleanup(retention_days: int = 30):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise Exception("First call error")
                return 0

            runner.metrics_manager.cleanup_old_metrics = mock_cleanup

            shutdown_requested = False

            def is_shutdown():
                return shutdown_requested

            original_sleep = asyncio.sleep
            iteration = 0

            async def fast_sleep(seconds):
                nonlocal iteration, shutdown_requested
                if seconds > 1:
                    iteration += 1
                    if iteration >= 2:  # Allow 2 iterations then stop
                        shutdown_requested = True
                    return
                await original_sleep(seconds)

            with patch("asyncio.sleep", side_effect=fast_sleep):
                task = asyncio.create_task(
                    metrics_cleanup_loop(runner.metrics_manager, is_shutdown)
                )
                await asyncio.wait_for(task, timeout=2.0)

            # Cleanup should have been called twice (once erroring, once successful)
            assert call_count == 2
