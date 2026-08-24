"""Dedicated threads for long-lived blocking stream reads.

A pipe or PTY pump blocks its worker for as long as the stream stays quiet --
for the whole life of the subprocess, in the case of a stderr drain that never
sees output. The event loop's default executor is only
``min(32, cpu_count + 4)`` threads wide and is shared by every
``asyncio.to_thread`` call in the daemon, so a pump parked there permanently
retires one of those slots. Enough live pumps and ordinary short offloads queue
behind them: that is what pushed ``GET /api/health`` past the hook client's
five-second timeout (#20839).

Each pump therefore takes a thread of its own. The thread count is unchanged --
a pump occupied one before and occupies one now -- but nothing else is waiting
on it, and the pool grows with the number of live streams instead of capping
them against a fixed width.

Shut the executor down when the pump stops, and close the stream as well: a
worker parked in ``read`` only returns once its file descriptor reaches EOF, so
``shutdown`` alone leaves the thread alive.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor


def open_stream_pump_executor(label: str) -> ThreadPoolExecutor:
    """Return a single-thread executor dedicated to one long-lived stream pump."""
    return ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"gobby-pump-{label}")


__all__ = ["open_stream_pump_executor"]
