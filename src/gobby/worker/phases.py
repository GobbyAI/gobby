"""Facade side of the phase gate: PhaseDriver runs GobbyRunner.create one phase at a time.

create runs as one task whose context carries the gate (R0 design 5.7). Each initialize call
releases one phase's permit, then waits until create parks at the next phase scope, finishes,
or fails. Importing this module loads no runner code: gobby.runner and gobby.runner_init load
only inside the functions that need them.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from gobby.worker.gate import _ACTIVE, PhaseGate
from gobby.worker.phase_ids import PHASE_ORDER, InitPhase, PhaseStatus

_MESSAGE_LIMIT: Final = 2048
# A create that returns before every phase scope ran (R55). No exception exists for it, so its
# error type is this fixed name.
_EARLY_RETURN_MESSAGE: Final = "create returned before every phase ran"
_EARLY_RETURN_TYPE: Final = "IncompleteCreate"


@dataclass(frozen=True, slots=True)
class PhaseOutcome:
    """The answer to an initialize call that ran a phase (design 5.7)."""

    status: PhaseStatus
    next_phase: InitPhase | None
    message: str | None
    error_type: str | None


@dataclass(frozen=True, slots=True)
class _Sentinel:
    label: str

    def __repr__(self) -> str:
        return self.label


UNUSABLE: Final = _Sentinel("UNUSABLE")
OUT_OF_ORDER: Final = _Sentinel("OUT_OF_ORDER")


def runner_create_factory(
    config_path: Path | None, verbose: bool
) -> Callable[[], Coroutine[Any, Any, object]]:
    """Return the zero-argument factory that runs the real GobbyRunner.create (GF-20).

    The runner import happens here, so importing the worker package never loads the runner.
    """
    from gobby.runner import GobbyRunner

    return functools.partial(GobbyRunner.create, config_path, verbose)


def _is_start_refusal(exc: BaseException) -> bool:
    from gobby.runner_init import StartRefusal

    return isinstance(exc, StartRefusal)


class PhaseDriver:
    """Runs one GobbyRunner.create task and advances it one init phase per initialize call."""

    def __init__(self, create: Callable[[], Coroutine[Any, Any, object]]) -> None:
        self._create = create
        self._gate = PhaseGate()
        self._next = 0
        self._task: asyncio.Task[object] | None = None
        self._lock = asyncio.Lock()

    @property
    def unusable(self) -> bool:
        return self._gate.unusable

    @property
    def ready(self) -> bool:
        return self._next == len(PHASE_ORDER) and not self._gate.unusable

    @property
    def owner(self) -> object | None:
        return self._gate.owner

    async def initialize(self, phase: InitPhase) -> PhaseOutcome | _Sentinel:
        """Run one phase. Returns a PhaseOutcome, or UNUSABLE, or OUT_OF_ORDER."""
        async with self._lock:
            if self._gate.unusable:
                return UNUSABLE
            if self._next == len(PHASE_ORDER) or PHASE_ORDER[self._next] != phase:
                return OUT_OF_ORDER
            if self._task is None:
                self._task = self._start_create()
            task = self._task
            permit = self._gate.permits[phase]
            if not permit.done():
                permit.set_result(None)
            following = PHASE_ORDER[self._next + 1] if self._next + 1 < len(PHASE_ORDER) else None
            try:
                return await self._settle(phase, following, task)
            except asyncio.CancelledError:
                # The waiter was cancelled: mark unusable, stop create, and let its rollback
                # finish before the cancellation propagates with no response.
                self._gate.unusable = True
                task.cancel()
                await asyncio.wait({task})
                raise

    def _start_create(self) -> asyncio.Task[object]:
        context = contextvars.copy_context()
        context.run(_ACTIVE.set, self._gate)
        task = asyncio.get_running_loop().create_task(self._create(), context=context)
        # A task that ends without a scope transition still wakes a waiting initialize.
        task.add_done_callback(lambda _done: self._gate.changed.set())
        return task

    async def _settle(
        self, phase: InitPhase, following: InitPhase | None, task: asyncio.Task[object]
    ) -> PhaseOutcome:
        gate = self._gate
        while True:
            failure = gate.failure
            if failure is not None:
                return await self._settle_failure(task, failure[1])
            if following is not None and gate.parked is following:
                self._next += 1
                return PhaseOutcome(PhaseStatus.READY, following, None, None)
            if task.done():
                if task.cancelled():
                    gate.unusable = True
                    raise asyncio.CancelledError
                error = task.exception()
                if error is None:
                    if phase is PHASE_ORDER[-1] and gate.completed == list(PHASE_ORDER):
                        self._next += 1
                        return PhaseOutcome(PhaseStatus.READY, None, None, None)
                    # create returned before every phase scope ran (R55): this phase fails, and
                    # the facade is unusable from here on.
                    gate.unusable = True
                    return PhaseOutcome(
                        PhaseStatus.FAILED, None, _EARLY_RETURN_MESSAGE, _EARLY_RETURN_TYPE
                    )
                gate.unusable = True
                if not isinstance(error, Exception):
                    raise error
                gate.failure = (phase, error)
                continue
            gate.changed.clear()
            await gate.changed.wait()

    async def _settle_failure(
        self, task: asyncio.Task[object], original: BaseException
    ) -> PhaseOutcome:
        """Report a failed phase once create's rollback has finished (design 5.7)."""
        await asyncio.wait({task})
        if task.cancelled():
            raise asyncio.CancelledError
        error = task.exception()
        if error is not None and not isinstance(error, Exception):
            raise error
        if error is not None and error is not original:
            return PhaseOutcome(
                PhaseStatus.FAILED,
                None,
                f"rollback failed: {error}"[:_MESSAGE_LIMIT],
                type(error).__name__,
            )
        # The rollback finished and create re-raised the phase's own error.
        message = str(original)[:_MESSAGE_LIMIT]
        if _is_start_refusal(original):
            return PhaseOutcome(PhaseStatus.REFUSED, None, message, None)
        return PhaseOutcome(PhaseStatus.FAILED, None, message, type(original).__name__)
