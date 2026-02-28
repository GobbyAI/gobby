from unittest.mock import MagicMock, patch

import httpx
import pytest

from gobby.utils.status import fetch_rich_status, format_status_message

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

        status = await fetch_rich_status(8080)

        assert status["memory_mb"] == 100.5
        assert status["mcp_total"] == 2
        assert status["mcp_connected"] == 1
        assert status["mcp_tools_cached"] == 5
        assert status["mcp_unhealthy"] == [("server2", "error")]
        assert status["sessions_active"] == 1
        assert status["tasks_open"] == 2
        assert status["memories_count"] == 10

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
            memory_mb=100.0,
            cpu_percent=5.0,
            mcp_total=2,
            mcp_connected=1,
            sessions_active=1,
        )
        assert "Status: Running (PID: 1234)" in msg
        assert "Uptime: 1h" in msg
        assert "Memory: 100.0 MB" in msg
        assert "HTTP: localhost:8080" in msg
        assert "Servers: 1 connected / 2 total" in msg
        assert "Active: 1" in msg

    def test_format_status_message_stopped(self) -> None:
        msg = format_status_message(running=False)
        assert "Status: Stopped" in msg

    def test_format_status_message_unhealthy_mcp(self) -> None:
        msg = format_status_message(running=True, mcp_total=1, mcp_unhealthy=[("s1", "error")])
        assert "Unhealthy: s1 (error)" in msg

    def test_format_status_message_paths(self) -> None:
        msg = format_status_message(running=True, pid_file="/tmp/pid", log_files="/tmp/logs")
        assert "PID file: /tmp/pid" in msg
        assert "Logs: /tmp/logs" in msg

    def test_format_status_message_full(self) -> None:
        msg = format_status_message(
            running=True,
            tasks_open=1,
            tasks_in_progress=2,
            tasks_ready=3,
            tasks_blocked=4,
            memories_count=10,
            sessions_paused=1,
            sessions_handoff_ready=1,
        )
        assert "Tasks:" in msg
        assert "Open: 1" in msg
        assert "In Progress: 2" in msg
        assert "Ready: 3" in msg
        assert "Blocked: 4" in msg
        assert "Memory:" in msg
        assert "Memories: 10" in msg
        assert "Paused: 1" in msg
        assert "Handoff Ready: 1" in msg
