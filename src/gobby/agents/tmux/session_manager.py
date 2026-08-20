"""Tmux session lifecycle management.

Creates, lists, kills, and queries tmux sessions on an isolated socket
(``-L gobby``) so Gobby never interferes with the user's personal tmux.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
import signal
import tempfile
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from gobby.agents.spawners.auth_env import split_credential_env
from gobby.agents.tmux.errors import TmuxNotFoundError, TmuxSessionError
from gobby.agents.tmux.text_injection import (
    TmuxTextInjectionError,
    send_literal_text_to_tmux_target,
)
from gobby.agents.tmux.wsl_compat import needs_wsl
from gobby.config.tmux import TmuxConfig

logger = logging.getLogger(__name__)


_MISSING_SESSION_ERRORS = ("can't find session", "no such session", "no server running")
_MISSING_TARGET_ERRORS = (
    *_MISSING_SESSION_ERRORS,
    "can't find pane",
    "no such pane",
    "can't find window",
    "no such window",
)
TMUX_COMMAND_TIMEOUT_SECONDS = 10.0
TMUX_HEALTH_CHECK_TIMEOUT_FAILURE_LIMIT = 3


class TmuxProbeState(StrEnum):
    """Observed state of the tmux server used for a target probe."""

    LIVE = "live"
    SERVER_MISSING = "server_missing"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class TmuxProbeResult:
    """Server liveness plus target presence from one tmux command."""

    state: TmuxProbeState
    pane_exists: bool | None
    detail: str = ""


class TmuxReleaseOutcome(StrEnum):
    """Outcome of releasing Gobby-owned tmux title state."""

    RELEASED = "released"
    ALREADY_RELEASED = "already_released"
    INDETERMINATE = "indeterminate"


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


def _is_missing_tmux_target_error(stderr: str) -> bool:
    """Return True for tmux errors that mean the target disappeared."""
    message = stderr.lower()
    return any(fragment in message for fragment in _MISSING_TARGET_ERRORS)


def _escape_tmux_format(value: str) -> str:
    """Escape tmux format markers in user-visible strings."""
    return value.replace("#", "##")


def _is_missing_tmux_server_error(stderr: str) -> bool:
    """Return True when tmux reports that the isolated server is not running."""
    message = stderr.strip().lower()
    return "no server running" in message or (
        message.startswith("error connecting to ") and "(no such file or directory)" in message
    )


def _is_tmux_permission_error(stderr: str) -> bool:
    message = stderr.strip().lower()
    return "permission denied" in message or "operation not permitted" in message


def _exact_session_target(name: str) -> str:
    """Return a tmux target that requires an exact session-name match."""
    return f"={name}:"


def _send_keys_target(target: str) -> str:
    """Return the tmux target form used for keystroke delivery."""
    if target.startswith("%"):
        return target
    return _exact_session_target(target)


@dataclass
class TmuxSessionInfo:
    """Metadata about a running tmux session."""

    name: str
    created_at: float = field(default_factory=time.time)
    pane_pid: int | None = None
    pane_id: str | None = None
    window_name: str | None = None
    pane_title: str | None = None
    pane_dead: bool = False
    pane_command: str | None = None
    pane_path: str | None = None


class TmuxSessionManager:
    """Manages tmux sessions on an isolated Gobby socket.

    All tmux commands use ``-L <socket_name>`` so that Gobby sessions
    are invisible to ``tmux ls`` in the user's default server.
    """

    def __init__(self, config: TmuxConfig | None = None) -> None:
        self._config = config or TmuxConfig()
        self._health_check_timeout_failures = 0

    @property
    def config(self) -> TmuxConfig:
        return self._config

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _base_args(self) -> list[str]:
        """Return the common tmux prefix args (binary + socket + config).

        On Windows the command is prefixed with ``wsl`` (and optionally
        ``-d <distro>``) so that tmux runs inside WSL.
        """
        from gobby.agents.tmux.wsl_compat import needs_wsl

        args: list[str] = []
        if needs_wsl():
            args.append("wsl")
            if self._config.wsl_distribution:
                args.extend(["-d", self._config.wsl_distribution])

        args.append(self._config.command)
        if self._config.socket_path:
            args.extend(["-S", self._config.socket_path])
        elif self._config.socket_name:
            args.extend(["-L", self._config.socket_name])
        # Always use explicit config to prevent user's ~/.tmux.conf from
        # interfering (e.g. 'destroy-unattached on' kills detached sessions).
        if self._config.config_file:
            args.extend(["-f", self._config.config_file])
        else:
            args.extend(["-f", "/dev/null"])
        return args

    def base_args(self) -> list[str]:
        """Return the public tmux command prefix for this manager."""
        return self._base_args()

    @staticmethod
    def _parse_session_info_line(line: str) -> TmuxSessionInfo | None:
        """Parse one tab-delimited tmux metadata row."""
        if not line.strip():
            return None
        parts = line.split("\t")
        if len(parts) < 2:
            return None
        pid_str = parts[1]
        return TmuxSessionInfo(
            name=parts[0],
            pane_pid=int(pid_str) if pid_str.isdigit() else None,
            pane_id=parts[2] if len(parts) > 2 and parts[2] else None,
            window_name=parts[3] if len(parts) > 3 and parts[3] else None,
            pane_title=parts[4] if len(parts) > 4 and parts[4] else None,
            pane_dead=parts[5] == "1" if len(parts) > 5 else False,
            pane_command=parts[6] if len(parts) > 6 and parts[6] else None,
            pane_path=parts[7] if len(parts) > 7 and parts[7] else None,
        )

    async def _run(
        self,
        *tmux_args: str,
        timeout: float = TMUX_COMMAND_TIMEOUT_SECONDS,
    ) -> tuple[int, str, str]:
        """Run a tmux subcommand and return (returncode, stdout, stderr)."""
        cmd = [*self._base_args(), *tmux_args]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                logger.debug(
                    "Tmux command exited before timeout kill "
                    "(pid=%s, timeout=%ss, command=%r, socket_name=%r, socket_path=%r)",
                    getattr(proc, "pid", None),
                    timeout,
                    cmd,
                    self._config.socket_name,
                    self._config.socket_path,
                )
            await proc.wait()
            raise
        return (
            proc.returncode or 0,
            (stdout_bytes or b"").decode(),
            (stderr_bytes or b"").decode(),
        )

    async def probe_target(self, target: str) -> TmuxProbeResult:
        """Probe one pane while distinguishing server loss from uncertainty."""
        try:
            rc, _stdout, stderr = await self._run(
                "display-message", "-p", "-t", target, "#{pane_id}"
            )
        except (TimeoutError, PermissionError) as exc:
            logger.debug("Tmux target probe was indeterminate for '%s': %s", target, exc)
            return TmuxProbeResult(TmuxProbeState.INDETERMINATE, None, str(exc))
        except OSError as exc:
            logger.warning("Tmux target probe failed unexpectedly for '%s': %s", target, exc)
            return TmuxProbeResult(TmuxProbeState.INDETERMINATE, None, str(exc))

        detail = stderr.strip()
        if rc == 0:
            return TmuxProbeResult(TmuxProbeState.LIVE, True)
        if _is_missing_tmux_server_error(detail):
            return TmuxProbeResult(TmuxProbeState.SERVER_MISSING, None, detail)
        if _is_missing_tmux_target_error(detail):
            return TmuxProbeResult(TmuxProbeState.LIVE, False, detail)
        if _is_tmux_permission_error(detail):
            logger.debug("Tmux target probe was indeterminate for '%s': %s", target, detail)
            return TmuxProbeResult(TmuxProbeState.INDETERMINATE, None, detail)
        logger.warning("Tmux target probe failed unexpectedly for '%s': %s", target, detail)
        return TmuxProbeResult(TmuxProbeState.INDETERMINATE, None, detail)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Check whether tmux (or WSL on Windows) is available."""
        from gobby.agents.tmux.wsl_compat import needs_wsl

        if needs_wsl():
            if not shutil.which("wsl"):
                return False
            import subprocess

            try:
                result = subprocess.run(
                    ["wsl", "--exec", "which", self._config.command],
                    capture_output=True,
                    timeout=5,
                )
                return result.returncode == 0
            except (subprocess.TimeoutExpired, OSError):
                return False
        return shutil.which(self._config.command) is not None

    def require_available(self) -> None:
        """Raise :class:`TmuxNotFoundError` if tmux is missing."""
        if not self.is_available():
            raise TmuxNotFoundError(self._config.command)

    async def health_check(self) -> bool:
        """Verify the tmux socket is responsive. Kill stale server if not.

        Returns True if healthy (or recovered), False if tmux is unavailable.
        """
        if not self.is_available():
            self._health_check_timeout_failures = 0
            return False

        try:
            rc, _stdout, stderr = await self._run("list-sessions", timeout=5.0)
            # rc=1 with no server is fine; tmux will start it on next create.
            if rc == 0 or _is_missing_tmux_server_error(stderr):
                self._health_check_timeout_failures = 0
                return True
            self._health_check_timeout_failures = 0
            logger.warning("tmux health check returned rc=%s: %s", rc, stderr.strip())
            return False
        except TimeoutError:
            self._health_check_timeout_failures += 1
            if self._health_check_timeout_failures < TMUX_HEALTH_CHECK_TIMEOUT_FAILURE_LIMIT:
                logger.warning(
                    "tmux socket unresponsive (timeout %s/%s); deferring kill-server.",
                    self._health_check_timeout_failures,
                    TMUX_HEALTH_CHECK_TIMEOUT_FAILURE_LIMIT,
                )
                return False
            logger.warning(
                "tmux socket unresponsive after %s consecutive timeouts. Killing stale server.",
                self._health_check_timeout_failures,
            )
        except Exception as e:
            self._health_check_timeout_failures = 0
            logger.warning("tmux health check failed: %s", e)
            return False

        # Attempt to kill the stale server and let it restart on next use
        try:
            await self._run("kill-server", timeout=5.0)
            self._health_check_timeout_failures = 0
            logger.info("Killed stale tmux server on socket '%s'", self._config.socket_name)
            return True
        except Exception as e:
            logger.warning("Failed to kill stale tmux server: %s", e)
            return False

    async def shutdown(self) -> None:
        """Stop the configured tmux server."""
        await self._run("kill-server", timeout=5.0)

    async def set_option(self, session_name: str, option: str, value: str) -> None:
        """Set an option on a tmux session."""
        await self._run("set-option", "-t", session_name, option, value, timeout=5.0)

    async def refresh_client(self, session_name: str) -> None:
        """Redraw every client attached to a tmux session.

        refresh-client targets a client tty, not a session, so the session's
        clients are resolved first. A session with no attached clients is a
        no-op; a client that detaches between the two commands is ignored.
        """
        rc, stdout, stderr = await self._run(
            "list-clients", "-t", session_name, "-F", "#{client_tty}", timeout=5.0
        )
        if rc != 0:
            raise RuntimeError(f"tmux list-clients failed for '{session_name}': {stderr.strip()}")
        for tty in (line.strip() for line in stdout.splitlines()):
            if not tty:
                continue
            refresh_rc, _stdout, refresh_stderr = await self._run(
                "refresh-client", "-t", tty, timeout=5.0
            )
            if refresh_rc != 0:
                logger.debug(
                    "tmux refresh-client failed for tty %s: %s", tty, refresh_stderr.strip()
                )

    async def create_session(
        self,
        name: str,
        command: str | list[str] | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> TmuxSessionInfo:
        """Create a new detached tmux session.

        Args:
            name: Session name (will be sanitised).
            command: Shell command (string) or argv list to run.
            cwd: Working directory for the initial pane.
            env: Extra environment variables to set inside the session.

        Returns:
            :class:`TmuxSessionInfo` for the new session.

        Raises:
            TmuxSessionError: If session creation fails.
        """
        self.require_available()

        # Convert Windows paths to WSL format when needed
        from gobby.agents.tmux.wsl_compat import convert_windows_path_to_wsl, needs_wsl

        if needs_wsl() and cwd:
            cwd = convert_windows_path_to_wsl(cwd)

        # Sanitise name (tmux dislikes dots and colons)
        safe_name = "".join(c if c.isalnum() or c in "-_" else "-" for c in name)

        # Fail fast if session already exists — never silently reuse
        if await self.has_session(safe_name):
            raise TmuxSessionError(
                f"Session '{safe_name}' already exists on socket '{self._config.socket_name}'",
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
        args.extend(
            [
                "-e",
                f"HISTSIZE={self._config.history_limit}",
            ]
        )

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
            if isinstance(command, list):
                command_text = shlex.join(command)
            else:
                command_text = command
        if secret_env_file_arg:
            command_text = _source_secret_env_command(command_text, secret_env_file_arg)
        if command_text:
            args.append(command_text)

        # Chain set-option to disable destroy-unattached atomically
        args.extend(
            [
                ";",
                "set-option",
                "-t",
                _exact_session_target(safe_name),
                "destroy-unattached",
                "off",
            ]
        )

        # Set scrollback history
        args.extend(
            [
                ";",
                "set-option",
                "-t",
                _exact_session_target(safe_name),
                "history-limit",
                str(self._config.history_limit),
            ]
        )

        # Keep pane alive after process exits so capture-pane can retrieve output
        args.extend(
            [
                ";",
                "set-option",
                "-w",
                "-t",
                _exact_session_target(safe_name),
                "remain-on-exit",
                "on",
            ]
        )

        try:
            rc, _stdout, stderr = await self._run(*args)
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

        # Fetch pane PID
        pane_pid = await self.get_pane_pid(safe_name)

        logger.info("Created tmux session '%s' (pane_pid=%s)", safe_name, pane_pid)
        return TmuxSessionInfo(
            name=safe_name,
            pane_pid=pane_pid,
        )

    async def list_sessions(self) -> list[TmuxSessionInfo]:
        """List all Gobby tmux sessions on the isolated socket."""
        # Fetch name, pid, pane_id, window name, pane title, pane_dead,
        # running command, and cwd in one go
        rc, stdout, _stderr = await self._run(
            "list-sessions",
            "-F",
            "#{session_name}\t#{pane_pid}\t#{pane_id}\t#{window_name}\t#{pane_title}\t#{pane_dead}\t#{pane_current_command}\t#{pane_current_path}",
        )
        if rc != 0:
            # No server running is rc=1 with "no server running"
            return []

        results: list[TmuxSessionInfo] = []
        for line in stdout.splitlines():
            if not line.strip():
                continue
            info = self._parse_session_info_line(line)
            if info:
                results.append(info)
        return results

    async def get_session(self, name: str) -> TmuxSessionInfo | None:
        """Fetch metadata for the first pane in one tmux session."""
        rc, stdout, _stderr = await self._run(
            "list-panes",
            "-t",
            _exact_session_target(name),
            "-F",
            "#{session_name}\t#{pane_pid}\t#{pane_id}\t#{window_name}\t#{pane_title}\t#{pane_dead}\t#{pane_current_command}\t#{pane_current_path}",
            timeout=2.0,
        )
        if rc != 0:
            return None
        for line in stdout.splitlines():
            info = self._parse_session_info_line(line)
            if info:
                return info
        return None

    async def list_pane_ids(self) -> set[str]:
        """Return the set of all live pane IDs (e.g. {"%0", "%5"}) across all sessions."""
        rc, stdout, _stderr = await self._run(
            "list-panes",
            "-a",
            "-F",
            "#{pane_id}\t#{pane_dead}",
        )
        if rc != 0:
            return set()
        pane_ids: set[str] = set()
        for line in stdout.splitlines():
            pane_id, _separator, pane_dead = line.strip().partition("\t")
            if pane_id and pane_dead != "1":
                pane_ids.add(pane_id)
        return pane_ids

    async def has_session(self, name: str) -> bool:
        """Check whether a session with *name* exists."""
        rc, _stdout, _stderr = await self._run("has-session", "-t", _exact_session_target(name))
        return rc == 0

    @staticmethod
    def _live_process_groups(pgids: set[int]) -> set[int]:
        live_pgids: set[int] = set()
        for pgid in pgids:
            try:
                os.killpg(pgid, 0)
                live_pgids.add(pgid)
            except ProcessLookupError:
                continue
            except PermissionError:
                live_pgids.add(pgid)
            except OSError:
                continue
        return live_pgids

    @classmethod
    async def _wait_for_process_groups_exit(cls, pgids: set[int], timeout: float) -> set[int]:
        if not pgids or timeout <= 0:
            return cls._live_process_groups(pgids)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        live_pgids = cls._live_process_groups(pgids)
        while live_pgids and loop.time() < deadline:
            await asyncio.sleep(min(0.1, max(0.0, deadline - loop.time())))
            live_pgids = cls._live_process_groups(live_pgids)
        return live_pgids

    async def kill_session(
        self, name: str, *, missing_ok: bool = False, timeout: float = 5.0
    ) -> bool:
        """Kill a tmux session and all processes in it.

        Collects pane PIDs before destroying the session, then sends SIGTERM
        to the process groups so that child processes (the actual agent CLI)
        are also killed. Stragglers get SIGKILL after a brief grace period.
        """
        # Collect pane PIDs before killing the session
        pids = await self._get_session_pids(name)

        # Kill the tmux session
        target = _exact_session_target(name)
        rc, _stdout, stderr = await self._run("kill-session", "-t", target)
        if rc != 0:
            message = stderr.strip()
            if any(error in message.lower() for error in _MISSING_SESSION_ERRORS):
                logger.debug(
                    "Tmux session '%s' was already missing during kill (missing_ok=%s): %s",
                    name,
                    missing_ok,
                    message,
                )
                return missing_ok
            logger.warning("Failed to kill tmux session '%s': %s", name, message)
            return False

        if needs_wsl():
            logger.info("Killed tmux session '%s' via WSL tmux", name)
            return True

        # Kill process groups rooted at each pane shell
        pgids: set[int] = set()
        for pid in pids:
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGTERM)
                pgids.add(pgid)
            except (ProcessLookupError, PermissionError, OSError):
                pass

        # Honor the caller's grace period, then SIGKILL straggling process groups.
        live_pgids = await self._wait_for_process_groups_exit(pgids, timeout)
        for pgid in live_pgids:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass

        logger.info("Killed tmux session '%s' (pids: %s)", name, pids)
        return True

    async def _get_session_pids(self, name: str) -> list[int]:
        """Get all pane PIDs in a tmux session."""
        rc, stdout, _ = await self._run(
            "list-panes",
            "-t",
            _exact_session_target(name),
            "-F",
            "#{pane_pid}",
        )
        if rc != 0:
            return []
        pids: list[int] = []
        for line in stdout.strip().splitlines():
            try:
                pids.append(int(line.strip()))
            except ValueError:
                pass
        return pids

    async def get_pane_pid(self, session_name: str) -> int | None:
        """Get the PID of the process running in the first pane."""
        rc, stdout, _stderr = await self._run(
            "display-message",
            "-t",
            _exact_session_target(session_name),
            "-p",
            "#{pane_pid}",
        )
        if rc != 0 or not stdout.strip():
            return None
        try:
            return int(stdout.strip())
        except ValueError:
            return None

    async def get_window_automatic_rename(self, target: str) -> bool | None:
        """Return whether ``automatic-rename`` is on for *target*'s window.

        A window Gobby has named via :meth:`rename_window` has
        ``automatic-rename`` disabled, so this is a cheap "has Gobby named this
        window yet?" probe for the repair sweep.

        Returns True/False, or None when the option cannot be read (e.g. the
        target window no longer exists).
        """
        rc, stdout, _stderr = await self._run(
            "display-message", "-t", target, "-p", "#{automatic-rename}"
        )
        if rc != 0:
            return None
        value = stdout.strip()
        if value in ("1", "on"):
            return True
        if value in ("0", "off"):
            return False
        return None

    async def get_window_name(self, target: str) -> str | None:
        """Return *target*'s current tmux window name, or None when unreadable."""
        rc, stdout, _stderr = await self._run(
            "display-message", "-t", target, "-p", "#{window_name}"
        )
        if rc != 0:
            return None
        value = stdout.strip()
        return value or None

    async def rename_window(self, target: str, title: str) -> bool:
        """Rename the tmux window containing *target*.

        Also enables ``set-titles`` so the name propagates to the outer
        terminal emulator, disables ``automatic-rename`` to prevent tmux
        from overwriting it, and disables ``allow-rename`` so a program
        running inside the pane (e.g. Claude Code's version/status OSC
        title escapes) cannot overwrite the window name either.

        Args:
            target: A tmux target (session name, pane ID like ``%42``, etc.).
            title: New window title.

        Returns:
            True on success.
        """
        tmux_title = _escape_tmux_format(title)
        rc, _stdout, stderr = await self._run(
            "set-option",
            "-t",
            target,
            "set-titles",
            "on",
            ";",
            "set-option",
            "-t",
            target,
            "set-titles-string",
            "#W",
            ";",
            "rename-window",
            "-t",
            target,
            tmux_title,
            ";",
            "select-pane",
            "-t",
            target,
            "-T",
            tmux_title,
            ";",
            "set-option",
            "-w",
            "-t",
            target,
            "automatic-rename",
            "off",
            ";",
            "set-option",
            "-w",
            "-t",
            target,
            "allow-rename",
            "off",
        )
        if rc != 0:
            message = stderr.strip()
            if _is_missing_tmux_target_error(message):
                logger.debug(
                    "Skipping tmux window rename for missing target '%s': %s", target, message
                )
            else:
                logger.warning("Failed to rename tmux window for '%s': %s", target, message)
            return False
        return True

    async def release_window_title_ownership(self, target: str) -> TmuxReleaseOutcome:
        """Release Gobby's window and pane title overrides for *target*."""
        try:
            rc, _stdout, stderr = await self._run(
                "set-option",
                "-w",
                "-u",
                "-t",
                target,
                "automatic-rename",
                ";",
                "set-option",
                "-w",
                "-u",
                "-t",
                target,
                "allow-rename",
                ";",
                "select-pane",
                "-t",
                target,
                "-T",
                "",
            )
        except (TimeoutError, PermissionError) as exc:
            logger.debug("Tmux title release was indeterminate for '%s': %s", target, exc)
            return TmuxReleaseOutcome.INDETERMINATE
        except OSError as exc:
            logger.warning("Tmux title release failed unexpectedly for '%s': %s", target, exc)
            return TmuxReleaseOutcome.INDETERMINATE
        if rc != 0:
            message = stderr.strip()
            if _is_missing_tmux_server_error(message) or _is_missing_tmux_target_error(message):
                logger.debug(
                    "Tmux title for missing target '%s' is already released: %s", target, message
                )
                return TmuxReleaseOutcome.ALREADY_RELEASED
            if _is_tmux_permission_error(message):
                logger.debug("Tmux title release was indeterminate for '%s': %s", target, message)
                return TmuxReleaseOutcome.INDETERMINATE
            logger.warning("Failed to release tmux title for '%s': %s", target, message)
            return TmuxReleaseOutcome.INDETERMINATE
        return TmuxReleaseOutcome.RELEASED

    async def capture_pane(self, session_name: str, lines: int = 5) -> str | None:
        """Capture the last N lines from a tmux session's pane.

        Args:
            session_name: Target session name.
            lines: Number of lines to capture from the bottom.

        Returns:
            Captured text, or None on failure.
        """
        rc, stdout, _stderr = await self._run(
            "capture-pane",
            "-t",
            _send_keys_target(session_name),
            "-p",  # print to stdout
            "-J",  # join wrapped lines
            f"-S-{max(lines, 0)}",  # tmux returns history plus the visible pane
        )
        if rc != 0:
            return None
        if lines <= 0:
            return ""
        return "".join(stdout.splitlines(keepends=True)[-lines:])

    async def capture_full_pane(self, session_name: str) -> str | None:
        """Capture the complete configured tmux history and visible pane."""
        rc, stdout, _stderr = await self._run(
            "capture-pane",
            "-t",
            _send_keys_target(session_name),
            "-p",
            "-S",
            "-",
        )
        if rc != 0:
            return None
        return stdout

    async def send_keys(self, session_name: str, keys: str, *, literal: bool = True) -> bool:
        """Send keys to a tmux session.

        Args:
            session_name: Target session name.
            keys: Key string to send.  When *literal* is True a trailing
                  ``\\n`` triggers an ``Enter`` keypress after the literal
                  text.  When *literal* is False, *keys* are passed directly
                  to ``tmux send-keys`` (accepts tmux key names such as
                  ``C-c``, ``Escape``, ``Enter``, ``C-d``).
            literal: If True (default), send text in literal mode (``-l``)
                     so special characters are not interpreted.  If False,
                     pass keys directly to tmux without ``-l``, allowing
                     tmux key names.

        Returns:
            True on success.
        """
        if not literal:
            # Raw mode: pass keys directly, tmux interprets key names.
            rc, _stdout, stderr = await self._run(
                "send-keys",
                "-t",
                _send_keys_target(session_name),
                keys,
            )
            if rc != 0:
                logger.warning(
                    "Failed to send raw keys to tmux session '%s': %s", session_name, stderr.strip()
                )
                return False
            return True

        try:
            await send_literal_text_to_tmux_target(
                _send_keys_target(session_name),
                keys,
                tmux_cmd=self._base_args(),
            )
        except TmuxTextInjectionError as exc:
            logger.warning(
                "Failed to send keys to tmux session '%s': %s",
                session_name,
                exc,
            )
            return False

        return True

    async def dispatch_keys(self, session_name: str, keys: str, *, literal: bool = True) -> bool:
        """Backend-neutral alias used by plan-keystroke playback."""
        return await self.send_keys(session_name, keys, literal=literal)

    async def destroy_session(self, session_name: str, *, missing_ok: bool = False) -> bool:
        """Backend-neutral alias for killing a tmux session by name."""
        return await self.kill_session(session_name, missing_ok=missing_ok)

    async def snapshot_lines(self, session_name: str, lines: int = 5) -> str | None:
        """Backend-neutral alias for capturing the last N pane lines."""
        return await self.capture_pane(session_name, lines=lines)
