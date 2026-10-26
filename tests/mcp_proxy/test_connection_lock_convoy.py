"""Connection-lock convoy and breaker-recovery coverage for the MCP proxy.

Task #25147: a server start that holds the per-server connection lock past
the waiters' budget must not fail those waiters once the server answers, a
call that arrives during the breaker's recovery probe must share the probe's
outcome, and an owner task that absorbs cancellation must not keep
``connect()`` open past its cleanup bound.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Any

import pytest
from mcp.client import Transport

from gobby.mcp_proxy.lazy import CircuitBreaker, CircuitState
from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.models import ConnectionState, MCPError, MCPServerConfig
from gobby.mcp_proxy.transports.base import BaseTransportConnection, OwnerTaskTransportConnection
from tests._timing import drain_asyncio_tasks, wait_for_async_condition, wait_forever

pytestmark = pytest.mark.unit

CONNECT_TIMEOUT = 0.2
SETTLE_BOUND = 5.0
TOOL_RESULT: dict[str, Any] = {"outlets": []}


class FakeSession:
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return TOOL_RESULT


class ConnectedConnection(BaseTransportConnection):
    """A connection the manager treats as live."""

    def __init__(self, config: MCPServerConfig, session: FakeSession) -> None:
        super().__init__(config)
        self.fake_session = session
        self._session = session
        self._state = ConnectionState.CONNECTED

    async def connect(self) -> FakeSession:
        return self.fake_session

    async def disconnect(self) -> None:
        self._session = None
        self._state = ConnectionState.DISCONNECTED


class ServerStart:
    """Stand-in for ``MCPClientManager._connect_server`` driven by the test.

    Until ``reachable`` is set, a start blocks like a hung stdio launch; once
    cancelled it stays in teardown until ``teardown_gate`` opens, absorbing
    further cancellation the way an abandoned owner task's cleanup does. Once
    reachable, a start publishes a live connection, waiting on ``release``
    first when ``hold`` is set. ``fail_fast`` makes a start fail immediately.
    """

    def __init__(self, manager: MCPClientManager, session: FakeSession) -> None:
        self.manager = manager
        self.session = session
        self.entered = asyncio.Event()
        self.reachable = asyncio.Event()
        self.hold = asyncio.Event()
        self.release = asyncio.Event()
        self.teardown_gate = asyncio.Event()
        self.fail_fast = False
        self.attempts = 0

    async def __call__(self, config: MCPServerConfig) -> FakeSession:
        self.attempts += 1
        self.entered.set()
        if self.fail_fast:
            raise MCPError("server start failed")
        if not self.reachable.is_set():
            try:
                await self.reachable.wait()
            except asyncio.CancelledError:
                while not self.teardown_gate.is_set():
                    try:
                        await self.teardown_gate.wait()
                    except asyncio.CancelledError:
                        continue
                raise
        if self.hold.is_set():
            await self.release.wait()
        self.manager._connections[config.id] = ConnectedConnection(config, self.session)
        return self.session

    def unblock(self) -> None:
        self.reachable.set()
        self.release.set()
        self.teardown_gate.set()


def make_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[MCPClientManager, MCPServerConfig, ServerStart]:
    config = MCPServerConfig(
        name="lightspeed",
        project_id="test-project",
        transport="stdio",
        command="unused",
        connect_timeout=CONNECT_TIMEOUT,
    )
    manager = MCPClientManager(
        server_configs=[config],
        connection_timeout=CONNECT_TIMEOUT,
        max_connection_retries=0,
    )
    assert manager._lazy_connector.get_state(config.id) is not None
    server = ServerStart(manager, FakeSession())
    monkeypatch.setattr(manager, "_connect_server", server)
    return manager, config, server


def _breaker_state(breaker: CircuitBreaker) -> CircuitState:
    return breaker.state


def _outcome(task: asyncio.Task[Any]) -> Any:
    if task.cancelled():
        return asyncio.CancelledError()
    return task.exception() or task.result()


async def _settle(tasks: list[asyncio.Task[Any]], server: ServerStart) -> list[Any]:
    """Wait for every call to finish, then unblock and reap whatever is left."""
    try:
        done, _ = await asyncio.wait(tasks, timeout=SETTLE_BOUND)
        assert len(done) == len(tasks), f"{len(tasks) - len(done)} call(s) still blocked"
    finally:
        server.unblock()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return [_outcome(task) for task in tasks]


@pytest.mark.parametrize("holder_end", ["budget", "caller_gave_up"])
async def test_waiters_survive_a_start_that_holds_the_lock_past_their_budget(
    monkeypatch: pytest.MonkeyPatch, holder_end: str
) -> None:
    manager, config, server = make_manager(monkeypatch)
    lock = manager._lazy_connector.get_connection_lock(config.id)
    loop = asyncio.get_running_loop()

    holder = asyncio.create_task(manager.ensure_connected(config.id))
    await wait_for_async_condition(server.entered.is_set, description="the start to begin")
    waiters = [
        asyncio.create_task(manager.call_tool(config.id, "ListOutlets", {})) for _ in range(3)
    ]
    budgets_lapsed = loop.time() + 2 * CONNECT_TIMEOUT
    if holder_end == "caller_gave_up":
        # The client behind the holder times out and drops the request.
        holder.cancel()
    try:
        await wait_for_async_condition(
            lambda: loop.time() >= budgets_lapsed,
            timeout=SETTLE_BOUND,
            description="every waiter's lock budget to lapse",
        )
        assert server.attempts == 1
        assert lock.locked(), "the abandoned start must still hold the lock"
        assert not holder.done()
        server.reachable.set()
        server.teardown_gate.set()
    finally:
        outcomes = await _settle([holder, *waiters], server)

    assert outcomes[1:] == [TOOL_RESULT] * 3
    assert await manager.call_tool(config.id, "ListOutlets", {}) == TOOL_RESULT


async def test_call_during_breaker_recovery_probe_succeeds_once_server_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, config, server = make_manager(monkeypatch)
    state = manager._lazy_connector.get_state(config.id)
    assert state is not None
    breaker = state.circuit_breaker

    server.fail_fast = True
    for _ in range(breaker.failure_threshold):
        with pytest.raises(MCPError, match="Failed to connect"):
            await manager.ensure_connected(config.id)
    assert _breaker_state(breaker) is CircuitState.OPEN

    # The server answers again, but slowly; the breaker's recovery window has passed.
    server.fail_fast = False
    server.reachable.set()
    server.hold.set()
    server.entered.clear()
    breaker.recovery_timeout = 0.0

    probe = asyncio.create_task(manager.call_tool(config.id, "ListOutlets", {}))
    await wait_for_async_condition(server.entered.is_set, description="the probe to begin")
    # The sync job's discovery and tool call arrive while the probe is still starting.
    followers = [
        asyncio.create_task(manager.call_tool(config.id, "ListOutlets", {})) for _ in range(2)
    ]
    await drain_asyncio_tasks(cycles=3)
    server.release.set()
    results = await _settle([probe, *followers], server)

    assert results == [TOOL_RESULT] * 3
    assert _breaker_state(breaker) is CircuitState.CLOSED


class StuckExitTransport(OwnerTaskTransportConnection):
    """A start that never publishes a session and whose exit absorbs cancellation."""

    _OWNER_TASK_SHUTDOWN_TIMEOUT = 0.05

    def __init__(self, config: MCPServerConfig) -> None:
        super().__init__(config)
        self.child_exited = asyncio.Event()

    async def _open_transport(self, stack: AsyncExitStack) -> Transport:
        stack.push_async_callback(self._exit_like_a_shielded_scope)
        await wait_forever()
        raise AssertionError("unreachable")

    async def _exit_like_a_shielded_scope(self) -> None:
        while True:
            try:
                await self.child_exited.wait()
                return
            except asyncio.CancelledError:
                continue


@pytest.mark.parametrize("trigger", ["budget", "caller_cancel"])
async def test_connect_unwinds_within_its_cleanup_bound_when_the_owner_task_ignores_cancel(
    trigger: str,
) -> None:
    config = MCPServerConfig(
        name="lightspeed",
        project_id="test-project",
        transport="stdio",
        command="unused",
        connect_timeout=CONNECT_TIMEOUT,
    )
    connection = StuckExitTransport(config)
    bound = CONNECT_TIMEOUT + 2 * connection._OWNER_TASK_SHUTDOWN_TIMEOUT + 1.0

    connect = asyncio.create_task(connection.connect())
    owner = await wait_for_async_condition(
        lambda: connection._owner_task, description="the owner task to start"
    )
    assert owner is not None
    try:
        if trigger == "caller_cancel":
            connect.cancel()
        done, _ = await asyncio.wait({connect}, timeout=bound)
        assert connect in done, f"connect() was still blocked after {bound}s"
    finally:
        connection.child_exited.set()
        await asyncio.gather(connect, owner, return_exceptions=True)

    if trigger == "budget":
        with pytest.raises(MCPError, match="Connection timeout"):
            connect.result()
        assert connection.state is ConnectionState.FAILED
    else:
        assert connect.cancelled()
        assert connection.state is ConnectionState.DISCONNECTED
    assert connection._owner_task is None
