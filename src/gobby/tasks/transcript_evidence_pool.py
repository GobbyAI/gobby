"""Process-pool offload for CPU-bound transcript evidence derivation."""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import threading
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

logger = logging.getLogger(__name__)

_pool: ProcessPoolExecutor | None = None
_pool_lock = threading.Lock()
_fallback_warning_logged = False


def _get_pool() -> ProcessPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ProcessPoolExecutor(
                max_workers=1,
                mp_context=multiprocessing.get_context("spawn"),
            )
        return _pool


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


async def run_in_transcript_evidence_pool[T](
    function: Callable[..., T],
    /,
    *args: object,
) -> T:
    """Run one picklable derivation outside the daemon process."""
    pool: ProcessPoolExecutor | None = None
    try:
        pool = _get_pool()
        pending = asyncio.get_running_loop().run_in_executor(pool, function, *args)
    except (BrokenProcessPool, OSError) as exc:
        _handle_pool_failure(pool, exc)
        return await asyncio.to_thread(function, *args)
    try:
        return await pending
    except BrokenProcessPool as exc:
        _handle_pool_failure(pool, exc)
        return await asyncio.to_thread(function, *args)


def shutdown_transcript_evidence_pool() -> None:
    """Stop accepting derivations and let the worker exit with the daemon."""
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


__all__ = ["run_in_transcript_evidence_pool", "shutdown_transcript_evidence_pool"]
