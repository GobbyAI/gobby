"""The foreground command running in a terminal, resolved from its shell pid.

A terminal row records the pid of the shell the daemon started (``process.pgid``
on a native row). That shell leads its own process group, so its controlling
terminal's foreground process group -- ``tpgid`` -- names whatever is running
*inside* the shell: ``zsh`` at an idle prompt, ``nvim`` or ``cargo`` while a job
holds the terminal.

This is the one rung of the pane label ladder that must be observed rather than
stored, which is why it is resolved at read time instead of persisted: a column
would be wrong the moment the user ran anything. Resolution costs a single
``ps`` for an entire page, never one per row, and every failure collapses to
"no command", which the ladder covers with its literal last rung.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Mapping

from gobby.utils import spawn

logger = logging.getLogger(__name__)

PS_TIMEOUT_SECONDS = 2.0


def _process_snapshot() -> dict[int, tuple[int, str]]:
    """Every process on this machine as ``pid -> (foreground pgid, command)``.

    The whole table is read rather than the pids of interest: ``ps -p`` refuses
    the entire request when one id is out of the platform's pid range, so a
    single stale row would blank the command for every other terminal on the
    page.
    """
    try:
        result = spawn.run(
            ["ps", "-A", "-o", "pid=,tpgid=,comm="],
            capture_output=True,
            text=True,
            timeout=PS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("foreground command lookup failed", exc_info=True)
        return {}

    snapshot: dict[int, tuple[int, str]] = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=2)
        if len(parts) != 3:
            continue
        # comm is last because it is the only field that can contain spaces.
        raw_pid, raw_tpgid, comm = parts
        try:
            snapshot[int(raw_pid)] = (int(raw_tpgid), comm)
        except ValueError:
            continue
    return snapshot


def command_name(raw: str) -> str:
    """Reduce a ``ps`` comm value to the bare command a label should show.

    macOS reports an absolute path for some processes and a login shell keeps its
    leading dash, so ``/bin/zsh`` and ``-zsh`` both mean ``zsh``.
    """
    return raw.strip().rsplit("/", 1)[-1].lstrip("-")


def foreground_commands(shell_pids: Mapping[str, int]) -> dict[str, str]:
    """Map each key in ``shell_pids`` to the command in its terminal's foreground.

    Keys whose shell is gone, whose shell has no controlling terminal, or whose
    foreground group leader cannot be read are absent from the result rather than
    present with a placeholder, so a caller can tell "unknown" from a real name.
    An idle shell is its own foreground group, which is how a bare prompt
    resolves to ``zsh`` instead of to nothing.
    """
    wanted = {key: pid for key, pid in shell_pids.items() if pid > 0}
    if not wanted:
        return {}

    snapshot = _process_snapshot()
    resolved: dict[str, str] = {}
    for key, pid in wanted.items():
        shell = snapshot.get(pid)
        if shell is None:
            continue
        leader = snapshot.get(shell[0])
        if leader is None:
            continue
        if name := command_name(leader[1]):
            resolved[key] = name
    return resolved


def shell_pid(row: object) -> int | None:
    """The pid of the shell a terminal row runs, when the row records one.

    A native row carries it as ``process.pgid``, written when the host promotes
    the spawn. A tmux row's pane pid is not persisted -- the pane sweep reports
    ``pane_current_command`` directly -- so this returns ``None`` for those.
    """
    process = getattr(row, "process", None)
    if not isinstance(process, Mapping):
        return None
    pgid = process.get("pgid")
    if isinstance(pgid, bool) or not isinstance(pgid, int) or pgid <= 0:
        return None
    return pgid


def process_shell(row: object) -> str | None:
    """The basename of the shell a daemon spawn launched, from ``process.shell``.

    The inventories fall back to it when no live foreground command resolves.
    """
    process = getattr(row, "process", None)
    if not isinstance(process, Mapping):
        return None
    shell = process.get("shell")
    return shell if isinstance(shell, str) and shell else None


__all__ = ["command_name", "foreground_commands", "process_shell", "shell_pid"]
