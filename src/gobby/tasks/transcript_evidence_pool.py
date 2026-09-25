"""Process-pool offload for CPU-bound transcript evidence derivation."""

from __future__ import annotations

import asyncio
import importlib
import logging
import multiprocessing
import os
import tempfile
import threading
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from time import monotonic, sleep

from gobby.hooks.phase_timing import add_hook_phase, timed_to_thread

logger = logging.getLogger(__name__)

_pool: ProcessPoolExecutor | None = None
_pool_lock = threading.Lock()
_fallback_warning_logged = False
_POOL_WORKERS = 4
_POOL_PREWARM_TIMEOUT_SECONDS = 60.0


def _prewarm_probe(marker_directory: str) -> None:
    importlib.import_module("gobby.tasks.transcript_evidence")
    markers = Path(marker_directory)
    (markers / str(os.getpid())).touch()
    deadline = monotonic() + _POOL_PREWARM_TIMEOUT_SECONDS
    while len(list(markers.iterdir())) < _POOL_WORKERS:
        if monotonic() >= deadline:
            raise TimeoutError("Transcript evidence workers did not prewarm together")
        sleep(0.02)


def _get_pool() -> ProcessPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ProcessPoolExecutor(
                max_workers=_POOL_WORKERS,
                mp_context=multiprocessing.get_context("spawn"),
            )
        return _pool


async def prewarm_transcript_evidence_pool() -> None:
    """Finish all worker imports before the daemon admits Stop hooks."""
    with tempfile.TemporaryDirectory(prefix="gobby-transcript-pool-") as marker_directory:
        try:
            pool = _get_pool()
            loop = asyncio.get_running_loop()
            probes = [
                loop.run_in_executor(pool, _prewarm_probe, marker_directory)
                for _ in range(_POOL_WORKERS)
            ]
            await asyncio.wait_for(
                asyncio.gather(*probes), timeout=_POOL_PREWARM_TIMEOUT_SECONDS + 5
            )
        except BaseException:
            shutdown_transcript_evidence_pool()
            raise
    logger.info("Transcript evidence pool prewarmed with %d workers", _POOL_WORKERS)


def _discard_pool(pool: ProcessPoolExecutor) -> None:
    global _pool
    with _pool_lock:
        if _pool is pool:
            _pool = None
    pool.shutdown(wait=False, cancel_futures=True)


def _warn_fallback_once(exc: BrokenProcessPool | OSError) -> None:
    global _fallback_warning_logged
    with _pool_lock:
        if _fallback_warning_logged:
            return
        _fallback_warning_logged = True
    logger.warning(
        "Transcript evidence process pool unavailable; falling back to asyncio.to_thread: %s: %s",
        type(exc).__name__,
        exc,
    )


def _handle_pool_failure(
    pool: ProcessPoolExecutor | None,
    exc: BrokenProcessPool | OSError,
) -> None:
    if pool is not None:
        _discard_pool(pool)
    _warn_fallback_once(exc)


def _timed_run[T](function: Callable[..., T], args: tuple[object, ...]) -> tuple[T, float, float]:
    started_at = monotonic()
    result = function(*args)
    return result, started_at, monotonic()


async def run_in_transcript_evidence_pool[T](
    function: Callable[..., T],
    /,
    *args: object,
) -> T:
    """Run one picklable derivation outside the daemon process."""
    pool: ProcessPoolExecutor | None = None
    submitted_at = monotonic()
    try:
        pool = _get_pool()
        pending = asyncio.get_running_loop().run_in_executor(pool, _timed_run, function, args)
    except (BrokenProcessPool, OSError) as exc:
        _handle_pool_failure(pool, exc)
        return await timed_to_thread("prelude_transcript_fallback", function, *args)
    try:
        result, started_at, finished_at = await pending
        add_hook_phase("prelude_transcript_pool_queue", started_at - submitted_at)
        add_hook_phase("prelude_transcript_pool_work", finished_at - started_at)
        return result
    except BrokenProcessPool as exc:
        _handle_pool_failure(pool, exc)
        return await timed_to_thread("prelude_transcript_fallback", function, *args)


POOL_EXIT_TIMEOUT_SECONDS = 2.0


def _stop_resource_tracker() -> None:
    """Stop the multiprocessing resource tracker once no pool process needs it.

    The tracker ignores SIGTERM, so leaving it running hands it to the shutdown
    reaper's force-kill path. ``ResourceTracker._stop`` is CPython-private
    (3.13+); its absence or failure is tolerated.
    """
    from multiprocessing import resource_tracker

    stop = getattr(getattr(resource_tracker, "_resource_tracker", None), "_stop", None)
    if not callable(stop):
        return
    try:
        stop()
    except Exception:
        logger.debug("Resource tracker stop failed", exc_info=True)


def _drain_pool_and_stop_tracker(pool: ProcessPoolExecutor, done: threading.Event) -> None:
    # One waiting shutdown: ``ProcessPoolExecutor.shutdown`` drops its manager
    # thread reference on the first call, so a preceding ``wait=False`` call
    # would make this one return before the worker, the queue feeder thread,
    # and the queue semaphores are gone -- and a semaphore finalized after the
    # tracker stop relaunches the tracker.
    try:
        pool.shutdown(wait=True, cancel_futures=True)
        _stop_resource_tracker()
    finally:
        done.set()


def shutdown_transcript_evidence_pool(*, timeout: float = POOL_EXIT_TIMEOUT_SECONDS) -> None:
    """Stop accepting derivations, wait for the worker to exit, then stop the tracker.

    The wait is bounded by ``timeout``; when the worker does not exit in time the
    survivors are left to the shutdown reaper.
    """
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is None:
        return
    done = threading.Event()
    threading.Thread(
        target=_drain_pool_and_stop_tracker,
        args=(pool, done),
        name="transcript-evidence-pool-exit",
        daemon=True,
    ).start()
    if not done.wait(timeout):
        logger.info(
            "Transcript evidence pool worker did not exit within %.1fs; "
            "leaving it to the shutdown reaper",
            timeout,
        )


__all__ = [
    "prewarm_transcript_evidence_pool",
    "run_in_transcript_evidence_pool",
    "shutdown_transcript_evidence_pool",
]
