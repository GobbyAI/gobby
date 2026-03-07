"""Tests for the MCP proxy stdio module."""

import signal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.stdio import (
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

    def test_returns_pid_when_daemon_process_found(self) -> None:
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
                            "60887",
                        ],
                    }
                ),
            ]
            assert get_daemon_pid() == 12345

    def test_ignores_current_process(self) -> None:
        """Test ignores the current process even if it matches."""
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
    async def test_returns_already_running_if_daemon_running(self):
        """Test returns already_running if daemon is already running."""
        with patch("gobby.mcp_proxy.daemon_control.is_daemon_running", return_value=True):
            with patch("gobby.mcp_proxy.daemon_control.get_daemon_pid", return_value=12345):
                result = await start_daemon_process(60887, 60888)

                assert result["success"] is False
                assert result["already_running"] is True
                assert result["pid"] == 12345

    @pytest.mark.asyncio
    async def test_starts_daemon_successfully(self):
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
    async def test_handles_start_failure(self):
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
    async def test_handles_timeout(self):
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
    async def test_handles_exception(self):
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

            def kill_side_effect(pid, sig):
                if sig == 0:
                    raise ProcessLookupError("Process gone")
                return None

            with patch(
                "gobby.mcp_proxy.daemon_control.os.kill", side_effect=kill_side_effect
            ) as mock_kill:
                with patch("gobby.mcp_proxy.daemon_control.asyncio.sleep", new_callable=AsyncMock):
                    result = await stop_daemon_process()

                    assert result["success"] is True
                    assert result["output"] == "Daemon stopped"
                    mock_kill.assert_any_call(12345, signal.SIGTERM)

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
                        mock_stop.assert_called_once_with(12345)
                        mock_start.assert_called_once_with(60887, 60888)


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
            result = await check_daemon_http_health(60887)
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
            result = await check_daemon_http_health(60887)
            assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_connection_error(self):
        """Test returns False when connection fails."""
        mock_client = AsyncMock()
        mock_client.get.side_effect = Exception("Connection refused")
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        with patch("gobby.mcp_proxy.daemon_control.httpx.AsyncClient", return_value=mock_client):
            result = await check_daemon_http_health(60887)
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


class TestEnsureDaemonRunning:
    """Tests for ensure_daemon_running function."""

    @pytest.mark.asyncio
    async def test_does_nothing_if_healthy(self):
        """Test does nothing if daemon is already healthy."""
        with patch("gobby.mcp_proxy.stdio.load_config") as mock_config:
            mock_config.return_value = MagicMock(daemon_port=60887, websocket=MagicMock(port=60888))
            with patch("gobby.mcp_proxy.stdio.is_daemon_running", return_value=True):
                with patch(
                    "gobby.mcp_proxy.stdio.check_daemon_http_health",
                    new_callable=AsyncMock,
                    return_value=True,
                ):
                    # Should not raise or call start
                    # Must import function from module to ensure patches apply
                    from gobby.mcp_proxy.stdio import ensure_daemon_running

                    await ensure_daemon_running()

    @pytest.mark.asyncio
    async def test_restarts_unhealthy_daemon(self):
        """Test restarts daemon if running but unhealthy."""
        with patch("gobby.mcp_proxy.stdio.load_config") as mock_config:
            mock_config.return_value = MagicMock(daemon_port=60887, websocket=MagicMock(port=60888))
            with patch("gobby.mcp_proxy.stdio.is_daemon_running", return_value=True):
                health_checks = [False, False, False, True]
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
                            with patch("gobby.mcp_proxy.stdio.asyncio.sleep", new_callable=AsyncMock):
                                from gobby.mcp_proxy.stdio import ensure_daemon_running

                                await ensure_daemon_running()
                                mock_restart.assert_called_once()

    @pytest.mark.asyncio
    async def test_starts_daemon_if_not_running(self):
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


class TestDaemonProxy:
    """Tests for DaemonProxy."""

    @pytest.mark.asyncio
    async def test_request_handles_empty_exception_message(self):
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
    async def test_call_tool_uses_extended_timeout_for_expand_task(self):
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
                    "POST", "/api/mcp/server/tools/normal_tool", json={}, timeout=30.0
                )
                await proxy.call_tool("server", "expand_task", {})
                mock_request.assert_called_with(
                    "POST", "/api/mcp/server/tools/expand_task", json={}, timeout=300.0
                )


class TestDaemonProxyMethods:
    """Tests for DaemonProxy specific methods."""

    @pytest.mark.asyncio
    async def test_list_tools(self):
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
    async def test_get_tool_schema_success(self):
        from gobby.mcp_proxy.stdio import DaemonProxy

        proxy = DaemonProxy(60887)
        with patch.object(proxy, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"name": "tool", "description": "desc", "inputSchema": {}}
            result = await proxy.get_tool_schema("srv", "tool")
            assert result["success"] is True
            assert result["tool"]["name"] == "tool"

    @pytest.mark.asyncio
    async def test_list_mcp_servers(self):
        from gobby.mcp_proxy.stdio import DaemonProxy

        proxy = DaemonProxy(60887)
        with patch.object(proxy, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {
                "total": 1,
                "connected": 1,
                "servers": [{"name": "srv1", "state": "connected", "transport": "http"}],
            }
            result = await proxy.list_mcp_servers()
            assert result["total"] == 1
            assert result["servers"][0]["name"] == "srv1"

    @pytest.mark.asyncio
    async def test_recommend_tools(self):
        from gobby.mcp_proxy.stdio import DaemonProxy

        proxy = DaemonProxy(60887)
        with patch.object(proxy, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"tools": ["t1"]}
            result = await proxy.recommend_tools("task")
            assert result["tools"] == ["t1"]

    @pytest.mark.asyncio
    async def test_search_tools(self):
        from gobby.mcp_proxy.stdio import DaemonProxy

        proxy = DaemonProxy(60887)
        with patch.object(proxy, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"tools": ["t1"]}
            result = await proxy.search_tools("query")
            assert result["tools"] == ["t1"]

    @pytest.mark.asyncio
    async def test_init_project(self):
        from gobby.mcp_proxy.stdio import DaemonProxy

        proxy = DaemonProxy(60887)
        with patch.object(proxy, "_request", new_callable=AsyncMock) as mock_req:
            result = await proxy.init_project("name")
            assert result["success"] is False
            assert "requires CLI access" in result["error"]
            mock_req.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_mcp_server(self):
        from gobby.mcp_proxy.stdio import DaemonProxy

        proxy = DaemonProxy(60887)
        with patch.object(proxy, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"success": True, "message": "Server added"}
            result = await proxy.add_mcp_server(name="n", transport="stdio", command="c")
            assert result["success"] is True
            mock_req.assert_called_once()

    @pytest.mark.asyncio
    async def test_remove_mcp_server(self):
        from gobby.mcp_proxy.stdio import DaemonProxy

        proxy = DaemonProxy(60887)
        with patch.object(proxy, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"success": True, "message": "Server removed"}
            result = await proxy.remove_mcp_server("name")
            assert result["success"] is True
            mock_req.assert_called_once()

    @pytest.mark.asyncio
    async def test_import_mcp_server(self):
        from gobby.mcp_proxy.stdio import DaemonProxy

        proxy = DaemonProxy(60887)
        with patch.object(proxy, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"success": True, "imported": ["server1"]}
            result = await proxy.import_mcp_server(from_project="p")
            assert result["success"] is True
            mock_req.assert_called_once()


class TestMCPToolsWrapper:
    """Tests for the FastMCP tools registered by register_proxy_tools."""

    @pytest.mark.asyncio
    async def test_tools_exist_and_delegate(self):
        """Test that tools are registered and delegate to proxy."""
        captured_tools = {}

        def mock_tool_decorator(name=None, **kwargs):
            def real_decorator(func):
                tool_name = name or func.__name__
                captured_tools[tool_name] = func
                return func

            return real_decorator

        mock_mcp = MagicMock()
        mock_mcp.tool.side_effect = mock_tool_decorator

        # Mock DaemonProxy to intercept calls
        mock_proxy = MagicMock()

        # Setup all proxy methods
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

        # Call the registration directly!
        register_proxy_tools(mock_mcp, mock_proxy)

        # Assertion: Did we use the mock?
        assert captured_tools, "No tools captured! Mocking failed."

        async def run_tool(_tool_name, **kwargs):
            if _tool_name in captured_tools:
                return await captured_tools[_tool_name](**kwargs)
            raise ValueError(f"Tool {_tool_name} not captured")

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
        mock_proxy.call_tool.assert_called_with("s", "t", {})

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


class TestEnsureDaemonRunningFailures:
    """Tests for ensure_daemon_running failure paths."""

    @pytest.mark.asyncio
    async def test_start_failure_exits(self):
        """Test ensure_daemon_running exits if start fails."""
        with patch("gobby.mcp_proxy.stdio.load_config"):
            with patch("gobby.mcp_proxy.stdio.is_daemon_running", return_value=False):
                with patch("gobby.mcp_proxy.stdio.start_daemon_process") as mock_start:
                    mock_start.return_value = {"success": False, "error": "failed"}

                    # Use side_effect to make sys.exit raise SystemExit
                    with patch("sys.exit", side_effect=SystemExit(1)) as mock_exit:
                        from gobby.mcp_proxy.stdio import ensure_daemon_running

                        # sys.exit(1) will raise SystemExit due to side_effect
                        with pytest.raises(SystemExit):
                            await ensure_daemon_running()

                        mock_exit.assert_called_with(1)

    @pytest.mark.asyncio
    async def test_health_check_timeout_exits(self):
        """Test ensure_daemon_running exits if health check times out."""
        with patch("gobby.mcp_proxy.stdio.load_config"):
            with patch("gobby.mcp_proxy.stdio.is_daemon_running", return_value=False):
                with patch("gobby.mcp_proxy.stdio.start_daemon_process") as mock_start:
                    mock_start.return_value = {"success": True}

                    # Always unhealthy
                    with patch(
                        "gobby.mcp_proxy.stdio.check_daemon_http_health", return_value=False
                    ):
                        with patch("gobby.mcp_proxy.stdio.get_daemon_pid", return_value=123):
                            with patch(
                                "gobby.mcp_proxy.stdio.asyncio.sleep", new_callable=AsyncMock
                            ):
                                # Use side_effect to make sys.exit raise SystemExit
                                with patch("sys.exit", side_effect=SystemExit(1)) as mock_exit:
                                    from gobby.mcp_proxy.stdio import ensure_daemon_running

                                    with pytest.raises(SystemExit):
                                        await ensure_daemon_running()
                                    mock_exit.assert_called_with(1)
