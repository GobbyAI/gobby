"""Private fixture-only cancellation; no production endpoint is installed."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from typing import cast

from gobby.events.wake import WakeDispatcher
from gobby.storage.terminals import Terminal
from tests.e2e.composer_proof import ProofRefused, Surface, require_private_root


async def cancel_proof_retries(dispatcher: WakeDispatcher, allowed: frozenset[str]) -> None:
    owners = (dispatcher._composer_retries, dispatcher._deferred_refreshes)
    if any(set(owner) - allowed for owner in owners):
        raise ProofRefused("unowned retry in isolated daemon")
    tasks = {task for owner in owners for task in owner.values()}
    for task in tasks:
        task.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    if any(isinstance(result, Exception) for result in results):
        raise ProofRefused("proof retry cancellation failed")


class CleanupControl:
    """One cleanup operation on the isolated daemon's owner loop."""

    def __init__(self, socket: Path) -> None:
        self.socket = socket
        self.quiesced = False
        self.task: asyncio.Task[None] | None = None
        self.started = asyncio.Event()
        self.handlers: set[asyncio.Task[None]] = set()

    def schedule(self, dispatcher: WakeDispatcher) -> None:
        if self.task is not None:
            raise ProofRefused("duplicate fixture owner loop")
        self.task = asyncio.create_task(self.serve(dispatcher))

    async def serve(self, dispatcher: WakeDispatcher) -> None:
        require_private_root(self.socket.parent)

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            task = asyncio.current_task()
            if task is not None:
                self.handlers.add(task)
            accepted = False
            try:
                async with asyncio.timeout(20):
                    value = json.loads(await reader.readline())
                    if not isinstance(value, dict) or set(value) != {"surfaces"}:
                        raise ProofRefused("invalid cleanup request")
                    items = value["surfaces"]
                    if not isinstance(items, list):
                        raise ProofRefused("invalid cleanup surfaces")
                    surfaces = [Surface(**item) for item in items]
                    for surface in surfaces:
                        if (
                            surface.provider not in {"claude", "codex"}
                            or surface.project_id != "00000000-0000-0000-0000-000000000e2e"
                        ):
                            raise ProofRefused("invalid cleanup project/provider")
                        session = await asyncio.to_thread(
                            dispatcher._session_manager.get, surface.session_id
                        )
                        route = await dispatcher._terminal_route_for_session(session)
                        row = route.managed_terminal
                        if not isinstance(row, Terminal) or (
                            row.id,
                            row.session_id,
                            row.project_id,
                            row.host_epoch,
                        ) != (
                            surface.terminal_id,
                            surface.session_id,
                            surface.project_id,
                            surface.host_epoch,
                        ):
                            raise ProofRefused("cleanup binding changed")
                    self.quiesced = True
                    await cancel_proof_retries(
                        dispatcher, frozenset(surface.session_id for surface in surfaces)
                    )
                    # Await any dispatch already inside its per-session critical section.
                    for lock in list(dispatcher._live_wake_locks.values()):
                        async with lock:
                            pass
                    await cancel_proof_retries(
                        dispatcher, frozenset(surface.session_id for surface in surfaces)
                    )
                    accepted = True
            except (ProofRefused, ValueError, TypeError, TimeoutError, OSError):
                accepted = False
            finally:
                try:
                    writer.write(json.dumps({"quiesced": accepted}).encode() + b"\n")
                    await writer.drain()
                finally:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    finally:
                        if task is not None:
                            self.handlers.discard(task)

        server = await asyncio.start_unix_server(handle, path=self.socket)
        self.socket.chmod(0o600)
        self.started.set()
        try:
            async with server:
                await server.serve_forever()
        finally:
            handlers = list(self.handlers)
            for task in handlers:
                task.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)


async def quiesce_proof(socket: Path, surfaces: list[Surface]) -> None:
    require_private_root(socket.parent)
    async with asyncio.timeout(25):
        reader, writer = await asyncio.open_unix_connection(socket)
        try:
            writer.write(
                json.dumps({"surfaces": [asdict(item) for item in surfaces]}).encode() + b"\n"
            )
            await writer.drain()
            result = json.loads(await reader.readline())
            if not isinstance(result, dict) or cast(dict[str, object], result) != {
                "quiesced": True
            }:
                raise ProofRefused("proof retry cleanup unconfirmed")
        finally:
            writer.close()
            await writer.wait_closed()
