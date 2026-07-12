"""Runner lifecycle, maintenance, and entrypoint tests."""

import asyncio
import logging
import os
import signal
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack, suppress
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import gobby.runner_lifecycle as runner_lifecycle
import gobby.runner_lifecycle_agents as runner_lifecycle_agents
import gobby.runner_lifecycle_shutdown as runner_lifecycle_shutdown
import gobby.runner_lifecycle_subsystems as runner_lifecycle_subsystems
from gobby.agents.readiness import spawn_readiness_blocker
from gobby.app_context import clear_app_context, get_app_context
from gobby.runner import GobbyRunner, main, run_gobby
from gobby.shutdown_intent import ShutdownIntent
from tests.runner_helpers import create_base_patches

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("fast_stop_hook_grace_window")]


@pytest.fixture(autouse=True)
def _clear_app_context_between_tests() -> Iterator[None]:
    clear_app_context()
    yield
    clear_app_context()


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

            runner = GobbyRunner()

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
                port: int,
                test_mode: bool,
                codex_client: object | None,
            ) -> None:
                http_init.update(
                    services=services,
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
        runner.config = config
        runner.codex_client = None
        runner.text_generation_service = build_daemon_text_generation_service(
            config,
            registry=registry,
        )
        runner.database = object()
        runner.db_executor = None
        runner.session_manager = None
        runner.task_manager = object()
        runner.span_storage = None
        runner.task_sync_manager = None
        runner.memory_sync_manager = None
        runner.memory_manager = None
        runner.llm_service = object()
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
        runner.communications_manager = None
        runner.code_indexer = None
        runner.cron_storage = None
        runner.cron_scheduler = None
        runner.system_automation_loop = None
        runner.skill_manager = None
        runner.hub_manager = None
        runner.config_store = None
        runner.prompt_manager = None
        runner.tool_chat_service = None
        runner._dev_mode = False

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
            patch("gobby.runner_init.servers.set_app_context"),
        ):
            init_servers(runner)

        assert runner.codex_client is fake_client
        assert web_chat_init["codex_client"] is fake_client
        assert http_init["codex_client"] is fake_client
        assert http_init["services"].text_generation_service is runner.text_generation_service
        assert fake_client.start_calls == 0
        assert fake_client.stop_calls == 0
        assert fake_client.archived_thread_ids == []

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
                    falkordb=SimpleNamespace(password=None),
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
    async def test_falkordb_health_failure_clears_memory_graph_refs(self) -> None:
        from gobby.config.persistence import MemoryConfig
        from gobby.memory.manager import MemoryManager
        from gobby.runner_lifecycle_subsystems import _check_external_services

        manager = MemoryManager(db=MagicMock(), config=MemoryConfig(), falkordb_host=None)
        kg_service = MagicMock()
        manager._falkor_client = SimpleNamespace(ping=AsyncMock(return_value=False))
        manager._kg_service = kg_service
        manager._search_service._kg_service = kg_service
        manager._indexing_service._kg_service = kg_service
        runner = SimpleNamespace(
            config=SimpleNamespace(
                databases=SimpleNamespace(
                    qdrant=SimpleNamespace(url=""),
                    falkordb=SimpleNamespace(
                        host="127.0.0.1",
                        port=16379,
                        password="secret",
                    ),
                )
            ),
            memory_manager=manager,
            code_indexer=SimpleNamespace(),
        )

        await _check_external_services(runner, tracker=None)

        assert manager._falkor_client is None
        assert manager._kg_service is None
        assert manager._search_service._kg_service is None
        assert manager._indexing_service._kg_service is None

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
                    falkordb=SimpleNamespace(password=None),
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
        vector_store.ensure_collection.assert_awaited_once_with(
            "tool_embeddings",
            768,
            recreate_on_mismatch=True,
        )
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
    async def test_refresh_provider_model_catalog_awaits_refresh_with_codex_client(
        self,
    ) -> None:
        from gobby.runner_lifecycle_startup import _refresh_provider_model_catalog

        async def refreshed(**_kwargs: object) -> dict[str, dict[str, object]]:
            return {"gemini": {"source": "live"}}

        codex_client = object()
        provider_catalog = SimpleNamespace(refresh=MagicMock(return_value=refreshed()))

        result = await _refresh_provider_model_catalog(provider_catalog, codex_client)

        assert result == {"gemini": {"source": "live"}}
        provider_catalog.refresh.assert_called_once_with(codex_client=codex_client)

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

    async def test_start_failures_do_not_abort_init_and_readiness_is_last(self) -> None:
        events: list[str] = []

        class RecordingServices:
            provider_model_catalog = None
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

        async def message_start() -> None:
            events.append("message-start")
            raise RuntimeError("message failed")

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
            http_server=SimpleNamespace(services=services),
            message_processor=SimpleNamespace(start=AsyncMock(side_effect=message_start)),
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
            patch.object(runner_lifecycle_subsystems, "_schedule_provider_model_refresh"),
            patch.object(runner_lifecycle_subsystems, "_connect_mcp_servers", async_noop),
            patch.object(runner_lifecycle_subsystems, "_check_external_services", async_noop),
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
            "message-start",
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
        assert tracker.errors == [
            {"subsystem": "Message processor", "error": "message failed"},
            {"subsystem": "Session lifecycle manager", "error": "lifecycle failed"},
            {"subsystem": "Cron scheduler", "error": "cron failed"},
        ]
        assert tracker.steps_completed == [
            "Communications manager",
            "System automation loop",
        ]
        assert tracker.done is True
        assert services.startup_ready is True

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
            memory_manager=None,
            vector_store=None,
            mcp_proxy=SimpleNamespace(disconnect_all=AsyncMock()),
            database=SimpleNamespace(close=MagicMock()),
        )

    @pytest.mark.asyncio
    async def test_late_subsystem_init_cannot_activate_after_shutdown_starts(self) -> None:
        services = SimpleNamespace(startup_ready=False, shutdown_in_progress=False)
        runner = SimpleNamespace(http_server=SimpleNamespace(services=services))

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
            patch.object(runner_lifecycle_subsystems, "_schedule_provider_model_refresh"),
            patch.object(runner_lifecycle_subsystems, "_connect_mcp_servers", async_noop),
            patch.object(runner_lifecycle_subsystems, "_check_external_services", async_noop),
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
                cancel_active_agent_runs_for_shutdown=AsyncMock(return_value=0),
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
            cancel_active_agent_runs_for_shutdown=AsyncMock(return_value=0),
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
    async def test_restart_skips_stop_hook_grace(
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
            cancel_active_agent_runs_for_shutdown=AsyncMock(return_value=0),
            reap_remaining_child_processes=AsyncMock(),
            shutdown_telemetry=MagicMock(),
            cleanup_pid_file=MagicMock(),
        )

        grace_window.assert_not_awaited()
        assert server.should_exit is True
        assert marker.exists() is False

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
            cancel_active_agent_runs_for_shutdown=AsyncMock(return_value=0),
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
                cancel_active_agent_runs_for_shutdown=AsyncMock(return_value=0),
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
            preserve_agents=False,
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

        def shutdown_executor(*, wait: bool, cancel_futures: bool = False) -> None:
            assert wait is True
            assert cancel_futures is False
            events.append("db-executor")

        runner.cron_scheduler = SimpleNamespace(stop=fail_cron_stop)
        runner.message_processor = SimpleNamespace(stop=stop_message_processor)
        runner.mcp_proxy.disconnect_all = disconnect_mcp
        runner.db_executor = SimpleNamespace(shutdown=shutdown_executor)
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
            cancel_active_agent_runs_for_shutdown=AsyncMock(return_value=0),
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
                cancel_active_agent_runs_for_shutdown=AsyncMock(return_value=0),
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
    async def test_db_executor_shutdown_timeout_does_not_block_database_close(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner = self._minimal_shutdown_runner(ShutdownIntent.STOP)
        server = SimpleNamespace(should_exit=False)
        executor_shutdown = MagicMock()
        runner.db_executor = SimpleNamespace(shutdown=executor_shutdown)
        cleanup_pid_file = MagicMock()
        to_thread_calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

        async def blocked_to_thread(
            func: object,
            /,
            *args: object,
            **kwargs: object,
        ) -> None:
            to_thread_calls.append((func, args, kwargs))
            await asyncio.Event().wait()

        async def completed_server() -> None:
            return None

        monkeypatch.setattr(runner_lifecycle_shutdown.asyncio, "to_thread", blocked_to_thread)
        monkeypatch.setattr(
            runner_lifecycle_shutdown,
            "_DB_EXECUTOR_SHUTDOWN_TIMEOUT_SECONDS",
            0.01,
        )

        await asyncio.wait_for(
            runner_lifecycle_shutdown.shutdown_daemon_services(
                runner,
                server,
                asyncio.create_task(completed_server()),
                1,
                await_critical_stop_hook_grace_window=AsyncMock(),
                shutdown_websocket_server=AsyncMock(),
                cancel_active_agent_runs_for_shutdown=AsyncMock(return_value=0),
                reap_remaining_child_processes=AsyncMock(),
                shutdown_telemetry=MagicMock(),
                cleanup_pid_file=cleanup_pid_file,
            ),
            timeout=0.25,
        )

        assert to_thread_calls == [
            (executor_shutdown, (), {"wait": True}),
            (executor_shutdown, (), {"wait": False, "cancel_futures": True}),
        ]
        assert server.should_exit is True
        runner.database.close.assert_called_once_with()
        cleanup_pid_file.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_pending_interactions_and_http_sessions_stop_before_uvicorn_exit(self) -> None:
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

        runner.http_server._cleanup_pending_interactions = AsyncMock(side_effect=cleanup_pending)
        runner.http_server._terminate_streamable_http_sessions.side_effect = terminate_sessions

        await runner_lifecycle_shutdown.shutdown_daemon_services(
            runner,
            server,
            server_task,
            1,
            await_critical_stop_hook_grace_window=grace_window,
            shutdown_websocket_server=AsyncMock(),
            cancel_active_agent_runs_for_shutdown=AsyncMock(return_value=0),
            reap_remaining_child_processes=AsyncMock(),
            shutdown_telemetry=MagicMock(),
            cleanup_pid_file=MagicMock(),
        )

        assert events[:3] == ["grace", "pending", "terminate"]
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
            cancel_active_agent_runs_for_shutdown=AsyncMock(return_value=0),
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
            cancel_active_agent_runs_for_shutdown=AsyncMock(return_value=0),
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
                cancel_active_agent_runs_for_shutdown=AsyncMock(return_value=0),
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
                cancel_active_agent_runs_for_shutdown=AsyncMock(return_value=0),
                reap_remaining_child_processes=AsyncMock(),
                shutdown_telemetry=MagicMock(),
                cleanup_pid_file=MagicMock(),
            )

        assert "Lifecycle manager shutdown timed out" in caplog.text
        assert any(
            "Lifecycle manager shutdown timed out" in record.message for record in caplog.records
        )

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

    def test_restart_preserve_set_paginates_every_active_tmux_run(self) -> None:
        run_count = 1_005
        runs = [
            SimpleNamespace(
                id=f"run-{index}",
                pid=10_000 + index,
                tmux_session_name=f"agent-{index}",
            )
            for index in range(run_count)
        ]
        list_active = MagicMock(
            side_effect=lambda *, limit, offset=0: runs[offset : offset + limit]
        )
        runner = SimpleNamespace(
            agent_runner=SimpleNamespace(
                run_storage=SimpleNamespace(list_active=list_active),
            )
        )

        preserved_pids = runner_lifecycle_shutdown._preserved_agent_terminal_pids(runner)

        assert preserved_pids == {10_000 + index for index in range(run_count)}
        assert [invocation.kwargs for invocation in list_active.call_args_list] == [
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
        assert calls[0][1].execution_id == "pe-124"
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
        assert payload.execution_id == "pe-125"
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

    def test_signal_handler_preserves_restart_after_marker_is_consumed(
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
            patch("gobby.runner_maintenance.get_gobby_home", return_value=tmp_path),
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

    def test_signal_handler_recovers_consumed_thirty_second_restart_marker(
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
            patch("gobby.runner_maintenance.get_gobby_home", return_value=tmp_path),
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
        assert not (tmp_path / "shutdown_intent_active.json").exists()

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

            runner = GobbyRunner()

            assert runner.message_processor is not None
            assert runner.message_processor.websocket_server == mock_ws_server
            mock_communications_manager.set_websocket_broadcast.assert_called_once_with(
                mock_ws_server.broadcast
            )


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
            runner = GobbyRunner()

            server = MagicMock()
            server.started = False
            failure = RuntimeError("serve loop crashed before bind")

            async def serve() -> None:
                raise failure

            server.serve = AsyncMock(side_effect=serve)
            pid_claim = MagicMock()

            stack.enter_context(patch("uvicorn.Config"))
            stack.enter_context(patch("uvicorn.Server", return_value=server))
            stack.enter_context(patch("gobby.runner_maintenance.setup_signal_handlers"))
            stack.enter_context(patch("gobby.runner_maintenance.cleanup_pid_file"))
            stack.enter_context(
                patch("gobby.runner_lifecycle.claim_pid_file", return_value=pid_claim)
            )
            init_subsystems = stack.enter_context(patch("gobby.runner_lifecycle._init_subsystems"))
            start_periodic_tasks = stack.enter_context(
                patch("gobby.runner_lifecycle._start_periodic_tasks")
            )
            shutdown_services = stack.enter_context(
                patch("gobby.runner_lifecycle.shutdown_daemon_services")
            )
            stack.enter_context(patch("gobby.runner._healthy_daemon_running", return_value=False))

            with caplog.at_level(logging.ERROR, logger="gobby.runner_lifecycle"):
                with pytest.raises(SystemExit) as exc_info:
                    await asyncio.wait_for(
                        runner_lifecycle.run_daemon(runner),
                        timeout=1.0,
                    )

            assert exc_info.value.code == 1
            assert runner._shutdown_requested is True
            init_subsystems.assert_not_awaited()
            start_periodic_tasks.assert_not_called()
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
            runner = GobbyRunner()

            server = MagicMock()
            server.started = False
            side_effects_started = asyncio.Event()
            failure = RuntimeError("serve loop crashed after bind")

            async def serve() -> None:
                server.started = True
                await side_effects_started.wait()
                raise failure

            server.serve = AsyncMock(side_effect=serve)
            pid_claim = MagicMock()

            stack.enter_context(patch("uvicorn.Config"))
            stack.enter_context(patch("uvicorn.Server", return_value=server))
            stack.enter_context(patch("gobby.runner_maintenance.setup_signal_handlers"))
            stack.enter_context(patch("gobby.runner_maintenance.cleanup_pid_file"))
            stack.enter_context(
                patch("gobby.runner_lifecycle.claim_pid_file", return_value=pid_claim)
            )
            init_subsystems = stack.enter_context(patch("gobby.runner_lifecycle._init_subsystems"))

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
                        runner_lifecycle.run_daemon(runner),
                        timeout=1.0,
                    )

            assert exc_info.value.code == 1
            assert runner._shutdown_requested is True
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


class TestAgentRestartRecoveryHelpers:
    """Tests for agent restart/shutdown recovery helpers."""

    @pytest.mark.asyncio
    async def test_recover_agent_runs_after_restart_rehydrates_completion_event(self) -> None:
        run = SimpleNamespace(
            id="ac314d27-4314-5fe3-a0ab-01645086e137", continuation_prompt="Check the agent result"
        )
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
        runner.agent_runner.run_storage.list_active.assert_called_once_with(limit=500, offset=0)
        runner.completion_registry.register.assert_called_once_with(
            "ac314d27-4314-5fe3-a0ab-01645086e137",
            subscribers=["sess-1"],
            continuation_prompt="Check the agent result",
        )

    @pytest.mark.asyncio
    async def test_recover_agent_runs_after_restart_sweeps_terminal_subscribers(self) -> None:
        runner = SimpleNamespace(
            agent_runner=None,
            pipeline_execution_manager=SimpleNamespace(
                remove_completion_subscribers_for_terminal_agent_runs=MagicMock(return_value=2),
            ),
            completion_registry=None,
        )

        recovered = await runner_lifecycle._recover_agent_runs_after_restart(runner)

        assert recovered == 0
        runner.pipeline_execution_manager.remove_completion_subscribers_for_terminal_agent_runs.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_rehydrated_agent_completion_event_fires_on_later_notify(self) -> None:
        from gobby.events.completion_registry import CompletionEventRegistry

        registry = CompletionEventRegistry()
        run = SimpleNamespace(id="ac314d27-4314-5fe3-a0ab-01645086e137", continuation_prompt=None)
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
    async def test_cancel_active_agent_runs_for_shutdown_kills_and_cancels(self) -> None:
        run = SimpleNamespace(id="ac314d27-4314-5fe3-a0ab-01645086e137")
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
            "ac314d27-4314-5fe3-a0ab-01645086e137",
            terminal_reason="daemon_stop",
        )
        runner.pipeline_execution_manager.remove_completion_subscribers.assert_not_called()
        runner.completion_registry.cleanup.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_shutdown_policy_cancels_active_agents(self) -> None:
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
        cancelled_runners: list[object] = []

        async def cancel_active(active_runner: object) -> int:
            cancelled_runners.append(active_runner)
            return 2

        await runner_lifecycle_shutdown._stop_started_services(
            runner,
            cancel_active,
            shutdown_intent=ShutdownIntent.STOP,
        )

        assert cancelled_runners == [runner]
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
        cancelled_runners: list[object] = []

        async def cancel_active(active_runner: object) -> int:
            cancelled_runners.append(active_runner)
            return 2

        await runner_lifecycle_shutdown._stop_started_services(
            runner,
            cancel_active,
            shutdown_intent=ShutdownIntent.RESTART,
        )

        assert cancelled_runners == []
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
        cancel_active = AsyncMock(return_value=0)

        async def raise_timeout(awaitable, timeout: float):
            awaitable.close()
            raise TimeoutError

        caplog.set_level(logging.INFO, logger="gobby.runner_lifecycle")
        with patch("gobby.runner_lifecycle_shutdown.asyncio.wait_for", side_effect=raise_timeout):
            await runner_lifecycle_shutdown._stop_started_services(
                runner,
                cancel_active,
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
        cancel_active = AsyncMock(return_value=0)

        async def raise_timeout(awaitable, timeout: float):
            awaitable.close()
            raise TimeoutError

        caplog.set_level(logging.WARNING, logger="gobby.runner_lifecycle")
        with patch("gobby.runner_lifecycle_shutdown.asyncio.wait_for", side_effect=raise_timeout):
            await runner_lifecycle_shutdown._stop_started_services(
                runner,
                cancel_active,
                shutdown_intent=ShutdownIntent.STOP,
            )

        assert "Cron scheduler shutdown timed out" in caplog.text
