"""Standalone agent kill logic.

Works entirely from DB records. No in-memory registry dependency.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import sys
from collections.abc import Awaitable, Callable
from typing import Any

import psutil

from gobby.storage.agents import AgentRun
from gobby.storage.hub.protocol import HubDatabase
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


def _signal_process_group(pid: int, sig: int) -> None:
    if sys.platform == "win32":
        os.kill(pid, sig)
        return
    os.killpg(os.getpgid(pid), sig)


def _configured_tmux_command_prefix() -> list[str]:
    from gobby.agents.tmux import get_configured_tmux_command_prefix

    return get_configured_tmux_command_prefix()


async def _wait_for_pid_exit(pid: int, timeout: float) -> bool:
    if timeout <= 0:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        return False

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        await asyncio.sleep(min(0.1, max(0.0, deadline - loop.time())))
    return False


async def pid_matches_agent_identity(
    pid: int,
    *,
    provider: str,
    session_id: str | None,
    run_subprocess: Callable[..., Awaitable[tuple[int, str, str]]] | None = None,
    unverifiable_result: bool = False,
) -> bool:
    """Verify a recorded PID still belongs to the expected provider/session.

    unverifiable_result is returned when identity cannot be determined at all
    (the ps lookup itself fails, e.g. times out under load). Signal/kill paths
    keep the False default (never signal an unverified PID); liveness checks
    must pass True so a failed lookup is not mistaken for a dead agent.
    """
    if not session_id or not _validate_terminal_value("session_id", session_id):
        logger.warning("Refusing to signal PID %s: missing or invalid session id", pid)
        return False

    provider_marker = provider.strip().lower()
    if not provider_marker:
        logger.warning("Refusing to signal PID %s: missing provider", pid)
        return False

    runner = run_subprocess or _run_subprocess
    try:
        rc, stdout, _ = await runner("ps", "-p", str(pid), "-o", "args=", timeout=2.0)
    except Exception as e:
        logger.warning(
            "PID %s identity unverifiable (cmdline lookup failed: %s); treating as %s",
            pid,
            e or type(e).__name__,
            "alive" if unverifiable_result else "unsafe to signal",
        )
        return unverifiable_result

    cmdline = stdout.strip()
    if rc != 0 or provider_marker not in cmdline.lower():
        logger.warning(
            "Refusing to signal PID %s: cmdline does not match provider identity",
            pid,
        )
        return False
    if f"session-id {session_id}" in cmdline:
        return True
    # Providers like codex carry no session marker in argv; the spawn-time
    # GOBBY_SESSION_ID environment variable is the identity there.
    env_match = await _process_env_matches_session(pid, session_id)
    if env_match:
        return True
    if env_match is None:
        logger.warning(
            "PID %s identity unverifiable (environment unreadable); treating as %s",
            pid,
            "alive" if unverifiable_result else "unsafe to signal",
        )
        return unverifiable_result
    logger.warning(
        "Refusing to signal PID %s: cmdline does not match provider/session identity",
        pid,
    )
    return False


async def _process_env_matches_session(pid: int, session_id: str) -> bool | None:
    """Check the process environment for GOBBY_SESSION_ID; None means unreadable."""

    def _read_env() -> bool | None:
        try:
            env = psutil.Process(pid).environ()
        except psutil.NoSuchProcess:
            return False
        except (psutil.Error, OSError):
            return None
        return bool(env.get("GOBBY_SESSION_ID") == session_id)

    return await asyncio.to_thread(_read_env)


async def _close_terminal_window(
    session_id: str,
    db: HubDatabase,
    session_manager: SessionManager | None = None,
    signal_name: str = "TERM",
    timeout: float = 5.0,
    provider: str | None = None,
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
            rc, stdout, _ = await _run_subprocess(
                *_configured_tmux_command_prefix(),
                "display-message",
                "-t",
                ctx["tmux_pane"],
                "-p",
                "#{pane_id}",
                timeout=timeout,
            )
            if rc == 0 and stdout.strip():
                await _run_subprocess(
                    *_configured_tmux_command_prefix(),
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
                if provider and not await pid_matches_agent_identity(
                    int(parent_pid),
                    provider=provider,
                    session_id=session_id,
                ):
                    return {
                        "success": False,
                        "error": f"PID {parent_pid} does not match agent identity",
                        "pid": parent_pid,
                        "method": "taskkill_tree",
                    }
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
            if provider and not await pid_matches_agent_identity(
                pid,
                provider=provider,
                session_id=session_id,
            ):
                return {
                    "success": False,
                    "error": f"PID {pid} does not match agent identity",
                    "pid": pid,
                    "method": "parent_pid",
                }
            if is_windows:
                await _run_subprocess(
                    "taskkill",
                    "/F",
                    "/PID",
                    str(pid),
                    timeout=timeout,
                )
            else:
                _signal_process_group(pid, signal.SIGTERM)
            return {"success": True, "method": "parent_pid", "pid": pid}
        except (ProcessLookupError, OSError, ValueError) as e:
            logger.debug(f"parent_pid kill failed: {e}")

    return {"success": False, "error": "No terminal close method available"}


async def _close_tmux_session(session_name: str, *, timeout: float = 5.0) -> dict[str, Any]:
    """Close a persisted Gobby tmux session and its process groups."""
    try:
        from gobby.agents.tmux import get_tmux_session_manager

        tmux = get_tmux_session_manager()
        already_missing = not await tmux.has_session(session_name)
        killed = await tmux.kill_session(session_name, missing_ok=True, timeout=timeout)
    except Exception as e:
        logger.debug("tmux session close failed for %s: %s", session_name, e)
        return {"success": False, "error": str(e)}

    if not killed:
        return {"success": False, "error": f"failed to kill tmux session '{session_name}'"}
    if already_missing:
        return {
            "success": True,
            "message": f"tmux session '{session_name}' already dead",
            "already_dead": True,
            "method": "tmux_already_dead",
            "tmux_session_name": session_name,
        }
    return {"success": True, "method": "tmux_kill_session", "tmux_session_name": session_name}


async def kill_agent(
    run: AgentRun,
    db: HubDatabase,
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
    session_id = run.child_session_id
    session_manager = SessionManager(db) if session_id else None
    terminal_close_result: dict[str, Any] | None = None

    # Try terminal-specific close
    if close_terminal and run.tmux_session_name:
        result = await _close_tmux_session(run.tmux_session_name, timeout=timeout)
        if result.get("success"):
            terminal_close_result = result

    if close_terminal and session_id and terminal_close_result is None:
        result = await _close_terminal_window(
            session_id,
            db,
            session_manager=session_manager,
            signal_name=signal_name,
            timeout=timeout,
            provider=run.provider,
        )
        if result.get("success"):
            terminal_close_result = result

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
                                    is_matched = await pid_matches_agent_identity(
                                        candidate_pid,
                                        provider=run.provider,
                                        session_id=session_id,
                                    )
                                    if is_matched:
                                        if matched_pid is not None:
                                            logger.info(
                                                f"Multiple PID matches ({matched_pid}, "
                                                f"{candidate_pid}) - picking highest"
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
        if terminal_close_result is not None and terminal_close_result.get("already_dead"):
            return {
                "success": True,
                "message": "Terminal already dead and no target PID was found",
                "already_dead": True,
                "terminal_close": terminal_close_result,
                "method": terminal_close_result.get("method"),
            }
        if terminal_close_result is not None:
            return {
                "success": False,
                "error": "Terminal closed but no target PID was found to verify process death",
                "terminal_close": terminal_close_result,
            }
        return {"success": False, "error": "No target PID found"}

    # Check if process is alive
    try:
        os.kill(target_pid, 0)
    except ProcessLookupError:
        response = {
            "success": True,
            "message": f"Process {target_pid} already dead",
            "already_dead": True,
        }
        if terminal_close_result is not None:
            response["terminal_close"] = terminal_close_result
            response["method"] = terminal_close_result.get("method")
        return response
    except PermissionError:
        return {"success": False, "error": f"No permission to signal PID {target_pid}"}

    if not await pid_matches_agent_identity(
        target_pid,
        provider=run.provider,
        session_id=session_id,
    ):
        return {
            "success": False,
            "error": f"PID {target_pid} does not match agent identity",
            "pid": target_pid,
            "found_via": found_via,
        }

    # Close PTY if embedded mode
    if master_fd is not None:
        try:
            os.close(master_fd)
        except OSError:
            pass

    # Send signal, unless terminal close already requested termination.
    sig = getattr(signal, f"SIG{signal_name}", signal.SIGTERM)
    if terminal_close_result is None or signal_name != "TERM":
        try:
            _signal_process_group(target_pid, sig)
        except ProcessLookupError:
            response = {
                "success": True,
                "message": "Process died during signal",
                "already_dead": True,
            }
            if terminal_close_result is not None:
                response["terminal_close"] = terminal_close_result
                response["method"] = terminal_close_result.get("method")
            return response

    # Wait for termination with optional SIGKILL escalation
    if signal_name == "TERM":
        should_escalate = terminal_close_result is not None and timeout <= 0
        if timeout > 0:
            should_escalate = not await _wait_for_pid_exit(target_pid, timeout)
        if should_escalate:
            try:
                if not await pid_matches_agent_identity(
                    target_pid,
                    provider=run.provider,
                    session_id=session_id,
                ):
                    return {
                        "success": False,
                        "error": f"PID {target_pid} no longer matches agent identity",
                        "pid": target_pid,
                        "found_via": found_via,
                    }
                _signal_process_group(target_pid, signal.SIGKILL)
                logger.info(f"Escalated to SIGKILL for PID {target_pid}")
                if not await _wait_for_pid_exit(target_pid, max(0.5, min(timeout, 1.0))):
                    return {
                        "success": False,
                        "error": f"PID {target_pid} still alive after SIGKILL",
                        "pid": target_pid,
                        "found_via": found_via,
                    }
            except ProcessLookupError:
                pass

    response = {
        "success": True,
        "message": f"Sent SIG{signal_name} to PID {target_pid}",
        "pid": target_pid,
        "signal": signal_name,
        "found_via": found_via,
    }
    if terminal_close_result is not None:
        response["terminal_close"] = terminal_close_result
        response["method"] = terminal_close_result.get("method")
    return response
