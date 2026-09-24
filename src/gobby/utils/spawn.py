"""Daemon subprocess spawns that start through posix_spawn instead of fork.

On macOS CPython forks the calling process, holding the GIL, unless the
executable path names a directory, close_fds is False, cwd is None and no
session, process-group or fd-passing option is set. Forking the daemon stalls
every thread for about 70 ms per spawn; posix_spawn costs about 1 ms (#22815).
With close_fds=False every inheritable descriptor reaches the child. Python
creates descriptors non-inheritable (PEP 446), runner.main seals the ones the
daemon inherited (seal_inherited_descriptors), and adopt_inherited_claim clears
the singleton lock handed over through pass_fds.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import shutil
import subprocess  # nosec B404 - the helper owns daemon spawns
from collections.abc import Mapping, Sequence
from typing import Any, Literal, overload

# Each of these forces the fork path, or bypasses executable resolution. A
# caller that needs one spawns directly and is named in the allowlist of
# tests/utils/test_spawn.py.
_REJECTED_OPTIONS = frozenset(
    {
        "close_fds",
        "start_new_session",
        "process_group",
        "pass_fds",
        "preexec_fn",
        "shell",
        "executable",
    }
)
# Runs the command from a directory without Popen's cwd. Given an absolute
# path, cd skips CDPATH; -P resolves symlinks as chdir(2) does. Failures past
# _spawn_plan's checks surface as the shell's exit status, not OSError: a
# directory that vanished exits 1, and an exec failure exits 126 (macOS sh;
# dash reports a missing file as 127).
_CHDIR_EXEC = 'cd -P -- "$1" && shift && exec "$@"'

type Argv = Sequence[str | os.PathLike[str]]
type Directory = str | os.PathLike[str]


def resolve_executable(
    name: str, *, cwd: Directory | None = None, env: Mapping[str, str] | None = None
) -> str:
    """Return the absolute path exec would run for ``name``.

    A name containing a slash is relative to ``cwd``; a bare name is searched
    on the PATH of ``env``, or of this process when ``env`` is None.
    """
    if os.path.dirname(name):
        executable: str | None = os.path.join(os.fspath(cwd) if cwd is not None else "", name)
    else:
        executable = shutil.which(name, path=os.pathsep.join(os.get_exec_path(env)))
    if executable is None:
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), name)
    return os.path.abspath(executable)


def _spawn_plan(
    argv: Argv, cwd: Directory | None, env: Mapping[str, str] | None
) -> tuple[list[str], str | None, str | None]:
    """Return the argv to start, its absolute executable and the cwd Popen still needs.

    A command that is not installed goes to subprocess as written, so it raises
    the usual FileNotFoundError; only that failed start takes the fork path.
    """
    command = [os.fspath(part) for part in argv]
    directory = os.fspath(cwd) if cwd is not None else None
    try:
        executable = resolve_executable(command[0], cwd=directory, env=env)
    except FileNotFoundError:
        return command, None, directory
    if directory is None:
        return command, executable, None
    if not os.path.isdir(directory):
        # Popen reports a missing cwd as an OSError before anything runs.
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), directory)
    # Not os.path.abspath: its normpath folds "link/.." lexically, and chdir(2) does not.
    absolute = directory if os.path.isabs(directory) else os.path.join(os.getcwd(), directory)
    chdir_argv = ["/bin/sh", "-c", _CHDIR_EXEC, "gobby-spawn", absolute, executable, *command[1:]]
    return chdir_argv, "/bin/sh", None


def _reject(options: Mapping[str, Any]) -> None:
    if rejected := _REJECTED_OPTIONS & options.keys():
        raise TypeError(f"spawn helper cannot take {sorted(rejected)}")


@overload
def run(
    argv: Argv,
    *,
    cwd: Directory | None = None,
    env: Mapping[str, str] | None = None,
    text: Literal[True],
    **options: Any,
) -> subprocess.CompletedProcess[str]: ...


@overload
def run(
    argv: Argv,
    *,
    cwd: Directory | None = None,
    env: Mapping[str, str] | None = None,
    text: Literal[False] = False,
    **options: Any,
) -> subprocess.CompletedProcess[bytes]: ...


def run(
    argv: Argv,
    *,
    cwd: Directory | None = None,
    env: Mapping[str, str] | None = None,
    text: bool = False,
    **options: Any,
) -> subprocess.CompletedProcess[Any]:
    """``subprocess.run`` without forking the daemon; ``text`` picks str or bytes output."""
    _reject(options)
    command, executable, directory = _spawn_plan(argv, cwd, env)
    return subprocess.run(  # nosec B603 - argv form, resolved executable
        command,
        executable=executable,
        cwd=directory,
        env=env,
        close_fds=False,
        text=text,
        **options,
    )


@overload
def popen(
    argv: Argv,
    *,
    cwd: Directory | None = None,
    env: Mapping[str, str] | None = None,
    text: Literal[True],
    **options: Any,
) -> subprocess.Popen[str]: ...


@overload
def popen(
    argv: Argv,
    *,
    cwd: Directory | None = None,
    env: Mapping[str, str] | None = None,
    text: Literal[False] = False,
    **options: Any,
) -> subprocess.Popen[bytes]: ...


def popen(
    argv: Argv,
    *,
    cwd: Directory | None = None,
    env: Mapping[str, str] | None = None,
    text: bool = False,
    **options: Any,
) -> subprocess.Popen[Any]:
    """``subprocess.Popen`` without forking the daemon; ``text`` picks str or bytes pipes."""
    _reject(options)
    command, executable, directory = _spawn_plan(argv, cwd, env)
    return subprocess.Popen(  # nosec B603 - argv form, resolved executable
        command,
        executable=executable,
        cwd=directory,
        env=env,
        close_fds=False,
        text=text,
        **options,
    )


async def create_subprocess_exec(
    *argv: str | os.PathLike[str],
    cwd: Directory | None = None,
    env: Mapping[str, str] | None = None,
    **options: Any,
) -> asyncio.subprocess.Process:
    """``asyncio.create_subprocess_exec`` without forking the daemon."""
    _reject(options)
    command, executable, directory = _spawn_plan(argv, cwd, env)
    return await asyncio.create_subprocess_exec(
        *command, executable=executable, cwd=directory, env=env, close_fds=False, **options
    )


def seal_inherited_descriptors() -> None:
    """Make every descriptor above stderr non-inheritable; the daemon calls it at start.

    Spawns keep close_fds=False, so a descriptor the launcher left inheritable
    would reach every child.
    """
    try:
        names = os.listdir("/dev/fd")
    except FileNotFoundError:  # no /dev/fd (Windows)
        return
    for name in names:
        fd = int(name)
        if fd > 2:
            # The listing's own directory descriptor is already closed.
            with contextlib.suppress(OSError):
                os.set_inheritable(fd, False)
