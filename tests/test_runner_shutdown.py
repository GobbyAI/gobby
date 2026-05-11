"""Runner shutdown tests."""

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.runner import GobbyRunner
from tests.runner_helpers import create_base_patches

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("fast_stop_hook_grace_window")]


async def _never_complete() -> None:
    await asyncio.Event().wait()


class _ExitAwareServer:
    def __init__(self) -> None:
        self._should_exit = False
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
            assert http_shutdown_complete is True
            assert runner.database.close.called is True

    @pytest.mark.asyncio
    async def test_run_handles_http_server_shutdown_timeout(self, mock_config):
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
                    await asyncio.wait_for(runner.run(), timeout=25.0)

            assert mock_server.should_exit is True
            assert runner.database.close.called is True

    @pytest.mark.asyncio
    async def test_run_handles_lifecycle_manager_shutdown_timeout(self, mock_config):
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
                    await asyncio.wait_for(runner.run(), timeout=10.0)

            assert mock_lifecycle_manager.stop.await_count == 1
            assert runner.database.close.called is True

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
                    await asyncio.wait_for(runner.run(), timeout=10.0)

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

            async def terminate_sessions() -> None:
                events.append("terminate")
                assert mock_server.should_exit is True

            fast_stop_hook_grace_window.side_effect = note_grace_wait
            runner.http_server._terminate_streamable_http_sessions.side_effect = terminate_sessions

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = _ExitAwareServer()

                async def serve() -> None:
                    await mock_server.exit_requested.wait()
                    events.append("serve-exit")

                mock_server.serve = AsyncMock(side_effect=serve)
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    await asyncio.wait_for(runner.run(), timeout=10.0)

            fast_stop_hook_grace_window.assert_awaited_once()
            runner.http_server._terminate_streamable_http_sessions.assert_awaited_once()
            assert events[:2] == ["grace", "terminate"]
            assert events[-1] == "serve-exit"

    @pytest.mark.asyncio
    async def test_run_handles_message_processor_shutdown_timeout(self, mock_config):
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
                    await asyncio.wait_for(runner.run(), timeout=10.0)

            assert mock_message_processor.stop.await_count == 1
            assert runner.database.close.called is True

    @pytest.mark.asyncio
    async def test_run_handles_mcp_disconnect_timeout(self, mock_config):
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
                    await asyncio.wait_for(runner.run(), timeout=10.0)

            assert mock_mcp_manager.disconnect_all.await_count == 1
            assert runner.database.close.called is True

    @pytest.mark.asyncio
    async def test_run_starts_message_processor(self, mock_config):
        """Test that run starts the message processor when enabled."""
        mock_config.message_tracking = MagicMock()
        mock_config.message_tracking.enabled = True
        mock_config.message_tracking.poll_interval = 5.0
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
            patch(
                "gobby.runner_init.services.SessionMessageProcessor",
                return_value=mock_message_processor,
            )
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            async def stop_after_mcp_connect() -> None:
                runner._shutdown_requested = True

            mock_mcp_manager.connect_all.side_effect = stop_after_mcp_connect

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    await runner.run()

            mock_message_processor.start.assert_called_once()
            assert mock_message_processor.start.await_count == 1
            assert runner._shutdown_requested is True

    @pytest.mark.asyncio
    async def test_run_runs_startup_metrics_cleanup(self, mock_config):
        """Test that run performs startup metrics cleanup."""
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

            async def stop_after_mcp_connect() -> None:
                runner._shutdown_requested = True

            mock_mcp_manager.connect_all.side_effect = stop_after_mcp_connect

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    await runner.run()

            runner.metrics_manager.cleanup_old_metrics.assert_called()
            assert runner.metrics_manager.cleanup_old_metrics.call_count >= 1
            assert runner._shutdown_requested is True

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

            def fail_cleanup() -> None:
                runner._shutdown_requested = True
                raise Exception("Cleanup failed")

            runner.metrics_manager.cleanup_old_metrics = MagicMock(side_effect=fail_cleanup)

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    await runner.run()

            assert runner.metrics_manager.cleanup_old_metrics.call_count == 1
            assert runner.database.close.called is True

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

            assert (
                runner._metrics_cleanup_task is None
                or runner._metrics_cleanup_task.done()
                or runner._metrics_cleanup_task.cancelled()
            )
            assert runner.database.close.called is True


class TestWebSocketServerShutdown:
    """Tests for WebSocket server shutdown handling."""

    @pytest.mark.asyncio
    async def test_run_with_websocket_shutdown(self, mock_config_with_websocket):
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
                    await asyncio.wait_for(runner.run(), timeout=10.0)

            assert mock_ws_server.start.await_count == 1
            assert websocket_started.is_set()
            assert runner.database.close.called is True

    @pytest.mark.asyncio
    async def test_run_websocket_shutdown_timeout(self, mock_config_with_websocket):
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
                    await asyncio.wait_for(runner.run(), timeout=15.0)

            assert mock_ws_server.start.await_count == 1
            assert websocket_started.is_set()
            assert runner.database.close.called is True


class TestMetricsCleanupTaskShutdown:
    """Tests for metrics cleanup task shutdown behavior."""

    @pytest.mark.asyncio
    async def test_run_handles_metrics_cleanup_task_cancelled_error(self, mock_config):
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

                    await asyncio.wait_for(runner.run(), timeout=10.0)
                    await shutdown_task

            assert runner._metrics_cleanup_task is None or runner._metrics_cleanup_task.done()
            assert runner.database.close.called is True


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

            mock_mcp_manager.disconnect_all.assert_called_once()
            assert mock_mcp_manager.disconnect_all.await_count == 1
            assert runner.database.close.called is True

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
            assert runner.database.close.call_count == 1
