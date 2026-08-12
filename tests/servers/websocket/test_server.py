"""Tests for the WebSocket server wrapper."""

from types import MappingProxyType
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.app import DaemonConfig
from gobby.config.bootstrap import BootstrapConfig
from gobby.config.runtime import RuntimeActiveBundle
from gobby.config.runtime_models import ConfigSnapshot
from gobby.servers.websocket.models import WebSocketConfig
from gobby.servers.websocket.server import WebSocketServer, websockets_logger

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
        daemon_config=startup,
        config_runtime=cast(Any, runtime),
    )

    captured = server.daemon_config
    assert captured == startup
    assert captured is not startup

    no_runtime = WebSocketServer(config=WebSocketConfig(), mcp_manager=MagicMock())
    assert no_runtime.daemon_config is None


@pytest.mark.asyncio
async def test_message_dispatch_pins_one_runtime_bundle() -> None:
    runtime = _LiveRuntime(DaemonConfig(voice={"enabled": False}))
    server = WebSocketServer(
        config=WebSocketConfig(),
        mcp_manager=MagicMock(),
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

    server = WebSocketServer(config=config, mcp_manager=MagicMock())
    server._cleanup_idle_sessions = AsyncMock()

    with patch("gobby.servers.websocket.server.serve", new_callable=AsyncMock) as mock_serve:
        await server.start()

    assert server._cleanup_task is not None
    await server._cleanup_task
    mock_serve.assert_awaited_once()
    serve_call = mock_serve.await_args
    assert serve_call is not None
    serve_kwargs = serve_call.kwargs
    assert serve_kwargs["logger"] is websockets_logger
