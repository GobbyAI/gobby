"""Report when the event loop stops scheduling, and what was running.

The daemon had no way to answer this. A stall shows up as every route timing
out at once -- liveness included -- but ``sample(1)`` collapses every Python
frame into ``_PyEval_EvalFrameDefault`` and py-spy needs root on macOS, so an
external profile can prove the loop thread is running Python without naming the
coroutine. This closes that gap from inside: sleep in a tight cycle, and when a
wake-up comes back late, report the gap together with every live task and where
it is suspended (#20841).

Listing every live task names nothing -- the daemon runs dozens, and a dump of
them is a haystack. The signal is that while the loop is blocked nothing else
gets a step, so a task whose await position *moved* across the gap is a task
that was executing during it. Each poll records where every task is suspended,
and a stall report separates the tasks that advanced from the bystanders that
merely sat there.

One limitation is inherent: a watchdog that lives on the loop cannot run
*during* the block, so it observes the gap only once the loop comes back. A
task that both blocks and finishes inside the gap shows up as ended rather than
advanced, which still locates it. A stall with nothing advancing points away
from tasks altogether -- to a bare ``call_soon`` callback, a signal handler, or
a C extension holding the thread.

A stall's full report is multi-kilobyte, which made daemon.log unreadable when
it landed there. It now goes to ``loop_lag.jsonl`` beside the other daemon
logs, one JSON object per stall, and the main log keeps a single WARNING
naming the gap and pointing at the sidecar (#20886). The sidecar write runs on
a worker thread: the watchdog must not become a stall the loop it polices
would have to report.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import FrameType

from gobby.telemetry.logging import get_loop_lag_logger, loop_lag_log_path
from gobby.telemetry.loop_stack_sampler import LoopStackSampler

logger = logging.getLogger(__name__)

# The ceiling #20841 holds a spawn to. One second was the right threshold while
# stalls ran to 66s, but the blockers that produced those are fixed: what remains
# is a few hundred milliseconds at a time, entirely invisible to a watchdog that
# only speaks up past a second, and #20845 committed the loop to p95 <= 50ms and
# p99 <= 100ms. A watchdog set ten times above the bound it exists to police
# cannot report the only stalls left.
DEFAULT_THRESHOLD_SECONDS = 0.2
DEFAULT_POLL_SECONDS = 0.05
# One report per stall rather than one per poll, and a floor on how often a
# pathological loop can fill the log.
DEFAULT_REARM_SECONDS = 15.0
MAX_REPORTED_TASKS = 12
# A guard against a pathological or self-referential await chain, not a budget.
MAX_CHAIN_FRAMES = 60
# The blocking work sits close to the await the task reached right after it.
MAX_TRACE_FRAMES = 10
# A task can finish between the diff and the trace scan a moment later.
GONE = "<no longer live>"


@dataclass(frozen=True)
class LoopLagReport:
    """One observed stall: how long the loop was away, and what moved."""

    lag_seconds: float
    threshold_seconds: float
    advanced: list[tuple[str, str]] = field(default_factory=list)
    started: list[tuple[str, str]] = field(default_factory=list)
    ended: list[str] = field(default_factory=list)
    bystanders: list[str] = field(default_factory=list)
    hot_stacks: list[tuple[str, int]] = field(default_factory=list)
    sample_interval_seconds: float = 0.0

    def to_payload(self) -> dict[str, object]:
        """The whole report as one JSON-ready object, stamped at emit time.

        Values stay primitive -- strings, numbers, flat lists of small dicts --
        so serialization is one cheap ``json.dumps`` with no default hook. The
        expected sample count rides along because the sampler is starved during
        exactly the stalls it exists to explain -- real reports carried 5 to 14
        samples where the interval predicted hundreds -- so a share of the
        collected samples is a hypothesis to time, not a diagnosis (#20845).
        """
        return {
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "lag_seconds": self.lag_seconds,
            "threshold_seconds": self.threshold_seconds,
            "sample_count": sum(count for _, count in self.hot_stacks),
            "expected_sample_count": (
                int(self.lag_seconds / self.sample_interval_seconds)
                if self.sample_interval_seconds > 0
                else None
            ),
            "sample_interval_seconds": self.sample_interval_seconds,
            "hot_stacks": [{"stack": stack, "samples": count} for stack, count in self.hot_stacks],
            "advanced": [{"task": task, "trace": trace} for task, trace in self.advanced],
            "started": [{"task": task, "trace": trace} for task, trace in self.started],
            "ended": list(self.ended),
            "bystander_count": len(self.bystanders),
        }

    def top_hot_frame(self) -> str | None:
        """The innermost frame of the hottest sampled stack, if any was captured."""
        if not self.hot_stacks:
            return None
        stack, _ = self.hot_stacks[0]
        return stack.rsplit(" -> ", 1)[-1]

    def summary(self) -> str:
        """One line for the main log: the fact of the stall, not its contents."""
        head = (
            f"Event loop stalled for {self.lag_seconds:.2f}s "
            f"(threshold {self.threshold_seconds:.2f}s)"
        )
        frame = self.top_hot_frame()
        if frame is None:
            return f"{head}; no loop-thread samples"
        return f"{head}; top hot frame: {frame}"


async def measure_loop_lag(poll_seconds: float) -> float:
    """Sleep, then report how much later than requested the loop came back."""
    loop = asyncio.get_running_loop()
    before = loop.time()
    await asyncio.sleep(poll_seconds)
    return max(0.0, loop.time() - before - poll_seconds)


def _task_key(task: asyncio.Task[object]) -> str:
    """Identify a task across polls, by name plus the coroutine it runs."""
    try:
        name = task.get_name()
    except Exception:  # pragma: no cover - defensive
        name = "<unnamed>"
    try:
        qualname = getattr(task.get_coro(), "__qualname__", None)
    except Exception:  # pragma: no cover - defensive
        qualname = None
    return f"{name} {qualname}" if qualname else name


def _await_frames(task: asyncio.Task[object]) -> list[FrameType]:
    """Walk a task's await chain, outermost frame first.

    ``Task.get_stack`` returns only the task's own coroutine frame. For a
    request that is the outermost middleware, which names the framework rather
    than the handler underneath it. The chain the task is actually blocked on
    hangs off ``cr_await``, so follow it by hand.
    """
    try:
        coro: object | None = task.get_coro()
    except Exception:  # pragma: no cover - defensive
        return []
    frames: list[FrameType] = []
    seen: set[int] = set()
    while coro is not None and id(coro) not in seen and len(frames) < MAX_CHAIN_FRAMES:
        seen.add(id(coro))
        frame = getattr(coro, "cr_frame", None) or getattr(coro, "gi_frame", None)
        if isinstance(frame, FrameType):
            frames.append(frame)
        coro = (
            getattr(coro, "cr_await", None)
            or getattr(coro, "gi_yieldfrom", None)
            or getattr(coro, "ag_await", None)
        )
    return frames


def _format_frame(frame: FrameType) -> str:
    return f"{frame.f_code.co_qualname}@{frame.f_code.co_filename}:{frame.f_lineno}"


def _task_position(task: asyncio.Task[object]) -> str:
    """Fingerprint where a task is suspended, across its whole await chain.

    The innermost frame alone is not enough to tell movement: two unrelated
    awaits in the same task both bottom out at ``Event.wait`` on one line, so a
    leaf-only position reads as "did not move" when the chain above it changed
    entirely. Signing every frame makes any step visible.
    """
    frames = _await_frames(task)
    if not frames:
        return "<no frame>"
    signature = tuple((frame.f_code.co_filename, frame.f_lineno) for frame in frames)
    return f"{len(frames)}:{hash(signature):x}"


def _task_trace(task: asyncio.Task[object]) -> str:
    """Render a task's await chain, so the caller path is visible."""
    frames = _await_frames(task)
    if not frames:
        return "<no frame>"
    return " -> ".join(_format_frame(frame) for frame in frames[-MAX_TRACE_FRAMES:])


def _snapshot_positions(exclude: asyncio.Task[object] | None) -> dict[str, str]:
    """Map every live task to where it is currently suspended."""
    try:
        live = asyncio.all_tasks()
    except RuntimeError:  # pragma: no cover - loop shutting down
        return {}
    return {
        _task_key(task): _task_position(task)
        for task in live
        if task is not exclude and not task.done()
    }


@dataclass(frozen=True)
class LoopLagReportParts:
    """The three groups a stall splits the task set into, as keys only.

    Keys are cheap; await-chain traces are not. This runs on every poll, so it
    stays free of formatting -- a diagnostic that costs the loop real time on
    every cycle inflates the very lag it is trying to measure.
    """

    advanced: list[str]
    started: list[str]
    ended: list[str]
    bystanders: list[str]


def _diff_positions(before: dict[str, str], after: dict[str, str]) -> LoopLagReportParts:
    """Split the tasks that moved from the ones that merely sat there.

    A task present on both sides at a different position is the strongest
    signal: it took a step while nothing else could. A task that only appears
    afterwards is a weaker one -- something ran to create it, but a single
    spawn creates many, so they are kept apart from the suspects.
    """
    advanced = [key for key, position in after.items() if key in before and before[key] != position]
    started = [key for key in after if key not in before]
    ended = [key for key in before if key not in after]
    bystanders = [key for key, position in after.items() if before.get(key) == position]
    return LoopLagReportParts(
        advanced=sorted(advanced),
        started=sorted(started),
        ended=sorted(ended),
        bystanders=bystanders,
    )


def _write_report_line(report: LoopLagReport) -> None:
    """Serialize and append one sidecar line; runs on a worker thread."""
    line = json.dumps(report.to_payload(), separators=(",", ":"))
    get_loop_lag_logger().info("%s", line)


async def _emit_report(report: LoopLagReport) -> None:
    """Full report to the JSONL sidecar, one summary WARNING to the main log.

    The sidecar write is file I/O -- handler setup included -- and goes through
    ``to_thread``: the watchdog lives on the loop it polices, and a
    multi-kilobyte blocking write in its report path would surface in its own
    next measurement.
    """
    sidecar_path = loop_lag_log_path()
    try:
        await asyncio.to_thread(_write_report_line, report)
    except Exception:  # pragma: no cover - a diagnostic must never take the daemon down
        logger.debug("Failed to write the loop lag sidecar line", exc_info=True)
    logger.warning("%s; full report: %s", report.summary(), sidecar_path)


def _traces_for(keys: set[str]) -> dict[str, str]:
    """Capture full await chains, but only for the tasks worth reading."""
    if not keys:
        return {}
    try:
        live = asyncio.all_tasks()
    except RuntimeError:  # pragma: no cover - loop shutting down
        return {}
    traces = {}
    for task in live:
        key = _task_key(task)
        if key in keys and key not in traces:
            traces[key] = _task_trace(task)
    return traces


async def loop_lag_watchdog(
    is_shutdown_requested: Callable[[], bool],
    *,
    threshold_seconds: float = DEFAULT_THRESHOLD_SECONDS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    rearm_seconds: float = DEFAULT_REARM_SECONDS,
    on_lag: Callable[[LoopLagReport], None] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    stack_sampler: LoopStackSampler | None = None,
) -> None:
    """Watch the loop's scheduling delay and report every stall past the threshold."""
    loop = asyncio.get_running_loop()
    self_task = asyncio.current_task()
    last_report_at = float("-inf")
    positions_before = _snapshot_positions(self_task)

    while not is_shutdown_requested():
        try:
            before = loop.time()
            if sleep is not None:
                await sleep(poll_seconds)
            else:
                await asyncio.sleep(poll_seconds)
            lag = max(0.0, loop.time() - before - poll_seconds)
            # Drain every cycle so a report carries only the gap's own samples,
            # never the quiet minutes that preceded it.
            hot_stacks = stack_sampler.drain() if stack_sampler is not None else []
            positions_after = _snapshot_positions(self_task)
            moved = _diff_positions(positions_before, positions_after)
            positions_before = positions_after

            if lag < threshold_seconds or loop.time() - last_report_at < rearm_seconds:
                continue
            last_report_at = loop.time()

            traces = _traces_for(set(moved.advanced) | set(moved.started))
            report = LoopLagReport(
                lag_seconds=lag,
                threshold_seconds=threshold_seconds,
                advanced=[(key, traces.get(key, GONE)) for key in moved.advanced][
                    :MAX_REPORTED_TASKS
                ],
                started=[(key, traces.get(key, GONE)) for key in moved.started][
                    :MAX_REPORTED_TASKS
                ],
                ended=moved.ended[:MAX_REPORTED_TASKS],
                bystanders=moved.bystanders,
                hot_stacks=hot_stacks,
                sample_interval_seconds=(
                    stack_sampler.interval_seconds if stack_sampler is not None else 0.0
                ),
            )
            if on_lag is not None:
                on_lag(report)
            else:
                await _emit_report(report)
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
