"""Private shell-file transport for commands larger than tmux's message limit."""

from __future__ import annotations

import os
import shlex
import tempfile
from pathlib import Path

from gobby.agents.tmux.wsl_compat import convert_windows_path_to_wsl, needs_wsl

# Leave ample space for tmux's framing, options, and environment arguments.
INLINE_LAUNCH_LIMIT = 16 * 1024


def write_launcher(command: str) -> tuple[Path, str]:
    """Stage a command for sourcing by tmux's shell, unlinking before execution.

    Sourcing preserves the shell and command semantics of the inline transport.
    The open shell input survives unlink, including commands that replace the
    shell with exec or exit before reaching the end of the script.
    """
    fd, filename = tempfile.mkstemp(prefix="gobby-agent-launch-", suffix=".sh")
    path = Path(filename)
    shell_path = convert_windows_path_to_wsl(filename) if needs_wsl() else filename
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"rm -f -- {shlex.quote(shell_path)}\n{command}\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path, f". {shlex.quote(shell_path)}"
