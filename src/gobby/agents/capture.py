"""Serialized, durable capture-before-kill policy for managed tmux agents."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast

from gobby.storage.agents import AgentRun, AgentRunTerminalReason, TerminalAction

_LOCK_POLL_SECONDS = 0.05
_DEFAULT_LOCK_TIMEOUT_SECONDS = 5.0
_CAPTURE_END = "--- END GOBBY TMUX CAPTURE ---"
_locks_guard = threading.Lock()
_capture_locks: dict[str, threading.Lock] = {}


class KillOutcome(StrEnum):
    """Successful destructive outcomes."""

    KILLED = "killed"
    ALREADY_ABSENT = "already_absent"


class TerminationErrorCode(StrEnum):
    """Retryable typed termination failures."""

    CAPTURE_LOCK_TIMEOUT = "capture_lock_timeout"
    CAPTURE_PERSIST_FAILED = "capture_persist_failed"
    CAPTURE_CONFLICT = "capture_conflict"
    KILL_FAILED = "kill_failed"
    TERMINAL_TRANSITION_FAILED = "terminal_transition_failed"


@dataclass(frozen=True)
class CaptureTerminationResult:
    """Result of one two-phase termination attempt."""

    success: bool
    run: AgentRun | None = None
    kill_outcome: KillOutcome | None = None
    error_code: TerminationErrorCode | None = None
    error: str | None = None


class CaptureStorage(Protocol):
    """Storage surface required by the capture policy."""

    def get(self, run_id: str) -> AgentRun | None: ...

    def record_termination_intent(
        self,
        run_id: str,
        *,
        action: TerminalAction,
        reason: str | None = None,
        result_prefix: str | None = None,
    ) -> AgentRun | None: ...

    def replace_capture_slot(
        self,
        run_id: str,
        *,
        capture_id: str,
        expected_revision: int,
        marker: str,
        slot_content: str,
    ) -> AgentRun | None: ...

    def complete(self, run_id: str, result: str | None = None) -> AgentRun | None: ...

    def fail(
        self,
        run_id: str,
        error: str,
        tool_calls_count: int = 0,
        turns_used: int = 0,
        result: str | None = None,
    ) -> AgentRun | None: ...

    def timeout(
        self,
        run_id: str,
        turns_used: int = 0,
        error: str = "Execution timed out",
        tool_calls_count: int = 0,
        result: str | None = None,
    ) -> AgentRun | None: ...

    def cancel(
        self,
        run_id: str,
        *,
        terminal_reason: AgentRunTerminalReason | None = None,
        result: str | None = None,
    ) -> AgentRun | None: ...


class ManagedTmux(Protocol):
    """Tmux operations used by managed async termination callers."""

    async def has_session(self, name: str) -> bool: ...

    async def capture_full_pane(self, session_name: str) -> str | None: ...

    async def kill_session(
        self,
        name: str,
        *,
        missing_ok: bool = False,
        timeout: float = 5.0,
    ) -> bool: ...


SyncTerminalCallback = Callable[[TerminalAction, str | None], AgentRun | None]
AsyncTerminalCallback = Callable[[TerminalAction, str | None], Awaitable[AgentRun | None]]


def _capture_lock(session_name: str) -> threading.Lock:
    with _locks_guard:
        return _capture_locks.setdefault(session_name, threading.Lock())


def _capture_marker(capture_id: str) -> str:
    return f"--- GOBBY TMUX CAPTURE {capture_id} ---"


def _capture_slot(capture_id: str, capture: str) -> str:
    return f"{_capture_marker(capture_id)}\n{capture}\n{_CAPTURE_END}"


def _capture_failure(reason: str) -> str:
    return f"[capture failed: {reason}]"


def _terminal_error(reason: str | None, capture: str | None) -> str:
    message = reason or "Agent termination requested"
    if not capture:
        return message
    tail = "\n".join(capture.splitlines()[-20:])
    return (
        f"{message}\n\n--- Last 20 lines of terminal output ---\n{tail}"
        "\n[full capture in agent_runs.result]"
    )


def _default_terminalize(
    storage: CaptureStorage,
    run_id: str,
    action: TerminalAction,
    reason: str | None,
) -> AgentRun | None:
    if action == "complete":
        return storage.complete(run_id)
    if action == "fail":
        return storage.fail(run_id, error=reason or "Agent failed")
    if action == "timeout":
        return storage.timeout(run_id, error=reason or "Execution timed out")
    return storage.cancel(
        run_id,
        terminal_reason=cast("AgentRunTerminalReason | None", reason),
    )


def _successful_kill_outcome(
    value: bool | KillOutcome,
    *,
    session_alive: Callable[[], bool],
) -> KillOutcome | None:
    if isinstance(value, KillOutcome):
        return value
    if value:
        return KillOutcome.KILLED
    if not session_alive():
        return KillOutcome.ALREADY_ABSENT
    return None


def _persist_capture_sync(
    storage: CaptureStorage,
    run: AgentRun,
    capture: str,
) -> AgentRun | None:
    capture_id = run.capture_id or str(uuid.uuid4())
    return storage.replace_capture_slot(
        run.id,
        capture_id=capture_id,
        expected_revision=run.capture_revision,
        marker=_capture_marker(capture_id),
        slot_content=_capture_slot(capture_id, capture),
    )


def _failure(
    code: TerminationErrorCode,
    error: str,
    run: AgentRun | None = None,
) -> CaptureTerminationResult:
    return CaptureTerminationResult(False, run=run, error_code=code, error=error)


def capture_then_kill_sync(
    *,
    storage: CaptureStorage,
    run_id: str,
    session_name: str,
    action: TerminalAction,
    session_alive: Callable[[], bool],
    capture: Callable[[], str | None],
    kill: Callable[[], bool | KillOutcome],
    reason: str | None = None,
    result_prefix: str | None = None,
    terminalize: SyncTerminalCallback | None = None,
    lock_timeout: float = _DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> CaptureTerminationResult:
    """Capture, persist, kill, then terminalize under the session lock."""
    lock = _capture_lock(session_name)
    if not lock.acquire(timeout=max(0.0, lock_timeout)):
        return _failure(
            TerminationErrorCode.CAPTURE_LOCK_TIMEOUT,
            f"timed out acquiring capture lock for {session_name}",
        )

    try:
        try:
            run = storage.record_termination_intent(
                run_id,
                action=action,
                reason=reason,
                result_prefix=result_prefix,
            )
        except Exception as exc:
            return _failure(TerminationErrorCode.CAPTURE_PERSIST_FAILED, str(exc))
        if run is None:
            return _failure(
                TerminationErrorCode.CAPTURE_PERSIST_FAILED,
                "agent run is missing or already terminal",
            )

        alive = session_alive()
        captured: str | None = None
        should_capture = not (run.result or "").strip() or action != "complete"
        if should_capture and (alive or run.capture_id is None):
            try:
                captured = capture()
                if captured is None:
                    captured = _capture_failure("capture-pane returned no output")
            except Exception as exc:
                captured = _capture_failure(str(exc))

            alive = session_alive()
            capture_failed = captured.startswith("[capture failed:")
            if not (capture_failed and not alive and run.capture_id is not None):
                try:
                    persisted = _persist_capture_sync(storage, run, captured)
                except Exception as exc:
                    return _failure(
                        TerminationErrorCode.CAPTURE_PERSIST_FAILED,
                        str(exc),
                        run,
                    )
                if persisted is None:
                    return _failure(
                        TerminationErrorCode.CAPTURE_CONFLICT,
                        "capture slot compare-and-set rejected",
                        run,
                    )
                run = persisted

        alive = session_alive()
        if alive:
            try:
                kill_outcome = _successful_kill_outcome(kill(), session_alive=session_alive)
            except Exception as exc:
                return _failure(TerminationErrorCode.KILL_FAILED, str(exc), run)
            if kill_outcome is None:
                return _failure(
                    TerminationErrorCode.KILL_FAILED,
                    f"kill callback left tmux session {session_name} alive",
                    run,
                )
        else:
            kill_outcome = KillOutcome.ALREADY_ABSENT

        payload = reason
        if action in ("fail", "timeout"):
            payload = _terminal_error(reason, captured)
        try:
            transitioned = (
                terminalize(action, payload)
                if terminalize is not None
                else _default_terminalize(storage, run_id, action, payload)
            )
        except Exception as exc:
            return _failure(TerminationErrorCode.TERMINAL_TRANSITION_FAILED, str(exc), run)
        if transitioned is None:
            latest = storage.get(run_id)
            if latest is None or latest.status in ("pending", "running"):
                return _failure(
                    TerminationErrorCode.TERMINAL_TRANSITION_FAILED,
                    "terminal transition compare-and-set rejected",
                    latest or run,
                )
            transitioned = latest
        return CaptureTerminationResult(True, run=transitioned, kill_outcome=kill_outcome)
    finally:
        lock.release()


def _acquire_until(
    lock: threading.Lock,
    stop: threading.Event,
    deadline: float,
) -> bool:
    while not stop.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if lock.acquire(timeout=min(_LOCK_POLL_SECONDS, remaining)):
            return True
    return False


async def _settle_acquisition_worker(
    worker: asyncio.Task[bool],
) -> bool:
    try:
        async with asyncio.timeout((_LOCK_POLL_SECONDS * 3) + 0.1):
            return await asyncio.shield(worker)
    except (TimeoutError, asyncio.CancelledError):
        return False


async def _async_storage_call[ResultT](
    explicit_run_id: str,
    callback: Callable[..., ResultT],
    *args: object,
    **kwargs: object,
) -> ResultT:
    from gobby.agents.terminal_delivery import (
        run_terminal_delivery_offload,
        shielded_terminal_delivery,
    )

    async def operation() -> ResultT:
        return await run_terminal_delivery_offload(callback, *args, **kwargs)

    return await shielded_terminal_delivery(
        explicit_run_id,
        operation,
        raise_if_closed=True,
    )


async def capture_then_kill_async(
    *,
    storage: CaptureStorage,
    run_id: str,
    session_name: str,
    action: TerminalAction,
    session_alive: Callable[[], Awaitable[bool]],
    capture: Callable[[], Awaitable[str | None]],
    kill: Callable[[], Awaitable[bool | KillOutcome]],
    reason: str | None = None,
    result_prefix: str | None = None,
    terminalize: AsyncTerminalCallback | None = None,
    lock_timeout: float = _DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> CaptureTerminationResult:
    """Async capture policy with cancellable, bounded lock acquisition."""
    lock = _capture_lock(session_name)
    stop = threading.Event()
    deadline = time.monotonic() + max(0.0, lock_timeout)
    worker = asyncio.create_task(
        asyncio.to_thread(_acquire_until, lock, stop, deadline),
        name=f"capture-lock:{session_name}",
    )
    try:
        acquired = await asyncio.shield(worker)
    except asyncio.CancelledError:
        stop.set()
        acquired = await _settle_acquisition_worker(worker)
        if acquired:
            lock.release()
        raise

    if not acquired:
        return _failure(
            TerminationErrorCode.CAPTURE_LOCK_TIMEOUT,
            f"timed out acquiring capture lock for {session_name}",
        )

    try:
        try:
            run = await _async_storage_call(
                run_id,
                storage.record_termination_intent,
                run_id,
                action=action,
                reason=reason,
                result_prefix=result_prefix,
            )
        except Exception as exc:
            return _failure(TerminationErrorCode.CAPTURE_PERSIST_FAILED, str(exc))
        if run is None:
            return _failure(
                TerminationErrorCode.CAPTURE_PERSIST_FAILED,
                "agent run is missing or already terminal",
            )

        alive = await session_alive()
        captured: str | None = None
        should_capture = not (run.result or "").strip() or action != "complete"
        if should_capture and (alive or run.capture_id is None):
            try:
                captured = await capture()
                if captured is None:
                    captured = _capture_failure("capture-pane returned no output")
            except Exception as exc:
                captured = _capture_failure(str(exc))

            alive = await session_alive()
            capture_failed = captured.startswith("[capture failed:")
            if not (capture_failed and not alive and run.capture_id is not None):
                try:
                    persisted = await _async_storage_call(
                        run_id,
                        _persist_capture_sync,
                        storage,
                        run,
                        captured,
                    )
                except Exception as exc:
                    return _failure(
                        TerminationErrorCode.CAPTURE_PERSIST_FAILED,
                        str(exc),
                        run,
                    )
                if persisted is None:
                    return _failure(
                        TerminationErrorCode.CAPTURE_CONFLICT,
                        "capture slot compare-and-set rejected",
                        run,
                    )
                run = persisted

        alive = await session_alive()
        if alive:
            try:
                value = await kill()
            except Exception as exc:
                return _failure(TerminationErrorCode.KILL_FAILED, str(exc), run)
            if isinstance(value, KillOutcome):
                kill_outcome = value
            elif value:
                kill_outcome = KillOutcome.KILLED
            elif not await session_alive():
                kill_outcome = KillOutcome.ALREADY_ABSENT
            else:
                kill_outcome = None
            if kill_outcome is None:
                return _failure(
                    TerminationErrorCode.KILL_FAILED,
                    f"kill callback left tmux session {session_name} alive",
                    run,
                )
        else:
            kill_outcome = KillOutcome.ALREADY_ABSENT

        payload = reason
        if action in ("fail", "timeout"):
            payload = _terminal_error(reason, captured)
        try:
            if terminalize is not None:
                transitioned = await terminalize(action, payload)
            else:
                transitioned = await _async_storage_call(
                    run_id,
                    _default_terminalize,
                    storage,
                    run_id,
                    action,
                    payload,
                )
        except Exception as exc:
            return _failure(TerminationErrorCode.TERMINAL_TRANSITION_FAILED, str(exc), run)
        if transitioned is None:
            latest = await _async_storage_call(run_id, storage.get, run_id)
            if latest is None or latest.status in ("pending", "running"):
                return _failure(
                    TerminationErrorCode.TERMINAL_TRANSITION_FAILED,
                    "terminal transition compare-and-set rejected",
                    latest or run,
                )
            transitioned = latest
        return CaptureTerminationResult(True, run=transitioned, kill_outcome=kill_outcome)
    finally:
        lock.release()


async def terminate_managed_tmux_async(
    *,
    storage: CaptureStorage,
    run: AgentRun,
    tmux: ManagedTmux,
    action: TerminalAction,
    reason: str | None = None,
    result_prefix: str | None = None,
    terminalize: AsyncTerminalCallback | None = None,
    lock_timeout: float = _DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> CaptureTerminationResult:
    """Apply the policy using the standard async tmux manager operations."""
    session_name = run.tmux_session_name
    if not session_name:
        return _failure(
            TerminationErrorCode.KILL_FAILED,
            "agent run has no tmux session name",
            run,
        )
    return await capture_then_kill_async(
        storage=storage,
        run_id=run.id,
        session_name=session_name,
        action=action,
        reason=reason,
        result_prefix=result_prefix,
        terminalize=terminalize,
        session_alive=lambda: tmux.has_session(session_name),
        capture=lambda: tmux.capture_full_pane(session_name),
        kill=lambda: tmux.kill_session(session_name, missing_ok=True),
        lock_timeout=lock_timeout,
    )
