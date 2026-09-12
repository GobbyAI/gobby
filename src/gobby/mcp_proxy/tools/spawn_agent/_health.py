"""Health check utilities for spawned agents."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Any, Protocol

import psycopg

from gobby.agents.capture import _capture_marker, _capture_slot
from gobby.agents.tmux.errors import TmuxNotFoundError, TmuxSessionError
from gobby.storage.terminals import Terminal, TerminalManager
from gobby.terminals.runtime import TerminalRuntimeRegistry
from gobby.utils.terminal_output import redact_terminal_output

logger = logging.getLogger(__name__)

_TMUX_HEALTH_CHECK_TIMEOUT_SECONDS = 5.0
_PANE_ERROR_MAX_CHARS = 1024
_PANE_ERROR_TRUNCATION_MARKER = "[truncated]\n"

# Track fire-and-forget health check tasks for clean shutdown
_health_check_tasks: set[asyncio.Task[None]] = set()
_health_check_handles: set[asyncio.TimerHandle] = set()


class _RunStorageForHealth(Protocol):
    db: Any

    def get(self, run_id: str) -> Any | None: ...

    def fail(
        self,
        run_id: str,
        error: str,
        tool_calls_count: int = 0,
        turns_used: int = 0,
    ) -> Any | None: ...

    def replace_capture_slot(
        self,
        run_id: str,
        *,
        capture_id: str,
        expected_revision: int,
        marker: str,
        slot_content: str,
    ) -> Any | None: ...


class _RunnerWithRunStorage(Protocol):
    @property
    def run_storage(self) -> _RunStorageForHealth: ...

    @property
    def terminal_manager(self) -> TerminalManager: ...

    @property
    def terminal_runtime_registry(self) -> TerminalRuntimeRegistry: ...


def _redacted_pane_output(output: str) -> str:
    return redact_terminal_output(output.strip())


def _intentional_pane_tail(redacted: str) -> str:
    if len(redacted) <= _PANE_ERROR_MAX_CHARS:
        return redacted
    tail_chars = _PANE_ERROR_MAX_CHARS - len(_PANE_ERROR_TRUNCATION_MARKER)
    return f"{_PANE_ERROR_TRUNCATION_MARKER}{redacted[-tail_chars:]}"


def _bounded_redacted_pane_output(output: str) -> str:
    """Intentional error-field tail. The full redacted pane is persisted separately."""
    return _intentional_pane_tail(_redacted_pane_output(output))


def _persist_health_pane_capture(
    storage: _RunStorageForHealth,
    run: Any,
    run_id: str,
    redacted: str,
) -> str | None:
    replace = getattr(storage, "replace_capture_slot", None)
    if run is None or not callable(replace):
        return None
    capture_id = getattr(run, "capture_id", None)
    if not isinstance(capture_id, str) or not capture_id:
        capture_id = str(uuid.uuid4())
    expected_revision = getattr(run, "capture_revision", 0) or 0
    try:
        updated = replace(
            run_id,
            capture_id=capture_id,
            expected_revision=expected_revision,
            marker=_capture_marker(capture_id),
            slot_content=_capture_slot(capture_id, redacted),
        )
    except psycopg.Error as exc:
        logger.warning("Failed to persist health pane capture for %s: %s", run_id, exc)
        return None
    if updated is None:
        return None
    return capture_id


def cancel_health_checks() -> None:
    """Cancel all pending health check tasks (call on shutdown)."""
    for handle in _health_check_handles:
        handle.cancel()
    _health_check_handles.clear()
    for task in _health_check_tasks:
        task.cancel()
    _health_check_tasks.clear()


async def cancel_and_await_health_checks() -> None:
    """Cancel deferred health checks and await shielded terminal settlements."""
    for handle in _health_check_handles:
        handle.cancel()
    _health_check_handles.clear()
    tasks = tuple(_health_check_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# Seconds to wait before checking if tmux session survived spawn.
# Configurable via GOBBY_TMUX_HEALTH_CHECK_DELAY env var.
try:
    TMUX_HEALTH_CHECK_DELAY = float(os.environ.get("GOBBY_TMUX_HEALTH_CHECK_DELAY", "0.5"))
except (ValueError, TypeError):
    TMUX_HEALTH_CHECK_DELAY = 0.5


async def _terminal_is_live(
    row: Terminal,
    registry: TerminalRuntimeRegistry,
) -> tuple[bool, str | None]:
    """Check a terminal row through the runtime registered for its backend."""
    runtime = registry.resolve(row.backend)
    try:
        alive = await asyncio.wait_for(
            runtime.is_live(row),
            timeout=_TMUX_HEALTH_CHECK_TIMEOUT_SECONDS,
        )
        if alive:
            return alive, None
        try:
            snapshot = await asyncio.wait_for(
                runtime.snapshot(row, lines=50),
                timeout=_TMUX_HEALTH_CHECK_TIMEOUT_SECONDS,
            )
        except (TimeoutError, OSError, TmuxNotFoundError, TmuxSessionError):
            return False, None
        output = snapshot.text
        if not output or not output.strip():
            return False, None
        return False, _bounded_redacted_pane_output(output)
    except (TimeoutError, OSError, TmuxNotFoundError, TmuxSessionError):
        return True, None  # Timed out, assume alive
    except asyncio.CancelledError:
        raise


async def _deferred_tmux_health_check(
    runner: _RunnerWithRunStorage,
    run_id: str,
    terminal_id: str,
    delay: float = 0,
    completion_registry: Any | None = None,
) -> None:
    try:
        await asyncio.sleep(delay)
        row = await asyncio.to_thread(runner.terminal_manager.get, terminal_id)
        if row is None:
            return
        alive, pane_output = await _terminal_is_live(row, runner.terminal_runtime_registry)
        if not alive:
            run = runner.run_storage.get(run_id)
            if run is not None and run.status not in ("pending", "running"):
                return
            error = "Agent process exited immediately after spawn"
            if pane_output:
                redacted_output = _redacted_pane_output(pane_output)
                safe_output = _intentional_pane_tail(redacted_output)
                capture_id = _persist_health_pane_capture(
                    runner.run_storage, run, run_id, redacted_output
                )
                error = f"{error}\nPane output:\n{safe_output}"
                if capture_id:
                    error = f"{error}\ncapture_id={capture_id}"
            logger.error("Agent %s terminal %r: %s", run_id, terminal_id, error)
            try:
                failed = runner.run_storage.fail(run_id, error=error)
                if failed is not None:
                    from gobby.agents.terminal_delivery import (
                        deliver_existing_terminal_run,
                        run_terminal_delivery_offload,
                    )

                    try:
                        await deliver_existing_terminal_run(
                            db=runner.run_storage.db,
                            agent_run_manager=runner.run_storage,
                            completion_registry=completion_registry,
                            run_id=run_id,
                            run_db=run_terminal_delivery_offload,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Failed to deliver terminal agent_run %s after health check: %s",
                            run_id,
                            exc,
                            exc_info=True,
                        )
            except psycopg.Error as e:
                logger.warning("Failed to mark agent_run %s as failed: %s", run_id, e)
    except asyncio.CancelledError:
        pass
    except (TimeoutError, OSError, TmuxNotFoundError, TmuxSessionError, psycopg.Error) as e:
        logger.warning("Deferred health check for %s failed: %s", run_id, e)


def _start_tmux_health_check(
    runner: _RunnerWithRunStorage,
    run_id: str,
    terminal_id: str,
    completion_registry: Any | None = None,
) -> None:
    health_task = asyncio.create_task(
        _deferred_tmux_health_check(
            runner,
            run_id,
            terminal_id,
            0,
            completion_registry,
        ),
        name=f"tmux-health-{run_id}",
    )
    _health_check_tasks.add(health_task)
    health_task.add_done_callback(_health_check_tasks.discard)


def schedule_tmux_health_check(
    runner: _RunnerWithRunStorage,
    run_id: str,
    terminal_id: str,
    completion_registry: Any | None = None,
    delay: float = TMUX_HEALTH_CHECK_DELAY,
) -> asyncio.TimerHandle:
    """Schedule a post-spawn terminal liveness check without a sleeping task."""
    loop = asyncio.get_running_loop()
    handle: asyncio.TimerHandle | None = None

    def start_health_check() -> None:
        if handle is not None:
            _health_check_handles.discard(handle)
        _start_tmux_health_check(
            runner,
            run_id,
            terminal_id,
            completion_registry,
        )

    handle = loop.call_later(delay, start_health_check)
    _health_check_handles.add(handle)
    return handle
