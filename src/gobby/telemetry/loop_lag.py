"""Report when the event loop stops scheduling, and what was running.

The daemon had no way to answer this. A stall shows up as every route timing
out at once -- liveness included -- but ``sample(1)`` collapses every Python
frame into ``_PyEval_EvalFrameDefault`` and py-spy needs root on macOS, so an
external profile can prove the loop thread is running Python without naming the
coroutine. This closes that gap from inside: sleep in a tight cycle, and when a
wake-up comes back late, report the gap together with every live task and where
it is suspended (#20841).

One limitation is inherent: a watchdog that lives on the loop cannot run
*during* the block, so it names the tasks alive once the loop comes back. That
covers what this is for -- a request or spawn that burns partway through and
carries on -- and its stack points at the frame it was in. A task that both
blocks and finishes inside the gap is gone by then; what remains is the frame
of whatever awaited it, which still locates the call site.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD_SECONDS = 1.0
DEFAULT_POLL_SECONDS = 0.05
# One report per stall rather than one per poll, and a floor on how often a
# pathological loop can fill the log.
DEFAULT_REARM_SECONDS = 15.0
MAX_REPORTED_TASKS = 12


@dataclass(frozen=True)
class LoopLagReport:
    """One observed stall: how long the loop was away, and what was live."""

    lag_seconds: float
    threshold_seconds: float
    tasks: list[str] = field(default_factory=list)

    def render(self) -> str:
        head = (
            f"Event loop stalled for {self.lag_seconds:.2f}s "
            f"(threshold {self.threshold_seconds:.2f}s)"
        )
        if not self.tasks:
            return head
        return head + " | live tasks: " + "; ".join(self.tasks)


async def measure_loop_lag(poll_seconds: float) -> float:
    """Sleep, then report how much later than requested the loop came back."""
    loop = asyncio.get_running_loop()
    before = loop.time()
    await asyncio.sleep(poll_seconds)
    return max(0.0, loop.time() - before - poll_seconds)


def _describe_task(task: asyncio.Task[object]) -> str:
    """Name a task and the frame it is sitting in, without raising."""
    try:
        name = task.get_name()
    except Exception:  # pragma: no cover - defensive
        name = "<unnamed>"
    location = ""
    try:
        stack = task.get_stack(limit=1)
        if stack:
            frame = stack[-1]
            location = f" at {frame.f_code.co_filename}:{frame.f_lineno}"
    except Exception:  # pragma: no cover - defensive
        location = ""
    coro = ""
    try:
        target = task.get_coro()
        qualname = getattr(target, "__qualname__", None)
        if qualname:
            coro = f" {qualname}"
    except Exception:  # pragma: no cover - defensive
        coro = ""
    return f"{name}{coro}{location}"


def _snapshot_tasks(exclude: asyncio.Task[object] | None) -> list[str]:
    try:
        live = [t for t in asyncio.all_tasks() if t is not exclude and not t.done()]
    except RuntimeError:  # pragma: no cover - loop shutting down
        return []
    return [_describe_task(task) for task in live[:MAX_REPORTED_TASKS]]


async def loop_lag_watchdog(
    is_shutdown_requested: Callable[[], bool],
    *,
    threshold_seconds: float = DEFAULT_THRESHOLD_SECONDS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    rearm_seconds: float = DEFAULT_REARM_SECONDS,
    on_lag: Callable[[LoopLagReport], None] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> None:
    """Watch the loop's scheduling delay and report every stall past the threshold."""
    loop = asyncio.get_running_loop()
    self_task = asyncio.current_task()
    last_report_at = float("-inf")

    while not is_shutdown_requested():
        try:
            before = loop.time()
            if sleep is not None:
                await sleep(poll_seconds)
            else:
                await asyncio.sleep(poll_seconds)
            lag = max(0.0, loop.time() - before - poll_seconds)

            if lag < threshold_seconds or loop.time() - last_report_at < rearm_seconds:
                continue
            last_report_at = loop.time()

            report = LoopLagReport(
                lag_seconds=lag,
                threshold_seconds=threshold_seconds,
                tasks=_snapshot_tasks(self_task),
            )
            if on_lag is not None:
                on_lag(report)
            else:
                logger.warning("%s", report.render())
        except asyncio.CancelledError:
            break
        except Exception:  # pragma: no cover - a diagnostic must never take the daemon down
            logger.debug("Loop lag watchdog iteration failed", exc_info=True)


__all__ = [
    "DEFAULT_POLL_SECONDS",
    "DEFAULT_REARM_SECONDS",
    "DEFAULT_THRESHOLD_SECONDS",
    "LoopLagReport",
    "loop_lag_watchdog",
    "measure_loop_lag",
]
