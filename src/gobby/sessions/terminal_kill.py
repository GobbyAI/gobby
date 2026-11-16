"""Terminal session kill utility.

Provides kill_terminal_session() for terminating CLI sessions via their
terminal process identity. Extracted from session_control.py
for reuse by HTTP routes.
"""

from __future__ import annotations

import logging
import os
import signal
from collections.abc import Mapping
from typing import Any

import psutil

logger = logging.getLogger(__name__)


async def kill_terminal_session(terminal_ctx: dict[str, Any], session_id: str) -> bool:
    """Kill a plain terminal CLI session using its terminal context.

    Signals the recorded process only when its birth identity still matches.

    Args:
        terminal_ctx: Session's terminal process identity.
        session_id: Session ID for logging.

    Returns:
        True if any kill method succeeded.
    """
    parent_pid = terminal_ctx.get("parent_pid")
    if parent_pid:
        try:
            pid = int(parent_pid)
            if not process_identity_matches(pid, terminal_ctx):
                logger.warning(
                    "Refusing PID fallback for session %s: process identity changed or missing",
                    session_id[:8],
                )
                return False
            os.kill(pid, signal.SIGTERM)
            logger.info("Killed terminal session %s via SIGTERM to PID %s", session_id[:8], pid)
            return True
        except ProcessLookupError:
            logger.debug("PID %s already dead for session %s", parent_pid, session_id[:8])
        except (ValueError, OSError) as e:
            logger.warning("PID kill failed for session %s: %s", session_id[:8], e)

    logger.debug("No kill method available for session %s (no parent_pid)", session_id[:8])
    return False


def create_time_matches(actual: float, recorded: float) -> bool:
    """True when two process create times name the same process, within the clock's resolution."""
    return abs(actual - recorded) < 1.0


def process_identity_matches(pid: int, terminal_ctx: Mapping[str, Any]) -> bool:
    """True when pid is still the process the session recorded: same create time and name."""
    recorded_create_time = terminal_ctx.get("parent_create_time")
    recorded_name = terminal_ctx.get("parent_name")
    if (
        isinstance(recorded_create_time, bool)
        or not isinstance(recorded_create_time, (int, float, str))
        or not isinstance(recorded_name, str)
    ):
        return False
    try:
        expected_create_time = float(recorded_create_time)
        process = psutil.Process(pid)
        return bool(
            create_time_matches(process.create_time(), expected_create_time)
            and process.name() == recorded_name
        )
    except (
        TypeError,
        ValueError,
        psutil.NoSuchProcess,
        psutil.AccessDenied,
        psutil.ZombieProcess,
        OSError,
    ):
        return False
