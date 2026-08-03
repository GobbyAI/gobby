"""Discover and reap managed SRT sandbox-runner process trees."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Protocol, cast

import psutil

from gobby.paths import get_gobby_home

logger = logging.getLogger(__name__)

_POLICY_FILENAMES = {"settings", "settings.json", "violations", "violations.jsonl"}


class ProcessHandle(Protocol):
    """Subset of ``psutil.Process`` needed for tree cleanup."""

    pid: int
    info: dict[str, object]

    def children(self, *, recursive: bool) -> list[ProcessHandle]: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


ProcessIter = Callable[[list[str]], Iterable[ProcessHandle]]
WaitProcs = Callable[..., tuple[list[ProcessHandle], list[ProcessHandle]]]


def _run_id_from_cmdline(cmdline: object, sandbox_root: Path) -> str | None:
    if not isinstance(cmdline, (list, tuple)):
        return None
    args = [arg.replace("\\", "/") for arg in cmdline if isinstance(arg, str)]
    if not any(arg.rsplit("/", 1)[-1] == "runner.mjs" for arg in args):
        return None

    sandbox_prefix = f"{sandbox_root.as_posix().rstrip('/')}/"
    run_ids: set[str] = set()
    for arg in args:
        policy_path = arg.split("=", 1)[-1]
        if not policy_path.startswith(sandbox_prefix):
            continue
        relative_path = policy_path.removeprefix(sandbox_prefix)
        run_id, separator, filename = relative_path.partition("/")
        if separator and run_id and filename in _POLICY_FILENAMES:
            run_ids.add(run_id)
    if len(run_ids) != 1:
        return None
    return run_ids.pop()


def _discover_runner_roots(
    process_iter: ProcessIter,
    sandbox_root: Path,
) -> dict[str, list[ProcessHandle]]:
    roots: dict[str, list[ProcessHandle]] = {}
    try:
        for process in process_iter(["pid", "cmdline"]):
            try:
                run_id = _run_id_from_cmdline(process.info.get("cmdline"), sandbox_root)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
                continue
            if run_id is not None:
                roots.setdefault(run_id, []).append(process)
    except (psutil.Error, OSError):
        logger.warning("Failed to scan for managed SRT sandbox runners", exc_info=True)
    return roots


def _collect_process_tree(roots: Sequence[ProcessHandle]) -> list[ProcessHandle]:
    processes_by_pid: dict[int, ProcessHandle] = {}
    for root in roots:
        try:
            descendants = root.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            descendants = []
        for process in [*descendants, root]:
            try:
                processes_by_pid.setdefault(process.pid, process)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
                continue
    return list(processes_by_pid.values())


def _reap_roots(run_id: str, roots: Sequence[ProcessHandle], wait_procs: WaitProcs) -> int:
    processes = _collect_process_tree(roots)
    if not processes:
        return 0

    for process in processes:
        try:
            process.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            continue

    try:
        _, alive = wait_procs(processes, timeout=3.0)
    except (psutil.Error, OSError):
        alive = processes
    for process in alive:
        try:
            process.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            continue
    if alive:
        try:
            _, alive = wait_procs(alive, timeout=1.0)
        except (psutil.Error, OSError):
            pass
    if alive:
        logger.warning(
            "SRT sandbox runner processes survived reap run_id=%s pid_count=%d",
            run_id,
            len(alive),
        )

    pid_count = len(processes)
    logger.info(
        "Reaped SRT sandbox runner run_id=%s pid_count=%d",
        run_id,
        pid_count,
        extra={"run_id": run_id, "pid_count": pid_count},
    )
    return pid_count


def _reap_srt_runner_process_tree(
    run_id: str,
    *,
    process_iter: ProcessIter | None = None,
    wait_procs: WaitProcs | None = None,
    sandbox_root: Path | None = None,
) -> int:
    iterator = process_iter or cast(ProcessIter, psutil.process_iter)
    waiter = wait_procs or cast(WaitProcs, psutil.wait_procs)
    policy_root = sandbox_root or get_gobby_home() / "run" / "sandbox"
    roots = _discover_runner_roots(iterator, policy_root).get(run_id, [])
    return _reap_roots(run_id, roots, waiter)


async def reap_srt_runner_process_tree(
    run_id: str,
    *,
    process_iter: ProcessIter | None = None,
    wait_procs: WaitProcs | None = None,
    sandbox_root: Path | None = None,
) -> int:
    """Verify a run's SRT runner tree is gone, terminating survivors."""
    return await asyncio.to_thread(
        _reap_srt_runner_process_tree,
        run_id,
        process_iter=process_iter,
        wait_procs=wait_procs,
        sandbox_root=sandbox_root,
    )


def reap_orphaned_srt_runner_process_trees(
    active_run_ids: set[str],
    *,
    process_iter: ProcessIter | None = None,
    wait_procs: WaitProcs | None = None,
    sandbox_root: Path | None = None,
) -> int:
    """Reap SRT runner trees whose run ids have no active agent-run row."""
    iterator = process_iter or cast(ProcessIter, psutil.process_iter)
    waiter = wait_procs or cast(WaitProcs, psutil.wait_procs)
    policy_root = sandbox_root or get_gobby_home() / "run" / "sandbox"
    roots_by_run_id = _discover_runner_roots(iterator, policy_root)
    return sum(
        _reap_roots(run_id, roots_by_run_id[run_id], waiter)
        for run_id in sorted(roots_by_run_id.keys() - active_run_ids)
    )
