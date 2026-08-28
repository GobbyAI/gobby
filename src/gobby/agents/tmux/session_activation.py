"""Tmux session activation.

The subprocess seam every tmux command goes through, session creation with
its secret-env staging, and the bounded client refresh. ``TmuxSessionManager``
delegates here and keeps the public surface.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from gobby.agents.spawners.auth_env import split_credential_env
from gobby.agents.tmux.errors import TmuxSessionError
from gobby.agents.tmux.wsl_compat import convert_windows_path_to_wsl, needs_wsl

if TYPE_CHECKING:
    from gobby.agents.tmux.session_manager import TmuxSessionManager

logger = logging.getLogger(__name__)

TMUX_COMMAND_TIMEOUT_SECONDS = 10.0
# One deadline for a whole refresh-client sweep: the lookup and every per-tty
# redraw it fans out share it, so the cost does not scale with attached clients.
REFRESH_CLIENT_TIMEOUT_SECONDS = 5.0


def exact_session_target(name: str) -> str:
    """Return a tmux target that requires an exact session-name match."""
    return f"={name}:"


def _write_secret_env_file(env: dict[str, str]) -> Path:
    """Write env vars that should not ride in tmux ``-e`` to a private shell file."""
    fd, tmp_path = tempfile.mkstemp(prefix="gobby-agent-env-", suffix=".sh")
    path = Path(tmp_path)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        for key, value in env.items():
            handle.write(f"{key}={shlex.quote(value)}\n")
    path.chmod(0o600)
    return path


def _requires_tmux_env_file(value: str) -> bool:
    return ";" in value or value.endswith("\\")


def _split_tmux_env(env: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Split env into values safe for tmux ``-e`` and values requiring shell sourcing."""
    public_env, file_env = split_credential_env(env)
    for key, value in list(public_env.items()):
        if _requires_tmux_env_file(value):
            file_env[key] = public_env.pop(key)
    return public_env, file_env


def _source_secret_env_command(command: str | None, env_file: str) -> str:
    env_file_arg = shlex.quote(env_file)
    command_text = command or 'exec "${SHELL:-/bin/sh}"'
    return (
        f"__gobby_env_file={env_file_arg}; "
        'set -a; . "$__gobby_env_file"; __gobby_env_status=$?; set +a; '
        'rm -f "$__gobby_env_file"; unset __gobby_env_file; '
        'if [ "$__gobby_env_status" -ne 0 ]; then exit "$__gobby_env_status"; fi; '
        "unset __gobby_env_status; "
        f"{command_text}"
    )


async def run_tmux_command(
    cmd: list[str],
    *,
    timeout: float = TMUX_COMMAND_TIMEOUT_SECONDS,
    socket_name: str = "",
    socket_path: str | None = None,
) -> tuple[int, str, str]:
    """Run a full tmux command line and return (returncode, stdout, stderr).

    The spawn runs in a worker thread rather than through
    ``asyncio.create_subprocess_exec``, whose ``Popen.__init__`` forks
    inline on the event loop. With the daemon around a gigabyte resident
    that fork is expensive, and it is on the hot path: the pane monitor
    polls tmux continuously and the window-name repair loop spawns per
    session, so a stack sampler caught this reaching
    ``Popen._execute_child`` on the loop thread during multi-second
    stalls (#20841).
    """
    try:
        completed = await asyncio.to_thread(
            subprocess.run,
            cmd,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        # subprocess.run has already killed and reaped the child. Callers
        # branch on TimeoutError, so keep that contract.
        logger.debug(
            "Tmux command timed out (timeout=%ss, command=%r, socket_name=%r, socket_path=%r)",
            timeout,
            cmd,
            socket_name,
            socket_path,
        )
        raise TimeoutError(f"tmux command timed out after {timeout}s") from exc
    return (
        completed.returncode,
        (completed.stdout or b"").decode(),
        (completed.stderr or b"").decode(),
    )


async def refresh_session_clients(
    manager: TmuxSessionManager,
    session_name: str,
    *,
    timeout: float = REFRESH_CLIENT_TIMEOUT_SECONDS,
) -> None:
    """Redraw every client attached to a tmux session, within ``timeout``.

    refresh-client targets a client tty, not a session, so the session's
    clients are resolved first. A session with no attached clients is a
    no-op; a client that detaches between the two commands is ignored.

    The per-tty refreshes are independent, so they fan out concurrently and
    share one deadline with the lookup. Running them serially made the
    worst case scale with the number of attached clients, which is charged
    to whichever caller is waiting -- on the attach path, to a user whose
    next request is queued behind it.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    rc, stdout, stderr = await manager._run(
        "list-clients", "-t", session_name, "-F", "#{client_tty}", timeout=timeout
    )
    if rc != 0:
        raise RuntimeError(f"tmux list-clients failed for '{session_name}': {stderr.strip()}")

    ttys = [tty for tty in (line.strip() for line in stdout.splitlines()) if tty]
    if not ttys:
        return

    remaining = max(deadline - loop.time(), 0.0)
    outcomes = await asyncio.gather(
        *(manager._run("refresh-client", "-t", tty, timeout=remaining) for tty in ttys),
        return_exceptions=True,
    )
    for tty, outcome in zip(ttys, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            if isinstance(outcome, asyncio.CancelledError):
                raise outcome
            logger.debug("tmux refresh-client errored for tty %s: %s", tty, outcome)
            continue
        refresh_rc, _stdout, refresh_stderr = outcome
        if refresh_rc != 0:
            logger.debug("tmux refresh-client failed for tty %s: %s", tty, refresh_stderr.strip())


async def activate_session(
    manager: TmuxSessionManager,
    name: str,
    command: str | list[str] | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[str, int | None]:
    """Create a detached tmux session and return ``(safe_name, pane_pid)``.

    Raises:
        TmuxSessionError: If the session exists already or creation fails.
    """
    manager.require_available()
    config = manager.config

    if needs_wsl() and cwd:
        cwd = convert_windows_path_to_wsl(cwd)

    # Sanitise name (tmux dislikes dots and colons)
    safe_name = "".join(c if c.isalnum() or c in "-_" else "-" for c in name)

    # Fail fast if session already exists — never silently reuse
    if await manager.has_session(safe_name):
        raise TmuxSessionError(
            f"Session '{safe_name}' already exists on socket '{config.socket_name}'",
            session_name=safe_name,
        )

    args: list[str] = [
        "new-session",
        "-d",
        "-s",
        safe_name,
        "-n",
        safe_name,
        "-x",
        "200",
        "-y",
        "50",
    ]

    if cwd:
        args.extend(["-c", cwd])

    # Set history limit
    args.extend(["-e", f"HISTSIZE={config.history_limit}"])

    secret_env_file: Path | None = None
    secret_env_file_arg: str | None = None

    # Inject env vars via -e (tmux 3.2+)
    if env:
        public_env, credential_env = _split_tmux_env(env)
        for key, val in public_env.items():
            args.extend(["-e", f"{key}={val}"])
        if credential_env:
            secret_env_file = _write_secret_env_file(credential_env)
            secret_env_file_arg = str(secret_env_file)
            if needs_wsl():
                secret_env_file_arg = convert_windows_path_to_wsl(secret_env_file_arg)

    # Append shell command
    command_text: str | None = None
    if command:
        command_text = shlex.join(command) if isinstance(command, list) else command
    if secret_env_file_arg:
        command_text = _source_secret_env_command(command_text, secret_env_file_arg)
    if command_text:
        args.append(command_text)

    target = exact_session_target(safe_name)
    # Chain set-option to disable destroy-unattached atomically
    args.extend([";", "set-option", "-t", target, "destroy-unattached", "off"])
    # Set scrollback history
    args.extend([";", "set-option", "-t", target, "history-limit", str(config.history_limit)])
    # Keep pane alive after process exits so capture-pane can retrieve output
    args.extend([";", "set-option", "-w", "-t", target, "remain-on-exit", "on"])

    try:
        rc, _stdout, stderr = await manager._run(*args)
    except Exception:
        if secret_env_file:
            secret_env_file.unlink(missing_ok=True)
        raise
    if rc != 0:
        if secret_env_file:
            secret_env_file.unlink(missing_ok=True)
        raise TmuxSessionError(
            f"Failed to create session (rc={rc}): {stderr.strip()}",
            session_name=safe_name,
        )

    pane_pid = await manager.get_pane_pid(safe_name)
    logger.info("Created tmux session '%s' (pane_pid=%s)", safe_name, pane_pid)
    return safe_name, pane_pid
