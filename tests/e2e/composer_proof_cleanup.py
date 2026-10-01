"""Private fixture-only cancellation; no production endpoint is installed."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
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
    """Private cancellation and cleanup on the isolated daemon's owner loop."""

    def __init__(
        self,
        socket: Path,
        *,
        cancel_handoff: Callable[[Surface, str], bool] | None = None,
        drain_handoffs: Callable[[list[Surface]], Awaitable[None]] | None = None,
        terminal_lookup: Callable[[Surface], Awaitable[Terminal | None]] | None = None,
    ) -> None:
        self.socket = socket
        self.cancel_handoff = cancel_handoff
        self.drain_handoffs = drain_handoffs
        self.terminal_lookup = terminal_lookup
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
            reply = {"quiesced": False}
            try:
                async with asyncio.timeout(20):
                    value = json.loads(await reader.readline())
                    if isinstance(value, dict) and set(value) == {"retired_surface"}:
                        reply = {"retired": False}
                        surface = Surface(**value["retired_surface"])
                        if (
                            surface.provider not in {"claude", "codex"}
                            or surface.project_id != "00000000-0000-0000-0000-000000000e2e"
                            or self.terminal_lookup is None
                        ):
                            raise ProofRefused("invalid retired proof surface")
                        row = await self.terminal_lookup(surface)
                        if (
                            not isinstance(row, Terminal)
                            or row.state != "exited"
                            or (row.id, row.session_id, row.project_id, row.host_epoch)
                            != (
                                surface.terminal_id,
                                surface.session_id,
                                surface.project_id,
                                surface.host_epoch,
                            )
                        ):
                            raise ProofRefused("retired cleanup binding changed")
                        await cancel_proof_retries(dispatcher, frozenset({surface.session_id}))
                        lock = dispatcher._live_wake_locks.get(surface.session_id)
                        if lock is not None:
                            async with lock:
                                pass
                        await cancel_proof_retries(dispatcher, frozenset({surface.session_id}))
                        reply["retired"] = True
                        return
                    if isinstance(value, dict) and set(value) == {"surface", "attempt_id"}:
                        reply = {"cancel_requested": False}
                        if (
                            self.cancel_handoff is None
                            or not isinstance(value["surface"], dict)
                            or not isinstance(value["attempt_id"], str)
                            or not value["attempt_id"].strip()
                        ):
                            raise ProofRefused("invalid caller cancellation request")
                        surface = Surface(**value["surface"])
                        if (
                            surface.provider not in {"claude", "codex"}
                            or surface.project_id != "00000000-0000-0000-0000-000000000e2e"
                        ):
                            raise ProofRefused("invalid cancellation project/provider")
                        reply["cancel_requested"] = self.cancel_handoff(
                            surface, value["attempt_id"]
                        )
                        return
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
                        if row is None and self.terminal_lookup is not None:
                            row = await self.terminal_lookup(surface)
                            if row is None or row.state != "exited":
                                raise ProofRefused("cleanup has no confirmed exited terminal")
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
                    if self.drain_handoffs is not None:
                        await self.drain_handoffs(surfaces)
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
                    if "quiesced" in reply:
                        reply["quiesced"] = accepted
                    writer.write(json.dumps(reply).encode() + b"\n")
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


async def cancel_caller(socket: Path, surface: Surface, attempt_id: str) -> None:
    require_private_root(socket.parent)
    async with asyncio.timeout(10):
        reader, writer = await asyncio.open_unix_connection(socket)
        try:
            writer.write(
                json.dumps({"surface": asdict(surface), "attempt_id": attempt_id}).encode() + b"\n"
            )
            await writer.drain()
            result = json.loads(await reader.readline())
            if result != {"cancel_requested": True}:
                raise ProofRefused("actual handoff caller cancellation unconfirmed")
        finally:
            writer.close()
            await writer.wait_closed()


async def retire_terminal(socket: Path, surface: Surface) -> None:
    """Drain retries for one proven exited owned terminal, leaving the next case enabled."""
    require_private_root(socket.parent)
    async with asyncio.timeout(25):
        reader, writer = await asyncio.open_unix_connection(socket)
        try:
            writer.write(json.dumps({"retired_surface": asdict(surface)}).encode() + b"\n")
            await writer.drain()
            result = json.loads(await reader.readline())
            if result != {"retired": True}:
                raise ProofRefused("retired proof retry cleanup unconfirmed")
        finally:
            writer.close()
            await writer.wait_closed()
