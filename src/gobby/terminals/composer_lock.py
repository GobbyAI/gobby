"""Shared per-terminal arbitration for the one physical composer.

Wake delivery, handoff compaction, ``/clear``, and the post-compaction pull
prompt all write into the same physical terminal composer. The probe that
authorizes a write and the write itself are separate awaits, so an earlier
empty snapshot cannot by itself authorize a later overlapping write: a wake
that probed an empty composer just before a handoff staged ``/compact`` would
otherwise type its drain over that staged command.

``WriteCoordinator.logical_action_lock`` is the daemon's existing per-terminal
action lock -- the wake sender already serializes its drain/text/Enter sequence
under it. Every other composer writer takes the *same* lock object here, so one
writer's probe-stage-submit sequence cannot interleave with another's. When the
coordinator is unbound (CLI worktrees, unit fixtures) the lock degrades to a
no-op rather than failing the write.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

_coordinator: Any | None = None

# Reentrancy bookkeeping so a caller can hold the lock across a probe and hand
# the same terminal to a nested writer that also acquires it, without the
# non-reentrant ``asyncio.Lock`` deadlocking on itself. Keyed by coordinator
# identity so independent fixtures never share nesting state.
_nesting: dict[tuple[int, str], tuple[Any, int]] = {}


def bind_composer_coordinator(coordinator: Any | None) -> None:
    """Bind the daemon's shared write coordinator for composer arbitration."""
    global _coordinator
    _coordinator = coordinator


@asynccontextmanager
async def composer_action_lock(
    terminal_id: str, coordinator: Any | None = None
) -> AsyncIterator[None]:
    """Hold the shared per-terminal composer lock across one writer sequence.

    Reentrant for the task already holding the terminal's lock, so the wake
    dispatcher can serialize a probe with the sender it calls, and a handoff
    writer can serialize its own stage/submit/verify ladder end to end. Pass
    ``coordinator`` to reuse a caller's injected coordinator; otherwise the
    process-bound one is used.
    """
    resolved = coordinator if coordinator is not None else _coordinator
    if resolved is None or not terminal_id:
        yield
        return
    task = asyncio.current_task()
    key = (id(resolved), terminal_id)
    held = _nesting.get(key)
    if held is not None and held[0] is task:
        _nesting[key] = (task, held[1] + 1)
        try:
            yield
        finally:
            current = _nesting.get(key)
            if current is not None and current[0] is task:
                if current[1] <= 1:
                    _nesting.pop(key, None)
                else:
                    _nesting[key] = (task, current[1] - 1)
        return
    async with resolved.logical_action_lock(terminal_id):
        _nesting[key] = (task, 1)
        try:
            yield
        finally:
            _nesting.pop(key, None)
