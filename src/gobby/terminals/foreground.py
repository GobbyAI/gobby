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
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

import psutil

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
    # ucomm, the accounting name: on macOS ``comm`` prints the full command line, arguments
    # included, so the daemon never asks for it. The labels take only the bare name anyway.
    try:
        result = spawn.run(
            ["ps", "-A", "-o", "pid=,tpgid=,ucomm="],
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
        # ucomm is last because it is the only field that can contain spaces.
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


def foreground_executables(shell_pids: Mapping[str, int]) -> dict[str, str]:
    """Map each key in ``shell_pids`` to the executable in its terminal's foreground.

    The value is the leader's absolute on-disk executable path, which :func:`command_name`
    reduces to a bare name. ``ps`` reports only the accounting name, a basename cut to 16
    characters on macOS, so a versioned build (``2.1.322``, ``grok-1.0.77-maco``) cannot be
    told apart by it; the path can. When the path is unreadable (another user's process),
    the accounting name stands, which names a program but never proves a versioned build.
    Keys whose shell is gone, whose
    shell has no controlling terminal, or whose foreground group leader cannot be
    read are absent from the result rather than present with a placeholder, so a
    caller can tell "unknown" from a real name. An idle shell is its own foreground
    group, which is how a bare prompt resolves to ``zsh`` instead of to nothing.
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
        resolved[key] = _executable_path(shell[0]) or leader[1]
    return resolved


def _executable_path(pid: int) -> str | None:
    """``pid``'s absolute on-disk executable, or ``None`` when it cannot be read."""
    try:
        path = psutil.Process(pid).exe()
    except (psutil.Error, OSError):
        return None
    return path if path and Path(path).is_absolute() else None


def foreground_commands(shell_pids: Mapping[str, int]) -> dict[str, str]:
    """Map each key in ``shell_pids`` to the command in its terminal's foreground.

    Same keys as :func:`foreground_executables`, reduced to bare command names.
    """
    commands = {
        key: command_name(executable)
        for key, executable in foreground_executables(shell_pids).items()
    }
    return {key: name for key, name in commands.items() if name}


def foreground_node_script(pid: int) -> str | None:
    """Return the foreground node process's direct script entrypoint, if proven."""
    snapshot = _process_snapshot()
    shell = snapshot.get(pid)
    leader = snapshot.get(shell[0]) if shell else None
    if shell is None or leader is None or command_name(leader[1]) != "node":
        return None
    try:
        process = psutil.Process(shell[0])
        argv = process.cmdline()
        if command_name(process.exe()) != "node" or len(argv) < 2:
            return None
        script = argv[1]
        if (
            command_name(argv[0]) != "node"
            or not isinstance(script, str)
            or not Path(script).is_absolute()
        ):
            return None
        return script
    except (psutil.Error, OSError):
        return None


def foreground_child_executables(pid: int) -> list[str]:
    """Executables of ``pid``'s direct children that share its process group.

    A launcher that runs a provider without giving it a group of its own leaves the
    provider in the launcher's foreground group. Such a child holds the terminal with
    its parent, and its on-disk executable names the provider, which the group leader's
    command does not. A child whose lookup fails is dropped, never guessed at.
    """
    try:
        group = os.getpgid(pid)
        children = psutil.Process(pid).children(recursive=False)
    except (OSError, psutil.Error):
        return []
    executables: list[str] = []
    for child in children:
        try:
            if os.getpgid(child.pid) == group:
                executables.append(child.exe())
        except (OSError, psutil.Error):
            continue
    return executables


def shell_cwds(shell_pids: Mapping[str, int]) -> dict[str, str]:
    """Map each key in ``shell_pids`` to its shell's working directory.

    The shell's own directory is the prompt's, which is what a Terminals row
    shows; a job the shell runs almost always shares it. Each lookup is one
    in-process ``psutil`` call. Keys whose shell is gone or unreadable are
    absent, as in :func:`foreground_commands`.
    """
    resolved: dict[str, str] = {}
    for key, pid in shell_pids.items():
        if pid <= 0:
            continue
        try:
            cwd = psutil.Process(pid).cwd()
        except (psutil.Error, OSError):
            continue
        if cwd:
            resolved[key] = cwd
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


__all__ = [
    "command_name",
    "foreground_commands",
    "foreground_executables",
    "process_shell",
    "shell_cwds",
    "shell_pid",
]
