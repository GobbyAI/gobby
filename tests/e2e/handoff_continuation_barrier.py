"""Isolated-daemon compact submission barrier for the handoff proof only."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any
from unittest.mock import patch

from gobby.mcp_proxy.tools.sessions import _terminal_compaction as compaction
from gobby.terminals.pane_io import ComposerReader, PaneIO
from tests.e2e.composer_proof import ProofRefused, require_private_root

_TIMEOUT = 240.0


class HandoffBarrier:
    def __init__(self, socket: Path) -> None:
        self.socket = socket
        self._reached: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._released = asyncio.Event()
        self._handlers: set[asyncio.Task[None]] = set()

    async def __aenter__(self) -> HandoffBarrier:
        require_private_root(self.socket.parent)
        self._server = await asyncio.start_unix_server(self._handle, path=str(self.socket))
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._server.close()
        await self._server.wait_closed()
        self.release()
        async with asyncio.timeout(_TIMEOUT):
            await asyncio.gather(*self._handlers)
        self.socket.unlink(missing_ok=True)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._handlers.add(task)
        try:
            async with asyncio.timeout(_TIMEOUT):
                request = json.loads(await reader.readline())
                if request.get("command") != "/compact" or not request.get("session_id"):
                    raise ProofRefused("unexpected compact barrier request")
                if self._reached.done():
                    raise ProofRefused("duplicate compact barrier request")
                self._reached.set_result(str(request["session_id"]))
                await self._released.wait()
                writer.write(b"release\n")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            self._handlers.remove(task)

    async def wait_for(self, session_id: str) -> None:
        async with asyncio.timeout(_TIMEOUT):
            actual = await asyncio.shield(self._reached)
        if actual != session_id:
            raise ProofRefused("compact barrier reached by a different seat")

    def release(self) -> None:
        self._released.set()


@contextmanager
def install_barrier(socket: Path) -> Iterator[None]:
    original = compaction._submit_command

    async def held(
        pane: PaneIO,
        command: str,
        session_id: str,
        *,
        cli_source: str | None,
        composer_read: ComposerReader | None,
        verify_seconds: float,
    ) -> tuple[bool, str | None, dict[str, Any] | None]:
        if command == "/compact":
            # The original submit and boundary timers start only after this RPC releases.
            async with asyncio.timeout(_TIMEOUT):
                reader, writer = await asyncio.open_unix_connection(str(socket))
                try:
                    writer.write(
                        json.dumps({"command": command, "session_id": session_id}).encode() + b"\n"
                    )
                    await writer.drain()
                    if await reader.readline() != b"release\n":
                        raise ProofRefused("compact barrier closed without release")
                finally:
                    writer.close()
                    await writer.wait_closed()
        return await original(
            pane,
            command,
            session_id,
            cli_source=cli_source,
            composer_read=composer_read,
            verify_seconds=verify_seconds,
        )

    with patch.object(compaction, "_submit_command", held):
        yield
