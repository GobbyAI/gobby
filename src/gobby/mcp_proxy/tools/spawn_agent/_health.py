"""Health check utilities for spawned agents."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import psycopg

from gobby.agents.tmux.errors import TmuxNotFoundError, TmuxSessionError
from gobby.agents.tmux.session_manager import TmuxSessionManager

logger = logging.getLogger(__name__)

# Track fire-and-forget health check tasks for clean shutdown
_health_check_tasks: set[asyncio.Task[None]] = set()


def cancel_health_checks() -> None:
    """Cancel all pending health check tasks (call on shutdown)."""
    for task in _health_check_tasks:
        task.cancel()
    _health_check_tasks.clear()


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
) -> bool:
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
        return True  # Can't check without tmux binary, assume alive
    try:
        info = await asyncio.wait_for(manager.get_session(session_name), timeout=5.0)
        return bool(info and not info.pane_dead and info.pane_pid is not None)
    except (TimeoutError, OSError, TmuxNotFoundError, TmuxSessionError):
        return True  # Timed out, assume alive
    except asyncio.CancelledError:
        raise


async def _deferred_tmux_health_check(
    runner: Any,
    run_id: str,
    tmux_session_name: str,
    socket_name: str | None,
    socket_path: str | None,
    delay: float,
) -> None:
    try:
        await asyncio.sleep(delay)
        alive = await _check_tmux_session_alive(
            tmux_session_name,
            socket_name=socket_name,
            socket_path=socket_path,
        )
        if not alive:
            run = runner.run_storage.get(run_id)
            if run is not None and run.status not in ("pending", "running"):
                return
            logger.error(
                "Agent %s tmux session %r exited immediately after spawn",
                run_id,
                tmux_session_name,
            )
            try:
                runner.run_storage.fail(
                    run_id,
                    error="Agent process exited immediately after spawn",
                )
            except psycopg.Error as e:
                logger.warning("Failed to mark agent_run %s as failed: %s", run_id, e)
    except asyncio.CancelledError:
        pass
    except (TimeoutError, OSError, TmuxNotFoundError, TmuxSessionError, psycopg.Error) as e:
        logger.warning("Deferred health check for %s failed: %s", run_id, e)


def schedule_tmux_health_check(
    runner: Any,
    run_id: str,
    tmux_session_name: str,
    socket_name: str | None,
    socket_path: str | None,
    delay: float = TMUX_HEALTH_CHECK_DELAY,
) -> None:
    """Schedule a post-spawn tmux liveness check."""
    health_task = asyncio.create_task(
        _deferred_tmux_health_check(
            runner,
            run_id,
            tmux_session_name,
            socket_name,
            socket_path,
            delay,
        ),
        name=f"tmux-health-{run_id}",
    )
    _health_check_tasks.add(health_task)
    health_task.add_done_callback(_health_check_tasks.discard)
