from unittest.mock import MagicMock, patch

import httpx
import pytest

from gobby.utils.dependency_requirements import STARTING_GRACE_SECONDS
from gobby.utils.status import (
    fetch_rich_status,
    format_startup_summary,
    format_status_message,
)

pytestmark = pytest.mark.unit


def _dependency(
    installed_version: str | None,
    *,
    minimum_version: str | None = None,
    expected_version: str | None = None,
    state: str = "healthy",
    error: str | None = None,
) -> dict[str, str | None]:
    return {
        "state": state,
        "installed_version": installed_version,
        "minimum_version": minimum_version,
        "expected_version": expected_version,
        "path": "/usr/bin/tool" if installed_version else None,
        "error": error,
    }


@patch("gobby.utils.status.daemon_auth_headers", return_value={"Authorization": "Bearer status"})
@patch("httpx.AsyncClient.get")
async def test_fetch_rich_status_sends_bearer(mock_get, _mock_headers) -> None:
    response = MagicMock(status_code=200)
    response.json.return_value = {"success": True}
    mock_get.return_value = response

    assert await fetch_rich_status(60887) == {"success": True}
    assert mock_get.await_args.kwargs["headers"] == {"Authorization": "Bearer status"}


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

    def test_format_status_message_tolerates_null_mapping_sections(self) -> None:
        msg = format_status_message(
            running=True,
            deps_info={"services": None, "integrations": None},
        )

        assert "Services:" in msg

    def test_format_status_message_shows_resolved_ui_mode_and_pid(self) -> None:
        msg = format_status_message(
            running=True,
            http_port=60887,
            ui_enabled=True,
            ui_mode="auto -> dev",
            ui_url="http://localhost:60887/",
            ui_pid=1234,
        )

        assert "http://localhost:60887/ (auto -> dev, PID: 1234)" in msg

    def test_format_status_message_degraded_when_control_plane_unavailable(self) -> None:
        msg = format_status_message(
            running=True,
            pid=1234,
            http_port=8080,
            control_plane_error="HTTP control plane unavailable at localhost:8080",
        )

        assert "Degraded (PID: 1234; HTTP unavailable)" in msg
        assert "Health Issues:" in msg
        assert "Daemon control plane:" in msg
        assert "HTTP control plane unavailable at localhost:8080" in msg

    @pytest.mark.parametrize("age", [0.0, 6.0, STARTING_GRACE_SECONDS - 0.001])
    def test_format_status_message_starting_during_control_plane_grace(
        self,
        age: float,
    ) -> None:
        msg = format_status_message(
            running=True,
            pid=1234,
            control_plane_error="HTTP control plane unavailable",
            process_uptime_seconds=age,
        )

        assert "Starting (PID: 1234)" in msg
        assert "Daemon control plane:" not in msg

    def test_format_status_message_degraded_at_control_plane_grace_boundary(self) -> None:
        msg = format_status_message(
            running=True,
            pid=1234,
            control_plane_error="HTTP control plane unavailable",
            process_uptime_seconds=STARTING_GRACE_SECONDS,
        )

        assert "Degraded (PID: 1234; HTTP unavailable)" in msg
        assert "Daemon control plane:" in msg

    def test_required_dependency_failure_degrades_running_daemon(self) -> None:
        msg = format_status_message(
            running=True,
            pid=1234,
            api_data={"process": {}},
            deps_info={
                "dependencies": {
                    "required": {
                        "git": _dependency(
                            "2.37.0",
                            minimum_version="2.38.0",
                            state="outdated",
                            error=(
                                "Git is outdated; detected 2.37.0, requires >=2.38.0. Install Git."
                            ),
                        )
                    },
                    "optional": {
                        "tailscale": _dependency(None, state="missing"),
                        "impeccable": _dependency("3.5.0", expected_version="3.5.0"),
                    },
                }
            },
        )

        assert "Degraded (PID: 1234)" in msg
        assert "Required dependency git: Git is outdated" in msg
        assert "Required dependency tailscale" not in msg

    def test_native_windows_status_is_unsupported(self) -> None:
        msg = format_status_message(running=False, unsupported_platform=True)

        assert "Unsupported (native Windows; use WSL 2)" in msg

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

    def test_format_status_message_reports_hook_runtime_schema_skew(self) -> None:
        msg = format_status_message(
            running=True,
            api_data={
                "hook_runtime": {
                    "state": "schema_mismatch",
                    "detail": "ghook envelope schema 2 does not match daemon schema 1.",
                }
            },
        )

        assert "Health Issues:" in msg
        assert "Hook runtime: schema_mismatch" in msg
        assert "ghook envelope schema 2 does not match daemon schema 1" in msg

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
                    "ghook": "0.2.0",
                    "ghook_path": "/Users/test/.gobby/bin/ghook",
                    "gwiki": "0.6.6",
                    "gwiki_path": "/Users/test/.gobby/bin/gwiki",
                },
                "coding_clis": {
                    "claude": "installed",
                    "qwen": None,
                    "codex": None,
                    "hooks": {"claude": True, "qwen": False, "codex": False},
                },
                "runtime": {"python": _dependency("3.13.5", minimum_version="3.13.0")},
                "dependencies": {
                    "required": {
                        "tmux": _dependency("3.7b", minimum_version="3.2"),
                        "git": _dependency("2.50.1", minimum_version="2.38.0"),
                        "node": _dependency("26.5.0", minimum_version="20.11.0"),
                        "srt": _dependency("0.0.66", expected_version="0.0.66"),
                        "impeccable": _dependency("3.5.0", expected_version="3.5.0"),
                    },
                    "optional": {
                        "tailscale": _dependency(None, state="missing"),
                    },
                },
            },
        )
        assert "0.3.6" in msg
        assert "0.2.1" in msg
        assert "0.2.0" in msg
        assert "0.6.6" in msg
        assert "Claude Code:" in msg
        assert "Python:           3.13.5 (min: 3.13.0)" in msg
        assert "Required Dependencies:" in msg
        assert "tmux:" in msg
        assert "git:" in msg
        assert "SRT:              0.0.66 (managed, verified)" in msg
        assert "Optional Dependencies:" in msg
        assert "Impeccable:       3.5.0 (managed, verified)" in msg

    def test_format_status_message_prefers_configured_embeddings_provider(self) -> None:
        msg = format_status_message(
            running=True,
            deps_info={
                "integrations": {
                    "embeddings_provider": "lmstudio",
                    "ollama": {"version": "0.1.30", "running": True},
                    "lmstudio": {"running": True},
                },
            },
        )
        assert "LM Studio (running)" in msg

    def test_format_status_message_shows_degraded_embeddings_probe(self) -> None:
        msg = format_status_message(
            running=True,
            deps_info={
                "integrations": {
                    "embeddings_provider": {
                        "status": "degraded",
                        "error": "BootstrapConfigError",
                    },
                },
            },
        )

        assert "Embeddings:       degraded (BootstrapConfigError)" in msg

    def test_format_status_message_shows_configured_openai_over_local_fallback(self) -> None:
        msg = format_status_message(
            running=True,
            deps_info={
                "integrations": {
                    "embeddings_provider": "openai",
                    "ollama": {"version": "0.1.30", "running": False},
                    "lmstudio": {"running": False},
                },
            },
        )
        assert "Embeddings:       OpenAI" in msg
        assert "Ollama (stopped)" not in msg

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
                    "extensions": {"pg_search": True, "pgaudit": True, "pgcrypto": True},
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
                    "extensions": {"pg_search": False, "pgaudit": False, "pgcrypto": False},
                }
            },
        )

        assert "unhealthy (external; db.example.com/gobby" in msg
        assert "missing pg_search, pgaudit, pgcrypto" in msg
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
            ui_mode="auto -> dev",
            log_files="/tmp/logs",
        )
        assert "Gobby daemon ready (PID: 12345)" in msg
        assert "localhost:60887" in msg
        assert "localhost:60888" in msg
        assert "http://localhost:5173 (auto -> dev)" in msg
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
