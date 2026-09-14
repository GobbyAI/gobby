"""Bounded adapter execution with shielded timeout finalization."""

from __future__ import annotations

import asyncio
import logging
import threading
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
from gobby.hooks.fifo_lock import CrossLoopFifoLock
from gobby.hooks.receipt_effects import (
    STAGED_EFFECTS_FIELD,
    take_worker_staging,
    worker_staging_scope,
)
from gobby.workflows.evaluation_runtime import WorkflowEvaluationTimeout

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
        session_id: str | None = None,
        admission_wait_seconds: float | None = None,
        queue_duration_seconds: float | None = None,
        execution_duration_seconds: float | None = None,
    ) -> None:
        super().__init__("hook adapter timed out")
        self.executor_future = executor_future
        self.timeout_seconds = timeout_seconds
        self.session_id = session_id
        self.admission_wait_seconds = admission_wait_seconds
        self.queue_duration_seconds = queue_duration_seconds
        self.execution_duration_seconds = execution_duration_seconds


def start_envelope_lease_renewal(envelope_id: str, owner_token: str) -> asyncio.Task[None]:
    """Renew a live envelope lease until the owner can no longer CAS.

    The caller owns the returned task: cancel it when the execution that
    holds the lease ends, or the lease outlives its owner and every replay
    of the envelope is refused as a duplicate.
    """
    return create_background_task(_renew_envelope_lease(envelope_id, owner_token))


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


class _SessionAdmission:
    """One session's adapter admission lock and its outstanding reservations."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.lock = CrossLoopFifoLock()
        self.references = 0


_session_admissions_lock = threading.Lock()
_session_admissions: dict[str, _SessionAdmission] = {}


def _reserve_session_admission(session_id: str) -> _SessionAdmission:
    with _session_admissions_lock:
        admission = _session_admissions.get(session_id)
        if admission is None:
            admission = _SessionAdmission(session_id)
            _session_admissions[session_id] = admission
        admission.references += 1
        return admission


def _end_session_admission(admission: _SessionAdmission, *, admitted: bool) -> None:
    if admitted:
        admission.lock.release()
    with _session_admissions_lock:
        admission.references -= 1
        if admission.references == 0:
            _session_admissions.pop(admission.session_id, None)


async def run_adapter_hook(
    adapter: Any,
    payload: dict[str, Any],
    hook_manager: Any,
    *,
    timeout_seconds: float | None,
) -> dict[str, Any]:
    """Run blocking hook work in the bounded adapter executor.

    A session's hooks take adapter workers one at a time, in arrival order. Its
    rule evaluations serialize on the session eval lock anyway; without this, a
    flooding session (native subagents hook under the parent's session id)
    parks every shared worker and times out every other session's hooks.
    """
    loop = asyncio.get_running_loop()
    arrived_at = time.perf_counter()
    submitted_at: float | None = None
    started_at: float | None = None
    finished_at: float | None = None
    exception_type: str | None = None
    session_key = payload.get("_platform_session_id")
    session_id = session_key if isinstance(session_key, str) and session_key else None
    admission = _reserve_session_admission(session_id) if session_id else None
    admitted = False
    executor_future: Future[dict[str, Any]] | None = None

    def run_adapter() -> dict[str, Any]:
        nonlocal started_at, finished_at
        started_at = time.perf_counter()
        # This scope is the boundary of one logical delivery. Rule evaluation
        # hops to the workflow runtime thread and offloads to the rule-engine
        # executor; both inherit this context, so they share this delivery's
        # staging buffer and nothing they stage survives into the next delivery
        # that lands on those shared threads (#21427).
        with worker_staging_scope():
            try:
                result = cast(dict[str, Any], adapter.handle_native(payload, hook_manager))
                staged = take_worker_staging()
                if not staged:
                    return result
                attached = dict(result) if isinstance(result, dict) else {}
                attached[STAGED_EFFECTS_FIELD] = staged
                return attached
            finally:
                finished_at = time.perf_counter()

    def durations() -> tuple[float, float, float]:
        """Return admission wait, executor queue, and execution seconds so far."""
        now = finished_at or time.perf_counter()
        if submitted_at is None:
            return now - arrived_at, 0.0, 0.0
        admission_wait = submitted_at - arrived_at
        if started_at is None:
            return admission_wait, now - submitted_at, 0.0
        return admission_wait, started_at - submitted_at, now - started_at

    try:
        if admission is not None:
            await asyncio.wait_for(admission.lock.acquire(), timeout=timeout_seconds)
            admitted = True
        submitted_at = time.perf_counter()
        executor_future = _HOOK_ADAPTER_EXECUTOR.submit(run_adapter)
        if admission is not None:
            # The worker owns the slot, so one that outlives this request's
            # timeout keeps the session's later hooks queued until it exits.
            held = admission
            executor_future.add_done_callback(
                lambda _worker: _end_session_admission(held, admitted=True)
            )
        pending = asyncio.wrap_future(executor_future, loop=loop)
        if timeout_seconds is None:
            return await pending
        remaining = max(0.0, timeout_seconds - (submitted_at - arrived_at))
        return await asyncio.wait_for(asyncio.shield(pending), timeout=remaining)
    except WorkflowEvaluationTimeout as exc:
        exception_type = type(exc).__name__
        (
            exc.admission_wait_seconds,
            exc.queue_duration_seconds,
            exc.execution_duration_seconds,
        ) = durations()
        raise
    except TimeoutError as exc:
        exception_type = type(exc).__name__
        admission_wait, queue_duration, execution_duration = durations()
        raise AdapterHookTimeout(
            executor_future=executor_future,
            timeout_seconds=timeout_seconds,
            session_id=session_id,
            admission_wait_seconds=admission_wait,
            queue_duration_seconds=queue_duration,
            execution_duration_seconds=execution_duration,
        ) from exc
    except BaseException as exc:
        exception_type = type(exc).__name__
        raise
    finally:
        if admission is not None and executor_future is None:
            _end_session_admission(admission, admitted=admitted)
        admission_wait, queue_duration, execution_duration = durations()
        input_data = payload.get("input_data")
        payload_session_id = input_data.get("session_id") if isinstance(input_data, dict) else None
        logger.debug(
            "Hook adapter timing",
            extra={
                "hook_type": payload.get("hook_type"),
                "source": payload.get("source"),
                "session_id": session_id or payload_session_id,
                "timeout_seconds": timeout_seconds,
                "admission_wait_seconds": admission_wait,
                "queue_duration_seconds": queue_duration,
                "execution_duration_seconds": execution_duration,
                "exception_type": exception_type,
            },
        )


async def _renew_envelope_lease(envelope_id: str, owner_token: str) -> None:
    interval = ENVELOPE_PROCESSING_LEASE_TTL_SECONDS / 3
    while True:
        await asyncio.sleep(interval)
        if not await asyncio.to_thread(renew_envelope_processing_lease, envelope_id, owner_token):
            return
