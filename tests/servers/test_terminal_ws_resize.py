"""Viewer-owned terminal sizing at the WebSocket boundary."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.config.tmux import TmuxConfig
from gobby.servers.websocket.proxy_relay import ProxyAttachment
from gobby.servers.websocket.terminal_sizing import TerminalSizingMixin
from gobby.servers.websocket.terminal_ws import TerminalWsMixin
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.terminals.tmux_runtime import TmuxTerminalRuntime
from tests.terminals.fakes import MemoryTerminalStore, make_memory_terminal


class _SizingServer(TerminalSizingMixin, TerminalWsMixin):
    def __init__(self, row: Any, runtime: TmuxTerminalRuntime) -> None:
        self.lease_registry = TerminalLeaseRegistry()
        self.terminal_manager = MemoryTerminalStore(row)
        self.runtime = runtime
        self.sent: list[dict[str, Any]] = []

    def _leases(self) -> TerminalLeaseRegistry:
        return self.lease_registry

    def _runtime_for(self, backend: str) -> TmuxTerminalRuntime | None:
        return self.runtime if backend == "tmux" else None

    async def _send_json(self, _websocket: Any, payload: dict[str, Any]) -> None:
        self.sent.append(payload)


@pytest.mark.asyncio
async def test_external_tmux_row_resizes_and_releases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = replace(
        make_memory_terminal(terminal_id="term-1", session_name="external-demo"),
        ownership="external",
    )
    commands: list[tuple[str, ...]] = []

    async def run_tmux(
        _manager: TmuxSessionManager, *args: str, **_kwargs: object
    ) -> tuple[int, str, str]:
        commands.append(args)
        return (0, "", "")

    monkeypatch.setattr(TmuxSessionManager, "_run", run_tmux)
    runtime = TmuxTerminalRuntime(TmuxSessionManager(TmuxConfig(socket_name="gobby")))
    server = _SizingServer(row, runtime)
    attachment = server.lease_registry.attach("term-1", viewer="gclient")

    await server._handle_terminal_resize(
        object(),
        {"attachment_id": attachment.attachment_id, "rows": 40, "cols": 120},
    )
    assert commands[:2] == [
        ("set-option", "-w", "-t", "%1", "window-size", "manual"),
        ("resize-window", "-t", "%1", "-x", "120", "-y", "40"),
    ]

    await TerminalWsMixin._handle_terminal_detach(
        server,
        object(),
        {"attachment_id": attachment.attachment_id, "terminal_id": "term-1"},
    )
    assert commands[-1] == ("set-option", "-wu", "-t", "%1", "window-size")
    await server.lease_registry.shutdown_lifecycle_publication()


@pytest.mark.asyncio
async def test_proxy_socket_failure_applies_re_elected_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = replace(
        make_memory_terminal(terminal_id="term-1", session_name="external-demo"),
        ownership="external",
    )
    commands: list[tuple[str, ...]] = []

    async def run_tmux(
        _manager: TmuxSessionManager, *args: str, **_kwargs: object
    ) -> tuple[int, str, str]:
        commands.append(args)
        return (0, "", "")

    monkeypatch.setattr(TmuxSessionManager, "_run", run_tmux)
    runtime = TmuxTerminalRuntime(TmuxSessionManager(TmuxConfig(socket_name="gobby")))
    server = _SizingServer(row, runtime)
    web = server.lease_registry.attach("term-1", viewer="web")
    gclient = server.lease_registry.attach("term-1", viewer="gclient")
    websocket = object()

    await server._handle_terminal_resize(
        websocket,
        {"attachment_id": web.attachment_id, "rows": 50, "cols": 150},
    )
    await server._handle_terminal_resize(
        websocket,
        {"attachment_id": gclient.attachment_id, "rows": 40, "cols": 120},
    )

    hub = server._proxy()
    frame = AsyncMock()
    hub.attachments[web.attachment_id] = ProxyAttachment(
        terminal_id="term-1",
        attachment_id=web.attachment_id,
        websocket=websocket,
        frame=frame,
        encoding="json",
    )
    hub.by_socket[websocket] = {web.attachment_id}

    await hub._on_socket_fail(websocket, "proxy_lag")

    assert commands[-2:] == [
        ("set-option", "-w", "-t", "%1", "window-size", "manual"),
        ("resize-window", "-t", "%1", "-x", "120", "-y", "40"),
    ]
    frame.close.assert_awaited_once()
    await server.lease_registry.shutdown_lifecycle_publication()
