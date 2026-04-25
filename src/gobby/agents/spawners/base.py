"""Base classes and types for terminal spawners."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SpawnResult:
    """Result of spawning a terminal process."""

    success: bool
    message: str
    pid: int | None = None
    terminal_type: str | None = None
    error: str | None = None
    tmux_session_name: str | None = None
    """Tmux session name (set when terminal_type is tmux)."""
    tmux_socket_name: str | None = None
    """Tmux socket name used for the session."""
    tmux_socket_path: str | None = None
    """Explicit tmux socket path used for the session, if configured."""


def make_spawn_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """
    Create a clean environment for spawning child processes.

    Copies os.environ, merges provided env vars, and removes problematic
    variables like VIRTUAL_ENV that cause warnings when the child runs
    in a different directory (e.g., clones).

    Args:
        env: Additional environment variables to set

    Returns:
        Clean environment dict ready for subprocess.Popen
    """
    from gobby.telemetry import inject_into_env

    spawn_env = os.environ.copy()
    if env:
        spawn_env.update(env)

    # Inject trace context for propagation
    spawn_env = inject_into_env(spawn_env)

    # Clear VIRTUAL_ENV to prevent uv warnings in clones/worktrees
    # "VIRTUAL_ENV=/path/to/.venv does not match project environment"
    spawn_env.pop("VIRTUAL_ENV", None)
    spawn_env.pop("VIRTUAL_ENV_PROMPT", None)
    # Clear parent tmux identity vars so child sessions don't inherit them.
    # Without this, kill_agent can kill the parent's tmux pane instead of
    # the child's.
    for var in ("TMUX", "TMUX_PANE"):
        spawn_env.pop(var, None)

    # Ensure ~/.gobby/bin is on PATH (gcode, gsqz)
    gobby_bin = str(Path.home() / ".gobby" / "bin")
    current_path = spawn_env.get("PATH", "")
    if gobby_bin not in current_path.split(os.pathsep):
        spawn_env["PATH"] = f"{gobby_bin}{os.pathsep}{current_path}"

    return spawn_env


class TerminalSpawnerBase(ABC):
    """Base class for terminal spawners."""

    @property
    @abstractmethod
    def terminal_type(self) -> str:
        """The terminal type this spawner handles. Always 'tmux'."""
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this terminal is available on the system."""
        pass

    @abstractmethod
    def spawn(
        self,
        command: list[str],
        cwd: str | Path,
        env: dict[str, str] | None = None,
        title: str | None = None,
    ) -> SpawnResult:
        """
        Spawn a new terminal window with the given command.

        Args:
            command: Command to run in the terminal
            cwd: Working directory
            env: Environment variables to set
            title: Optional window title

        Returns:
            SpawnResult with success status and process info
        """
        pass
