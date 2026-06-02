"""Tests for the MCP proxy stdio module."""

import asyncio
import signal
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from gobby.mcp_proxy.stdio import (
    _strip_none,
    check_daemon_http_health,
    create_stdio_mcp_server,
    get_daemon_pid,
    is_daemon_running,
    register_proxy_tools,
    restart_daemon_process,
    start_daemon_process,
    stop_daemon_process,
)

pytestmark = pytest.mark.unit


class TestGetDaemonPid:
    """Tests for get_daemon_pid using psutil."""

    def test_returns_none_when_no_daemon_process(self) -> None:
        """Test returns None when no daemon process logic matches."""
        # Mock psutil.process_iter to return processes that DONT match
        with patch("gobby.mcp_proxy.daemon_control.psutil.process_iter") as mock_iter:
            mock_iter.return_value = [
                MagicMock(info={"pid": 1, "name": "init", "cmdline": ["init"]}),
                MagicMock(info={"pid": 999, "name": "python", "cmdline": ["other", "script"]}),
            ]
            assert get_daemon_pid() is None

    def test_returns_pid_when_daemon_process_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test returns PID when valid daemon process found."""
        # Disable the test-protect fence to exercise production-path behavior.
        monkeypatch.delenv("GOBBY_TEST_PROTECT", raising=False)
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
                            "60887",
                        ],
                    }
                ),
            ]
            assert get_daemon_pid() == 12345

    def test_ignores_current_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test ignores the current process even if it matches."""
        monkeypatch.delenv("GOBBY_TEST_PROTECT", raising=False)
        current_pid = 777
        with patch("gobby.mcp_proxy.daemon_control.os.getpid", return_value=current_pid):
            with patch("gobby.mcp_proxy.daemon_control.psutil.process_iter") as mock_iter:
                mock_proc_self = MagicMock(
                    info={
                        "pid": current_pid,
                        "name": "python",
                        "cmdline": ["python", "-m", "gobby.cli.app", "daemon", "start"],
                    }
                )
                mock_proc_other = MagicMock(
                    info={
                        "pid": 888,
                        "name": "python",
                        "cmdline": ["python", "-m", "gobby.cli.app", "daemon", "start"],
                    }
                )

                mock_iter.return_value = [mock_proc_self, mock_proc_other]

                assert get_daemon_pid() == 888
                assert mock_iter.call_count == 1

    def test_test_protect_skips_processes_outside_gobby_home(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Under GOBBY_TEST_PROTECT, processes whose cmdline does not reference
        the current GOBBY_HOME / GOBBY_CONFIG_FILE are not returned — this is
        the fence that prevents tests from discovering the production daemon.
        """
        monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")
        monkeypatch.setenv("GOBBY_HOME", "/tmp/gobby-e2e-isolated")
        monkeypatch.delenv("GOBBY_CONFIG_FILE", raising=False)
        with patch("gobby.mcp_proxy.daemon_control.psutil.process_iter") as mock_iter:
            mock_iter.return_value = [
                MagicMock(
                    info={
                        "pid": 12345,
                        "name": "python",
                        "cmdline": [
                            "python",
                            "-m",
                            "gobby.runner",
                            "--config",
                            "/Users/someone/.gobby/config.yaml",
                        ],
                    }
                ),
            ]
            assert get_daemon_pid() is None

    def test_test_protect_returns_pid_inside_gobby_home(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Under GOBBY_TEST_PROTECT, processes whose cmdline DOES reference the
        current GOBBY_HOME are still returned — the fence must not break tests
        that legitimately spawn an isolated daemon and look it up."""
        isolated_home = "/tmp/gobby-e2e-isolated"
        monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")
        monkeypatch.setenv("GOBBY_HOME", isolated_home)
        with patch("gobby.mcp_proxy.daemon_control.psutil.process_iter") as mock_iter:
            mock_iter.return_value = [
                MagicMock(
                    info={
                        "pid": 12345,
                        "name": "python",
                        "cmdline": [
                            "python",
                            "-m",
                            "gobby.runner",
                            "--config",
                            f"{isolated_home}/config.yaml",
                        ],
                    }
                ),
            ]
            assert get_daemon_pid() == 12345


class TestIsDaemonRunning:
    """Tests for is_daemon_running function."""

    def test_returns_false_when_no_pid(self) -> None:
        """Test returns False when no PID."""
        with patch("gobby.mcp_proxy.daemon_control.get_daemon_pid", return_value=None):
            assert is_daemon_running() is False

    def test_returns_true_when_pid_exists(self) -> None:
        """Test returns True when PID exists."""
        with patch("gobby.mcp_proxy.daemon_control.get_daemon_pid", return_value=12345):
            assert is_daemon_running() is True


class TestStartDaemonProcess:
    """Tests for start_daemon_process function."""

    @pytest.mark.asyncio
    async def test_returns_already_running_if_daemon_running(self) -> None:
        """Test returns already_running if daemon is already running."""
        with patch("gobby.mcp_proxy.daemon_control.is_daemon_running", return_value=True):
            with patch("gobby.mcp_proxy.daemon_control.get_daemon_pid", return_value=12345):
                result = await start_daemon_process(60887, 60888)

                assert result["success"] is False
                assert result["already_running"] is True
                assert result["pid"] == 12345

    @pytest.mark.asyncio
    async def test_starts_daemon_successfully(self) -> None:
        """Test successful daemon start."""
        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.pid = 12345

        with patch("gobby.mcp_proxy.daemon_control.is_daemon_running", return_value=False):
            with patch(
                "gobby.mcp_proxy.daemon_control.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
            ) as mock_exec:
                mock_exec.return_value = mock_proc
                with patch("gobby.mcp_proxy.daemon_control.get_daemon_pid", return_value=12345):
                    with patch(
                        "gobby.mcp_proxy.daemon_control.check_daemon_http_health",
                        new_callable=AsyncMock,
                        return_value=True,
                    ):
                        with patch(
                            "gobby.mcp_proxy.daemon_control.asyncio.sleep", new_callable=AsyncMock
                        ):
                            result = await start_daemon_process(60887, 60888)

                            assert result["success"] is True
                            assert result["pid"] == 12345
                            assert "started successfully" in result["output"]

    @pytest.mark.asyncio
    async def test_handles_start_failure(self) -> None:
        """Test handles daemon start failure."""
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b"", b"Start failed"))

        with patch("gobby.mcp_proxy.daemon_control.is_daemon_running", return_value=False):
            with patch(
                "gobby.mcp_proxy.daemon_control.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
            ) as mock_exec:
                mock_exec.return_value = mock_proc
                with patch("gobby.mcp_proxy.daemon_control.asyncio.sleep", new_callable=AsyncMock):
                    result = await start_daemon_process(60887, 60888)

                    assert result["success"] is False
                    assert "process exited immediately" in result["message"]
                    assert result["error"] == "Start failed"

    @pytest.mark.asyncio
    async def test_handles_timeout(self) -> None:
        """Test handles start command checks timeout."""
        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.pid = 12345

        with patch("gobby.mcp_proxy.daemon_control.is_daemon_running", return_value=False):
            with patch(
                "gobby.mcp_proxy.daemon_control.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
            ) as mock_exec:
                mock_exec.return_value = mock_proc
                with patch(
                    "gobby.mcp_proxy.daemon_control.check_daemon_http_health",
                    new_callable=AsyncMock,
                    return_value=False,
                ):
                    with patch("gobby.mcp_proxy.daemon_control.get_daemon_pid", return_value=None):
                        with patch(
                            "gobby.mcp_proxy.daemon_control.asyncio.sleep", new_callable=AsyncMock
                        ):
                            result = await start_daemon_process(60887, 60888)

                            assert result["success"] is False
                            assert "unhealthy" in result["message"]

    @pytest.mark.asyncio
    async def test_handles_exception(self) -> None:
        """Test handles unexpected exception."""
        with patch("gobby.mcp_proxy.daemon_control.is_daemon_running", return_value=False):
            with patch(
                "gobby.mcp_proxy.daemon_control.asyncio.create_subprocess_exec",
                side_effect=Exception("Unexpected error"),
            ):
                result = await start_daemon_process(60887, 60888)

                assert result["success"] is False
                assert "Unexpected error" in result["error"]


class TestStopDaemonProcess:
    """Tests for stop_daemon_process function."""

    @pytest.mark.asyncio
    async def test_skips_when_test_protect_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Under GOBBY_TEST_PROTECT, stop_daemon_process must short-circuit
        BEFORE looking up a PID and BEFORE issuing any signal — this is the
        guard that prevents the production daemon from being SIGTERMed when
        a test path reaches mcp_proxy.daemon_control.
        """
        monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")
        with patch("gobby.mcp_proxy.daemon_control.get_daemon_pid", return_value=12345) as mock_pid:
            with patch("gobby.mcp_proxy.daemon_control.os.kill") as mock_kill:
                result = await stop_daemon_process()

                assert result["success"] is True
                assert result["skipped"] == "test_protect"
                mock_pid.assert_not_called()
                mock_kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_not_running_if_daemon_not_running(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test returns not_running if daemon is not running."""
        monkeypatch.delenv("GOBBY_TEST_PROTECT", raising=False)
        with patch("gobby.mcp_proxy.daemon_control.get_daemon_pid", return_value=None):
            result = await stop_daemon_process()

            assert result["success"] is False
            assert result["not_running"] is True

    @pytest.mark.asyncio
    async def test_stops_daemon_successfully(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test successful daemon stop."""
        monkeypatch.delenv("GOBBY_TEST_PROTECT", raising=False)
        with patch("gobby.mcp_proxy.daemon_control.get_daemon_pid", return_value=12345):

            def kill_side_effect(pid: int, sig: int) -> None:
                if sig == 0:
                    raise ProcessLookupError("Process gone")
                return None

            with patch(
                "gobby.mcp_proxy.daemon_control.os.kill", side_effect=kill_side_effect
            ) as mock_kill:
                with (
                    patch(
                        "gobby.runner_maintenance.write_shutdown_source",
                    ) as mock_write_shutdown_source,
                    patch(
                        "gobby.mcp_proxy.daemon_control.asyncio.sleep",
                        new_callable=AsyncMock,
                    ),
                ):
                    result = await stop_daemon_process()

                    assert result["success"] is True
                    assert result["output"] == "Daemon stopped"
                    mock_kill.assert_any_call(12345, signal.SIGTERM)
                    mock_write_shutdown_source.assert_called_once_with(
                        "mcp_stop",
                        intent="stop",
                    )

    @pytest.mark.asyncio
    async def test_handles_stop_failure_permission(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test handles daemon stop failure due to permission."""
        monkeypatch.delenv("GOBBY_TEST_PROTECT", raising=False)
        with patch("gobby.mcp_proxy.daemon_control.get_daemon_pid", return_value=12345):
            with patch(
                "gobby.mcp_proxy.daemon_control.os.kill", side_effect=PermissionError("Denied")
            ):
                result = await stop_daemon_process()

                assert result["success"] is False
                assert result["error"] == "Permission denied"

    @pytest.mark.asyncio
    async def test_handles_stop_failure_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test handles daemon stop failure due to process lookup."""
        monkeypatch.delenv("GOBBY_TEST_PROTECT", raising=False)
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
    async def test_restarts_daemon_successfully(self) -> None:
        """Test successful daemon restart."""
        with patch(
            "gobby.mcp_proxy.daemon_control.stop_daemon_process", new_callable=AsyncMock
        ) as mock_stop:
            mock_stop.return_value = {"success": True}

            with patch(
                "gobby.mcp_proxy.daemon_control.start_daemon_process", new_callable=AsyncMock
            ) as mock_start:
                mock_start.return_value = {
                    "success": True,
                    "pid": 54321,
                    "output": "Daemon restarted",
                }

                # Mock both sleep and to_thread (for port checking)
                with patch("gobby.mcp_proxy.daemon_control.asyncio.sleep", new_callable=AsyncMock):
                    with patch(
                        "gobby.mcp_proxy.daemon_control.asyncio.to_thread",
                        new_callable=AsyncMock,
                        return_value=True,  # Ports are free
                    ):
                        result = await restart_daemon_process(12345, 60887, 60888)

                        assert result["success"] is True
                        assert result["pid"] == 54321
                        mock_stop.assert_called_once_with(
                            12345,
                            shutdown_intent="restart",
                            shutdown_source="mcp_restart",
                        )
                        mock_start.assert_called_once_with(60887, 60888)


class TestCheckDaemonHttpHealth:
    """Tests for check_daemon_http_health function."""

    @pytest.mark.asyncio
    async def test_returns_true_on_200_response(self) -> None:
        """Test returns True when daemon responds with 200."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        with patch("gobby.mcp_proxy.daemon_control.httpx.AsyncClient", return_value=mock_client):
            result = await check_daemon_http_health(60887)
            assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_non_200_response(self) -> None:
        """Test returns False when daemon responds with non-200."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        with patch("gobby.mcp_proxy.daemon_control.httpx.AsyncClient", return_value=mock_client):
            result = await check_daemon_http_health(60887)
            assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_connection_error(self) -> None:
        """Test returns False when connection fails."""
        mock_client = AsyncMock()
        mock_client.get.side_effect = Exception("Connection refused")
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        with patch("gobby.mcp_proxy.daemon_control.httpx.AsyncClient", return_value=mock_client):
            result = await check_daemon_http_health(60887)
            assert result is False

    @pytest.mark.asyncio
    async def test_uses_provided_timeout(self) -> None:
        """Test uses provided timeout value."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        with patch("gobby.mcp_proxy.daemon_control.httpx.AsyncClient", return_value=mock_client):
            await check_daemon_http_health(60887, timeout=5.0)
            mock_client.get.assert_called_once()
            call_kwargs = mock_client.get.call_args
            assert call_kwargs.kwargs["timeout"] == 5.0


class TestCreateStdioMcpServer:
    """Tests for create_stdio_mcp_server function."""

    def test_creates_mcp_server(self) -> None:
        """Test creates FastMCP server instance."""
        # Use simple patching here since we don't need capture
        with patch("gobby.mcp_proxy.stdio.load_config") as mock_config:
            mock_config.return_value = MagicMock(
                daemon_port=60887,
                websocket=MagicMock(port=60888),
            )
            with patch("gobby.mcp_proxy.stdio.setup_internal_registries"):
                mcp = create_stdio_mcp_server()
                # Just check it's returned
                assert mcp is not None
                assert "list_mcp_servers" in mcp._tool_manager._tools


class TestEnsureDaemonRunning:
    """Tests for ensure_daemon_running function."""

    @pytest.mark.asyncio
    async def test_does_nothing_if_healthy(self) -> None:
        """Test does nothing if daemon is already healthy."""
        with patch("gobby.mcp_proxy.stdio.load_config") as mock_config:
            mock_config.return_value = MagicMock(daemon_port=60887, websocket=MagicMock(port=60888))
            with patch("gobby.mcp_proxy.stdio.is_daemon_running", return_value=True):
                with patch(
                    "gobby.mcp_proxy.stdio.check_daemon_http_health",
                    new_callable=AsyncMock,
                    return_value=True,
                ) as mock_health:
                    # Should not raise or call start
                    # Must import function from module to ensure patches apply
                    from gobby.mcp_proxy.stdio import ensure_daemon_running

                    result = await ensure_daemon_running()
                    assert result is None
                    assert mock_health.await_count == 1

    @pytest.mark.asyncio
    async def test_waits_for_unhealthy_daemon_without_restart(self) -> None:
        """Test waits for an unhealthy daemon instead of restarting it."""
        with patch("gobby.mcp_proxy.stdio.load_config") as mock_config:
            mock_config.return_value = MagicMock(daemon_port=60887, websocket=MagicMock(port=60888))
            with patch("gobby.mcp_proxy.stdio.is_daemon_running", return_value=True):
                health_checks = [False, False, True]
                with patch(
                    "gobby.mcp_proxy.stdio.check_daemon_http_health",
                    new_callable=AsyncMock,
                    side_effect=health_checks,
                ):
                    with patch(
                        "gobby.mcp_proxy.stdio.restart_daemon_process",
                        new_callable=AsyncMock,
                        return_value={"success": True},
                    ) as mock_restart:
                        with patch("gobby.mcp_proxy.stdio.get_daemon_pid", return_value=12345):
                            with patch(
                                "gobby.mcp_proxy.stdio.asyncio.sleep",
                                new_callable=AsyncMock,
                            ) as mock_sleep:
                                from gobby.mcp_proxy.stdio import ensure_daemon_running

                                result = await ensure_daemon_running()
                                assert result is None
                                mock_restart.assert_not_called()
                                assert mock_sleep.await_count == 2

    @pytest.mark.asyncio
    async def test_keeps_stdio_alive_for_persistently_unhealthy_daemon(self) -> None:
        """Test stdio MCP clients do not restart or exit on a busy or unhealthy daemon."""
        with patch("gobby.mcp_proxy.stdio.load_config") as mock_config:
            mock_config.return_value = MagicMock(daemon_port=60887, websocket=MagicMock(port=60888))
            with patch("gobby.mcp_proxy.stdio.is_daemon_running", return_value=True):
                with patch(
                    "gobby.mcp_proxy.stdio.check_daemon_http_health",
                    new_callable=AsyncMock,
                    return_value=False,
                ) as mock_health:
                    with patch(
                        "gobby.mcp_proxy.stdio.restart_daemon_process",
                        new_callable=AsyncMock,
                    ) as mock_restart:
                        with patch(
                            "gobby.mcp_proxy.stdio.get_daemon_pid",
                            return_value=12345,
                        ) as mock_pid:
                            with patch(
                                "gobby.mcp_proxy.stdio.asyncio.sleep",
                                new_callable=AsyncMock,
                            ) as mock_sleep:
                                from gobby.mcp_proxy.stdio import (
                                    DAEMON_HEALTH_ATTEMPTS,
                                    ensure_daemon_running,
                                )

                                result = await ensure_daemon_running()

                                assert result is None
                                assert mock_health.await_count == DAEMON_HEALTH_ATTEMPTS
                                assert mock_sleep.await_count == DAEMON_HEALTH_ATTEMPTS - 1
                                mock_pid.assert_called_once_with()
                                mock_restart.assert_not_called()

    @pytest.mark.asyncio
    async def test_starts_daemon_if_not_running(self) -> None:
        """Test starts daemon if not running."""
        with patch("gobby.mcp_proxy.stdio.load_config") as mock_config:
            mock_config.return_value = MagicMock(daemon_port=60887, websocket=MagicMock(port=60888))
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
                        from gobby.mcp_proxy.stdio import ensure_daemon_running

                        await ensure_daemon_running()
                        mock_start.assert_called_once()
                        assert mock_start.call_count == 1
                        assert mock_start.call_args is not None

    @pytest.mark.asyncio
    async def test_managed_agent_refuses_to_auto_start_daemon(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Managed agent MCP clients must not bootstrap the daemon themselves."""
        monkeypatch.setenv("GOBBY_AGENT_RUN_ID", "run-agent")
        with patch("gobby.mcp_proxy.stdio.load_config") as mock_config:
            mock_config.return_value = MagicMock(daemon_port=60887, websocket=MagicMock(port=60888))
            with patch(
                "gobby.mcp_proxy.stdio.is_daemon_running",
                return_value=False,
            ) as mock_running:
                with patch(
                    "gobby.mcp_proxy.stdio.start_daemon_process",
                    new_callable=AsyncMock,
                ) as mock_start:
                    from gobby.mcp_proxy.stdio import ensure_daemon_running

                    result = await ensure_daemon_running()

                    assert result is None
                    assert mock_config.call_count == 1
                    assert mock_running.call_count == 1
                    assert mock_start.call_count == 0
                    assert mock_start.await_count == 0


class TestDaemonProxy:
    """Tests for DaemonProxy."""

    @pytest.mark.asyncio
    async def test_request_handles_empty_exception_message(self) -> None:
        """Test _request handles exceptions with empty messages."""
        from gobby.mcp_proxy.stdio import DaemonProxy

        proxy = DaemonProxy(60887)
        with patch("gobby.mcp_proxy.stdio.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.request.side_effect = Exception("")
            mock_client_cls.return_value = mock_client
            result = await proxy._request("GET", "/some/path")
            assert result["success"] is False
            assert result["error"] == "Exception: (no message)"

    @pytest.mark.asyncio
    async def test_request_preflight_fails_fast_when_daemon_http_is_unavailable(self) -> None:
        """preflight avoids hook-visible read timeouts when the daemon control plane is down."""
        from gobby.mcp_proxy.stdio import DaemonProxy

        proxy = DaemonProxy(60887)
        with patch(
            "gobby.mcp_proxy.stdio.check_daemon_http_health",
            new_callable=AsyncMock,
        ) as mock_health:
            mock_health.return_value = False
            with patch("gobby.mcp_proxy.stdio.httpx.AsyncClient") as mock_client_cls:
                result = await proxy._request("GET", "/some/path", preflight=True)

        assert result["success"] is False
        assert result["error_code"] == "DAEMON_UNAVAILABLE"
        assert "localhost:60887" in result["error"]
        assert "gobby restart --verbose" in result["error"]
        mock_client_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_request_preflight_caches_successful_health_check(self) -> None:
        from gobby.mcp_proxy.stdio import DaemonProxy

        proxy = DaemonProxy(60887)
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {"success": True}
        with (
            patch("gobby.mcp_proxy.stdio.time.monotonic", side_effect=[100.0, 100.1, 102.0]),
            patch(
                "gobby.mcp_proxy.stdio.check_daemon_http_health",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_health,
            patch("gobby.mcp_proxy.stdio.httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.request.return_value = mock_response
            mock_client_cls.return_value = mock_client

            assert await proxy._request("GET", "/some/path", preflight=True) == {"success": True}
            assert await proxy._request("GET", "/some/path", preflight=True) == {"success": True}

        mock_health.assert_awaited_once()
        assert mock_client.request.await_count == 2

    @pytest.mark.asyncio
    async def test_request_timeout_returns_request_timeout(self) -> None:
        """Daemon read timeouts should become actionable proxy errors."""
        from gobby.mcp_proxy.stdio import DaemonProxy

        proxy = DaemonProxy(60887)
        with patch("gobby.mcp_proxy.stdio.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.request.side_effect = httpx.ReadTimeout("")
            mock_client_cls.return_value = mock_client

            result = await proxy._request("POST", "/api/mcp/gobby-agents/tools/list", timeout=5.0)

        assert result["success"] is False
        assert result["error_code"] == "REQUEST_TIMEOUT"
        assert "request timed out after 5s" in result["error"]
        assert "/api/mcp/gobby-agents/tools/list" in result["error"]

    @pytest.mark.asyncio
    async def test_call_tool_uses_extended_timeout_for_expand_task(self) -> None:
        """Test call_tool uses extended timeout for expand_task."""
        from gobby.mcp_proxy.stdio import DaemonProxy

        proxy = DaemonProxy(60887)
        with patch("gobby.mcp_proxy.stdio.load_config") as mock_config:
            mock_config.return_value = MagicMock(
                mcp_client_proxy=MagicMock(tool_timeouts={"expand_task": 300.0})
            )
            with patch.object(proxy, "_request", new_callable=AsyncMock) as mock_request:
                mock_request.return_value = {"success": True}
                await proxy.call_tool("server", "normal_tool", {})
                mock_request.assert_called_with(
                    "POST",
                    "/api/mcp/server/tools/normal_tool",
                    json={},
                    timeout=30.0,
                    preflight=True,
                )
                assert mock_request.call_count >= 1
                assert mock_request.call_args is not None
                await proxy.call_tool("server", "expand_task", {})
                mock_request.assert_called_with(
                    "POST",
                    "/api/mcp/server/tools/expand_task",
                    json={},
                    timeout=300.0,
                    preflight=True,
                )
                assert mock_request.call_count >= 1
                assert mock_request.call_args is not None

    @pytest.mark.asyncio
    async def test_call_tool_uses_extended_timeout_for_merge_resolve(self) -> None:
        """merge_resolve is LLM-backed and must not use the default 30s timeout."""
        from gobby.mcp_proxy.stdio import DaemonProxy

        proxy = DaemonProxy(60887)
        with patch("gobby.mcp_proxy.stdio.load_config") as mock_config:
            mock_config.return_value = MagicMock(mcp_client_proxy=MagicMock(tool_timeouts={}))
            with patch.object(proxy, "_request", new_callable=AsyncMock) as mock_request:
                mock_request.return_value = {"success": True}

                result = await proxy.call_tool(
                    "gobby-merge",
                    "merge_resolve",
                    {"conflict_id": "mc-one", "use_ai": True},
                )

                assert result == {"success": True}
                assert result["success"] is True
                mock_request.assert_called_once_with(
                    "POST",
                    "/api/mcp/gobby-merge/tools/merge_resolve",
                    json={"conflict_id": "mc-one", "use_ai": True},
                    timeout=300.0,
                    preflight=True,
                )

    @pytest.mark.asyncio
    async def test_call_tool_uses_extended_timeout_for_compact_self(self) -> None:
        """compact_self refreshes handoff context before terminal compaction."""
        from gobby.mcp_proxy.stdio import DaemonProxy

        proxy = DaemonProxy(60887)
        with patch("gobby.mcp_proxy.stdio.load_config") as mock_config:
            mock_config.return_value = MagicMock(mcp_client_proxy=MagicMock(tool_timeouts={}))
            with patch.object(proxy, "_request", new_callable=AsyncMock) as mock_request:
                mock_request.return_value = {"success": True}

                result = await proxy.call_tool(
                    "gobby-sessions",
                    "compact_self",
                    {"rule_name": "build-coordinator-handoff"},
                    session_id="#6074",
                )

                assert result == {"success": True}
                assert result["success"] is True
                mock_request.assert_called_once_with(
                    "POST",
                    "/api/mcp/gobby-sessions/tools/compact_self",
                    json={"rule_name": "build-coordinator-handoff"},
                    timeout=300.0,
                    session_id="#6074",
                    preflight=True,
                )

    @pytest.mark.asyncio
    async def test_call_tool_uses_timeout_seconds_buffer_for_wait_tools(self) -> None:
        """Wait tools use timeout_seconds plus buffer for the daemon HTTP request."""
        from gobby.mcp_proxy.stdio import DaemonProxy

        proxy = DaemonProxy(60887)
        with patch("gobby.mcp_proxy.stdio.load_config") as mock_config:
            mock_config.return_value = MagicMock(mcp_client_proxy=MagicMock(tool_timeouts={}))
            with patch.object(proxy, "_request", new_callable=AsyncMock) as mock_request:
                mock_request.return_value = {"success": True}

                result = await proxy.call_tool(
                    "gobby-agents",
                    "wait_for_agent",
                    {"run_id": "run-123", "timeout_seconds": 120},
                )

                assert result == {"success": True}
                assert result["success"] is True
                mock_request.assert_called_once_with(
                    "POST",
                    "/api/mcp/gobby-agents/tools/wait_for_agent",
                    json={"run_id": "run-123", "timeout_seconds": 120},
                    timeout=150.0,
                    preflight=True,
                )

    @pytest.mark.asyncio
    async def test_call_tool_proxies_when_timeout_config_load_fails(self) -> None:
        """call_tool should not fail just because optional timeout config is unavailable."""
        from gobby.mcp_proxy.stdio import DaemonProxy

        proxy = DaemonProxy(60887)
        with patch("gobby.mcp_proxy.stdio.load_config", side_effect=ValueError("bad config")):
            with patch.object(proxy, "_request", new_callable=AsyncMock) as mock_request:
                mock_request.return_value = {"success": True}

                result = await proxy.call_tool("gobby-tasks", "get_task", {"task_id": "#1"})

                assert result == {"success": True}
                mock_request.assert_called_once_with(
                    "POST",
                    "/api/mcp/gobby-tasks/tools/get_task",
                    json={"task_id": "#1"},
                    timeout=30.0,
                    preflight=True,
                )

    @pytest.mark.asyncio
    async def test_call_tool_can_disable_preflight(self) -> None:
        from gobby.mcp_proxy.stdio import DaemonProxy

        proxy = DaemonProxy(60887)
        with patch("gobby.mcp_proxy.stdio.load_config") as mock_config:
            mock_config.return_value = MagicMock(mcp_client_proxy=MagicMock(tool_timeouts={}))
            with patch.object(proxy, "_request", new_callable=AsyncMock) as mock_request:
                mock_request.return_value = {"success": True}

                result = await proxy.call_tool(
                    "gobby-tasks",
                    "get_task",
                    {"task_id": "#1"},
                    preflight_enabled=False,
                )

        assert result == {"success": True}
        mock_config.assert_called_once()
        assert mock_request.await_count == 1
        mock_request.assert_called_once_with(
            "POST",
            "/api/mcp/gobby-tasks/tools/get_task",
            json={"task_id": "#1"},
            timeout=30.0,
            preflight=False,
        )


class TestDaemonProxyMethods:
    """Tests for DaemonProxy specific methods."""

    @pytest.mark.asyncio
    async def test_get_tool_schema_uses_seeded_session_header(self) -> None:
        from gobby.mcp_proxy.stdio import DaemonProxy

        with patch.dict("os.environ", {"GOBBY_SESSION_ID": "session-123"}):
            proxy = DaemonProxy(60887)

        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {
            "name": "tool",
            "description": "desc",
            "inputSchema": {},
        }

        with patch("gobby.mcp_proxy.stdio.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value.__aenter__.return_value = mock_client

            result = await proxy.get_tool_schema("srv", "tool")

        assert result["success"] is True
        _, kwargs = mock_client.request.call_args
        assert kwargs["headers"]["X-Gobby-Session-Id"] == "session-123"

    @pytest.mark.asyncio
    async def test_request_env_session_id_skips_bootstrap_lookup(self) -> None:
        from gobby.mcp_proxy.stdio import DaemonProxy

        with (
            patch("gobby.mcp_proxy.stdio.read_project_id", return_value="project-123"),
            patch.dict("os.environ", {"GOBBY_SESSION_ID": "env-session"}, clear=True),
        ):
            proxy = DaemonProxy(60887)

        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {"success": True}

        with (
            patch(
                "gobby.mcp_proxy.stdio.resolve_session_id_from_terminal_context",
                new_callable=AsyncMock,
            ) as mock_bootstrap,
            patch("gobby.mcp_proxy.stdio.httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value.__aenter__.return_value = mock_client

            result = await proxy._request("GET", "/api/status")

        assert result == {"success": True}
        mock_bootstrap.assert_not_awaited()
        _, kwargs = mock_client.request.call_args
        assert kwargs["headers"]["X-Gobby-Session-Id"] == "env-session"

    @pytest.mark.asyncio
    async def test_request_missing_env_triggers_bootstrap_lookup(self) -> None:
        from gobby.mcp_proxy.stdio import DaemonProxy

        with (
            patch("gobby.mcp_proxy.stdio.read_project_id", return_value="project-123"),
            patch.dict("os.environ", {}, clear=True),
        ):
            proxy = DaemonProxy(60887)

        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {"success": True}

        with (
            patch(
                "gobby.mcp_proxy.stdio.resolve_session_id_from_terminal_context",
                new_callable=AsyncMock,
            ) as mock_bootstrap,
            patch("gobby.mcp_proxy.stdio.httpx.AsyncClient") as mock_client_cls,
        ):
            mock_bootstrap.return_value = "bootstrapped-session"
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value.__aenter__.return_value = mock_client

            result = await proxy._request("GET", "/api/status")

        assert result == {"success": True}
        mock_bootstrap.assert_awaited_once_with("http://localhost:60887", "project-123")
        assert proxy._session_id == "bootstrapped-session"
        _, kwargs = mock_client.request.call_args
        assert kwargs["headers"]["X-Gobby-Session-Id"] == "bootstrapped-session"

    @pytest.mark.asyncio
    async def test_request_successful_bootstrap_adds_session_header(self) -> None:
        from gobby.mcp_proxy.stdio import DaemonProxy

        with (
            patch("gobby.mcp_proxy.stdio.read_project_id", return_value="project-123"),
            patch.dict("os.environ", {}, clear=True),
        ):
            proxy = DaemonProxy(60887)

        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {"success": True}

        with (
            patch(
                "gobby.mcp_proxy.stdio.resolve_session_id_from_terminal_context",
                new_callable=AsyncMock,
                return_value="bootstrapped-session",
            ),
            patch("gobby.mcp_proxy.stdio.httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value.__aenter__.return_value = mock_client

            result = await proxy._request("GET", "/api/status")

        assert result == {"success": True}
        _, kwargs = mock_client.request.call_args
        assert kwargs["headers"]["X-Gobby-Session-Id"] == "bootstrapped-session"

    @pytest.mark.asyncio
    async def test_request_bootstrap_no_match_sends_no_session_header(self) -> None:
        from gobby.mcp_proxy.stdio import DaemonProxy

        with (
            patch("gobby.mcp_proxy.stdio.read_project_id", return_value="project-123"),
            patch.dict("os.environ", {}, clear=True),
        ):
            proxy = DaemonProxy(60887)

        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {"success": True}

        with (
            patch(
                "gobby.mcp_proxy.stdio.resolve_session_id_from_terminal_context",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("gobby.mcp_proxy.stdio.httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value.__aenter__.return_value = mock_client

            result = await proxy._request("GET", "/api/status")

        assert result == {"success": True}
        _, kwargs = mock_client.request.call_args
        assert "X-Gobby-Session-Id" not in kwargs["headers"]

    @pytest.mark.asyncio
    async def test_list_tools(self) -> None:
        from gobby.mcp_proxy.stdio import DaemonProxy

        proxy = DaemonProxy(60887)
        with patch.object(proxy, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.side_effect = [
                {"success": True, "mcp_servers": {"srv1": {}, "srv2": {}}},  # details
                {"success": True, "tools": [{"name": "t1"}]},  # srv1 tools
                {"success": True, "tools": [{"name": "t2"}]},  # srv2 tools
            ]
            result = await proxy.list_tools()
            assert result["success"] is True
            assert len(result["servers"]) == 2

    @pytest.mark.asyncio
    async def test_get_tool_schema_success(self) -> None:
        from gobby.mcp_proxy.stdio import DaemonProxy

        proxy = DaemonProxy(60887)
        with patch.object(proxy, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"name": "tool", "description": "desc", "inputSchema": {}}
            result = await proxy.get_tool_schema("srv", "tool")
            assert result["success"] is True
            assert result["tool"]["name"] == "tool"

    @pytest.mark.asyncio
    async def test_list_mcp_servers(self) -> None:
        from gobby.mcp_proxy.stdio import DaemonProxy

        proxy = DaemonProxy(60887)
        with patch.object(proxy, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {
                "total": 2,
                "connected": 2,
                "servers": [
                    {"name": "srv1", "state": "connected", "transport": "http"},
                    {"name": "srv2", "state": "connected", "transport": "stdio"},
                ],
            }
            result = await proxy.list_mcp_servers()
            assert result["total"] == 2
            assert result["servers"] == ["srv1", "srv2"]
            assert "issues" not in result

    @pytest.mark.asyncio
    async def test_list_mcp_servers_keeps_issue_details(self) -> None:
        from gobby.mcp_proxy.stdio import DaemonProxy

        proxy = DaemonProxy(60887)
        with patch.object(proxy, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {
                "total": 2,
                "connected": 1,
                "servers": [
                    {"name": "srv1", "state": "connected", "transport": "http"},
                    {"name": "srv2", "state": "pending", "transport": "stdio"},
                ],
            }
            result = await proxy.list_mcp_servers()
            assert result["servers"] == ["srv1", "srv2"]
            assert result["issues"] == [{"name": "srv2", "state": "pending", "transport": "stdio"}]

    @pytest.mark.asyncio
    async def test_recommend_tools(self) -> None:
        from gobby.mcp_proxy.stdio import DaemonProxy

        proxy = DaemonProxy(60887)
        with patch.object(proxy, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"tools": ["t1"]}
            result = await proxy.recommend_tools("task")
            assert result["tools"] == ["t1"]

    @pytest.mark.asyncio
    async def test_search_tools(self) -> None:
        from gobby.mcp_proxy.stdio import DaemonProxy

        proxy = DaemonProxy(60887)
        with patch.object(proxy, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"tools": ["t1"]}
            result = await proxy.search_tools("query")
            assert result["tools"] == ["t1"]

    @pytest.mark.asyncio
    async def test_init_project(self) -> None:
        from gobby.mcp_proxy.stdio import DaemonProxy

        proxy = DaemonProxy(60887)
        with patch.object(proxy, "_request", new_callable=AsyncMock) as mock_req:
            result = await proxy.init_project("name")
            assert result["success"] is False
            assert "requires CLI access" in result["error"]
            mock_req.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_mcp_server(self) -> None:
        from gobby.mcp_proxy.stdio import DaemonProxy

        proxy = DaemonProxy(60887)
        with patch.object(proxy, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"success": True, "message": "Server added"}
            result = await proxy.add_mcp_server(name="n", transport="stdio", command="c")
            assert result["success"] is True
            mock_req.assert_called_once()

    @pytest.mark.asyncio
    async def test_remove_mcp_server(self) -> None:
        from gobby.mcp_proxy.stdio import DaemonProxy

        proxy = DaemonProxy(60887)
        with patch.object(proxy, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"success": True, "message": "Server removed"}
            result = await proxy.remove_mcp_server("name")
            assert result["success"] is True
            mock_req.assert_called_once()

    @pytest.mark.asyncio
    async def test_import_mcp_server(self) -> None:
        from gobby.mcp_proxy.stdio import DaemonProxy

        proxy = DaemonProxy(60887)
        with patch.object(proxy, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"success": True, "imported": ["server1"]}
            result = await proxy.import_mcp_server(from_project="p")
            assert result["success"] is True
            mock_req.assert_called_once()

    @pytest.mark.asyncio
    async def test_removed_wait_for_completion_returns_error_without_request(self) -> None:
        from gobby.mcp_proxy.stdio import DaemonProxy

        proxy = DaemonProxy(60887)
        with patch.object(proxy, "_request", new_callable=AsyncMock) as mock_req:
            result = await proxy.call_tool(
                "gobby-workflows",
                "wait_for_completion",
                {"completion_id": "run-1"},
            )

        assert result["success"] is False
        assert result["error_code"] == "TOOL_REMOVED"
        mock_req.assert_not_called()


class TestMCPToolsWrapper:
    """Tests for the FastMCP tools registered by register_proxy_tools."""

    @staticmethod
    def _register_tools() -> tuple[
        dict[str, Callable[..., Awaitable[Any]]],
        MagicMock,
        Callable[..., Coroutine[Any, Any, Any]],
    ]:
        captured_tools: dict[str, Callable[..., Awaitable[Any]]] = {}

        def mock_tool_decorator(
            name: str | None = None,
            **_kwargs: Any,
        ) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
            def real_decorator(
                func: Callable[..., Awaitable[Any]],
            ) -> Callable[..., Awaitable[Any]]:
                tool_name = name or func.__name__
                captured_tools[tool_name] = func
                return func

            return real_decorator

        mock_mcp = MagicMock()
        mock_mcp.tool.side_effect = mock_tool_decorator

        mock_proxy = MagicMock()
        mock_proxy._session_id = None
        mock_proxy.list_mcp_servers = AsyncMock(return_value={"res": "servers"})
        mock_proxy.list_tools = AsyncMock(return_value={"res": "tools"})
        mock_proxy.get_tool_schema = AsyncMock(return_value={"res": "schema"})
        mock_proxy.call_tool = AsyncMock(return_value={"res": "call"})
        mock_proxy.recommend_tools = AsyncMock(return_value={"res": "rec"})
        mock_proxy.search_tools = AsyncMock(return_value={"res": "search"})
        mock_proxy.init_project = AsyncMock(return_value={"res": "init"})
        mock_proxy.add_mcp_server = AsyncMock(return_value={"res": "add"})
        mock_proxy.remove_mcp_server = AsyncMock(return_value={"res": "remove"})
        mock_proxy.import_mcp_server = AsyncMock(return_value={"res": "import"})

        register_proxy_tools(mock_mcp, mock_proxy)

        async def run_tool(_tool_name: str, **kwargs: Any) -> Any:
            if _tool_name in captured_tools:
                return await captured_tools[_tool_name](**kwargs)
            raise ValueError(f"Tool {_tool_name} not captured")

        return captured_tools, mock_proxy, run_tool

    @pytest.mark.asyncio
    async def test_tools_exist_and_delegate(self) -> None:
        """Test that tools are registered and delegate to proxy."""
        captured_tools, mock_proxy, run_tool = self._register_tools()

        assert captured_tools, "No tools captured! Mocking failed."

        # 1. list_mcp_servers
        await run_tool("list_mcp_servers")
        mock_proxy.list_mcp_servers.assert_called_once()

        # 2. list_tools
        await run_tool("list_tools", server_name="s1")
        mock_proxy.list_tools.assert_called_with("s1")

        # 3. get_tool_schema
        await run_tool("get_tool_schema", server_name="s", tool_name="t")
        mock_proxy.get_tool_schema.assert_called_with("s", "t")

        # 4. call_tool
        await run_tool("call_tool", server_name="s", tool_name="t", arguments={})
        mock_proxy.call_tool.assert_called_with("s", "t", {}, preflight_enabled=True)

        # 5. recommend_tools
        with patch("os.getcwd", return_value="/cwd"):
            await run_tool("recommend_tools", task_description="task")
            mock_proxy.recommend_tools.assert_called_with(
                "task", None, search_mode="llm", top_k=10, min_similarity=0.3, cwd="/cwd"
            )

        # 6. search_tools
        with patch("os.getcwd", return_value="/cwd"):
            await run_tool("search_tools", query="q")
            mock_proxy.search_tools.assert_called_with(
                "q", top_k=10, min_similarity=0.0, server_name=None, cwd="/cwd"
            )

        # 7. init_project
        await run_tool("init_project", name="p")
        mock_proxy.init_project.assert_called_with("p", None)

        # 8. add_mcp_server
        await run_tool("add_mcp_server", name="n", transport="stdio", command="c")
        mock_proxy.add_mcp_server.assert_called()

        # 9. remove_mcp_server
        await run_tool("remove_mcp_server", name="n")
        mock_proxy.remove_mcp_server.assert_called_with("n")

        # 10. import_mcp_server
        await run_tool("import_mcp_server", from_project="p")
        mock_proxy.import_mcp_server.assert_called()

    @pytest.mark.asyncio
    async def test_call_tool_hoists_wrapper_fields_but_keeps_target_session_id(self) -> None:
        _, mock_proxy, run_tool = self._register_tools()

        await run_tool(
            "call_tool",
            arguments={
                "server_name": "gobby-skills",
                "tool_name": "get_skill",
                "session_id": "session-123",
                "name": "brevity",
            },
        )

        mock_proxy.call_tool.assert_called_with(
            "gobby-skills",
            "get_skill",
            {"session_id": "session-123", "name": "brevity"},
            preflight_enabled=True,
        )
        assert mock_proxy._session_id is None

    @pytest.mark.asyncio
    async def test_call_tool_top_level_wrapper_fields_win_and_target_session_id_stays(
        self,
    ) -> None:
        _, mock_proxy, run_tool = self._register_tools()

        await run_tool(
            "call_tool",
            server_name="outer-server",
            tool_name="outer-tool",
            session_id="outer-session",
            project_id="outer-project",
            arguments={
                "server_name": "inner-server",
                "tool_name": "inner-tool",
                "session_id": "inner-session",
                "project_id": "inner-project",
                "value": "ok",
            },
        )

        mock_proxy.call_tool.assert_called_with(
            "outer-server",
            "outer-tool",
            {"session_id": "inner-session", "value": "ok"},
            project_id="outer-project",
            session_id="outer-session",
            preflight_enabled=True,
        )
        assert mock_proxy._session_id is None

    @pytest.mark.asyncio
    async def test_call_tool_delegates_review_learning_record_lesson_unchanged(
        self,
    ) -> None:
        _, mock_proxy, run_tool = self._register_tools()
        arguments = {
            "source_kind": "agent_review",
            "source": "code-reviewer",
            "source_review": "review-123",
            "decision": "confirmed",
            "finding": {
                "title": "Durable writes missing",
                "pattern_id": "durable-write-after-state-change",
                "finding_fingerprint": "durable-write-fingerprint",
            },
            "evidence": {"commit_sha": "abc123"},
        }

        result = await run_tool(
            "call_tool",
            server_name="gobby-review-learning",
            tool_name="record_review_lesson",
            arguments=arguments,
        )

        assert result == {"res": "call"}
        mock_proxy.call_tool.assert_called_with(
            "gobby-review-learning",
            "record_review_lesson",
            arguments,
            preflight_enabled=True,
        )

    @pytest.mark.asyncio
    async def test_call_tool_emits_progress_heartbeat_for_wait_tools(self) -> None:
        _, mock_proxy, run_tool = self._register_tools()

        heartbeat_seen = asyncio.Event()
        release_call = asyncio.Event()
        ctx = MagicMock()
        ctx.report_progress = AsyncMock(side_effect=lambda **_: heartbeat_seen.set())

        async def _block_until_heartbeat(*_args: Any, **_kwargs: Any) -> dict[str, str]:
            await heartbeat_seen.wait()
            await release_call.wait()
            return {"res": "call"}

        mock_proxy.call_tool.side_effect = _block_until_heartbeat

        with patch("gobby.mcp_proxy.wait_tools.WAIT_TOOL_HEARTBEAT_INTERVAL_SECONDS", 0.01):
            task: asyncio.Task[Any] = asyncio.create_task(
                run_tool(
                    "call_tool",
                    server_name="gobby-agents",
                    tool_name="wait_for_agent",
                    arguments={"run_id": "run-123", "timeout_seconds": 300},
                    ctx=ctx,
                )
            )
            await asyncio.wait_for(heartbeat_seen.wait(), timeout=0.2)
            assert ctx.report_progress.await_count >= 1
            assert ctx.report_progress.await_args.kwargs["total"] == 60.0
            release_call.set()
            result = await asyncio.wait_for(task, timeout=0.2)

        assert result == {
            "res": "call",
            "requested_timeout_seconds": 300.0,
            "effective_timeout_seconds": 60.0,
            "wait_timeout_capped_by_mcp_wrapper": True,
        }
        mock_proxy.call_tool.assert_awaited_once_with(
            "gobby-agents",
            "wait_for_agent",
            {"run_id": "run-123", "timeout_seconds": 60.0},
            preflight_enabled=True,
        )

    @pytest.mark.asyncio
    async def test_call_tool_returns_wrapper_timeout_for_stuck_wait_tool(self) -> None:
        _, mock_proxy, run_tool = self._register_tools()
        release_call = asyncio.Event()
        call_finished = asyncio.Event()

        async def _block_until_cancelled(*_args: Any, **_kwargs: Any) -> dict[str, str]:
            await release_call.wait()
            call_finished.set()
            return {"res": "too late"}

        mock_proxy.call_tool.side_effect = _block_until_cancelled

        with (
            patch("gobby.mcp_proxy.wait_tools.MCP_WRAPPER_WAIT_TOOL_TIMEOUT_SECONDS", 0.02),
            patch("gobby.mcp_proxy.wait_tools.WAIT_TOOL_WRAPPER_GRACE_SECONDS", 0.01),
        ):
            result = await asyncio.wait_for(
                run_tool(
                    "call_tool",
                    server_name="gobby-agents",
                    tool_name="wait_for_agent",
                    arguments={"run_id": "run-123", "timeout_seconds": 300},
                ),
                timeout=0.2,
            )

        assert result == {
            "success": True,
            "completed": False,
            "timeout_seconds": 0.02,
            "effective_timeout_seconds": 0.02,
            "mcp_wrapper_timeout": True,
            "background_call_continues": True,
            "tool_name": "wait_for_agent",
            "requested_timeout_seconds": 300.0,
            "wait_timeout_capped_by_mcp_wrapper": True,
        }
        mock_proxy.call_tool.assert_awaited_once_with(
            "gobby-agents",
            "wait_for_agent",
            {"run_id": "run-123", "timeout_seconds": 0.02},
            preflight_enabled=True,
        )
        release_call.set()
        await asyncio.wait_for(call_finished.wait(), timeout=0.2)

    @pytest.mark.asyncio
    async def test_call_tool_returns_wrapper_timeout_for_stuck_close_task(self) -> None:
        _, mock_proxy, run_tool = self._register_tools()
        release_call = asyncio.Event()
        call_finished = asyncio.Event()

        async def _block_until_released(*_args: Any, **_kwargs: Any) -> dict[str, str]:
            await release_call.wait()
            call_finished.set()
            return {"res": "closed"}

        mock_proxy.call_tool.side_effect = _block_until_released

        with (
            patch("gobby.mcp_proxy.wait_tools.MCP_WRAPPER_EXTENDED_TOOL_TIMEOUT_SECONDS", 0.02),
            patch("gobby.mcp_proxy.wait_tools.WAIT_TOOL_WRAPPER_GRACE_SECONDS", 0.01),
        ):
            result = await asyncio.wait_for(
                run_tool(
                    "call_tool",
                    server_name="gobby-tasks",
                    tool_name="close_task",
                    arguments={"task_id": "#15531", "commit_sha": "abc123"},
                ),
                timeout=0.2,
            )

        assert result == {
            "success": True,
            "completed": False,
            "timeout_seconds": 0.02,
            "effective_timeout_seconds": 0.02,
            "mcp_wrapper_timeout": True,
            "background_call_continues": True,
            "tool_name": "close_task",
        }
        mock_proxy.call_tool.assert_awaited_once_with(
            "gobby-tasks",
            "close_task",
            {"task_id": "#15531", "commit_sha": "abc123"},
            preflight_enabled=True,
        )
        release_call.set()
        await asyncio.wait_for(call_finished.wait(), timeout=0.2)

    @pytest.mark.asyncio
    async def test_call_tool_emits_progress_heartbeat_for_compact_self(self) -> None:
        _, mock_proxy, run_tool = self._register_tools()

        heartbeat_seen = asyncio.Event()
        release_call = asyncio.Event()
        ctx = MagicMock()
        ctx.report_progress = AsyncMock(side_effect=lambda **_: heartbeat_seen.set())

        async def _block_until_heartbeat(*_args: Any, **_kwargs: Any) -> dict[str, str]:
            await heartbeat_seen.wait()
            await release_call.wait()
            return {"res": "compacted"}

        mock_proxy.call_tool.side_effect = _block_until_heartbeat

        with patch("gobby.mcp_proxy.wait_tools.WAIT_TOOL_HEARTBEAT_INTERVAL_SECONDS", 0.01):
            task: asyncio.Task[Any] = asyncio.create_task(
                run_tool(
                    "call_tool",
                    server_name="gobby-sessions",
                    tool_name="compact_self",
                    arguments={"rule_name": "build-coordinator-handoff"},
                    session_id="#6074",
                    ctx=ctx,
                )
            )
            await asyncio.wait_for(heartbeat_seen.wait(), timeout=0.2)
            assert ctx.report_progress.await_count >= 1
            progress_kwargs = ctx.report_progress.await_args.kwargs
            assert progress_kwargs["total"] == 90.0
            release_call.set()
            result = await asyncio.wait_for(task, timeout=0.2)

        assert result == {"res": "compacted"}

    @pytest.mark.asyncio
    async def test_call_tool_skips_progress_heartbeat_for_non_wait_tools(self) -> None:
        _, mock_proxy, run_tool = self._register_tools()

        ctx = MagicMock()
        ctx.report_progress = AsyncMock()

        await run_tool(
            "call_tool",
            server_name="gobby-skills",
            tool_name="get_skill",
            arguments={"name": "brevity"},
            ctx=ctx,
        )

        ctx.report_progress.assert_not_awaited()
        assert ctx.report_progress.await_count == 0
        assert ctx.report_progress.await_args is None


class TestEnsureDaemonRunningFailures:
    """Tests for ensure_daemon_running failure paths."""

    @pytest.mark.asyncio
    async def test_start_failure_keeps_stdio_alive(self) -> None:
        """Test ensure_daemon_running keeps stdio alive if daemon start fails."""
        with patch("gobby.mcp_proxy.stdio.load_config"):
            with patch("gobby.mcp_proxy.stdio.is_daemon_running", return_value=False):
                with patch("gobby.mcp_proxy.stdio.start_daemon_process") as mock_start:
                    mock_start.return_value = {"success": False, "error": "failed"}

                    from gobby.mcp_proxy.stdio import ensure_daemon_running

                    result = await ensure_daemon_running()

                    assert result is None

    @pytest.mark.asyncio
    async def test_health_check_timeout_keeps_stdio_alive(self) -> None:
        """Test ensure_daemon_running keeps stdio alive if health check times out."""
        with patch("gobby.mcp_proxy.stdio.load_config"):
            with patch("gobby.mcp_proxy.stdio.is_daemon_running", return_value=False):
                with patch("gobby.mcp_proxy.stdio.start_daemon_process") as mock_start:
                    mock_start.return_value = {"success": True}

                    # Always unhealthy
                    with patch(
                        "gobby.mcp_proxy.stdio.check_daemon_http_health",
                        new_callable=AsyncMock,
                        return_value=False,
                    ) as mock_health:
                        with patch(
                            "gobby.mcp_proxy.stdio.get_daemon_pid",
                            return_value=123,
                        ) as mock_pid:
                            with patch(
                                "gobby.mcp_proxy.stdio.asyncio.sleep", new_callable=AsyncMock
                            ) as mock_sleep:
                                from gobby.mcp_proxy.stdio import (
                                    DAEMON_HEALTH_ATTEMPTS,
                                    ensure_daemon_running,
                                )

                                result = await ensure_daemon_running()

                                assert result is None
                                assert mock_health.await_count == DAEMON_HEALTH_ATTEMPTS
                                assert mock_sleep.await_count == DAEMON_HEALTH_ATTEMPTS
                                mock_pid.assert_called_once_with()


class TestStripNone:
    """Tests for _strip_none utility that prevents null fields in MCP payloads."""

    def test_strips_none_from_flat_dict(self) -> None:
        assert _strip_none({"a": 1, "b": None, "c": "x"}) == {"a": 1, "c": "x"}

    def test_strips_none_from_nested_dict(self) -> None:
        result = _strip_none({"outer": {"inner": None, "keep": 1}, "top": None})
        assert result == {"outer": {"keep": 1}}

    def test_strips_none_from_list_elements(self) -> None:
        result = _strip_none([{"a": None, "b": 1}, {"c": None}])
        assert result == [{"b": 1}, {}]

    def test_preserves_falsy_non_none_values(self) -> None:
        data = {"zero": 0, "false": False, "empty_str": "", "empty_list": []}
        assert _strip_none(data) == data

    def test_handles_already_clean_data(self) -> None:
        data = {"a": 1, "b": "hello", "c": [1, 2, 3]}
        assert _strip_none(data) == data

    def test_returns_non_dict_non_list_unchanged(self) -> None:
        assert _strip_none("hello") == "hello"
        assert _strip_none(42) == 42
        assert _strip_none(True) is True

    def test_handles_tool_schema_with_null_defaults(self) -> None:
        """Reproduce the exact pattern that breaks LMStudio Jinja templates."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "opt": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                    "title": "Opt",
                },
            },
            "required": ["name"],
            "title": "exampleArguments",
        }
        result = _strip_none(schema)
        # "default": None should be stripped
        assert "default" not in result["properties"]["opt"]
        # Everything else preserved
        assert result["properties"]["name"] == {"type": "string"}
        assert result["required"] == ["name"]
