"""Database execution helpers for websocket handlers."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any


async def run_db(owner: Any, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run SQLite work through the websocket server's bounded executor when available."""
    runner = getattr(owner, "run_db", None)
    if inspect.iscoroutinefunction(runner):
        return await runner(func, *args, **kwargs)
    return await asyncio.to_thread(func, *args, **kwargs)
