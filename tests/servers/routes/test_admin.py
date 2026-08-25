import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider

from gobby.config.persistence import DatabasesConfig
from gobby.hooks.runtime_compat import GhookRuntimeDiagnostic, GhookRuntimeState
from gobby.mcp_proxy.models import ConnectionState, HealthState, MCPConnectionHealth
from gobby.servers.routes.admin import create_admin_router, create_health_router
from gobby.shutdown_intent import ShutdownIntent, read_shutdown_intent, write_shutdown_intent
from gobby.telemetry import health_metrics
from gobby.telemetry.health_metrics import (
    configure_health_metrics,
    record_automation_event,
    record_logging_record,
)
from gobby.telemetry.instruments import TelemetryMetrics

pytestmark = pytest.mark.unit


def _hook_runtime_diagnostic(state: GhookRuntimeState) -> GhookRuntimeDiagnostic:
    return GhookRuntimeDiagnostic(
        state=state,
        stamp_path="/tmp/.ghook-runtime.json",
        detail=f"runtime state: {state.value}",
        schema_version=99 if state is GhookRuntimeState.SCHEMA_MISMATCH else 1,
        ghook_version="0.1.0" if state is GhookRuntimeState.STALE_VERSION else "0.7.1",
    )


class RunnerShutdownStub:
    def __init__(self) -> None:
        self._shutdown_requested = False
        self._shutdown_intent: ShutdownIntent | None = None
        self.request_shutdown_calls: list[ShutdownIntent | None] = []

    def request_shutdown(self, intent: ShutdownIntent | None = None) -> None:
        self.request_shutdown_calls.append(intent)
        if intent is not None:
            self._shutdown_intent = intent
        self._shutdown_requested = True


class MinimalRunnerFallbackStub:
    def __init__(self) -> None:
        self._shutdown_requested = False
        self._shutdown_intent: ShutdownIntent | None = None


class TestAdminRoutes:
    @pytest.fixture(autouse=True)
    def reset_restart_state(self):
        import gobby.servers.routes.admin._lifecycle as lifecycle

        lifecycle._restart_lock = None
        yield
        lifecycle._restart_lock = None

    @pytest.fixture
    def mock_server(self):
        server = MagicMock()
        server._start_time = 1234567890.0
        server._running = True
        server.port = 60887
        server.test_mode = False

        # Mock Daemon
        server._daemon = MagicMock()
        server._daemon.status.return_value = {"status": "running"}
        server._daemon.uptime = 100.0

        # Mock Managers
        server.mcp_manager = MagicMock()
        server.mcp_manager.server_configs = []
        server.mcp_manager.health = {}
        server.mcp_manager.connections = {}

        server._internal_manager = MagicMock()
        server._internal_manager.get_all_registries.return_value = []

        server.session_manager = MagicMock()
        server.session_manager.count_by_status.return_value = {"active": 1, "paused": 0}

        server.task_manager = MagicMock()
        server.task_manager.count_by_state.return_value = {"ready": 2}
        server.task_manager.count_ready_tasks.return_value = 1
        server.task_manager.count_blocked_tasks.return_value = 0

        server.memory_manager = MagicMock()
        server.memory_manager.get_stats = AsyncMock(return_value={"total_count": 10})
        server.memory_manager._vector_store = None
        server.memory_manager._falkor_client = None

        server._background_tasks = set()
        server._runner = RunnerShutdownStub()
        server.get_runner = lambda: server._runner
        server.services = SimpleNamespace(
            config=SimpleNamespace(
                databases=DatabasesConfig(),
                hub_backend="postgres",
            ),
            database=MagicMock(),
            db_executor_stats=lambda: None,
            dev_mode=False,
            project_id=None,
        )
        server.config = server.services.config

        async def run_db(func, *args, **kwargs):
            return func(*args, **kwargs)

        server.run_db = AsyncMock(side_effect=run_db)

        # Shutdown support
        server._process_shutdown = AsyncMock()

        return server

    @pytest.fixture
    def client(self, mock_server):
        from fastapi import FastAPI

        app = FastAPI()
        router = create_admin_router(mock_server)
        app.include_router(router)
        return TestClient(app)

    def test_status_endpoint_includes_generation_endpoint_health(self, client, mock_server) -> None:
        response = client.get("/api/admin/status")
        assert response.status_code == 200
        assert response.json()["generation_endpoints"] == []

        cached_snapshot = [
            {
                "name": "vllm",
                "protocol": "vllm",
                "provider_label": "vLLM",
                "wire_api": "chat-completions",
                "api_base": "http://localhost:8321/v1",
                "model": "auto",
                "healthy": True,
                "served_model": "qwen-3b",
                "model_count": 1,
                "error": None,
            }
        ]
        snapshot = MagicMock(return_value=cached_snapshot)
        mock_server.services.generation_endpoint_health = SimpleNamespace(snapshot=snapshot)
        with patch(
            "gobby.servers.local_provider_models.probe_generation_endpoints",
            AsyncMock(side_effect=AssertionError("status route performed endpoint I/O")),
        ) as mock_probe:
            first = client.get("/api/admin/status")
            second = client.get("/api/admin/status")

        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["generation_endpoints"] == cached_snapshot
        assert second.json()["generation_endpoints"] == cached_snapshot
        assert snapshot.call_count == 2
        mock_probe.assert_not_awaited()

    @patch("gobby.servers.routes.admin._health.psutil")
    def test_status_endpoint(self, mock_psutil, client, mock_server) -> None:
        # Mock psutil
        mock_process = MagicMock()
        mock_process.memory_info.return_value = MagicMock(
            rss=1024 * 1024 * 100, vms=1024 * 1024 * 200
        )
        mock_process.num_threads.return_value = 10
        mock_psutil.Process.return_value = mock_process

        mock_process.cpu_percent.return_value = 1.5

        # Mock MCP servers config
        mock_config = MagicMock()
        mock_config.name = "test-server"
        mock_config.enabled = True
        mock_config.transport = "stdio"
        mock_config.tools = []
        pending_config = MagicMock()
        pending_config.name = "pending-server"
        pending_config.enabled = True
        pending_config.transport = "http"
        pending_config.tools = []
        mock_server.mcp_manager.server_configs = [mock_config, pending_config]
        mock_server.mcp_manager.connections = ["test-server"]
        mock_server.mcp_manager.health["test-server"] = MCPConnectionHealth(
            name="test-server",
            state=ConnectionState.CONNECTED,
            health=HealthState.DEGRADED,
            consecutive_failures=3,
            last_error="list_tools timed out after 5s",
        )
        internal_registry = MagicMock()
        internal_registry.name = "gobby-test"
        internal_registry.list_tools.return_value = []
        mock_server._internal_manager.get_all_registries.return_value = [internal_registry]

        with patch(
            "gobby.storage.pipelines.LocalPipelineExecutionManager"
        ) as execution_manager_cls:
            execution_manager_cls.return_value.count_by_status.return_value = {
                "running": 2,
                "waiting_approval": 3,
                "completed": 5,
                "failed": 7,
            }
            response = client.get("/api/admin/status")

        assert response.status_code == 200
        data = response.json()

        assert data["daemon"]["status"] == "running"
        assert data["process"]["cpu_percent"] == 1.5
        assert data["process"]["memory_rss_mb"] == 100.0
        assert "test-server" in data["mcp_servers"]
        assert data["mcp_servers"]["test-server"]["connected"] is True
        assert data["mcp_servers"]["test-server"]["last_error"] == "list_tools timed out after 5s"
        assert data["mcp_servers"]["pending-server"]["last_error"] is None
        assert data["mcp_servers"]["gobby-test"]["last_error"] is None
        assert data["pipelines"] == {
            "running": 2,
            "waiting_approval": 3,
            "completed": 5,
            "failed": 7,
            "total": 17,
        }
        execution_manager_cls.assert_called_once_with(
            db=mock_server.services.database, project_id=None
        )

    @patch("gobby.servers.routes.admin._health.psutil")
    def test_status_endpoint_surfaces_hook_runtime_schema_mismatch(
        self,
        mock_psutil: MagicMock,
        client: TestClient,
    ) -> None:
        mock_process = MagicMock()
        mock_process.memory_info.return_value = MagicMock(rss=0, vms=0)
        mock_process.num_threads.return_value = 1
        mock_psutil.Process.return_value = mock_process
        mock_process.cpu_percent.return_value = 0.0
        diagnostic = _hook_runtime_diagnostic(GhookRuntimeState.SCHEMA_MISMATCH)

        with patch(
            "gobby.servers.routes.admin._health.read_ghook_runtime_diagnostic",
            return_value=diagnostic,
        ):
            response = client.get("/api/admin/status")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "degraded"
        assert data["hook_runtime"]["state"] == "schema_mismatch"
        assert data["hook_runtime"]["compatible"] is False

    @patch("gobby.servers.routes.admin._health.psutil")
    def test_status_endpoint_reads_persistent_shutdown_source(
        self,
        mock_psutil: MagicMock,
        client: TestClient,
        tmp_path: Path,
    ) -> None:
        mock_process = MagicMock()
        mock_process.memory_info.return_value = MagicMock(rss=0, vms=0)
        mock_process.num_threads.return_value = 1
        mock_psutil.Process.return_value = mock_process
        mock_process.cpu_percent.return_value = 0.0
        write_shutdown_intent("cli_restart", ShutdownIntent.RESTART, sender_pid=123, home=tmp_path)
        # Consume the active marker so status must fall back to the persisted source file.
        read_shutdown_intent(home=tmp_path)

        with patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path):
            response = client.get("/api/admin/status")

        assert response.status_code == 200
        assert response.json()["last_shutdown"] == (
            "source=cli_restart, intent=restart, sender_pid=123"
        )

    def test_status_endpoint_logs_file_descriptor_collection_failure(
        self,
        client: TestClient,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with (
            caplog.at_level(logging.DEBUG, logger="gobby.servers.routes.admin._health"),
            patch("resource.getrlimit", side_effect=RuntimeError("fd failure")),
        ):
            response = client.get("/api/admin/status")

        assert response.status_code == 200
        record = next(
            record
            for record in caplog.records
            if record.message == "Could not collect file descriptor usage"
        )
        assert record.exc_info is not None

    def test_status_endpoint_logs_last_shutdown_failure(
        self,
        client: TestClient,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with (
            caplog.at_level(logging.DEBUG, logger="gobby.servers.routes.admin._health"),
            patch("gobby.cli.utils.get_gobby_home", side_effect=RuntimeError("home failure")),
        ):
            response = client.get("/api/admin/status")

        assert response.status_code == 200
        record = next(
            record
            for record in caplog.records
            if record.message == "Could not read the last shutdown source"
        )
        assert record.exc_info is not None

    def test_status_endpoint_logs_agent_stats_failure(
        self,
        client: TestClient,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with (
            caplog.at_level(logging.DEBUG, logger="gobby.servers.routes.admin._health"),
            patch(
                "gobby.storage.agents.LocalAgentRunManager.list_running",
                side_effect=RuntimeError("agent failure"),
            ),
        ):
            response = client.get("/api/admin/status")

        assert response.status_code == 200
        assert response.json()["agents"]["running"] == 0
        record = next(
            record
            for record in caplog.records
            if record.message == "Could not collect running agent count"
        )
        assert record.exc_info is not None

    def test_status_endpoint_logs_database_size_failure(
        self,
        client: TestClient,
        mock_server: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_server.services.database.db_path = "/tmp/gobby.db"
        resolved_path = MagicMock()
        resolved_path.expanduser.return_value = resolved_path
        resolved_path.exists.return_value = True
        resolved_path.stat.side_effect = RuntimeError("stat failure")

        with (
            caplog.at_level(logging.DEBUG, logger="gobby.servers.routes.admin._health"),
            patch("gobby.servers.routes.admin._health.Path", return_value=resolved_path),
        ):
            response = client.get("/api/admin/status")

        assert response.status_code == 200
        record = next(
            record
            for record in caplog.records
            if record.message == "Could not read database file size"
        )
        assert record.exc_info is not None

    @patch("gobby.servers.routes.admin._health.is_qdrant_healthy", new_callable=AsyncMock)
    @patch("gobby.servers.routes.admin._health.psutil")
    def test_status_endpoint_uses_qdrant_service_health_when_url_configured(
        self,
        mock_psutil,
        mock_is_qdrant_healthy,
        client,
        mock_server,
    ) -> None:
        mock_process = MagicMock()
        mock_process.memory_info.return_value = MagicMock(
            rss=1024 * 1024 * 100, vms=1024 * 1024 * 200
        )
        mock_process.num_threads.return_value = 10
        mock_psutil.Process.return_value = mock_process
        mock_process.cpu_percent.return_value = 0.5
        mock_is_qdrant_healthy.return_value = True

        vector_store = MagicMock()
        vector_store._client = None
        vector_store._url = "http://localhost:6333"
        mock_server.memory_manager._vector_store = vector_store

        response = client.get("/api/admin/status")
        assert response.status_code == 200

        data = response.json()
        assert data["memory"]["qdrant"] == {"configured": True, "healthy": True}
        mock_is_qdrant_healthy.assert_awaited_once_with("http://localhost:6333")

    @patch("gobby.servers.routes.admin._health.is_qdrant_healthy", new_callable=AsyncMock)
    @patch("gobby.servers.routes.admin._health.psutil")
    def test_status_endpoint_reports_qdrant_dimension_rebuild_state(
        self,
        mock_psutil,
        mock_is_qdrant_healthy,
        client,
        mock_server,
    ) -> None:
        mock_process = MagicMock()
        mock_process.memory_info.return_value = MagicMock(rss=0, vms=0)
        mock_process.num_threads.return_value = 1
        mock_psutil.Process.return_value = mock_process
        mock_process.cpu_percent.return_value = 0.0
        mock_is_qdrant_healthy.return_value = True

        vector_store = MagicMock()
        vector_store._client = None
        vector_store._url = "http://localhost:6333"
        vector_store.status_snapshot.return_value = {
            "state": "recreated_pending_rebuild",
            "collection": "memories",
            "configured_dimension": 768,
            "rebuild_required": True,
            "dimension_recovery": {
                "action": "recreated",
                "previous_dimension": 384,
                "configured_dimension": 768,
            },
        }
        mock_server.memory_manager._vector_store = vector_store

        response = client.get("/api/admin/status")

        assert response.status_code == 200
        qdrant = response.json()["memory"]["qdrant"]
        assert qdrant["healthy"] is True
        assert qdrant["state"] == "recreated_pending_rebuild"
        assert qdrant["rebuild_required"] is True
        assert qdrant["dimension_recovery"]["previous_dimension"] == 384

    @patch("gobby.cli.services.get_falkordb_status", new_callable=AsyncMock)
    @patch("gobby.servers.routes.admin._health.psutil")
    def test_status_endpoint_reports_falkordb_not_neo4j(
        self,
        mock_psutil,
        mock_get_falkordb_status,
        client,
        mock_server,
    ) -> None:
        mock_process = MagicMock()
        mock_process.memory_info.return_value = MagicMock(
            rss=1024 * 1024 * 100, vms=1024 * 1024 * 200
        )
        mock_process.num_threads.return_value = 10
        mock_psutil.Process.return_value = mock_process
        mock_process.cpu_percent.return_value = 0.5
        mock_get_falkordb_status.return_value = {
            "installed": True,
            "healthy": False,
            "url": "redis://127.0.0.1:16379",
        }
        mock_server.services.config.databases = DatabasesConfig(falkordb={"password": "Valid-123"})
        mock_server.config = mock_server.services.config
        mock_server.memory_manager._falkor_client = None
        mock_server.memory_manager._kg_service = None
        del mock_server.db

        response = client.get("/api/admin/status")

        assert response.status_code == 200
        memory = response.json()["memory"]
        assert "neo4j" not in memory
        assert memory["falkordb"] == {
            "configured": True,
            "installed": True,
            "healthy": False,
            "url": "redis://127.0.0.1:16379",
        }
        mock_get_falkordb_status.assert_awaited_once_with(
            db=mock_server.services.database,
            host="127.0.0.1",
            port=16379,
            password="Valid-123",
        )

    @patch("gobby.cli.services.get_falkordb_status", new_callable=AsyncMock)
    @patch("gobby.servers.routes.admin._health.psutil")
    def test_status_endpoint_always_includes_falkordb_payload(
        self,
        mock_psutil,
        mock_get_falkordb_status,
        client,
        mock_server,
    ) -> None:
        mock_process = MagicMock()
        mock_process.memory_info.return_value = MagicMock(
            rss=1024 * 1024 * 100, vms=1024 * 1024 * 200
        )
        mock_process.num_threads.return_value = 10
        mock_psutil.Process.return_value = mock_process
        mock_process.cpu_percent.return_value = 0.5
        mock_get_falkordb_status.return_value = {
            "installed": False,
            "healthy": False,
            "url": None,
        }
        mock_server.memory_manager = None

        response = client.get("/api/admin/status")

        assert response.status_code == 200
        assert response.json()["memory"]["falkordb"] == {
            "configured": False,
            "installed": False,
            "healthy": False,
            "url": None,
        }

    @patch("gobby.cli.installers.postgres.get_postgres_status", new_callable=AsyncMock)
    @patch("gobby.servers.routes.admin._health.psutil")
    def test_status_endpoint_includes_postgres_hub_status(
        self,
        mock_psutil,
        mock_get_postgres_status,
        client,
        mock_server,
    ) -> None:
        mock_process = MagicMock()
        mock_process.memory_info.return_value = MagicMock(
            rss=1024 * 1024 * 100, vms=1024 * 1024 * 200
        )
        mock_process.num_threads.return_value = 10
        mock_psutil.Process.return_value = mock_process
        mock_process.cpu_percent.return_value = 0.5
        mock_server.services.database.dialect = "postgres"
        mock_server.services.database.connection_count = 2
        mock_server.services.config.hub_backend = "postgres"
        mock_get_postgres_status.return_value = {
            "dsn_host": "localhost",
            "dsn_db": "gobby",
            "healthy": True,
        }

        response = client.get("/api/admin/status")
        assert response.status_code == 200

        data = response.json()
        assert data["database"]["backend"] == "postgres"
        assert "mode" not in data["postgres"]
        assert data["postgres"]["healthy"] is True
        mock_get_postgres_status.assert_awaited_once_with(
            readiness_timeout=1.5,
            connect_timeout=1,
        )

    @patch("gobby.cli.installers.postgres.get_postgres_status", new_callable=AsyncMock)
    @patch("gobby.servers.routes.admin._health.psutil")
    def test_status_endpoint_degrades_for_damaged_bm25_index(
        self,
        mock_psutil,
        mock_get_postgres_status,
        client,
        mock_server,
    ) -> None:
        mock_process = MagicMock()
        mock_process.memory_info.return_value = MagicMock(
            rss=1024 * 1024 * 100, vms=1024 * 1024 * 200
        )
        mock_process.num_threads.return_value = 10
        mock_psutil.Process.return_value = mock_process
        mock_process.cpu_percent.return_value = 0.5
        mock_server.services.database.dialect = "postgres"
        mock_server.services.config.hub_backend = "postgres"
        mock_get_postgres_status.return_value = {
            "healthy": True,
            "code_index": {
                "healthy": False,
                "repair_command": "gobby postgres repair-code-index",
                "indexes": [
                    {
                        "name": "public.code_symbols_search_bm25",
                        "state": "damaged",
                        "repaired": False,
                        "checks": [],
                        "error": "invalid chunk style tag: 254",
                    }
                ],
            },
        }

        response = client.get("/api/admin/status")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "degraded"
        assert data["postgres"]["code_index"]["healthy"] is False

    @patch("gobby.servers.routes.admin._health.get_all_metrics")
    @patch("gobby.servers.routes.admin._health.generate_latest")
    @patch("gobby.servers.routes.admin._health.psutil")
    def test_metrics_endpoint(self, mock_psutil, mock_generate, mock_get_all, client) -> None:
        mock_generate.return_value = b"metric_name 1.0\n"
        mock_get_all.return_value = {"counters": {}, "gauges": {}, "histograms": {}}

        # Mock psutil for daemon metrics
        mock_process = MagicMock()
        mock_process.memory_info.return_value = MagicMock(rss=1000)
        mock_process.cpu_percent.return_value = 0.5
        mock_psutil.Process.return_value = mock_process

        response = client.get("/api/admin/metrics")
        assert response.status_code == 200
        assert response.text == "metric_name 1.0\n"
        assert "text/plain" in response.headers["content-type"]

    def test_metrics_endpoint_exposes_logging_and_automation_health(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        reader = PrometheusMetricReader()
        provider = MeterProvider(metric_readers=[reader])
        collector = TelemetryMetrics(provider.get_meter("admin-health-test"))
        monkeypatch.setattr(health_metrics, "get_telemetry_metrics", lambda: collector)
        configure_health_metrics(enabled=True)
        try:
            record_logging_record("daemon", "WARNING")
            record_automation_event("cron", "fired")
            with (
                patch("gobby.servers.routes.admin._health.update_daemon_metrics"),
                patch("gobby.servers.routes.admin._health.set_gauge"),
            ):
                response = client.get("/api/admin/metrics")
        finally:
            configure_health_metrics(enabled=False)
            provider.shutdown()

        assert response.status_code == 200
        assert 'logging_records_total{severity="WARNING",surface="daemon"} 1.0' in response.text
        assert 'automation_events_total{component="cron",outcome="fired"} 1.0' in response.text

    @patch("gobby.servers.routes.admin._config.get_version")
    def test_config_endpoint(self, mock_get_version, client) -> None:
        mock_get_version.return_value = "1.0.0"

        response = client.get("/api/admin/config")
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "success"
        assert data["config"]["server"]["version"] == "1.0.0"
        assert data["config"]["features"]["session_manager"] is True

    def test_shutdown_endpoint(self, client, mock_server) -> None:
        with patch("gobby.runner_maintenance.write_shutdown_source") as mock_write_shutdown:
            response = client.post("/api/admin/shutdown")
        assert response.status_code == 200
        assert response.json() == {
            "status": "shutting_down",
            "message": "Graceful shutdown initiated",
            "response_time_ms": response.json()["response_time_ms"],  # ignore value
        }
        mock_write_shutdown.assert_called_once_with("http_shutdown", intent="stop")
        assert mock_server._runner._shutdown_requested is True
        assert mock_server._runner._shutdown_intent is ShutdownIntent.STOP
        assert mock_server._runner.request_shutdown_calls == [ShutdownIntent.STOP]

        mock_server._process_shutdown.assert_not_called()
        assert mock_server._background_tasks == set()

    def test_shutdown_endpoint_without_runner_schedules_process_shutdown(
        self, client, mock_server
    ) -> None:
        mock_server._runner = None

        with patch("gobby.runner_maintenance.write_shutdown_source") as mock_write_shutdown:
            response = client.post("/api/admin/shutdown")

        assert response.status_code == 200
        assert response.json()["status"] == "shutting_down"
        mock_write_shutdown.assert_called_once_with("http_shutdown", intent="stop")
        mock_server._process_shutdown.assert_called_once()

    def test_shutdown_endpoint_returns_500_when_shutdown_fails(self, client) -> None:
        with patch(
            "gobby.runner_maintenance.write_shutdown_source",
            side_effect=RuntimeError("write failed"),
        ):
            response = client.post("/api/admin/shutdown")

        assert response.status_code == 500
        assert response.json() == {
            "status": "error",
            "message": "Shutdown failed to initiate",
        }

    def test_request_runner_shutdown_rejects_runner_without_shutdown_api(self) -> None:
        from gobby.servers.routes.admin._lifecycle import _request_runner_shutdown

        runner = MinimalRunnerFallbackStub()
        requested = _request_runner_shutdown(
            SimpleNamespace(_runner=runner),
            ShutdownIntent.RESTART,
        )

        assert requested is False
        assert runner._shutdown_requested is False
        assert runner._shutdown_intent is None

    @patch("gobby.servers.routes.admin._lifecycle.os.getpid", return_value=4321)
    @patch(
        "gobby.servers.routes.admin._lifecycle._should_restart_via_service_manager",
        return_value=False,
    )
    @patch("gobby.servers.routes.admin._lifecycle.subprocess.Popen")
    def test_restart_endpoint_uses_direct_helper(
        self,
        mock_popen,
        _mock_service_mode,
        _mock_getpid,
        client,
        mock_server,
    ) -> None:
        import gobby.servers.routes.admin._lifecycle as lifecycle

        with patch("gobby.runner_maintenance.write_shutdown_source") as mock_write_shutdown:
            response = client.post("/api/admin/restart")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "restarting"
        assert data["message"] == "Daemon restart initiated"
        assert "response_time_ms" in data

        mock_popen.assert_called_once()
        command = mock_popen.call_args.args[0]
        assert command == [sys.executable, "-c", lifecycle._DIRECT_RESTART_HELPER, "4321"]
        mock_write_shutdown.assert_called_once_with("http_restart", intent="restart")
        assert mock_server._runner._shutdown_requested is True
        assert mock_server._runner._shutdown_intent is ShutdownIntent.RESTART
        assert mock_server._runner.request_shutdown_calls == [ShutdownIntent.RESTART]

        mock_server._process_shutdown.assert_not_called()
        assert mock_server._background_tasks == set()

    @patch("gobby.servers.routes.admin._lifecycle.os.getpid", return_value=4321)
    @patch(
        "gobby.servers.routes.admin._lifecycle._should_restart_via_service_manager",
        return_value=True,
    )
    @patch("gobby.servers.routes.admin._lifecycle.subprocess.Popen")
    def test_restart_endpoint_uses_service_helper(
        self,
        mock_popen,
        _mock_service_mode,
        _mock_getpid,
        client,
        mock_server,
    ) -> None:
        import gobby.servers.routes.admin._lifecycle as lifecycle

        with patch("gobby.runner_maintenance.write_shutdown_source") as mock_write_shutdown:
            response = client.post("/api/admin/restart")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "restarting"
        assert data["message"] == "Daemon restart initiated"
        assert "response_time_ms" in data

        mock_popen.assert_called_once()
        command = mock_popen.call_args.args[0]
        assert command == [
            sys.executable,
            "-c",
            lifecycle._SERVICE_RESTART_HELPER,
            "4321",
            str(mock_server.port),
            "http_restart",
        ]
        mock_write_shutdown.assert_called_once_with("http_restart", intent="restart")
        assert mock_server._runner._shutdown_requested is True
        assert mock_server._runner._shutdown_intent is ShutdownIntent.RESTART
        assert mock_server._runner.request_shutdown_calls == [ShutdownIntent.RESTART]

        mock_server._process_shutdown.assert_not_called()
        assert mock_server._background_tasks == set()

    @patch("gobby.servers.routes.admin._lifecycle.os.getpid", return_value=4321)
    @patch(
        "gobby.servers.routes.admin._lifecycle._should_restart_via_service_manager",
        return_value=False,
    )
    @patch("gobby.servers.routes.admin._lifecycle.subprocess.Popen")
    def test_restart_endpoint_without_runner_schedules_process_shutdown(
        self,
        mock_popen,
        _mock_service_mode,
        _mock_getpid,
        client,
        mock_server,
    ) -> None:
        mock_server._runner = None

        with patch("gobby.runner_maintenance.write_shutdown_source") as mock_write_shutdown:
            response = client.post("/api/admin/restart")

        assert response.status_code == 200
        assert response.json()["status"] == "restarting"
        mock_popen.assert_called_once()
        mock_write_shutdown.assert_called_once_with("http_restart", intent="restart")
        mock_server._process_shutdown.assert_called_once()

    @patch("gobby.servers.routes.admin._lifecycle._spawn_restart_helper")
    @patch(
        "gobby.servers.routes.admin._lifecycle.asyncio.to_thread",
        new_callable=AsyncMock,
        return_value=False,
    )
    @patch("gobby.servers.routes.admin._lifecycle._should_restart_via_service_manager")
    def test_restart_endpoint_offloads_service_manager_probe(
        self,
        mock_service_mode,
        mock_to_thread,
        _mock_spawn,
        client,
    ) -> None:
        with patch("gobby.runner_maintenance.write_shutdown_source"):
            response = client.post("/api/admin/restart")

        payload = response.json()
        assert response.status_code == 200
        assert payload["status"] == "restarting"
        assert payload["message"] == "Daemon restart initiated"
        assert payload["response_time_ms"] >= 0
        mock_to_thread.assert_awaited_once_with(mock_service_mode)

    @patch(
        "gobby.servers.routes.admin._lifecycle.asyncio.to_thread",
        new_callable=AsyncMock,
        return_value=False,
    )
    def test_restart_endpoint_starts_shutdown_before_spawning_helper(
        self,
        _mock_to_thread,
        client,
    ) -> None:
        events: list[str] = []

        with (
            patch(
                "gobby.runner_maintenance.write_shutdown_source",
                side_effect=lambda *_args, **_kwargs: events.append("write_shutdown_source"),
            ),
            patch(
                "gobby.servers.routes.admin._lifecycle._request_runner_shutdown",
                side_effect=lambda *_args: events.append("request_runner_shutdown") or True,
            ),
            patch(
                "gobby.servers.routes.admin._lifecycle._spawn_restart_helper",
                side_effect=lambda **_kwargs: events.append("spawn_restart_helper"),
            ),
        ):
            response = client.post("/api/admin/restart")

        assert response.json()["status"] == "restarting"
        assert events == [
            "write_shutdown_source",
            "request_runner_shutdown",
            "spawn_restart_helper",
        ]

    @patch("gobby.servers.routes.admin._lifecycle._spawn_restart_helper")
    @patch(
        "gobby.servers.routes.admin._lifecycle.asyncio.to_thread",
        new_callable=AsyncMock,
        return_value=False,
    )
    def test_restart_endpoint_releases_lock_when_shutdown_not_initiated(
        self,
        _mock_to_thread,
        mock_spawn,
        client,
    ) -> None:
        with (
            patch("gobby.runner_maintenance.write_shutdown_source"),
            patch(
                "gobby.servers.routes.admin._lifecycle._request_runner_shutdown",
                side_effect=[RuntimeError("shutdown failed"), True],
            ),
        ):
            failed_response = client.post("/api/admin/restart")
            retry_response = client.post("/api/admin/restart")

        assert failed_response.json()["status"] == "error"
        assert retry_response.json()["status"] == "restarting"
        mock_spawn.assert_called_once()

    @patch(
        "gobby.servers.routes.admin._lifecycle._should_restart_via_service_manager",
        return_value=False,
    )
    @patch("gobby.servers.routes.admin._lifecycle.subprocess.Popen")
    def test_restart_endpoint_double_restart_guard(
        self,
        mock_popen,
        _mock_service_mode,
        client,
        mock_server,
    ) -> None:
        # First restart should succeed
        with patch("gobby.runner_maintenance.write_shutdown_source") as mock_write_shutdown:
            response1 = client.post("/api/admin/restart")
            response2 = client.post("/api/admin/restart")
        assert response1.json()["status"] == "restarting"

        # Second restart should be rejected
        assert response2.json()["status"] == "already_restarting"
        mock_write_shutdown.assert_called_once_with("http_restart", intent="restart")
        mock_popen.assert_called_once()


class TestAdminRestartHelpers:
    @patch("gobby.servers.routes.admin._lifecycle._append_restart_helper_log")
    @patch("gobby.cli.installers.service.service_restart")
    @patch("gobby.cli.daemon._wait_for_daemon_health")
    @patch("gobby.servers.routes.admin._lifecycle._wait_for_process_exit", return_value=True)
    def test_service_restart_helper_invokes_service_restart_when_needed(
        self,
        _mock_wait_for_exit,
        mock_wait_for_health,
        mock_service_restart,
        mock_log,
    ) -> None:
        import gobby.servers.routes.admin._lifecycle as lifecycle

        mock_wait_for_health.side_effect = [None, 0.5]
        mock_service_restart.return_value = {"success": True, "method": "launchctl"}

        lifecycle._run_service_restart_helper(4321, 60887, "http_restart")

        mock_service_restart.assert_called_once_with(shutdown_source="http_restart")
        assert mock_service_restart.call_count == 1
        assert mock_service_restart.call_args is not None
        assert mock_wait_for_health.call_count == 2
        mock_log.assert_not_called()
        assert mock_log.call_count == 0
        assert not mock_log.called


class TestHealthEndpoint:
    """Tests for GET /admin/health."""

    @pytest.fixture
    def mock_server(self):
        server = MagicMock()
        server.test_mode = False
        return server

    @pytest.fixture
    def client(self, mock_server):
        from fastapi import FastAPI

        app = FastAPI()
        router = create_admin_router(mock_server)
        app.include_router(router)
        app.include_router(create_health_router(mock_server))
        return TestClient(app)

    @pytest.mark.parametrize(
        ("runtime_state", "expected_health"),
        [
            (GhookRuntimeState.ABSENT, "ok"),
            (GhookRuntimeState.COMPATIBLE, "ok"),
            (GhookRuntimeState.MALFORMED, "degraded"),
            (GhookRuntimeState.SCHEMA_MISMATCH, "degraded"),
            (GhookRuntimeState.STALE_VERSION, "degraded"),
        ],
    )
    def test_health_surfaces_typed_hook_runtime_state(
        self,
        client: TestClient,
        runtime_state: GhookRuntimeState,
        expected_health: str,
    ) -> None:
        diagnostic = _hook_runtime_diagnostic(runtime_state)

        with patch(
            "gobby.servers.routes.admin._health.read_ghook_runtime_diagnostic",
            return_value=diagnostic,
        ) as mock_read:
            response = client.get("/api/health")

        assert response.status_code == 200
        assert response.json()["status"] == expected_health
        assert response.json()["hook_runtime"]["state"] == runtime_state.value
        # Read once per probe, and read inline: liveness must not wait on a
        # slot in the shared default executor (#20839).
        mock_read.assert_called_once_with()

    def test_health_surfaces_forced_runner_init_failure(
        self,
        client: TestClient,
        mock_server: MagicMock,
    ) -> None:
        from gobby.runner_init.services import _init_llm_service

        runner = SimpleNamespace(config=SimpleNamespace())
        mock_server.get_runner.return_value = runner

        with patch(
            "gobby.runner_init.services.build_daemon_text_generation_service",
            side_effect=RuntimeError("forced init failure"),
        ):
            _init_llm_service(runner)

        response = client.get("/api/health")

        assert response.status_code == 200
        assert response.json()["status"] == "degraded"
        assert response.json()["degraded_services"] == ["llm_service"]


class TestWorkflowsReloadEndpoint:
    """Tests for POST /admin/workflows/reload."""

    @pytest.fixture
    def mock_server(self):
        server = MagicMock()
        server.test_mode = False
        server._background_tasks = set()

        # Internal manager with workflows registry
        workflows_registry = MagicMock()
        workflows_registry.name = "gobby-workflows"
        workflows_registry.call = AsyncMock(return_value={"reloaded": 5})

        server._internal_manager = MagicMock()
        server._internal_manager.get_all_registries.return_value = [workflows_registry]

        return server

    @pytest.fixture
    def client(self, mock_server):
        from fastapi import FastAPI

        app = FastAPI()
        router = create_admin_router(mock_server)
        app.include_router(router)
        return TestClient(app)

    def test_reload_workflows_success(self, client) -> None:
        response = client.post("/api/admin/workflows/reload")
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "success"
        assert data["message"] == "Workflow cache reloaded"
        assert data["details"] == {"reloaded": 5}
        assert "response_time_ms" in data

    def test_reload_workflows_forwards_project_scope(self, client, mock_server) -> None:
        registry = mock_server._internal_manager.get_all_registries.return_value[0]

        response = client.post(
            "/api/admin/workflows/reload",
            params={"project_path": "/tmp/project", "project_id": "project-id"},
        )

        assert response.status_code == 200
        registry.call.assert_awaited_once_with(
            "reload_cache",
            {"project_path": "/tmp/project", "project_id": "project-id"},
        )

    def test_reload_workflows_no_registry(self, client, mock_server) -> None:
        # Return registries that don't include gobby-workflows
        other_registry = MagicMock()
        other_registry.name = "gobby-tasks"
        mock_server._internal_manager.get_all_registries.return_value = [other_registry]

        response = client.post("/api/admin/workflows/reload")
        assert response.status_code == 503
        data = response.json()

        assert data["status"] == "error"
        assert data["message"] == "Workflow registry not available"

    def test_reload_workflows_no_internal_manager(self, client, mock_server) -> None:
        mock_server._internal_manager = None

        response = client.post("/api/admin/workflows/reload")
        assert response.status_code == 503
        data = response.json()

        assert data["status"] == "error"
        assert data["message"] == "Workflow registry not available"

    def test_reload_workflows_tool_not_found(self, client, mock_server) -> None:
        registry = mock_server._internal_manager.get_all_registries.return_value[0]
        registry.call = AsyncMock(side_effect=ValueError("Tool not found"))

        response = client.post("/api/admin/workflows/reload")
        assert response.status_code == 503
        data = response.json()

        assert data["status"] == "error"
        assert data["message"] == "reload_cache tool not found"

    def test_reload_workflows_call_exception(self, client, mock_server) -> None:
        registry = mock_server._internal_manager.get_all_registries.return_value[0]
        registry.call = AsyncMock(side_effect=RuntimeError("Cache corrupted"))

        response = client.post("/api/admin/workflows/reload")
        assert response.status_code == 500
        data = response.json()

        assert data["status"] == "error"
        assert "Failed to reload cache" in data["message"]

    def test_reload_workflows_manager_exception(
        self,
        client: TestClient,
        mock_server: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_server._internal_manager.get_all_registries.side_effect = RuntimeError(
            "Manager unavailable"
        )
        caplog.set_level("ERROR", logger="gobby.servers.routes.admin._lifecycle")

        response = client.post("/api/admin/workflows/reload")
        assert response.status_code == 500
        data = response.json()

        assert data["status"] == "error"
        assert data["message"] == "Failed to reload workflows"
        record = next(
            record
            for record in caplog.records
            if record.getMessage() == "Error reloading workflows"
        )
        assert record.__dict__["error"] == "Manager unavailable"


class TestTestEndpoints:
    """Tests for /admin/test/* endpoints (E2E test-mode only)."""

    @pytest.fixture
    def mock_server(self):
        server = MagicMock()
        server.test_mode = True
        server._background_tasks = set()

        # Session manager with db
        server.session_manager = MagicMock()
        server.session_manager.db = MagicMock()
        server.session_manager.update_usage.return_value = True

        return server

    @pytest.fixture
    def client(self, mock_server):
        from fastapi import FastAPI

        app = FastAPI()
        router = create_admin_router(mock_server)
        app.include_router(router)
        return TestClient(app)

    # --- register-project ---

    @patch("gobby.storage.projects.LocalProjectManager")
    def test_register_project_success(self, mock_pm_cls, client, mock_server) -> None:
        mock_server.session_manager.db.execute.return_value.fetchone.return_value = {"id": "proj-1"}

        response = client.post(
            "/api/admin/test/register-project",
            json={"project_id": "proj-1", "name": "Test Project"},
        )
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "success"
        assert data["project_id"] == "proj-1"
        assert data["name"] == "Test Project"
        assert "response_time_ms" in data
        sql = mock_server.session_manager.db.execute.call_args.args[0]
        assert "ON CONFLICT (id) DO NOTHING" in sql
        assert "RETURNING id" in sql
        mock_pm_cls.return_value.get.assert_not_called()

    @patch("gobby.storage.projects.LocalProjectManager")
    def test_register_project_already_exists(self, mock_pm_cls, client, mock_server) -> None:
        existing = MagicMock()
        existing.id = "proj-1"
        existing.name = "Existing"

        mock_server.session_manager.db.execute.return_value.fetchone.return_value = None
        mock_pm = MagicMock()
        mock_pm.get.return_value = existing
        mock_pm_cls.return_value = mock_pm

        response = client.post(
            "/api/admin/test/register-project",
            json={"project_id": "proj-1", "name": "Test"},
        )
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "already_exists"
        assert data["project_id"] == "proj-1"
        assert data["name"] == "Existing"
        mock_pm.get.assert_called_once_with("proj-1")

    def test_register_project_forbidden_when_not_test_mode(self, mock_server) -> None:
        mock_server.test_mode = False

        from fastapi import FastAPI

        app = FastAPI()
        router = create_admin_router(mock_server)
        app.include_router(router)
        client = TestClient(app)

        response = client.post(
            "/api/admin/test/register-project",
            json={"project_id": "proj-1", "name": "Test"},
        )
        assert response.status_code == 403
        assert "test mode" in response.json()["detail"].lower()

    @patch("gobby.storage.projects.LocalProjectManager")
    def test_register_project_no_session_manager(self, mock_pm_cls, client, mock_server) -> None:
        mock_server.session_manager = None

        response = client.post(
            "/api/admin/test/register-project",
            json={"project_id": "proj-1", "name": "Test"},
        )
        # HTTPException(503) caught by generic except → re-raised as 500
        assert response.status_code == 500

    # --- register-agent ---

    @patch("gobby.storage.agents.LocalAgentRunManager")
    def test_register_agent_success(
        self,
        mock_arm_cls: MagicMock,
        client: TestClient,
    ) -> None:
        mock_arm = MagicMock()
        mock_run = MagicMock()
        mock_run.to_dict.return_value = {
            "run_id": "run-1",
            "id": "run-1",
            "session_id": "sess-1",
            "parent_session_id": "parent-1",
            "mode": "interactive",
        }
        mock_arm.get.return_value = mock_run
        mock_arm_cls.return_value = mock_arm

        response = client.post(
            "/api/admin/test/register-agent",
            json={
                "run_id": "run-1",
                "session_id": "sess-1",
                "parent_session_id": "parent-1",
                "mode": "interactive",
            },
        )
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "success"
        assert data["agent"]["run_id"] == "run-1"
        mock_arm.create.assert_called_once()
        mock_arm.start.assert_called_once_with("run-1")

    def test_register_agent_forbidden_when_not_test_mode(self, mock_server) -> None:
        mock_server.test_mode = False

        from fastapi import FastAPI

        app = FastAPI()
        router = create_admin_router(mock_server)
        app.include_router(router)
        client = TestClient(app)

        response = client.post(
            "/api/admin/test/register-agent",
            json={
                "run_id": "run-1",
                "session_id": "sess-1",
                "parent_session_id": "parent-1",
            },
        )
        assert response.status_code == 403

    # --- unregister-agent ---

    @patch(
        "gobby.agents.terminal_delivery.deliver_existing_terminal_run",
        new_callable=AsyncMock,
    )
    @patch("gobby.storage.agents.LocalAgentRunManager")
    def test_unregister_agent_success(
        self,
        mock_arm_cls: MagicMock,
        mock_deliver_terminal_run: AsyncMock,
        client: TestClient,
    ) -> None:
        mock_arm = MagicMock()
        mock_arm.get.return_value = MagicMock()  # agent found
        mock_arm_cls.return_value = mock_arm

        response = client.delete("/api/admin/test/unregister-agent/run-1")
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "success"
        assert "run-1" in data["message"]
        mock_arm.fail.assert_called_once_with("run-1", error="Unregistered via test endpoint")
        mock_deliver_terminal_run.assert_awaited_once()

    @patch("gobby.storage.agents.LocalAgentRunManager")
    def test_unregister_agent_not_found(
        self,
        mock_arm_cls: MagicMock,
        client: TestClient,
    ) -> None:
        mock_arm = MagicMock()
        mock_arm.get.return_value = None  # agent not found
        mock_arm_cls.return_value = mock_arm

        response = client.delete("/api/admin/test/unregister-agent/run-nonexistent")
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "not_found"

    def test_unregister_agent_forbidden_when_not_test_mode(self, mock_server) -> None:
        mock_server.test_mode = False

        from fastapi import FastAPI

        app = FastAPI()
        router = create_admin_router(mock_server)
        app.include_router(router)
        client = TestClient(app)

        response = client.delete("/api/admin/test/unregister-agent/run-1")
        assert response.status_code == 403

    # --- set-session-usage ---

    def test_set_session_usage_success(self, client, mock_server) -> None:
        mock_server.session_manager.update_usage.return_value = True

        response = client.post(
            "/api/admin/test/set-session-usage",
            json={
                "session_id": "sess-1",
                "input_tokens": 1000,
                "output_tokens": 500,
                "cache_creation_tokens": 200,
                "cache_read_tokens": 100,
            },
        )
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "success"
        assert data["session_id"] == "sess-1"
        assert data["usage_set"]["input_tokens"] == 1000
        assert data["usage_set"]["output_tokens"] == 500

        mock_server.session_manager.update_usage.assert_called_once_with(
            session_id="sess-1",
            input_tokens=1000,
            output_tokens=500,
            cache_creation_tokens=200,
            cache_read_tokens=100,
        )

    def test_set_session_usage_not_found(self, client, mock_server) -> None:
        mock_server.session_manager.update_usage.return_value = False

        response = client.post(
            "/api/admin/test/set-session-usage",
            json={"session_id": "nonexistent"},
        )
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "not_found"
        assert "nonexistent" in data["message"]

    def test_set_session_usage_defaults(self, client, mock_server) -> None:
        """When only session_id is provided, defaults should be zero."""
        mock_server.session_manager.update_usage.return_value = True

        response = client.post(
            "/api/admin/test/set-session-usage",
            json={"session_id": "sess-1"},
        )
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "success"
        assert data["usage_set"]["input_tokens"] == 0
        assert data["usage_set"]["output_tokens"] == 0

    def test_set_session_usage_forbidden_when_not_test_mode(self, mock_server) -> None:
        mock_server.test_mode = False

        from fastapi import FastAPI

        app = FastAPI()
        router = create_admin_router(mock_server)
        app.include_router(router)
        client = TestClient(app)

        response = client.post(
            "/api/admin/test/set-session-usage",
            json={"session_id": "sess-1"},
        )
        assert response.status_code == 403

    def test_set_session_usage_no_session_manager(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        mock_server.session_manager = None

        response = client.post(
            "/api/admin/test/set-session-usage",
            json={"session_id": "sess-1"},
        )
        # HTTPException(503) caught by generic except → re-raised as 500
        assert response.status_code == 500
