"""Bounded adapter execution with shielded timeout finalization."""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Final, cast

from gobby.hooks.background_tasks import create_background_task
from gobby.hooks.envelope_dedupe import (
    ENVELOPE_PROCESSING_LEASE_TTL_SECONDS,
    finalize_envelope_processed,
    release_envelope_processing_claim,
    renew_envelope_processing_lease,
)
from gobby.workflows.hooks import WorkflowEvaluationTimeout

logger = logging.getLogger(__name__)

HOOK_ADAPTER_MAX_WORKERS: Final = 8
_HOOK_ADAPTER_EXECUTOR = ThreadPoolExecutor(
    max_workers=HOOK_ADAPTER_MAX_WORKERS,
    thread_name_prefix="gobby-hook-adapter",
)


class AdapterHookTimeout(TimeoutError):
    """Timed out waiting for an adapter worker that may still be running."""

    def __init__(
        self,
        *,
        executor_future: Future[dict[str, Any]] | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        super().__init__("hook adapter timed out")
        self.executor_future = executor_future
        self.timeout_seconds = timeout_seconds


def start_envelope_lease_renewal(envelope_id: str, owner_token: str) -> None:
    """Renew a live envelope lease until the owner can no longer CAS."""
    create_background_task(_renew_envelope_lease(envelope_id, owner_token))


def schedule_adapter_timeout_finalization(
    executor_future: Future[dict[str, Any]],
    *,
    envelope_id: str,
    owner_token: str,
    hook_type: str | None,
) -> None:
    """Finalize the envelope claim when a timed-out worker actually exits."""

    def _on_done(fut: Future[dict[str, Any]]) -> None:
        try:
            result = fut.result()
        except Exception:
            release_envelope_processing_claim(
                envelope_id,
                owner_token=owner_token,
            )
            return
        if isinstance(result, dict):
            finalize_envelope_processed(
                envelope_id,
                owner_token,
                response=result,
                hook_type=hook_type,
            )
            return
        release_envelope_processing_claim(envelope_id, owner_token=owner_token)

    if executor_future.done():
        _on_done(executor_future)
        return
    executor_future.add_done_callback(_on_done)


async def run_adapter_hook(
    adapter: Any,
    payload: dict[str, Any],
    hook_manager: Any,
    *,
    timeout_seconds: float | None,
) -> dict[str, Any]:
    """Run blocking hook work in the bounded adapter executor."""
    loop = asyncio.get_running_loop()
    queued_at = time.perf_counter()
    started_at: float | None = None
    finished_at: float | None = None
    exception_type: str | None = None

    def run_adapter() -> dict[str, Any]:
        nonlocal started_at, finished_at
        started_at = time.perf_counter()
        try:
            return cast(dict[str, Any], adapter.handle_native(payload, hook_manager))
        finally:
            finished_at = time.perf_counter()

    executor_future = _HOOK_ADAPTER_EXECUTOR.submit(run_adapter)
    pending = asyncio.wrap_future(executor_future, loop=loop)
    try:
        if timeout_seconds is None:
            return await pending
        return await asyncio.wait_for(asyncio.shield(pending), timeout=timeout_seconds)
    except WorkflowEvaluationTimeout as exc:
        exception_type = type(exc).__name__
        now = time.perf_counter()
        exc.queue_duration_seconds = (
            started_at - queued_at if started_at is not None else now - queued_at
        )
        exc.execution_duration_seconds = now - started_at if started_at is not None else 0.0
        raise
    except TimeoutError as exc:
        exception_type = type(exc).__name__
        raise AdapterHookTimeout(
            executor_future=executor_future,
            timeout_seconds=timeout_seconds,
        ) from exc
    except BaseException as exc:
        exception_type = type(exc).__name__
        raise
    finally:
        now = finished_at or time.perf_counter()
        queue_duration = started_at - queued_at if started_at is not None else now - queued_at
        execution_duration = now - started_at if started_at is not None else 0.0
        input_data = payload.get("input_data")
        payload_session_id = input_data.get("session_id") if isinstance(input_data, dict) else None
        logger.debug(
            "Hook adapter timing",
            extra={
                "hook_type": payload.get("hook_type"),
                "source": payload.get("source"),
                "session_id": payload.get("_platform_session_id") or payload_session_id,
                "timeout_seconds": timeout_seconds,
                "queue_duration_seconds": queue_duration,
                "execution_duration_seconds": execution_duration,
                "exception_type": exception_type,
            },
        )


async def _renew_envelope_lease(envelope_id: str, owner_token: str) -> None:
    interval = ENVELOPE_PROCESSING_LEASE_TTL_SECONDS / 3
    while True:
        await asyncio.sleep(interval)
        if not renew_envelope_processing_lease(envelope_id, owner_token):
            return
