from unittest.mock import MagicMock, patch

import httpx
import pytest

from gobby.utils.status import (
    fetch_rich_status,
    format_startup_summary,
    format_status_message,
)

pytestmark = pytest.mark.unit


class TestStatusUtils:
    @patch("httpx.AsyncClient.get")
    async def test_fetch_rich_status_success(self, mock_get) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "process": {"memory_rss_mb": 100.5, "cpu_percent": 10.5},
            "mcp_servers": {
                "server1": {"connected": True, "health": "healthy"},
                "server2": {"connected": False, "health": "error"},
            },
            "mcp_tools_cached": 5,
            "sessions": {"active": 1, "paused": 0, "handoff_ready": 0},
            "tasks": {"open": 2, "in_progress": 1},
            "memory": {"count": 10},
        }
        mock_get.return_value = mock_response

        data = await fetch_rich_status(8080)

        # fetch_rich_status now returns the raw API response dict
        assert data["process"]["memory_rss_mb"] == 100.5
        assert len(data["mcp_servers"]) == 2
        assert data["sessions"]["active"] == 1
        assert data["tasks"]["open"] == 2

    @patch("httpx.AsyncClient.get")
    async def test_fetch_rich_status_failure(self, mock_get) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_get.return_value = mock_response

        status = await fetch_rich_status(8080)
        assert status == {}

    @patch("httpx.AsyncClient.get")
    async def test_fetch_rich_status_connection_error(self, mock_get) -> None:
        mock_get.side_effect = httpx.ConnectError("Connection failed")
        status = await fetch_rich_status(8080)
        assert status == {}

    @patch("httpx.AsyncClient.get")
    async def test_fetch_rich_status_other_error(self, mock_get) -> None:
        mock_get.side_effect = Exception("Unknown error")
        status = await fetch_rich_status(8080)
        assert status == {}

    def test_format_status_message_running(self) -> None:
        msg = format_status_message(
            running=True,
            pid=1234,
            uptime="1h",
            http_port=8080,
            api_data={
                "process": {"memory_rss_mb": 100.0, "cpu_percent": 5.0},
            },
        )
        assert "Running (PID: 1234)" in msg
        assert "1h" in msg
        assert "100.0 MB" in msg
        assert "localhost:8080" in msg

    def test_format_status_message_stopped(self) -> None:
        msg = format_status_message(running=False)
        assert "Stopped" in msg

    def test_format_status_message_health_issues(self) -> None:
        msg = format_status_message(
            running=True,
            api_data={
                "mcp_servers": {
                    "s1": {"health": "error", "consecutive_failures": 3},
                },
            },
        )
        assert "Health Issues:" in msg
        assert "s1" in msg

    def test_format_status_message_log_files(self) -> None:
        msg = format_status_message(
            running=True,
            log_files="/tmp/logs",
        )
        # Log files not shown in status (only in startup summary)
        # Status focuses on runtime health, not paths
        assert "GOBBY DAEMON STATUS" in msg

    def test_format_status_message_active_work(self) -> None:
        msg = format_status_message(
            running=True,
            api_data={
                "sessions": {"active": 2, "paused": 1},
                "agents": {"running": 1},
                "pipelines": {"running": 1, "waiting_approval": 0},
            },
        )
        assert "Active Work:" in msg
        assert "2 active" in msg
        assert "1 paused" in msg

    def test_format_status_message_deps(self) -> None:
        msg = format_status_message(
            running=True,
            deps_info={
                "gobby": {
                    "gobby": "0.3.6",
                    "gcode": "0.2.1",
                    "gcode_path": None,
                    "gsqz": None,
                    "gsqz_path": None,
                    "ghook": "0.2.0",
                    "ghook_path": "/Users/test/.gobby/bin/ghook",
                    "gloc": "0.1.1",
                    "gloc_path": "/Users/test/.gobby/bin/gloc",
                },
                "coding_clis": {
                    "claude": "installed",
                    "gemini": None,
                    "codex": None,
                    "hooks": {"claude": True, "gemini": False, "codex": False},
                },
                "dependencies": {
                    "tmux": "installed",
                    "docker": None,
                    "docker_running": False,
                    "git": "installed",
                    "node": None,
                    "tailscale": None,
                    "ollama": None,
                    "lmstudio": None,
                },
            },
        )
        assert "0.3.6" in msg
        assert "0.2.1" in msg
        assert "0.2.0" in msg
        assert "0.1.1" in msg
        assert "Claude Code:" in msg
        assert "tmux:" in msg
        assert "git:" in msg

    def test_format_status_message_prefers_configured_embeddings_provider(self) -> None:
        msg = format_status_message(
            running=True,
            deps_info={
                "dependencies": {
                    "embeddings_provider": "lmstudio",
                    "ollama": {"version": "0.1.30", "running": True},
                    "lmstudio": {"running": True},
                },
            },
        )
        assert "LM Studio (running)" in msg

    def test_format_status_message_postgres_healthy(self) -> None:
        msg = format_status_message(
            running=True,
            api_data={
                "postgres": {
                    "mode": "docker",
                    "dsn_host": "localhost",
                    "dsn_db": "gobby",
                    "database_url": "postgresql://gobby:secret@localhost:60891/gobby",
                    "healthy": True,
                    "extensions": {"pg_search": True, "pgaudit": True},
                }
            },
        )

        assert "PostgreSQL:" in msg
        assert "healthy (docker; localhost/gobby; extensions ok)" in msg
        assert "postgresql://" not in msg
        assert "secret" not in msg

    def test_format_status_message_postgres_unhealthy(self) -> None:
        msg = format_status_message(
            running=True,
            api_data={
                "postgres": {
                    "mode": "external",
                    "dsn_host": "db.example.com",
                    "dsn_db": "gobby",
                    "healthy": False,
                    "extensions": {"pg_search": False, "pgaudit": False},
                }
            },
        )

        assert "unhealthy (external; db.example.com/gobby" in msg
        assert "missing pg_search, pgaudit" in msg
        assert "Health Issues:" in msg
        assert "PostgreSQL: unhealthy" in msg

    def test_format_status_message_postgres_unavailable(self) -> None:
        msg = format_status_message(
            running=True,
            api_data={
                "postgres": {
                    "available": False,
                    "mode": "native",
                    "healthy": False,
                    "error": "BootstrapConfigError",
                }
            },
        )

        assert "PostgreSQL:" in msg
        assert "unavailable (native; BootstrapConfigError)" in msg
        assert "Health Issues:" in msg
        assert "PostgreSQL: BootstrapConfigError" in msg

    def test_format_status_message_config_issues(self) -> None:
        msg = format_status_message(
            running=True,
            config_issues=[
                {"subsystem": "Codex", "error": "provider configured but codex CLI not in PATH"},
            ],
        )
        assert "Health Issues:" in msg
        assert "Codex" in msg

    def test_format_startup_summary(self) -> None:
        msg = format_startup_summary(
            pid=12345,
            http_port=60887,
            websocket_port=60888,
            ui_url="http://localhost:5173",
            ui_mode="dev",
            log_files="/tmp/logs",
        )
        assert "Gobby daemon ready (PID: 12345)" in msg
        assert "localhost:60887" in msg
        assert "localhost:60888" in msg
        assert "http://localhost:5173 (dev)" in msg
        assert "/tmp/logs" in msg

    def test_format_startup_summary_minimal(self) -> None:
        msg = format_startup_summary(
            pid=1,
            http_port=8080,
            websocket_port=8081,
        )
        assert "Gobby daemon ready (PID: 1)" in msg
        assert "Web UI" not in msg
        assert "Logs" not in msg
