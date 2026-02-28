"""Tmux session lifecycle management.

Creates, lists, kills, and queries tmux sessions on an isolated socket
(``-L gobby``) so Gobby never interferes with the user's personal tmux.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from dataclasses import dataclass, field

from gobby.agents.tmux.config import TmuxConfig
from gobby.agents.tmux.errors import TmuxNotFoundError, TmuxSessionError

logger = logging.getLogger(__name__)


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
        if self._config.socket_name:
            args.extend(["-L", self._config.socket_name])
        if self._config.config_file:
            args.extend(["-f", self._config.config_file])
        return args

    async def _run(
        self,
        *tmux_args: str,
        timeout: float = 10.0,
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
            proc.kill()
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
            parts = line.split("\t")
            if len(parts) >= 2:
                name = parts[0]
                pid_str = parts[1]
                pid = int(pid_str) if pid_str.isdigit() else None
                pane_id = parts[2] if len(parts) > 2 and parts[2] else None
                window_name = parts[3] if len(parts) > 3 and parts[3] else None
                pane_title = parts[4] if len(parts) > 4 and parts[4] else None
                pane_dead = parts[5] == "1" if len(parts) > 5 else False
                results.append(
                    TmuxSessionInfo(
                        name=name,
                        pane_pid=pid,
                        pane_id=pane_id,
                        window_name=window_name,
                        pane_title=pane_title,
                        pane_dead=pane_dead,
                    )
                )
        return results

    async def list_pane_ids(self) -> set[str]:
        """Return the set of all live pane IDs (e.g. {"%0", "%5"}) across all sessions."""
        rc, stdout, _stderr = await self._run(
            "list-panes",
            "-a",
            "-F",
            "#{pane_id}",
        )
        if rc != 0:
            return set()
        return {line.strip() for line in stdout.splitlines() if line.strip()}

    async def has_session(self, name: str) -> bool:
        """Check whether a session with *name* exists."""
        rc, _stdout, _stderr = await self._run("has-session", "-t", name)
        return rc == 0

    async def kill_session(self, name: str) -> bool:
        """Kill a tmux session by name. Returns True if killed."""
        rc, _stdout, stderr = await self._run("kill-session", "-t", name)
        if rc != 0:
            logger.warning(f"Failed to kill tmux session '{name}': {stderr.strip()}")
            return False
        logger.info(f"Killed tmux session '{name}'")
        return True

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
            "set-option",
            "-w",
            "-t",
            target,
            "automatic-rename",
            "off",
        )
        if rc != 0:
            logger.warning(f"Failed to rename tmux window for '{target}': {stderr.strip()}")
            return False
        return True

    async def send_keys(self, session_name: str, keys: str) -> bool:
        """Send raw keys to a tmux session (for web UI input forwarding).

        Args:
            session_name: Target session name.
            keys: Key string to send (uses tmux ``send-keys -l``).

        Returns:
            True on success.
        """
        rc, _stdout, stderr = await self._run("send-keys", "-t", session_name, "-l", keys)
        if rc != 0:
            logger.warning(
                f"Failed to send keys to tmux session '{session_name}': {stderr.strip()}"
            )
            return False
        return True
