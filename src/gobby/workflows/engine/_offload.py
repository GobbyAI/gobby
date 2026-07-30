"""Dedicated executor for rule-engine evaluation offloads.

Rule evaluation runs inside a strict wall-clock budget
(``WorkflowEvaluationTimeout``), so its millisecond-scale storage reads must
not queue behind unrelated daemon work on the shared default executor — under
multi-agent burst that queue delay alone can exhaust the entire budget.
"""

from __future__ import annotations

import contextvars
import functools
from asyncio import get_running_loop
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")

ENGINE_EXECUTOR_THREAD_PREFIX = "rule-engine"

_ENGINE_EXECUTOR = ThreadPoolExecutor(
    max_workers=16,
    thread_name_prefix=ENGINE_EXECUTOR_THREAD_PREFIX,
)


async def offload(func: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> R:
    """Run ``func`` on the dedicated rule-engine executor.

    Drop-in for ``asyncio.to_thread``: contextvars propagate to the worker
    thread and keyword arguments are supported.
    """
    loop = get_running_loop()
    ctx = contextvars.copy_context()
    call = functools.partial(ctx.run, func, *args, **kwargs)
    return await loop.run_in_executor(_ENGINE_EXECUTOR, call)
