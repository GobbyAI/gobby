"""Runner lifecycle, maintenance, and entrypoint tests."""

import asyncio
import logging
import os
import signal
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import ExitStack, nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import ANY, AsyncMock, MagicMock, call, patch

import pytest

import gobby.runner_lifecycle as runner_lifecycle
import gobby.runner_lifecycle_agents as runner_lifecycle_agents
import gobby.runner_lifecycle_processes as runner_lifecycle_processes
import gobby.runner_lifecycle_shutdown as runner_lifecycle_shutdown
import gobby.runner_lifecycle_subsystems as runner_lifecycle_subsystems
from gobby.agents.readiness import spawn_readiness_blocker
from gobby.app_context import clear_app_context, get_app_context
from gobby.config.app import DaemonConfig
from gobby.runner import GobbyRunner, main, run_gobby
from gobby.runner_pid_file import FailOpenPidOwnership
from gobby.shutdown_intent import ShutdownIntent
from tests._timing import wait_for_async_condition
from tests.config_runtime_helpers import static_runtime_capture
from tests.runner_helpers import create_base_patches

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("fast_stop_hook_grace_window")]


@pytest.fixture(autouse=True)
def _clear_app_context_between_tests() -> Iterator[None]:
    clear_app_context()
    yield
    clear_app_context()


def _serve_mock_until_should_exit(server: Any) -> AsyncMock:
    server.started = True
    server.should_exit = False

    async def serve() -> None:
        while not server.should_exit:
            await asyncio.sleep(0)

    return AsyncMock(side_effect=serve)


def _runner_with_static_runtime() -> GobbyRunner:
    runner = GobbyRunner()
    runner.config_runtime._bundle = static_runtime_capture(DaemonConfig())()
    return runner


def test_pipeline_heartbeat_without_startup_project_is_cross_project(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from gobby.runner_init.orchestration import _init_pipeline_heartbeat

    runner = SimpleNamespace(
        database=MagicMock(),
        db_executor=SimpleNamespace(run=AsyncMock()),
        task_manager=MagicMock(),
        session_manager=MagicMock(),
        project_id=None,
    )

    with caplog.at_level(logging.INFO):
        heartbeat = _init_pipeline_heartbeat(runner)

    assert heartbeat is not None
    assert heartbeat._execution_manager.project_id is None
    assert "pipeline heartbeat will monitor all projects" in caplog.text
    assert "Failed to initialize pipeline heartbeat maintenance" not in caplog.text


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

            runner = _runner_with_static_runtime()

            async def stop_after_mcp_connect() -> None:
                runner._shutdown_requested = True

            mock_mcp_manager.connect_all.side_effect = stop_after_mcp_connect

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    await runner.run(ownership_resolution=FailOpenPidOwnership("test"))

            mock_mcp_manager.connect_all.assert_called_once()
            mock_mcp_manager.disconnect_all.assert_called_once()
            assert mock_server.capture_signals is nullcontext
            assert runner._shutdown_requested is True
            assert runner.database.close.called is True
            assert get_app_context() is None

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

            runner = _runner_with_static_runtime()
            runner._shutdown_requested = True

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    await runner.run(ownership_resolution=FailOpenPidOwnership("test"))

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

            runner = _runner_with_static_runtime()
            runner._shutdown_requested = True

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = AsyncMock()
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    await runner.run(ownership_resolution=FailOpenPidOwnership("test"))

            assert mock_mcp_manager.disconnect_all.await_count == 1
            assert runner.database.close.called is True

    @pytest.mark.asyncio
    async def test_shutdown_during_subsystem_init_does_not_start_websocket(
        self,
        mock_config_with_websocket,
    ):
        """Test shutdown blocks WebSocket activation while subsystems initialize."""
        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock()
        mock_mcp_manager.disconnect_all = AsyncMock()

        mock_ws_server = AsyncMock()
        mock_ws_server.start = AsyncMock()

        mock_config_with_websocket.databases.qdrant.url = ""
        mock_config_with_websocket.databases.falkordb.password = None
        mock_config_with_websocket.embeddings.api_base = ""
        mock_config_with_websocket.ui.enabled = False

        patches = create_base_patches(
            mock_config=mock_config_with_websocket,
            mock_mcp_manager=mock_mcp_manager,
            mock_ws_server=mock_ws_server,
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = _runner_with_static_runtime()

            async def stop_after_mcp_connect() -> None:
                runner._shutdown_requested = True

            mock_mcp_manager.connect_all.side_effect = stop_after_mcp_connect

            with patch("uvicorn.Config"), patch("uvicorn.Server") as mock_server_cls:
                mock_server = AsyncMock()
                mock_server.serve = _serve_mock_until_should_exit(mock_server)
                mock_server_cls.return_value = mock_server

                with patch("gobby.runner_maintenance.setup_signal_handlers"):
                    await runner.run(ownership_resolution=FailOpenPidOwnership("test"))

            mock_ws_server.start.assert_not_awaited()
            assert runner._shutdown_requested is True
            assert (
                spawn_readiness_blocker(runner.http_server.services)
                == "daemon_shutdown_in_progress"
            )

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

            runner = _runner_with_static_runtime()

            assert runner.http_server.websocket_server == mock_ws_server
            assert mock_http.websocket_server == mock_ws_server

    async def test_websocket_startup_failure_is_reported(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from gobby.runner_lifecycle_startup import StartupTracker
        from gobby.runner_lifecycle_subsystems import _start_websocket_server

        error = OSError("address already in use")
        websocket_server = SimpleNamespace(start=AsyncMock(side_effect=error))
        runner = SimpleNamespace(websocket_server=websocket_server)
        tracker = StartupTracker()

        with caplog.at_level(logging.ERROR, logger="gobby.runner_lifecycle"):
            _start_websocket_server(runner, tracker)
            with pytest.raises(OSError, match="address already in use"):
                await runner._websocket_task

        assert runner._websocket_task.get_name() == "websocket-server"
        assert tracker.steps_scheduled == ["WebSocket server"]
        assert tracker.errors == [
            {"subsystem": "WebSocket server", "error": "address already in use"}
        ]
        assert "WebSocket server startup failed" in caplog.text

    async def test_subsystem_init_failure_finishes_startup_tracker(self) -> None:
        tracker = runner_lifecycle.StartupTracker()

        async def fail_init() -> None:
            raise RuntimeError("init exploded")

        task = asyncio.create_task(fail_init())
        await asyncio.wait({task})

        runner_lifecycle._log_subsystem_init_result(task, tracker)

        assert tracker.to_dict()["done"] is True
        assert tracker.errors == [
            {"subsystem": "Subsystem initialization", "error": "init exploded"}
        ]


class TestInitSubsystems:
    """Tests for subsystem initialization helpers."""

    @pytest.mark.asyncio
    async def test_init_servers_wires_shared_codex_client_to_chat_backends(self) -> None:
        from gobby.ai import (
            AIAdapterStyle,
            AICapability,
            AICapabilityRegistry,
            CapabilityBinding,
            build_daemon_text_generation_service,
        )
        from gobby.app_context import ServiceContainer
        from gobby.config.app import DaemonConfig
        from gobby.runner_init.servers import init_servers

        class FakeCodexClient:
            def __init__(self) -> None:
                self.is_connected = True
                self.start_calls = 0
                self.stop_calls = 0
                self.archived_thread_ids: list[str] = []

            async def start(self) -> None:
                self.start_calls += 1

            async def stop(self) -> None:
                self.stop_calls += 1

            async def start_thread(self, **_kwargs: object) -> SimpleNamespace:
                return SimpleNamespace(id="one-shot-thread")

            async def run_turn(
                self, *_args: object, **_kwargs: object
            ) -> AsyncIterator[dict[str, object]]:
                yield {"type": "item/agentMessage/delta", "delta": "wired"}

            async def archive_thread(self, thread_id: str) -> None:
                self.archived_thread_ids.append(thread_id)

        fake_client = FakeCodexClient()
        http_init: dict[str, object] = {}
        web_chat_init: dict[str, object] = {}

        class FakeHTTPServer:
            def __init__(
                self,
                *,
                services: object,
                startup_config: object,
                port: int,
                test_mode: bool,
                codex_client: object | None,
                bootstrap_config: object,
            ) -> None:
                http_init.update(
                    services=services,
                    startup_config=startup_config,
                    port=port,
                    test_mode=test_mode,
                    codex_client=codex_client,
                )
                self.services = services
                self.codex_client = codex_client
                self._internal_manager = object()
                self.broadcaster = SimpleNamespace(websocket_server=None)

            def set_runner_getter(self, getter: object) -> None:
                self.runner_getter = getter

        class FakeWebChatRuntimeManager:
            def __init__(self, *, codex_client: object | None, **kwargs: object) -> None:
                web_chat_init.update(codex_client=codex_client, kwargs=kwargs)

        config = DaemonConfig(websocket={"enabled": False})
        registry = AICapabilityRegistry(
            [
                CapabilityBinding(
                    capability=AICapability.TEXT_GENERATE,
                    provider="codex",
                    adapter_style=AIAdapterStyle.DAEMON,
                    available=True,
                )
            ]
        )

        class RunnerStub:
            pass

        runner = RunnerStub()
        runner.startup_config = config
        runner.bootstrap_config = config
        runner.codex_client = None
        text_generation_service = build_daemon_text_generation_service(
            config,
            registry=registry,
        )
        runner.text_generation_service = text_generation_service
        runner.database = object()
        runner.db_executor = None
        runner.worktree_delete_executor = None
        runner.coverage_executor = None
        runner.database_concurrency = None
        runner.database_watchdog = None
        runner.session_manager = None
        runner.task_manager = object()
        runner.span_storage = None
        runner.memory_backup_manager = None
        runner.memory_manager = None
        runner.memory_dream_coordinator = None
        runner.llm_service = None
        runner.vector_store = None
        runner.mcp_proxy = None
        runner.mcp_db_manager = None
        runner.metrics_manager = None
        runner.agent_runner = None
        runner.message_processor = None
        runner.task_validator = None
        runner.worktree_storage = None
        runner.clone_storage = None
        runner.git_manager = None
        runner.project_id = "project-1"
        runner.pipeline_executor = None
        runner.workflow_loader = None
        runner.pipeline_execution_manager = None
        runner.completion_registry = None
        runner.wake_dispatcher = SimpleNamespace(set_web_chat_session_registry=MagicMock())
        runner.agent_lifecycle_monitor = None
        runner.attention_manager = None
        runner.detection_registry = None
        runner.communications_manager = None
        runner.code_indexer = None
        runner.cron_storage = None
        runner.cron_scheduler = None
        runner.system_automation_loop = None
        runner.skill_manager = None
        runner.hub_manager = None
        runner.config_store = None
        runner.config_runtime = SimpleNamespace(capture=static_runtime_capture(config))
        runner.prompt_manager = None
        runner.tool_chat_service = None
        runner._dev_mode = False
        capability_service = MagicMock()

        with (
            patch(
                "gobby.adapters.codex_impl.app_server_adapter.CodexAdapter.is_codex_available",
                return_value=True,
            ),
            patch(
                "gobby.adapters.codex_impl.client.CodexAppServerClient", return_value=fake_client
            ),
            patch("gobby.runner_init.servers.HTTPServer", FakeHTTPServer),
            patch("gobby.runner_init.servers.WebChatRuntimeManager", FakeWebChatRuntimeManager),
            patch(
                "gobby.runner_init.servers.CapabilityRefreshCoordinator",
                return_value=capability_service,
            ),
            patch("gobby.runner_init.servers.set_app_context"),
        ):
            init_servers(runner)

        assert runner.codex_client is fake_client
        assert web_chat_init["codex_client"] is fake_client
        assert http_init["codex_client"] is fake_client
        services = cast(ServiceContainer, http_init["services"])
        assert services.text_generation_service is text_generation_service
        assert services.llm_service is None
        assert services.provider_capability_service is capability_service
        capability_service.prepare.assert_called_once_with()
        assert fake_client.start_calls == 0
        assert fake_client.stop_calls == 0
        assert fake_client.archived_thread_ids == []

    @pytest.mark.asyncio
    async def test_init_subsystems_uses_embedding_readiness_helper_and_stays_alive(self) -> None:
        runner = SimpleNamespace(
            http_server=SimpleNamespace(),
            mcp_proxy=SimpleNamespace(connect_all=AsyncMock()),
            config=SimpleNamespace(
                databases=SimpleNamespace(
                    qdrant=SimpleNamespace(url=""),
                    falkordb=SimpleNamespace(password=None),
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
                code_index=SimpleNamespace(enabled=False),
            ),
            config_runtime=SimpleNamespace(
                capture=static_runtime_capture(
                    DaemonConfig(
                        embeddings={
                            "model": "nomic-embed-text",
                            "api_base": "http://localhost:1234/v1",
                            "api_key": "lm-studio",
                            "dim": 768,
                        },
                        code_index={"enabled": False},
                    )
                )
            ),
            startup_config=SimpleNamespace(
                ui=SimpleNamespace(enabled=False, mode="prod", port=5173, host="localhost")
            ),
            agent_runner=None,
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
            patch("gobby.agents.tmux.get_tmux_session_manager") as mock_tmux_manager,
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
            http_server=SimpleNamespace(),
            mcp_proxy=SimpleNamespace(connect_all=AsyncMock()),
            config=SimpleNamespace(
                databases=SimpleNamespace(
                    qdrant=SimpleNamespace(url="http://localhost:6333"),
                    falkordb=SimpleNamespace(password=None),
                ),
                embeddings=SimpleNamespace(model="", api_base="", api_key="", dim=768),
                ui=SimpleNamespace(enabled=False, mode="prod", port=5173, host="localhost"),
                telemetry=SimpleNamespace(log_file="/tmp/gobby.log"),
                bind_host="localhost",
                code_index=SimpleNamespace(enabled=False),
            ),
            config_runtime=SimpleNamespace(
                capture=static_runtime_capture(
                    DaemonConfig(
                        databases={"qdrant": {"url": "http://localhost:6333"}},
                        code_index={"enabled": False},
                    )
                )
            ),
            startup_config=SimpleNamespace(
                ui=SimpleNamespace(enabled=False, mode="prod", port=5173, host="localhost")
            ),
            agent_runner=None,
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
            patch("gobby.agents.tmux.get_tmux_session_manager") as mock_tmux_manager,
        ):
            mock_tmux_manager.return_value.health_check = AsyncMock()
            await runner_lifecycle._init_subsystems(runner, AsyncMock())

        assert runner.vector_store is vector_store
        assert runner.lifecycle_manager.start.await_count == 1
        vector_store.initialize.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_agent_lifecycle_monitor_cleanup_failure_is_non_fatal(
        self,
        caplog: pytest.LogCaptureFixture,
        enable_log_propagation: None,
    ) -> None:
        from gobby.runner_lifecycle_subsystems import _start_agent_lifecycle_monitor

        tracker = runner_lifecycle.StartupTracker()
        monitor = SimpleNamespace(
            set_reconciliation_callback=MagicMock(),
            cleanup_stale_pending_runs=AsyncMock(side_effect=RuntimeError("cleanup failed")),
            start=AsyncMock(),
        )
        runner = SimpleNamespace(agent_lifecycle_monitor=monitor)
        with caplog.at_level(logging.ERROR, logger="gobby.runner_lifecycle"):
            await _start_agent_lifecycle_monitor(
                runner,
                tracker,
            )

        monitor.cleanup_stale_pending_runs.assert_awaited_once()
        monitor.start.assert_awaited_once()
        assert tracker.steps_completed == []
        assert tracker.errors == [
            {
                "subsystem": "Agent lifecycle monitor",
                "error": "cleanup failed: cleanup failed",
            }
        ]
        assert "Agent stale pending cleanup failed during startup" in caplog.text

    @pytest.mark.asyncio
    async def test_agent_lifecycle_monitor_start_failure_fails_closed(
        self,
        caplog: pytest.LogCaptureFixture,
        enable_log_propagation: None,
    ) -> None:
        from gobby.runner_lifecycle_subsystems import _start_agent_lifecycle_monitor

        tracker = runner_lifecycle.StartupTracker()
        monitor = SimpleNamespace(
            set_reconciliation_callback=MagicMock(),
            cleanup_stale_pending_runs=AsyncMock(),
            start=AsyncMock(side_effect=RuntimeError("start failed")),
        )
        runner = SimpleNamespace(agent_lifecycle_monitor=monitor)
        with (
            caplog.at_level(logging.ERROR, logger="gobby.runner_lifecycle"),
            pytest.raises(RuntimeError, match="reconciliation owner"),
        ):
            await _start_agent_lifecycle_monitor(
                runner,
                tracker,
            )

        monitor.start.assert_awaited_once()
        assert tracker.steps_completed == []
        assert tracker.errors == [
            {
                "subsystem": "Agent lifecycle monitor",
                "error": "start failed: start failed",
            }
        ]
        assert "Agent lifecycle monitor start failed during startup" in caplog.text

    async def test_start_failures_do_not_abort_init_and_readiness_is_last(self) -> None:
        events: list[str] = []

        class RecordingServices:
            shutdown_in_progress = False

            def __init__(self) -> None:
                self._startup_ready = False

            @property
            def startup_ready(self) -> bool:
                return self._startup_ready

            @startup_ready.setter
            def startup_ready(self, value: bool) -> None:
                events.append(f"ready:{value}")
                self._startup_ready = value

        class RecordingTracker(runner_lifecycle.StartupTracker):
            def finish(self) -> None:
                events.append("tracker-finish")
                super().finish()

        async def communications_start() -> None:
            events.append("communications-start")

        async def lifecycle_start() -> None:
            events.append("lifecycle-start")
            raise RuntimeError("lifecycle failed")

        async def cron_start() -> None:
            events.append("cron-start")
            raise RuntimeError("cron failed")

        async def automation_start() -> None:
            events.append("automation-start")

        async def recover_pipelines(*_args: object) -> None:
            events.append("recover-pipelines")

        services = RecordingServices()
        tracker = RecordingTracker()
        runner = SimpleNamespace(
            config=SimpleNamespace(code_index=SimpleNamespace(enabled=False)),
            config_runtime=SimpleNamespace(
                capture=static_runtime_capture(DaemonConfig(code_index={"enabled": False}))
            ),
            http_server=SimpleNamespace(services=services),
            message_processor=SimpleNamespace(start=AsyncMock()),
            agent_runner=None,
            communications_manager=SimpleNamespace(
                start=AsyncMock(side_effect=communications_start)
            ),
            lifecycle_manager=SimpleNamespace(start=AsyncMock(side_effect=lifecycle_start)),
            cron_scheduler=SimpleNamespace(start=AsyncMock(side_effect=cron_start)),
            cron_storage=None,
            system_automation_loop=SimpleNamespace(start=AsyncMock(side_effect=automation_start)),
        )
        async_noop = AsyncMock()

        with (
            patch.object(runner_lifecycle_subsystems, "_connect_mcp_servers", async_noop),
            patch.object(runner_lifecycle_subsystems, "_check_embedding_service", async_noop),
            patch.object(runner_lifecycle_subsystems, "_cleanup_metrics_on_startup"),
            patch.object(runner_lifecycle_subsystems, "_initialize_vector_store", async_noop),
            patch.object(runner_lifecycle_subsystems, "_check_tmux_health", async_noop),
            patch.object(
                runner_lifecycle_subsystems,
                "_start_agent_lifecycle_monitor",
                async_noop,
            ),
            patch.object(runner_lifecycle_subsystems, "_register_wiki_cron_handlers", async_noop),
            patch.object(
                runner_lifecycle_subsystems,
                "_start_code_index_tasks",
                side_effect=lambda *_args: events.append("code-index"),
            ),
            patch.object(
                runner_lifecycle_subsystems,
                "_recover_pipelines",
                side_effect=recover_pipelines,
            ),
            patch.object(
                runner_lifecycle_subsystems,
                "_start_websocket_server",
                side_effect=lambda *_args: events.append("websocket"),
            ),
            patch.object(
                runner_lifecycle_subsystems,
                "_maybe_start_ui_dev_server",
                side_effect=lambda *_args: events.append("ui"),
            ),
        ):
            await runner_lifecycle_subsystems.init_subsystems(runner, AsyncMock(), tracker)

        assert events == [
            "communications-start",
            "lifecycle-start",
            "cron-start",
            "code-index",
            "recover-pipelines",
            "websocket",
            "ui",
            "automation-start",
            "tracker-finish",
            "ready:True",
        ]
        runner.message_processor.start.assert_not_awaited()
        assert tracker.errors == [
            {"subsystem": "Session lifecycle manager", "error": "lifecycle failed"},
            {"subsystem": "Cron scheduler", "error": "cron failed"},
        ]
        assert tracker.steps_completed == [
            "Communications manager",
            "System automation loop",
        ]
        assert tracker.done is True
        assert services.startup_ready is True

    async def test_cleanup_stale_expansion_runs_on_startup_uses_db_executor(
        self, temp_db, sample_project
    ) -> None:
        from datetime import timedelta

        from gobby.storage.expansion_runs import LocalExpansionRunManager
        from gobby.storage.tasks import LocalTaskManager
        from gobby.utils.datetime import utc_now

        task_manager = LocalTaskManager(temp_db)
        task = task_manager.create_task(
            project_id=sample_project["id"],
            title="Expand me",
            validation_criteria="Expansion run cleanup completes.",
        )
        run_manager = LocalExpansionRunManager(temp_db)
        run = run_manager.create(
            parent_task_id=task.id,
            project_id=task.project_id,
            triggering_session_id=None,
            input_source="task",
        )
        temp_db.execute(
            "UPDATE expansion_runs SET status = 'running', updated_at = %s WHERE id = %s",
            (utc_now() - timedelta(minutes=31), run.id),
        )
        run_db = AsyncMock(side_effect=lambda operation: operation())
        runner = SimpleNamespace(
            db_executor=SimpleNamespace(run=run_db),
            http_server=SimpleNamespace(services=SimpleNamespace(task_manager=task_manager)),
        )

        cleaned = await runner_lifecycle_subsystems._cleanup_stale_expansion_runs_on_startup(runner)

        assert cleaned == 1
        assert run_manager.get(run.id).status == "failed"
        run_db.assert_awaited_once()

    async def test_automation_start_failure_is_tracked_without_raising(self) -> None:
        tracker = runner_lifecycle.StartupTracker()
        runner = SimpleNamespace(
            system_automation_loop=SimpleNamespace(
                start=AsyncMock(side_effect=RuntimeError("automation failed"))
            )
        )

        await runner_lifecycle_subsystems._start_system_automation_loop(runner, tracker)

        assert tracker.steps_completed == []
        assert tracker.errors == [
            {"subsystem": "System automation loop", "error": "automation failed"}
        ]


class TestShutdownDaemonServices:
    @staticmethod
    def _minimal_shutdown_runner(intent: ShutdownIntent) -> SimpleNamespace:
        return SimpleNamespace(
            _shutdown_intent=intent,
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
            startup_config=SimpleNamespace(ui=SimpleNamespace(enabled=False, mode="production")),
            memory_manager=None,
            vector_store=None,
            mcp_proxy=SimpleNamespace(disconnect_all=AsyncMock()),
            database=SimpleNamespace(close=MagicMock()),
        )

    @pytest.mark.asyncio
    async def test_late_subsystem_init_cannot_activate_after_shutdown_starts(self) -> None:
        services = SimpleNamespace(startup_ready=False, shutdown_in_progress=False)
        runner = SimpleNamespace(
            config=SimpleNamespace(code_index=SimpleNamespace(enabled=False)),
            config_runtime=SimpleNamespace(
                capture=static_runtime_capture(DaemonConfig(code_index={"enabled": False}))
            ),
            http_server=SimpleNamespace(services=services),
        )

        async def begin_shutdown_during_pipeline_recovery(
            _runner: object,
            _tracker: object,
        ) -> None:
            services.shutdown_in_progress = True

        start_websocket = MagicMock()
        start_ui = MagicMock()
        start_automation = AsyncMock()
        async_noop = AsyncMock()

        with (
            patch.object(runner_lifecycle_subsystems, "_connect_mcp_servers", async_noop),
            patch.object(runner_lifecycle_subsystems, "_check_embedding_service", async_noop),
            patch.object(runner_lifecycle_subsystems, "_cleanup_metrics_on_startup"),
            patch.object(runner_lifecycle_subsystems, "_initialize_vector_store", async_noop),
            patch.object(runner_lifecycle_subsystems, "_start_core_services", async_noop),
            patch.object(runner_lifecycle_subsystems, "_check_tmux_health", async_noop),
            patch.object(
                runner_lifecycle_subsystems,
                "_start_agent_lifecycle_monitor",
                async_noop,
            ),
            patch.object(runner_lifecycle_subsystems, "_start_cron_scheduler", async_noop),
            patch.object(runner_lifecycle_subsystems, "_start_code_index_tasks"),
            patch.object(
                runner_lifecycle_subsystems,
                "_recover_pipelines",
                side_effect=begin_shutdown_during_pipeline_recovery,
            ),
            patch.object(
                runner_lifecycle_subsystems,
                "_start_websocket_server",
                start_websocket,
            ),
            patch.object(runner_lifecycle_subsystems, "_maybe_start_ui_dev_server", start_ui),
            patch.object(
                runner_lifecycle_subsystems,
                "_start_system_automation_loop",
                start_automation,
            ),
        ):
            await runner_lifecycle_subsystems.init_subsystems(runner, AsyncMock(), None)

        assert spawn_readiness_blocker(services) == "daemon_shutdown_in_progress"
        assert services.startup_ready is False
        assert services.shutdown_in_progress is True
        start_websocket.assert_not_called()
        start_ui.assert_not_called()
        start_automation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_shutdown_cancels_subsystem_init_before_grace_and_keeps_spawn_blocked(
        self,
    ) -> None:
        runner = self._minimal_shutdown_runner(ShutdownIntent.STOP)
        runner.http_server.services.startup_ready = False
        init_blocked = asyncio.Event()
        events: list[str] = []

        async def blocked_connect(_runner: object, _tracker: object) -> None:
            events.append("init-blocked")
            init_blocked.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                events.append("init-cancelled")
                raise
            finally:
                events.append("init-cleanup")

        async def grace_window() -> None:
            events.append("grace")
            assert runner._subsystem_init_task.cancelled()
            assert (
                spawn_readiness_blocker(runner.http_server.services)
                == "daemon_shutdown_in_progress"
            )

        start_core_services = AsyncMock()
        start_websocket = MagicMock()
        start_automation = AsyncMock()
        server = SimpleNamespace(should_exit=False)

        async def completed_server() -> None:
            return None

        with (
            patch.object(
                runner_lifecycle_subsystems,
                "_connect_mcp_servers",
                side_effect=blocked_connect,
            ),
            patch.object(
                runner_lifecycle_subsystems,
                "_start_core_services",
                start_core_services,
            ),
            patch.object(
                runner_lifecycle_subsystems,
                "_start_websocket_server",
                start_websocket,
            ),
            patch.object(
                runner_lifecycle_subsystems,
                "_start_system_automation_loop",
                start_automation,
            ),
        ):
            runner._subsystem_init_task = asyncio.create_task(
                runner_lifecycle_subsystems.init_subsystems(runner, AsyncMock(), None)
            )
            await init_blocked.wait()
            await runner_lifecycle_shutdown.shutdown_daemon_services(
                runner,
                server,
                asyncio.create_task(completed_server()),
                1,
                await_critical_stop_hook_grace_window=grace_window,
                shutdown_websocket_server=AsyncMock(),
                reap_remaining_child_processes=AsyncMock(),
                shutdown_telemetry=MagicMock(),
                cleanup_pid_file=MagicMock(),
            )

        assert events == ["init-blocked", "init-cancelled", "init-cleanup", "grace"]
        assert spawn_readiness_blocker(runner.http_server.services) == (
            "daemon_shutdown_in_progress"
        )
        assert runner.http_server.services.startup_ready is False
        assert runner.http_server.services.shutdown_in_progress is True
        start_core_services.assert_not_awaited()
        start_websocket.assert_not_called()
        start_automation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_subsystem_task_cancellation_does_not_swallow_shutdown_cancellation(
        self,
    ) -> None:
        init_cancelled = asyncio.Event()

        async def stubborn_init() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                init_cancelled.set()
                await asyncio.Event().wait()

        subsystem_task = asyncio.create_task(stubborn_init())
        runner = SimpleNamespace(_subsystem_init_task=subsystem_task)
        shutdown_task = asyncio.create_task(
            runner_lifecycle_shutdown._cancel_runner_task(
                runner,
                "_subsystem_init_task",
                timeout=10.0,
            )
        )
        await init_cancelled.wait()
        shutdown_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await shutdown_task
        assert subsystem_task.cancelled()

    @pytest.mark.asyncio
    async def test_shutdown_intent_marker_is_removed_after_cleanup(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        marker = tmp_path / "shutdown_intent_active.json"
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

        async def completed_server() -> None:
            return None

        server_task: asyncio.Task[None] = asyncio.create_task(completed_server())

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
            reap_remaining_child_processes=AsyncMock(),
            shutdown_telemetry=MagicMock(),
            cleanup_pid_file=cleanup_pid_file,
        )

        assert cleanup_saw_marker is True
        assert marker.exists() is False

    @pytest.mark.asyncio
    async def test_periodic_tasks_cancel_concurrently(self) -> None:
        all_cancelled = asyncio.Event()
        all_started = asyncio.Event()
        cancellation_count = 0
        started_count = 0

        async def wait_for_peer_cancellations() -> None:
            nonlocal cancellation_count, started_count
            started_count += 1
            if started_count == 3:
                all_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_count += 1
                if cancellation_count == 3:
                    all_cancelled.set()
                await all_cancelled.wait()
                raise

        tasks = [asyncio.create_task(wait_for_peer_cancellations()) for _ in range(3)]
        runner = SimpleNamespace(
            _metrics_cleanup_task=tasks[0],
            _metrics_archive_task=tasks[1],
            _span_cleanup_task=tasks[2],
        )
        await asyncio.wait_for(all_started.wait(), timeout=0.5)

        await asyncio.wait_for(
            runner_lifecycle_shutdown._cancel_periodic_tasks(runner),
            timeout=0.5,
        )

        assert cancellation_count == 3
        assert all(task.cancelled() for task in tasks)

    @pytest.mark.asyncio
    async def test_restart_skips_stop_hook_grace_and_preserves_marker(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        marker = tmp_path / "shutdown_intent_active.json"
        marker.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            runner_lifecycle_shutdown,
            "get_shutdown_marker_path",
            lambda: marker,
        )
        runner = self._minimal_shutdown_runner(ShutdownIntent.RESTART)
        server = SimpleNamespace(should_exit=False)
        grace_window = AsyncMock()

        async def completed_server() -> None:
            return None

        await runner_lifecycle_shutdown.shutdown_daemon_services(
            runner,
            server,
            asyncio.create_task(completed_server()),
            1,
            await_critical_stop_hook_grace_window=grace_window,
            shutdown_websocket_server=AsyncMock(),
            reap_remaining_child_processes=AsyncMock(),
            shutdown_telemetry=MagicMock(),
            cleanup_pid_file=MagicMock(),
        )

        grace_window.assert_not_awaited()
        assert server.should_exit is True
        assert marker.exists() is True

    @pytest.mark.asyncio
    async def test_shutdown_marker_is_removed_when_pid_cleanup_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        marker = tmp_path / "shutdown_intent_active.json"
        marker.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            runner_lifecycle_shutdown,
            "get_shutdown_marker_path",
            lambda: marker,
        )
        runner = self._minimal_shutdown_runner(ShutdownIntent.STOP)
        server = SimpleNamespace(should_exit=False)

        async def completed_server() -> None:
            return None

        def fail_pid_cleanup() -> None:
            raise OSError("pid file is busy")

        caplog.set_level(logging.WARNING, logger="gobby.runner_lifecycle")
        await runner_lifecycle_shutdown.shutdown_daemon_services(
            runner,
            server,
            asyncio.create_task(completed_server()),
            1,
            await_critical_stop_hook_grace_window=AsyncMock(),
            shutdown_websocket_server=AsyncMock(),
            reap_remaining_child_processes=AsyncMock(),
            shutdown_telemetry=MagicMock(),
            cleanup_pid_file=fail_pid_cleanup,
        )

        assert "PID file cleanup failed: pid file is busy" in caplog.text
        assert marker.exists() is False

    @pytest.mark.asyncio
    async def test_degraded_shutdown_deadline_preserves_cleanup_tail(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        marker = tmp_path / "shutdown_intent_active.json"
        marker.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            runner_lifecycle_shutdown,
            "get_shutdown_marker_path",
            lambda: marker,
        )
        monkeypatch.setattr(
            runner_lifecycle_shutdown,
            "_GRACEFUL_SHUTDOWN_BUDGET_SECONDS",
            0.05,
        )
        monkeypatch.setattr(
            runner_lifecycle_shutdown,
            "_OVERALL_SHUTDOWN_DEADLINE_SECONDS",
            0.15,
        )
        runner = self._minimal_shutdown_runner(ShutdownIntent.STOP)

        async def degraded_stop() -> None:
            await asyncio.Event().wait()

        runner.lifecycle_manager.stop = degraded_stop
        runner.agent_lifecycle_monitor = SimpleNamespace(stop=degraded_stop)
        runner.cron_scheduler = SimpleNamespace(stop=degraded_stop)
        runner.message_processor = SimpleNamespace(stop=degraded_stop)
        cleanup_pid_file = MagicMock()
        shutdown_telemetry = MagicMock()
        reap_remaining_child_processes = AsyncMock()
        server = SimpleNamespace(should_exit=False)

        async def completed_server() -> None:
            return None

        caplog.set_level(logging.WARNING, logger="gobby.runner_lifecycle")
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        await asyncio.wait_for(
            runner_lifecycle_shutdown.shutdown_daemon_services(
                runner,
                server,
                asyncio.create_task(completed_server()),
                1,
                await_critical_stop_hook_grace_window=AsyncMock(),
                shutdown_websocket_server=AsyncMock(),
                reap_remaining_child_processes=reap_remaining_child_processes,
                shutdown_telemetry=shutdown_telemetry,
                cleanup_pid_file=cleanup_pid_file,
            ),
            timeout=0.5,
        )
        elapsed = loop.time() - started_at

        assert elapsed < 0.5
        assert "Graceful shutdown exceeded 0.1s budget" in caplog.text
        reap_remaining_child_processes.assert_awaited_once_with(
            preserve_agents=True,
            preserved_agent_pids=set(),
        )
        shutdown_telemetry.assert_called_once_with()
        runner.database.close.assert_called_once_with()
        cleanup_pid_file.assert_called_once_with()
        assert marker.exists() is False

    @pytest.mark.asyncio
    async def test_mid_sequence_exception_preserves_full_cleanup_tail(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        marker = tmp_path / "shutdown_intent_active.json"
        marker.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            runner_lifecycle_shutdown,
            "get_shutdown_marker_path",
            lambda: marker,
        )
        events: list[str] = []
        runner = self._minimal_shutdown_runner(ShutdownIntent.STOP)

        async def fail_cron_stop() -> None:
            events.append("cron")
            raise ValueError("injected cron stop failure")

        async def stop_message_processor() -> None:
            events.append("message-processor")

        async def disconnect_mcp() -> None:
            events.append("mcp")

        async def reap_children(**_kwargs: object) -> None:
            events.append("reap")

        def shutdown_executor(*, cancel_futures: bool = True) -> None:
            assert cancel_futures is True
            events.append("db-executor")

        executor_state = {"joined": False}

        def join_executor() -> None:
            executor_state["joined"] = True

        runner.cron_scheduler = SimpleNamespace(stop=fail_cron_stop)
        runner.message_processor = SimpleNamespace(stop=stop_message_processor)
        runner.mcp_proxy.disconnect_all = disconnect_mcp
        runner.db_executor = SimpleNamespace(
            shutdown=shutdown_executor,
            join=join_executor,
            is_joined=lambda: executor_state["joined"],
        )
        runner.database.close.side_effect = lambda: events.append("database")
        server = SimpleNamespace(should_exit=False)

        async def completed_server() -> None:
            return None

        caplog.set_level(logging.WARNING, logger="gobby.runner_lifecycle")
        await runner_lifecycle_shutdown.shutdown_daemon_services(
            runner,
            server,
            asyncio.create_task(completed_server()),
            1,
            await_critical_stop_hook_grace_window=AsyncMock(),
            shutdown_websocket_server=AsyncMock(),
            reap_remaining_child_processes=reap_children,
            shutdown_telemetry=lambda: events.append("telemetry"),
            cleanup_pid_file=lambda: events.append("pid"),
        )

        assert "Cron scheduler shutdown failed: injected cron stop failure" in caplog.text
        assert events == [
            "cron",
            "message-processor",
            "mcp",
            "reap",
            "telemetry",
            "db-executor",
            "database",
            "pid",
        ]
        assert marker.exists() is False

    @pytest.mark.asyncio
    async def test_shutdown_cancellation_propagates_after_sync_finalizers(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        marker = tmp_path / "shutdown_intent_active.json"
        marker.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            runner_lifecycle_shutdown,
            "get_shutdown_marker_path",
            lambda: marker,
        )
        runner = self._minimal_shutdown_runner(ShutdownIntent.STOP)
        server = SimpleNamespace(should_exit=False)
        shutdown_telemetry = MagicMock()
        cleanup_pid_file = MagicMock()
        reap_remaining_child_processes = AsyncMock()

        async def completed_server() -> None:
            return None

        with pytest.raises(asyncio.CancelledError):
            await runner_lifecycle_shutdown.shutdown_daemon_services(
                runner,
                server,
                asyncio.create_task(completed_server()),
                1,
                await_critical_stop_hook_grace_window=AsyncMock(
                    side_effect=asyncio.CancelledError()
                ),
                shutdown_websocket_server=AsyncMock(),
                reap_remaining_child_processes=reap_remaining_child_processes,
                shutdown_telemetry=shutdown_telemetry,
                cleanup_pid_file=cleanup_pid_file,
            )

        reap_remaining_child_processes.assert_not_awaited()
        shutdown_telemetry.assert_not_called()
        runner.database.close.assert_called_once_with()
        cleanup_pid_file.assert_called_once_with()
        assert marker.exists() is False

    @pytest.mark.asyncio
    async def test_db_executor_shutdown_cancels_queued_work_without_default_executor(
        self,
    ) -> None:
        shutdown_calls: list[bool] = []
        join_calls = 0

        def shutdown_executor(*, cancel_futures: bool = True) -> None:
            shutdown_calls.append(cancel_futures)

        def join_executor() -> None:
            nonlocal join_calls
            join_calls += 1

        await runner_lifecycle_shutdown._shutdown_database_executor(
            SimpleNamespace(
                shutdown=shutdown_executor,
                join=join_executor,
                is_joined=lambda: False,
            )
        )

        assert shutdown_calls == [True]
        assert join_calls == 1

    @pytest.mark.asyncio
    async def test_worktree_delete_executor_drains_before_database_executor(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        worktree_delete_executor = object()
        coverage_executor = object()
        db_executor = object()
        shutdown_calls: list[tuple[object, dict[str, object]]] = []

        async def record_shutdown(executor: object, **kwargs: object) -> None:
            shutdown_calls.append((executor, kwargs))

        monkeypatch.setattr(
            runner_lifecycle_shutdown,
            "_shutdown_database_executor",
            record_shutdown,
        )

        await runner_lifecycle_shutdown._shutdown_database_concurrency(
            cast(
                GobbyRunner,
                SimpleNamespace(
                    database_watchdog=None,
                    worktree_delete_executor=worktree_delete_executor,
                    coverage_executor=coverage_executor,
                    db_executor=db_executor,
                ),
            )
        )

        assert [executor for executor, _ in shutdown_calls] == [
            worktree_delete_executor,
            coverage_executor,
            db_executor,
        ]
        assert shutdown_calls[0][1]["join_timeout_seconds"] is None

    async def test_overall_deadline_still_drains_active_worktree_delete_before_database_close(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import threading

        from gobby.worktrees.executor import DestructiveBoundary, WorktreeDeleteExecutor

        monkeypatch.setattr(
            runner_lifecycle_shutdown,
            "_OVERALL_SHUTDOWN_DEADLINE_SECONDS",
            0.05,
        )
        worker_started = threading.Event()
        release_worker = threading.Event()
        deadline_expired = asyncio.Event()

        async def wait_for_overall_deadline(*_args: object, **_kwargs: object) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                deadline_expired.set()
                raise

        monkeypatch.setattr(
            runner_lifecycle_shutdown,
            "_run_async_shutdown_cleanup",
            wait_for_overall_deadline,
        )

        def blocked_delete(boundary: DestructiveBoundary) -> None:
            assert boundary.begin_mutation()
            worker_started.set()
            release_worker.wait()

        runner = self._minimal_shutdown_runner(ShutdownIntent.STOP)
        runner.worktree_delete_executor = WorktreeDeleteExecutor(max_workers=1)
        server = SimpleNamespace(should_exit=False)

        async def completed_server() -> None:
            return None

        delete_task = asyncio.create_task(
            runner.worktree_delete_executor.run_delete(blocked_delete)
        )
        try:
            await asyncio.wait_for(asyncio.to_thread(worker_started.wait), timeout=1.0)
            shutdown_task = asyncio.create_task(
                runner_lifecycle_shutdown.shutdown_daemon_services(
                    runner,
                    server,
                    asyncio.create_task(completed_server()),
                    1,
                    await_critical_stop_hook_grace_window=AsyncMock(),
                    shutdown_websocket_server=AsyncMock(),
                    reap_remaining_child_processes=AsyncMock(),
                    shutdown_telemetry=MagicMock(),
                    cleanup_pid_file=MagicMock(),
                )
            )
            await wait_for_async_condition(lambda: runner.worktree_delete_executor.stats().shutdown)
            await asyncio.wait_for(deadline_expired.wait(), timeout=1.0)
            assert shutdown_task.done() is False
            runner.database.close.assert_not_called()

            release_worker.set()
            await asyncio.wait_for(shutdown_task, timeout=1.0)
            await asyncio.wait_for(delete_task, timeout=1.0)

            runner.database.close.assert_called_once_with()
            assert runner.worktree_delete_executor.is_joined() is True
        finally:
            release_worker.set()

    @pytest.mark.asyncio
    async def test_hung_db_call_does_not_block_database_close(self) -> None:
        import threading

        from gobby.storage.executor import DatabaseExecutor

        worker_started = threading.Event()
        release_worker = threading.Event()

        def blocked_db_call() -> None:
            worker_started.set()
            release_worker.wait()

        runner = self._minimal_shutdown_runner(ShutdownIntent.STOP)
        runner.db_executor = DatabaseExecutor(max_workers=1)
        server = SimpleNamespace(should_exit=False)

        async def completed_server() -> None:
            return None

        db_call = asyncio.create_task(runner.db_executor.run(blocked_db_call))
        try:
            await asyncio.wait_for(asyncio.to_thread(worker_started.wait), timeout=1.0)

            shutdown_task = asyncio.create_task(
                runner_lifecycle_shutdown.shutdown_daemon_services(
                    runner,
                    server,
                    asyncio.create_task(completed_server()),
                    1,
                    await_critical_stop_hook_grace_window=AsyncMock(),
                    shutdown_websocket_server=AsyncMock(),
                    reap_remaining_child_processes=AsyncMock(),
                    shutdown_telemetry=MagicMock(),
                    cleanup_pid_file=MagicMock(),
                )
            )
            await wait_for_async_condition(lambda: runner.db_executor.stats().shutdown)
            runner.database.close.assert_not_called()
            release_worker.set()
            await asyncio.wait_for(shutdown_task, timeout=1.0)

            runner.database.close.assert_called_once_with()
            executor_stats = runner.db_executor.stats()
            assert executor_stats.shutdown is True
            assert executor_stats.active == 0
            assert server.should_exit is True
            assert release_worker.is_set() is True
        finally:
            release_worker.set()
            await asyncio.wait_for(db_call, timeout=1.0)

    def test_db_executor_shutdown_does_not_strand_asyncio_run(self) -> None:
        import subprocess
        import sys
        import textwrap

        script = textwrap.dedent(
            """
            import asyncio
            import threading

            import gobby.runner_lifecycle_shutdown as shutdown

            class BlockingExecutor:
                def is_joined(self) -> bool:
                    return False

                def shutdown(self, *, cancel_futures: bool = True) -> None:
                    assert cancel_futures is True

                def join(self) -> None:
                    threading.Event().wait()

            shutdown._DATABASE_EXECUTOR_JOIN_SECONDS = 0.01
            asyncio.run(shutdown._shutdown_database_executor(BlockingExecutor()))
            """
        )

        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            check=False,
            text=True,
            timeout=15.0,
        )

        assert completed.returncode == 0, completed.stderr

    @pytest.mark.asyncio
    async def test_communications_stop_before_websocket_and_workflow_runtime_shutdown(
        self,
    ) -> None:
        runner = self._minimal_shutdown_runner(ShutdownIntent.STOP)
        server = SimpleNamespace(should_exit=False)

        async def server_done() -> None:
            return None

        server_task: asyncio.Task[None] = asyncio.create_task(server_done())
        events: list[str] = []

        async def grace_window() -> None:
            events.append("grace")
            assert server.should_exit is False

        async def cleanup_pending() -> None:
            events.append("pending")
            assert server.should_exit is False

        async def terminate_sessions() -> None:
            events.append("terminate")
            assert server.should_exit is False

        async def stop_communications() -> None:
            events.append("communications")
            assert server.should_exit is False

        async def shutdown_websocket(_runner: object) -> None:
            events.append("websocket")
            assert server.should_exit is False

        async def shutdown_workflow_runtime() -> None:
            events.append("workflow-runtime")

        async def drain_rule_allow_audit() -> None:
            events.append("rule-allow-audit")

        runner.http_server._cleanup_pending_interactions = AsyncMock(side_effect=cleanup_pending)
        runner.http_server._terminate_streamable_http_sessions.side_effect = terminate_sessions
        runner.http_server._hook_manager = SimpleNamespace(
            _shutdown_complete=False,
            shutdown_async=AsyncMock(side_effect=shutdown_workflow_runtime),
        )
        runner.communications_manager = SimpleNamespace(
            stop=AsyncMock(side_effect=stop_communications)
        )

        with patch(
            "gobby.telemetry.rule_allow_audit.shutdown_rule_allow_audit",
            new=AsyncMock(side_effect=drain_rule_allow_audit),
        ):
            await runner_lifecycle_shutdown.shutdown_daemon_services(
                runner,
                server,
                server_task,
                1,
                await_critical_stop_hook_grace_window=grace_window,
                shutdown_websocket_server=shutdown_websocket,
                reap_remaining_child_processes=AsyncMock(),
                shutdown_telemetry=MagicMock(),
                cleanup_pid_file=MagicMock(),
            )

        assert events[:5] == [
            "grace",
            "pending",
            "terminate",
            "communications",
            "websocket",
        ]
        assert events.index("communications") < events.index("workflow-runtime")
        assert events.index("rule-allow-audit") < events.index("workflow-runtime")
        assert server.should_exit is True

    @pytest.mark.asyncio
    async def test_http_connections_drain_before_uvicorn_exit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = self._minimal_shutdown_runner(ShutdownIntent.STOP)
        server = SimpleNamespace(should_exit=False)
        connections: set[MagicMock] = set()
        tasks: set[object] = {object()}
        events: list[str] = []

        transport = SimpleNamespace(closed=False)

        def close_transport() -> None:
            assert server.should_exit is False
            transport.closed = True
            connections.clear()
            tasks.clear()
            events.append("close")

        transport.close = close_transport
        transport.is_closing = lambda: bool(transport.closed)
        connection = MagicMock()
        connection.transport = transport
        connection.shutdown.side_effect = lambda: events.append("connection-shutdown")
        connections.add(connection)
        server.server_state = SimpleNamespace(connections=connections, tasks=tasks)

        async def server_done() -> None:
            return None

        server_task: asyncio.Task[None] = asyncio.create_task(server_done())
        monkeypatch.setattr(runner_lifecycle_shutdown, "_HTTP_CONNECTION_GRACE_SECONDS", 0.0)

        await runner_lifecycle_shutdown.shutdown_daemon_services(
            runner,
            server,
            server_task,
            1,
            await_critical_stop_hook_grace_window=AsyncMock(),
            shutdown_websocket_server=AsyncMock(),
            reap_remaining_child_processes=AsyncMock(),
            shutdown_telemetry=MagicMock(),
            cleanup_pid_file=MagicMock(),
        )

        assert events[:2] == ["connection-shutdown", "close"]
        assert transport.closed is True
        assert server.should_exit is True

    @pytest.mark.asyncio
    async def test_http_request_tasks_cancel_before_uvicorn_exit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = self._minimal_shutdown_runner(ShutdownIntent.STOP)
        events: list[str] = []
        connections: set[MagicMock] = set()
        tasks: set[asyncio.Task[None]] = set()

        class FakeServer:
            def __init__(self) -> None:
                self._should_exit = False
                self.server_state = SimpleNamespace(connections=connections, tasks=tasks)

            @property
            def should_exit(self) -> bool:
                return self._should_exit

            @should_exit.setter
            def should_exit(self, value: bool) -> None:
                events.append("should-exit")
                self._should_exit = value

        server = FakeServer()
        transport = SimpleNamespace(closed=False)
        request_started = asyncio.Event()

        def close_transport() -> None:
            assert server.should_exit is False
            transport.closed = True
            connections.clear()
            events.append("close")

        async def lingering_request() -> None:
            request_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as exc:
                assert server.should_exit is False
                events.append(f"request-cancel:{exc.args[0]}")
                raise

        request_task = asyncio.create_task(lingering_request())
        tasks.add(request_task)
        request_task.add_done_callback(tasks.discard)
        await asyncio.wait_for(request_started.wait(), timeout=1)

        transport.close = close_transport
        transport.is_closing = lambda: bool(transport.closed)
        connection = MagicMock()
        connection.transport = transport
        connection.shutdown.side_effect = lambda: events.append("connection-shutdown")
        connections.add(connection)

        async def server_done() -> None:
            return None

        server_task: asyncio.Task[None] = asyncio.create_task(server_done())
        monkeypatch.setattr(runner_lifecycle_shutdown, "_HTTP_CONNECTION_GRACE_SECONDS", 0.0)
        monkeypatch.setattr(runner_lifecycle_shutdown, "_HTTP_CONNECTION_DRAIN_SECONDS", 0.0)
        monkeypatch.setattr(
            runner_lifecycle_shutdown,
            "_HTTP_REQUEST_TASK_CANCEL_TIMEOUT_SECONDS",
            0.2,
        )

        await runner_lifecycle_shutdown.shutdown_daemon_services(
            runner,
            server,
            server_task,
            1,
            await_critical_stop_hook_grace_window=AsyncMock(),
            shutdown_websocket_server=AsyncMock(),
            reap_remaining_child_processes=AsyncMock(),
            shutdown_telemetry=MagicMock(),
            cleanup_pid_file=MagicMock(),
        )

        assert events[:4] == [
            "connection-shutdown",
            "close",
            "request-cancel:Gobby shutdown drain",
            "should-exit",
        ]
        assert transport.closed is True
        assert request_task.cancelled()
        assert tasks == set()

    @pytest.mark.parametrize("force_cleanup", [False, True])
    async def test_http_tasks_settle_before_database_executor_shutdown(
        self,
        force_cleanup: bool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        events: list[str] = []
        request_tasks: set[asyncio.Task[None]] = set()
        request_started = asyncio.Event()
        server_exit = asyncio.Event()

        async def request() -> None:
            request_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                events.append("request-cancel")
                raise

        request_task = asyncio.create_task(request())
        request_tasks.add(request_task)
        request_task.add_done_callback(request_tasks.discard)
        await request_started.wait()

        class FakeServer:
            def __init__(self) -> None:
                self._should_exit = False
                self.server_state = SimpleNamespace(connections=set(), tasks=request_tasks)

            @property
            def should_exit(self) -> bool:
                return self._should_exit

            @should_exit.setter
            def should_exit(self, value: bool) -> None:
                self._should_exit = value
                if value:
                    server_exit.set()

        async def serve() -> None:
            await server_exit.wait()
            events.append("uvicorn-settle")

        executor_state = {"joined": False}

        def shutdown_executor(*, cancel_futures: bool = True) -> None:
            assert cancel_futures is True
            events.append("db-executor")

        def join_executor() -> None:
            executor_state["joined"] = True

        runner = self._minimal_shutdown_runner(ShutdownIntent.STOP)
        runner.db_executor = SimpleNamespace(
            shutdown=shutdown_executor,
            join=join_executor,
            is_joined=lambda: executor_state["joined"],
        )
        server = FakeServer()
        server_task = asyncio.create_task(serve())

        async def grace_window() -> None:
            if force_cleanup:
                await asyncio.Event().wait()

        monkeypatch.setattr(runner_lifecycle_shutdown, "_HTTP_CONNECTION_GRACE_SECONDS", 0.0)
        monkeypatch.setattr(runner_lifecycle_shutdown, "_HTTP_CONNECTION_DRAIN_SECONDS", 0.0)
        monkeypatch.setattr(
            runner_lifecycle_shutdown,
            "_HTTP_REQUEST_TASK_CANCEL_TIMEOUT_SECONDS",
            0.05,
        )
        if force_cleanup:
            monkeypatch.setattr(
                runner_lifecycle_shutdown,
                "_GRACEFUL_SHUTDOWN_BUDGET_SECONDS",
                0.01,
            )
            monkeypatch.setattr(
                runner_lifecycle_shutdown,
                "_OVERALL_SHUTDOWN_DEADLINE_SECONDS",
                0.2,
            )

        await runner_lifecycle_shutdown.shutdown_daemon_services(
            runner,
            cast(Any, server),
            server_task,
            0,
            await_critical_stop_hook_grace_window=grace_window,
            shutdown_websocket_server=AsyncMock(),
            reap_remaining_child_processes=AsyncMock(),
            shutdown_telemetry=MagicMock(),
            cleanup_pid_file=MagicMock(),
        )

        assert events.index("request-cancel") < events.index("db-executor")
        assert events.index("uvicorn-settle") < events.index("db-executor")
        assert server_task.done()
        assert request_task.cancelled()

    @pytest.mark.asyncio
    async def test_restart_lifecycle_manager_timeout_logs_info(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        runner = self._minimal_shutdown_runner(ShutdownIntent.RESTART)
        server = SimpleNamespace(should_exit=False)

        async def server_done() -> None:
            return None

        server_task: asyncio.Task[None] = asyncio.create_task(server_done())
        real_wait_for = asyncio.wait_for
        call_count = 0

        async def timeout_lifecycle(awaitable, timeout: float):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                awaitable.close()
                raise TimeoutError
            return await real_wait_for(awaitable, timeout)

        caplog.set_level(logging.INFO, logger="gobby.runner_lifecycle")
        with patch(
            "gobby.runner_lifecycle_shutdown.asyncio.wait_for",
            side_effect=timeout_lifecycle,
        ):
            await runner_lifecycle_shutdown.shutdown_daemon_services(
                runner,
                server,
                server_task,
                1,
                await_critical_stop_hook_grace_window=AsyncMock(),
                shutdown_websocket_server=AsyncMock(),
                reap_remaining_child_processes=AsyncMock(),
                shutdown_telemetry=MagicMock(),
                cleanup_pid_file=MagicMock(),
            )

        assert "Lifecycle manager shutdown exceeded timeout during daemon restart" in caplog.text
        assert all(record.levelno < logging.WARNING for record in caplog.records)

    @pytest.mark.asyncio
    async def test_stop_lifecycle_manager_timeout_still_warns(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        runner = self._minimal_shutdown_runner(ShutdownIntent.STOP)
        server = SimpleNamespace(should_exit=False)

        async def server_done() -> None:
            return None

        server_task: asyncio.Task[None] = asyncio.create_task(server_done())
        real_wait_for = asyncio.wait_for
        call_count = 0

        async def timeout_lifecycle(awaitable, timeout: float):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                awaitable.close()
                raise TimeoutError
            return await real_wait_for(awaitable, timeout)

        caplog.set_level(logging.WARNING, logger="gobby.runner_lifecycle")
        with patch(
            "gobby.runner_lifecycle_shutdown.asyncio.wait_for",
            side_effect=timeout_lifecycle,
        ):
            await runner_lifecycle_shutdown.shutdown_daemon_services(
                runner,
                server,
                server_task,
                1,
                await_critical_stop_hook_grace_window=AsyncMock(),
                shutdown_websocket_server=AsyncMock(),
                reap_remaining_child_processes=AsyncMock(),
                shutdown_telemetry=MagicMock(),
                cleanup_pid_file=MagicMock(),
            )

        assert "Lifecycle manager shutdown timed out" in caplog.text
        assert any(
            "Lifecycle manager shutdown timed out" in record.message for record in caplog.records
        )

    @pytest.mark.parametrize("recycled_preserved_pid", [False, True])
    @pytest.mark.asyncio
    async def test_restart_reaps_only_non_terminal_agent_children(
        self,
        monkeypatch: pytest.MonkeyPatch,
        recycled_preserved_pid: bool,
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
                create_time: float | None = None,
            ) -> None:
                self.pid = pid
                self._name = name
                self._cmdline = cmdline
                self._children = children or []
                self._create_time = float(pid) if create_time is None else create_time
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

            def create_time(self) -> float:
                return self._create_time

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
        recycled_pane = FakeProcess(
            pane.pid,
            "zsh",
            ["zsh"],
            create_time=pane.create_time() + 1,
        )

        class FakePsutil:
            NoSuchProcess = FakeNoSuchProcess
            AccessDenied = FakeAccessDenied

            @staticmethod
            def Process(pid: int) -> FakeProcess:
                if recycled_preserved_pid and pid == pane.pid:
                    return recycled_pane
                return processes[pid]

            @staticmethod
            def wait_procs(
                children: list[FakeProcess], timeout: float
            ) -> tuple[list[FakeProcess], list[FakeProcess]]:
                return children, []

        monkeypatch.setitem(sys.modules, "psutil", FakePsutil)

        await runner_lifecycle_processes._reap_remaining_child_processes(
            preserve_agents=True,
            preserved_agent_pids={pane.pid},
        )

        assert tmux.terminated is recycled_preserved_pid
        assert pane.terminated is recycled_preserved_pid
        assert unrelated_tmux.terminated is True
        assert worker.terminated is True

    async def test_restart_preserve_set_paginates_every_active_tmux_run(self) -> None:
        run_count = 1_005
        runs = [
            SimpleNamespace(
                id=f"run-{index}",
                pid=10_000 + index,
                tmux_session_name=f"agent-{index}",
            )
            for index in range(run_count)
        ]
        list_active_for_machine = MagicMock(
            side_effect=lambda _machine_id, *, limit, offset=0: runs[offset : offset + limit]
        )
        db_calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

        async def run_db(func: object, *args: object, **kwargs: object) -> object:
            db_calls.append((func, args, kwargs))
            assert callable(func)
            return func(*args, **kwargs)

        runner = SimpleNamespace(
            agent_runner=SimpleNamespace(
                run_storage=SimpleNamespace(list_active_for_machine=list_active_for_machine),
            ),
            db_executor=SimpleNamespace(run=run_db),
        )
        tmux_manager = SimpleNamespace(
            config=SimpleNamespace(socket_name="gobby", socket_path=None),
            list_sessions=AsyncMock(
                return_value=[
                    SimpleNamespace(
                        name=f"agent-{index}",
                        pane_pid=20_000 + index,
                        pane_dead=False,
                    )
                    for index in range(run_count)
                ]
            ),
        )

        with patch(
            "gobby.agents.tmux.get_tmux_session_manager",
            return_value=tmux_manager,
        ):
            preserved_pids = await runner_lifecycle_processes._preserved_agent_terminal_pids(runner)

        assert preserved_pids == {20_000 + index for index in range(run_count)}
        assert db_calls == [
            (
                cast(Any, runner_lifecycle_processes)._list_active_agent_runs_once,
                (runner,),
                {"include_fenced": True},
            ),
        ]
        assert [invocation.kwargs for invocation in list_active_for_machine.call_args_list] == [
            {"limit": runner_lifecycle_agents._RUN_REPLAY_PAGE_SIZE, "offset": offset}
            for offset in range(
                0,
                run_count,
                runner_lifecycle_agents._RUN_REPLAY_PAGE_SIZE,
            )
        ]


class TestRunGobbyFunction:
    """Tests for run_gobby async function."""

    @pytest.mark.asyncio
    async def test_run_gobby_creates_runner(self):
        """Test that run_gobby creates and runs GobbyRunner."""
        bootstrap = SimpleNamespace(
            database_url="postgresql://test",
            bind_host="127.0.0.1",
            daemon_port=60887,
        )
        lease = MagicMock()
        lease.try_acquire.return_value = True

        with (
            patch("gobby.runner.GobbyRunner") as mock_runner_cls,
            patch(
                "gobby.config.bootstrap.load_bootstrap", return_value=bootstrap
            ) as mock_load_bootstrap,
            patch("gobby.daemon_lease.ActiveDaemonLease", return_value=lease) as mock_lease_cls,
            patch(
                "gobby.daemon_lease_control.monitor_active_lease",
                new_callable=AsyncMock,
            ) as mock_monitor_lease,
            patch("gobby.storage.schema_contract.verify_schema") as mock_verify_schema,
            patch(
                "gobby.utils.machine_id.require_machine_id", return_value="machine-id"
            ) as mock_require_machine_id,
            patch("gobby.deployment.deployment_token", return_value="deadbeefdeadbeef"),
        ):
            mock_runner = AsyncMock()
            mock_runner.run = AsyncMock()
            mock_runner_cls.create = AsyncMock(return_value=mock_runner)

            ownership = FailOpenPidOwnership("test")
            await run_gobby(
                config_path=Path("/tmp/config.yaml"),
                verbose=True,
                ownership_resolution=ownership,
            )

            mock_runner_cls.create.assert_awaited_once_with(
                config_path=Path("/tmp/config.yaml"), verbose=True
            )
            mock_runner.run.assert_called_once_with(ownership_resolution=ownership)
            assert mock_runner.run.await_count == 1
            mock_load_bootstrap.assert_called_once_with(
                "/tmp/config.yaml", resolve_database_url=True
            )
            mock_require_machine_id.assert_called_once_with()
            mock_lease_cls.assert_called_once_with(
                "postgresql://test",
                machine_id="machine-id",
                deployment_token="deadbeefdeadbeef",
            )
            mock_verify_schema.assert_called_once_with("postgresql://test")
            assert lease.try_acquire.call_count == 1
            assert mock_monitor_lease.await_count == 1
            assert lease.release.call_count == 1

    def test_runner_construction_failure_rolls_back_storage(self) -> None:
        database = MagicMock()
        db_executor = MagicMock()

        def init_storage(runner: GobbyRunner, *_args: object) -> None:
            runner.startup_config = DaemonConfig()
            runner.database = database
            runner.db_executor = db_executor

        with (
            patch("gobby.runner_init.init_storage_and_config", side_effect=init_storage),
            patch("gobby.runner_init.init_runtime_capacity"),
            patch("gobby.runner_init.init_services", side_effect=RuntimeError("init failed")),
            patch("gobby.runner_init.init_orchestration"),
            patch("gobby.runner_init.init_servers"),
            patch("gobby.agents.pty_reader.PTYReaderManager") as pty_reader_manager,
            pytest.raises(RuntimeError, match="init failed"),
        ):
            _runner_with_static_runtime()

        db_executor.shutdown.assert_called_once_with()
        db_executor.join.assert_called_once_with()
        database.close.assert_called_once_with()
        pty_reader_manager.assert_not_called()
        assert get_app_context() is None


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

                _runner_with_static_runtime()

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

                _runner_with_static_runtime()

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


class TestPipelineEventBroadcasting:
    """Tests for setup_pipeline_event_broadcasting function."""

    @pytest.mark.asyncio
    async def test_terminal_dispatch_uses_pipeline_db_executor(self) -> None:
        """Terminal pipeline hooks run through the bounded DB executor when available."""
        from gobby.runner_broadcasting import setup_pipeline_event_broadcasting

        mock_ws_server = AsyncMock()
        mock_pipeline_executor = MagicMock()
        mock_pipeline_executor.db = object()
        mock_pipeline_executor.run_db = AsyncMock()

        setup_pipeline_event_broadcasting(mock_ws_server, mock_pipeline_executor)

        with patch("gobby.hooks.event_handlers._dispatch.on_pipeline_completed") as handler:
            await mock_pipeline_executor.event_callback(
                "pipeline_completed",
                "pe-123",
                task_id="task-1",
            )

        mock_pipeline_executor.run_db.assert_awaited_once()
        args, kwargs = mock_pipeline_executor.run_db.await_args
        assert args[0] is handler
        assert args[1].execution_id == "pe-123"
        assert args[1].data == {"task_id": "task-1"}
        assert kwargs == {"db": mock_pipeline_executor.db}
        mock_ws_server.broadcast_pipeline_event.assert_awaited_once_with(
            event="pipeline_completed",
            execution_id="pe-123",
            task_id="task-1",
        )

    @pytest.mark.asyncio
    async def test_terminal_dispatch_uses_thread_for_sync_run_db(self) -> None:
        """Sync DB bridge is invoked when run_db is not awaitable."""
        from gobby.runner_broadcasting import setup_pipeline_event_broadcasting

        mock_ws_server = AsyncMock()
        mock_pipeline_executor = MagicMock()
        mock_pipeline_executor.db = object()
        run_db = MagicMock()
        mock_pipeline_executor.run_db = run_db

        setup_pipeline_event_broadcasting(mock_ws_server, mock_pipeline_executor)

        with patch("gobby.hooks.event_handlers._dispatch.on_pipeline_failed") as handler:
            await mock_pipeline_executor.event_callback(
                "pipeline_failed",
                "pe-124",
                task_id="task-2",
            )

        run_db.assert_called_once()
        args, kwargs = run_db.call_args
        assert args[0] is handler
        assert args[1].execution_id == "pe-124"
        assert args[1].data == {"task_id": "task-2"}
        assert kwargs == {"db": mock_pipeline_executor.db}
        mock_ws_server.broadcast_pipeline_event.assert_awaited_once_with(
            event="pipeline_failed",
            execution_id="pe-124",
            task_id="task-2",
        )

    @pytest.mark.asyncio
    async def test_terminal_dispatch_omits_db_for_run_db_without_db_parameter(self) -> None:
        """run_db signature controls whether the terminal dispatch receives db."""
        from gobby.runner_broadcasting import setup_pipeline_event_broadcasting

        calls: list[tuple[object, object]] = []

        def run_db(dispatch: object, payload: object) -> None:
            calls.append((dispatch, payload))

        mock_ws_server = AsyncMock()
        mock_pipeline_executor = MagicMock()
        mock_pipeline_executor.db = object()
        mock_pipeline_executor.run_db = run_db

        setup_pipeline_event_broadcasting(mock_ws_server, mock_pipeline_executor)

        with patch("gobby.hooks.event_handlers._dispatch.on_pipeline_failed") as handler:
            await mock_pipeline_executor.event_callback("pipeline_failed", "pe-124")

        assert len(calls) == 1
        assert calls[0][0] is handler
        assert cast(Any, calls[0][1]).execution_id == "pe-124"
        mock_ws_server.broadcast_pipeline_event.assert_awaited_once_with(
            event="pipeline_failed",
            execution_id="pe-124",
        )

    @pytest.mark.asyncio
    async def test_terminal_dispatch_awaits_async_callable_object_run_db(self) -> None:
        """Callable objects returning awaitables are handled like async functions."""
        from gobby.runner_broadcasting import setup_pipeline_event_broadcasting

        class AsyncRunDb:
            def __init__(self) -> None:
                self.calls: list[tuple[object, object, dict[str, object]]] = []

            def __call__(self, dispatch: object, payload: object, **kwargs: object) -> object:
                self.calls.append((dispatch, payload, kwargs))

                async def _run() -> None:
                    return None

                return _run()

        mock_ws_server = AsyncMock()
        mock_pipeline_executor = MagicMock()
        mock_pipeline_executor.db = object()
        run_db = AsyncRunDb()
        mock_pipeline_executor.run_db = run_db

        setup_pipeline_event_broadcasting(mock_ws_server, mock_pipeline_executor)

        with patch("gobby.hooks.event_handlers._dispatch.on_pipeline_cancelled") as handler:
            await mock_pipeline_executor.event_callback("pipeline_cancelled", "pe-125")

        assert len(run_db.calls) == 1
        dispatch, payload, kwargs = run_db.calls[0]
        assert dispatch is handler
        assert getattr(payload, "execution_id", None) == "pe-125"
        assert kwargs == {"db": mock_pipeline_executor.db}


class TestCronEventBroadcasting:
    """Tests for setup_cron_event_broadcasting function."""

    @pytest.mark.asyncio
    async def test_dispatched_run_broadcasts_child_payload(self) -> None:
        from gobby.runner_broadcasting import setup_cron_event_broadcasting
        from gobby.storage.cron_models import CronJob, CronRun, CronRunChild

        mock_ws_server = AsyncMock()
        mock_scheduler = MagicMock()
        setup_cron_event_broadcasting(mock_ws_server, mock_scheduler)
        job = CronJob(
            id="cj-1",
            project_id="project-1",
            name="Pipeline Cron",
            schedule_type="cron",
            action_type="pipeline",
            action_config={},
            created_at="2026-02-10T00:00:00+00:00",
            updated_at="2026-02-10T00:00:00+00:00",
        )
        run = CronRun(
            id="cr-1",
            cron_job_id="cj-1",
            triggered_at="2026-02-10T00:00:00+00:00",
            created_at="2026-02-10T00:00:00+00:00",
            status="dispatched",
            pipeline_execution_id="pe-1",
            child=CronRunChild(
                type="pipeline_execution",
                id="pe-1",
                status="waiting_approval",
                terminal=False,
            ),
        )

        await mock_scheduler.on_run_complete(job, run)

        mock_ws_server.broadcast_cron_event.assert_awaited_once()
        kwargs = mock_ws_server.broadcast_cron_event.await_args.kwargs
        assert kwargs["event"] == "run_dispatched"
        assert kwargs["run"]["child"] == {
            "type": "pipeline_execution",
            "id": "pe-1",
            "status": "waiting_approval",
            "terminal": False,
            "missing": False,
        }

    @pytest.mark.asyncio
    async def test_unknown_run_status_broadcasts_neutral_event(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from gobby.runner_broadcasting import setup_cron_event_broadcasting
        from gobby.storage.cron_models import CronJob, CronRun

        mock_ws_server = AsyncMock()
        mock_scheduler = MagicMock()
        setup_cron_event_broadcasting(mock_ws_server, mock_scheduler)
        job = CronJob(
            id="cj-1",
            project_id="project-1",
            name="Unknown Cron",
            schedule_type="cron",
            action_type="handler",
            action_config={},
            created_at="2026-02-10T00:00:00+00:00",
            updated_at="2026-02-10T00:00:00+00:00",
        )
        run = CronRun(
            id="cr-1",
            cron_job_id="cj-1",
            triggered_at="2026-02-10T00:00:00+00:00",
            created_at="2026-02-10T00:00:00+00:00",
            status="paused",
        )

        with caplog.at_level(logging.WARNING, logger="gobby.runner_broadcasting"):
            await mock_scheduler.on_run_complete(job, run)

        kwargs = mock_ws_server.broadcast_cron_event.await_args.kwargs
        assert kwargs["event"] == "run_unknown"
        assert kwargs["status"] == "paused"
        assert "Unknown cron run status paused" in caplog.text


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

            runner = _runner_with_static_runtime()
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
                    startup_delay_seconds=0,
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

            runner = _runner_with_static_runtime()
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
                    startup_delay_seconds=0,
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

            runner = _runner_with_static_runtime()
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

    @pytest.mark.asyncio
    async def test_tool_result_cleanup_loop_runs_once_and_stops(self) -> None:
        from gobby.runner_maintenance_recurring import tool_result_cleanup_loop

        shutdown_requested = False
        cleanup_calls = 0

        def cleanup_expired() -> int:
            nonlocal cleanup_calls, shutdown_requested
            cleanup_calls += 1
            shutdown_requested = True
            return 2

        async def run_db(func: Callable[[], int]) -> int:
            return func()

        sleep_delays: list[float] = []

        async def sleep(delay: float) -> None:
            sleep_delays.append(delay)

        with patch(
            "gobby.storage.tool_results.ToolResultStore",
            return_value=SimpleNamespace(cleanup_expired=cleanup_expired),
        ):
            await tool_result_cleanup_loop(
                object(),
                lambda: shutdown_requested,
                capture_bundle=static_runtime_capture(DaemonConfig()),
                run_db=run_db,
                interval_seconds=1,
                startup_delay_seconds=0,
                sleep=sleep,
            )

        assert cleanup_calls == 1
        assert shutdown_requested is True
        assert sleep_delays == [1]


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

    def test_signal_handler_preserves_restart_marker_for_restart_downtime(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        enable_log_propagation: None,
    ) -> None:
        from gobby.runner_maintenance import setup_signal_handlers
        from gobby.shutdown_intent import write_shutdown_intent

        mock_loop = MagicMock()
        captured_handler = None

        def capture_handler(sig, handler):
            nonlocal captured_handler
            if sig == signal.SIGTERM:
                captured_handler = handler

        mock_loop.add_signal_handler = capture_handler
        shutdown_callback = MagicMock()
        shutdown_intent_callback = MagicMock()

        with (
            patch("asyncio.get_running_loop", return_value=mock_loop),
            patch("gobby.runner_maintenance.lifecycle.get_gobby_home", return_value=tmp_path),
        ):
            setup_signal_handlers(
                shutdown_callback,
                shutdown_intent_callback=shutdown_intent_callback,
            )
            write_shutdown_intent(
                "cli_restart",
                ShutdownIntent.RESTART,
                sender_pid=123,
                home=tmp_path,
            )

            assert captured_handler is not None
            with caplog.at_level(logging.DEBUG, logger="gobby.runner_maintenance"):
                captured_handler()
                captured_handler()

        shutdown_intent_callback.assert_called_once_with(ShutdownIntent.RESTART)
        assert shutdown_callback.call_count == 2
        assert (tmp_path / "shutdown_intent_active.json").exists()
        received_logs = [
            record
            for record in caplog.records
            if record.levelno == logging.INFO and record.message.startswith("Received SIGTERM")
        ]
        source_logs = [
            record
            for record in caplog.records
            if record.levelno == logging.INFO and record.message.startswith("Shutdown source:")
        ]
        assert len(received_logs) == 1
        assert len(source_logs) == 1
        assert source_logs[0].message == (
            "Shutdown source: source=cli_restart, intent=restart, sender_pid=123"
        )
        assert "unknown (no shutdown_intent_active.json - external SIGTERM)" not in caplog.text
        assert any(
            record.levelno == logging.DEBUG
            and "Shutdown already in progress" in record.message
            and "source=cli_restart" in record.message
            for record in caplog.records
        )

    def test_signal_handler_recovers_thirty_second_restart_marker_without_consuming(
        self,
        tmp_path: Path,
    ) -> None:
        from gobby.runner_maintenance import setup_signal_handlers
        from gobby.shutdown_intent import write_shutdown_intent

        mock_loop = MagicMock()
        captured_handler = None

        def capture_handler(sig, handler):
            nonlocal captured_handler
            if sig == signal.SIGTERM:
                captured_handler = handler

        mock_loop.add_signal_handler = capture_handler
        shutdown_callback = MagicMock()
        shutdown_intent_callback = MagicMock()
        marker_written_at = 1_000.0

        with patch("gobby.shutdown_intent.time.time", return_value=marker_written_at):
            write_shutdown_intent(
                "cli_restart",
                ShutdownIntent.RESTART,
                sender_pid=123,
                home=tmp_path,
            )

        with (
            patch("asyncio.get_running_loop", return_value=mock_loop),
            patch("gobby.runner_maintenance.lifecycle.get_gobby_home", return_value=tmp_path),
            patch("gobby.shutdown_intent.time.time", return_value=marker_written_at + 30.0),
        ):
            setup_signal_handlers(
                shutdown_callback,
                shutdown_intent_callback=shutdown_intent_callback,
            )
            assert captured_handler is not None
            captured_handler()

        shutdown_intent_callback.assert_called_once_with(ShutdownIntent.RESTART)
        shutdown_callback.assert_called_once_with()
        assert (tmp_path / "shutdown_intent_active.json").exists()

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
            patch("gobby.runner_maintenance.lifecycle.get_gobby_home", return_value=tmp_path),
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
    @pytest.mark.parametrize(
        ("event_type", "expected_task_count"),
        [("agent_started", 3), ("agent_completed", 4)],
    )
    async def test_broadcast_tasks_retained_until_completion(
        self,
        event_type: str,
        expected_task_count: int,
    ) -> None:
        """Every agent lifecycle broadcast stays anchored until its coroutine completes."""
        import gobby.runner_broadcasting as rb
        from gobby.runner_broadcasting import fire_agent_event, setup_agent_event_broadcasting

        release = asyncio.Event()

        async def wait_for_release(*_args: object, **_kwargs: object) -> None:
            await release.wait()

        mock_ws_server = MagicMock()
        mock_ws_server.broadcast_agent_event = AsyncMock(side_effect=wait_for_release)
        mock_ws_server.broadcast_tmux_session_event = AsyncMock(side_effect=wait_for_release)

        mock_pty_manager = MagicMock()
        mock_pty_manager.stop_reader = AsyncMock(side_effect=wait_for_release)
        mock_tmux_reader = MagicMock()
        mock_tmux_reader.start_reader = AsyncMock(side_effect=wait_for_release)
        mock_tmux_reader.stop_reader = AsyncMock(side_effect=wait_for_release)

        old_callback = rb._agent_event_callback
        tasks_before = set(rb._agent_broadcast_tasks)
        scheduled_tasks: set[asyncio.Task[None]] = set()
        try:
            with (
                patch(
                    "gobby.agents.pty_reader.get_pty_reader_manager",
                    return_value=mock_pty_manager,
                ),
                patch(
                    "gobby.agents.tmux.get_tmux_output_reader",
                    return_value=mock_tmux_reader,
                ),
            ):
                setup_agent_event_broadcasting(mock_ws_server)

            fire_agent_event(
                event_type,
                "run-123",
                {"tmux_session_name": "agent-run-123"},
            )

            scheduled_tasks = rb._agent_broadcast_tasks - tasks_before
            assert len(scheduled_tasks) == expected_task_count
            assert all(not task.done() for task in scheduled_tasks)

            release.set()
            await asyncio.wait_for(asyncio.gather(*scheduled_tasks), timeout=1.0)

            assert scheduled_tasks.isdisjoint(rb._agent_broadcast_tasks)
        finally:
            release.set()
            unfinished_tasks = [task for task in scheduled_tasks if not task.done()]
            if unfinished_tasks:
                await asyncio.gather(*unfinished_tasks, return_exceptions=True)
            rb._agent_event_callback = old_callback

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


class TestRuntimeServiceIdentityAfterRebuild:
    """Resolver-converted consumers observe rebuilt runtime services."""

    def test_session_lifecycle_manager_observes_rebuilt_services(self) -> None:
        from gobby.sessions.lifecycle import SessionLifecycleManager

        services: dict[str, object] = {}
        capture = static_runtime_capture(DaemonConfig(), services=services)
        with patch("gobby.sessions.lifecycle.SessionManager"):
            manager = SessionLifecycleManager(db=MagicMock(), capture_bundle=capture)

        assert manager.memory_manager is None
        assert manager.llm_service is None

        first_memory = SimpleNamespace(memory_manager=object())
        first_ai = SimpleNamespace(llm_service=object())
        services["memory_services"] = first_memory
        services["ai_services"] = first_ai
        assert manager.memory_manager is first_memory.memory_manager
        assert manager.llm_service is first_ai.llm_service

        rebuilt_memory = SimpleNamespace(memory_manager=object())
        rebuilt_ai = SimpleNamespace(llm_service=object())
        services["memory_services"] = rebuilt_memory
        services["ai_services"] = rebuilt_ai
        assert manager.memory_manager is rebuilt_memory.memory_manager
        assert manager.llm_service is rebuilt_ai.llm_service

    def test_memory_manager_llm_resolver_observes_runtime_rebuilds(self) -> None:
        from gobby.runner_init.services import AIServiceBundle, _resolve_llm_service

        services: dict[str, object] = {}
        runtime = SimpleNamespace(
            ready=True,
            capture=static_runtime_capture(DaemonConfig(), services=services),
        )
        runner = SimpleNamespace(config_runtime=runtime, llm_service=object())

        assert _resolve_llm_service(runner) is None

        first = MagicMock(spec=AIServiceBundle)
        first.llm_service = object()
        services["ai_services"] = first
        assert _resolve_llm_service(runner) is first.llm_service

        rebuilt = MagicMock(spec=AIServiceBundle)
        rebuilt.llm_service = object()
        services["ai_services"] = rebuilt
        assert _resolve_llm_service(runner) is rebuilt.llm_service


class TestMessageProcessorPreparedService:
    """Lifecycle contract for the rebuilt message-processor subscriber."""

    @pytest.mark.asyncio
    async def test_activate_rewires_refs_and_schedules_start(self) -> None:
        from gobby.runner_init.services import _build_message_processor

        config = MagicMock()
        config.message_tracking.enabled = True
        config.message_tracking.poll_interval = 5.0
        hook_manager = MagicMock()
        processor = MagicMock()
        processor.start = AsyncMock()
        processor.stop = AsyncMock()
        runner = SimpleNamespace(
            database=MagicMock(),
            session_manager=MagicMock(),
            db_executor=SimpleNamespace(run=AsyncMock()),
            websocket_server=MagicMock(),
            http_server=SimpleNamespace(_hook_manager=hook_manager),
            message_processor=None,
        )
        loop = asyncio.get_running_loop()

        with patch(
            "gobby.runner_init.services.SessionMessageProcessor",
            return_value=processor,
        ):
            prepared = _build_message_processor(runner, config, loop)

        assert prepared is not None
        assert prepared.value is processor
        processor.start.assert_not_called()

        original_debug = loop.get_debug()
        loop.set_debug(True)
        try:
            await asyncio.to_thread(prepared.activate)
        finally:
            loop.set_debug(original_debug)

        assert runner.message_processor is processor
        assert processor.session_manager is runner.session_manager
        assert processor.websocket_server is runner.websocket_server
        processor.set_hook_manager.assert_called_once_with(hook_manager)
        processor.start.assert_awaited_once()
        hook_manager._session_coordinator.reregister_active_sessions.assert_called_once_with(
            message_processor=processor
        )

        await asyncio.to_thread(prepared.dispose)
        assert runner.message_processor is None
        processor.stop.assert_awaited_once()

    def test_failed_activation_cancels_future_and_clears_runner_reference(self) -> None:
        from gobby.runner_init.services import _build_message_processor

        config = MagicMock()
        config.message_tracking.enabled = True
        processor = MagicMock()
        future = MagicMock()
        future.result.side_effect = RuntimeError("start failed")
        runner = SimpleNamespace(
            database=MagicMock(),
            session_manager=MagicMock(),
            db_executor=SimpleNamespace(run=AsyncMock()),
            websocket_server=MagicMock(),
            http_server=SimpleNamespace(_hook_manager=None),
            message_processor=None,
        )

        with (
            patch(
                "gobby.runner_init.services.SessionMessageProcessor",
                return_value=processor,
            ),
            patch(
                "gobby.runner_init.services.asyncio.run_coroutine_threadsafe",
                return_value=future,
            ),
        ):
            prepared = _build_message_processor(runner, config, MagicMock())
            assert prepared is not None
            with pytest.raises(RuntimeError, match="start failed"):
                prepared.activate()

        future.cancel.assert_called_once_with()
        assert runner.message_processor is None

    def test_disabled_tracking_builds_no_service(self) -> None:
        from gobby.runner_init.services import _build_message_processor

        config = MagicMock()
        config.message_tracking.enabled = False
        runner = SimpleNamespace()

        assert _build_message_processor(runner, config, MagicMock()) is None

    @pytest.mark.asyncio
    async def test_runtime_disable_and_rebuild_updates_production_resolver(self) -> None:
        from gobby.app_context import ServiceContainer
        from gobby.config.runtime import ConfigRuntime
        from gobby.config.sessions import MessageTrackingConfig
        from gobby.hooks.session_coordinator import SessionCoordinator
        from gobby.runner_init.servers import _resolve_message_processor
        from gobby.runner_init.services import _build_message_processor
        from tests.config.test_stateful_config_subscribers import (
            FakeRegistry,
            FakeRepository,
            StoredSnapshot,
            snapshot,
            subscriber,
        )

        class MessageRepository(FakeRepository):
            def runtime_candidate(
                self, overrides: dict[str, object], _secret_bindings: object
            ) -> DaemonConfig:
                enabled = cast(bool, overrides["message_tracking.enabled"])
                return DaemonConfig(
                    message_tracking=MessageTrackingConfig(enabled=enabled, poll_interval=0.1)
                )

        repository = MessageRepository(
            cast(
                list[StoredSnapshot],
                [
                    snapshot(0, **{"message_tracking.enabled": True}),
                    snapshot(1, **{"message_tracking.enabled": False}),
                    snapshot(2, **{"message_tracking.enabled": True}),
                ],
            )
        )
        session = MagicMock(
            id="session-1",
            transcript_path="/tmp/transcript.jsonl",
            source="claude",
        )
        session_storage = MagicMock()
        session_storage.list.side_effect = lambda status, limit: (
            [session] if status == "active" else []
        )
        processors = [MagicMock(), MagicMock()]
        for processor in processors:
            processor.start = AsyncMock()
            processor.stop = AsyncMock()
        runner = SimpleNamespace(
            config_runtime=None,
            database=MagicMock(),
            session_manager=session_storage,
            db_executor=SimpleNamespace(run=AsyncMock()),
            websocket_server=MagicMock(),
            http_server=None,
            message_processor=None,
        )
        loop = asyncio.get_running_loop()
        runtime = ConfigRuntime(
            repository,
            registry=FakeRegistry(),
            subscribers=[
                subscriber(
                    "message_processor",
                    {"message_tracking.enabled"},
                    lambda change: _build_message_processor(runner, change.desired, loop),
                )
            ],
        )
        runner.config_runtime = runtime
        container = ServiceContainer(
            database=MagicMock(),
            session_manager=session_storage,
            task_manager=MagicMock(),
            message_processor_resolver=lambda: _resolve_message_processor(cast(Any, runner)),
        )
        coordinator = SessionCoordinator(
            session_storage=session_storage,
            message_processor_resolver=container.resolve_message_processor,
        )
        runner.http_server = SimpleNamespace(
            _hook_manager=SimpleNamespace(_session_coordinator=coordinator)
        )

        with patch(
            "gobby.runner_init.services.SessionMessageProcessor",
            side_effect=processors,
        ):
            await runtime.start()
            assert container.resolve_message_processor() is processors[0]
            processors[0].register_session.assert_called_once()

            repository.index = 1
            await runtime.reconcile_revision(1)
            assert container.resolve_message_processor() is None
            assert runner.message_processor is None
            assert coordinator.reregister_active_sessions() == 0
            processors[0].register_session.assert_called_once()

            repository.index = 2
            await runtime.reconcile_revision(2)
            assert container.resolve_message_processor() is processors[1]
            processors[1].register_session.assert_called_once()

        await runtime.close()


class TestProjectPurgeRuntimeResolvers:
    def test_vector_cleaner_is_noop_when_qdrant_url_is_unconfigured(self) -> None:
        from gobby.projects.purge import NoopProjectVectorCleaner
        from gobby.runner_init.orchestration import _resolve_project_vector_cleaner

        active = DaemonConfig()
        assert active.databases.qdrant.url is None
        bundle = SimpleNamespace(snapshot=SimpleNamespace(active=active), services={})
        runner = SimpleNamespace(
            config_runtime=SimpleNamespace(capture=lambda: bundle),
        )

        assert isinstance(_resolve_project_vector_cleaner(runner), NoopProjectVectorCleaner)

    def test_graph_cleaner_resolves_current_memory_bundle_each_run(self) -> None:
        from gobby.runner_init.orchestration import _resolve_project_graph_cleaner

        active = DaemonConfig()
        first = object()
        second = object()
        services = {
            "memory_services": SimpleNamespace(
                memory_manager=SimpleNamespace(kg_service=first),
            )
        }
        bundle = SimpleNamespace(snapshot=SimpleNamespace(active=active), services=services)
        runner = SimpleNamespace(
            config_runtime=SimpleNamespace(capture=lambda: bundle),
        )

        assert _resolve_project_graph_cleaner(runner) is first
        services["memory_services"] = SimpleNamespace(
            memory_manager=SimpleNamespace(kg_service=second),
        )
        assert _resolve_project_graph_cleaner(runner) is second


class TestMessageProcessorWebSocketIntegration:
    """Tests for message processor and WebSocket server integration."""

    def test_message_processor_gets_websocket_server(self, mock_config_with_websocket) -> None:
        """Test that message processor receives the WebSocket server reference."""
        mock_config_with_websocket.message_tracking = MagicMock()
        mock_config_with_websocket.message_tracking.enabled = True
        mock_config_with_websocket.message_tracking.poll_interval = 5.0
        # Preset before create_base_patches: the safe defaults disable
        # communications, and this test asserts the enabled wiring path.
        mock_config_with_websocket.communications = MagicMock()
        mock_config_with_websocket.communications.enabled = True

        mock_ws_server = AsyncMock()
        mock_ws_server.start = AsyncMock()

        mock_message_processor = MagicMock()
        mock_communications_manager = MagicMock()

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
        patches.append(
            patch(
                "gobby.communications.manager.CommunicationsManager",
                return_value=mock_communications_manager,
            )
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = _runner_with_static_runtime()

            assert runner.message_processor is not None
            assert runner.message_processor.websocket_server == mock_ws_server
            mock_communications_manager.set_websocket_broadcast.assert_called_once_with(
                mock_ws_server.broadcast
            )
        mock_communications_manager.set_voice_transcriber_getter.assert_called_once_with(
            mock_ws_server.get_voice_transcriber,
            timeout_seconds=mock_config_with_websocket.voice.transcription_timeout_seconds,
        )


class TestShutdownLoop:
    """Tests for the shutdown waiting loop."""

    @staticmethod
    def _minimal_runner(mock_config: object) -> SimpleNamespace:
        return SimpleNamespace(
            request_shutdown=MagicMock(),
            _shutdown_requested=False,
            config=mock_config,
            bootstrap_config=mock_config,
            http_server=SimpleNamespace(
                app=MagicMock(),
                port=8765,
                services=SimpleNamespace(shutdown_in_progress=False),
            ),
            db_executor=SimpleNamespace(run=AsyncMock(), submit=MagicMock()),
        )

    @pytest.mark.asyncio
    async def test_readiness_failure_rolls_back_runner_resources(
        self, mock_config: MagicMock
    ) -> None:
        """Startup failures before HTTP bind release constructor-owned resources."""
        patches = create_base_patches(mock_config=mock_config)

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]
            runner = self._minimal_runner(mock_config)
            readiness = stack.enter_context(
                patch(
                    "gobby.runner_service_readiness.require_managed_services_ready",
                    new=AsyncMock(side_effect=RuntimeError("readiness failed")),
                )
            )
            rollback = stack.enter_context(patch("gobby.runner_rollback.rollback_runner_resources"))
            stack.enter_context(patch("gobby.runner_maintenance.setup_signal_handlers"))
            stack.enter_context(patch("gobby.runner_maintenance.cleanup_pid_file"))

            with pytest.raises(SystemExit) as exc_info:
                await runner_lifecycle.run_daemon(
                    runner,
                    ownership_resolution=FailOpenPidOwnership("test"),
                )

            assert exc_info.value.code == 1
            readiness.assert_awaited_once_with(runner)
            rollback.assert_called_once_with(runner)

    @pytest.mark.asyncio
    async def test_web_chat_runtime_starts_after_http_bind(self, mock_config) -> None:
        """Daemon-owned chat subprocesses start only after HTTP accepts connections."""
        patches = create_base_patches(mock_config=mock_config)

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]
            runner = self._minimal_runner(mock_config)
            runtime_started = asyncio.Event()

            server = MagicMock()
            server.started = False

            async def serve() -> None:
                assert runner.http_server.services.web_chat_runtime_manager.start.await_count == 0
                server.started = True
                await runtime_started.wait()
                runner._shutdown_requested = True

            async def start_runtime(*, background: bool) -> None:
                assert background is True
                assert server.started is True
                runtime_started.set()

            runtime_manager = SimpleNamespace(
                start=AsyncMock(side_effect=start_runtime),
                stop=AsyncMock(),
            )
            runner.http_server.services.web_chat_runtime_manager = runtime_manager
            server.serve = AsyncMock(side_effect=serve)

            stack.enter_context(patch("uvicorn.Config"))
            stack.enter_context(patch("uvicorn.Server", return_value=server))
            stack.enter_context(patch("gobby.runner_maintenance.setup_signal_handlers"))
            stack.enter_context(patch("gobby.runner_lifecycle._init_subsystems", new=AsyncMock()))
            stack.enter_context(patch("gobby.runner_lifecycle._start_periodic_tasks"))
            stack.enter_context(
                patch("gobby.runner_lifecycle.shutdown_daemon_services", new=AsyncMock())
            )

            await asyncio.wait_for(
                runner_lifecycle.run_daemon(
                    runner,
                    ownership_resolution=FailOpenPidOwnership("test"),
                ),
                timeout=1.0,
            )

            runtime_manager.start.assert_awaited_once_with(background=True)

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

            runner = _runner_with_static_runtime()

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
                        await asyncio.wait_for(
                            runner.run(ownership_resolution=FailOpenPidOwnership("test")),
                            timeout=5.0,
                        )

                    assert main_loop_slept is True
                    assert runner._shutdown_requested is True

    @pytest.mark.asyncio
    async def test_server_crash_before_bind_skips_side_effects_and_shuts_down(
        self,
        mock_config,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A server that never binds cannot start shared-state background work."""
        patches = create_base_patches(mock_config=mock_config)

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]
            runner = self._minimal_runner(mock_config)

            server = MagicMock()
            server.started = False
            failure = RuntimeError("serve loop crashed before bind")

            async def serve() -> None:
                raise failure

            server.serve = AsyncMock(side_effect=serve)
            stack.enter_context(patch("uvicorn.Config"))
            stack.enter_context(patch("uvicorn.Server", return_value=server))
            stack.enter_context(patch("gobby.runner_maintenance.setup_signal_handlers"))
            stack.enter_context(patch("gobby.runner_maintenance.cleanup_pid_file"))
            init_subsystems = stack.enter_context(patch("gobby.runner_lifecycle._init_subsystems"))
            start_periodic_tasks = stack.enter_context(
                patch("gobby.runner_lifecycle._start_periodic_tasks")
            )
            clear_shutdown_intent = stack.enter_context(
                patch("gobby.runner_lifecycle.clear_active_shutdown_intent")
            )
            shutdown_services = stack.enter_context(
                patch("gobby.runner_lifecycle.shutdown_daemon_services")
            )
            stack.enter_context(patch("gobby.runner._healthy_daemon_running", return_value=False))

            with caplog.at_level(logging.ERROR, logger="gobby.runner_lifecycle"):
                with pytest.raises(SystemExit) as exc_info:
                    await asyncio.wait_for(
                        runner_lifecycle.run_daemon(
                            runner,
                            ownership_resolution=FailOpenPidOwnership("test"),
                        ),
                        timeout=1.0,
                    )

            assert exc_info.value.code == 1
            assert runner._shutdown_requested is True
            init_subsystems.assert_not_awaited()
            start_periodic_tasks.assert_not_called()
            clear_shutdown_intent.assert_not_called()
            shutdown_services.assert_awaited_once()
            assert "serve loop crashed before bind" in caplog.text
            assert "requesting daemon shutdown" in caplog.text

    @pytest.mark.asyncio
    async def test_server_crash_after_bind_requests_shutdown(
        self,
        mock_config,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The server done-callback stops a daemon whose bound serve loop dies."""
        patches = create_base_patches(mock_config=mock_config)

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]
            runner = self._minimal_runner(mock_config)

            server = MagicMock()
            server.started = False
            side_effects_started = asyncio.Event()
            failure = RuntimeError("serve loop crashed after bind")

            async def serve() -> None:
                server.started = True
                await side_effects_started.wait()
                raise failure

            server.serve = AsyncMock(side_effect=serve)
            stack.enter_context(patch("uvicorn.Config"))
            stack.enter_context(patch("uvicorn.Server", return_value=server))
            stack.enter_context(patch("gobby.runner_maintenance.setup_signal_handlers"))
            stack.enter_context(patch("gobby.runner_maintenance.cleanup_pid_file"))
            init_subsystems = stack.enter_context(patch("gobby.runner_lifecycle._init_subsystems"))
            clear_shutdown_intent = stack.enter_context(
                patch("gobby.runner_lifecycle.clear_active_shutdown_intent")
            )

            def start_periodic_tasks(*_args: object, **_kwargs: object) -> None:
                side_effects_started.set()

            periodic_tasks = stack.enter_context(
                patch(
                    "gobby.runner_lifecycle._start_periodic_tasks",
                    side_effect=start_periodic_tasks,
                )
            )
            shutdown_services = stack.enter_context(
                patch("gobby.runner_lifecycle.shutdown_daemon_services")
            )
            stack.enter_context(patch("gobby.runner._healthy_daemon_running", return_value=False))

            with caplog.at_level(logging.ERROR, logger="gobby.runner_lifecycle"):
                with pytest.raises(SystemExit) as exc_info:
                    await asyncio.wait_for(
                        runner_lifecycle.run_daemon(
                            runner,
                            ownership_resolution=FailOpenPidOwnership("test"),
                        ),
                        timeout=1.0,
                    )

            assert exc_info.value.code == 1
            assert runner._shutdown_requested is True
            clear_shutdown_intent.assert_called_once_with()
            init_subsystems.assert_awaited_once()
            periodic_tasks.assert_called_once()
            shutdown_services.assert_awaited_once()
            assert "serve loop crashed after bind" in caplog.text
            assert "requesting daemon shutdown" in caplog.text


class TestMetricsCleanupLoopDetailed:
    """Detailed tests for the metrics cleanup loop."""

    @pytest.mark.asyncio
    async def test_metrics_cleanup_loop_performs_cleanup_after_sleep(self, mock_config):
        """Test that metrics cleanup loop performs cleanup after sleep interval."""
        from gobby.runner_maintenance import metrics_cleanup_loop

        patches = create_base_patches(mock_config=mock_config)

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = _runner_with_static_runtime()
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
                    startup_delay_seconds=0,
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

            runner = _runner_with_static_runtime()
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
                    startup_delay_seconds=0,
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

            runner = _runner_with_static_runtime()
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
                    startup_delay_seconds=0,
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


@pytest.mark.asyncio
async def test_startup_barrier_precedes_subscriber_recovery_and_optional_failure() -> None:
    events: list[str] = []
    monitor = SimpleNamespace(
        set_reconciliation_callback=MagicMock(),
        set_non_task_resume_callback=MagicMock(),
    )

    async def barrier(_runner: object) -> bool:
        assert monitor.set_reconciliation_callback.call_count == 1
        events.append("barrier")
        return True

    async def reconcile(_runner: object) -> int:
        events.append("classify")
        return 0

    async def recover(_runner: object) -> int:
        events.append("recover")
        return 0

    async def reap(_runner: object) -> int:
        events.append("reap")
        return 0

    async def fail_connect(*_args: object) -> None:
        events.append("connect")
        raise RuntimeError("optional startup failed")

    with (
        patch.object(
            runner_lifecycle_subsystems,
            "_run_agent_hook_replay_barrier",
            side_effect=barrier,
        ),
        patch.object(
            runner_lifecycle_subsystems,
            "_connect_mcp_servers",
            side_effect=fail_connect,
        ),
        pytest.raises(RuntimeError, match="optional startup failed"),
    ):
        await runner_lifecycle_subsystems.init_subsystems(
            SimpleNamespace(
                agent_runner=object(),
                agent_lifecycle_monitor=monitor,
            ),
            AsyncMock(),
            None,
            reconcile_agent_runs_after_restart=reconcile,
            reap_orphaned_srt_runners=reap,
            recover_agent_completion_subscribers=recover,
        )

    assert events == ["barrier", "classify", "reap", "recover", "connect"]


@pytest.mark.asyncio
async def test_startup_fails_closed_without_agent_reconciliation_owner() -> None:
    with pytest.raises(RuntimeError, match="Agent reconciliation owner is unavailable"):
        await runner_lifecycle_subsystems.init_subsystems(
            SimpleNamespace(
                agent_runner=object(),
                agent_lifecycle_monitor=None,
            ),
            AsyncMock(),
            None,
        )


class TestAgentRestartRecoveryHelpers:
    """Tests for agent restart/shutdown recovery helpers."""

    @pytest.mark.asyncio
    async def test_recover_agent_runs_after_restart_rehydrates_completion_event(self) -> None:
        run = SimpleNamespace(
            id="ac314d27-4314-5fe3-a0ab-01645086e137", continuation_prompt="Check the agent result"
        )
        runner = SimpleNamespace(
            agent_runner=SimpleNamespace(
                run_storage=SimpleNamespace(list_active_for_machine=MagicMock(return_value=[run]))
            ),
            pipeline_execution_manager=SimpleNamespace(
                get_completion_subscribers=MagicMock(return_value=["sess-1"]),
            ),
            completion_registry=SimpleNamespace(
                is_registered=MagicMock(return_value=False),
                register=MagicMock(),
            ),
        )

        recovered = await runner_lifecycle._recover_agent_runs_after_restart(
            cast(GobbyRunner, runner)
        )

        assert recovered == 1
        assert runner.completion_registry.register.call_count == 1
        runner.agent_runner.run_storage.list_active_for_machine.assert_called_once_with(
            ANY,
            limit=500,
            offset=0,
        )
        runner.completion_registry.register.assert_called_once_with(
            "ac314d27-4314-5fe3-a0ab-01645086e137",
            subscribers=[],
            continuation_prompt="Check the agent result",
        )
        runner.pipeline_execution_manager.get_completion_subscribers.assert_not_called()

    @pytest.mark.asyncio
    async def test_recover_agent_runs_after_restart_leaves_terminal_sweep_to_startup(self) -> None:
        runner = SimpleNamespace(
            agent_runner=None,
            pipeline_execution_manager=SimpleNamespace(
                remove_completion_subscribers_for_terminal_agent_runs=MagicMock(return_value=2),
            ),
            completion_registry=None,
        )

        recovered = await runner_lifecycle._recover_agent_runs_after_restart(
            cast(GobbyRunner, runner)
        )

        assert recovered == 0
        runner.pipeline_execution_manager.remove_completion_subscribers_for_terminal_agent_runs.assert_not_called()

    @pytest.mark.asyncio
    async def test_rehydrated_agent_completion_event_fires_on_later_notify(self) -> None:
        from gobby.events.completion_registry import CompletionEventRegistry

        registry = CompletionEventRegistry()
        run = SimpleNamespace(id="ac314d27-4314-5fe3-a0ab-01645086e137", continuation_prompt=None)
        runner = SimpleNamespace(
            agent_runner=SimpleNamespace(
                run_storage=SimpleNamespace(list_active_for_machine=MagicMock(return_value=[run]))
            ),
            pipeline_execution_manager=SimpleNamespace(
                get_completion_subscribers=MagicMock(return_value=["sess-1"]),
            ),
            completion_registry=registry,
        )

        rehydrated = await runner_lifecycle._recover_agent_runs_after_restart(
            cast(GobbyRunner, runner)
        )
        waiter = asyncio.create_task(
            registry.wait("ac314d27-4314-5fe3-a0ab-01645086e137", timeout=1.0)
        )

        await registry.notify(
            "ac314d27-4314-5fe3-a0ab-01645086e137",
            {"status": "success", "run_id": "ac314d27-4314-5fe3-a0ab-01645086e137"},
        )

        assert rehydrated == 1
        assert await waiter == {
            "status": "success",
            "run_id": "ac314d27-4314-5fe3-a0ab-01645086e137",
        }

    @pytest.mark.asyncio
    async def test_startup_completion_recovery_rehydrates_active_and_sweeps_terminal(
        self,
    ) -> None:
        from gobby.events.completion_registry import CompletionEventRegistry

        active = SimpleNamespace(id="active-run", continuation_prompt="Inspect result")
        terminal = SimpleNamespace(id="terminal-run", status="success")
        subscriber_manager = MagicMock()
        subscriber_manager.get_completion_subscribers.side_effect = lambda run_id: {
            "active-run": ["active-session"],
            "terminal-run": ["terminal-session"],
        }.get(run_id, [])
        subscriber_manager.list_completion_ids.return_value = ["terminal-run"]
        run_manager = MagicMock()
        run_manager.list_active_for_machine.return_value = [active]
        run_manager.get.return_value = terminal
        wake = AsyncMock(return_value={"ism_persisted": True})
        registry = CompletionEventRegistry(wake_callback=wake)
        runner = cast(
            GobbyRunner,
            SimpleNamespace(
                database=MagicMock(),
                db_executor=None,
                completion_registry=registry,
                wake_dispatcher=SimpleNamespace(wake=wake),
                agent_lifecycle_monitor=None,
                pipeline_execution_manager=None,
            ),
        )

        with (
            patch.object(
                runner_lifecycle_agents,
                "CompletionSubscriberManager",
                return_value=subscriber_manager,
            ),
            patch.object(
                runner_lifecycle_agents,
                "LocalAgentRunManager",
                return_value=run_manager,
            ),
        ):
            recovered = (
                await runner_lifecycle_agents._recover_agent_completion_subscribers_on_startup(
                    runner
                )
            )

        active_delivery = await registry.notify(
            "active-run",
            {"status": "success", "run_id": "active-run"},
            message="Agent active-run completed",
        )

        assert recovered == 2
        assert registry.get_subscribers("active-run") == ["active-session"]
        assert active_delivery == {"active-session": True}
        assert wake.await_count == 2
        wake.assert_any_await(
            "terminal-session",
            "Agent terminal-run reached terminal status success",
            {"status": "success", "run_id": "terminal-run"},
        )
        wake.assert_any_await(
            "active-session",
            "Agent active-run completed",
            {
                "status": "success",
                "run_id": "active-run",
                "continuation_prompt": "Inspect result",
            },
        )
        subscriber_manager.remove_completion_subscribers.assert_called_once_with(
            "terminal-run",
            session_ids=["terminal-session"],
        )

    @pytest.mark.asyncio
    async def test_startup_completion_recovery_sweeps_boot_concurrent_transition(
        self,
    ) -> None:
        from gobby.events.completion_registry import CompletionEventRegistry

        run = SimpleNamespace(
            id="racing-run",
            status="running",
            continuation_prompt="Inspect result",
        )
        subscriber_manager = MagicMock()

        def get_subscribers(_run_id: str) -> list[str]:
            run.status = "success"
            return ["session-1"]

        subscriber_manager.get_completion_subscribers.side_effect = get_subscribers
        subscriber_manager.list_completion_ids.return_value = ["racing-run"]
        run_manager = MagicMock()
        run_manager.list_active_for_machine.return_value = [run]
        run_manager.get.return_value = run
        wake = AsyncMock(return_value={"ism_persisted": True})
        runner = cast(
            GobbyRunner,
            SimpleNamespace(
                database=MagicMock(),
                db_executor=None,
                completion_registry=CompletionEventRegistry(wake_callback=wake),
                wake_dispatcher=SimpleNamespace(wake=wake),
                agent_lifecycle_monitor=None,
                pipeline_execution_manager=None,
            ),
        )

        with (
            patch.object(
                runner_lifecycle_agents,
                "CompletionSubscriberManager",
                return_value=subscriber_manager,
            ),
            patch.object(
                runner_lifecycle_agents,
                "LocalAgentRunManager",
                return_value=run_manager,
            ),
        ):
            recovered = (
                await runner_lifecycle_agents._recover_agent_completion_subscribers_on_startup(
                    runner
                )
            )

        assert recovered == 2
        assert runner.completion_registry.get_subscribers("racing-run") == ["session-1"]
        wake.assert_awaited_once_with(
            "session-1",
            "Agent racing-run reached terminal status success",
            {"status": "success", "run_id": "racing-run"},
        )
        subscriber_manager.remove_completion_subscribers.assert_called_once_with(
            "racing-run",
            session_ids=["session-1"],
        )
        assert subscriber_manager.get_completion_subscribers.call_count == 2

    @pytest.mark.asyncio
    async def test_startup_completion_recovery_is_idempotent_with_monitor_reconciliation(
        self,
    ) -> None:
        from gobby.events.completion_registry import CompletionEventRegistry

        run = SimpleNamespace(id="active-run", continuation_prompt="Inspect result")
        subscriber_manager = MagicMock()
        subscriber_manager.get_completion_subscribers.return_value = ["session-1"]
        subscriber_manager.list_completion_ids.return_value = []
        run_manager = MagicMock()
        run_manager.list_active_for_machine.return_value = [run]
        registry = CompletionEventRegistry()
        runner = cast(
            GobbyRunner,
            SimpleNamespace(
                database=MagicMock(),
                db_executor=None,
                completion_registry=registry,
                wake_dispatcher=SimpleNamespace(wake=AsyncMock()),
                agent_lifecycle_monitor=MagicMock(),
                pipeline_execution_manager=None,
                agent_runner=SimpleNamespace(run_storage=run_manager),
            ),
        )

        with (
            patch.object(
                runner_lifecycle_agents,
                "CompletionSubscriberManager",
                return_value=subscriber_manager,
            ),
            patch.object(
                runner_lifecycle_agents,
                "LocalAgentRunManager",
                return_value=run_manager,
            ),
        ):
            startup_recovered = (
                await runner_lifecycle_agents._recover_agent_completion_subscribers_on_startup(
                    runner
                )
            )
            monitor_recovered = await runner_lifecycle_agents._recover_agent_runs_after_restart(
                runner
            )

        assert startup_recovered == 1
        assert monitor_recovered == 0
        assert registry.get_subscribers("active-run") == ["session-1"]

    @pytest.mark.asyncio
    async def test_startup_completion_recovery_retries_unacknowledged_terminal_row(
        self,
    ) -> None:
        terminal = SimpleNamespace(id="terminal-run", status="error")
        subscriber_manager = MagicMock()
        subscriber_manager.get_completion_subscribers.return_value = ["session-1"]
        subscriber_manager.list_completion_ids.return_value = ["terminal-run"]
        run_manager = MagicMock()
        run_manager.get.return_value = terminal
        wake = AsyncMock(
            side_effect=[
                {"ism_persisted": False, "error_code": "ism_persist_failed"},
                {"ism_persisted": True},
            ]
        )
        runner = SimpleNamespace(
            database=MagicMock(),
            db_executor=None,
            completion_registry=MagicMock(),
            wake_dispatcher=SimpleNamespace(wake=wake),
        )

        with (
            patch.object(
                runner_lifecycle_agents,
                "CompletionSubscriberManager",
                return_value=subscriber_manager,
            ),
            patch.object(
                runner_lifecycle_agents,
                "LocalAgentRunManager",
                return_value=run_manager,
            ),
        ):
            first = await runner_lifecycle_agents._cleanup_terminal_agent_completion_subscribers(
                cast(GobbyRunner, runner)
            )
            second = await runner_lifecycle_agents._cleanup_terminal_agent_completion_subscribers(
                cast(GobbyRunner, runner)
            )

        assert first == 0
        assert second == 1
        subscriber_manager.remove_completion_subscribers.assert_called_once_with(
            "terminal-run",
            session_ids=["session-1"],
        )

    @pytest.mark.asyncio
    async def test_startup_completion_recovery_removes_missing_session_row(self) -> None:
        terminal = SimpleNamespace(id="terminal-run", status="cancelled")
        subscriber_manager = MagicMock()
        subscriber_manager.get_completion_subscribers.return_value = ["deleted-session"]
        subscriber_manager.list_completion_ids.return_value = ["terminal-run"]
        removed: list[tuple[str, list[str]]] = []
        subscriber_manager.remove_completion_subscribers.side_effect = (
            lambda run_id, *, session_ids: removed.append((run_id, session_ids))
        )
        run_manager = MagicMock()
        run_manager.get.return_value = terminal
        wake_calls: list[tuple[str, str, dict[str, str]]] = []

        async def wake(
            session_id: str,
            message: str,
            metadata: dict[str, str],
        ) -> dict[str, str]:
            wake_calls.append((session_id, message, metadata))
            return {"error_code": "session_not_found"}

        runner = SimpleNamespace(
            database=MagicMock(),
            db_executor=None,
            completion_registry=MagicMock(),
            wake_dispatcher=SimpleNamespace(wake=wake),
        )

        with (
            patch.object(
                runner_lifecycle_agents,
                "CompletionSubscriberManager",
                return_value=subscriber_manager,
            ),
            patch.object(
                runner_lifecycle_agents,
                "LocalAgentRunManager",
                return_value=run_manager,
            ),
        ):
            delivered = (
                await runner_lifecycle_agents._cleanup_terminal_agent_completion_subscribers(
                    cast(GobbyRunner, runner)
                )
            )

        assert delivered == 1
        assert wake_calls == [
            (
                "deleted-session",
                "Agent terminal-run reached terminal status cancelled",
                {"status": "cancelled", "run_id": "terminal-run"},
            )
        ]
        assert removed == [("terminal-run", ["deleted-session"])]
        subscriber_manager.get_completion_subscribers.assert_called_once_with("terminal-run")
        run_manager.get.assert_called_once_with("terminal-run")

    @pytest.mark.asyncio
    async def test_startup_terminal_redelivery_skips_parked_daemon_stop_originals(self) -> None:
        """R2-P0-1: parked recovery-pending originals get no false cancellation."""
        genuine = SimpleNamespace(
            id="genuine-run",
            status="error",
            terminal_reason=None,
            resume_metadata_json=None,
        )
        parked = SimpleNamespace(
            id="parked-run",
            status="cancelled",
            terminal_reason="daemon_stop",
            resume_metadata_json={},
        )
        subscriber_manager = MagicMock()
        subscriber_manager.get_completion_subscribers.side_effect = lambda run_id: {
            "genuine-run": ["genuine-session"],
            "parked-run": ["parked-session"],
        }.get(run_id, [])
        subscriber_manager.list_completion_ids.return_value = ["genuine-run", "parked-run"]
        removed: list[tuple[str, list[str]]] = []
        subscriber_manager.remove_completion_subscribers.side_effect = (
            lambda run_id, *, session_ids: removed.append((run_id, session_ids))
        )
        run_manager = MagicMock()
        run_manager.get.side_effect = lambda run_id: {
            "genuine-run": genuine,
            "parked-run": parked,
        }[run_id]
        wake_calls: list[tuple[str, str, dict[str, str]]] = []

        async def wake(
            session_id: str,
            message: str,
            metadata: dict[str, str],
        ) -> dict[str, bool]:
            wake_calls.append((session_id, message, metadata))
            return {"ism_persisted": True}

        runner = SimpleNamespace(
            database=MagicMock(),
            db_executor=None,
            completion_registry=MagicMock(),
            wake_dispatcher=SimpleNamespace(wake=wake),
        )

        with (
            patch.object(
                runner_lifecycle_agents,
                "CompletionSubscriberManager",
                return_value=subscriber_manager,
            ),
            patch.object(
                runner_lifecycle_agents,
                "LocalAgentRunManager",
                return_value=run_manager,
            ),
        ):
            delivered = (
                await runner_lifecycle_agents._cleanup_terminal_agent_completion_subscribers(
                    cast(GobbyRunner, runner)
                )
            )

        assert delivered == 1
        assert wake_calls == [
            (
                "genuine-session",
                "Agent genuine-run reached terminal status error",
                {"status": "error", "run_id": "genuine-run"},
            )
        ]
        assert removed == [("genuine-run", ["genuine-session"])]
        # The parked original is skipped entirely: its durable subscriber rows
        # are retained for the reaper/genuine completion instead of receiving a
        # false cancellation redelivery at startup.
        subscriber_manager.get_completion_subscribers.assert_called_once_with("genuine-run")

    def test_find_live_tmux_by_planned_name_prefers_exact_then_sorted_prefix(self) -> None:
        exact = SimpleNamespace(name="wf-agent")
        suffixed_a = SimpleNamespace(name="wf-agent-aaaa1111")
        suffixed_b = SimpleNamespace(name="wf-agent-bbbb2222")

        assert (
            runner_lifecycle_agents._find_live_tmux_by_planned_name(
                {"wf-agent": exact, "wf-agent-aaaa1111": suffixed_a},
                "wf-agent",
            )
            is exact
        )
        assert (
            runner_lifecycle_agents._find_live_tmux_by_planned_name(
                {"wf-agent-aaaa1111": suffixed_a},
                "wf-agent",
            )
            is suffixed_a
        )
        assert (
            runner_lifecycle_agents._find_live_tmux_by_planned_name(
                {"wf-agent-bbbb2222": suffixed_b, "wf-agent-aaaa1111": suffixed_a},
                "wf-agent",
            )
            is suffixed_a
        )
        assert (
            runner_lifecycle_agents._find_live_tmux_by_planned_name(
                {"other-session": exact, "wf-agent2-aaaa1111": suffixed_a},
                "wf-agent",
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_retry_parked_non_task_resumes_honors_failure_budget(self) -> None:
        missing_metadata = SimpleNamespace(id="run-missing-metadata", resume_metadata_json=None)
        at_cap = SimpleNamespace(
            id="run-at-cap",
            resume_metadata_json={"daemon_stop_resume_failure_count": 3},
        )
        raising = SimpleNamespace(
            id="run-raising",
            resume_metadata_json={"daemon_stop_resume_failure_count": 1},
        )
        succeeding = SimpleNamespace(
            id="run-succeeding",
            resume_metadata_json={"daemon_stop_resume_failure_count": 2},
        )
        run_storage = SimpleNamespace(
            list_parked_non_task_resume_candidates=MagicMock(
                return_value=[missing_metadata, at_cap, raising, succeeding]
            ),
        )
        runner = SimpleNamespace(
            agent_runner=SimpleNamespace(run_storage=run_storage),
            database=MagicMock(),
            db_executor=None,
            session_manager=MagicMock(),
            config=MagicMock(),
            config_runtime=SimpleNamespace(
                capture=static_runtime_capture(DaemonConfig()),
            ),
            completion_registry=MagicMock(),
        )
        resume = AsyncMock(
            side_effect=[
                RuntimeError("resume exploded"),
                SimpleNamespace(success=True, error=None),
            ]
        )
        increment = MagicMock()

        with (
            patch("gobby.agents.resume_executor.resume_agent_run", resume),
            patch(
                "gobby.storage.agent_resume.increment_daemon_resume_failure_count",
                increment,
            ),
        ):
            resumed = await runner_lifecycle_agents._retry_parked_non_task_resumes(
                cast(Any, runner)
            )

        assert resumed == 1
        assert [c.args[0].id for c in resume.await_args_list] == [
            "run-raising",
            "run-succeeding",
        ]
        assert resume.await_args_list[0].kwargs["resume_metadata"] == {
            "daemon_stop_resume_failure_count": 1
        }
        assert resume.await_args_list[0].kwargs["runner"] is runner.agent_runner
        increment.assert_called_once_with(runner.database, run_id="run-raising")

    @pytest.mark.asyncio
    async def test_stop_shutdown_policy_preserves_active_agents(self) -> None:
        class LifecycleMonitor:
            def __init__(self) -> None:
                self.stopped = False

            async def stop(self) -> None:
                self.stopped = True

        monitor = LifecycleMonitor()
        runner = SimpleNamespace(
            agent_lifecycle_monitor=monitor,
            cron_scheduler=None,
            message_processor=None,
            communications_manager=None,
        )
        await runner_lifecycle_shutdown._stop_started_services(
            cast(GobbyRunner, runner),
            shutdown_intent=ShutdownIntent.STOP,
        )

        assert monitor.stopped is True

    @pytest.mark.asyncio
    async def test_restart_shutdown_policy_preserves_active_agents(self) -> None:
        class LifecycleMonitor:
            def __init__(self) -> None:
                self.stopped = False

            async def stop(self) -> None:
                self.stopped = True

        monitor = LifecycleMonitor()
        runner = SimpleNamespace(
            agent_lifecycle_monitor=monitor,
            cron_scheduler=None,
            message_processor=None,
            communications_manager=None,
        )
        await runner_lifecycle_shutdown._stop_started_services(
            cast(GobbyRunner, runner),
            shutdown_intent=ShutdownIntent.RESTART,
        )

        assert monitor.stopped is True

    @pytest.mark.asyncio
    async def test_restart_cron_scheduler_timeout_logs_info(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        runner = SimpleNamespace(
            agent_lifecycle_monitor=None,
            cron_scheduler=SimpleNamespace(stop=AsyncMock()),
            message_processor=None,
            communications_manager=None,
        )

        async def raise_timeout(awaitable, timeout: float):
            awaitable.close()
            raise TimeoutError

        caplog.set_level(logging.INFO, logger="gobby.runner_lifecycle")
        with patch("gobby.runner_lifecycle_shutdown.asyncio.wait_for", side_effect=raise_timeout):
            await runner_lifecycle_shutdown._stop_started_services(
                cast(GobbyRunner, runner),
                shutdown_intent=ShutdownIntent.RESTART,
            )

        assert "Cron scheduler shutdown exceeded timeout during daemon restart" in caplog.text
        assert all(record.levelno < logging.WARNING for record in caplog.records)

    @pytest.mark.asyncio
    async def test_stop_cron_scheduler_timeout_still_warns(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        runner = SimpleNamespace(
            agent_lifecycle_monitor=None,
            cron_scheduler=SimpleNamespace(stop=AsyncMock()),
            message_processor=None,
            communications_manager=None,
        )

        async def raise_timeout(awaitable, timeout: float):
            awaitable.close()
            raise TimeoutError

        caplog.set_level(logging.WARNING, logger="gobby.runner_lifecycle")
        with patch("gobby.runner_lifecycle_shutdown.asyncio.wait_for", side_effect=raise_timeout):
            await runner_lifecycle_shutdown._stop_started_services(
                cast(GobbyRunner, runner),
                shutdown_intent=ShutdownIntent.STOP,
            )

        assert "Cron scheduler shutdown timed out" in caplog.text

    @pytest.mark.asyncio
    async def test_async_shutdown_quiesces_terminal_delivery_before_executor(
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

        async def reap_children(**_kwargs: object) -> None:
            events.append("reap")

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

        await runner_lifecycle_shutdown._run_async_shutdown_cleanup(
            cast(GobbyRunner, SimpleNamespace(db_executor=object())),
            shutdown_intent=ShutdownIntent.STOP,
            reap_remaining_child_processes=reap_children,
            shutdown_telemetry=lambda: events.append("telemetry"),
        )

        assert events[:3] == ["close", "health", "drain"]
        assert events[-1] == "executor"


async def test_restart_preserve_set_uses_and_caches_persisted_tmux_socket() -> None:
    runs = [
        SimpleNamespace(
            id=f"run-{index}",
            pid=1_000 + index,
            tmux_session_name=f"agent-{index}",
            resume_metadata_json={
                "tmux_socket_name": "persisted",
                "tmux_socket_path": "/tmp/persisted.sock",
            },
        )
        for index in range(2)
    ]
    run_db = AsyncMock(return_value=runs)
    runner = SimpleNamespace(
        agent_runner=SimpleNamespace(run_storage=object()),
        db_executor=SimpleNamespace(run=run_db),
    )
    persisted_config = object()
    default_config = SimpleNamespace(
        socket_name="gobby",
        socket_path=None,
        model_copy=MagicMock(return_value=persisted_config),
    )
    default_manager = SimpleNamespace(config=default_config)
    persisted_manager = SimpleNamespace(
        list_sessions=AsyncMock(
            return_value=[
                SimpleNamespace(name=f"agent-{index}", pane_pid=2_000 + index) for index in range(2)
            ]
        )
    )

    with (
        patch(
            "gobby.agents.tmux.get_tmux_session_manager",
            return_value=default_manager,
        ),
        patch(
            "gobby.agents.tmux.session_manager.TmuxSessionManager",
            return_value=persisted_manager,
        ) as manager_type,
    ):
        preserved_pids = await runner_lifecycle_processes._preserved_agent_terminal_pids(
            cast(GobbyRunner, runner)
        )

    assert preserved_pids == {2_000, 2_001}
    assert default_config.model_copy.call_count == 1
    assert default_config.model_copy.call_args == call(
        update={
            "socket_name": "persisted",
            "socket_path": "/tmp/persisted.sock",
        }
    )
    assert manager_type.call_args_list == [call(persisted_config)]
    assert persisted_manager.list_sessions.await_count == 1


async def test_restart_preserve_set_falls_back_to_stored_pids() -> None:
    runs = [
        SimpleNamespace(
            id="lookup-failed",
            pid=1_001,
            tmux_session_name="lookup-failed",
            resume_metadata_json={"tmux_socket_name": "failed"},
        ),
        SimpleNamespace(
            id="pane-pid-unusable",
            pid=1_002,
            tmux_session_name="pane-pid-unusable",
            resume_metadata_json={"tmux_socket_name": "unusable"},
        ),
    ]
    runner = SimpleNamespace(
        agent_runner=SimpleNamespace(run_storage=object()),
        db_executor=SimpleNamespace(run=AsyncMock(return_value=runs)),
    )

    with patch.object(
        runner_lifecycle_processes,
        "_agent_live_sessions_by_name",
        AsyncMock(
            side_effect=[
                None,
                {"pane-pid-unusable": SimpleNamespace(pane_pid=0)},
            ]
        ),
    ):
        preserved_pids = await runner_lifecycle_processes._preserved_agent_terminal_pids(
            cast(GobbyRunner, runner)
        )

    assert preserved_pids == {1_001, 1_002}


async def test_restart_preserve_set_returns_none_when_run_enumeration_fails() -> None:
    run_db = AsyncMock(side_effect=RuntimeError("database unavailable"))
    runner = SimpleNamespace(
        agent_runner=SimpleNamespace(run_storage=object()),
        db_executor=SimpleNamespace(run=run_db),
    )

    preserved_pids = await runner_lifecycle_processes._preserved_agent_terminal_pids(
        cast(GobbyRunner, runner)
    )

    assert preserved_pids is None
    run_db.assert_awaited_once()
    await_args = run_db.await_args
    assert await_args is not None
    assert await_args.args[1] is runner
    assert await_args.kwargs == {"include_fenced": True}
