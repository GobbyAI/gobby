"""Runner shutdown tests."""

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.runner import GobbyRunner
from tests.runner_helpers import create_base_patches

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("fast_stop_hook_grace_window")]


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

                async def hanging_serve():
                    await asyncio.sleep(100)

                mock_server.serve = hanging_serve
                mock_server.should_exit = False
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    await asyncio.wait_for(runner.run(), timeout=25.0)

    @pytest.mark.asyncio
    async def test_run_handles_lifecycle_manager_shutdown_timeout(self, mock_config):
        """Test that run handles lifecycle manager shutdown timeout."""
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
            runner.http_server._terminate_streamable_http_sessions.side_effect = terminate_sessions

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


class TestWebSocketServerShutdown:
    """Tests for WebSocket server shutdown handling."""

    @pytest.mark.asyncio
    async def test_run_with_websocket_shutdown(self, mock_config_with_websocket):
        """Test run properly shuts down WebSocket server."""
        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock()
        mock_mcp_manager.disconnect_all = AsyncMock()

        mock_ws_server = AsyncMock()

        async def ws_start():
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
        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock()
        mock_mcp_manager.disconnect_all = AsyncMock()

        mock_ws_server = AsyncMock()

        async def ws_start_hang():
            try:
                await asyncio.sleep(1000)
            except asyncio.CancelledError:
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
                    await asyncio.wait_for(runner.run(), timeout=15.0)


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

                    async def delayed_shutdown():
                        await asyncio.sleep(0.1)
                        runner._shutdown_requested = True

                    shutdown_task = asyncio.create_task(delayed_shutdown())

                    await asyncio.wait_for(runner.run(), timeout=10.0)
                    await shutdown_task


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
