"""Dedicated executor for rule-engine evaluation offloads.

Rule evaluation runs inside a strict wall-clock budget
(``WorkflowEvaluationTimeout``), so its millisecond-scale storage reads must
not queue behind unrelated daemon work on the shared default executor — under
multi-agent burst that queue delay alone can exhaust the entire budget.
"""

from __future__ import annotations

import contextvars
import functools
import time
from asyncio import get_running_loop
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import ParamSpec, TypeVar

from gobby.hooks.phase_timing import add_hook_phase

P = ParamSpec("P")
R = TypeVar("R")

ENGINE_EXECUTOR_THREAD_PREFIX = "rule-engine"
RULE_LOOP_THREAD_PREFIX = "rule-loop"

_ENGINE_EXECUTOR = ThreadPoolExecutor(
    max_workers=16,
    thread_name_prefix=ENGINE_EXECUTOR_THREAD_PREFIX,
)
_RULE_LOOP_EXECUTOR = ThreadPoolExecutor(max_workers=16, thread_name_prefix=RULE_LOOP_THREAD_PREFIX)
_INLINE_OFFLOAD: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "rule_engine_inline_offload", default=False
)


@contextmanager
def inline_offload_scope(enabled: bool = True) -> Iterator[None]:
    """Keep synchronous rule work on its rule-loop worker."""
    token = _INLINE_OFFLOAD.set(enabled)
    try:
        yield
    finally:
        _INLINE_OFFLOAD.reset(token)


async def _run_timed[T](
    executor: ThreadPoolExecutor,
    keys: tuple[str, str, str],
    func: Callable[[], T],
) -> T:
    """Run ``func`` on ``executor``, recording its queue, work and resume phases.

    Resume is the gap from ``func`` finishing until the awaiting coroutine
    runs again: lag on the loop that awaited it.
    """
    queue_key, work_key, resume_key = keys
    loop = get_running_loop()
    ctx = contextvars.copy_context()
    queued_at = time.perf_counter()
    finished_at: float | None = None

    def run() -> T:
        nonlocal finished_at
        started_at = time.perf_counter()
        add_hook_phase(queue_key, started_at - queued_at)
        try:
            return func()
        finally:
            finished_at = time.perf_counter()
            add_hook_phase(work_key, finished_at - started_at)

    try:
        return await loop.run_in_executor(executor, functools.partial(ctx.run, run))
    finally:
        if finished_at is not None:
            add_hook_phase(resume_key, time.perf_counter() - finished_at)


async def offload_rule_loop(func: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> R:
    """Run a complete rule pass on an executor separate from effect offloads."""
    return await _run_timed(
        _RULE_LOOP_EXECUTOR,
        ("rule_loop_executor_queue", "rule_loop_pass_work", "rule_loop_pass_resume"),
        functools.partial(func, *args, **kwargs),
    )


async def timed_offload(
    phase: str, func: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs
) -> R:
    """:func:`offload`, recording ``<phase>_queue``, ``_work`` and ``_resume``."""
    if _INLINE_OFFLOAD.get():
        return func(*args, **kwargs)
    return await _run_timed(
        _ENGINE_EXECUTOR,
        (f"{phase}_queue", f"{phase}_work", f"{phase}_resume"),
        functools.partial(func, *args, **kwargs),
    )


async def offload(func: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> R:
    """Run ``func`` on the dedicated rule-engine executor.

    Drop-in for ``asyncio.to_thread``: contextvars propagate to the worker
    thread and keyword arguments are supported.
    """
    if _INLINE_OFFLOAD.get():
        return func(*args, **kwargs)
    loop = get_running_loop()
    ctx = contextvars.copy_context()
    call = functools.partial(ctx.run, func, *args, **kwargs)
    return await loop.run_in_executor(_ENGINE_EXECUTOR, call)
