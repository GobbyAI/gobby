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
from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.sessions.session_wiki_file import redact_session_markdown

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


def _redacted_pane_output(output: str) -> str:
    return redact_session_markdown(output.strip())


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


async def _check_tmux_session_alive(
    session_name: str,
    socket_name: str | None = None,
    socket_path: str | None = None,
) -> tuple[bool, str | None]:
    """Check if a tmux session is still alive after spawn."""
    from gobby.agents.tmux import get_configured_tmux_config

    config = get_configured_tmux_config()
    if socket_name is not None or socket_path is not None:
        config = config.model_copy(
            update={
                "socket_name": config.socket_name if socket_name is None else socket_name,
                "socket_path": socket_path,
            }
        )
    manager = TmuxSessionManager(config)
    if not manager.is_available():
        return True, None  # Can't check without tmux binary, assume alive
    try:
        info = await asyncio.wait_for(
            manager.get_session(session_name),
            timeout=_TMUX_HEALTH_CHECK_TIMEOUT_SECONDS,
        )
        alive = bool(info and not info.pane_dead and info.pane_pid is not None)
        if alive or info is None:
            return alive, None
        try:
            output = await asyncio.wait_for(
                manager.capture_pane(session_name, lines=50),
                timeout=_TMUX_HEALTH_CHECK_TIMEOUT_SECONDS,
            )
        except (TimeoutError, OSError, TmuxNotFoundError, TmuxSessionError):
            output = None
        if not output or not output.strip():
            return False, None
        return False, output.strip()
    except (TimeoutError, OSError, TmuxNotFoundError, TmuxSessionError):
        return True, None  # Timed out, assume alive
    except asyncio.CancelledError:
        raise


async def _deferred_tmux_health_check(
    runner: _RunnerWithRunStorage,
    run_id: str,
    tmux_session_name: str,
    socket_name: str | None,
    socket_path: str | None,
    delay: float,
    completion_registry: Any | None = None,
) -> None:
    try:
        await asyncio.sleep(delay)
        alive, pane_output = await _check_tmux_session_alive(
            tmux_session_name,
            socket_name=socket_name,
            socket_path=socket_path,
        )
        if not alive:
            run = runner.run_storage.get(run_id)
            if run is not None and run.status not in ("pending", "running"):
                return
            error = "Agent process exited immediately after spawn"
            if pane_output:
                redacted = _redacted_pane_output(pane_output)
                tail = _intentional_pane_tail(redacted)
                capture_id = _persist_health_pane_capture(runner.run_storage, run, run_id, redacted)
                error = f"{error}\nPane output:\n{tail}"
                if capture_id:
                    error = f"{error}\ncapture_id={capture_id}"
            logger.error("Agent %s tmux session %r: %s", run_id, tmux_session_name, error)
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
    tmux_session_name: str,
    socket_name: str | None,
    socket_path: str | None,
    completion_registry: Any | None = None,
) -> None:
    health_task = asyncio.create_task(
        _deferred_tmux_health_check(
            runner,
            run_id,
            tmux_session_name,
            socket_name,
            socket_path,
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
    tmux_session_name: str,
    socket_name: str | None,
    socket_path: str | None,
    completion_registry: Any | None = None,
    delay: float = TMUX_HEALTH_CHECK_DELAY,
) -> asyncio.TimerHandle:
    """Schedule a post-spawn tmux liveness check without leaving a sleeping task."""
    loop = asyncio.get_running_loop()
    handle: asyncio.TimerHandle | None = None

    def start_health_check() -> None:
        if handle is not None:
            _health_check_handles.discard(handle)
        _start_tmux_health_check(
            runner,
            run_id,
            tmux_session_name,
            socket_name,
            socket_path,
            completion_registry,
        )

    handle = loop.call_later(delay, start_health_check)
    _health_check_handles.add(handle)
    return handle
