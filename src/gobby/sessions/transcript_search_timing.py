"""Content-free phase diagnostics for transcript searches."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter, thread_time
from uuid import uuid4

logger = logging.getLogger(__name__)
_search_id: ContextVar[str | None] = ContextVar("transcript_search_id", default=None)


@contextmanager
def transcript_search_timing(phase: str = "search") -> Iterator[None]:
    """Correlate worker timings without retaining session IDs, paths or content."""
    if phase != "search" and _search_id.get() is None:
        yield
        return
    search_id = _search_id.get() or uuid4().hex
    token = _search_id.set(search_id)
    started = perf_counter()
    logger.debug("transcript_search trace=%s phase=%s event=started", search_id, phase)
    try:
        yield
    finally:
        logger.debug(
            "transcript_search trace=%s phase=%s event=finished elapsed_s=%.6f",
            search_id,
            phase,
            perf_counter() - started,
        )
        _search_id.reset(token)


async def transcript_to_thread[**P, T](
    func: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs
) -> T:
    """Keep default-executor behavior while separating queue, work and resume time."""
    search_id = _search_id.get()
    if search_id is None:
        return await asyncio.to_thread(func, *args, **kwargs)
    phase = getattr(func, "__name__", type(func).__name__)
    submitted = perf_counter()
    finished: float | None = None
    logger.debug("transcript_search trace=%s phase=%s event=queued", search_id, phase)

    def work() -> T:
        nonlocal finished
        started = perf_counter()
        cpu_started = thread_time()
        try:
            return func(*args, **kwargs)
        finally:
            work_finished = perf_counter()
            logger.debug(
                "transcript_search trace=%s phase=%s event=worker_finished "
                "queue_s=%.6f work_s=%.6f cpu_s=%.6f",
                search_id,
                phase,
                started - submitted,
                work_finished - started,
                thread_time() - cpu_started,
            )
            finished = perf_counter()

    try:
        return await asyncio.to_thread(work)
    finally:
        if finished is not None:
            logger.debug(
                "transcript_search trace=%s phase=%s event=resumed resume_s=%.6f",
                search_id,
                phase,
                perf_counter() - finished,
            )
