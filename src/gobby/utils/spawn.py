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
import signal
import subprocess  # nosec B404 - the helper owns daemon spawns
import tempfile
import threading
from collections.abc import Mapping, Sequence
from typing import Any, BinaryIO, Literal, overload

# Each of these forces the fork path, or bypasses executable resolution. A
# caller that needs its own session and the output at exit uses
# create_session_exec; any other caller that needs one spawns directly and is
# named in the allowlist of tests/utils/test_spawn.py.
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


def can_posix_spawn() -> bool:
    """Whether os.posix_spawn and the file actions SpawnedSession needs exist here."""
    return os.name == "posix" and all(
        hasattr(os, name)
        for name in ("posix_spawn", "POSIX_SPAWN_OPEN", "POSIX_SPAWN_DUP2", "POSIX_SPAWN_CLOSE")
    )


class SpawnedSession:
    """A ``posix_spawn`` process leading its own session, its output spooled to files.

    Popen needs fork for a new session; this starts the same session leader
    without forking the daemon. ``wait`` and ``communicate`` block, so the event
    loop awaits an AsyncSpawnedSession instead.
    """

    def __init__(
        self,
        pid: int,
        stdin_file: BinaryIO | None,
        stdout_file: BinaryIO,
        stderr_file: BinaryIO,
    ) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self._stdin_file = stdin_file
        self._stdout_file = stdout_file
        self._stderr_file = stderr_file

    @classmethod
    def spawn(
        cls,
        argv: tuple[str, ...],
        *,
        env: dict[str, str],
        input_bytes: bytes | None,
        stdout_file: BinaryIO | None = None,
        stderr_file: BinaryIO | None = None,
    ) -> SpawnedSession:
        """Start argv; output spools to the given files, or to new temporary ones.

        A failed spawn closes only the temporary files it created.
        """
        owned: list[BinaryIO] = []

        def spool(given: BinaryIO | None) -> BinaryIO:
            if given is None:
                given = tempfile.TemporaryFile()
                owned.append(given)
            return given

        reset_signals = tuple(
            getattr(signal, name)
            for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ")
            if hasattr(signal, name)
        )
        try:
            stdout_file = spool(stdout_file)
            stderr_file = spool(stderr_file)
            targets = [(stdout_file, 1), (stderr_file, 2)]
            stdin_file: BinaryIO | None = None
            if input_bytes is not None:
                stdin_file = spool(None)
                stdin_file.write(input_bytes)
                stdin_file.seek(0)
                targets.append((stdin_file, 0))
            file_actions: list[
                tuple[int, int] | tuple[int, int, int] | tuple[int, int, str, int, int]
            ] = []
            for source, target in targets:
                source_fd = source.fileno()
                file_actions.append((os.POSIX_SPAWN_DUP2, source_fd, target))
                if source_fd != target:
                    file_actions.append((os.POSIX_SPAWN_CLOSE, source_fd))
            if stdin_file is None:
                # The child never reads the daemon's own stdin.
                file_actions.append((os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0))
            pid = os.posix_spawn(
                argv[0],
                argv,
                env,
                file_actions=file_actions,
                # A session of its own, as start_new_session: a terminal read
                # cannot stop the child with SIGTTIN, and killpg reaches its group.
                setsid=True,
                setsigdef=reset_signals,
            )
        except Exception:
            for file in owned:
                file.close()
            raise
        return cls(pid, stdin_file, stdout_file, stderr_file)

    def communicate(self, _input_bytes: bytes | None = None) -> tuple[bytes, bytes]:
        try:
            self.wait()
            self._stdout_file.seek(0)
            self._stderr_file.seek(0)
            return self._stdout_file.read(), self._stderr_file.read()
        finally:
            if self._stdin_file is not None:
                self._stdin_file.close()
            self._stdout_file.close()
            self._stderr_file.close()

    def wait(self) -> int:
        if self.returncode is not None:
            return self.returncode
        while True:
            try:
                _pid, status = os.waitpid(self.pid, 0)
                break
            except InterruptedError:
                continue
        self.returncode = os.waitstatus_to_exitcode(status)
        return self.returncode

    def kill(self) -> None:
        os.kill(self.pid, signal.SIGKILL)


class AsyncSpawnedSession:
    """A SpawnedSession awaited like ``asyncio.subprocess.Process``.

    One thread reaps the leader and reads its output, as asyncio's threaded
    child watcher does, and every awaiter shares that one result: a second
    waitpid for the same child would fail with ECHILD.
    """

    def __init__(self, process: SpawnedSession) -> None:
        self._process = process
        loop = asyncio.get_running_loop()
        self._finished: asyncio.Future[tuple[int, bytes, bytes]] = loop.create_future()
        threading.Thread(
            target=self._reap, args=(loop,), name="gobby-spawn-reap", daemon=True
        ).start()

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    def kill(self) -> None:
        self._process.kill()

    async def wait(self) -> int:
        returncode, _stdout, _stderr = await asyncio.shield(self._finished)
        return returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        _returncode, stdout, stderr = await asyncio.shield(self._finished)
        return stdout, stderr

    def _reap(self, loop: asyncio.AbstractEventLoop) -> None:
        outcome: tuple[int, bytes, bytes] | Exception
        try:
            stdout, stderr = self._process.communicate()
            outcome = (self._process.wait(), stdout, stderr)
        except Exception as exc:
            outcome = exc
        # A loop that closed before the leader exited has no awaiter left.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(self._settle, outcome)

    def _settle(self, outcome: tuple[int, bytes, bytes] | Exception) -> None:
        if isinstance(outcome, Exception):
            self._finished.set_exception(outcome)
        else:
            self._finished.set_result(outcome)


type SessionProcess = AsyncSpawnedSession | asyncio.subprocess.Process


async def create_session_exec(
    *argv: str,
    env: Mapping[str, str] | None = None,
    cwd: Directory | None = None,
    input_bytes: bytes | None = None,
) -> SessionProcess:
    """``asyncio.create_subprocess_exec(..., start_new_session=True)`` without forking.

    stdin is ``input_bytes``, or /dev/null without it, and stdout and stderr
    are read once the leader exits. Where posix_spawn is missing (Windows),
    asyncio starts the process, and no fork stalls the daemon there.
    """
    if not can_posix_spawn():
        with tempfile.TemporaryFile() as stdin_file:
            if input_bytes is not None:
                stdin_file.write(input_bytes)
                stdin_file.seek(0)
            return await asyncio.create_subprocess_exec(
                *argv,
                stdin=stdin_file if input_bytes is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
                start_new_session=True,
            )
    process_env = dict(env if env is not None else os.environ)
    command, executable, _ = _spawn_plan(argv, cwd, process_env)
    if executable is None:
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), command[0])
    return AsyncSpawnedSession(
        SpawnedSession.spawn((executable, *command[1:]), env=process_env, input_bytes=input_bytes)
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
