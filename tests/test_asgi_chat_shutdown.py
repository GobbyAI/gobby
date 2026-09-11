"""ASGI chat teardown must finish while lifecycle hooks still accept work."""

from contextlib import ExitStack
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import uvicorn

from gobby.runner import GobbyRunner
from gobby.runner_lifecycle_shutdown import (
    _run_graceful_shutdown_sequence,
    _shutdown_websocket_server,
)
from gobby.servers.websocket.models import WebSocketConfig
from gobby.servers.websocket.server import WebSocketServer
from gobby.shutdown_intent import ShutdownIntent


@pytest.mark.asyncio
async def test_asgi_stop_ends_chat_without_standalone_listener() -> None:
    server = WebSocketServer(
        config=WebSocketConfig(), mcp_manager=MagicMock(), auth_callback=AsyncMock()
    )
    chat = MagicMock()
    chat.stop = AsyncMock()
    server._chat_sessions["conversation"] = chat
    client = MagicMock()
    client.close = AsyncMock()
    server.clients[client] = {}
    assert server._server is None

    with (
        patch.object(server, "_cleanup_tmux", new_callable=AsyncMock),
        patch.object(server, "cleanup_voice", new_callable=AsyncMock),
        patch.object(server, "_fire_session_end", new_callable=AsyncMock) as session_end,
        patch.object(server, "_cancel_active_chat", new_callable=AsyncMock),
    ):
        await server.stop()
        await server.stop()

    session_end.assert_awaited_once_with("conversation")
    chat.stop.assert_awaited_once()
    assert not server.web_chat_session_registry.sessions
    client.close.assert_awaited()


@pytest.mark.asyncio
async def test_runner_stops_asgi_chat_without_standalone_listener() -> None:
    active_chats = {"conversation"}

    async def stop() -> None:
        active_chats.clear()

    websocket = SimpleNamespace(_server=None, stop=stop)
    runner = cast(GobbyRunner, SimpleNamespace(websocket_server=websocket, _websocket_task=None))
    await _shutdown_websocket_server(runner)
    assert active_chats == set()


@pytest.mark.asyncio
async def test_chat_end_precedes_http_lifespan_hook_shutdown() -> None:
    events: list[str] = []
    hook_worker = SimpleNamespace(closed=False, accepted=[])
    runner = MagicMock()
    runner.communications_manager = None
    runner.http_server._cleanup_pending_interactions = AsyncMock()
    runner.http_server._terminate_streamable_http_sessions = AsyncMock()
    runner.lifecycle_manager.stop = AsyncMock()
    runner.mcp_proxy.disconnect_all = AsyncMock()
    runner.terminal_config = None

    async def close_http(*args: object, **kwargs: object) -> None:
        hook_worker.closed = True
        events.append("hook worker closed")

    async def stop_chat(_runner: GobbyRunner) -> None:
        if not hook_worker.closed:
            hook_worker.accepted.append("conversation")
        events.append("chat SESSION_END")

    with ExitStack() as stack:
        for name in (
            "settle_uvicorn_http_server",
            "_stop_started_services",
            "_cleanup_pipeline_background_tasks",
            "_cancel_periodic_tasks",
            "_close_managers_and_storage",
        ):
            stack.enter_context(
                patch("gobby.runner_lifecycle_shutdown." + name, new_callable=AsyncMock)
            )
        stack.enter_context(patch("gobby.runner_lifecycle_shutdown._stop_ui_dev_server_if_needed"))
        stack.enter_context(
            patch("gobby.runner_lifecycle_shutdown.begin_uvicorn_http_shutdown", close_http)
        )
        stack.enter_context(
            patch("gobby.telemetry.rule_allow_audit.shutdown_rule_allow_audit", AsyncMock())
        )
        await _run_graceful_shutdown_sequence(
            runner,
            cast(uvicorn.Server, MagicMock()),
            MagicMock(),
            1,
            shutdown_intent=ShutdownIntent.RESTART,
            await_critical_stop_hook_grace_window=AsyncMock(),
            shutdown_websocket_server=stop_chat,
        )

    assert events == ["chat SESSION_END", "hook worker closed"]
    assert hook_worker.accepted == ["conversation"]
