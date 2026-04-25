"""Health check utilities for spawned agents."""

from __future__ import annotations

import asyncio
import logging
import os

from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.config.tmux import TmuxConfig

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
    config = TmuxConfig()
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
    except TimeoutError:
        return True  # Timed out, assume alive
    except asyncio.CancelledError:
        raise
    except Exception:
        return True  # If check itself fails, don't false-positive
