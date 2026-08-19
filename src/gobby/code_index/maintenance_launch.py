"""Narrow per-project managed launch port for daemon gcode children."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractContextManager, asynccontextmanager, contextmanager
from types import TracebackType
from typing import Protocol

from gobby.runtime_grants.launch import ManagedLaunch


class MaintenanceLaunchFactory(Protocol):
    def open(
        self, project_id: str, *, timeout_seconds: float
    ) -> AbstractContextManager[ManagedLaunch]: ...


@asynccontextmanager
async def open_launch_async(
    factory: MaintenanceLaunchFactory,
    project_id: str,
    *,
    timeout_seconds: float,
) -> AsyncIterator[ManagedLaunch]:
    """Enter a maintenance launch without blocking the event loop."""
    open_async = getattr(factory, "open_async", None)
    if open_async is not None:
        async with open_async(project_id, timeout_seconds=timeout_seconds) as launch:
            yield launch
        return
    cm = factory.open(project_id, timeout_seconds=timeout_seconds)
    launch = await asyncio.to_thread(cm.__enter__)

    async def _exit(
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> object:
        task = asyncio.ensure_future(
            asyncio.shield(asyncio.to_thread(cm.__exit__, exc_type, exc, tb))
        )
        try:
            return await task
        except asyncio.CancelledError:
            await task
            raise

    try:
        yield launch
    except BaseException as exc:
        if await _exit(type(exc), exc, exc.__traceback__):
            return
        raise
    else:
        await _exit(None, None, None)


@contextmanager
def unavailable_launch(project_id: str, *, timeout_seconds: float) -> Iterator[ManagedLaunch]:
    del project_id, timeout_seconds
    raise RuntimeError("maintenance launch factory is not configured")
    yield  # pragma: no cover
