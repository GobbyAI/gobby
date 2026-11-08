"""Child-process preservation and reaping for daemon shutdown."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

from gobby import shutdown_budget
from gobby.runner_lifecycle_agents import _list_active_agent_runs_once

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

logger = logging.getLogger("gobby.runner_lifecycle")

# Bounds the managed-run listing that gates the child reap, so a stalled hub cannot
# consume the deadline-path reap budget. A timeout skips the reap, as a failed listing does.
_AGENT_RUN_LISTING_SECONDS = shutdown_budget.AGENT_LISTING_SECONDS
# Bounds the reaper's pre-kill enumeration. The live-broker and node-launcher probes in
# srt_monitor each take at most _PROBE_TIMEOUT_SECONDS (2s); the psutil reads are synchronous.
_CHILD_ENUMERATION_BUDGET_SECONDS = shutdown_budget.CHILD_ENUMERATION_SECONDS


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


def start_preserved_agent_listing(runner: GobbyRunner) -> None:
    """Start the managed-run listing at shutdown start, inside the graceful window.

    The deadline path reads this result, so a stalled hub at the deadline does not
    decide the reap. The bound is spent while the graceful sequence runs.
    """
    runner._preserved_agent_listing_task = asyncio.create_task(
        _preserved_agent_terminal_pids(
            runner, timeout=shutdown_budget.SHUTDOWN_AGENT_LISTING_SECONDS
        ),
        name="preserved-agent-listing",
    )


async def preserved_agent_pids_for_reap(runner: GobbyRunner) -> set[int] | None:
    """The shutdown-start listing when it succeeded; otherwise a fresh bounded listing.

    A failed start-time listing is re-run here with the shorter deadline bound. A second
    failure returns None, and the caller skips the reap rather than risk a live agent.
    """
    cached: asyncio.Task[set[int] | None] | None = getattr(
        runner, "_preserved_agent_listing_task", None
    )
    if cached is not None:
        await asyncio.wait({cached})
        if not cached.cancelled() and cached.exception() is None:
            pids = cached.result()
            if pids is not None:
                return pids
    return await _preserved_agent_terminal_pids(runner)


def log_children_left_unreaped(reason: str) -> None:
    """Name each child a skipped reap leaves running, with the reason it was skipped."""
    import psutil

    try:
        root = psutil.Process(os.getpid())
        children = root.children(recursive=True)
    except (psutil.Error, OSError) as e:
        logger.warning(
            "Child process reap skipped (%s); child processes could not be listed: %s", reason, e
        )
        return
    logger.warning(
        "Child process reap skipped (%s); left unreaped: %s",
        reason,
        _describe_children(children, root_pid=root.pid) if children else "none",
    )


async def _preserved_agent_terminal_pids(
    runner: GobbyRunner, *, timeout: float = _AGENT_RUN_LISTING_SECONDS
) -> set[int] | None:
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
            listing = db_executor.run(_list_active_agent_runs_once, runner, include_fenced=True)
        else:
            listing = asyncio.to_thread(_list_active_agent_runs_once, runner, include_fenced=True)
        # Bounded: a stalled hub must not hold the deadline-path reap hostage.
        runs = await asyncio.wait_for(listing, timeout=timeout)
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
    timeout: float = shutdown_budget.CHILD_SWEEP_WAIT_SECONDS,
    *,
    preserve_agents: bool = False,
    preserved_agent_pids: set[int] | None = None,
    preserve_srt_monitor: bool = False,
) -> None:
    """Terminate then force-kill child processes that survived graceful shutdown.

    Enumeration is bounded and names the step it was in when it overran. Once the reapable
    children are known, the sweeps run in a shielded task: a cancelled caller still waits
    for every SIGKILLed child to be reaped before the cancellation is raised.
    """
    try:
        import psutil

        current_process = psutil.Process(os.getpid())
        children = current_process.children(recursive=True)
        if not children:
            logger.debug("No child processes remaining after graceful shutdown")
            return

        step = "child enumeration"
        try:
            async with asyncio.timeout(_CHILD_ENUMERATION_BUDGET_SECONDS):
                if preserve_agents:
                    from gobby.agents.srt_monitor import (
                        live_monitor_identities,
                        pinned_monitor_identities,
                        verified_monitor_paths,
                    )

                    monitor_identities: dict[int, float] = {}
                    pinned_only: set[int] = set()
                    if preserve_srt_monitor:
                        step = "live_monitor_identities"
                        registered = await asyncio.to_thread(live_monitor_identities)
                        step = "verified_monitor_paths"
                        paths = await asyncio.to_thread(verified_monitor_paths)
                        pinned: dict[int, float] = {}
                        if paths is not None:
                            node, runner = paths
                            step = "pinned_monitor_identities"
                            pinned = await asyncio.to_thread(
                                pinned_monitor_identities, children, node, runner
                            )
                        pinned_only = set(pinned) - set(registered)
                        monitor_identities = {**pinned, **registered}
                    step = "preserved-process expansion"
                    preserved_pids = _expand_preserved_agent_processes(
                        psutil,
                        children,
                        (preserved_agent_pids or set()) | set(monitor_identities),
                        monitor_identities,
                    )
                    for pid in sorted(monitor_identities):
                        if pid in preserved_pids:
                            source = "pinned-argv" if pid in pinned_only else "registry-or-record"
                            logger.info(
                                "Preserving SRT monitor broker pid=%d source=%s", pid, source
                            )
                    reapable_children = [
                        child for child in children if child.pid not in preserved_pids
                    ]
                    preserved_count = len(children) - len(reapable_children)
                    if preserved_count:
                        logger.info(
                            "Preserving %d agent and monitor child process(es) during restart",
                            preserved_count,
                        )
                    children = reapable_children
                    if not children:
                        logger.debug(
                            "No non-agent child processes remaining after restart preservation"
                        )
                        return
        except TimeoutError:
            logger.error(
                "Child process enumeration exceeded %.1fs during %s; left unreaped: %s",
                _CHILD_ENUMERATION_BUDGET_SECONDS,
                step,
                _describe_children(children, root_pid=current_process.pid),
            )
            return
        except asyncio.CancelledError:
            logger.warning(
                "Child process enumeration was cancelled during %s; left unreaped: %s",
                step,
                _describe_children(children, root_pid=current_process.pid),
            )
            raise

        logger.info(
            "Reaping %d remaining child process(es) after graceful shutdown: %s",
            len(children),
            _describe_children(children, root_pid=current_process.pid),
        )
        # Shielded: a caller's cancellation waits for the sweep, so every SIGKILLed child is
        # waited on before the cancellation propagates.
        sweep = asyncio.create_task(
            _sweep_children(children, timeout=timeout, root_pid=current_process.pid),
            name="child-process-sweep",
        )
        cancellation: asyncio.CancelledError | None = None
        while not sweep.done():
            try:
                await asyncio.shield(sweep)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
        if cancellation is not None:
            raise cancellation
    except Exception as e:
        logger.warning("Child process reap failed: %s", e)


async def _sweep_children(children: list[Any], *, timeout: float, root_pid: int) -> None:
    """Terminate each child, then force-kill and reap each one still alive after the grace."""
    import psutil

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
    if not alive:
        return

    logger.warning(
        "Force-killing %d child process(es) still alive after graceful shutdown: %s",
        len(alive),
        _describe_children(alive, root_pid=root_pid),
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
            _describe_children(still_alive, root_pid=root_pid),
        )


def _describe_children(processes: list[Any], *, root_pid: int) -> str:
    return "; ".join(_describe_child_process(child, root_pid=root_pid) for child in processes)


def _expand_preserved_agent_processes(
    psutil_module: Any,
    children: list[Any],
    root_pids: set[int],
    identities: dict[int, float] | None = None,
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
            if identities and pid in identities and process.create_time() != identities[pid]:
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
