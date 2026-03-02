"""Session lifecycle workflow actions.

Extracted from actions.py as part of strangler fig decomposition.
These functions handle session status updates, mode switching, and session chaining.
"""

import logging
import shlex
import subprocess  # nosec B404 - subprocess needed for session spawning
from typing import Any

logger = logging.getLogger(__name__)


def start_new_session(
    session_manager: Any,
    session_id: str,
    command: str | None = None,
    args: list[str] | str | None = None,
    prompt: str | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Start a new CLI session (chaining).

    Args:
        session_manager: The session manager instance
        session_id: Current session ID
        command: CLI command to run (default: auto-detect from source)
        args: List of arguments or string to split
        prompt: Initial prompt/context to inject
        cwd: Working directory (default: current session's cwd)

    Returns:
        Dict with started_new_session, pid, and command, or error
    """
    session = session_manager.get(session_id)
    if not session:
        return {"error": "Session not found"}

    # Determine command
    if not command:
        source = getattr(session, "source", "claude")
        if source == "claude":
            command = "claude"
        elif source == "antigravity":
            command = "claude"  # Antigravity uses Claude Code
        elif source == "gemini":
            command = "gemini"
        else:
            command = "claude"  # Default fallthrough

    # Parse args
    cmd_args: list[str] = []
    if args:
        if isinstance(args, str):
            cmd_args = shlex.split(args)
        else:
            cmd_args = list(args)

    # Determine working directory
    if not cwd:
        cwd = getattr(session, "project_path", None) or "."

    logger.info(f"Starting new session: {command} {cmd_args} in {cwd}")

    try:
        full_cmd = [command] + cmd_args

        # Inject prompt via -p flag for Claude/Gemini if supported
        if prompt and command in ["claude", "gemini"]:
            full_cmd.extend(["-p", prompt])

        proc = subprocess.Popen(  # nosec B603 - cmd built from config, no shell
            full_cmd,
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # Detach
        )

        logger.info(f"Spawned process {proc.pid}")
        return {"started_new_session": True, "pid": proc.pid, "command": str(full_cmd)}

    except Exception as e:
        logger.error(f"Failed to start new session: {e}", exc_info=True)
        return {"error": str(e)}


def mark_session_status(
    session_manager: Any,
    session_id: str,
    status: str | None = None,
    target: str = "current_session",
) -> dict[str, Any]:
    """Mark a session status (current or parent).

    Args:
        session_manager: The session manager instance
        session_id: Current session ID
        status: New status to set
        target: "current_session" or "parent_session"

    Returns:
        Dict with status_updated, session_id, and status, or error
    """
    if not status:
        return {"error": "Missing status"}

    target_session_id = session_id
    if target == "parent_session":
        current_session = session_manager.get(session_id)
        if current_session and current_session.parent_session_id:
            target_session_id = current_session.parent_session_id
        else:
            return {"error": "No parent session linked"}

    session_manager.update_status(target_session_id, status)
    return {"status_updated": True, "session_id": target_session_id, "status": status}


def switch_mode(mode: str | None = None) -> dict[str, Any]:
    """Signal the agent to switch modes (e.g., PLAN, ACT).

    Args:
        mode: The mode to switch to

    Returns:
        Dict with inject_context and mode_switch, or error
    """
    if not mode:
        return {"error": "Missing mode"}

    message = (
        f"SYSTEM: SWITCH MODE TO {mode.upper()}\n"
        f"You are now in {mode.upper()} mode. Adjust your behavior accordingly."
    )

    return {"inject_context": message, "mode_switch": mode}


# --- ActionHandler-compatible wrappers ---
# These match the ActionHandler protocol: (context: ActionContext, **kwargs) -> dict | None


async def handle_start_new_session(context: Any, **kwargs: Any) -> dict[str, Any] | None:
    """ActionHandler wrapper for start_new_session."""
    import asyncio

    return await asyncio.to_thread(
        start_new_session,
        session_manager=context.session_manager,
        session_id=context.session_id,
        command=kwargs.get("command"),
        args=kwargs.get("args"),
        prompt=kwargs.get("prompt"),
        cwd=kwargs.get("cwd"),
    )


async def handle_mark_session_status(context: Any, **kwargs: Any) -> dict[str, Any] | None:
    """ActionHandler wrapper for mark_session_status."""
    return mark_session_status(
        session_manager=context.session_manager,
        session_id=context.session_id,
        status=kwargs.get("status"),
        target=kwargs.get("target", "current_session"),
    )


async def handle_switch_mode(context: Any, **kwargs: Any) -> dict[str, Any] | None:
    """ActionHandler wrapper for switch_mode."""
    return switch_mode(kwargs.get("mode"))
