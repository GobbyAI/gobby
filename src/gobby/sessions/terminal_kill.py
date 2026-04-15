"""Terminal session kill utility.

Provides kill_terminal_session() for terminating CLI sessions via their
terminal context (tmux pane or PID). Extracted from session_control.py
for reuse by HTTP routes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from typing import Any

from gobby.sessions.tmux_context import get_tmux_prefix_for_context

logger = logging.getLogger(__name__)


async def kill_terminal_session(terminal_ctx: dict[str, Any], session_id: str) -> bool:
    """Kill a plain terminal CLI session using its terminal context.

    Tries tmux pane kill first (cleanest — kills just that pane), then
    falls back to PID-based SIGTERM.

    Args:
        terminal_ctx: Session's terminal_context dict (tmux_pane, parent_pid, etc.)
        session_id: Session ID for logging.

    Returns:
        True if any kill method succeeded.
    """
    # 1. Try tmux pane kill (sends SIGHUP to process in pane)
    tmux_pane = terminal_ctx.get("tmux_pane")
    if tmux_pane:
        try:
            proc = await asyncio.create_subprocess_exec(
                *get_tmux_prefix_for_context(terminal_ctx),
                "kill-pane",
                "-t",
                str(tmux_pane),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            if proc.returncode == 0:
                logger.info(
                    f"Killed terminal session {session_id[:8]} via tmux pane {tmux_pane}",
                )
                return True
            else:
                stderr_text = stderr.decode().strip() if stderr else "unknown"
                if (
                    "can't find pane" in stderr_text.lower()
                    or "can't find window" in stderr_text.lower()
                ):
                    logger.debug(
                        "tmux pane %s already gone for session %s",
                        tmux_pane,
                        session_id[:8],
                    )
                    return True
                logger.debug(
                    f"tmux kill-pane failed for {tmux_pane}: {stderr_text}",
                )
        except TimeoutError:
            logger.warning(f"tmux kill-pane timed out for pane {tmux_pane}")
        except FileNotFoundError:
            logger.debug("tmux not available, skipping pane kill")
        except Exception as e:
            logger.warning(f"tmux kill-pane error for {tmux_pane}: {e}")

    # 2. Fallback: PID-based kill
    parent_pid = terminal_ctx.get("parent_pid")
    if parent_pid:
        try:
            pid = int(parent_pid)
            os.kill(pid, signal.SIGTERM)
            logger.info(
                f"Killed terminal session {session_id[:8]} via SIGTERM to PID {pid}",
            )
            return True
        except ProcessLookupError:
            logger.debug(f"PID {parent_pid} already dead for session {session_id[:8]}")
        except (ValueError, OSError) as e:
            logger.warning(f"PID kill failed for session {session_id[:8]}: {e}")

    logger.debug(
        f"No kill method available for session {session_id[:8]} (no tmux_pane or parent_pid)",
    )
    return False
