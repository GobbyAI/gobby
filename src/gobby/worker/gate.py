"""Runner side of the in-frame phase gate (R0 design 5.7).

Stdlib only, because the live runner imports this module from GobbyRunner.create. With no
gate active in the current context, every phase_scope returns at once and changes nothing,
so production startup runs exactly the statements it ran before the gate existed.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from types import TracebackType

from gobby.worker.phase_ids import InitPhase

_ACTIVE: ContextVar[PhaseGate | None] = ContextVar("gobby_worker_phase_gate", default=None)


class PhaseGate:
    """State shared by one PhaseDriver and the single create task it runs."""

    def __init__(self) -> None:
        loop = asyncio.get_running_loop()
        self.owner: object | None = None
        self.parked: InitPhase | None = None
        self.completed: list[InitPhase] = []
        self.failure: tuple[InitPhase, BaseException] | None = None
        self.unusable = False
        self.changed = asyncio.Event()
        self.permits: dict[InitPhase, asyncio.Future[None]] = {
            phase: loop.create_future() for phase in InitPhase
        }


class _PhaseScope:
    """Async context manager around one phase body. Built by phase_scope."""

    def __init__(self, phase: InitPhase, owner: object) -> None:
        self._phase = phase
        self._owner = owner
        self._gate: PhaseGate | None = None

    async def __aenter__(self) -> None:
        gate = _ACTIVE.get()
        self._gate = gate
        if gate is None:
            return
        if gate.owner is None:
            gate.owner = self._owner
        gate.parked = self._phase
        gate.changed.set()
        # A cancellation at this await raises here, so the body never runs.
        await gate.permits[self._phase]
        gate.parked = None
        gate.changed.set()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        gate = self._gate
        if gate is None:
            return False
        if exc is None:
            gate.completed.append(self._phase)
        else:
            # Set before the exception reaches create's rollback (ruling R5).
            gate.failure = (self._phase, exc)
            gate.unusable = True
        gate.changed.set()
        return False


def phase_scope(phase: InitPhase, owner: object) -> _PhaseScope:
    """Wrap one phase body of GobbyRunner.create. A no-op unless a gate is active."""
    return _PhaseScope(phase, owner)
