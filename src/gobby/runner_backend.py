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
    if bootstrap.front_door.enabled or "GOBBY_SUPERVISOR_FD" in os.environ:
        http, ws = backend_ports(bootstrap.daemon_port, bootstrap.websocket_port)
        return BackendBind("127.0.0.1", http, ws)
    return BackendBind(bootstrap.bind_host, bootstrap.daemon_port, bootstrap.websocket_port)


async def watch_supervisor(on_eof: Callable[[], None]) -> None:
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
            if os.read(fd, 4096):
                return
        except OSError:
            pass
        finish()

    if os.name == "nt":

        def read_pipe() -> None:
            try:
                while os.read(fd, 4096):
                    pass
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
        if os.name != "nt":
            loop.remove_reader(fd)
        os.close(fd)
