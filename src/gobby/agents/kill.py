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
from collections.abc import Callable
from typing import Any

import psutil

from gobby.agents.capture import KillOutcome
from gobby.storage.agents import AgentRun, LocalAgentRunManager, TerminalAction
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)

KILL_ERROR_NO_TARGET_PID = "no_target_pid"

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
    process_factory: Callable[[int], psutil.Process] | None = None,
    unverifiable_result: bool = False,
) -> bool:
    """Verify a recorded PID still belongs to the expected provider/session.

    unverifiable_result is returned when identity cannot be determined at all.
    Signal/kill paths keep the False default (never signal an unverified PID);
    liveness checks must pass True so a failed lookup is not mistaken for a
    dead agent.
    """
    if not session_id or not _validate_terminal_value("session_id", session_id):
        logger.warning("Refusing to signal PID %s: missing or invalid session id", pid)
        return False

    provider_marker = provider.strip().lower()
    if not provider_marker:
        logger.warning("Refusing to signal PID %s: missing provider", pid)
        return False

    matches, failure_stage, error = await asyncio.to_thread(
        _inspect_process_identity,
        pid,
        provider_marker,
        session_id,
        process_factory or psutil.Process,
    )
    if matches is None:
        assert failure_stage is not None
        assert error is not None
        error_name = type(error).__name__
        error_message = str(error).strip()
        error_details = f"{error_name}: {error_message}" if error_message else error_name
        log = logger.debug if unverifiable_result else logger.warning
        log(
            "PID %s identity unverifiable (%s inspection failed: %s); treating as %s",
            pid,
            failure_stage,
            error_details,
            "alive" if unverifiable_result else "unsafe to signal",
        )
        return unverifiable_result
    if not matches:
        if unverifiable_result:
            logger.debug(
                "PID %s no longer matches provider identity during liveness check",
                pid,
            )
        else:
            logger.warning(
                "Refusing to signal PID %s: cmdline does not match provider identity",
                pid,
            )
        return False
    return True


def _inspect_process_identity(
    pid: int,
    provider_marker: str,
    session_id: str,
    process_factory: Callable[[int], psutil.Process],
) -> tuple[bool | None, str | None, Exception | None]:
    """Inspect process identity synchronously; None means inspection failed."""
    try:
        process = process_factory(pid)
    except psutil.NoSuchProcess:
        return False, None, None
    except Exception as error:
        return None, "process", error

    try:
        cmdline = " ".join(process.cmdline())
    except psutil.NoSuchProcess:
        return False, None, None
    except Exception as error:
        return None, "cmdline", error

    if provider_marker not in cmdline.lower():
        return False, None, None
    if f"session-id {session_id}" in cmdline:
        return True, None, None

    # Providers like codex carry no session marker in argv; the spawn-time
    # GOBBY_SESSION_ID environment variable is the identity there.
    try:
        environment = process.environ()
    except psutil.NoSuchProcess:
        return False, None, None
    except Exception as error:
        return None, "environment", error
    return environment.get("GOBBY_SESSION_ID") == session_id, None, None


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
        logger.debug("Failed to get terminal context: %s", e)

    # Validate terminal context values
    for _key in ("tmux_pane", "parent_pid"):
        _val = ctx.get(_key)
        if _val is not None and not _validate_terminal_value(_key, str(_val)):
            logger.warning("Invalid %s format: %r, ignoring", _key, _val)
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
                logger.debug("tmux pane %s not found, skipping", ctx["tmux_pane"])
        except Exception as e:
            logger.debug("tmux kill-pane failed: %s", e)

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
                logger.debug("taskkill failed: %s", e)

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
            logger.debug("parent_pid kill failed: %s", e)

    return {"success": False, "error": "No terminal close method available"}


async def _close_tmux_session(
    run: AgentRun,
    db: HubDatabase,
    *,
    terminal_action: TerminalAction,
    terminal_reason: str | None,
    timeout: float = 5.0,
    terminal_services: Any | None = None,
) -> dict[str, Any]:
    """Close a Gobby-owned terminal through TerminalRuntime."""
    from gobby.agents.capture import terminate_managed_runtime_async
    from gobby.terminals.lookup import active_terminal_for_run

    services = terminal_services
    terminal = None if services is None else active_terminal_for_run(services.manager, run)
    if services is None or terminal is None:
        return {"success": False, "error": "agent run has no terminal"}
    try:
        result = await terminate_managed_runtime_async(
            storage=LocalAgentRunManager(db),
            run=run,
            terminal=terminal,
            runtime=services.runtime_for(terminal),
            action=terminal_action,
            reason=terminal_reason,
            lock_timeout=timeout,
        )
    except Exception as e:
        logger.debug("terminal close failed for %s: %s", terminal.id, e)
        return {"success": False, "error": str(e)}

    if not result.success:
        return {
            "success": False,
            "error": result.error or f"failed to terminate terminal '{terminal.id}'",
            "error_code": result.error_code,
        }
    if result.kill_outcome == KillOutcome.ALREADY_ABSENT:
        return {
            "success": True,
            "message": f"terminal '{terminal.id}' already dead",
            "already_dead": True,
            "method": "runtime_already_dead",
            "terminal_id": terminal.id,
        }
    return {"success": True, "method": "runtime_terminate", "terminal_id": terminal.id}


async def kill_agent(
    run: AgentRun,
    db: HubDatabase,
    *,
    master_fd: int | None = None,
    signal_name: str = "TERM",
    timeout: float = 5.0,
    close_terminal: bool = False,
    terminal_action: TerminalAction = "cancel",
    terminal_reason: str | None = "user_cancelled",
    terminal_services: Any | None = None,
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
    if close_terminal and run.terminal_id:
        result = await _close_tmux_session(
            run,
            db,
            terminal_action=terminal_action,
            terminal_reason=terminal_reason,
            timeout=timeout,
            terminal_services=terminal_services,
        )
        if result.get("success"):
            response = {
                "success": True,
                "message": result.get(
                    "message",
                    f"Closed managed terminal '{run.terminal_id}'",
                ),
                "terminal_close": result,
                "method": result.get("method"),
            }
            if result.get("already_dead"):
                response["already_dead"] = True
            return response

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
                    logger.info("Found PID from session terminal_context: %s", target_pid)
        except Exception as e:
            logger.debug("terminal_context lookup failed: %s", e)

        # Strategy 2: pgrep fallback
        if not target_pid:
            session_id_pattern = re.compile(r"^[a-zA-Z0-9_-]+$")
            if not session_id_pattern.match(session_id):
                logger.warning("Invalid session_id format, skipping pgrep: %s", session_id)
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
                            logger.info("Found PID via pgrep: %s", target_pid)
                        else:
                            logger.warning(
                                "pgrep returned %s PIDs for session %s: %s",
                                len(pids),
                                session_id,
                                pids,
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
                                                "Multiple PID matches (%s, %s) - picking highest",
                                                matched_pid,
                                                candidate_pid,
                                            )
                                            matched_pid = max(matched_pid, candidate_pid)
                                        else:
                                            matched_pid = candidate_pid
                                except (ValueError, TimeoutError):
                                    continue
                            if matched_pid is not None:
                                target_pid = matched_pid
                                found_via = "pgrep_disambiguated"
                                logger.info("Disambiguated PID: %s", target_pid)
                            else:
                                logger.error(
                                    "Could not disambiguate PIDs for session %s: %s",
                                    session_id,
                                    pids,
                                )
                except Exception as e:
                    logger.warning("pgrep fallback failed: %s", e)

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
        return {
            "success": False,
            "error": "No target PID found",
            "error_code": KILL_ERROR_NO_TARGET_PID,
        }

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
                logger.info("Escalated to SIGKILL for PID %s", target_pid)
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
