"""Runner lifecycle, maintenance, and entrypoint tests."""

import asyncio
import logging
import os
import signal
import sys
from contextlib import ExitStack, suppress
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import gobby.runner_lifecycle as runner_lifecycle
import gobby.runner_lifecycle_shutdown as runner_lifecycle_shutdown
from gobby.runner import GobbyRunner, main, run_gobby
from gobby.shutdown_intent import ShutdownIntent
from tests.runner_helpers import create_base_patches

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("fast_stop_hook_grace_window")]


class TestGobbyRunnerSignalHandlers:
    """Tests for signal handler setup."""

    def test_setup_signal_handlers(self) -> None:
        """Test that signal handlers are registered."""
        from gobby.runner_maintenance import setup_signal_handlers

        mock_loop = MagicMock()
        mock_callback = MagicMock()

        with patch("asyncio.get_running_loop", return_value=mock_loop):
            setup_signal_handlers(mock_callback)

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

            async def stop_after_mcp_connect() -> None:
                runner._shutdown_requested = True

            mock_mcp_manager.connect_all.side_effect = stop_after_mcp_connect

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    await runner.run()

            mock_mcp_manager.connect_all.assert_called_once()
            mock_mcp_manager.disconnect_all.assert_called_once()
            assert runner._shutdown_requested is True
            assert runner.database.close.called is True

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
                    await runner.run()

            assert mock_mcp_manager.disconnect_all.await_count == 1
            assert runner.database.close.called is True

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
                    await runner.run()

            assert mock_mcp_manager.disconnect_all.await_count == 1
            assert runner.database.close.called is True

    @pytest.mark.asyncio
    async def test_run_with_websocket_server(self, mock_config_with_websocket):
        """Test run with WebSocket server enabled."""
        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock()
        mock_mcp_manager.disconnect_all = AsyncMock()

        mock_ws_server = AsyncMock()
        mock_ws_server.start = AsyncMock()

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

            async def stop_after_mcp_connect() -> None:
                runner._shutdown_requested = True

            mock_mcp_manager.connect_all.side_effect = stop_after_mcp_connect

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    await runner.run()

            mock_ws_server.start.assert_called()
            assert mock_ws_server.start.call_count == 1
            assert runner._shutdown_requested is True

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

            assert runner.http_server.websocket_server == mock_ws_server
            assert mock_http.websocket_server == mock_ws_server


class TestInitSubsystems:
    """Tests for subsystem initialization helpers."""

    @pytest.mark.asyncio
    async def test_init_subsystems_uses_embedding_readiness_helper_and_stays_alive(self) -> None:
        runner = SimpleNamespace(
            http_server=SimpleNamespace(
                services=SimpleNamespace(provider_model_catalog=None),
            ),
            mcp_proxy=SimpleNamespace(connect_all=AsyncMock()),
            config=SimpleNamespace(
                databases=SimpleNamespace(
                    qdrant=SimpleNamespace(url=""),
                    neo4j=SimpleNamespace(url=""),
                ),
                embeddings=SimpleNamespace(
                    model="nomic-embed-text",
                    api_base="http://localhost:1234/v1",
                    api_key="lm-studio",
                    dim=768,
                ),
                ui=SimpleNamespace(enabled=False, mode="prod", port=5173, host="localhost"),
                telemetry=SimpleNamespace(log_file="/tmp/gobby.log"),
                bind_host="localhost",
            ),
            memory_manager=None,
            metrics_manager=SimpleNamespace(cleanup_old_metrics=MagicMock(return_value=0)),
            vector_store=None,
            message_processor=None,
            communications_manager=None,
            lifecycle_manager=SimpleNamespace(start=AsyncMock()),
            agent_lifecycle_monitor=None,
            cron_scheduler=None,
            code_indexer=None,
            pipeline_executor=None,
            pipeline_execution_manager=None,
            workflow_loader=None,
            completion_registry=None,
            websocket_server=None,
        )
        tracker = runner_lifecycle.StartupTracker()

        with (
            patch.object(runner_lifecycle, "_startup_tracker", tracker),
            patch(
                "gobby.cli.services.ensure_local_embedding_service_ready",
                new=AsyncMock(return_value=False),
            ) as mock_ready,
            patch(
                "gobby.cli.services.get_local_embedding_service_failure_reason",
                return_value="LM Studio server start failed: boom",
            ),
            patch("gobby.agents.tmux.session_manager.TmuxSessionManager") as mock_tmux_manager,
        ):
            mock_tmux_manager.return_value.health_check = AsyncMock()
            await runner_lifecycle._init_subsystems(runner, AsyncMock())

        mock_ready.assert_awaited_once_with(
            model="nomic-embed-text",
            api_base="http://localhost:1234/v1",
            api_key="lm-studio",
            expected_dim=768,
        )
        runner.lifecycle_manager.start.assert_awaited_once()
        assert {
            "subsystem": "Embeddings",
            "error": "LM Studio server start failed: boom",
        } in tracker.errors
        assert tracker.done is True

    @pytest.mark.asyncio
    async def test_qdrant_health_failure_keeps_vector_store(self) -> None:
        vector_store = SimpleNamespace(
            initialize=AsyncMock(side_effect=RuntimeError("qdrant down")),
            ensure_collection=AsyncMock(),
            count=AsyncMock(return_value=0),
        )
        runner = SimpleNamespace(
            http_server=SimpleNamespace(
                services=SimpleNamespace(provider_model_catalog=None),
            ),
            mcp_proxy=SimpleNamespace(connect_all=AsyncMock()),
            config=SimpleNamespace(
                databases=SimpleNamespace(
                    qdrant=SimpleNamespace(url="http://localhost:6333"),
                    neo4j=SimpleNamespace(url=""),
                ),
                embeddings=SimpleNamespace(model="", api_base="", api_key="", dim=768),
                ui=SimpleNamespace(enabled=False, mode="prod", port=5173, host="localhost"),
                telemetry=SimpleNamespace(log_file="/tmp/gobby.log"),
                bind_host="localhost",
            ),
            memory_manager=None,
            metrics_manager=SimpleNamespace(cleanup_old_metrics=MagicMock(return_value=0)),
            vector_store=vector_store,
            message_processor=None,
            communications_manager=None,
            lifecycle_manager=SimpleNamespace(start=AsyncMock()),
            agent_lifecycle_monitor=None,
            cron_scheduler=None,
            code_indexer=None,
            pipeline_executor=None,
            pipeline_execution_manager=None,
            workflow_loader=None,
            completion_registry=None,
            websocket_server=None,
        )

        with (
            patch("gobby.cli.services.is_qdrant_healthy", new=AsyncMock(return_value=False)),
            patch("gobby.agents.tmux.session_manager.TmuxSessionManager") as mock_tmux_manager,
        ):
            mock_tmux_manager.return_value.health_check = AsyncMock()
            await runner_lifecycle._init_subsystems(runner, AsyncMock())

        assert runner.vector_store is vector_store
        assert runner.lifecycle_manager.start.await_count == 1
        vector_store.initialize.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_provider_model_discovery_runs_in_background_without_startup_warning(
        self, caplog
    ) -> None:
        refresh_started = asyncio.Event()
        refresh_release = asyncio.Event()

        async def slow_refresh(**_kwargs: object) -> None:
            refresh_started.set()
            await refresh_release.wait()

        provider_catalog = SimpleNamespace(refresh=AsyncMock(side_effect=slow_refresh))
        vector_store = SimpleNamespace(
            initialize=AsyncMock(),
            ensure_collection=AsyncMock(),
            count=AsyncMock(return_value=1),
        )
        runner = SimpleNamespace(
            http_server=SimpleNamespace(
                services=SimpleNamespace(provider_model_catalog=provider_catalog),
                codex_client=MagicMock(),
            ),
            mcp_proxy=SimpleNamespace(connect_all=AsyncMock()),
            config=SimpleNamespace(
                databases=SimpleNamespace(
                    qdrant=SimpleNamespace(url=""),
                    neo4j=SimpleNamespace(url=""),
                ),
                embeddings=SimpleNamespace(model="", api_base="", api_key="", dim=768),
                ui=SimpleNamespace(enabled=False, mode="prod", port=5173, host="localhost"),
                telemetry=SimpleNamespace(log_file="/tmp/gobby.log"),
                bind_host="localhost",
            ),
            memory_manager=None,
            metrics_manager=SimpleNamespace(cleanup_old_metrics=MagicMock(return_value=0)),
            vector_store=vector_store,
            message_processor=None,
            communications_manager=None,
            lifecycle_manager=SimpleNamespace(start=AsyncMock()),
            agent_lifecycle_monitor=None,
            cron_scheduler=None,
            code_indexer=None,
            pipeline_executor=None,
            pipeline_execution_manager=None,
            workflow_loader=None,
            completion_registry=None,
            websocket_server=None,
        )

        with patch("gobby.agents.tmux.session_manager.TmuxSessionManager") as mock_tmux_manager:
            mock_tmux_manager.return_value.health_check = AsyncMock()
            await runner_lifecycle._init_subsystems(runner, AsyncMock())

        vector_store.initialize.assert_awaited_once()
        vector_store.ensure_collection.assert_awaited_once()
        await asyncio.wait_for(refresh_started.wait(), timeout=1.0)
        assert "Provider model discovery timed out" not in caplog.text

        task = runner._provider_model_refresh_task
        assert task is not None
        assert not task.done()
        assert refresh_started.is_set()
        refresh_release.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_agent_lifecycle_monitor_startup_failures_are_non_fatal(
        self,
        caplog: pytest.LogCaptureFixture,
        enable_log_propagation: None,
    ) -> None:
        from gobby.runner_lifecycle_subsystems import _start_agent_lifecycle_monitor

        tracker = runner_lifecycle.StartupTracker()
        monitor = SimpleNamespace(
            cleanup_stale_pending_runs=AsyncMock(side_effect=RuntimeError("cleanup failed")),
            start=AsyncMock(side_effect=RuntimeError("start failed")),
        )
        runner = SimpleNamespace(agent_lifecycle_monitor=monitor)
        reconcile = AsyncMock(side_effect=RuntimeError("reconcile failed"))

        with caplog.at_level(logging.ERROR, logger="gobby.runner_lifecycle"):
            await _start_agent_lifecycle_monitor(runner, tracker, reconcile)

        reconcile.assert_awaited_once_with(runner)
        monitor.cleanup_stale_pending_runs.assert_awaited_once()
        monitor.start.assert_awaited_once()
        assert tracker.steps_completed == []
        assert tracker.errors == [
            {
                "subsystem": "Agent lifecycle monitor",
                "error": (
                    "reconcile failed: reconcile failed; cleanup failed: cleanup failed; "
                    "start failed: start failed"
                ),
            }
        ]
        assert "Agent restart reconciliation failed during startup" in caplog.text
        assert "Agent stale pending cleanup failed during startup" in caplog.text
        assert "Agent lifecycle monitor start failed during startup" in caplog.text


class TestShutdownDaemonServices:
    @pytest.mark.asyncio
    async def test_shutdown_marker_is_removed_after_cleanup(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        marker = tmp_path / "shutdown_source.json"
        marker.write_text("{}", encoding="utf-8")
        cleanup_saw_marker = False
        runner = SimpleNamespace(
            _shutdown_intent=ShutdownIntent.STOP,
            http_server=SimpleNamespace(
                services=SimpleNamespace(startup_ready=True, shutdown_in_progress=False),
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
            database=SimpleNamespace(close=MagicMock()),
        )
        server = SimpleNamespace(should_exit=False)
        server_task: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(0))

        def cleanup_pid_file() -> None:
            nonlocal cleanup_saw_marker
            cleanup_saw_marker = marker.exists()

        monkeypatch.setattr(
            runner_lifecycle_shutdown,
            "get_shutdown_marker_path",
            lambda: marker,
        )
        await runner_lifecycle_shutdown.shutdown_daemon_services(
            runner,
            server,
            server_task,
            1,
            await_critical_stop_hook_grace_window=AsyncMock(),
            shutdown_websocket_server=AsyncMock(),
            cancel_active_agent_runs_for_shutdown=AsyncMock(return_value=0),
            reap_remaining_child_processes=AsyncMock(),
            shutdown_telemetry=MagicMock(),
            cleanup_pid_file=cleanup_pid_file,
        )

        assert cleanup_saw_marker is True
        assert marker.exists() is False

    @pytest.mark.asyncio
    async def test_restart_reaps_only_non_terminal_agent_children(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class FakeNoSuchProcess(Exception):
            pass

        class FakeAccessDenied(Exception):
            pass

        class FakeProcess:
            def __init__(
                self,
                pid: int,
                name: str,
                cmdline: list[str],
                children: list["FakeProcess"] | None = None,
            ) -> None:
                self.pid = pid
                self._name = name
                self._cmdline = cmdline
                self._children = children or []
                self._parent: FakeProcess | None = None
                self.terminated = False
                for child in self._children:
                    child._parent = self

            def children(self, recursive: bool = False) -> list["FakeProcess"]:
                if not recursive:
                    return list(self._children)
                result: list[FakeProcess] = []
                pending = list(self._children)
                while pending:
                    child = pending.pop(0)
                    result.append(child)
                    pending.extend(child._children)
                return result

            def parent(self) -> "FakeProcess | None":
                return self._parent

            def name(self) -> str:
                return self._name

            def cmdline(self) -> list[str]:
                return self._cmdline

            def terminate(self) -> None:
                self.terminated = True

            def kill(self) -> None:
                self.terminated = True

        pane = FakeProcess(200, "zsh", ["zsh"])
        tmux = FakeProcess(100, "tmux", ["tmux", "-L", "gobby"], [pane])
        unrelated_tmux = FakeProcess(250, "tmux", ["tmux", "-L", "other"])
        worker = FakeProcess(300, "gcode", ["gcode", "index"])
        current = FakeProcess(os.getpid(), "python", ["python"], [tmux, unrelated_tmux, worker])
        processes = {
            process.pid: process for process in [current, tmux, pane, unrelated_tmux, worker]
        }

        class FakePsutil:
            NoSuchProcess = FakeNoSuchProcess
            AccessDenied = FakeAccessDenied

            @staticmethod
            def Process(pid: int) -> FakeProcess:
                return processes[pid]

            @staticmethod
            def wait_procs(
                children: list[FakeProcess], timeout: float
            ) -> tuple[list[FakeProcess], list[FakeProcess]]:
                return children, []

        monkeypatch.setitem(sys.modules, "psutil", FakePsutil)

        await runner_lifecycle_shutdown._reap_remaining_child_processes(
            preserve_agents=True,
            preserved_agent_pids={pane.pid},
        )

        assert tmux.terminated is False
        assert pane.terminated is False
        assert unrelated_tmux.terminated is True
        assert worker.terminated is True


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
            assert mock_runner.run.await_count == 1


class TestMainFunction:
    """Tests for main synchronous entry point."""

    def _mock_bootstrap(self):
        """Return a patch for load_bootstrap that returns a minimal stub."""
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
                    assert mock_run.call_count == 1

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

                GobbyRunner()

                assert rb._agent_event_callback is not None
        finally:
            rb._agent_event_callback = old_callback

    def test_setup_agent_event_broadcasting_without_websocket(self, mock_config) -> None:
        """Test agent event broadcasting is skipped without WebSocket."""
        import gobby.runner_broadcasting as rb

        patches = create_base_patches(mock_config=mock_config)

        old_callback = rb._agent_event_callback
        try:
            rb._agent_event_callback = None
            with ExitStack() as stack:
                [stack.enter_context(p) for p in patches]

                GobbyRunner()

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

            assert rb._agent_event_callback is not None
        finally:
            rb._agent_event_callback = old_callback


class TestMetricsCleanupLoop:
    """Tests for metrics_cleanup_loop function."""

    @pytest.mark.asyncio
    async def test_metrics_cleanup_loop_runs_cleanup(self, mock_config):
        """Test that metrics cleanup loop runs cleanup."""
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

            intervals: list[float] = []

            async def complete_first_cycle(seconds: float) -> None:
                nonlocal shutdown_requested
                intervals.append(seconds)
                shutdown_requested = True

            task = asyncio.create_task(
                metrics_cleanup_loop(
                    runner.metrics_manager,
                    is_shutdown,
                    interval_seconds=1,
                    sleep=complete_first_cycle,
                )
            )
            await asyncio.wait_for(task, timeout=1.0)

            assert intervals == [1]
            assert runner.metrics_manager.cleanup_old_metrics.call_count == 1

    @pytest.mark.asyncio
    async def test_metrics_cleanup_loop_handles_exception(self, mock_config):
        """Test that metrics cleanup loop handles exceptions gracefully."""
        from gobby.runner_maintenance import metrics_cleanup_loop

        patches = create_base_patches(mock_config=mock_config)

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            runner.metrics_manager.cleanup_old_metrics = MagicMock(
                side_effect=[Exception("Cleanup error"), 0]
            )

            shutdown_requested = False

            def is_shutdown():
                return shutdown_requested

            intervals: list[float] = []

            async def complete_after_retry(seconds: float) -> None:
                nonlocal shutdown_requested
                intervals.append(seconds)
                if len(intervals) == 2:
                    shutdown_requested = True

            task = asyncio.create_task(
                metrics_cleanup_loop(
                    runner.metrics_manager,
                    is_shutdown,
                    interval_seconds=1,
                    sleep=complete_after_retry,
                )
            )
            await asyncio.wait_for(task, timeout=1.0)

            assert intervals == [1, 1]
            assert runner.metrics_manager.cleanup_old_metrics.call_count == 2

    @pytest.mark.asyncio
    async def test_metrics_cleanup_loop_cancelled(self, mock_config):
        """Test that metrics cleanup loop handles cancellation."""
        from gobby.runner_maintenance import metrics_cleanup_loop

        patches = create_base_patches(mock_config=mock_config)

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            runner.metrics_manager.cleanup_old_metrics = MagicMock()

            async def cancelled_sleep(_seconds: float) -> None:
                raise asyncio.CancelledError

            task = asyncio.create_task(
                metrics_cleanup_loop(
                    runner.metrics_manager,
                    lambda: False,
                    interval_seconds=1,
                    sleep=cancelled_sleep,
                )
            )
            await asyncio.wait_for(task, timeout=1.0)

            assert task.done()
            assert runner.metrics_manager.cleanup_old_metrics.call_count == 0


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

        assert captured_handler is not None
        assert shutdown_called is False
        captured_handler()
        assert shutdown_called is True

    def test_signal_handler_still_shuts_down_when_intent_callback_fails(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        enable_log_propagation: None,
    ) -> None:
        from gobby.runner_maintenance import setup_signal_handlers

        mock_loop = MagicMock()
        captured_handler = None

        def capture_handler(sig, handler):
            nonlocal captured_handler
            if sig == signal.SIGTERM:
                captured_handler = handler

        mock_loop.add_signal_handler = capture_handler
        shutdown_callback = MagicMock()
        shutdown_intent_callback = MagicMock(side_effect=RuntimeError("intent failed"))

        with (
            patch("asyncio.get_running_loop", return_value=mock_loop),
            patch("gobby.runner_maintenance.get_gobby_home", return_value=tmp_path),
        ):
            setup_signal_handlers(
                shutdown_callback,
                shutdown_intent_callback=shutdown_intent_callback,
            )

        assert captured_handler is not None
        with caplog.at_level(logging.ERROR, logger="gobby.runner_maintenance"):
            captured_handler()

        shutdown_intent_callback.assert_called_once_with(ShutdownIntent.STOP)
        shutdown_callback.assert_called_once_with()
        assert "Shutdown intent callback failed" in caplog.text


class TestAgentEventBroadcastingCallback:
    """Tests for the broadcast_agent_event callback via fire_agent_event."""

    @pytest.mark.asyncio
    async def test_broadcast_callback_invoked(self):
        """Test that fire_agent_event invokes the broadcast callback."""
        import gobby.runner_broadcasting as rb
        from gobby.runner_broadcasting import fire_agent_event, setup_agent_event_broadcasting

        broadcast_seen = asyncio.Event()

        async def broadcast_agent_event(**_kwargs: object) -> None:
            broadcast_seen.set()

        mock_ws_server = AsyncMock()
        mock_ws_server.broadcast_agent_event = AsyncMock(side_effect=broadcast_agent_event)

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

            await asyncio.wait_for(broadcast_seen.wait(), timeout=1.0)

            mock_ws_server.broadcast_agent_event.assert_called_once()
            assert broadcast_seen.is_set()
            assert mock_ws_server.broadcast_agent_event.await_args.kwargs["run_id"] == "run-123"
        finally:
            rb._agent_event_callback = old_callback

    @pytest.mark.asyncio
    async def test_broadcast_callback_handles_exception(self):
        """Test that the broadcast callback handles exceptions gracefully."""
        import gobby.runner_broadcasting as rb
        from gobby.runner_broadcasting import fire_agent_event, setup_agent_event_broadcasting

        broadcast_attempted = asyncio.Event()

        async def fail_broadcast(**_kwargs: object) -> None:
            broadcast_attempted.set()
            raise Exception("Broadcast failed")

        mock_ws_server = AsyncMock()
        mock_ws_server.broadcast_agent_event = AsyncMock(side_effect=fail_broadcast)

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
                {"parent_session_id": "sess-456"},
            )

            await asyncio.wait_for(broadcast_attempted.wait(), timeout=1.0)

            assert broadcast_attempted.is_set()
            assert mock_ws_server.broadcast_agent_event.await_count == 1
        finally:
            rb._agent_event_callback = old_callback

    @pytest.mark.asyncio
    async def test_broadcast_callback_handles_cancelled_error(self):
        """Test that the broadcast callback handles CancelledError gracefully."""
        import gobby.runner_broadcasting as rb
        from gobby.runner_broadcasting import fire_agent_event, setup_agent_event_broadcasting

        broadcast_attempted = asyncio.Event()

        async def cancel_broadcast(**_kwargs: object) -> None:
            broadcast_attempted.set()
            raise asyncio.CancelledError

        mock_ws_server = AsyncMock()
        mock_ws_server.broadcast_agent_event = AsyncMock(side_effect=cancel_broadcast)

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
                {},
            )

            await asyncio.wait_for(broadcast_attempted.wait(), timeout=1.0)

            assert broadcast_attempted.is_set()
            assert mock_ws_server.broadcast_agent_event.await_count == 1
        finally:
            rb._agent_event_callback = old_callback

    @pytest.mark.asyncio
    async def test_broadcast_callback_still_works_with_captured_reference(self):
        """Test callback uses the captured websocket_server reference from setup time."""
        import gobby.runner_broadcasting as rb
        from gobby.runner_broadcasting import fire_agent_event, setup_agent_event_broadcasting

        broadcast_seen = asyncio.Event()

        async def broadcast_agent_event(**_kwargs: object) -> None:
            broadcast_seen.set()

        mock_ws_server = AsyncMock()
        mock_ws_server.broadcast_agent_event = AsyncMock(side_effect=broadcast_agent_event)

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
                {"parent_session_id": "sess-456"},
            )

            await asyncio.wait_for(broadcast_seen.wait(), timeout=1.0)

            mock_ws_server.broadcast_agent_event.assert_called_once()
            assert broadcast_seen.is_set()
            assert (
                mock_ws_server.broadcast_agent_event.await_args.kwargs["parent_session_id"]
                == "sess-456"
            )
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
            patch(
                "gobby.runner_init.services.SessionMessageProcessor",
                return_value=mock_message_processor,
            )
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            assert runner.message_processor is not None
            assert runner.message_processor.websocket_server == mock_ws_server


class TestShutdownLoop:
    """Tests for the shutdown waiting loop."""

    @pytest.mark.asyncio
    async def test_run_waits_for_shutdown_signal(self, mock_config):
        """Test that run waits for shutdown signal in the main loop."""
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
                    main_loop_slept = False

                    async def trigger_shutdown(_seconds: float) -> None:
                        nonlocal main_loop_slept
                        main_loop_slept = True
                        runner._shutdown_requested = True

                    with patch(
                        "gobby.runner_lifecycle.asyncio.sleep",
                        side_effect=trigger_shutdown,
                    ):
                        await asyncio.wait_for(runner.run(), timeout=5.0)

                    assert main_loop_slept is True
                    assert runner._shutdown_requested is True


class TestMetricsCleanupLoopDetailed:
    """Detailed tests for the metrics cleanup loop."""

    @pytest.mark.asyncio
    async def test_metrics_cleanup_loop_performs_cleanup_after_sleep(self, mock_config):
        """Test that metrics cleanup loop performs cleanup after sleep interval."""
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

            intervals: list[float] = []

            async def complete_first_cycle(seconds: float) -> None:
                nonlocal shutdown_requested
                intervals.append(seconds)
                shutdown_requested = True

            task = asyncio.create_task(
                metrics_cleanup_loop(
                    runner.metrics_manager,
                    is_shutdown,
                    interval_seconds=1,
                    sleep=complete_first_cycle,
                )
            )
            await asyncio.wait_for(task, timeout=2.0)

            assert intervals == [1]
            assert cleanup_call_count == 1

    @pytest.mark.asyncio
    async def test_metrics_cleanup_loop_logs_deleted_entries(
        self, mock_config, caplog, enable_log_propagation
    ):
        """Test that metrics cleanup loop logs when entries are deleted."""
        from gobby.runner_maintenance import metrics_cleanup_loop

        caplog.set_level(logging.INFO, logger="gobby.runner_maintenance")
        patches = create_base_patches(mock_config=mock_config)

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            runner.metrics_manager.cleanup_old_metrics = MagicMock(return_value=10)

            shutdown_requested = False

            def is_shutdown():
                return shutdown_requested

            intervals: list[float] = []

            async def complete_first_cycle(seconds: float) -> None:
                nonlocal shutdown_requested
                intervals.append(seconds)
                shutdown_requested = True

            task = asyncio.create_task(
                metrics_cleanup_loop(
                    runner.metrics_manager,
                    is_shutdown,
                    interval_seconds=1,
                    sleep=complete_first_cycle,
                )
            )
            await asyncio.wait_for(task, timeout=2.0)

            assert intervals == [1]
            assert "Periodic metrics cleanup: removed 10 old entries" in caplog.text

    @pytest.mark.asyncio
    async def test_metrics_cleanup_loop_continues_on_error(self, mock_config):
        """Test that metrics cleanup loop continues after an error."""
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

            iteration = 0
            intervals: list[float] = []

            async def complete_after_retry(seconds: float) -> None:
                nonlocal iteration, shutdown_requested
                intervals.append(seconds)
                iteration += 1
                if iteration >= 2:
                    shutdown_requested = True

            task = asyncio.create_task(
                metrics_cleanup_loop(
                    runner.metrics_manager,
                    is_shutdown,
                    interval_seconds=1,
                    sleep=complete_after_retry,
                )
            )
            await asyncio.wait_for(task, timeout=2.0)

            assert intervals == [1, 1]
            assert call_count == 2


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
        assert mock_asyncio_run.call_count == 1

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


class TestAgentRestartRecoveryHelpers:
    """Tests for agent restart/shutdown recovery helpers."""

    @pytest.mark.asyncio
    async def test_recover_agent_runs_after_restart_rehydrates_completion_event(self) -> None:
        run = SimpleNamespace(id="run-1", continuation_prompt="Check the agent result")
        runner = SimpleNamespace(
            agent_runner=SimpleNamespace(
                run_storage=SimpleNamespace(list_active=MagicMock(return_value=[run]))
            ),
            pipeline_execution_manager=SimpleNamespace(
                get_completion_subscribers=MagicMock(return_value=["sess-1"]),
            ),
            completion_registry=SimpleNamespace(
                is_registered=MagicMock(return_value=False),
                register=MagicMock(),
            ),
        )

        recovered = await runner_lifecycle._recover_agent_runs_after_restart(runner)

        assert recovered == 1
        assert runner.completion_registry.register.call_count == 1
        runner.agent_runner.run_storage.list_active.assert_called_once_with(limit=500)
        runner.completion_registry.register.assert_called_once_with(
            "run-1",
            subscribers=["sess-1"],
            continuation_prompt="Check the agent result",
        )

    @pytest.mark.asyncio
    async def test_rehydrated_agent_completion_event_fires_on_later_notify(self) -> None:
        from gobby.events.completion_registry import CompletionEventRegistry

        registry = CompletionEventRegistry()
        run = SimpleNamespace(id="run-1", continuation_prompt=None)
        runner = SimpleNamespace(
            agent_runner=SimpleNamespace(
                run_storage=SimpleNamespace(list_active=MagicMock(return_value=[run]))
            ),
            pipeline_execution_manager=SimpleNamespace(
                get_completion_subscribers=MagicMock(return_value=["sess-1"]),
            ),
            completion_registry=registry,
        )

        rehydrated = await runner_lifecycle._recover_agent_runs_after_restart(runner)
        waiter = asyncio.create_task(registry.wait("run-1", timeout=1.0))

        await registry.notify("run-1", {"status": "success", "run_id": "run-1"})

        assert rehydrated == 1
        assert await waiter == {"status": "success", "run_id": "run-1"}

    @pytest.mark.asyncio
    async def test_cancel_active_agent_runs_for_shutdown_kills_and_cancels(self) -> None:
        run = SimpleNamespace(id="run-1")
        runner = SimpleNamespace(
            agent_lifecycle_monitor=SimpleNamespace(
                terminalize_cancelled_run=AsyncMock(return_value=True)
            ),
            agent_runner=SimpleNamespace(
                run_storage=SimpleNamespace(list_active=MagicMock(return_value=[run]))
            ),
            pipeline_execution_manager=SimpleNamespace(
                get_completion_subscribers=MagicMock(return_value=["sess-1"]),
                remove_completion_subscribers=MagicMock(),
            ),
            completion_registry=SimpleNamespace(register=MagicMock(), cleanup=MagicMock()),
            database=MagicMock(),
        )

        with patch(
            "gobby.agents.kill.kill_agent",
            new_callable=AsyncMock,
            return_value={"success": True},
        ) as mock_kill:
            cancelled = await runner_lifecycle._cancel_active_agent_runs_for_shutdown(runner)

        assert cancelled == 1
        assert runner.agent_lifecycle_monitor.terminalize_cancelled_run.await_count == 1
        mock_kill.assert_awaited_once_with(
            run,
            runner.database,
            signal_name="TERM",
            close_terminal=True,
        )
        runner.agent_lifecycle_monitor.terminalize_cancelled_run.assert_awaited_once_with(
            "run-1",
            terminal_reason="daemon_stop",
        )
        runner.pipeline_execution_manager.remove_completion_subscribers.assert_not_called()
        runner.completion_registry.cleanup.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_shutdown_policy_cancels_active_agents(self) -> None:
        runner = SimpleNamespace(
            agent_lifecycle_monitor=SimpleNamespace(stop=AsyncMock()),
            cron_scheduler=None,
            message_processor=None,
            communications_manager=None,
        )
        cancel_active = AsyncMock(return_value=2)

        await runner_lifecycle_shutdown._stop_started_services(
            runner,
            cancel_active,
            shutdown_intent=ShutdownIntent.STOP,
        )

        cancel_active.assert_awaited_once_with(runner)
        runner.agent_lifecycle_monitor.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_restart_shutdown_policy_preserves_active_agents(self) -> None:
        runner = SimpleNamespace(
            agent_lifecycle_monitor=SimpleNamespace(stop=AsyncMock()),
            cron_scheduler=None,
            message_processor=None,
            communications_manager=None,
        )
        cancel_active = AsyncMock(return_value=2)

        await runner_lifecycle_shutdown._stop_started_services(
            runner,
            cancel_active,
            shutdown_intent=ShutdownIntent.RESTART,
        )

        cancel_active.assert_not_awaited()
        runner.agent_lifecycle_monitor.stop.assert_awaited_once()
