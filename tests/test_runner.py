"""Tests for src/runner.py - Gobby Daemon Runner."""

import asyncio
import signal
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.runner import GobbyRunner, main, run_gobby
from gobby.runner_init import resolve_embedding_api_key

pytestmark = pytest.mark.unit

_RUNNER_INIT_SESSION_MANAGER_PATCH = "gobby.runner_init.SessionManager"


@pytest.fixture(autouse=True)
def fast_stop_hook_grace_window():
    """Avoid real 5s shutdown delays in runner tests."""
    with patch(
        "gobby.runner_lifecycle._await_critical_stop_hook_grace_window",
        new=AsyncMock(),
    ) as mock_wait:
        yield mock_wait


def _set_mock_default(obj: MagicMock, name: str, default):
    """Assign a default only when a MagicMock placeholder has not been made concrete."""
    value = getattr(obj, name, None)
    if isinstance(value, AsyncMock):
        return
    if isinstance(value, MagicMock):
        setattr(obj, name, default)
    elif value is None:
        setattr(obj, name, default)


def _apply_safe_runner_config_defaults(config: MagicMock) -> MagicMock:
    """Populate runner tests with scalar defaults so background tasks stay deterministic."""
    config.bind_host = "localhost"

    if getattr(config, "websocket", None) is None:
        config.websocket = None

    config.session_lifecycle = getattr(config, "session_lifecycle", MagicMock())
    config.message_tracking = getattr(config, "message_tracking", None)
    config.memory_sync = getattr(config, "memory_sync", MagicMock())
    _set_mock_default(config.memory_sync, "enabled", False)

    config.ui = getattr(config, "ui", MagicMock())
    _set_mock_default(config.ui, "enabled", False)
    _set_mock_default(config.ui, "mode", "prod")
    _set_mock_default(config.ui, "host", "localhost")
    _set_mock_default(config.ui, "port", 5173)

    config.embeddings = getattr(config, "embeddings", MagicMock())
    _set_mock_default(config.embeddings, "api_base", "")
    _set_mock_default(config.embeddings, "model", "text-embedding-3-small")
    _set_mock_default(config.embeddings, "api_key", None)
    _set_mock_default(config.embeddings, "dim", 1536)

    config.databases = getattr(config, "databases", MagicMock())
    config.databases.qdrant = getattr(config.databases, "qdrant", MagicMock())
    _set_mock_default(config.databases.qdrant, "url", "")
    _set_mock_default(config.databases.qdrant, "collection_prefix", "test_")
    config.databases.neo4j = getattr(config.databases, "neo4j", MagicMock())
    _set_mock_default(config.databases.neo4j, "url", "")
    _set_mock_default(config.databases.neo4j, "auth", None)
    _set_mock_default(config.databases.neo4j, "database", "neo4j")
    _set_mock_default(config.databases.neo4j, "graph_search", True)
    _set_mock_default(config.databases.neo4j, "graph_min_score", 0.5)
    _set_mock_default(config.databases.neo4j, "rrf_k", 60)

    config.code_index = getattr(config, "code_index", MagicMock())
    _set_mock_default(config.code_index, "enabled", False)
    _set_mock_default(config.code_index, "embedding_enabled", False)
    _set_mock_default(config.code_index, "graph_enabled", False)
    _set_mock_default(config.code_index, "summary_enabled", False)
    _set_mock_default(config.code_index, "summary_batch_size", 20)
    _set_mock_default(config.code_index, "maintenance_interval_seconds", 300)
    _set_mock_default(config.code_index, "sync_worker_interval_seconds", 5)
    _set_mock_default(config.code_index, "sync_worker_batch_size", 50)

    return config


@pytest.fixture
def mock_config():
    """Create a mock config with WebSocket disabled by default."""
    config = MagicMock()
    config.daemon_port = 60887
    config.websocket = None
    return _apply_safe_runner_config_defaults(config)


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
    return _apply_safe_runner_config_defaults(config)


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
    if mock_config is not None:
        _apply_safe_runner_config_defaults(mock_config)

    # Create default mocks if not provided
    if mock_mcp_manager is None:
        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock()
        mock_mcp_manager.disconnect_all = AsyncMock()

    if mock_http is None:
        mock_http = MagicMock()
        mock_http.app = MagicMock()
        mock_http.port = 60887
    _set_mock_default(mock_http, "_terminate_streamable_http_sessions", AsyncMock())

    mock_agent_monitor = AsyncMock()
    mock_agent_monitor.recover_or_cleanup_agents.return_value = (0, 0)

    patches = [
        patch("gobby.runner_init.init_telemetry"),
        patch("gobby.runner_init.get_machine_id", return_value="test-machine"),
        patch("gobby.runner_init.LocalDatabase"),
        patch("gobby.runner_init.run_migrations"),
        patch(_RUNNER_INIT_SESSION_MANAGER_PATCH),
        patch("gobby.runner_init.LocalTaskManager"),
        patch("gobby.runner_init.SessionTaskManager"),
        patch("gobby.runner_init.MCPClientManager", return_value=mock_mcp_manager),
        patch("gobby.runner_init.TaskSyncManager"),
        patch("gobby.runner_init.MemorySyncManager"),
        patch("gobby.runner_init.SessionMessageProcessor", return_value=AsyncMock()),
        patch("gobby.runner_init.TaskValidator"),
        patch("gobby.runner_init.SessionLifecycleManager", return_value=AsyncMock()),
        patch("gobby.runner_init.create_llm_service", return_value=None),
        patch("gobby.runner_init.MemoryManager", return_value=None),
        patch("gobby.runner_init.HTTPServer", return_value=mock_http),
        patch("gobby.storage.secrets.SecretStore"),
        patch("gobby.storage.config_store.ConfigStore"),
        patch("gobby.runner_init.AgentLifecycleMonitor", return_value=mock_agent_monitor),
    ]

    # Add config patch
    if mock_config is not None:
        patches.insert(1, patch("gobby.runner_init.load_config", return_value=mock_config))
    else:
        patches.insert(1, patch("gobby.runner_init.load_config"))

    # Add WebSocketServer patch
    if mock_ws_server is not None:
        patches.append(patch("gobby.runner_init.WebSocketServer", return_value=mock_ws_server))
    else:
        patches.append(patch("gobby.runner_init.WebSocketServer"))

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


class TestSetMockDefault:
    def test_preserves_asyncmock_overrides(self) -> None:
        obj = MagicMock()
        existing = AsyncMock()
        obj.child = existing

        _set_mock_default(obj, "child", False)

        assert obj.child is existing

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

            async def _delayed_shutdown() -> None:
                await asyncio.sleep(0)
                runner._shutdown_requested = True

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    asyncio.create_task(_delayed_shutdown())
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
        """Test run with WebSocket server enabled.

        Subsystem init (including WebSocket start) runs as a background task.
        We delay shutdown briefly to let it execute, and skip health checks
        so it completes quickly.
        """
        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock()
        mock_mcp_manager.disconnect_all = AsyncMock()

        mock_ws_server = AsyncMock()
        mock_ws_server.start = AsyncMock()

        # Skip health checks so _init_subsystems completes quickly
        mock_config_with_websocket.databases.qdrant.url = ""
        mock_config_with_websocket.databases.neo4j.url = ""
        mock_config_with_websocket.embeddings.api_base = ""
        mock_config_with_websocket.ui.enabled = False

        patches = create_base_patches(
            mock_config=mock_config_with_websocket,
            mock_mcp_manager=mock_mcp_manager,
            mock_ws_server=mock_ws_server,
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            # Schedule shutdown after a brief delay to let subsystem init run
            async def _delayed_shutdown() -> None:
                await asyncio.sleep(0.3)
                runner._shutdown_requested = True

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    asyncio.create_task(_delayed_shutdown())
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

    def _mock_bootstrap(self):
        """Return a patch for load_bootstrap that returns a minimal stub."""
        from unittest.mock import MagicMock

        stub = MagicMock(daemon_port=8765, bind_host="localhost")
        return patch("gobby.config.bootstrap.load_bootstrap", return_value=stub)

    def test_main_runs_asyncio(self) -> None:
        """Test that main runs the async runner."""
        with self._mock_bootstrap():
            with patch("gobby.runner._healthy_daemon_running", return_value=False):
                with patch("asyncio.run") as mock_run:
                    with patch("gobby.runner.run_gobby") as mock_run_gobby:
                        mock_run_gobby.return_value = None
                        main(config_path=Path("/tmp/config.yaml"), verbose=True)

                    mock_run.assert_called_once()

    def test_main_handles_keyboard_interrupt(self) -> None:
        """Test that main handles KeyboardInterrupt gracefully."""
        with self._mock_bootstrap():
            with patch("gobby.runner._healthy_daemon_running", return_value=False):
                with patch("asyncio.run", side_effect=KeyboardInterrupt()):
                    with patch("gobby.runner.run_gobby") as mock_run_gobby:
                        mock_run_gobby.return_value = None
                        with pytest.raises(SystemExit) as exc_info:
                            main()

                    assert exc_info.value.code == 0

    def test_main_handles_exception(self) -> None:
        """Test that main handles exceptions and exits with code 1."""
        with self._mock_bootstrap():
            with patch("gobby.runner._healthy_daemon_running", return_value=False):
                with patch("asyncio.run", side_effect=Exception("Test error")):
                    with patch("gobby.runner.run_gobby") as mock_run_gobby:
                        mock_run_gobby.return_value = None
                        with pytest.raises(SystemExit) as exc_info:
                            main()

                assert exc_info.value.code == 1

    def test_main_exits_cleanly_when_daemon_already_running(self) -> None:
        """Test that main exits with code 0 when a healthy daemon is already running."""
        with self._mock_bootstrap():
            with patch("gobby.runner._healthy_daemon_running", return_value=True):
                with pytest.raises(SystemExit) as exc_info:
                    main()

                assert exc_info.value.code == 0


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
        patches.append(patch("gobby.runner_init.MemoryManager", return_value=mock_memory_manager))

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
            patch("gobby.runner_init.MemoryManager", side_effect=Exception("Memory init error"))
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
        patches.append(patch("gobby.runner_init.MemoryManager", return_value=mock_memory_manager))
        patches.append(
            patch("gobby.runner_init.MemorySyncManager", return_value=mock_memory_sync_manager)
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            assert runner.memory_sync_manager == mock_memory_sync_manager
            mock_memory_sync_manager.import_sync.assert_called_once()

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
        patches.append(patch("gobby.runner_init.MemoryManager", return_value=mock_memory_manager))
        patches.append(
            patch(
                "gobby.runner_init.MemorySyncManager", side_effect=Exception("Sync manager error")
            )
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
            patch("gobby.runner_init.SessionMessageProcessor", return_value=mock_message_processor)
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
        patches.append(patch("gobby.runner_init.create_llm_service", return_value=mock_llm_service))
        patches.append(patch("gobby.runner_init.TaskValidator", return_value=mock_task_validator))

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
        patches.append(patch("gobby.runner_init.create_llm_service", return_value=mock_llm_service))
        patches.append(
            patch("gobby.runner_init.TaskValidator", side_effect=Exception("Validator error"))
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
            patch("gobby.runner_init.AgentRunner", side_effect=Exception("Agent runner error"))
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
            patch("gobby.runner_init.create_llm_service", side_effect=Exception("LLM init error"))
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
        import gobby.runner_broadcasting as rb

        mock_ws_server = AsyncMock()
        mock_ws_server.start = AsyncMock()
        mock_ws_server.broadcast_agent_event = AsyncMock()

        patches = create_base_patches(
            mock_config=mock_config_with_websocket,
            mock_ws_server=mock_ws_server,
        )

        old_callback = rb._agent_event_callback
        try:
            with ExitStack() as stack:
                [stack.enter_context(p) for p in patches]

                GobbyRunner()  # Constructor triggers event broadcasting setup

                # Verify module-level callback was set
                assert rb._agent_event_callback is not None
        finally:
            rb._agent_event_callback = old_callback

    def test_setup_agent_event_broadcasting_without_websocket(self, mock_config) -> None:
        """Test agent event broadcasting is skipped without WebSocket.

        When config.websocket is None, the broadcasting setup block is
        skipped entirely, so _agent_event_callback remains unchanged.
        """
        import gobby.runner_broadcasting as rb

        patches = create_base_patches(mock_config=mock_config)

        old_callback = rb._agent_event_callback
        try:
            rb._agent_event_callback = None  # ensure clean baseline
            with ExitStack() as stack:
                [stack.enter_context(p) for p in patches]

                GobbyRunner()  # Constructor runs without WebSocket

                # Callback should NOT be set since broadcasting setup was skipped
                assert rb._agent_event_callback is None
        finally:
            rb._agent_event_callback = old_callback

    def test_setup_agent_event_broadcasting_direct_call_sets_callback(self) -> None:
        """Test setup_agent_event_broadcasting sets the module-level callback."""
        import gobby.runner_broadcasting as rb
        from gobby.runner_broadcasting import setup_agent_event_broadcasting

        mock_ws_server = MagicMock()

        old_callback = rb._agent_event_callback
        try:
            with (
                patch("gobby.agents.pty_reader.get_pty_reader_manager"),
                patch("gobby.agents.tmux.get_tmux_output_reader"),
            ):
                setup_agent_event_broadcasting(mock_ws_server)

            # Module-level callback should have been set
            assert rb._agent_event_callback is not None
        finally:
            rb._agent_event_callback = old_callback


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
    async def test_run_waits_for_http_shutdown_before_reaping_children(self, mock_config):
        """Child reaping should only happen after HTTP shutdown completes."""
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

            http_shutdown_complete = False

            async def serve() -> None:
                nonlocal http_shutdown_complete
                await asyncio.sleep(0)
                http_shutdown_complete = True

            mock_process = MagicMock()

            def children(*, recursive: bool) -> list[MagicMock]:
                assert recursive is True
                assert http_shutdown_complete is True
                return []

            mock_process.children.side_effect = children

            with (
                patch("uvicorn.Config"),
                patch("uvicorn.Server") as mock_server_cls,
                patch("psutil.Process", return_value=mock_process),
            ):
                mock_server = MagicMock()
                mock_server.serve = AsyncMock(side_effect=serve)
                mock_server.should_exit = False
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    await runner.run()

            mock_process.children.assert_called_once_with(recursive=True)

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
            patch("gobby.runner_init.SessionLifecycleManager", return_value=mock_lifecycle_manager)
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
    async def test_run_stops_lifecycle_manager_before_http_shutdown_completes(self, mock_config):
        """Lifecycle cleanup should stop before we wait for HTTP lifespan shutdown."""
        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock()
        mock_mcp_manager.disconnect_all = AsyncMock()

        lifecycle_stopped = False
        mock_lifecycle_manager = AsyncMock()
        mock_lifecycle_manager.start = AsyncMock()

        async def stop_lifecycle() -> None:
            nonlocal lifecycle_stopped
            lifecycle_stopped = True

        mock_lifecycle_manager.stop = AsyncMock(side_effect=stop_lifecycle)

        patches = create_base_patches(
            mock_config=mock_config,
            mock_mcp_manager=mock_mcp_manager,
        )
        patches = [p for p in patches if "SessionLifecycleManager" not in str(p)]
        patches.append(
            patch("gobby.runner_init.SessionLifecycleManager", return_value=mock_lifecycle_manager)
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            async def trigger_shutdown() -> None:
                await asyncio.sleep(0)
                runner._shutdown_requested = True

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = MagicMock()
                mock_server.should_exit = False

                async def serve() -> None:
                    while not mock_server.should_exit:
                        await asyncio.sleep(0)
                    await asyncio.sleep(0)
                    assert lifecycle_stopped is True

                mock_server.serve = AsyncMock(side_effect=serve)
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    asyncio.create_task(trigger_shutdown())
                    await asyncio.wait_for(runner.run(), timeout=10.0)

    @pytest.mark.asyncio
    async def test_run_waits_for_stop_hook_grace_before_http_shutdown(
        self, mock_config, fast_stop_hook_grace_window
    ):
        """Shutdown should keep HTTP up for the Stop-hook grace window before exit."""
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
            events: list[str] = []

            async def trigger_shutdown() -> None:
                await asyncio.sleep(0)
                runner._shutdown_requested = True

            async def note_grace_wait() -> None:
                events.append("grace")
                assert mock_server.should_exit is False

            async def terminate_sessions() -> None:
                events.append("terminate")
                assert mock_server.should_exit is True

            fast_stop_hook_grace_window.side_effect = note_grace_wait
            runner.http_server._terminate_streamable_http_sessions.side_effect = (
                terminate_sessions
            )

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = MagicMock()
                mock_server.should_exit = False

                async def serve() -> None:
                    while not mock_server.should_exit:
                        await asyncio.sleep(0)
                    events.append("serve-exit")

                mock_server.serve = AsyncMock(side_effect=serve)
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    asyncio.create_task(trigger_shutdown())
                    await asyncio.wait_for(runner.run(), timeout=10.0)

            fast_stop_hook_grace_window.assert_awaited_once()
            runner.http_server._terminate_streamable_http_sessions.assert_awaited_once()
            assert events[:2] == ["grace", "terminate"]

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
            patch("gobby.runner_init.SessionMessageProcessor", return_value=mock_message_processor)
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
        # Skip health checks so _init_subsystems completes quickly
        mock_config.databases.qdrant.url = ""
        mock_config.databases.neo4j.url = ""
        mock_config.embeddings.api_base = ""
        mock_config.ui.enabled = False

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
            patch("gobby.runner_init.SessionMessageProcessor", return_value=mock_message_processor)
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            async def _delayed_shutdown() -> None:
                await asyncio.sleep(0.3)
                runner._shutdown_requested = True

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    asyncio.create_task(_delayed_shutdown())
                    await runner.run()

            mock_message_processor.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_runs_startup_metrics_cleanup(self, mock_config):
        """Test that run performs startup metrics cleanup."""
        # Skip health checks so _init_subsystems completes quickly
        mock_config.databases.qdrant.url = ""
        mock_config.databases.neo4j.url = ""
        mock_config.embeddings.api_base = ""
        mock_config.ui.enabled = False

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

            async def _delayed_shutdown() -> None:
                await asyncio.sleep(0.3)
                runner._shutdown_requested = True

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    asyncio.create_task(_delayed_shutdown())
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
    """Tests for the broadcast_agent_event callback via fire_agent_event."""

    @pytest.mark.asyncio
    async def test_broadcast_callback_invoked(self):
        """Test that fire_agent_event invokes the broadcast callback."""
        import asyncio

        import gobby.runner_broadcasting as rb
        from gobby.runner_broadcasting import fire_agent_event, setup_agent_event_broadcasting

        mock_ws_server = AsyncMock()
        mock_ws_server.broadcast_agent_event = AsyncMock()

        old_callback = rb._agent_event_callback
        try:
            with (
                patch("gobby.agents.pty_reader.get_pty_reader_manager"),
                patch("gobby.agents.tmux.get_tmux_output_reader"),
            ):
                setup_agent_event_broadcasting(mock_ws_server)

            fire_agent_event(
                "agent_started",
                "run-123",
                {
                    "parent_session_id": "sess-456",
                    "session_id": "sess-789",
                    "mode": "interactive",
                    "provider": "claude",
                    "pid": 12345,
                },
            )

            # Allow the async task to run
            await asyncio.sleep(0.01)

            mock_ws_server.broadcast_agent_event.assert_called_once()
        finally:
            rb._agent_event_callback = old_callback

    @pytest.mark.asyncio
    async def test_broadcast_callback_handles_exception(self):
        """Test that the broadcast callback handles exceptions gracefully."""
        import asyncio

        import gobby.runner_broadcasting as rb
        from gobby.runner_broadcasting import fire_agent_event, setup_agent_event_broadcasting

        mock_ws_server = AsyncMock()
        mock_ws_server.broadcast_agent_event = AsyncMock(side_effect=Exception("Broadcast failed"))

        old_callback = rb._agent_event_callback
        try:
            with (
                patch("gobby.agents.pty_reader.get_pty_reader_manager"),
                patch("gobby.agents.tmux.get_tmux_output_reader"),
            ):
                setup_agent_event_broadcasting(mock_ws_server)

            # Should not raise despite exception in broadcast
            fire_agent_event(
                "agent_started",
                "run-123",
                {"parent_session_id": "sess-456"},
            )

            await asyncio.sleep(0.1)
        finally:
            rb._agent_event_callback = old_callback

    @pytest.mark.asyncio
    async def test_broadcast_callback_handles_cancelled_error(self):
        """Test that the broadcast callback handles CancelledError gracefully."""
        import asyncio

        import gobby.runner_broadcasting as rb
        from gobby.runner_broadcasting import fire_agent_event, setup_agent_event_broadcasting

        mock_ws_server = AsyncMock()
        mock_ws_server.broadcast_agent_event = AsyncMock(side_effect=asyncio.CancelledError())

        old_callback = rb._agent_event_callback
        try:
            with (
                patch("gobby.agents.pty_reader.get_pty_reader_manager"),
                patch("gobby.agents.tmux.get_tmux_output_reader"),
            ):
                setup_agent_event_broadcasting(mock_ws_server)

            # Should not raise
            fire_agent_event(
                "agent_started",
                "run-123",
                {},
            )

            await asyncio.sleep(0.1)
        finally:
            rb._agent_event_callback = old_callback

    @pytest.mark.asyncio
    async def test_broadcast_callback_still_works_with_captured_reference(self):
        """Test callback uses the captured websocket_server reference from setup time.

        setup_agent_event_broadcasting() captures the websocket_server reference
        at call time via closure. The module-level _agent_event_callback holds
        a reference to the closure, not to the caller's variable.
        """
        import asyncio

        import gobby.runner_broadcasting as rb
        from gobby.runner_broadcasting import fire_agent_event, setup_agent_event_broadcasting

        mock_ws_server = AsyncMock()
        mock_ws_server.broadcast_agent_event = AsyncMock()

        old_callback = rb._agent_event_callback
        try:
            with (
                patch("gobby.agents.pty_reader.get_pty_reader_manager"),
                patch("gobby.agents.tmux.get_tmux_output_reader"),
            ):
                setup_agent_event_broadcasting(mock_ws_server)

            # Even if external references to ws_server are dropped,
            # the callback closure still holds the original reference
            fire_agent_event(
                "agent_started",
                "run-123",
                {"parent_session_id": "sess-456"},
            )

            await asyncio.sleep(0.01)

            mock_ws_server.broadcast_agent_event.assert_called_once()
        finally:
            rb._agent_event_callback = old_callback


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
            patch("gobby.runner_init.SessionMessageProcessor", return_value=mock_message_processor)
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


class TestResolveEmbeddingApiKey:
    """Tests for resolve_embedding_api_key (runner_init.py)."""

    @pytest.fixture
    def mock_secret_store(self):
        """Create a mock secret store."""
        store = MagicMock()
        store.get = MagicMock(
            side_effect=lambda name: {
                "openai_api_key": "sk-test-openai",
                "gemini_api_key": "gemini-test-key",
                "mistral_api_key": "mistral-test-key",
            }.get(name)
        )
        return store

    def test_default_model_resolves_openai(self, mock_secret_store) -> None:
        """text-embedding-3-small (no prefix) should resolve to openai_api_key."""
        key = resolve_embedding_api_key(mock_secret_store, "text-embedding-3-small")
        assert key == "sk-test-openai"
        mock_secret_store.get.assert_called_with("openai_api_key")

    def test_openai_prefix_resolves_openai(self, mock_secret_store) -> None:
        """openai/text-embedding-3-small should resolve to openai_api_key."""
        key = resolve_embedding_api_key(mock_secret_store, "openai/text-embedding-3-small")
        assert key == "sk-test-openai"
        mock_secret_store.get.assert_called_with("openai_api_key")

    def test_gemini_prefix_resolves_gemini(self, mock_secret_store) -> None:
        """gemini/ prefix should resolve to gemini_api_key."""
        key = resolve_embedding_api_key(mock_secret_store, "gemini/text-embedding-004")
        assert key == "gemini-test-key"
        mock_secret_store.get.assert_called_with("gemini_api_key")

    def test_mistral_prefix_resolves_mistral(self, mock_secret_store) -> None:
        """mistral/ prefix should resolve to mistral_api_key."""
        key = resolve_embedding_api_key(mock_secret_store, "mistral/mistral-embed")
        assert key == "mistral-test-key"
        mock_secret_store.get.assert_called_with("mistral_api_key")

    def test_local_returns_none(self, mock_secret_store) -> None:
        """local/ models don't need an API key."""
        key = resolve_embedding_api_key(mock_secret_store, "local/nomic-embed-text-v1.5")
        assert key is None
        mock_secret_store.get.assert_not_called()

    def test_ollama_returns_none(self, mock_secret_store) -> None:
        """ollama/ models don't need an API key."""
        key = resolve_embedding_api_key(mock_secret_store, "ollama/nomic-embed-text")
        assert key is None
        mock_secret_store.get.assert_not_called()

    def test_missing_secret_returns_none(self) -> None:
        """Returns None when the secret doesn't exist."""
        store = MagicMock()
        store.get = MagicMock(return_value=None)
        key = resolve_embedding_api_key(store, "text-embedding-3-small")
        assert key is None


class TestGobbyRunnerShutdownExtended:
    """Tests for GobbyRunner shutdown behavior."""

    @pytest.mark.asyncio
    async def test_run_calls_disconnect_on_shutdown(self, mock_config):
        """Test that run always disconnects MCP on shutdown."""
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

            # Disconnect should always be called (cleanup)
            mock_mcp_manager.disconnect_all.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_shuts_down_telemetry_before_closing_database(self, mock_config):
        """Telemetry flush should happen before the main daemon DB is closed."""
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
            events: list[str] = []
            runner.database.close.side_effect = lambda: events.append("database")

            with (
                patch("uvicorn.Config"),
                patch("uvicorn.Server") as mock_server_cls,
                patch(
                    "gobby.runner_lifecycle.shutdown_telemetry",
                    side_effect=lambda: events.append("telemetry"),
                ),
            ):
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    await runner.run()

            assert events == ["telemetry", "database"]


class TestGobbyRunnerInitEdgeCases:
    """Edge case tests for GobbyRunner initialization."""

    def test_init_with_no_llm_service(self, mock_config) -> None:
        """Test init when LLM service creation returns None."""
        patches = create_base_patches(mock_config=mock_config)

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            assert runner.llm_service is None

    def test_init_llm_service_exception(self, mock_config) -> None:
        """Test init when LLM service creation raises."""
        patches = create_base_patches(mock_config=mock_config)
        # Replace create_llm_service patch with one that raises
        patches = [p for p in patches if "create_llm_service" not in str(p)]
        patches.append(
            patch("gobby.runner_init.create_llm_service", side_effect=Exception("LLM init error"))
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            # Should not raise - error is logged
            runner = GobbyRunner()

            assert runner.llm_service is None

    def test_init_with_verbose_false(self, mock_config) -> None:
        """Test init with verbose=False (default)."""
        patches = create_base_patches(mock_config=mock_config)

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            assert runner.verbose is False

    def test_shutdown_requested_initially_false(self, mock_config) -> None:
        """Test that _shutdown_requested is False on init."""
        patches = create_base_patches(mock_config=mock_config)

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            assert runner._shutdown_requested is False


class TestMainFunctionExtended:
    """Tests for the main() entry point function."""

    def test_main_calls_run_gobby(self) -> None:
        """Test that main() calls asyncio.run when no daemon is running."""
        with (
            patch("gobby.runner._healthy_daemon_running", return_value=False),
            patch("asyncio.run") as mock_asyncio_run,
        ):
            main()
            mock_asyncio_run.assert_called_once()

    def test_main_exits_when_daemon_running(self) -> None:
        """Test that main() exits with code 0 when daemon already running."""
        with (
            patch("gobby.runner._healthy_daemon_running", return_value=True),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 0

    def test_main_handles_keyboard_interrupt(self) -> None:
        """Test that main() handles KeyboardInterrupt gracefully."""
        with (
            patch("gobby.runner._healthy_daemon_running", return_value=False),
            patch("asyncio.run", side_effect=KeyboardInterrupt),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 0

    def test_main_handles_fatal_error(self) -> None:
        """Test that main() exits with code 1 on fatal error."""
        with (
            patch("gobby.runner._healthy_daemon_running", return_value=False),
            patch("asyncio.run", side_effect=RuntimeError("Fatal")),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 1
