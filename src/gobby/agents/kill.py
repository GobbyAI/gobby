"""Standalone agent kill logic.

Extracted from RunningAgentRegistry — works entirely from DB records.
No in-memory registry dependency.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import sys
from typing import Any

from gobby.storage.agents import AgentRun
from gobby.storage.database import DatabaseProtocol
from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)

# Validation patterns for terminal context values passed to subprocess calls
_TERMINAL_CTX_PATTERNS: dict[str, re.Pattern[str]] = {
    "tmux_pane": re.compile(r"^%\d+$"),
    "parent_pid": re.compile(r"^\d+$"),
    "session_id": re.compile(r"^[a-zA-Z0-9_\-]+$"),
}


def _validate_terminal_value(key: str, value: str) -> bool:
    """Validate a terminal context value against its expected format."""
    pattern = _TERMINAL_CTX_PATTERNS.get(key)
    if pattern is None:
        return False
    return pattern.fullmatch(str(value)) is not None


async def _run_subprocess(*args: str, timeout: float = 5.0) -> tuple[int, str, str]:
    """Run a subprocess asynchronously with timeout.

    Returns:
        Tuple of (returncode, stdout, stderr).
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return (
        proc.returncode or 0,
        stdout_bytes.decode() if stdout_bytes else "",
        stderr_bytes.decode() if stderr_bytes else "",
    )


async def _close_terminal_window(
    session_id: str,
    db: DatabaseProtocol,
    session_manager: SessionManager | None = None,
    signal_name: str = "TERM",
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Close the terminal window/pane for an agent session.

    Uses the session's terminal_context to find tmux pane or parent PID.
    """
    is_windows = sys.platform == "win32"

    ctx: dict[str, Any] = {}
    try:
        session_mgr = session_manager or SessionManager(db)
        session = session_mgr.get(session_id)
        if session and session.terminal_context:
            ctx = session.terminal_context
    except Exception as e:
        logger.debug(f"Failed to get terminal context: {e}")

    # Validate terminal context values
    for _key in ("tmux_pane", "parent_pid"):
        _val = ctx.get(_key)
        if _val is not None and not _validate_terminal_value(_key, str(_val)):
            logger.warning(f"Invalid {_key} format: {_val!r}, ignoring")
            ctx.pop(_key, None)

    # Strategy 1: tmux kill-pane (primary — all agents use tmux)
    if ctx.get("tmux_pane"):
        try:
            from gobby.config.tmux import TmuxConfig

            tmux_socket = TmuxConfig().socket_name or "gobby"

            rc, stdout, _ = await _run_subprocess(
                "tmux",
                "-L",
                tmux_socket,
                "display-message",
                "-t",
                ctx["tmux_pane"],
                "-p",
                "#{pane_id}",
                timeout=timeout,
            )
            if rc == 0 and stdout.strip():
                await _run_subprocess(
                    "tmux",
                    "-L",
                    tmux_socket,
                    "kill-pane",
                    "-t",
                    ctx["tmux_pane"],
                    timeout=timeout,
                )
                return {"success": True, "method": "tmux_kill_pane", "pane": ctx["tmux_pane"]}
            else:
                logger.debug(f"tmux pane {ctx['tmux_pane']} not found, skipping")
        except Exception as e:
            logger.debug(f"tmux kill-pane failed: {e}")

    # Strategy 2: Windows taskkill
    if is_windows:
        parent_pid = ctx.get("parent_pid")
        if parent_pid:
            try:
                await _run_subprocess(
                    "taskkill",
                    "/F",
                    "/T",
                    "/PID",
                    str(parent_pid),
                    timeout=timeout,
                )
                return {"success": True, "method": "taskkill_tree", "pid": parent_pid}
            except Exception as e:
                logger.debug(f"taskkill failed: {e}")

    # Strategy 3: Kill parent_pid directly (fallback)
    parent_pid = ctx.get("parent_pid")
    if parent_pid:
        try:
            pid = int(parent_pid)
            if is_windows:
                await _run_subprocess(
                    "taskkill",
                    "/F",
                    "/PID",
                    str(pid),
                    timeout=timeout,
                )
            else:
                os.kill(pid, signal.SIGTERM)
            return {"success": True, "method": "parent_pid", "pid": pid}
        except (ProcessLookupError, OSError, ValueError) as e:
            logger.debug(f"parent_pid kill failed: {e}")

    return {"success": False, "error": "No terminal close method available"}


async def kill_agent(
    run: AgentRun,
    db: DatabaseProtocol,
    *,
    master_fd: int | None = None,
    signal_name: str = "TERM",
    timeout: float = 5.0,
    close_terminal: bool = False,
) -> dict[str, Any]:
    """Kill an agent process using DB records.

    Works entirely from the AgentRun DB model. All agents run via tmux.

    Args:
        run: Agent run DB record.
        db: Database connection.
        master_fd: Optional PTY file descriptor to close.
        signal_name: Signal without SIG prefix (TERM, KILL).
        timeout: Seconds before escalating TERM → KILL.
        close_terminal: Close the terminal window/pane instead of just the process.

    Returns:
        Dict with success status and details.
    """
    session_id = run.child_session_id or run.parent_session_id
    session_manager = SessionManager(db) if session_id else None

    # Try terminal-specific close
    if close_terminal and session_id:
        result = await _close_terminal_window(
            session_id,
            db,
            session_manager=session_manager,
            signal_name=signal_name,
            timeout=timeout,
        )
        if result.get("success"):
            return result

    # Find PID via multiple strategies
    target_pid = run.pid
    found_via = "db"

    if session_id and not target_pid:
        # Strategy 1: Check session's terminal_context
        try:
            session = session_manager.get(session_id) if session_manager else None
            if session and session.terminal_context:
                ctx_pid = session.terminal_context.get("parent_pid")
                if ctx_pid:
                    target_pid = int(ctx_pid)
                    found_via = "terminal_context"
                    logger.info(f"Found PID from session terminal_context: {target_pid}")
        except Exception as e:
            logger.debug(f"terminal_context lookup failed: {e}")

        # Strategy 2: pgrep fallback
        if not target_pid:
            session_id_pattern = re.compile(r"^[a-zA-Z0-9_-]+$")
            if not session_id_pattern.match(session_id):
                logger.warning(f"Invalid session_id format, skipping pgrep: {session_id}")
            else:
                try:
                    rc, stdout, _ = await _run_subprocess(
                        "pgrep",
                        "-f",
                        "--",
                        f"session-id {session_id}",
                        timeout=5.0,
                    )
                    if rc == 0 and stdout.strip():
                        pids = stdout.strip().split("\n")
                        if len(pids) == 1:
                            target_pid = int(pids[0])
                            found_via = "pgrep"
                            logger.info(f"Found PID via pgrep: {target_pid}")
                        else:
                            logger.warning(
                                f"pgrep returned {len(pids)} PIDs for session {session_id}: {pids}"
                            )
                            matched_pid = None
                            for pid_str in pids:
                                try:
                                    candidate_pid = int(pid_str)
                                    ps_rc, ps_stdout, _ = await _run_subprocess(
                                        "ps",
                                        "-p",
                                        str(candidate_pid),
                                        "-o",
                                        "args=",
                                        timeout=2.0,
                                    )
                                    if ps_rc == 0:
                                        cmdline = ps_stdout.strip()
                                        is_matched = run.provider in cmdline.lower()

                                        if f"session-id {session_id}" in cmdline and is_matched:
                                            if matched_pid is not None:
                                                logger.info(
                                                    f"Multiple PID matches ({matched_pid}, "
                                                    f"{candidate_pid}) — picking highest"
                                                )
                                                matched_pid = max(matched_pid, candidate_pid)
                                            else:
                                                matched_pid = candidate_pid
                                except (ValueError, TimeoutError):
                                    continue
                            if matched_pid is not None:
                                target_pid = matched_pid
                                found_via = "pgrep_disambiguated"
                                logger.info(f"Disambiguated PID: {target_pid}")
                            else:
                                logger.error(
                                    f"Could not disambiguate PIDs for session {session_id}: {pids}"
                                )
                except Exception as e:
                    logger.warning(f"pgrep fallback failed: {e}")

    if not target_pid:
        return {"success": False, "error": "No target PID found"}

    # Check if process is alive
    try:
        os.kill(target_pid, 0)
    except ProcessLookupError:
        return {
            "success": True,
            "message": f"Process {target_pid} already dead",
            "already_dead": True,
        }
    except PermissionError:
        return {"success": False, "error": f"No permission to signal PID {target_pid}"}

    # Close PTY if embedded mode
    if master_fd is not None:
        try:
            os.close(master_fd)
        except OSError:
            pass

    # Send signal
    sig = getattr(signal, f"SIG{signal_name}", signal.SIGTERM)
    try:
        os.kill(target_pid, sig)
    except ProcessLookupError:
        return {
            "success": True,
            "message": "Process died during signal",
            "already_dead": True,
        }

    # Wait for termination with optional SIGKILL escalation
    if signal_name == "TERM" and timeout > 0:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            try:
                os.kill(target_pid, 0)
                await asyncio.sleep(0.1)
            except ProcessLookupError:
                break
        else:
            try:
                os.kill(target_pid, signal.SIGKILL)
                logger.info(f"Escalated to SIGKILL for PID {target_pid}")
            except ProcessLookupError:
                pass

    return {
        "success": True,
        "message": f"Sent SIG{signal_name} to PID {target_pid}",
        "pid": target_pid,
        "signal": signal_name,
        "found_via": found_via,
    }
