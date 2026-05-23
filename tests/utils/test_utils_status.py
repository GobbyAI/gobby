"""Tests for src/utils/status.py - Status Message Formatting."""

import pytest

from gobby.utils.status import format_startup_summary, format_status_message

pytestmark = pytest.mark.unit


def _status_line(output: str, label: str) -> str:
    prefix = f"{label}:"
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            return stripped
    raise AssertionError(f"missing status line for {label}")


class TestFormatStatusMessage:
    """Tests for format_status_message function."""

    def test_stopped_status(self) -> None:
        result = format_status_message(running=False)

        assert "GOBBY DAEMON STATUS" in result
        assert "Stopped" in result
        assert "=" * 70 in result

    def test_running_status_minimal(self) -> None:
        result = format_status_message(running=True)

        assert "Running" in result
        assert "PID:" not in result

    def test_running_status_with_pid(self) -> None:
        result = format_status_message(running=True, pid=12345)

        assert "Running (PID: 12345)" in result

    def test_running_status_with_uptime(self) -> None:
        result = format_status_message(running=True, uptime="1h 23m 45s")

        assert "1h 23m 45s" in result

    def test_network_section_with_http_port(self) -> None:
        result = format_status_message(running=True, http_port=60887)

        assert "Network:" in result
        assert "localhost:60887" in result

    def test_network_section_with_websocket_port(self) -> None:
        result = format_status_message(running=True, websocket_port=60888)

        assert "Network:" in result
        assert "localhost:60888" in result

    def test_network_section_with_both_ports(self) -> None:
        result = format_status_message(running=True, http_port=60887, websocket_port=60888)

        assert "localhost:60887" in result
        assert "localhost:60888" in result

    def test_no_network_section_when_no_ports(self) -> None:
        result = format_status_message(running=True, pid=123)

        assert "Network:" not in result

    def test_full_status_message(self) -> None:
        result = format_status_message(
            running=True,
            pid=54321,
            uptime="2h 30m 15s",
            http_port=60887,
            websocket_port=60888,
            log_files="/home/user/.gobby/logs/",
        )

        assert "=" * 70 in result
        assert "GOBBY DAEMON STATUS" in result
        assert "Running (PID: 54321)" in result
        assert "2h 30m 15s" in result
        assert "localhost:60887" in result
        assert "localhost:60888" in result

    def test_stopped_status_no_details(self) -> None:
        result = format_status_message(
            running=False,
            pid=12345,
            uptime="1h",
        )

        assert "Stopped" in result
        assert "PID: 12345" not in result
        assert "Uptime:" not in result

    def test_extra_kwargs_ignored(self) -> None:
        result = format_status_message(running=True, unknown_field="value", another_unknown=123)

        assert "Running" in result

    def test_output_is_string(self) -> None:
        result = format_status_message(running=True)

        assert isinstance(result, str)

    def test_output_has_newlines(self) -> None:
        result = format_status_message(running=True, pid=123)

        assert "\n" in result
        lines = result.split("\n")
        assert len(lines) > 5

    def test_mcp_health_issues(self) -> None:
        result = format_status_message(
            running=True,
            api_data={
                "mcp_servers": {
                    "server1": {"health": "error", "consecutive_failures": 3},
                    "server2": {"health": "healthy", "consecutive_failures": 0},
                },
            },
        )

        assert "Health Issues:" in result
        assert "server1" in result
        assert "server2" not in result.split("Health Issues:")[1]

    def test_sessions_in_active_work(self) -> None:
        result = format_status_message(
            running=True,
            api_data={
                "sessions": {"active": 2, "paused": 3},
            },
        )

        assert "Active Work:" in result
        assert "2 active" in result
        assert "3 paused" in result

    def test_process_metrics(self) -> None:
        result = format_status_message(
            running=True,
            uptime="1h 0m 0s",
            api_data={
                "process": {"memory_rss_mb": 45.5, "cpu_percent": 2.3},
            },
        )

        assert "45.5 MB" in result
        assert "CPU: 2.3%" in result

    def test_services_section(self) -> None:
        result = format_status_message(
            running=True,
            api_data={
                "memory": {
                    "qdrant": {"configured": True, "healthy": True},
                    "falkordb": {
                        "configured": True,
                        "installed": True,
                        "healthy": True,
                        "url": "redis://127.0.0.1:16379",
                    },
                },
            },
        )

        assert "Services:" in result
        assert "Qdrant" in result
        assert "healthy" in result
        assert "FalkorDB" in result

    def test_services_section_distinguishes_installed_unconfigured_falkordb(self) -> None:
        result = format_status_message(
            running=True,
            api_data={
                "memory": {
                    "falkordb": {
                        "configured": False,
                        "installed": True,
                        "healthy": False,
                        "url": "redis://127.0.0.1:16379",
                    },
                },
            },
        )

        assert "FalkorDB" in result
        assert "installed, not configured" in result
        assert "redis://127.0.0.1:16379" in result

    def test_provider_model_counts_attach_to_coding_clis_and_health_issues(self) -> None:
        result = format_status_message(
            running=True,
            api_data={
                "provider_models": {
                    "claude": {"source": "live", "model_count": 3, "error": None},
                    "codex": {
                        "source": "cache",
                        "model_count": 4,
                        "error": "probe failed",
                    },
                },
            },
            deps_info={
                "coding_clis": {
                    "claude": "installed",
                    "codex": "installed",
                    "droid": None,
                    "gemini": None,
                    "qwen": None,
                    "hooks": {"claude": True},
                },
            },
        )

        assert "Claude Code:" in result
        assert "hooks installed, 3 models available" in _status_line(result, "Claude Code")
        assert "Codex CLI:" in result
        assert "4 models available" in _status_line(result, "Codex CLI")
        assert "Models claude:" not in result
        assert "(live)" not in result
        assert "using cache (probe failed)" in result

    def test_config_issues(self) -> None:
        result = format_status_message(
            running=True,
            config_issues=[
                {"subsystem": "Codex", "error": "provider configured but codex CLI not in PATH"},
            ],
        )

        assert "Health Issues:" in result
        assert "Codex" in result

    def test_deps_info(self) -> None:
        result = format_status_message(
            running=True,
            deps_info={
                "gobby": {
                    "gobby": "0.3.6",
                    "gcode": "0.2.1",
                    "gcode_path": None,
                    "gsqz": None,
                    "gsqz_path": None,
                },
                "coding_clis": {"claude": "installed", "gemini": None, "codex": None, "hooks": {}},
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

        assert "Gobby:" in result
        assert "0.3.6" in result
        assert "Coding CLIs:" in result
        assert "Claude Code:" in result
        assert "Dependencies:" in result
        assert "tmux:" in result
        assert "git:" in result

    def test_coding_clis_include_qwen_and_droid(self) -> None:
        result = format_status_message(
            running=True,
            api_data={
                "provider_models": {
                    "droid": {"source": "live", "model_count": 24, "error": None},
                    "qwen": {"source": "live", "model_count": 3, "error": None},
                },
            },
            deps_info={
                "coding_clis": {
                    "claude": "installed",
                    "codex": "installed",
                    "droid": "installed",
                    "gemini": "installed",
                    "qwen": "installed",
                    "hooks": {"droid": True, "qwen": True},
                },
            },
        )

        assert "Qwen CLI:" in result
        assert "hooks installed, 3 models available" in _status_line(result, "Qwen CLI")
        assert "Droid CLI:" in result
        assert "hooks installed, 24 models available" in _status_line(result, "Droid CLI")

    def test_services_place_postgres_after_docker(self) -> None:
        result = format_status_message(
            running=True,
            api_data={
                "postgres": {
                    "mode": "docker",
                    "dsn_host": "localhost",
                    "dsn_db": "gobby",
                    "healthy": True,
                }
            },
            deps_info={"dependencies": {"docker": "installed", "docker_running": True}},
        )

        assert result.index("Docker:") < result.index("PostgreSQL:")


class TestFormatStartupSummary:
    def test_basic(self) -> None:
        result = format_startup_summary(pid=1, http_port=8080, websocket_port=8081)

        assert "Gobby daemon ready (PID: 1)" in result
        assert "localhost:8080" in result
        assert "localhost:8081" in result

    def test_with_ui(self) -> None:
        result = format_startup_summary(
            pid=1,
            http_port=8080,
            websocket_port=8081,
            ui_url="http://localhost:5173",
            ui_mode="dev",
        )

        assert "http://localhost:5173 (dev)" in result

    def test_with_logs(self) -> None:
        result = format_startup_summary(
            pid=1,
            http_port=8080,
            websocket_port=8081,
            log_files="/tmp/logs",
        )

        assert "/tmp/logs" in result
