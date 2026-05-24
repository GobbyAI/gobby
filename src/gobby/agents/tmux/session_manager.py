"""Tmux session lifecycle management.

Creates, lists, kills, and queries tmux sessions on an isolated socket
(``-L gobby``) so Gobby never interferes with the user's personal tmux.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import time
from dataclasses import dataclass, field

from gobby.agents.tmux.errors import TmuxNotFoundError, TmuxSessionError
from gobby.config.tmux import TmuxConfig

logger = logging.getLogger(__name__)


_MISSING_SESSION_ERRORS = ("can't find session", "no such session", "no server running")
_MISSING_TARGET_ERRORS = (
    *_MISSING_SESSION_ERRORS,
    "can't find pane",
    "no such pane",
)
TMUX_COMMAND_TIMEOUT_SECONDS = 10.0


def _is_missing_tmux_target_error(stderr: str) -> bool:
    """Return True for tmux errors that mean the target disappeared."""
    message = stderr.lower()
    return any(fragment in message for fragment in _MISSING_TARGET_ERRORS)


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


class TmuxSessionManager:
    """Manages tmux sessions on an isolated Gobby socket.

    All tmux commands use ``-L <socket_name>`` so that Gobby sessions
    are invisible to ``tmux ls`` in the user's default server.
    """

    def __init__(self, config: TmuxConfig | None = None) -> None:
        self._config = config or TmuxConfig()

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
            return False

        try:
            rc, _stdout, stderr = await self._run("list-sessions", timeout=5.0)
            # rc=1 with "no server running" is fine — server will start on next create
            if rc == 0 or "no server running" in stderr:
                return True
        except TimeoutError:
            logger.warning("tmux socket unresponsive (timeout). Killing stale server.")
        except Exception as e:
            logger.warning(f"tmux health check failed: {e}")

        # Attempt to kill the stale server and let it restart on next use
        try:
            await self._run("kill-server", timeout=5.0)
            logger.info(f"Killed stale tmux server on socket '{self._config.socket_name}'")
            return True
        except Exception as e:
            logger.warning(f"Failed to kill stale tmux server: {e}")
            return False

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

        # Inject env vars via -e (tmux 3.2+)
        if env:
            for key, val in env.items():
                args.extend(["-e", f"{key}={val}"])

        # Append shell command
        if command:
            if isinstance(command, list):
                import shlex

                args.append(shlex.join(command))
            else:
                args.append(command)

        # Chain set-option to disable destroy-unattached atomically
        args.extend(
            [
                ";",
                "set-option",
                "-t",
                safe_name,
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
                safe_name,
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
                safe_name,
                "remain-on-exit",
                "on",
            ]
        )

        rc, _stdout, stderr = await self._run(*args)
        if rc != 0:
            raise TmuxSessionError(
                f"Failed to create session (rc={rc}): {stderr.strip()}",
                session_name=safe_name,
            )

        # Fetch pane PID
        pane_pid = await self.get_pane_pid(safe_name)

        logger.info(f"Created tmux session '{safe_name}' (pane_pid={pane_pid})")
        return TmuxSessionInfo(
            name=safe_name,
            pane_pid=pane_pid,
        )

    async def list_sessions(self) -> list[TmuxSessionInfo]:
        """List all Gobby tmux sessions on the isolated socket."""
        # Fetch name, pid, pane_id, window name, pane title, and pane_dead status in one go
        rc, stdout, _stderr = await self._run(
            "list-sessions",
            "-F",
            "#{session_name}\t#{pane_pid}\t#{pane_id}\t#{window_name}\t#{pane_title}\t#{pane_dead}",
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
            name,
            "-F",
            "#{session_name}\t#{pane_pid}\t#{pane_id}\t#{window_name}\t#{pane_title}\t#{pane_dead}",
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
        rc, _stdout, _stderr = await self._run("has-session", "-t", name)
        return rc == 0

    async def kill_session(self, name: str, *, missing_ok: bool = False) -> bool:
        """Kill a tmux session and all processes in it.

        Collects pane PIDs before destroying the session, then sends SIGTERM
        to the process groups so that child processes (the actual agent CLI)
        are also killed. Stragglers get SIGKILL after a brief grace period.
        """
        # Collect pane PIDs before killing the session
        pids = await self._get_session_pids(name)

        # Kill the tmux session
        rc, _stdout, stderr = await self._run("kill-session", "-t", name)
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

        # Kill process groups rooted at each pane shell
        for pid in pids:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass

        # Brief grace period, then SIGKILL stragglers
        if pids:
            await asyncio.sleep(0.5)
            for pid in pids:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass

        logger.info(f"Killed tmux session '{name}' (pids: {pids})")
        return True

    async def _get_session_pids(self, name: str) -> list[int]:
        """Get all pane PIDs in a tmux session."""
        rc, stdout, _ = await self._run("list-panes", "-t", name, "-F", "#{pane_pid}")
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
            "display-message", "-t", session_name, "-p", "#{pane_pid}"
        )
        if rc != 0 or not stdout.strip():
            return None
        try:
            return int(stdout.strip())
        except ValueError:
            return None

    async def rename_window(self, target: str, title: str) -> bool:
        """Rename the tmux window containing *target*.

        Also enables ``set-titles`` so the name propagates to the outer
        terminal emulator, and disables ``automatic-rename`` to prevent
        tmux from overwriting it.

        Args:
            target: A tmux target (session name, pane ID like ``%42``, etc.).
            title: New window title.

        Returns:
            True on success.
        """
        rc, _stdout, stderr = await self._run(
            "set-option",
            "-g",
            "set-titles",
            "on",
            ";",
            "set-option",
            "-g",
            "set-titles-string",
            "#W",
            ";",
            "rename-window",
            "-t",
            target,
            title,
            ";",
            "select-pane",
            "-t",
            target,
            "-T",
            title,
            ";",
            "set-option",
            "-w",
            "-t",
            target,
            "automatic-rename",
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
            session_name,
            "-p",  # print to stdout
            "-J",  # join wrapped lines
            f"-S-{lines}",  # start N lines from bottom
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
                session_name,
                keys,
            )
            if rc != 0:
                logger.warning(
                    f"Failed to send raw keys to tmux session '{session_name}': {stderr.strip()}"
                )
                return False
            return True

        # Literal mode: split trailing newline into literal text + Enter.
        # TUI apps (Claude Code, Gemini CLI) treat a literal \n inside
        # send-keys -l as "add a line" rather than "submit the prompt".
        send_enter = keys.endswith("\n")
        text = keys.rstrip("\n")

        if text:
            rc, _stdout, stderr = await self._run(
                "send-keys",
                "-t",
                session_name,
                "-l",
                text,
            )
            if rc != 0:
                logger.warning(
                    f"Failed to send keys to tmux session '{session_name}': {stderr.strip()}"
                )
                return False

        if send_enter:
            # Brief pause so TUI apps (Claude Code, Gemini CLI) finish
            # processing literal text before receiving the Enter keystroke.
            # Without this, Enter can arrive before the TUI has committed
            # the preceding characters to its input state, causing it to
            # be silently dropped.
            if text:
                await asyncio.sleep(0.05)
            rc, _stdout, stderr = await self._run(
                "send-keys",
                "-t",
                session_name,
                "Enter",
            )
            if rc != 0:
                logger.warning(
                    f"Failed to send Enter to tmux session '{session_name}': {stderr.strip()}"
                )
                return False

        return True
