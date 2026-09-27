"""Child-process preservation and reaping for daemon shutdown."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

from gobby.runner_lifecycle_agents import _list_active_agent_runs_once

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

logger = logging.getLogger("gobby.runner_lifecycle")


def _host_preserve_pids(runner: GobbyRunner) -> set[int]:
    """Identity-checked gterm host PID to keep out of the child reap.

    The supervisor answers from its pidfile as well as its live handle, so a
    host that was preserved by ``stop()`` still survives the reaper; a drained
    host returns nothing and is reaped with the rest of the tree (#22002).
    """
    host = getattr(runner, "terminal_host_manager", None)
    preserved = getattr(host, "preserved_host_pid", None)
    if not callable(preserved):
        return set()
    pid = preserved()
    if isinstance(pid, int) and pid > 0:
        return {pid}
    return set()


async def _preserved_agent_terminal_pids(runner: GobbyRunner) -> set[int] | None:
    """Preserve the native host and active agent process IDs during shutdown.

    The managed-run set must be known before reaping any daemon descendants.
    A lost hub query therefore returns None and the caller skips child reaping.
    """
    pids = _host_preserve_pids(runner)
    agent_runner = getattr(runner, "agent_runner", None)
    run_storage = getattr(agent_runner, "run_storage", None)
    if run_storage is None:
        return pids
    try:
        db_executor = getattr(runner, "db_executor", None)
        if db_executor is not None:
            runs = await db_executor.run(_list_active_agent_runs_once, runner, include_fenced=True)
        else:
            runs = await asyncio.to_thread(
                _list_active_agent_runs_once, runner, include_fenced=True
            )
    except Exception as e:
        logger.warning("Failed to list active agent runs for restart preservation: %s", e)
        return None

    for run in runs:
        pid = getattr(run, "pid", None)
        if isinstance(pid, int) and pid > 0:
            pids.add(pid)
    return pids


def _describe_child_process(process: Any, *, root_pid: int) -> str:
    """Return stable best-effort identity for shutdown diagnostics."""
    try:
        name = process.name() or "<unknown>"
    except Exception:
        name = "<unavailable>"
    try:
        cmdline = " ".join(process.cmdline())[:80] or "<unknown>"
    except Exception:
        cmdline = "<unavailable>"

    description = f"pid={process.pid} name={name} cmdline={cmdline!r}"
    try:
        parent = process.parent()
        if parent is not None and parent.pid != root_pid:
            description += f" parent_pid={parent.pid}"
    except Exception:
        pass
    return description


async def _reap_remaining_child_processes(
    timeout: float = 1.0,
    *,
    preserve_agents: bool = False,
    preserved_agent_pids: set[int] | None = None,
) -> None:
    """Terminate then force-kill child processes that survived graceful shutdown."""
    try:
        import psutil

        current_process = psutil.Process(os.getpid())
        children = current_process.children(recursive=True)
        if not children:
            logger.debug("No child processes remaining after graceful shutdown")
            return

        if preserve_agents:
            preserved_pids = _expand_preserved_agent_processes(
                psutil,
                children,
                preserved_agent_pids or set(),
            )
            reapable_children = [child for child in children if child.pid not in preserved_pids]
            preserved_count = len(children) - len(reapable_children)
            if preserved_count:
                logger.info(
                    "Preserving %d terminal agent child process(es) during restart",
                    preserved_count,
                )
            children = reapable_children
            if not children:
                logger.debug("No non-agent child processes remaining after restart preservation")
                return

        logger.info(
            "Reaping %d remaining child process(es) after graceful shutdown",
            len(children),
        )
        for child in children:
            try:
                child.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        gone, alive = await asyncio.to_thread(psutil.wait_procs, children, timeout=timeout)
        logger.debug(
            "Child process termination sweep complete",
            extra={"terminated": len(gone), "remaining": len(alive)},
        )

        if alive:
            logger.warning(
                "Force-killing %d child process(es) still alive after graceful shutdown: %s",
                len(alive),
                "; ".join(
                    _describe_child_process(child, root_pid=current_process.pid) for child in alive
                ),
            )
            for child in alive:
                try:
                    child.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            _, still_alive = await asyncio.to_thread(psutil.wait_procs, alive, timeout=timeout)
            if still_alive:
                logger.error(
                    "%d child process(es) still alive after force-kill: %s",
                    len(still_alive),
                    "; ".join(
                        _describe_child_process(child, root_pid=current_process.pid)
                        for child in still_alive
                    ),
                )
    except Exception as e:
        logger.warning("Child process reap failed: %s", e)


def _expand_preserved_agent_processes(
    psutil_module: Any,
    children: list[Any],
    root_pids: set[int],
) -> set[int]:
    """Include descendants and in-daemon ancestors for preserved agent pane PIDs."""
    preserved: set[int] = set()
    children_by_pid = {child.pid: child for child in children}
    child_pids = set(children_by_pid)
    for pid in root_pids:
        snapshotted_process = children_by_pid.get(pid)
        if snapshotted_process is None:
            continue
        try:
            process = psutil_module.Process(pid)
        except (psutil_module.NoSuchProcess, psutil_module.AccessDenied):
            continue
        try:
            if process.create_time() != snapshotted_process.create_time():
                continue
        except (psutil_module.NoSuchProcess, psutil_module.AccessDenied):
            continue
        preserved.add(pid)
        try:
            preserved.update(child.pid for child in process.children(recursive=True))
        except (psutil_module.NoSuchProcess, psutil_module.AccessDenied):
            pass
        try:
            parent = process.parent()
        except (psutil_module.NoSuchProcess, psutil_module.AccessDenied):
            parent = None
        while parent is not None and parent.pid in child_pids:
            preserved.add(parent.pid)
            try:
                parent = parent.parent()
            except (psutil_module.NoSuchProcess, psutil_module.AccessDenied):
                break
    return preserved
