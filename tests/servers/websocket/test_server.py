"""Tests for the WebSocket server wrapper."""

import asyncio
import json
import logging
import time
from types import MappingProxyType, SimpleNamespace
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
    _off_loop_chain_key,
    websockets_logger,
)
from gobby.terminals.leases import TerminalLeaseRegistry

pytestmark = pytest.mark.unit


def test_workspace_op_chain_ignores_terminal_id() -> None:
    """A workspace_op stays in the workspace queue even when it names a terminal."""
    message = json.dumps({"type": "workspace_op", "terminal_id": "term-1", "op": "pane.resize"})
    assert _off_loop_chain_key(message) == "workspace_op"


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


class _AttachThenInputSocket(_ScriptedSocket):
    """Yields attach, then one injected input, and stays open until closed."""

    def __init__(self, attach_message: str) -> None:
        super().__init__([attach_message], hold_open=True)
        self._wake = asyncio.Event()
        self._returned = 0
        self._input_returned = False
        self.input_observed = asyncio.Event()
        self.both = asyncio.Event()
        self.write_seen = asyncio.Event()

    def inject(self, message: str) -> None:
        self._pending.append(message)
        self._wake.set()

    def close(self) -> None:
        super().close()
        self._wake.set()

    async def __anext__(self) -> str:
        while True:
            if self._pending:
                message = self._pending.pop(0)
                self._returned += 1
                if self._returned > 1:
                    self._input_returned = True
                return message
            if self._input_returned:
                self.input_observed.set()
            if self._closed.is_set():
                raise StopAsyncIteration
            self._wake.clear()
            if self._pending or self._closed.is_set():
                continue
            await self._wake.wait()

    async def send(self, payload: str) -> None:
        await super().send(payload)
        types = _frame_types(self)
        if "terminal_write_outcome" in types:
            self.write_seen.set()
        if "terminal_attach_result" in types and "terminal_write_outcome" in types:
            self.both.set()


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
@pytest.mark.parametrize(
    "message_type",
    ["terminal_take_control", "workspace_op", "terminal_resize"],
)
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


@pytest.mark.asyncio
async def test_terminal_input_for_the_same_attachment_waits_for_attach() -> None:
    """Input for the attachment just granted must not run while attach is still pending."""
    server = WebSocketServer(
        config=WebSocketConfig(),
        mcp_manager=MagicMock(),
        auth_callback=AsyncMock(return_value="test-user"),
    )
    _quiet_disconnect(server)
    row = SimpleNamespace(id="term-1", backend="native", rows=24, cols=80, state="live")
    server.terminal_manager = SimpleNamespace(
        get=lambda terminal_id: row if terminal_id == row.id else None
    )
    server.lease_registry = TerminalLeaseRegistry(daemon_epoch="test-epoch")
    hub = SimpleNamespace(start_pump=lambda _attachment_id: None, attachments={})
    object.__setattr__(server, "_proxy", lambda: hub)
    entered = asyncio.Event()
    release = asyncio.Event()
    attached: list[str] = []

    async def slow_backend(_websocket: Any, _row: Any, record: Any, _encoding: str) -> None:
        attached.append(record.attachment_id)
        entered.set()
        await release.wait()
        return None

    object.__setattr__(server, "_start_proxy_attach", slow_backend)
    socket = _AttachThenInputSocket(
        json.dumps(
            {
                "type": "terminal_attach",
                "request_id": "attach-1",
                "terminal_id": row.id,
                "frame_delivery": "proxy",
            }
        )
    )
    connection = asyncio.create_task(server.handle_connection(socket))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        socket.inject(
            json.dumps(
                {
                    "type": "terminal_input",
                    "request_id": "input-1",
                    "terminal_id": row.id,
                    "attachment_id": attached[0],
                    "data": "x",
                    "client_write_seq": 1,
                }
            )
        )
        await asyncio.wait_for(socket.input_observed.wait(), timeout=2)
        assert "terminal_write_outcome" not in _frame_types(socket)
        release.set()
        await asyncio.wait_for(socket.both.wait(), timeout=2)
        frames = [json.loads(payload) for payload in socket.sent]
        attach_result = next(
            frame for frame in frames if frame.get("type") == "terminal_attach_result"
        )
        write = next(frame for frame in frames if frame.get("type") == "terminal_write_outcome")
        assert attach_result["success"] is True
        assert write["attachment_id"] == attach_result["attachment_id"] == attached[0]
        types = _frame_types(socket)
        assert types.index("terminal_attach_result") < types.index("terminal_write_outcome")
    finally:
        release.set()
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
async def test_disconnect_during_attach_closes_the_proxy_frame() -> None:
    """A cancel inside the proxy handshake must close the host frame it opened."""
    server = WebSocketServer(
        config=WebSocketConfig(),
        mcp_manager=MagicMock(),
        auth_callback=AsyncMock(return_value="test-user"),
    )
    _quiet_disconnect(server)
    row = SimpleNamespace(id="term-1", backend="native", rows=24, cols=80, state="live")
    server.terminal_manager = SimpleNamespace(
        get=lambda terminal_id: row if terminal_id == row.id else None
    )
    server.lease_registry = TerminalLeaseRegistry(daemon_epoch="test-epoch")
    started = asyncio.Event()
    closed = asyncio.Event()

    class _Frame:
        def close(self) -> None:
            closed.set()

        async def read_message(self) -> None:
            await asyncio.Event().wait()

    async def resolve_locator(_row: Any) -> tuple[Any, None]:
        return SimpleNamespace(), None

    async def open_frame(_locator: Any) -> _Frame:
        return _Frame()

    async def start_proxy(_websocket: Any, **_kwargs: Any) -> None:
        started.set()
        await asyncio.Event().wait()

    object.__setattr__(server, "_resolve_attach_locator", resolve_locator)
    object.__setattr__(server, "open_proxy_frame", open_frame)
    object.__setattr__(
        server,
        "_proxy",
        lambda: SimpleNamespace(
            start_proxy=start_proxy,
            start_pump=lambda _attachment_id: None,
            attachments={},
        ),
    )
    socket = _ScriptedSocket(
        [
            json.dumps(
                {
                    "type": "terminal_attach",
                    "request_id": "attach-1",
                    "terminal_id": row.id,
                    "frame_delivery": "proxy",
                }
            )
        ],
        hold_open=True,
    )
    connection = asyncio.create_task(server.handle_connection(socket))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        socket.close()
        await asyncio.wait_for(connection, timeout=2)
        assert closed.is_set()
    finally:
        socket.close()
        connection.cancel()
        try:
            await connection
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_disconnect_lets_workspace_op_finish_after_its_commit() -> None:
    """A dropped socket must not cancel a workspace_op that already committed."""
    server = WebSocketServer(
        config=WebSocketConfig(),
        mcp_manager=MagicMock(),
        auth_callback=AsyncMock(return_value="test-user"),
    )
    _quiet_disconnect(server)
    committed = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def slow(_websocket: Any, _data: dict[str, Any]) -> None:
        committed.set()
        await release.wait()
        finished.set()

    server._dispatch_table = {"workspace_op": slow}
    socket = _ScriptedSocket(
        [json.dumps({"type": "workspace_op", "request_id": "op-1", "op": "tab.close"})],
        hold_open=True,
    )
    connection = asyncio.create_task(server.handle_connection(socket))
    try:
        await asyncio.wait_for(committed.wait(), timeout=2)
        socket.close()
        await asyncio.wait_for(connection, timeout=2)
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=2)
        assert finished.is_set()
    finally:
        release.set()
        socket.close()
        connection.cancel()
        try:
            await connection
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_disconnect_cancels_an_in_flight_pane_wait() -> None:
    """pane.wait_for_output is a read. Disconnect cancels it instead of leaving the poller."""
    server = WebSocketServer(
        config=WebSocketConfig(),
        mcp_manager=MagicMock(),
        auth_callback=AsyncMock(return_value="test-user"),
    )
    _quiet_disconnect(server)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow(_websocket: Any, _data: dict[str, Any]) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    server._dispatch_table = {"workspace_op": slow}
    socket = _ScriptedSocket(
        [
            json.dumps(
                {
                    "type": "workspace_op",
                    "request_id": "wait-1",
                    "op": "pane.wait_for_output",
                    "timeout_seconds": 1e9,
                }
            )
        ],
        hold_open=True,
    )
    connection = asyncio.create_task(server.handle_connection(socket))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        socket.close()
        await asyncio.wait_for(connection, timeout=2)
        await asyncio.wait_for(cancelled.wait(), timeout=2)
        assert cancelled.is_set()
    finally:
        socket.close()
        connection.cancel()
        try:
            await connection
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_stop_bounds_a_durable_workspace_op() -> None:
    """stop() must not leave a kept-alive workspace_op running after shutdown."""
    server = WebSocketServer(
        config=WebSocketConfig(),
        mcp_manager=MagicMock(),
        auth_callback=AsyncMock(return_value="test-user"),
    )
    _quiet_disconnect(server)
    server.lease_registry = TerminalLeaseRegistry(daemon_epoch="stop-bound")
    object.__setattr__(server, "_cleanup_tmux", AsyncMock())
    object.__setattr__(server, "cleanup_voice", AsyncMock())
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow(_websocket: Any, _data: dict[str, Any]) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    server._dispatch_table = {"workspace_op": slow}
    socket = _ScriptedSocket(
        [json.dumps({"type": "workspace_op", "request_id": "op-1", "op": "tab.close"})],
        hold_open=True,
    )
    connection = asyncio.create_task(server.handle_connection(socket))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        await server.stop()
        await asyncio.wait_for(cancelled.wait(), timeout=1)
        assert cancelled.is_set()
        assert [task for task in server._off_loop_durable if not task.done()] == []
    finally:
        socket.close()
        connection.cancel()
        try:
            await connection
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_input_for_another_terminal_runs_during_attach() -> None:
    """Attaching pane B must not hold input that belongs to pane A."""
    server = WebSocketServer(
        config=WebSocketConfig(),
        mcp_manager=MagicMock(),
        auth_callback=AsyncMock(return_value="test-user"),
    )
    _quiet_disconnect(server)
    row_a = SimpleNamespace(id="term-a", backend="native", rows=24, cols=80, state="live")
    row_b = SimpleNamespace(id="term-b", backend="native", rows=24, cols=80, state="live")
    rows = {row_a.id: row_a, row_b.id: row_b}
    server.terminal_manager = SimpleNamespace(get=lambda terminal_id: rows.get(terminal_id))
    server.lease_registry = TerminalLeaseRegistry(daemon_epoch="test-epoch")
    attached = await server.lease_registry.attach(row_a.id, backend="native")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_backend(_websocket: Any, _row: Any, _record: Any, _encoding: str) -> None:
        entered.set()
        await release.wait()
        return None

    object.__setattr__(server, "_start_proxy_attach", slow_backend)
    object.__setattr__(
        server,
        "_proxy",
        lambda: SimpleNamespace(start_pump=lambda _attachment_id: None, attachments={}),
    )
    socket = _AttachThenInputSocket(
        json.dumps(
            {
                "type": "terminal_attach",
                "request_id": "attach-b",
                "terminal_id": row_b.id,
                "frame_delivery": "proxy",
            }
        )
    )
    connection = asyncio.create_task(server.handle_connection(socket))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        socket.inject(
            json.dumps(
                {
                    "type": "terminal_input",
                    "request_id": "input-a",
                    "terminal_id": row_a.id,
                    "attachment_id": attached.attachment_id,
                    "data": "x",
                    "client_write_seq": 1,
                }
            )
        )
        await asyncio.wait_for(socket.write_seen.wait(), timeout=2)
        assert "terminal_attach_result" not in _frame_types(socket)
    finally:
        release.set()
        socket.close()
        connection.cancel()
        try:
            await connection
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_release_for_the_attaching_terminal_waits() -> None:
    """Releasing pane A must not run while pane A's attach is still in progress."""
    server = WebSocketServer(
        config=WebSocketConfig(),
        mcp_manager=MagicMock(),
        auth_callback=AsyncMock(return_value="test-user"),
    )
    _quiet_disconnect(server)
    row = SimpleNamespace(id="term-a", backend="native", rows=24, cols=80, state="live")
    server.terminal_manager = SimpleNamespace(
        get=lambda terminal_id: row if terminal_id == row.id else None
    )
    server.lease_registry = TerminalLeaseRegistry(daemon_epoch="test-epoch")
    entered = asyncio.Event()
    release = asyncio.Event()
    attached: list[str] = []

    async def slow_backend(_websocket: Any, _row: Any, record: Any, _encoding: str) -> None:
        attached.append(record.attachment_id)
        entered.set()
        await release.wait()
        return None

    object.__setattr__(server, "_start_proxy_attach", slow_backend)
    object.__setattr__(
        server,
        "_proxy",
        lambda: SimpleNamespace(start_pump=lambda _attachment_id: None, attachments={}),
    )
    socket = _AttachThenInputSocket(
        json.dumps(
            {
                "type": "terminal_attach",
                "request_id": "attach-a",
                "terminal_id": row.id,
                "frame_delivery": "proxy",
            }
        )
    )
    connection = asyncio.create_task(server.handle_connection(socket))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        socket.inject(
            json.dumps(
                {
                    "type": "terminal_release_control",
                    "request_id": "release-a",
                    "terminal_id": row.id,
                    "attachment_id": attached[0],
                }
            )
        )
        await asyncio.wait_for(socket.input_observed.wait(), timeout=2)
        assert "terminal_control_result" not in _frame_types(socket)
    finally:
        release.set()
        socket.close()
        connection.cancel()
        try:
            await connection
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_cancelled_attach_does_not_drop_the_next_frame() -> None:
    """Cancelling one chained frame must still run the next frame for that terminal."""
    server = WebSocketServer(
        config=WebSocketConfig(),
        mcp_manager=MagicMock(),
        auth_callback=AsyncMock(return_value="test-user"),
    )
    _quiet_disconnect(server)
    started: list[str] = []
    hold = asyncio.Event()
    first_entered = asyncio.Event()
    second_sent = asyncio.Event()

    async def attach(websocket: Any, data: dict[str, Any]) -> None:
        request_id = str(data.get("request_id"))
        started.append(request_id)
        if request_id == "first":
            first_entered.set()
            await hold.wait()
        await websocket.send(
            json.dumps({"type": "terminal_attach_result", "request_id": request_id})
        )
        if request_id == "second":
            second_sent.set()

    server._dispatch_table = {"terminal_attach": attach}
    socket = _AttachThenInputSocket(
        json.dumps({"type": "terminal_attach", "request_id": "first", "terminal_id": "term-a"})
    )
    connection = asyncio.create_task(server.handle_connection(socket))
    try:
        await asyncio.wait_for(first_entered.wait(), timeout=2)
        assert started == ["first"]
        first_tasks = list(server._off_loop_tasks[socket])
        socket.inject(
            json.dumps({"type": "terminal_attach", "request_id": "second", "terminal_id": "term-a"})
        )
        await asyncio.wait_for(socket.input_observed.wait(), timeout=2)
        for task in first_tasks:
            task.cancel()
        await asyncio.wait_for(second_sent.wait(), timeout=2)
        assert started == ["first", "second"]
    finally:
        hold.set()
        socket.close()
        connection.cancel()
        try:
            await connection
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_chained_handler_failure_is_reported_and_the_queue_continues() -> None:
    """A chained handler that raises reports an error and still runs the next frame."""
    server = WebSocketServer(
        config=WebSocketConfig(),
        mcp_manager=MagicMock(),
        auth_callback=AsyncMock(return_value="test-user"),
    )
    _quiet_disconnect(server)
    ran: list[str] = []
    finished = asyncio.Event()

    async def attach(_websocket: Any, data: dict[str, Any]) -> None:
        request_id = str(data.get("request_id"))
        ran.append(request_id)
        if request_id == "boom":
            raise RuntimeError("attach failed")
        finished.set()

    server._dispatch_table = {"terminal_attach": attach}
    socket = _ScriptedSocket(
        [
            json.dumps({"type": "terminal_attach", "request_id": "boom", "terminal_id": "term-a"}),
            json.dumps({"type": "terminal_attach", "request_id": "next", "terminal_id": "term-a"}),
        ],
        hold_open=True,
    )
    connection = asyncio.create_task(server.handle_connection(socket))
    try:
        await asyncio.wait_for(finished.wait(), timeout=2)
        assert ran == ["boom", "next"]
        assert any("Internal server error" in payload for payload in socket.sent)
    finally:
        socket.close()
        connection.cancel()
        try:
            await connection
        except asyncio.CancelledError:
            pass
