"""Runner shutdown tests."""

import asyncio
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if TYPE_CHECKING:
    import uvicorn

from gobby import app_context, runner_lifecycle_processes, runner_lifecycle_shutdown
from gobby.agents import terminal_delivery
from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEventType
from gobby.runner import GobbyRunner
from gobby.runner_pid_file import FailOpenPidOwnership
from gobby.shutdown_intent import ShutdownIntent
from tests.hooks._event_handler_helpers import make_event
from tests.runner_helpers import create_base_patches

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("fast_stop_hook_grace_window")]


@pytest.fixture(autouse=True)
def _reset_tmux_globals() -> Any:
    # Runner startup paths call configure_tmux with mocked configs; reset the
    # module-global so later suites see the unconfigured default again.
    from gobby.agents.tmux import reset_tmux_globals

    yield
    reset_tmux_globals()


async def _never_complete() -> None:
    await asyncio.Event().wait()


class _ExitAwareServer:
    def __init__(self) -> None:
        self._should_exit = False
        self.started = True
        self.exit_requested = asyncio.Event()
        self.serve = AsyncMock(side_effect=self._serve)

    @property
    def should_exit(self) -> bool:
        return self._should_exit

    @should_exit.setter
    def should_exit(self, value: bool) -> None:
        self._should_exit = value
        if value:
            self.exit_requested.set()

    async def _serve(self) -> None:
        await self.exit_requested.wait()


class TestGobbyRunnerShutdown:
    """Tests for shutdown handling in run method."""

    @pytest.mark.asyncio
    async def test_run_waits_for_http_shutdown_before_reaping_children(self, mock_config) -> None:
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
                    await runner.run(ownership_resolution=FailOpenPidOwnership("test"))

            mock_process.children.assert_called_once_with(recursive=True)
            assert http_shutdown_complete is True
            assert runner.database.close.called is True

    @pytest.mark.asyncio
    async def test_run_handles_http_server_shutdown_timeout(self, mock_config) -> None:
        """Test that run handles HTTP server shutdown timeout."""
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
                mock_server.serve = AsyncMock(side_effect=TimeoutError)
                mock_server.should_exit = False
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    await asyncio.wait_for(
                        runner.run(ownership_resolution=FailOpenPidOwnership("test")), timeout=25.0
                    )

            assert mock_server.should_exit is True
            assert runner.database.close.called is True

    @pytest.mark.asyncio
    async def test_run_handles_lifecycle_manager_shutdown_timeout(self, mock_config) -> None:
        """Test that run handles lifecycle manager shutdown timeout."""
        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock()
        mock_mcp_manager.disconnect_all = AsyncMock()

        mock_lifecycle_manager = AsyncMock()
        mock_lifecycle_manager.start = AsyncMock()
        mock_lifecycle_manager.stop = AsyncMock(side_effect=TimeoutError)

        patches = create_base_patches(
            mock_config=mock_config,
            mock_mcp_manager=mock_mcp_manager,
        )
        patches = [p for p in patches if "SessionLifecycleManager" not in str(p)]
        patches.append(
            patch(
                "gobby.runner_init.orchestration.SessionLifecycleManager",
                return_value=mock_lifecycle_manager,
            )
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
                    await asyncio.wait_for(
                        runner.run(ownership_resolution=FailOpenPidOwnership("test")), timeout=10.0
                    )

            assert mock_lifecycle_manager.stop.await_count == 1
            assert runner.database.close.called is True

    @pytest.mark.asyncio
    async def test_run_stops_lifecycle_manager_before_http_shutdown_completes(
        self,
        mock_config,
    ) -> None:
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
            patch(
                "gobby.runner_init.orchestration.SessionLifecycleManager",
                return_value=mock_lifecycle_manager,
            )
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            runner._shutdown_requested = True

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = _ExitAwareServer()
                lifecycle_stopped_event = asyncio.Event()

                async def serve() -> None:
                    await mock_server.exit_requested.wait()
                    await lifecycle_stopped_event.wait()
                    assert lifecycle_stopped is True

                async def stop_lifecycle_and_signal() -> None:
                    nonlocal lifecycle_stopped
                    lifecycle_stopped = True
                    lifecycle_stopped_event.set()

                mock_lifecycle_manager.stop.side_effect = stop_lifecycle_and_signal
                mock_server.serve = AsyncMock(side_effect=serve)
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    await asyncio.wait_for(
                        runner.run(ownership_resolution=FailOpenPidOwnership("test")), timeout=10.0
                    )

            assert lifecycle_stopped is True
            assert mock_server.serve.await_count == 1

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
            runner._shutdown_requested = True

            async def note_grace_wait() -> None:
                events.append("grace")
                assert mock_server.should_exit is False

            async def cleanup_pending() -> None:
                events.append("pending")
                assert mock_server.should_exit is False

            async def terminate_sessions() -> None:
                events.append("terminate")
                assert mock_server.should_exit is False

            fast_stop_hook_grace_window.side_effect = note_grace_wait
            runner.http_server._cleanup_pending_interactions = AsyncMock(
                side_effect=cleanup_pending
            )
            runner.http_server._terminate_streamable_http_sessions.side_effect = terminate_sessions

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = _ExitAwareServer()

                async def serve() -> None:
                    await mock_server.exit_requested.wait()
                    events.append("serve-exit")

                mock_server.serve = AsyncMock(side_effect=serve)
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    await asyncio.wait_for(
                        runner.run(ownership_resolution=FailOpenPidOwnership("test")), timeout=10.0
                    )

            fast_stop_hook_grace_window.assert_awaited_once()
            runner.http_server._cleanup_pending_interactions.assert_awaited_once()
            runner.http_server._terminate_streamable_http_sessions.assert_awaited_once()
            assert events[:3] == ["grace", "pending", "terminate"]
            assert events[-1] == "serve-exit"

    @pytest.mark.asyncio
    async def test_run_handles_message_processor_shutdown_timeout(self, mock_config) -> None:
        """Test that run handles message processor shutdown timeout."""
        mock_config.message_tracking = MagicMock()
        mock_config.message_tracking.enabled = True
        mock_config.message_tracking.poll_interval = 5.0

        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock()
        mock_mcp_manager.disconnect_all = AsyncMock()

        mock_message_processor = AsyncMock()
        mock_message_processor.start = AsyncMock()
        mock_message_processor.stop = AsyncMock(side_effect=TimeoutError)

        patches = create_base_patches(
            mock_config=mock_config,
            mock_mcp_manager=mock_mcp_manager,
        )
        patches = [p for p in patches if "SessionMessageProcessor" not in str(p)]
        patches.append(
            patch(
                "gobby.runner_init.services.SessionMessageProcessor",
                return_value=mock_message_processor,
            )
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
                    await asyncio.wait_for(
                        runner.run(ownership_resolution=FailOpenPidOwnership("test")), timeout=10.0
                    )

            assert mock_message_processor.stop.await_count == 1
            assert runner.database.close.called is True

    @pytest.mark.asyncio
    async def test_run_handles_mcp_disconnect_timeout(self, mock_config) -> None:
        """Test that run handles MCP disconnect timeout."""
        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock()
        mock_mcp_manager.disconnect_all = AsyncMock(side_effect=TimeoutError)

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
                    await asyncio.wait_for(
                        runner.run(ownership_resolution=FailOpenPidOwnership("test")), timeout=10.0
                    )

            assert mock_mcp_manager.disconnect_all.await_count == 1
            assert runner.database.close.called is True

    @pytest.mark.asyncio
    async def test_run_starts_message_processor(self, mock_config) -> None:
        """Test that run starts the message processor when enabled."""
        mock_config.message_tracking = MagicMock()
        mock_config.message_tracking.enabled = True
        mock_config.message_tracking.poll_interval = 5.0
        mock_config.databases.qdrant.url = ""
        mock_config.databases.falkordb.password = None
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
        config_runtime_patch = next(p for p in patches if p.attribute == "ConfigRuntime")
        config_runtime = config_runtime_patch.kwargs["return_value"]

        async def register_subscriber(subscriber: Any) -> Any:
            if subscriber.name == "message_processor":
                prepared = subscriber.builder(SimpleNamespace(desired=mock_config))
                assert prepared is not None
                await asyncio.to_thread(prepared.activate)
            return config_runtime.snapshot

        config_runtime.register_subscriber = AsyncMock(side_effect=register_subscriber)
        patches = [p for p in patches if p.attribute != "SessionMessageProcessor"]

        def apply_stateful_services(runner: GobbyRunner) -> None:
            runner.text_generation_service = None
            runner.llm_service = None
            runner.tool_chat_service = None
            runner.vector_store = None
            runner.memory_manager = None
            runner.memory_backup_manager = None
            runner.code_indexer = None
            runner.mcp_proxy = mock_mcp_manager
            runner.message_processor = mock_message_processor
            runner.task_validator = None

        patches.append(
            patch(
                "gobby.runner_init.services._apply_stateful_services",
                side_effect=apply_stateful_services,
            )
        )
        patches.append(
            patch(
                "gobby.runner_init.services.SessionMessageProcessor",
                return_value=mock_message_processor,
            )
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = await GobbyRunner.create()
            mock_message_processor.start.assert_awaited_once()
            runner._shutdown_requested = True

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    await runner.run(ownership_resolution=FailOpenPidOwnership("test"))

            assert runner._shutdown_requested is True

    @pytest.mark.asyncio
    async def test_run_runs_startup_metrics_cleanup(self, mock_config) -> None:
        """Test that run performs startup metrics cleanup."""
        mock_config.databases.qdrant.url = ""
        mock_config.databases.falkordb.password = None
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

            async def stop_after_mcp_connect() -> None:
                runner._shutdown_requested = True

            mock_mcp_manager.connect_all.side_effect = stop_after_mcp_connect

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    await runner.run(ownership_resolution=FailOpenPidOwnership("test"))

            runner.metrics_manager.cleanup_old_metrics.assert_called()
            assert runner.metrics_manager.cleanup_old_metrics.call_count >= 1
            assert runner._shutdown_requested is True

    @pytest.mark.asyncio
    async def test_run_handles_startup_metrics_cleanup_error(self, mock_config) -> None:
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
            loop = asyncio.get_running_loop()
            cleanup_started = asyncio.Event()

            def fail_cleanup() -> None:
                loop.call_soon_threadsafe(cleanup_started.set)
                runner._shutdown_requested = True
                raise Exception("Cleanup failed")

            runner.metrics_manager.cleanup_old_metrics = MagicMock(side_effect=fail_cleanup)

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()

                async def serve_until_cleanup() -> None:
                    await cleanup_started.wait()

                mock_server.serve = AsyncMock(side_effect=serve_until_cleanup)
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    await runner.run(ownership_resolution=FailOpenPidOwnership("test"))

            assert runner.metrics_manager.cleanup_old_metrics.call_count == 1
            assert runner.database.close.called is True

    @pytest.mark.asyncio
    async def test_run_fatal_error_exits(self, mock_config) -> None:
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

            with (
                patch(
                    "gobby.runner_maintenance.setup_signal_handlers",
                    side_effect=Exception("Fatal error"),
                ),
                pytest.raises(SystemExit) as exc_info,
            ):
                await runner.run(ownership_resolution=FailOpenPidOwnership("test"))

            assert exc_info.value.code == 1

    @pytest.mark.asyncio
    async def test_run_cancels_metrics_cleanup_task_on_shutdown(self, mock_config) -> None:
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
                    await runner.run(ownership_resolution=FailOpenPidOwnership("test"))

            assert (
                runner._metrics_cleanup_task is None
                or runner._metrics_cleanup_task.done()
                or runner._metrics_cleanup_task.cancelled()
            )
            assert runner.database.close.called is True


class TestWebSocketServerShutdown:
    """Tests for WebSocket server shutdown handling."""

    @pytest.mark.asyncio
    async def test_run_with_websocket_shutdown(self, mock_config_with_websocket) -> None:
        """Test run properly shuts down WebSocket server."""
        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock()
        mock_mcp_manager.disconnect_all = AsyncMock()

        mock_ws_server = AsyncMock()
        websocket_started = asyncio.Event()

        async def ws_start() -> None:
            websocket_started.set()

        patches = create_base_patches(
            mock_config=mock_config_with_websocket,
            mock_mcp_manager=mock_mcp_manager,
            mock_ws_server=mock_ws_server,
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            mock_ws_server.start = AsyncMock(side_effect=ws_start)

            async def init_subsystems(runner_arg, _rebuild_vector_store) -> None:
                runner_arg._websocket_task = asyncio.create_task(
                    runner_arg.websocket_server.start()
                )
                runner_arg._shutdown_requested = True

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with (
                    patch("gobby.runner_maintenance.setup_signal_handlers"),
                    patch("gobby.runner_lifecycle._init_subsystems", side_effect=init_subsystems),
                ):
                    await asyncio.wait_for(
                        runner.run(ownership_resolution=FailOpenPidOwnership("test")), timeout=10.0
                    )

            assert mock_ws_server.start.await_count == 1
            assert websocket_started.is_set()
            assert runner.database.close.called is True

    @pytest.mark.asyncio
    async def test_run_websocket_shutdown_timeout(self, mock_config_with_websocket) -> None:
        """Test run handles WebSocket server shutdown timeout."""
        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock()
        mock_mcp_manager.disconnect_all = AsyncMock()

        mock_ws_server = AsyncMock()
        websocket_started = asyncio.Event()

        async def ws_start_hang() -> None:
            websocket_started.set()
            try:
                await _never_complete()
            except asyncio.CancelledError:
                await _never_complete()

        patches = create_base_patches(
            mock_config=mock_config_with_websocket,
            mock_mcp_manager=mock_mcp_manager,
            mock_ws_server=mock_ws_server,
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            mock_ws_server.start = AsyncMock(side_effect=ws_start_hang)

            async def init_subsystems(runner_arg, _rebuild_vector_store) -> None:
                runner_arg._websocket_task = asyncio.create_task(
                    runner_arg.websocket_server.start()
                )
                runner_arg._shutdown_requested = True

            async def shutdown_websocket_server(runner_arg) -> None:
                from gobby.runner_lifecycle_shutdown import _shutdown_websocket_server

                await _shutdown_websocket_server(runner_arg, timeout=0.01)

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with (
                    patch("gobby.runner_maintenance.setup_signal_handlers"),
                    patch("gobby.runner_lifecycle._init_subsystems", side_effect=init_subsystems),
                    patch(
                        "gobby.runner_lifecycle._shutdown_websocket_server",
                        side_effect=shutdown_websocket_server,
                    ),
                ):
                    await asyncio.wait_for(
                        runner.run(ownership_resolution=FailOpenPidOwnership("test")), timeout=15.0
                    )

            assert mock_ws_server.start.await_count == 1
            assert websocket_started.is_set()
            assert runner.database.close.called is True


class TestMetricsCleanupTaskShutdown:
    """Tests for metrics cleanup task shutdown behavior."""

    @pytest.mark.asyncio
    async def test_run_handles_metrics_cleanup_task_cancelled_error(self, mock_config) -> None:
        """Test run handles CancelledError from metrics cleanup task cancellation."""
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

                    async def delayed_shutdown() -> None:
                        runner._shutdown_requested = True

                    shutdown_task = asyncio.create_task(delayed_shutdown())

                    await asyncio.wait_for(
                        runner.run(ownership_resolution=FailOpenPidOwnership("test")), timeout=10.0
                    )
                    await shutdown_task

            assert runner._metrics_cleanup_task is None or runner._metrics_cleanup_task.done()
            assert runner.database.close.called is True


class TestGobbyRunnerShutdownExtended:
    """Tests for GobbyRunner shutdown behavior."""

    @pytest.mark.asyncio
    async def test_run_calls_disconnect_on_shutdown(self, mock_config) -> None:
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
                    await runner.run(ownership_resolution=FailOpenPidOwnership("test"))

            mock_mcp_manager.disconnect_all.assert_called_once()
            assert mock_mcp_manager.disconnect_all.await_count == 1
            assert runner.database.close.called is True

    @pytest.mark.asyncio
    async def test_run_shuts_down_telemetry_before_closing_database(self, mock_config) -> None:
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
                    await runner.run(ownership_resolution=FailOpenPidOwnership("test"))

            assert events == ["telemetry", "database"]
            assert runner.database.close.call_count == 1

    @pytest.mark.asyncio
    async def test_run_does_not_close_code_index_graph_client(self, mock_config) -> None:
        """Code index graph projection is owned by gcode, not runner shutdown."""
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
            runner.code_indexer = SimpleNamespace(
                close_graph_client=AsyncMock(side_effect=lambda: events.append("code_graph"))
            )
            runner.database.close.side_effect = lambda: events.append("database")

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    await runner.run(ownership_resolution=FailOpenPidOwnership("test"))

            assert events == ["database"]
            runner.code_indexer.close_graph_client.assert_not_called()
            assert runner.database.close.call_count == 1


class TestShutdownSessionStatusLifecycle:
    def test_late_session_status_event_skips_storage_during_shutdown(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            app_context,
            "_current_container",
            SimpleNamespace(shutdown_in_progress=True),
        )
        session_manager = MagicMock()
        handlers = EventHandlers(session_manager=session_manager)
        event = make_event(
            HookEventType.AFTER_AGENT,
            metadata={"_platform_session_id": "sess-1"},
        )

        response = handlers.handle_after_agent(event)

        assert response.decision == "allow"
        session_manager.update_session_status.assert_not_called()

    async def test_shutdown_skips_hook_manager_fallback_after_lifespan_shutdown(self) -> None:
        events: list[str] = []
        hook_manager = SimpleNamespace(
            _shutdown_complete=True,
            shutdown_async=AsyncMock(),
        )
        runner = SimpleNamespace(
            _shutdown_intent=ShutdownIntent.STOP,
            http_server=SimpleNamespace(
                services=SimpleNamespace(startup_ready=True, shutdown_in_progress=False),
                _hook_manager=hook_manager,
                _terminate_streamable_http_sessions=AsyncMock(),
            ),
            lifecycle_manager=SimpleNamespace(stop=AsyncMock()),
            agent_lifecycle_monitor=None,
            cron_scheduler=None,
            message_processor=None,
            communications_manager=None,
            config=SimpleNamespace(ui=SimpleNamespace(enabled=False, mode="production")),
            memory_manager=None,
            vector_store=None,
            mcp_proxy=SimpleNamespace(disconnect_all=AsyncMock()),
            database=SimpleNamespace(
                close=MagicMock(side_effect=lambda: events.append("database"))
            ),
        )
        server = SimpleNamespace(should_exit=False)

        async def server_done() -> None:
            return None

        await runner_lifecycle_shutdown.shutdown_daemon_services(
            runner,
            server,
            asyncio.create_task(server_done()),
            1,
            await_critical_stop_hook_grace_window=AsyncMock(),
            shutdown_websocket_server=AsyncMock(),
            reap_remaining_child_processes=AsyncMock(),
            shutdown_telemetry=lambda: events.append("telemetry"),
            cleanup_pid_file=lambda: events.append("cleanup"),
        )

        assert events == ["telemetry", "database", "cleanup"]
        assert runner.http_server._hook_manager is hook_manager
        hook_manager.shutdown_async.assert_not_awaited()
        runner.database.close.assert_called_once()

    async def test_http_stop_cannot_downgrade_restart_before_shutdown_capture(
        self, tmp_path
    ) -> None:
        from gobby.servers.routes.admin._lifecycle import _request_runner_shutdown

        runner = object.__new__(GobbyRunner)
        runner._shutdown_requested = False
        runner._shutdown_intent = ShutdownIntent.STOP
        runner.http_server = SimpleNamespace(services=None)
        runner.database = SimpleNamespace(close=MagicMock())
        server = SimpleNamespace(_runner=runner)

        restart_requested = asyncio.Event()

        async def request_restart() -> None:
            assert _request_runner_shutdown(server, ShutdownIntent.RESTART) is True
            restart_requested.set()

        async def request_http_stop() -> None:
            await restart_requested.wait()
            assert _request_runner_shutdown(server, ShutdownIntent.STOP) is True

        await asyncio.gather(request_restart(), request_http_stop())

        graceful_shutdown = AsyncMock()
        async_cleanup = AsyncMock()

        async def server_done() -> None:
            return None

        with (
            patch.object(
                runner_lifecycle_shutdown,
                "_run_graceful_shutdown_sequence",
                graceful_shutdown,
            ),
            patch.object(
                runner_lifecycle_shutdown,
                "_run_async_shutdown_cleanup",
                async_cleanup,
            ),
            patch.object(
                runner_lifecycle_shutdown,
                "get_shutdown_marker_path",
                return_value=tmp_path / "shutdown.json",
            ),
        ):
            await runner_lifecycle_shutdown.shutdown_daemon_services(
                runner,
                server,
                asyncio.create_task(server_done()),
                1,
                await_critical_stop_hook_grace_window=AsyncMock(),
                shutdown_websocket_server=AsyncMock(),
                reap_remaining_child_processes=AsyncMock(),
                shutdown_telemetry=MagicMock(),
                cleanup_pid_file=MagicMock(),
            )

        assert runner._shutdown_intent is ShutdownIntent.RESTART
        assert graceful_shutdown.await_args.kwargs["shutdown_intent"] is ShutdownIntent.RESTART

    async def test_database_closes_after_session_writing_services_stop(self) -> None:
        events: list[str] = []
        approval_timeout_started = asyncio.Event()

        async def approval_timeout_loop() -> None:
            approval_timeout_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                events.append("approval-timeout-cancel")
                raise

        async def lifecycle_stop() -> None:
            events.append("lifecycle")

        async def hook_shutdown() -> None:
            events.append("hook")

        async def agent_monitor_stop() -> None:
            events.append("agent-monitor")

        async def message_processor_stop() -> None:
            events.append("message-processor")

        async def terminate_sessions() -> None:
            events.append("sessions")

        async def disconnect_mcp() -> None:
            events.append("mcp")

        runner = SimpleNamespace(
            _shutdown_intent=ShutdownIntent.STOP,
            http_server=SimpleNamespace(
                services=SimpleNamespace(startup_ready=True, shutdown_in_progress=False),
                _hook_manager=SimpleNamespace(shutdown_async=AsyncMock(side_effect=hook_shutdown)),
                _terminate_streamable_http_sessions=AsyncMock(side_effect=terminate_sessions),
            ),
            lifecycle_manager=SimpleNamespace(stop=AsyncMock(side_effect=lifecycle_stop)),
            agent_lifecycle_monitor=SimpleNamespace(stop=AsyncMock(side_effect=agent_monitor_stop)),
            cron_scheduler=None,
            message_processor=SimpleNamespace(stop=AsyncMock(side_effect=message_processor_stop)),
            communications_manager=None,
            config=SimpleNamespace(ui=SimpleNamespace(enabled=False, mode="production")),
            memory_manager=None,
            vector_store=None,
            mcp_proxy=SimpleNamespace(disconnect_all=AsyncMock(side_effect=disconnect_mcp)),
            database=SimpleNamespace(
                close=MagicMock(side_effect=lambda: events.append("database"))
            ),
            _approval_timeout_task=asyncio.create_task(approval_timeout_loop()),
        )
        server = SimpleNamespace(should_exit=False)

        async def server_done() -> None:
            return None

        await approval_timeout_started.wait()
        await runner_lifecycle_shutdown.shutdown_daemon_services(
            runner,
            server,
            asyncio.create_task(server_done()),
            1,
            await_critical_stop_hook_grace_window=AsyncMock(),
            shutdown_websocket_server=AsyncMock(),
            reap_remaining_child_processes=AsyncMock(),
            shutdown_telemetry=MagicMock(),
            cleanup_pid_file=MagicMock(),
        )

        assert runner._approval_timeout_task.cancelled()
        assert events == [
            "sessions",
            "lifecycle",
            "agent-monitor",
            "message-processor",
            "approval-timeout-cancel",
            "hook",
            "mcp",
            "database",
        ]
        database_index = events.index("database")
        for event in (
            "sessions",
            "lifecycle",
            "agent-monitor",
            "message-processor",
            "hook",
            "approval-timeout-cancel",
            "mcp",
        ):
            assert events.index(event) < database_index


class TestFinalizerExpiryBackstop:
    """Expiry-branch state that drives main()'s forced-exit backstop."""

    @pytest.mark.asyncio
    async def test_expiry_branch_sets_backstop_state(self, monkeypatch) -> None:
        monkeypatch.setattr(runner_lifecycle_shutdown, "_FINALIZER_SETTLE_SECONDS", 0.05)
        monkeypatch.setattr(
            runner_lifecycle_shutdown,
            "_run_terminal_delivery_finalizers",
            lambda runner: _never_complete(),
        )
        runner_lifecycle_shutdown._reset_finalizer_expiry_backstop()
        try:
            cancellation = await runner_lifecycle_shutdown._settle_finalizers_under_cancellation(
                SimpleNamespace(db_executor=None),
            )
            assert cancellation is None
            assert runner_lifecycle_shutdown.finalizer_expiry_backstop_required()
        finally:
            runner_lifecycle_shutdown._reset_finalizer_expiry_backstop()

    @pytest.mark.asyncio
    async def test_settled_finalizer_leaves_backstop_unset(self, monkeypatch) -> None:
        async def _instant(runner) -> None:
            return None

        monkeypatch.setattr(
            runner_lifecycle_shutdown, "_run_terminal_delivery_finalizers", _instant
        )
        runner_lifecycle_shutdown._reset_finalizer_expiry_backstop()
        cancellation = await runner_lifecycle_shutdown._settle_finalizers_under_cancellation(
            SimpleNamespace(db_executor=None),
        )
        assert cancellation is None
        assert not runner_lifecycle_shutdown.finalizer_expiry_backstop_required()


class TestForceExitBackstop:
    """main()'s expiry-branch os._exit backstop."""

    def _arm(self, monkeypatch) -> list[int]:
        from gobby import runner as runner_module

        exits: list[int] = []
        monkeypatch.setattr(runner_module.os, "_exit", exits.append)
        monkeypatch.setattr(runner_module.logging, "shutdown", lambda: None)
        return exits

    def test_no_exit_when_backstop_unset(self, monkeypatch) -> None:
        from gobby import runner as runner_module

        exits = self._arm(monkeypatch)
        runner_lifecycle_shutdown._reset_finalizer_expiry_backstop()
        runner_module._force_exit_after_expired_settlement()
        assert exits == []

    def test_forces_exit_code_zero_on_clean_unwind(self, monkeypatch) -> None:
        from gobby import runner as runner_module

        exits = self._arm(monkeypatch)
        monkeypatch.setattr(runner_lifecycle_shutdown, "_expiry_exit_backstop_required", True)
        runner_module._force_exit_after_expired_settlement()
        assert exits == [0]

    def test_forces_exit_with_systemexit_code_from_unwind(self, monkeypatch) -> None:
        from gobby import runner as runner_module

        exits = self._arm(monkeypatch)
        monkeypatch.setattr(runner_lifecycle_shutdown, "_expiry_exit_backstop_required", True)
        with pytest.raises(SystemExit):
            try:
                raise SystemExit(5)
            finally:
                runner_module._force_exit_after_expired_settlement()
        assert exits == [5]


class TestExpiryBackstopSubprocess:
    """Plan 1.4.19 subprocess shape: a wedged process still exits via the backstop."""

    def test_backstop_forces_wedged_process_exit_with_pid_released(self, tmp_path) -> None:
        import subprocess
        import sys
        import textwrap

        pid_file = tmp_path / "gobby.pid"
        script = textwrap.dedent(
            f"""
            import threading

            from pathlib import Path

            import gobby.runner_lifecycle_shutdown as shutdown
            from gobby.runner import _force_exit_after_expired_settlement
            from gobby.runner_pid_file import claim_pid_file

            claim = claim_pid_file(Path({str(pid_file)!r}))
            assert claim is not None

            # A wedged non-daemon worker would block interpreter exit at the
            # atexit thread join without the backstop.
            threading.Thread(
                target=threading.Event().wait, name="wedged-settlement-worker"
            ).start()

            shutdown._expiry_exit_backstop_required = True
            try:
                pass
            finally:
                claim.release()
                _force_exit_after_expired_settlement()
            print("UNREACHABLE")
            """
        )

        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            check=False,
            text=True,
            timeout=20.0,
        )

        assert completed.returncode == 0, completed.stderr
        assert "UNREACHABLE" not in completed.stdout

        from gobby.runner_pid_file import claim_pid_file

        reclaim = claim_pid_file(pid_file)
        assert reclaim is not None
        reclaim.release()


class TestStopShutdownAgentPreservation:
    """#18974: STOP-intent shutdown parks agent runs instead of cancelling them."""

    @pytest.mark.asyncio
    async def test_stop_shutdown_never_cancels_agent_runs(self, tmp_path: Path) -> None:
        run = SimpleNamespace(
            id="run-preserved",
            pid=4242,
            terminal_id=None,
            resume_metadata_json=None,
        )
        run_storage = MagicMock()
        run_storage.list_active_for_machine.return_value = [run]
        reap = AsyncMock()
        runner = SimpleNamespace(
            _shutdown_intent=ShutdownIntent.STOP,
            http_server=SimpleNamespace(
                services=SimpleNamespace(startup_ready=True, shutdown_in_progress=False),
                _hook_manager=SimpleNamespace(
                    _shutdown_complete=True,
                    shutdown_async=AsyncMock(),
                ),
                _terminate_streamable_http_sessions=AsyncMock(),
            ),
            lifecycle_manager=SimpleNamespace(stop=AsyncMock()),
            agent_lifecycle_monitor=SimpleNamespace(stop=AsyncMock()),
            cron_scheduler=None,
            message_processor=None,
            communications_manager=None,
            config=SimpleNamespace(ui=SimpleNamespace(enabled=False, mode="production")),
            memory_manager=None,
            vector_store=None,
            mcp_proxy=SimpleNamespace(disconnect_all=AsyncMock()),
            database=SimpleNamespace(close=MagicMock()),
            agent_runner=SimpleNamespace(run_storage=run_storage),
        )
        server = SimpleNamespace(should_exit=False)

        async def server_done() -> None:
            return None

        try:
            with patch.object(
                runner_lifecycle_shutdown,
                "get_shutdown_marker_path",
                return_value=tmp_path / "shutdown.json",
            ):
                await runner_lifecycle_shutdown.shutdown_daemon_services(
                    cast(GobbyRunner, runner),
                    cast("uvicorn.Server", server),
                    asyncio.create_task(server_done()),
                    1,
                    await_critical_stop_hook_grace_window=AsyncMock(),
                    shutdown_websocket_server=AsyncMock(),
                    reap_remaining_child_processes=reap,
                    shutdown_telemetry=MagicMock(),
                    cleanup_pid_file=MagicMock(),
                )
        finally:
            # The real cleanup closed the process-global terminal-delivery
            # admission gate; reopen it so later tests in this process can
            # start shielded deliveries again.
            terminal_delivery.reopen_terminal_delivery_admission()

        # The old shutdown-time cancellation helper is gone; run storage sees
        # only the preservation listing and no cancel/fail/terminalize writes.
        assert not hasattr(runner_lifecycle_shutdown, "_cancel_active_agent_runs_for_shutdown")
        assert run_storage.list_active_for_machine.called
        assert {name for name, _args, _kwargs in run_storage.mock_calls} == {
            "list_active_for_machine"
        }
        reap.assert_awaited_once_with(preserve_agents=True, preserved_agent_pids={4242})
        runner.agent_lifecycle_monitor.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_async_cleanup_skips_child_reap_when_preserve_set_unknown(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        events: list[str] = []

        async def cancel_health_checks() -> None:
            events.append("health")

        async def drain_deliveries() -> None:
            events.append("drain")

        async def shutdown_executor(_executor: object) -> None:
            events.append("executor")

        async def unknown_preserve_set(_runner: object) -> set[int] | None:
            events.append("preserve")
            return None

        reap = AsyncMock()
        monkeypatch.setattr(
            runner_lifecycle_shutdown,
            "close_terminal_delivery_admission",
            lambda: events.append("close"),
        )
        monkeypatch.setattr(
            runner_lifecycle_shutdown,
            "cancel_and_await_health_checks",
            cancel_health_checks,
        )
        monkeypatch.setattr(
            runner_lifecycle_shutdown,
            "drain_shielded_terminal_deliveries",
            drain_deliveries,
        )
        monkeypatch.setattr(
            runner_lifecycle_shutdown,
            "_shutdown_database_executor",
            shutdown_executor,
        )
        monkeypatch.setattr(
            runner_lifecycle_processes,
            "_preserved_agent_terminal_pids",
            unknown_preserve_set,
        )

        await runner_lifecycle_shutdown._run_async_shutdown_cleanup(
            cast(GobbyRunner, SimpleNamespace(db_executor=object())),
            shutdown_intent=ShutdownIntent.STOP,
            reap_remaining_child_processes=reap,
            shutdown_telemetry=lambda: events.append("telemetry"),
        )

        reap.assert_not_awaited()
        assert "preserve" in events
        assert "telemetry" in events
        assert events[-2:] == ["telemetry", "executor"]


@pytest.mark.asyncio
async def test_shutdown_drains_terminal_effects_before_storage_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    async def drain(_bridge: object, *, timeout_seconds: float) -> None:
        del timeout_seconds
        events.append("drain")

    async def close_storage(_runner: object) -> None:
        events.append("close")

    monkeypatch.setattr(
        "gobby.runner_lifecycle_terminal_effects.drain_terminal_effects",
        drain,
    )
    monkeypatch.setattr(
        runner_lifecycle_shutdown,
        "_close_managers_and_storage",
        close_storage,
    )
    source = (
        Path(__file__).resolve().parents[1] / "src/gobby/runner_lifecycle_shutdown.py"
    ).read_text(encoding="utf-8")
    start = source.find("async def _run_graceful_shutdown_sequence")
    assert start != -1
    chunk = source[start:]
    drain_at = chunk.find("drain_terminal_effects")
    close_at = chunk.find("_close_managers_and_storage")
    assert drain_at != -1
    assert close_at != -1
    assert drain_at < close_at
