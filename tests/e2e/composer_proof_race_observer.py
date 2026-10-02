"""Test-only observation of the existing physical composer coordination boundary."""

from __future__ import annotations

import asyncio
import hashlib
import time
import weakref
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal
from unittest.mock import patch
from uuid import uuid4

from gobby.events.wake import WakeDispatcher
from gobby.hooks import terminal_handoff_delivery
from gobby.sessions.handoff import ClaimedHandoffDelivery
from gobby.storage.terminals import Terminal
from gobby.terminals.write_coordinator import WriteCoordinator
from tests.e2e.composer_proof import ProofRefused, Surface
from tests.e2e.composer_proof_trace import observe_acquire

Writer = Literal["wake", "handoff"]


@dataclass
class HandoffCaller:
    task: asyncio.Task[Any]
    provider: str
    session_id: str
    surface: Surface | None = None


class RaceObservation:
    def __init__(self, exchange: Callable[[dict[str, object]], Awaitable[None]]) -> None:
        self.exchange = exchange
        self.current: ContextVar[tuple[Writer, str | None] | None] = ContextVar(
            "composer_proof_writer", default=None
        )
        self.locks: weakref.WeakKeyDictionary[asyncio.Lock, tuple[WriteCoordinator, str, str]] = (
            weakref.WeakKeyDictionary()
        )
        self.salt = uuid4().hex
        self.coordinators: dict[str, WriteCoordinator] = {}
        self.callers: dict[str, HandoffCaller] = {}
        self.quiesced = False

    async def observe_wake(
        self,
        dispatcher: WakeDispatcher,
        original: Callable[..., Awaitable[dict[str, Any]]],
        session_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Record the actual target separately from the physical row after a possible clear."""
        session = kwargs.get("session") or await asyncio.to_thread(
            dispatcher._session_manager.get, session_id
        )
        route = await dispatcher._terminal_route_for_session(session)
        before = route.managed_terminal
        if not isinstance(before, Terminal):
            raise ProofRefused("proof wake has no exact managed terminal")
        started = time.monotonic()
        result = await original(dispatcher, session_id, **kwargs)
        coordinator = self.coordinators.get(before.id)
        row: Terminal | None
        if coordinator is not None:
            row = await asyncio.to_thread(coordinator._require, before.id)
        else:
            current = await asyncio.to_thread(dispatcher._session_manager.get, session_id)
            current_route = await dispatcher._terminal_route_for_session(current)
            row = current_route.managed_terminal
        if (
            not isinstance(row, Terminal)
            or row.id != before.id
            or row.project_id != before.project_id
            or row.host_epoch != before.host_epoch
        ):
            raise ProofRefused("proof wake physical binding changed")
        await self.exchange(
            {
                "phase": "wake",
                "terminal_id": row.id,
                "session_id": row.session_id,
                "requested_session_id": session_id,
                "project_id": row.project_id,
                "host_epoch": row.host_epoch,
                "delivered": result.get("delivered") is True,
                "skipped": result.get("skipped"),
                "priority": kwargs.get("priority", "normal"),
                "monotonic": started,
            }
        )
        return result

    async def lookup_terminal(self, surface: Surface) -> Terminal:
        coordinator = self.coordinators.get(surface.terminal_id)
        if coordinator is None:
            raise ProofRefused("cleanup has no original physical coordinator")
        row = await asyncio.to_thread(coordinator._require, surface.terminal_id)
        if (row.id, row.session_id, row.project_id, row.host_epoch) != (
            surface.terminal_id,
            surface.session_id,
            surface.project_id,
            surface.host_epoch,
        ):
            raise ProofRefused("cleanup physical binding changed")
        return row

    async def quiesce(self, surfaces: list[Surface]) -> None:
        if any(
            caller.surface is None or caller.surface not in surfaces
            for caller in self.callers.values()
        ):
            raise ProofRefused("unowned handoff caller in isolated daemon")
        self.quiesced = True
        tasks = [caller.task for caller in self.callers.values()]
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        if any(isinstance(result, Exception) for result in results):
            raise ProofRefused("proof handoff cancellation failed")

    def cancel(self, surface: Surface, attempt_id: str) -> bool:
        caller = self.callers.get(attempt_id)
        if caller is None or caller.task.done():
            raise ProofRefused("no active handoff caller")
        if caller.surface != surface:
            raise ProofRefused("handoff cancellation binding changed")
        return caller.task.cancel()

    @contextmanager
    def writer(self, writer: Writer, attempt_id: str | None = None) -> Iterator[None]:
        if writer not in {"wake", "handoff"} or (writer == "handoff") != bool(attempt_id):
            raise ProofRefused("invalid observed writer")
        token = self.current.set((writer, attempt_id))
        try:
            yield
        finally:
            self.current.reset(token)

    @contextmanager
    def install(self) -> Iterator[None]:
        original_lock = WriteCoordinator.logical_action_lock
        original_acquire = asyncio.Lock.acquire
        original_wake = WakeDispatcher._dispatch_live_wake_unlocked
        original_handoff = terminal_handoff_delivery._settle_delivery

        async def wake(dispatcher: WakeDispatcher, *args: Any, **kwargs: Any) -> dict[str, Any]:
            with self.writer("wake"):
                return await original_wake(dispatcher, *args, **kwargs)

        async def handoff(claimed: ClaimedHandoffDelivery, **kwargs: Any) -> None:
            task = asyncio.current_task()
            session = await asyncio.to_thread(kwargs["session_manager"].get, claimed.session_id)
            if task is None or session is None or session.source not in {"claude", "codex"}:
                raise ProofRefused("no real provider handoff caller")
            if self.quiesced:
                raise ProofRefused("proof handoff caller quiesced")
            if claimed.attempt_id in self.callers:
                raise ProofRefused("duplicate observed handoff caller")
            caller = HandoffCaller(task, session.source, claimed.session_id)
            self.callers[claimed.attempt_id] = caller
            cancelled = False
            try:
                with self.writer("handoff", claimed.attempt_id):
                    await original_handoff(claimed, **kwargs)
            except asyncio.CancelledError:
                cancelled = True
                raise
            finally:
                del self.callers[claimed.attempt_id]
                if caller.surface is not None:
                    await self.exchange(
                        {
                            "phase": "caller",
                            "state": "cancelled" if cancelled else "settled",
                            "attempt_id": claimed.attempt_id,
                            "terminal_id": caller.surface.terminal_id,
                            "session_id": caller.surface.session_id,
                            "project_id": caller.surface.project_id,
                            "host_epoch": caller.surface.host_epoch,
                        }
                    )

        def logical_lock(coordinator: WriteCoordinator, terminal_id: str) -> asyncio.Lock:
            if (
                terminal_id in self.coordinators
                and self.coordinators[terminal_id] is not coordinator
            ):
                raise ProofRefused("proof has ambiguous original physical coordinators")
            self.coordinators[terminal_id] = coordinator
            lock = original_lock(coordinator, terminal_id)
            if lock not in self.locks:
                digest = hashlib.sha256(f"{self.salt}:{id(lock)}".encode()).hexdigest()
                self.locks[lock] = (coordinator, terminal_id, digest)
            return lock

        async def acquire(lock: asyncio.Lock) -> bool:
            label = self.current.get()
            bound = self.locks.get(lock)
            if label is None or bound is None:
                return await original_acquire(lock)
            coordinator, terminal_id, digest = bound
            row = await asyncio.to_thread(coordinator._require, terminal_id)
            caller = self.callers.get(label[1] or "")
            if caller is not None and row.session_id == caller.session_id:
                caller.surface = Surface(
                    caller.provider,
                    str(row.session_id),
                    row.id,
                    str(row.host_epoch),
                    str(row.project_id),
                )
            fields: dict[str, object] = {
                "terminal_id": row.id,
                "session_id": row.session_id,
                "project_id": row.project_id,
                "host_epoch": row.host_epoch,
                "writer": label[0],
                "attempt_id": label[1],
                "lock_sha256": digest,
            }
            await self.exchange({**fields, "phase": "admission", "state": "ready"})

            async def exchange_receipt(receipt: dict[str, object]) -> None:
                if receipt.get("state") in {"acquired", "cancelled"}:
                    current = await asyncio.to_thread(coordinator._require, terminal_id)
                    if (
                        current.id != row.id
                        or current.project_id != row.project_id
                        or current.host_epoch != row.host_epoch
                    ):
                        raise ProofRefused("original lock's physical binding changed")
                    receipt = {**receipt, "session_id": current.session_id}
                await self.exchange(receipt)

            return await observe_acquire(
                lock,
                original_acquire,
                exchange_receipt,
                {**fields, "phase": "lock"},
            )

        with (
            patch.object(WriteCoordinator, "logical_action_lock", logical_lock),
            patch.object(asyncio.Lock, "acquire", acquire),
            patch.object(WakeDispatcher, "_dispatch_live_wake_unlocked", wake),
            patch.object(terminal_handoff_delivery, "_settle_delivery", handoff),
        ):
            yield
