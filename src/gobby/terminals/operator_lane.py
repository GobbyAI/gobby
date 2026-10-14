"""Dedicated executor for the blocking calls an operator's terminal request waits on.

Attaching a workspace or terminal and taking control each make millisecond-scale
storage calls before they answer the client. On the shared default executor those
calls queue behind every other ``asyncio.to_thread`` in the daemon, and under fleet
load that queue alone held single-row lookups for seconds and timed out the
client's take-control and attach requests (#23080).
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

OPERATOR_LANE_THREAD_PREFIX = "operator-lane"

_OPERATOR_EXECUTOR = ThreadPoolExecutor(
    max_workers=8,
    thread_name_prefix=OPERATOR_LANE_THREAD_PREFIX,
)


async def run_on_operator_lane(func: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> R:
    """Run ``func`` on the operator executor.

    Drop-in for ``asyncio.to_thread``: contextvars propagate to the worker
    thread and keyword arguments are supported.
    """
    loop = get_running_loop()
    ctx = contextvars.copy_context()
    call = functools.partial(ctx.run, func, *args, **kwargs)
    return await loop.run_in_executor(_OPERATOR_EXECUTOR, call)


__all__ = ["OPERATOR_LANE_THREAD_PREFIX", "run_on_operator_lane"]
