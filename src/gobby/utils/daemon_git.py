"""Nonblocking, cancellation-safe Git subprocesses for daemon call sites."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess  # nosec B404 - argv-only Git process boundary
import threading
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from gobby.utils.git import git_subprocess_env


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
type _StatusKey = tuple[
    asyncio.AbstractEventLoop,
    str,
    tuple[str, ...],
    float,
    tuple[tuple[str, str], ...] | None,
]


def parse_porcelain_v1_z(output: str) -> tuple[GitStatusEntry, ...]:
    """Parse Git porcelain-v1 NUL records without interpreting path text."""
    records = output.split("\0")
    entries: list[GitStatusEntry] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if len(record) < 4 or record[2] != " ":
            raise ValueError("invalid porcelain-v1 status record")

        code = record[:2]
        path = record[3:]
        original_path = None
        if "R" in code or "C" in code:
            if index >= len(records) or not records[index]:
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

    process: subprocess.Popen[str] | None = None
    kill_requested: bool = False
    finished: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def attach(self, process: subprocess.Popen[str]) -> None:
        with self.lock:
            self.process = process
            kill_requested = self.kill_requested
        if kill_requested:
            _kill_process_group(process)

    def kill(self) -> None:
        with self.lock:
            self.kill_requested = True
            process = None if self.finished else self.process
        if process is not None:
            _kill_process_group(process)

    def mark_finished(self) -> None:
        with self.lock:
            self.finished = True


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
        loop = asyncio.get_running_loop()
        completion: asyncio.Future[GitOk | GitFailed] = loop.create_future()
        control = _ProcessControl()
        worker = threading.Thread(
            target=_run_git_worker,
            args=(loop, completion, control, argv),
            kwargs={"cwd": cwd, "env": env, "input_text": input_text},
            name="gobby-daemon-git",
            daemon=True,
        )
        try:
            worker.start()
        except RuntimeError as exc:
            return GitFailed("failed", argv, None, "", str(exc))

        try:
            return await asyncio.wait_for(asyncio.shield(completion), timeout)
        except TimeoutError:
            control.kill()
            # The dedicated worker owns eventual kill/reap, including a process
            # that has not returned from Popen yet. Never await a wedged spawn.
            return GitTimeout("timeout", argv, timeout)
        except asyncio.CancelledError:
            control.kill()
            raise


def _run_git_worker(
    loop: asyncio.AbstractEventLoop,
    completion: asyncio.Future[GitOk | GitFailed],
    control: _ProcessControl,
    argv: tuple[str, ...],
    *,
    cwd: str,
    env: dict[str, str] | None,
    input_text: str | None,
) -> None:
    """Own one process from spawn through communication and leader reap."""
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(  # nosec B603 B607 - fixed executable, argv-only args
            argv,
            cwd=cwd,
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            env=env if env is not None else git_subprocess_env(),
            start_new_session=True,
        )
        control.attach(process)
        stdout, stderr = process.communicate(input_text)
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


def _set_result_if_pending(
    future: asyncio.Future[GitOk | GitFailed],
    result: GitOk | GitFailed,
) -> None:
    if not future.done():
        future.set_result(result)


async def _await_cleanup[T](future: asyncio.Future[T] | asyncio.Task[T]) -> T:
    """Wait through repeated caller cancellation until process cleanup completes."""
    while True:
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            if future.done():
                raise


def _kill_process_group(process: subprocess.Popen[str]) -> None:
    """Kill the complete session-owned process group; the worker reaps its leader."""
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
