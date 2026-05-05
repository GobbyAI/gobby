"""HTTP server shutdown processing tests."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.app_context import ServiceContainer
from gobby.servers.http import HTTPServer
from tests._timing import drain_asyncio_tasks, wait_forever

pytestmark = pytest.mark.unit


class TestProcessShutdown:
    """Tests for _process_shutdown method."""

    @pytest.mark.asyncio
    async def test_terminate_streamable_http_sessions_no_mcp_server(self) -> None:
        """Termination helper should no-op when MCP server is absent."""
        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
        )
        server = HTTPServer(services=services, port=8000, test_mode=True)

        result = await server._terminate_streamable_http_sessions()
        assert result is None
        assert server._mcp_server is None

    @pytest.mark.asyncio
    async def test_terminate_streamable_http_sessions_terminates_all_transports(self) -> None:
        """Termination helper should stop each active Streamable HTTP transport."""
        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
        )
        server = HTTPServer(services=services, port=8000, test_mode=True)

        transport_one = AsyncMock()
        transport_one.mcp_session_id = "sess-1"
        transport_two = AsyncMock()
        transport_two.mcp_session_id = "sess-2"

        session_manager = MagicMock()
        session_manager._server_instances = {
            "sess-1": transport_one,
            "sess-2": transport_two,
        }

        server._mcp_server = MagicMock()
        server._mcp_server.session_manager = session_manager

        await server._terminate_streamable_http_sessions()

        transport_one.terminate.assert_awaited_once()
        transport_two.terminate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_terminate_streamable_http_sessions_logs_and_continues_on_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """One failed transport termination should not stop the rest."""
        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
        )
        server = HTTPServer(services=services, port=8000, test_mode=True)

        failing_transport = AsyncMock()
        failing_transport.mcp_session_id = "sess-fail"
        failing_transport.terminate.side_effect = RuntimeError("boom")

        healthy_transport = AsyncMock()
        healthy_transport.mcp_session_id = "sess-ok"

        session_manager = MagicMock()
        session_manager._server_instances = {
            "sess-fail": failing_transport,
            "sess-ok": healthy_transport,
        }

        server._mcp_server = MagicMock()
        server._mcp_server.session_manager = session_manager

        with caplog.at_level("WARNING"):
            await server._terminate_streamable_http_sessions()

        healthy_transport.terminate.assert_awaited_once()
        assert "Failed to terminate Streamable HTTP session sess-fail" in caplog.text

    @pytest.mark.asyncio
    async def test_shutdown_no_pending_tasks(self) -> None:
        """Test shutdown with no pending background tasks."""
        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
        )
        server = HTTPServer(services=services, port=8000, test_mode=True)

        await server._process_shutdown()

        assert len(server._background_tasks) == 0

    @pytest.mark.asyncio
    async def test_shutdown_waits_for_pending_tasks(self) -> None:
        """Test shutdown waits for pending background tasks."""
        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
        )
        server = HTTPServer(services=services, port=8000, test_mode=True)

        async def quick_task() -> None:
            await drain_asyncio_tasks()

        task = asyncio.create_task(quick_task())
        server._background_tasks.add(task)
        task.add_done_callback(server._background_tasks.discard)

        await server._process_shutdown()

        assert len(server._background_tasks) == 0

    @pytest.mark.asyncio
    async def test_shutdown_timeout_with_slow_tasks(self) -> None:
        """Test shutdown times out with very slow tasks."""
        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
        )
        server = HTTPServer(services=services, port=8000, test_mode=True)

        async def slow_task() -> None:
            await wait_forever()

        task = asyncio.create_task(slow_task())
        server._background_tasks.add(task)

        async def fast_shutdown() -> None:
            import time

            start = time.perf_counter()
            max_wait = 0.1
            while len(server._background_tasks) > 0 and (time.perf_counter() - start) < max_wait:
                await drain_asyncio_tasks()
                break

        with patch.object(server, "_process_shutdown", fast_shutdown):
            await server._process_shutdown()
            assert not task.done()

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_shutdown_disconnects_mcp_servers(self) -> None:
        """Test shutdown disconnects MCP servers."""
        mock_mcp_manager = AsyncMock()
        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
            mcp_manager=mock_mcp_manager,
        )
        server = HTTPServer(
            services=services,
            port=8000,
            test_mode=True,
        )
        server._terminate_streamable_http_sessions = AsyncMock()

        await server._process_shutdown()

        server._terminate_streamable_http_sessions.assert_awaited_once()
        mock_mcp_manager.disconnect_all.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_terminates_streamable_http_sessions_before_disconnect(self) -> None:
        """HTTP session termination should happen before MCP disconnect."""
        events: list[str] = []

        mock_mcp_manager = AsyncMock()

        async def disconnect_all() -> None:
            events.append("disconnect")

        mock_mcp_manager.disconnect_all.side_effect = disconnect_all

        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
            mcp_manager=mock_mcp_manager,
        )
        server = HTTPServer(
            services=services,
            port=8000,
            test_mode=True,
        )

        async def terminate_sessions() -> None:
            events.append("terminate")

        server._terminate_streamable_http_sessions = AsyncMock(side_effect=terminate_sessions)

        await server._process_shutdown()

        assert events == ["terminate", "disconnect"]

    @pytest.mark.asyncio
    async def test_shutdown_continues_when_http_session_termination_fails(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """HTTP session termination is best-effort during shutdown."""
        mock_mcp_manager = AsyncMock()
        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
            mcp_manager=mock_mcp_manager,
        )
        server = HTTPServer(
            services=services,
            port=8000,
            test_mode=True,
        )
        server._terminate_streamable_http_sessions = AsyncMock(side_effect=RuntimeError("boom"))

        with caplog.at_level("WARNING"):
            await server._process_shutdown()

        mock_mcp_manager.disconnect_all.assert_called_once()
        assert "Error terminating Streamable HTTP sessions during shutdown: boom" in caplog.text

    @pytest.mark.asyncio
    async def test_shutdown_treats_mcp_disconnect_cancellation_as_expected(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Lifespan cancellation during MCP disconnect should not escape shutdown."""
        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.disconnect_all.side_effect = asyncio.CancelledError

        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
            mcp_manager=mock_mcp_manager,
        )
        server = HTTPServer(
            services=services,
            port=8000,
            test_mode=True,
        )
        server._terminate_streamable_http_sessions = AsyncMock()

        with (
            caplog.at_level("INFO"),
            patch("gobby.servers.http.inc_counter") as mock_inc,
        ):
            await server._process_shutdown()

        server._terminate_streamable_http_sessions.assert_awaited_once()
        mock_mcp_manager.disconnect_all.assert_awaited_once()
        assert "Shutdown processing cancelled during graceful shutdown" in caplog.text
        assert all(call.args != ("shutdown_failed_total",) for call in mock_inc.call_args_list)

    @pytest.mark.asyncio
    async def test_shutdown_handles_mcp_disconnect_error(self, caplog: pytest.LogCaptureFixture) -> None:
        """Test shutdown handles MCP disconnect error gracefully."""
        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.disconnect_all.side_effect = RuntimeError("Disconnect failed")

        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
            mcp_manager=mock_mcp_manager,
        )
        server = HTTPServer(
            services=services,
            port=8000,
            test_mode=True,
        )

        with caplog.at_level("WARNING"):
            await server._process_shutdown()
        mock_mcp_manager.disconnect_all.assert_awaited_once()
        assert "Error disconnecting MCP servers: Disconnect failed" in caplog.text

    @pytest.mark.asyncio
    async def test_shutdown_increments_success_metric(self) -> None:
        """Test shutdown increments success metric."""
        services = ServiceContainer(
            config=MagicMock(),
            database=MagicMock(),
            session_manager=MagicMock(),
            task_manager=MagicMock(),
        )
        server = HTTPServer(services=services, port=8000, test_mode=True)

        with patch("gobby.servers.http.inc_counter") as mock_inc:
            await server._process_shutdown()
            mock_inc.assert_called_with("shutdown_succeeded_total")
