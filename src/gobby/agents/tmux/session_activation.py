"""The subprocess seam every tmux command goes through.

``TmuxSessionManager`` delegates here and keeps the public surface. Gobby
creates no tmux sessions (#22856); these commands reach hand-started panes.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess

from gobby.utils import spawn

logger = logging.getLogger(__name__)

TMUX_COMMAND_TIMEOUT_SECONDS = 10.0


def exact_session_target(name: str) -> str:
    """Return a tmux target that requires an exact session-name match."""
    return f"={name}:"


async def run_tmux_command(
    cmd: list[str],
    *,
    timeout: float = TMUX_COMMAND_TIMEOUT_SECONDS,
) -> tuple[int, str, str]:
    """Run a full tmux command line and return (returncode, stdout, stderr).

    This is on the hot path: the pane monitor polls tmux continuously and
    the window-name repair loop spawns per session. A stack sampler caught
    ``Popen._execute_child`` on the loop thread during multi-second stalls
    (#20841), and a fork holds the GIL even from a worker thread (#22815),
    so the command starts through posix_spawn and waits in a worker thread.
    """
    try:
        completed = await asyncio.to_thread(
            spawn.run,
            cmd,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        # subprocess.run has already killed and reaped the child. Callers
        # branch on TimeoutError, so keep that contract.
        logger.debug("Tmux command timed out (timeout=%ss, command=%r)", timeout, cmd)
        raise TimeoutError(f"tmux command timed out after {timeout}s") from exc
    return (
        completed.returncode,
        (completed.stdout or b"").decode(),
        (completed.stderr or b"").decode(),
    )
