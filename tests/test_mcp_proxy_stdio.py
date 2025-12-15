"""Tests for the MCP proxy stdio module."""

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.stdio import (
    check_daemon_http_health,
    get_daemon_pid,
    is_daemon_running,
    restart_daemon_process,
    start_daemon_process,
    stop_daemon_process,
)


class TestGetDaemonPid:
    """Tests for get_daemon_pid function."""

    def test_returns_none_when_pid_file_missing(self, tmp_path: Path):
        """Test returns None when PID file doesn't exist."""
        with patch("gobby.mcp_proxy.stdio.Path.home", return_value=tmp_path):
            result = get_daemon_pid()
            assert result is None

    def test_returns_none_when_pid_file_empty(self, tmp_path: Path):
        """Test returns None when PID file is empty."""
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        pid_file = gobby_dir / "gobby.pid"
        pid_file.write_text("")

        with patch("gobby.mcp_proxy.stdio.Path.home", return_value=tmp_path):
            result = get_daemon_pid()
            assert result is None

    def test_returns_none_when_pid_invalid(self, tmp_path: Path):
        """Test returns None when PID is not a valid integer."""
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        pid_file = gobby_dir / "gobby.pid"
        pid_file.write_text("not-a-number")

        with patch("gobby.mcp_proxy.stdio.Path.home", return_value=tmp_path):
            result = get_daemon_pid()
            assert result is None

    def test_returns_none_when_process_not_running(self, tmp_path: Path):
        """Test returns None when PID exists but process is not running."""
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        pid_file = gobby_dir / "gobby.pid"
        pid_file.write_text("999999999")  # Very high PID unlikely to exist

        with patch("gobby.mcp_proxy.stdio.Path.home", return_value=tmp_path):
            with patch("gobby.mcp_proxy.stdio.os.kill", side_effect=ProcessLookupError):
                result = get_daemon_pid()
                assert result is None

    def test_returns_pid_when_process_running(self, tmp_path: Path):
        """Test returns PID when process is running."""
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        pid_file = gobby_dir / "gobby.pid"
        pid_file.write_text("12345")

        with patch("gobby.mcp_proxy.stdio.Path.home", return_value=tmp_path):
            with patch("gobby.mcp_proxy.stdio.os.kill"):  # No exception means process exists
                result = get_daemon_pid()
                assert result == 12345

    def test_handles_whitespace_in_pid_file(self, tmp_path: Path):
        """Test handles whitespace in PID file."""
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        pid_file = gobby_dir / "gobby.pid"
        pid_file.write_text("  54321  \n")

        with patch("gobby.mcp_proxy.stdio.Path.home", return_value=tmp_path):
            with patch("gobby.mcp_proxy.stdio.os.kill"):
                result = get_daemon_pid()
                assert result == 54321


class TestIsDaemonRunning:
    """Tests for is_daemon_running function."""

    def test_returns_false_when_no_pid(self):
        """Test returns False when no PID."""
        with patch("gobby.mcp_proxy.stdio.get_daemon_pid", return_value=None):
            assert is_daemon_running() is False

    def test_returns_true_when_pid_exists(self):
        """Test returns True when PID exists."""
        with patch("gobby.mcp_proxy.stdio.get_daemon_pid", return_value=12345):
            assert is_daemon_running() is True


class TestStartDaemonProcess:
    """Tests for start_daemon_process function."""

    @pytest.mark.asyncio
    async def test_returns_already_running_if_daemon_running(self):
        """Test returns already_running if daemon is already running."""
        with patch("gobby.mcp_proxy.stdio.is_daemon_running", return_value=True):
            with patch("gobby.mcp_proxy.stdio.get_daemon_pid", return_value=12345):
                result = await start_daemon_process()

                assert result["success"] is False
                assert result["already_running"] is True
                assert result["pid"] == 12345

    @pytest.mark.asyncio
    async def test_starts_daemon_successfully(self):
        """Test successful daemon start."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Daemon started"
        mock_result.stderr = ""

        with patch("gobby.mcp_proxy.stdio.is_daemon_running", return_value=False):
            with patch("gobby.mcp_proxy.stdio.subprocess.run", return_value=mock_result):
                with patch("gobby.mcp_proxy.stdio.get_daemon_pid", return_value=12345):
                    with patch("gobby.mcp_proxy.stdio.asyncio.sleep", new_callable=AsyncMock):
                        result = await start_daemon_process()

                        assert result["success"] is True
                        assert result["pid"] == 12345
                        assert result["output"] == "Daemon started"

    @pytest.mark.asyncio
    async def test_handles_start_failure(self):
        """Test handles daemon start failure."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "Start failed"

        with patch("gobby.mcp_proxy.stdio.is_daemon_running", return_value=False):
            with patch("gobby.mcp_proxy.stdio.subprocess.run", return_value=mock_result):
                result = await start_daemon_process()

                assert result["success"] is False
                assert "failed" in result["message"].lower()
                assert result["error"] == "Start failed"

    @pytest.mark.asyncio
    async def test_handles_timeout(self):
        """Test handles start command timeout."""
        with patch("gobby.mcp_proxy.stdio.is_daemon_running", return_value=False):
            with patch(
                "gobby.mcp_proxy.stdio.subprocess.run",
                side_effect=subprocess.TimeoutExpired("gobby", 10)
            ):
                result = await start_daemon_process()

                assert result["success"] is False
                assert "timed out" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_handles_exception(self):
        """Test handles unexpected exception."""
        with patch("gobby.mcp_proxy.stdio.is_daemon_running", return_value=False):
            with patch("gobby.mcp_proxy.stdio.subprocess.run", side_effect=Exception("Unexpected error")):
                result = await start_daemon_process()

                assert result["success"] is False
                assert "Unexpected error" in result["error"]


class TestStopDaemonProcess:
    """Tests for stop_daemon_process function."""

    @pytest.mark.asyncio
    async def test_returns_not_running_if_daemon_not_running(self):
        """Test returns not_running if daemon is not running."""
        with patch("gobby.mcp_proxy.stdio.get_daemon_pid", return_value=None):
            result = await stop_daemon_process()

            assert result["success"] is False
            assert result["not_running"] is True

    @pytest.mark.asyncio
    async def test_stops_daemon_successfully(self):
        """Test successful daemon stop."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Daemon stopped"
        mock_result.stderr = ""

        with patch("gobby.mcp_proxy.stdio.get_daemon_pid", return_value=12345):
            with patch("gobby.mcp_proxy.stdio.subprocess.run", return_value=mock_result):
                result = await stop_daemon_process()

                assert result["success"] is True
                assert result["output"] == "Daemon stopped"

    @pytest.mark.asyncio
    async def test_handles_stop_failure(self):
        """Test handles daemon stop failure."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "Stop failed"

        with patch("gobby.mcp_proxy.stdio.get_daemon_pid", return_value=12345):
            with patch("gobby.mcp_proxy.stdio.subprocess.run", return_value=mock_result):
                result = await stop_daemon_process()

                assert result["success"] is False
                assert result["error"] == "Stop failed"

    @pytest.mark.asyncio
    async def test_handles_timeout(self):
        """Test handles stop command timeout."""
        with patch("gobby.mcp_proxy.stdio.get_daemon_pid", return_value=12345):
            with patch(
                "gobby.mcp_proxy.stdio.subprocess.run",
                side_effect=subprocess.TimeoutExpired("gobby", 10)
            ):
                result = await stop_daemon_process()

                assert result["success"] is False
                assert "timed out" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_handles_exception(self):
        """Test handles unexpected exception."""
        with patch("gobby.mcp_proxy.stdio.get_daemon_pid", return_value=12345):
            with patch("gobby.mcp_proxy.stdio.subprocess.run", side_effect=Exception("Stop error")):
                result = await stop_daemon_process()

                assert result["success"] is False
                assert "Stop error" in result["error"]


class TestRestartDaemonProcess:
    """Tests for restart_daemon_process function."""

    @pytest.mark.asyncio
    async def test_restarts_daemon_successfully(self):
        """Test successful daemon restart."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Daemon restarted"
        mock_result.stderr = ""

        with patch("gobby.mcp_proxy.stdio.subprocess.run", return_value=mock_result):
            with patch("gobby.mcp_proxy.stdio.get_daemon_pid", return_value=54321):
                with patch("gobby.mcp_proxy.stdio.asyncio.sleep", new_callable=AsyncMock):
                    result = await restart_daemon_process()

                    assert result["success"] is True
                    assert result["pid"] == 54321
                    assert result["output"] == "Daemon restarted"

    @pytest.mark.asyncio
    async def test_handles_restart_failure(self):
        """Test handles daemon restart failure."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "Restart failed"

        with patch("gobby.mcp_proxy.stdio.subprocess.run", return_value=mock_result):
            result = await restart_daemon_process()

            assert result["success"] is False
            assert result["error"] == "Restart failed"

    @pytest.mark.asyncio
    async def test_handles_timeout(self):
        """Test handles restart command timeout."""
        with patch(
            "gobby.mcp_proxy.stdio.subprocess.run",
            side_effect=subprocess.TimeoutExpired("gobby", 15)
        ):
            result = await restart_daemon_process()

            assert result["success"] is False
            assert "timed out" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_handles_exception(self):
        """Test handles unexpected exception."""
        with patch("gobby.mcp_proxy.stdio.subprocess.run", side_effect=Exception("Restart error")):
            result = await restart_daemon_process()

            assert result["success"] is False
            assert "Restart error" in result["error"]


class TestCheckDaemonHttpHealth:
    """Tests for check_daemon_http_health function."""

    @pytest.mark.asyncio
    async def test_returns_true_on_200_response(self):
        """Test returns True when daemon responds with 200."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        with patch("gobby.mcp_proxy.stdio.httpx.AsyncClient", return_value=mock_client):
            result = await check_daemon_http_health(8765)
            assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_non_200_response(self):
        """Test returns False when daemon responds with non-200."""
        mock_response = MagicMock()
        mock_response.status_code = 500

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        with patch("gobby.mcp_proxy.stdio.httpx.AsyncClient", return_value=mock_client):
            result = await check_daemon_http_health(8765)
            assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_connection_error(self):
        """Test returns False when connection fails."""
        mock_client = AsyncMock()
        mock_client.get.side_effect = Exception("Connection refused")
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        with patch("gobby.mcp_proxy.stdio.httpx.AsyncClient", return_value=mock_client):
            result = await check_daemon_http_health(8765)
            assert result is False

    @pytest.mark.asyncio
    async def test_uses_provided_timeout(self):
        """Test uses provided timeout value."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        with patch("gobby.mcp_proxy.stdio.httpx.AsyncClient", return_value=mock_client):
            result = await check_daemon_http_health(8765, timeout=5.0)

            # Verify the get was called with the timeout
            mock_client.get.assert_called_once()
            call_kwargs = mock_client.get.call_args
            assert call_kwargs.kwargs["timeout"] == 5.0


class TestCreateStdioMcpServer:
    """Tests for create_stdio_mcp_server function."""

    def test_creates_mcp_server(self):
        """Test creates FastMCP server instance."""
        from gobby.mcp_proxy.stdio import create_stdio_mcp_server

        with patch("gobby.mcp_proxy.stdio.load_config") as mock_config:
            mock_config.return_value = MagicMock(
                daemon_port=8765,
                websocket=MagicMock(port=8766),
            )

            mcp = create_stdio_mcp_server()

            # Should return a FastMCP instance
            from fastmcp import FastMCP
            assert isinstance(mcp, FastMCP)


class TestEnsureDaemonRunning:
    """Tests for ensure_daemon_running function."""

    @pytest.mark.asyncio
    async def test_does_nothing_if_healthy(self):
        """Test does nothing if daemon is already healthy."""
        from gobby.mcp_proxy.stdio import ensure_daemon_running

        with patch("gobby.mcp_proxy.stdio.load_config") as mock_config:
            mock_config.return_value = MagicMock(daemon_port=8765)

            with patch("gobby.mcp_proxy.stdio.is_daemon_running", return_value=True):
                with patch("gobby.mcp_proxy.stdio.check_daemon_http_health", new_callable=AsyncMock, return_value=True):
                    # Should not raise or call start
                    await ensure_daemon_running()

    @pytest.mark.asyncio
    async def test_restarts_unhealthy_daemon(self):
        """Test restarts daemon if running but unhealthy."""
        from gobby.mcp_proxy.stdio import ensure_daemon_running

        with patch("gobby.mcp_proxy.stdio.load_config") as mock_config:
            mock_config.return_value = MagicMock(daemon_port=8765)

            with patch("gobby.mcp_proxy.stdio.is_daemon_running", return_value=True):
                # First health check fails, second succeeds (after restart)
                health_checks = [False, True]
                with patch(
                    "gobby.mcp_proxy.stdio.check_daemon_http_health",
                    new_callable=AsyncMock,
                    side_effect=health_checks
                ):
                    with patch(
                        "gobby.mcp_proxy.stdio.restart_daemon_process",
                        new_callable=AsyncMock,
                        return_value={"success": True}
                    ) as mock_restart:
                        await ensure_daemon_running()
                        mock_restart.assert_called_once()

    @pytest.mark.asyncio
    async def test_starts_daemon_if_not_running(self):
        """Test starts daemon if not running."""
        from gobby.mcp_proxy.stdio import ensure_daemon_running

        with patch("gobby.mcp_proxy.stdio.load_config") as mock_config:
            mock_config.return_value = MagicMock(daemon_port=8765)

            with patch("gobby.mcp_proxy.stdio.is_daemon_running", return_value=False):
                with patch(
                    "gobby.mcp_proxy.stdio.start_daemon_process",
                    new_callable=AsyncMock,
                    return_value={"success": True}
                ) as mock_start:
                    with patch(
                        "gobby.mcp_proxy.stdio.check_daemon_http_health",
                        new_callable=AsyncMock,
                        return_value=True
                    ):
                        await ensure_daemon_running()
                        mock_start.assert_called_once()

    @pytest.mark.asyncio
    async def test_exits_on_start_failure(self):
        """Test exits if daemon fails to start."""
        from gobby.mcp_proxy.stdio import ensure_daemon_running

        with patch("gobby.mcp_proxy.stdio.load_config") as mock_config:
            mock_config.return_value = MagicMock(daemon_port=8765)

            with patch("gobby.mcp_proxy.stdio.is_daemon_running", return_value=False):
                with patch(
                    "gobby.mcp_proxy.stdio.start_daemon_process",
                    new_callable=AsyncMock,
                    return_value={"success": False, "message": "Failed to start"}
                ):
                    with pytest.raises(SystemExit):
                        await ensure_daemon_running()

    @pytest.mark.asyncio
    async def test_exits_if_health_check_never_passes(self):
        """Test exits if daemon never becomes healthy."""
        from gobby.mcp_proxy.stdio import ensure_daemon_running

        with patch("gobby.mcp_proxy.stdio.load_config") as mock_config:
            mock_config.return_value = MagicMock(daemon_port=8765)

            with patch("gobby.mcp_proxy.stdio.is_daemon_running", return_value=False):
                with patch(
                    "gobby.mcp_proxy.stdio.start_daemon_process",
                    new_callable=AsyncMock,
                    return_value={"success": True}
                ):
                    with patch(
                        "gobby.mcp_proxy.stdio.check_daemon_http_health",
                        new_callable=AsyncMock,
                        return_value=False  # Never healthy
                    ):
                        with patch("gobby.mcp_proxy.stdio.asyncio.sleep", new_callable=AsyncMock):
                            with pytest.raises(SystemExit):
                                await ensure_daemon_running()
