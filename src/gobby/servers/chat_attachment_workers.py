"""Named shielded workers for attachment publication and unlink."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class ShieldedOutcome[T]:
    result: T | None = None
    error: BaseException | None = None


async def run_shielded[T](
    name: str, fn: Callable[..., T], *args: object, **kwargs: object
) -> tuple[ShieldedOutcome[T], bool]:
    """Run ``fn`` in a named worker and await its terminal result even after cancel."""
    outcome: ShieldedOutcome[T] = ShieldedOutcome()

    def wrapper() -> None:
        try:
            outcome.result = fn(*args, **kwargs)
        except BaseException as exc:
            outcome.error = exc

    task = asyncio.create_task(asyncio.to_thread(wrapper), name=name)
    cancelled = False
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        cancelled = True
    return outcome, cancelled
