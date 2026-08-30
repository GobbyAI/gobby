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

    def test_agy_status_omits_machine_transport_disclaimer(self) -> None:
        result = format_status_message(
            running=True,
            deps_info={
                "coding_clis": {
                    "agy": "1.1.18",
                    "hooks": {"agy": True},
                }
            },
        )

        agy_line = _status_line(result, "AGY CLI")
        assert "unavailable: no machine transport" not in agy_line
        assert "hooks installed" in agy_line

    def test_format_status_message_renders_fingerprinted_embedding_providers(self) -> None:
        deps_info: dict[str, dict[str, object]] = {
            "integrations": {
                "embeddings_provider": "vllm",
                "ollama": {"running": False},
                "lmstudio": {"running": False},
            }
        }
        result = format_status_message(running=True, deps_info=deps_info)
        line = _status_line(result, "Embeddings")
        assert "vLLM" in line
        assert "Ollama" not in line
        assert "stopped" not in line

        deps_info["integrations"]["embeddings_provider"] = "openai-compatible"
        result = format_status_message(running=True, deps_info=deps_info)
        line = _status_line(result, "Embeddings")
        assert "OpenAI-compatible endpoint" in line
        assert "Ollama" not in line

    def test_format_status_message_renders_generation_endpoint_health(self) -> None:
        api_data = {
            "generation_endpoints": [
                {
                    "name": "vllm",
                    "protocol": "vllm",
                    "provider_label": "vLLM",
                    "wire_api": "chat-completions",
                    "api_base": "http://localhost:8321/v1",
                    "model": "auto",
                    "healthy": True,
                    "served_model": "mlx-community/Qwen2.5-3B-Instruct-4bit",
                    "model_count": 1,
                    "error": None,
                },
                {
                    "name": "vllm-vision",
                    "protocol": "vllm",
                    "provider_label": "vLLM",
                    "wire_api": "chat-completions",
                    "api_base": "http://localhost:8322/v1",
                    "model": "auto",
                    "healthy": False,
                    "served_model": None,
                    "model_count": None,
                    "error": "Cannot connect to local vllm endpoint",
                },
            ]
        }
        result = format_status_message(running=True, api_data=api_data)

        line = _status_line(result, "Generation")
        assert "vllm (vLLM) http://localhost:8321 — healthy" in line
        assert "mlx-community/Qwen2.5-3B-Instruct-4bit" in line
        assert (
            "vllm-vision (vLLM) http://localhost:8322 — unreachable "
            "(Cannot connect to local vllm endpoint)"
        ) in result
        # Continuation lines carry no repeated label.
        assert result.count("Generation:") == 1

        absent = format_status_message(running=True, api_data={})
        assert "Generation:" not in absent

        empty = format_status_message(running=True, api_data={"generation_endpoints": []})
        assert "Generation:" not in empty

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
                    "server1": {
                        "health": "error",
                        "consecutive_failures": 3,
                        "last_error": "list_tools timed out after 5s",
                    },
                    "server2": {"health": "healthy", "consecutive_failures": 0},
                },
            },
        )

        assert "Health Issues:" in result
        assert "MCP: server1 — error: list_tools timed out after 5s" in result
        assert "server2" not in result.split("Health Issues:")[1]

    def test_mcp_pre_degraded_failure_includes_last_error(self) -> None:
        result = format_status_message(
            running=True,
            api_data={
                "mcp_servers": {
                    "server1": {
                        "health": "healthy",
                        "consecutive_failures": 2,
                        "last_error": "Connection reset",
                    }
                }
            },
        )

        assert "MCP: server1 — 2 consecutive failures: Connection reset" in result

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
                    "ghook": None,
                    "ghook_path": None,
                    "gwiki": "0.1.0",
                    "gwiki_path": "/Users/test/.gobby/bin/gwiki",
                },
                "coding_clis": {"claude": "installed", "qwen": None, "codex": None, "hooks": {}},
                "dependencies": {
                    "required": {
                        "tmux": {
                            "state": "healthy",
                            "installed_version": "3.7b",
                            "minimum_version": "3.2",
                            "expected_version": None,
                            "path": "/usr/bin/tmux",
                            "error": None,
                        }
                    },
                    "optional": {},
                },
            },
        )

        assert "Gobby:" in result
        assert "0.3.6" in result
        assert "gwiki:" in result
        assert "0.1.0 (/Users/test/.gobby/bin/gwiki)" in result
        assert "Coding CLIs:" in result
        assert "Claude Code:" in result
        assert "Required Dependencies:" in result
        assert "tmux:" in result

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
            deps_info={
                "services": {
                    "docker": {
                        "state": "healthy",
                        "installed_version": "28.0.0",
                        "minimum_version": None,
                        "expected_version": None,
                        "path": "/usr/bin/docker",
                        "error": None,
                    },
                    "docker_running": True,
                    "docker_compose": {
                        "state": "healthy",
                        "installed_version": "2.39.1",
                        "minimum_version": "2.7.0",
                        "expected_version": None,
                        "path": "/usr/bin/docker",
                        "error": None,
                    },
                }
            },
        )

        assert result.index("Docker Engine:") < result.index("PostgreSQL:")
        assert "Docker Compose:   2.39.1 (min: 2.7.0)" in result


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
