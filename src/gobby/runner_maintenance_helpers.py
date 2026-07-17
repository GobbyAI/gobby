"""Shared helpers for daemon maintenance loops."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any


def _positive_int_or_default(value: Any, default: int) -> int:
    if not isinstance(value, int):
        return default
    return max(1, value)


async def _run_db(
    runner: Callable[..., Awaitable[Any]] | None,
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    if runner is None:
        return await asyncio.to_thread(func, *args, **kwargs)
    return await runner(func, *args, **kwargs)
