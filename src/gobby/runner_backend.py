"""Backend binding and the native supervisor's inherited liveness pipe."""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

from gobby.config.bootstrap import backend_ports
from gobby.shutdown_intent import ShutdownIntent, write_shutdown_intent

if TYPE_CHECKING:
    from gobby.config.bootstrap import BootstrapConfig


class _WindowsCrt(Protocol):
    def open_osfhandle(self, handle: int, flags: int) -> int: ...


@dataclass(frozen=True)
class BackendBind:
    host: str
    http_port: int
    ws_port: int


def backend_bind(bootstrap: BootstrapConfig) -> BackendBind:
    if os.name == "nt" and "GOBBY_SUPERVISOR_FD" not in os.environ:
        return BackendBind(bootstrap.bind_host, bootstrap.daemon_port, bootstrap.websocket_port)
    if bootstrap.front_door.enabled or "GOBBY_SUPERVISOR_FD" in os.environ:
        http, ws = backend_ports(bootstrap.daemon_port, bootstrap.websocket_port)
        return BackendBind("127.0.0.1", http, ws)
    return BackendBind(bootstrap.bind_host, bootstrap.daemon_port, bootstrap.websocket_port)


def require_latched_terminal_route(bootstrap: BootstrapConfig) -> None:
    """Refuse a bootstrap whose terminal route differs from the one gdaemon latched."""
    latched = os.environ.get("GOBBY_TERMINAL_ROUTE")
    configured = bootstrap.front_door.routes.get("terminal_ws", "proxy")
    if latched is not None and latched != configured:
        # runner_init imports this module for backend_bind.
        from gobby.runner_init import StartRefusal

        raise StartRefusal(
            f"front_door.routes.terminal_ws is {configured} but gdaemon latched {latched} "
            "at startup; restart gdaemon to change terminal ownership"
        )


class _TokenLine:
    """The first line gdaemon writes on the supervisor pipe: this start's write token."""

    _MAX_BYTES = 256
    value: str | None
    _pending: bytearray
    _done: bool

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.value = None
        self._pending = bytearray()
        self._done = False

    def feed(self, data: bytes) -> None:
        if self._done:
            return
        head, newline, _ = data.partition(b"\n")
        self._pending += head
        if len(self._pending) > self._MAX_BYTES:
            self._done = True
            self._pending.clear()
        elif newline:
            self._done = True
            if self._pending.isascii():
                self.value = self._pending.decode("ascii") or None
            self._pending.clear()


_token = _TokenLine()


def supervisor_write_token() -> str | None:
    """The write token gdaemon sent this start on the supervisor pipe, held only here."""
    return _token.value


async def watch_supervisor(on_eof: Callable[[], None]) -> None:
    _token.reset()
    value = os.environ.get("GOBBY_SUPERVISOR_FD")
    if value is None:
        return
    fd = int(value)
    if os.name == "nt":
        import msvcrt

        fd = cast(_WindowsCrt, msvcrt).open_osfhandle(fd, os.O_RDONLY)
    loop = asyncio.get_running_loop()
    gone: asyncio.Future[None] = loop.create_future()

    def finish() -> None:
        if not gone.done():
            gone.set_result(None)

    def read_ready() -> None:
        try:
            if data := os.read(fd, 4096):
                _token.feed(data)
                return
        except OSError:
            pass
        finish()

    if os.name == "nt":

        def read_pipe() -> None:
            try:
                while data := os.read(fd, 4096):
                    if not loop.is_closed():
                        loop.call_soon_threadsafe(_token.feed, data)
            except OSError:
                pass
            if not loop.is_closed():
                loop.call_soon_threadsafe(finish)

        threading.Thread(target=read_pipe, daemon=True, name="supervisor-liveness").start()
    else:
        loop.add_reader(fd, read_ready)
    try:
        await gone
        write_shutdown_intent("supervisor_liveness_eof", ShutdownIntent.STOP)
        on_eof()
    finally:
        _token.reset()
        if os.name != "nt":
            loop.remove_reader(fd)
        os.close(fd)
