"""Nonblocking, cancellation-safe Git subprocesses for daemon call sites."""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import signal
import subprocess  # nosec B404 - argv-only Git process boundary
import tempfile
import threading
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Literal

from gobby.utils.git import git_subprocess_env
from gobby.utils.spawn import SpawnedSession, can_posix_spawn, resolve_executable

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GitOk:
    """A Git command that exited successfully."""

    status: Literal["ok"]
    argv: tuple[str, ...]
    stdout: str
    stderr: str
    returncode: int = 0


@dataclass(frozen=True, slots=True)
class GitTimeout:
    """A command past its deadline; its worker owns eventual kill and reap."""

    status: Literal["timeout"]
    argv: tuple[str, ...]
    timeout: float
    stdout: str = ""
    stderr: str = ""
    returncode: None = None


@dataclass(frozen=True, slots=True)
class GitFailed:
    """Git exited nonzero or could not be started."""

    status: Literal["failed"]
    argv: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class GitStatusEntry:
    """One porcelain-v1 status record with literal decoded paths."""

    code: str
    path: str
    original_path: str | None = None


type GitResult = GitOk | GitTimeout | GitFailed
type _ProcessPhase = Literal["queued", "preparing", "spawning", "running", "consuming", "finished"]
type _StatusKey = tuple[
    asyncio.AbstractEventLoop,
    str,
    tuple[str, ...],
    float,
    tuple[tuple[str, str], ...] | None,
]

_STREAM_CHUNK_BYTES = 64 * 1024
_MAX_STREAM_STDERR_BYTES = 64 * 1024


type _GitProcess = subprocess.Popen[bytes] | SpawnedSession
type _GitWorker = Callable[
    [asyncio.AbstractEventLoop, asyncio.Future[GitOk | GitFailed], "_ProcessControl"], None
]
type _ProcessFactory = Callable[[bytes | None], _GitProcess]


def _spawn_git(
    argv: tuple[str, ...],
    *,
    cwd: str,
    env: dict[str, str] | None,
    input_bytes: bytes | None,
    stdout_file: BinaryIO | None = None,
    stderr_file: BinaryIO | None = None,
) -> _GitProcess:
    """Start Git as the leader of its own session and process group.

    Popen needs fork for cwd and a new session, and forking the daemon stalls
    every thread (#22815), so POSIX starts ``git -C <cwd>`` with posix_spawn.
    """
    process_env = env if env is not None else git_subprocess_env() or dict(os.environ)
    if can_posix_spawn():
        if not os.path.isdir(cwd):
            # Callers read a missing directory as Git never starting, as Popen reports it.
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), cwd)
        executable = resolve_executable(argv[0], env=process_env)
        return SpawnedSession.spawn(
            (executable, "-C", cwd, *argv[1:]),
            env=process_env,
            input_bytes=input_bytes,
            stdout_file=stdout_file,
            stderr_file=stderr_file,
        )
    return subprocess.Popen(  # nosec B603 B607 - fixed executable, argv-only args
        argv,
        cwd=cwd,
        stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
        stdout=stdout_file if stdout_file is not None else subprocess.PIPE,
        stderr=stderr_file if stderr_file is not None else subprocess.PIPE,
        env=process_env,
        start_new_session=True,
    )


def parse_porcelain_v1_z(output: str) -> tuple[GitStatusEntry, ...]:
    """Parse Git porcelain-v1 NUL records without interpreting path text."""
    if not output:
        return ()
    if not output.endswith("\0"):
        raise ValueError("invalid porcelain-v1 status output")
    records = output[:-1].split("\0")
    if any(not record for record in records):
        raise ValueError("invalid porcelain-v1 status output")
    entries: list[GitStatusEntry] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4 or record[2] != " ":
            raise ValueError("invalid porcelain-v1 status record")

        code = record[:2]
        path = record[3:]
        original_path = None
        if "R" in code or "C" in code:
            if index >= len(records):
                raise ValueError("rename status record is missing its original path")
            original_path = records[index]
            index += 1
        entries.append(GitStatusEntry(code=code, path=path, original_path=original_path))
    return tuple(entries)


@dataclass(slots=True)
class _InFlight:
    task: asyncio.Task[GitResult]
    waiters: int = 0


@dataclass(slots=True)
class _ProcessControl:
    """Coordinate a deadline with a process that may still be spawning."""

    process: _GitProcess | None = None
    kill_requested: bool = False
    phase: _ProcessPhase = "queued"
    lock: threading.Lock = field(default_factory=threading.Lock)
    started_at: float = field(default_factory=time.monotonic)
    spawn_started_at: float | None = None
    spawn_finished_at: float | None = None
    kill_started_at: float | None = None
    pid: int | None = None

    def begin_preparing(self) -> None:
        with self.lock:
            self.phase = "preparing"

    def begin_spawn(self) -> None:
        with self.lock:
            self.phase = "spawning"
            self.spawn_started_at = time.monotonic()

    def diagnostic(self, *, include_cleanup: bool = True) -> str:
        """Snapshot the phase before timeout cleanup changes process ownership."""
        with self.lock:
            now = time.monotonic()
            spawn_seconds = (
                (self.spawn_finished_at or now) - self.spawn_started_at
                if self.spawn_started_at is not None
                else 0.0
            )
            running_seconds = (
                max(0.0, (self.kill_started_at or now) - self.spawn_finished_at)
                if self.spawn_finished_at
                else 0.0
            )
            diagnostic = (
                f"phase={self.phase} pid={self.pid} elapsed_seconds={now - self.started_at:.3f} "
                f"spawn_seconds={spawn_seconds:.3f} running_seconds={running_seconds:.3f}"
            )
            if not include_cleanup:
                return diagnostic
            cleanup_seconds = now - self.kill_started_at if self.kill_started_at else 0.0
            return f"{diagnostic} cleanup_seconds={cleanup_seconds:.3f}"

    def cleanup_seconds(self) -> float:
        """Return elapsed timeout cleanup without changing process ownership."""
        with self.lock:
            return time.monotonic() - self.kill_started_at if self.kill_started_at else 0.0

    def attach(self, process: _GitProcess) -> None:
        with self.lock:
            self.process = process
            self.pid = process.pid
            self.phase = "running"
            self.spawn_finished_at = time.monotonic()
            kill_requested = self.kill_requested
        if kill_requested:
            _kill_process_group(process)

    def begin_consuming(self) -> bool:
        """Claim the consumer phase unless the caller already gave up."""
        with self.lock:
            if self.kill_requested:
                return False
            self.phase = "consuming"
            return True

    def continue_consuming(self) -> bool:
        with self.lock:
            return not self.kill_requested

    def kill(self) -> _ProcessPhase:
        """Request shutdown and return the phase that owned cleanup at request time."""
        with self.lock:
            self.kill_requested = True
            if self.kill_started_at is None:
                self.kill_started_at = time.monotonic()
            phase = self.phase
            process = self.process if self.phase == "running" else None
        if process is not None:
            _kill_process_group(process)
        return phase

    def mark_finished(self) -> None:
        with self.lock:
            self.phase = "finished"
            self.process = None
            killed = self.kill_requested
        if killed:
            logger.debug("Git worker cleanup settled: %s", self.diagnostic())


class DaemonGitService:
    """Run Git without occupying the event loop or its shared worker pool."""

    def __init__(self) -> None:
        self._status_inflight: dict[_StatusKey, _InFlight] = {}
        self._status_lock = threading.Lock()

    async def run(
        self,
        args: Sequence[str],
        *,
        cwd: str | Path,
        timeout: float = 10.0,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
    ) -> GitResult:
        """Run one argv-only Git command with a deadline that includes spawn."""
        argv = ("git", *args)
        if timeout <= 0:
            return GitTimeout("timeout", argv, timeout)
        try:
            resolved_cwd = os.path.abspath(os.fspath(cwd))
        except (OSError, TypeError, ValueError) as exc:
            return GitFailed("failed", argv, None, "", str(exc))
        effective_env = dict(env) if env is not None else None
        return await self._execute(
            argv,
            cwd=resolved_cwd,
            timeout=timeout,
            env=effective_env,
            input_text=input_text,
        )

    async def stream_bytes(
        self,
        args: Sequence[str],
        *,
        cwd: str | Path,
        consume: Callable[[bytes], object] | None,
        timeout: float = 10.0,
        env: Mapping[str, str] | None = None,
    ) -> GitResult:
        """Spool raw stdout to disk, then deliver bounded byte chunks before returning."""
        argv = ("git", *args)
        if timeout <= 0:
            return GitTimeout("timeout", argv, timeout)
        try:
            resolved_cwd = os.path.abspath(os.fspath(cwd))
        except (OSError, TypeError, ValueError) as exc:
            return GitFailed("failed", argv, None, "", str(exc))
        effective_env = dict(env) if env is not None else None

        def worker(
            loop: asyncio.AbstractEventLoop,
            completion: asyncio.Future[GitOk | GitFailed],
            control: _ProcessControl,
        ) -> None:
            _stream_git_worker(
                loop,
                completion,
                control,
                argv,
                cwd=resolved_cwd,
                env=effective_env,
                consume=consume,
            )

        return await self._execute_worker(argv, cwd=resolved_cwd, timeout=timeout, worker=worker)

    async def status(
        self,
        cwd: str | Path,
        paths: Collection[str] = (),
        *,
        timeout: float = 10.0,
        env: Mapping[str, str] | None = None,
    ) -> GitResult:
        """Inspect literal paths and coalesce only an identical in-flight status."""
        sorted_paths = tuple(sorted(paths))
        args = (
            "--literal-pathspecs",
            "--no-optional-locks",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--",
            *sorted_paths,
        )
        argv = ("git", *args)
        if timeout <= 0:
            return GitTimeout("timeout", argv, timeout)
        try:
            resolved_cwd = os.path.abspath(os.fspath(cwd))
        except (OSError, TypeError, ValueError) as exc:
            return GitFailed("failed", argv, None, "", str(exc))
        effective_env = dict(env) if env is not None else None
        env_key = tuple(sorted(effective_env.items())) if effective_env is not None else None
        key: _StatusKey = (asyncio.get_running_loop(), resolved_cwd, argv, timeout, env_key)

        with self._status_lock:
            flight = self._status_inflight.get(key)
            if flight is None:
                task = asyncio.create_task(
                    self._execute(
                        argv,
                        cwd=resolved_cwd,
                        timeout=timeout,
                        env=effective_env,
                        input_text=None,
                    )
                )
                flight = _InFlight(task=task)
                self._status_inflight[key] = flight

            flight.waiters += 1
        cancelled = False
        try:
            return await asyncio.shield(flight.task)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            with self._status_lock:
                flight.waiters -= 1
                last_cancelled = cancelled and flight.waiters == 0
                if (last_cancelled or flight.task.done()) and self._status_inflight.get(
                    key
                ) is flight:
                    self._status_inflight.pop(key)
            if last_cancelled and not flight.task.done():
                flight.task.cancel()
                await _await_cleanup(flight.task)

    async def _execute(
        self,
        argv: tuple[str, ...],
        *,
        cwd: str,
        timeout: float,
        env: dict[str, str] | None,
        input_text: str | None,
    ) -> GitResult:
        def spawn(input_bytes: bytes | None) -> _GitProcess:
            return _spawn_git(argv, cwd=cwd, env=env, input_bytes=input_bytes)

        def worker(
            loop: asyncio.AbstractEventLoop,
            completion: asyncio.Future[GitOk | GitFailed],
            control: _ProcessControl,
        ) -> None:
            _run_git_worker(
                loop,
                completion,
                control,
                argv,
                spawn=spawn,
                input_text=input_text,
            )

        return await self._execute_worker(argv, cwd=cwd, timeout=timeout, worker=worker)

    async def _execute_worker(
        self,
        argv: tuple[str, ...],
        *,
        cwd: str | None,
        timeout: float,
        worker: _GitWorker,
    ) -> GitResult:
        """Apply one deadline and cancellation lifecycle to a dedicated Git worker."""
        loop = asyncio.get_running_loop()
        completion: asyncio.Future[GitOk | GitFailed] = loop.create_future()
        control = _ProcessControl()
        worker_thread = threading.Thread(
            target=worker,
            args=(loop, completion, control),
            name="gobby-daemon-git",
            daemon=True,
        )
        try:
            worker_thread.start()
        except RuntimeError as exc:
            return GitFailed("failed", argv, None, "", str(exc))

        try:
            return await asyncio.wait_for(asyncio.shield(completion), timeout)
        except TimeoutError:
            diagnostic = control.diagnostic(include_cleanup=False)
            logger.warning(
                "Git command timed out: cwd=%s timeout_seconds=%.3f %s",
                cwd,
                timeout,
                diagnostic,
            )
            control.kill()
            cancelled = await _await_worker_cleanup(completion)
            completion.cancel()
            if cancelled:
                raise asyncio.CancelledError() from None
            cleanup_seconds = control.cleanup_seconds()
            return GitTimeout(
                "timeout",
                argv,
                timeout,
                stderr=f"Git timed out: {diagnostic} cleanup_seconds={cleanup_seconds:.3f}",
            )
        except asyncio.CancelledError:
            control.kill()
            await _await_worker_cleanup(completion)
            completion.cancel()
            raise


def _run_git_worker(
    loop: asyncio.AbstractEventLoop,
    completion: asyncio.Future[GitOk | GitFailed],
    control: _ProcessControl,
    argv: tuple[str, ...],
    *,
    spawn: _ProcessFactory,
    input_text: str | None,
) -> None:
    """Own one process from spawn through communication and leader reap."""
    process: _GitProcess | None = None
    control.begin_preparing()
    try:
        input_bytes = (
            input_text.encode("utf-8", errors="surrogateescape") if input_text is not None else None
        )
        control.begin_spawn()
        process = spawn(input_bytes)
        control.attach(process)
        stdout_bytes, stderr_bytes = process.communicate(input_bytes)
        stdout = stdout_bytes.decode("utf-8", errors="surrogateescape")
        stderr = stderr_bytes.decode("utf-8", errors="surrogateescape")
        if process.returncode == 0:
            result: GitOk | GitFailed = GitOk("ok", argv, stdout, stderr)
        else:
            result = GitFailed("failed", argv, process.returncode, stdout, stderr)
    except Exception as exc:
        if process is not None:
            _kill_process_group(process)
            process.wait()
        result = GitFailed("failed", argv, None, "", str(exc))
    finally:
        control.mark_finished()

    try:
        loop.call_soon_threadsafe(_set_result_if_pending, completion, result)
    except RuntimeError:
        pass


def _stream_git_worker(
    loop: asyncio.AbstractEventLoop,
    completion: asyncio.Future[GitOk | GitFailed],
    control: _ProcessControl,
    argv: tuple[str, ...],
    *,
    cwd: str,
    env: dict[str, str] | None,
    consume: Callable[[bytes], object] | None,
) -> None:
    """Spool raw process output and consume it without an in-memory copy."""
    process: _GitProcess | None = None
    consumer_error: Exception | None = None
    control.begin_preparing()
    try:
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            control.begin_spawn()
            process = _spawn_git(
                argv,
                cwd=cwd,
                env=env,
                input_bytes=None,
                stdout_file=stdout_file,
                stderr_file=stderr_file,
            )
            control.attach(process)
            process.wait()
            may_consume = control.begin_consuming()
            stderr_file.seek(0)
            stderr = stderr_file.read(_MAX_STREAM_STDERR_BYTES).decode(
                "utf-8", errors="surrogateescape"
            )
            if process.returncode == 0:
                if consume is not None and may_consume:
                    stdout_file.seek(0)
                    while control.continue_consuming():
                        chunk = stdout_file.read(_STREAM_CHUNK_BYTES)
                        if not chunk:
                            break
                        try:
                            consume(chunk)
                        except Exception as exc:
                            consumer_error = exc
                            break
                result: GitOk | GitFailed = GitOk("ok", argv, "", stderr)
            else:
                result = GitFailed("failed", argv, process.returncode, "", stderr)
    except Exception as exc:
        if process is not None:
            _kill_process_group(process)
            process.wait()
        result = GitFailed("failed", argv, None, "", str(exc))
    finally:
        control.mark_finished()

    try:
        if consumer_error is None:
            loop.call_soon_threadsafe(_set_result_if_pending, completion, result)
        else:
            loop.call_soon_threadsafe(_set_exception_if_pending, completion, consumer_error)
    except RuntimeError:
        pass


def _set_result_if_pending(
    future: asyncio.Future[GitOk | GitFailed],
    result: GitOk | GitFailed,
) -> None:
    if not future.done():
        future.set_result(result)


def _set_exception_if_pending(
    future: asyncio.Future[GitOk | GitFailed],
    error: Exception,
) -> None:
    if not future.done():
        future.set_exception(error)


async def _await_cleanup[T](future: asyncio.Future[T] | asyncio.Task[T]) -> T:
    """Wait through repeated caller cancellation until process cleanup completes."""
    while True:
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            if future.done():
                raise


async def _await_worker_cleanup(
    future: asyncio.Future[GitOk | GitFailed],
) -> bool:
    """Wait for the off-loop worker to finish process kill and reap."""
    cancelled = False
    while True:
        try:
            await asyncio.shield(future)
            return cancelled
        except asyncio.CancelledError:
            cancelled = True
            if future.done():
                return True
        except Exception:
            return cancelled


def _kill_process_group(process: _GitProcess) -> None:
    """Kill the complete owned process group; the worker reaps its leader."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
        return
    except ProcessLookupError:
        return
    except (AttributeError, PermissionError):
        pass

    try:
        process.kill()
    except ProcessLookupError:
        pass


daemon_git = DaemonGitService()


async def normalize_commit_sha(
    sha: str | None,
    *,
    cwd: str | Path | None = None,
    timeout: float = 5.0,
) -> str | None:
    """Resolve a commit object to Git's canonical unique short SHA."""
    if not sha or len(sha) < 4:
        return None
    repo_path = cwd if cwd is not None else Path.cwd()
    object_type = await daemon_git.run(("cat-file", "-t", sha), cwd=repo_path, timeout=timeout)
    if not isinstance(object_type, GitOk) or object_type.stdout.strip() != "commit":
        return None
    resolved = await daemon_git.run(("rev-parse", "--short", sha), cwd=repo_path, timeout=timeout)
    if not isinstance(resolved, GitOk):
        return None
    normalized = resolved.stdout.strip()
    return normalized or None
