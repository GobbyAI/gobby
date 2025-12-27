"""Tests for the MCP proxy stdio module."""

import signal
import subprocess
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
    """Tests for get_daemon_pid using psutil."""

    def test_returns_none_when_no_daemon_process(self):
        """Test returns None when no daemon process logic matches."""
        # Mock psutil.process_iter to return processes that DONT match
        with patch("gobby.mcp_proxy.daemon_control.psutil.process_iter") as mock_iter:
            mock_iter.return_value = [
                MagicMock(info={"pid": 1, "name": "init", "cmdline": ["init"]}),
                MagicMock(info={"pid": 999, "name": "python", "cmdline": ["other", "script"]}),
            ]
            assert get_daemon_pid() is None

    def test_returns_pid_when_daemon_process_found(self):
        """Test returns PID when valid daemon process found."""
        with patch("gobby.mcp_proxy.daemon_control.psutil.process_iter") as mock_iter:
            # Matches logic: "gobby.cli.app" and "daemon" and "start"
            mock_iter.return_value = [
                MagicMock(
                    info={
                        "pid": 12345,
                        "name": "python",
                        "cmdline": [
                            "python",
                            "-m",
                            "gobby.cli.app",
                            "daemon",
                            "start",
                            "--port",
                            "8765",
                        ],
                    }
                ),
            ]
            assert get_daemon_pid() == 12345

    def test_ignores_current_process(self):
        """Test ignores the current process even if it matches."""
        current_pid = 777
        with patch("gobby.mcp_proxy.daemon_control.os.getpid", return_value=current_pid):
            with patch("gobby.mcp_proxy.daemon_control.psutil.process_iter") as mock_iter:
                # One match is current pid, should be skipped.
                # Only if function explicitly checks.
                # (My implementation: `if proc.info["pid"] == current_pid: continue`)

                mock_proc_self = MagicMock(
                    info={
                        "pid": current_pid,
                        "name": "python",
                        "cmdline": ["python", "-m", "gobby.cli.app", "daemon", "start"],
                    }
                )

                # Another match
                mock_proc_other = MagicMock(
                    info={
                        "pid": 888,
                        "name": "python",
                        "cmdline": ["python", "-m", "gobby.cli.app", "daemon", "start"],
                    }
                )

                mock_iter.return_value = [mock_proc_self, mock_proc_other]

                assert get_daemon_pid() == 888


class TestIsDaemonRunning:
    """Tests for is_daemon_running function."""

    def test_returns_false_when_no_pid(self):
        """Test returns False when no PID."""
        with patch("gobby.mcp_proxy.daemon_control.get_daemon_pid", return_value=None):
            assert is_daemon_running() is False

    def test_returns_true_when_pid_exists(self):
        """Test returns True when PID exists."""
        with patch("gobby.mcp_proxy.daemon_control.get_daemon_pid", return_value=12345):
            assert is_daemon_running() is True


class TestStartDaemonProcess:
    """Tests for start_daemon_process function."""

    @pytest.mark.asyncio
    async def test_returns_already_running_if_daemon_running(self):
        """Test returns already_running if daemon is already running."""
        with patch("gobby.mcp_proxy.daemon_control.is_daemon_running", return_value=True):
            with patch("gobby.mcp_proxy.daemon_control.get_daemon_pid", return_value=12345):
                result = await start_daemon_process(8765, 8766)

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

        with patch("gobby.mcp_proxy.daemon_control.is_daemon_running", return_value=False):
            with patch("gobby.mcp_proxy.daemon_control.subprocess.run", return_value=mock_result):
                with patch("gobby.mcp_proxy.daemon_control.get_daemon_pid", return_value=12345):
                    with patch(
                        "gobby.mcp_proxy.daemon_control.asyncio.sleep", new_callable=AsyncMock
                    ):
                        result = await start_daemon_process(8765, 8766)

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

        with patch("gobby.mcp_proxy.daemon_control.is_daemon_running", return_value=False):
            with patch("gobby.mcp_proxy.daemon_control.subprocess.run", return_value=mock_result):
                result = await start_daemon_process(8765, 8766)

                assert result["success"] is False
                assert "failed" in result["message"].lower()
                assert result["error"] == "Start failed"

    @pytest.mark.asyncio
    async def test_handles_timeout(self):
        """Test handles start command timeout."""
        with patch("gobby.mcp_proxy.daemon_control.is_daemon_running", return_value=False):
            with patch(
                "gobby.mcp_proxy.daemon_control.subprocess.run",
                side_effect=subprocess.TimeoutExpired("gobby", 10),
            ):
                result = await start_daemon_process(8765, 8766)

                assert result["success"] is False
                assert "timed out" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_handles_exception(self):
        """Test handles unexpected exception."""
        with patch("gobby.mcp_proxy.daemon_control.is_daemon_running", return_value=False):
            with patch(
                "gobby.mcp_proxy.daemon_control.subprocess.run",
                side_effect=Exception("Unexpected error"),
            ):
                result = await start_daemon_process(8765, 8766)

                assert result["success"] is False
                assert "Unexpected error" in result["error"]


class TestStopDaemonProcess:
    """Tests for stop_daemon_process function."""

    @pytest.mark.asyncio
    async def test_returns_not_running_if_daemon_not_running(self):
        """Test returns not_running if daemon is not running."""
        with patch("gobby.mcp_proxy.daemon_control.get_daemon_pid", return_value=None):
            result = await stop_daemon_process()

            assert result["success"] is False
            assert result["not_running"] is True

    @pytest.mark.asyncio
    async def test_stops_daemon_successfully(self):
        """Test successful daemon stop."""
        with patch("gobby.mcp_proxy.daemon_control.get_daemon_pid", return_value=12345):
            with patch("gobby.mcp_proxy.daemon_control.os.kill") as mock_kill:
                result = await stop_daemon_process()

                assert result["success"] is True
                assert result["output"] == "Daemon stopped"
                mock_kill.assert_called_once_with(12345, signal.SIGTERM)

    @pytest.mark.asyncio
    async def test_handles_stop_failure_permission(self):
        """Test handles daemon stop failure due to permission."""
        with patch("gobby.mcp_proxy.daemon_control.get_daemon_pid", return_value=12345):
            with patch(
                "gobby.mcp_proxy.daemon_control.os.kill", side_effect=PermissionError("Denied")
            ):
                result = await stop_daemon_process()

                assert result["success"] is False
                assert result["error"] == "Permission denied"

    @pytest.mark.asyncio
    async def test_handles_stop_failure_not_found(self):
        """Test handles daemon stop failure due to process lookup."""
        with patch("gobby.mcp_proxy.daemon_control.get_daemon_pid", return_value=12345):
            with patch(
                "gobby.mcp_proxy.daemon_control.os.kill",
                side_effect=ProcessLookupError("Not found"),
            ):
                result = await stop_daemon_process()

                assert result["success"] is False
                assert result["error"] == "Process not found"
                assert result["not_running"] is True


class TestRestartDaemonProcess:
    """Tests for restart_daemon_process function."""

    @pytest.mark.asyncio
    async def test_restarts_daemon_successfully(self):
        """Test successful daemon restart."""
        # Mock stop passing (returns success dict)
        with patch("gobby.mcp_proxy.daemon_control.stop_daemon_process") as mock_stop:
            mock_stop.return_value = {"success": True}

            # Mock start passing
            with patch("gobby.mcp_proxy.daemon_control.start_daemon_process") as mock_start:
                mock_start.return_value = {
                    "success": True,
                    "pid": 54321,
                    "output": "Daemon restarted",
                }

                with patch("gobby.mcp_proxy.daemon_control.asyncio.sleep", new_callable=AsyncMock):
                    result = await restart_daemon_process(12345, 8765, 8766)

                    assert result["success"] is True
                    assert result["pid"] == 54321
                    mock_stop.assert_called_once_with(12345)
                    mock_start.assert_called_once_with(8765, 8766)

    # Simplified checks since logic depends on start/stop delegation which we mock


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

        with patch("gobby.mcp_proxy.daemon_control.httpx.AsyncClient", return_value=mock_client):
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

        with patch("gobby.mcp_proxy.daemon_control.httpx.AsyncClient", return_value=mock_client):
            result = await check_daemon_http_health(8765)
            assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_connection_error(self):
        """Test returns False when connection fails."""
        mock_client = AsyncMock()
        mock_client.get.side_effect = Exception("Connection refused")
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        with patch("gobby.mcp_proxy.daemon_control.httpx.AsyncClient", return_value=mock_client):
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

        with patch("gobby.mcp_proxy.daemon_control.httpx.AsyncClient", return_value=mock_client):
            await check_daemon_http_health(8765, timeout=5.0)

            # Verify the get was called with the timeout
            mock_client.get.assert_called_once()
            call_kwargs = mock_client.get.call_args
            assert call_kwargs.kwargs["timeout"] == 5.0


class TestCreateStdioMcpServer:
    """Tests for create_stdio_mcp_server function."""

    # Needs patches of stdio imports which invoke daemon logic
    # But since create_stdio_mcp_server loads config but only DEFINES tools, it shouldn't auto-start daemon.
    # It just returns mcp object.

    def test_creates_mcp_server(self):
        """Test creates FastMCP server instance."""
        from gobby.mcp_proxy.stdio import create_stdio_mcp_server

        with patch("gobby.mcp_proxy.stdio.load_config") as mock_config:
            mock_config.return_value = MagicMock(
                daemon_port=8765,
                websocket=MagicMock(port=8766),
            )
            # Patch internals to avoid import side-effects? No side effects expected.

            mcp = create_stdio_mcp_server()

            # Should return a FastMCP instance
            from mcp.server.fastmcp import FastMCP

            assert isinstance(mcp, FastMCP)


class TestEnsureDaemonRunning:
    """Tests for ensure_daemon_running function."""

    @pytest.mark.asyncio
    async def test_does_nothing_if_healthy(self):
        """Test does nothing if daemon is already healthy."""
        from gobby.mcp_proxy.stdio import ensure_daemon_running

        with patch("gobby.mcp_proxy.stdio.load_config") as mock_config:
            mock_config.return_value = MagicMock(daemon_port=8765, websocket=MagicMock(port=8766))

            with patch("gobby.mcp_proxy.stdio.is_daemon_running", return_value=True):
                with patch(
                    "gobby.mcp_proxy.stdio.check_daemon_http_health",
                    new_callable=AsyncMock,
                    return_value=True,
                ):
                    # Should not raise or call start
                    await ensure_daemon_running()

    @pytest.mark.asyncio
    async def test_restarts_unhealthy_daemon(self):
        """Test restarts daemon if running but unhealthy."""
        from gobby.mcp_proxy.stdio import ensure_daemon_running

        with patch("gobby.mcp_proxy.stdio.load_config") as mock_config:
            mock_config.return_value = MagicMock(daemon_port=8765, websocket=MagicMock(port=8766))

            with patch("gobby.mcp_proxy.stdio.is_daemon_running", return_value=True):
                # First health check fails, second succeeds (after restart)
                health_checks = [False, True]
                with patch(
                    "gobby.mcp_proxy.stdio.check_daemon_http_health",
                    new_callable=AsyncMock,
                    side_effect=health_checks,
                ):
                    with patch(
                        "gobby.mcp_proxy.stdio.restart_daemon_process",
                        new_callable=AsyncMock,
                        # ensure_daemon_running awaits restart, no need for return val check inside ensure (it just awaits)
                        return_value={"success": True},
                    ) as mock_restart:
                        with patch("gobby.mcp_proxy.stdio.get_daemon_pid", return_value=12345):
                            await ensure_daemon_running()
                            mock_restart.assert_called_once()

    @pytest.mark.asyncio
    async def test_starts_daemon_if_not_running(self):
        """Test starts daemon if not running."""
        from gobby.mcp_proxy.stdio import ensure_daemon_running

        with patch("gobby.mcp_proxy.stdio.load_config") as mock_config:
            mock_config.return_value = MagicMock(daemon_port=8765, websocket=MagicMock(port=8766))

            with patch("gobby.mcp_proxy.stdio.is_daemon_running", return_value=False):
                with patch(
                    "gobby.mcp_proxy.stdio.start_daemon_process",
                    new_callable=AsyncMock,
                    return_value={"success": True},
                ) as mock_start:
                    with patch(
                        "gobby.mcp_proxy.stdio.check_daemon_http_health",
                        new_callable=AsyncMock,
                        return_value=True,
                    ):
                        await ensure_daemon_running()
                        mock_start.assert_called_once()
