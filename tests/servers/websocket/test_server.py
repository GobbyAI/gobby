"""Tests for the WebSocket server wrapper."""

import asyncio
import json
import logging
import time
from types import MappingProxyType
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.app import DaemonConfig
from gobby.config.bootstrap import BootstrapConfig
from gobby.config.runtime import RuntimeActiveBundle
from gobby.config.runtime_models import ConfigSnapshot
from gobby.servers.websocket.models import WebSocketConfig
from gobby.servers.websocket.server import (
    SLOW_WEBSOCKET_HANDLER_SECONDS,
    WebSocketServer,
    websockets_logger,
)
from gobby.terminals.leases import TerminalLeaseRegistry

pytestmark = pytest.mark.unit


def test_default_bind_is_localhost() -> None:
    assert WebSocketConfig().host == "localhost"


class _LiveRuntime:
    def __init__(self, config: DaemonConfig, *, ready: bool = True) -> None:
        self.ready = ready
        self.capture_count = 0
        self.set_active(config)

    def set_active(self, config: DaemonConfig) -> None:
        self._bundle = RuntimeActiveBundle(
            snapshot=ConfigSnapshot(
                revision=1,
                desired=config,
                active=config,
                row_revisions={},
                pending_restart_keys=frozenset(),
                failed_live_keys={},
            ),
            services=MappingProxyType({}),
        )

    def capture(self) -> RuntimeActiveBundle:
        self.capture_count += 1
        return self._bundle


@pytest.mark.unit
def test_daemon_config_reads_live_runtime_snapshot() -> None:
    startup = DaemonConfig(voice={"enabled": False})
    runtime = _LiveRuntime(DaemonConfig(voice={"enabled": False}))
    server = WebSocketServer(
        config=WebSocketConfig(),
        mcp_manager=MagicMock(),
        auth_callback=AsyncMock(return_value="test-user"),
        daemon_config=startup,
        config_runtime=cast(Any, runtime),
    )

    initial_config = server.daemon_config
    assert initial_config is not None
    assert initial_config.voice.enabled is False

    runtime.set_active(DaemonConfig(voice={"enabled": True}))

    live_config = server.daemon_config
    assert live_config is not None
    assert live_config.voice.enabled is True


@pytest.mark.unit
def test_daemon_config_serves_one_projection_per_epoch() -> None:
    runtime = _LiveRuntime(DaemonConfig())
    server = WebSocketServer(
        config=WebSocketConfig(),
        mcp_manager=MagicMock(),
        auth_callback=AsyncMock(return_value="test-user"),
        config_runtime=cast(Any, runtime),
    )

    first = server.daemon_config
    second = server.daemon_config

    assert first is second


def test_daemon_config_overlays_bootstrap_fields_on_live_snapshot() -> None:
    runtime = _LiveRuntime(DaemonConfig(daemon_port=61000))
    server = WebSocketServer(
        config=WebSocketConfig(),
        mcp_manager=MagicMock(),
        auth_callback=AsyncMock(return_value="test-user"),
        bootstrap_config=BootstrapConfig(daemon_port=62000),
        config_runtime=cast(Any, runtime),
    )

    captured = server.daemon_config

    assert captured is not None
    assert captured.daemon_port == 62000


@pytest.mark.unit
def test_daemon_config_falls_back_before_runtime_ready() -> None:
    startup = DaemonConfig(voice={"enabled": True})
    runtime = _LiveRuntime(DaemonConfig(voice={"enabled": False}), ready=False)
    server = WebSocketServer(
        config=WebSocketConfig(),
        mcp_manager=MagicMock(),
        auth_callback=AsyncMock(return_value="test-user"),
        daemon_config=startup,
        config_runtime=cast(Any, runtime),
    )

    captured = server.daemon_config
    assert captured == startup
    assert captured is not startup

    no_runtime = WebSocketServer(
        config=WebSocketConfig(),
        mcp_manager=MagicMock(),
        auth_callback=AsyncMock(return_value="test-user"),
    )
    assert no_runtime.daemon_config is None


@pytest.mark.asyncio
async def test_message_dispatch_pins_one_runtime_bundle() -> None:
    runtime = _LiveRuntime(DaemonConfig(voice={"enabled": False}))
    server = WebSocketServer(
        config=WebSocketConfig(),
        mcp_manager=MagicMock(),
        auth_callback=AsyncMock(return_value="test-user"),
        config_runtime=cast(Any, runtime),
    )
    observed: list[bool] = []

    async def handler(_websocket: Any, _data: dict[str, Any]) -> None:
        initial = server.daemon_config
        assert initial is not None
        observed.append(initial.voice.enabled)
        runtime.set_active(DaemonConfig(voice={"enabled": True}))
        repeated = server.daemon_config
        assert repeated is not None
        observed.append(repeated.voice.enabled)

    server._dispatch_table = {"test": handler}

    await server._handle_message(MagicMock(), '{"type":"test"}')
    await server._handle_message(MagicMock(), '{"type":"test"}')

    assert observed == [False, False, True, True]
    assert runtime.capture_count == 2


@pytest.mark.asyncio
async def test_start_passes_warning_level_websockets_logger() -> None:
    config = MagicMock()
    config.host = "127.0.0.1"
    config.port = 60888
    config.ping_interval = 30
    config.ping_timeout = 10
    config.max_message_size = 1024

    server = WebSocketServer(
        config=config,
        mcp_manager=MagicMock(),
        auth_callback=AsyncMock(return_value="test-user"),
    )
    server._cleanup_idle_sessions = AsyncMock()
    # Start publishes lifecycle events, so the server needs its lease registry first.
    server.lease_registry = TerminalLeaseRegistry(daemon_epoch="test-epoch")

    with patch("gobby.servers.websocket.server.serve", new_callable=AsyncMock) as mock_serve:
        await server.start()

    assert server._cleanup_task is not None
    await server._cleanup_task
    await server.lease_registry.shutdown_lifecycle_publication()
    mock_serve.assert_awaited_once()
    serve_call = mock_serve.await_args
    assert serve_call is not None
    serve_kwargs = serve_call.kwargs
    assert serve_kwargs["logger"] is websockets_logger


@pytest.mark.asyncio
@pytest.mark.parametrize(("threshold", "warns"), [(0.0, True), (60.0, False)])
async def test_slow_handler_is_logged_with_its_message_type(
    threshold: float,
    warns: bool,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Dispatch on one connection is serial, so a slow handler delays every
    message behind it; the warning names the type that held the line, and a
    handler inside the budget logs nothing (#22544)."""
    monkeypatch.setattr("gobby.servers.websocket.server.SLOW_WEBSOCKET_HANDLER_SECONDS", threshold)
    server = WebSocketServer(
        config=WebSocketConfig(),
        mcp_manager=MagicMock(),
        auth_callback=AsyncMock(return_value="test-user"),
    )
    handled: list[dict[str, Any]] = []

    async def handler(_websocket: Any, data: dict[str, Any]) -> None:
        handled.append(data)

    server._dispatch_table = {"slow_kind": handler}
    with caplog.at_level(logging.WARNING, logger="gobby.servers.websocket.server"):
        await server._handle_message(MagicMock(), '{"type":"slow_kind"}')

    assert handled == [{"type": "slow_kind"}]
    warnings = [
        record.getMessage() for record in caplog.records if record.levelno == logging.WARNING
    ]
    if not warns:
        assert warnings == []
        return
    assert len(warnings) == 1
    assert warnings[0].startswith("websocket handler slow_kind took ")
    assert warnings[0].endswith("s; later messages on this connection waited")


class _ScriptedSocket:
    """One connection that yields scripted frames and records what the server sends."""

    def __init__(self, messages: list[str], *, hold_open: bool = False) -> None:
        self._pending = list(messages)
        self._hold_open = hold_open
        self._closed = asyncio.Event()
        self.sent: list[str] = []
        self.user_id = "user-1"
        self.remote_address = "127.0.0.1"
        self.latency = 0.0

    def __aiter__(self) -> "_ScriptedSocket":
        return self

    def close(self) -> None:
        self._closed.set()

    async def __anext__(self) -> str:
        if self._pending:
            return self._pending.pop(0)
        if not self._hold_open:
            raise StopAsyncIteration
        await self._closed.wait()
        raise StopAsyncIteration

    async def send(self, payload: str) -> None:
        self.sent.append(payload)


def _quiet_disconnect(server: WebSocketServer) -> None:
    object.__setattr__(server, "_cleanup_tmux_client", AsyncMock())
    object.__setattr__(server, "_cleanup_attached_tts", AsyncMock())


def _frame_types(socket: _ScriptedSocket) -> list[str]:
    found: list[str] = []
    for payload in socket.sent:
        parsed = json.loads(payload)
        if not isinstance(parsed, dict):
            continue
        frame_type = parsed.get("type")
        if isinstance(frame_type, str):
            found.append(frame_type)
    return found


@pytest.mark.asyncio
async def test_terminal_attach_returns_the_loop_before_a_three_second_backend() -> None:
    """A 3s attach must not hold the next frame. The loop answers it within 100ms."""
    server = WebSocketServer(
        config=WebSocketConfig(),
        mcp_manager=MagicMock(),
        auth_callback=AsyncMock(return_value="test-user"),
    )
    _quiet_disconnect(server)
    started = time.monotonic()
    pong_at: list[float] = []
    attach_sent = asyncio.Event()

    async def attach(websocket: Any, data: dict[str, Any]) -> None:
        # test-quality: allow SLEEP_IN_TEST -- #22709 requires a backend of at least 3 seconds
        await asyncio.sleep(3)
        await websocket.send(
            json.dumps(
                {
                    "type": "terminal_attach_result",
                    "request_id": data.get("request_id"),
                    "success": True,
                }
            )
        )
        attach_sent.set()

    async def ping(websocket: Any, _data: dict[str, Any]) -> None:
        pong_at.append(time.monotonic())
        await websocket.send(json.dumps({"type": "pong", "request_id": "ping-1"}))

    server._dispatch_table = {"terminal_attach": attach, "ping": ping}
    socket = _ScriptedSocket(
        [
            json.dumps({"type": "terminal_attach", "request_id": "attach-1"}),
            json.dumps({"type": "ping", "request_id": "ping-1"}),
        ],
        hold_open=True,
    )
    connection = asyncio.create_task(server.handle_connection(socket))
    try:
        await asyncio.wait_for(attach_sent.wait(), timeout=5)
        assert pong_at
        assert pong_at[0] - started < 0.1
        types = _frame_types(socket)
        assert types.index("pong") < types.index("terminal_attach_result")
    finally:
        socket.close()
        try:
            await asyncio.wait_for(connection, timeout=1)
        except (TimeoutError, asyncio.CancelledError):
            connection.cancel()
            try:
                await connection
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
async def test_off_loop_attach_does_not_warn_that_later_messages_waited(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A slow attach no longer blocks the read loop, so the wait warning stays quiet."""
    server = WebSocketServer(
        config=WebSocketConfig(),
        mcp_manager=MagicMock(),
        auth_callback=AsyncMock(return_value="test-user"),
    )
    _quiet_disconnect(server)
    finished = asyncio.Event()
    ping_answered = asyncio.Event()

    async def attach(_websocket: Any, _data: dict[str, Any]) -> None:
        # test-quality: allow SLEEP_IN_TEST -- the handler must outlast the 1s wait warning
        await asyncio.sleep(SLOW_WEBSOCKET_HANDLER_SECONDS + 0.1)
        finished.set()

    async def ping(_websocket: Any, _data: dict[str, Any]) -> None:
        ping_answered.set()

    server._dispatch_table = {"terminal_attach": attach, "ping": ping}
    socket = _ScriptedSocket(
        [
            json.dumps({"type": "terminal_attach", "request_id": "attach-1"}),
            json.dumps({"type": "ping", "request_id": "ping-1"}),
        ],
        hold_open=True,
    )
    connection = asyncio.create_task(server.handle_connection(socket))
    try:
        with caplog.at_level(logging.WARNING):
            await asyncio.wait_for(ping_answered.wait(), timeout=0.1)
            await asyncio.wait_for(finished.wait(), timeout=2)
        assert "later messages on this connection waited" not in caplog.text
    finally:
        socket.close()
        connection.cancel()
        try:
            await connection
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_later_terminal_attach_waits_for_the_one_already_running() -> None:
    """Attach frames on one connection stay in arrival order. Ping does not wait."""
    server = WebSocketServer(
        config=WebSocketConfig(),
        mcp_manager=MagicMock(),
        auth_callback=AsyncMock(return_value="test-user"),
    )
    _quiet_disconnect(server)
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    ping_answered = asyncio.Event()
    started: list[str] = []

    async def attach(_websocket: Any, data: dict[str, Any]) -> None:
        request_id = str(data.get("request_id"))
        started.append(request_id)
        if request_id == "first":
            await release_first.wait()
            return
        second_started.set()

    async def ping(_websocket: Any, _data: dict[str, Any]) -> None:
        ping_answered.set()

    server._dispatch_table = {"terminal_attach": attach, "ping": ping}
    socket = _ScriptedSocket(
        [
            json.dumps({"type": "terminal_attach", "request_id": "first"}),
            json.dumps({"type": "terminal_attach", "request_id": "second"}),
            json.dumps({"type": "ping", "request_id": "ping-1"}),
        ],
        hold_open=True,
    )
    connection = asyncio.create_task(server.handle_connection(socket))
    try:
        await asyncio.wait_for(ping_answered.wait(), timeout=0.1)
        assert started == ["first"]
        release_first.set()
        await asyncio.wait_for(second_started.wait(), timeout=0.1)
        assert started == ["first", "second"]
    finally:
        release_first.set()
        socket.close()
        connection.cancel()
        try:
            await connection
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize("message_type", ["terminal_take_control", "workspace_op"])
async def test_slow_terminal_operation_leaves_the_read_loop(message_type: str) -> None:
    """take_control and workspace_op must not hold the next frame for their whole run."""
    server = WebSocketServer(
        config=WebSocketConfig(),
        mcp_manager=MagicMock(),
        auth_callback=AsyncMock(return_value="test-user"),
    )
    _quiet_disconnect(server)
    started = time.monotonic()
    pong_at: list[float] = []
    slow_sent = asyncio.Event()
    result_type = f"{message_type}_result"

    async def slow(websocket: Any, _data: dict[str, Any]) -> None:
        # test-quality: allow SLEEP_IN_TEST -- take_control requires a backend of at least 2 seconds
        await asyncio.sleep(2)
        await websocket.send(json.dumps({"type": result_type, "request_id": "slow-1"}))
        slow_sent.set()

    async def ping(websocket: Any, _data: dict[str, Any]) -> None:
        pong_at.append(time.monotonic())
        await websocket.send(json.dumps({"type": "pong", "request_id": "ping-1"}))

    server._dispatch_table = {message_type: slow, "ping": ping}
    socket = _ScriptedSocket(
        [
            json.dumps({"type": message_type, "request_id": "slow-1"}),
            json.dumps({"type": "ping", "request_id": "ping-1"}),
        ],
        hold_open=True,
    )
    connection = asyncio.create_task(server.handle_connection(socket))
    try:
        await asyncio.wait_for(slow_sent.wait(), timeout=4)
        assert pong_at
        assert pong_at[0] - started < 0.1
        types = _frame_types(socket)
        assert types.index("pong") < types.index(result_type)
    finally:
        socket.close()
        try:
            await asyncio.wait_for(connection, timeout=1)
        except (TimeoutError, asyncio.CancelledError):
            connection.cancel()
            try:
                await connection
            except asyncio.CancelledError:
                pass
