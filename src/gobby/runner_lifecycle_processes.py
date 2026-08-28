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


async def _agent_live_sessions_by_name(
    socket_name: str | None,
    socket_path: str | None,
) -> dict[str, Any] | None:
    """List live tmux sessions for one socket identity; None on failure."""
    try:
        from gobby.agents.tmux import get_tmux_session_manager
        from gobby.agents.tmux.session_manager import TmuxSessionManager

        manager = get_tmux_session_manager()
        config = manager.config
        if (socket_name and socket_name != config.socket_name) or (
            socket_path and socket_path != config.socket_path
        ):
            config = config.model_copy(
                update={
                    "socket_name": socket_name or config.socket_name,
                    "socket_path": socket_path,
                }
            )
            manager = TmuxSessionManager(config)
        live_sessions = await manager.list_sessions()
    except Exception as e:
        logger.warning("Failed to verify tmux panes for agent preservation: %s", e)
        return None
    return {
        session.name: session
        for session in live_sessions
        if not getattr(session, "pane_dead", False)
    }


def _host_preserve_pids(runner: GobbyRunner) -> set[int]:
    """Host PID from the live gterm supervisor, if identity-checked."""
    host = getattr(runner, "terminal_host_manager", None)
    pid = getattr(host, "host_pid", None)
    if isinstance(pid, int) and pid > 0:
        return {pid}
    return set()


async def _preserved_agent_terminal_pids(runner: GobbyRunner) -> set[int] | None:
    """Resolve PIDs for managed agents that must survive shutdown.

    Fenced (reconciliation_pending) runs are preserved like any other managed
    run, sessions are verified against each run's persisted tmux socket
    identity, and tmux failures fall back to the stored run PID. Returns None
    when the managed-run set cannot be determined at all; the caller must then
    skip child reaping rather than risk killing live agents (including the
    daemon-owned tmux server).
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

    listings: dict[tuple[str | None, str | None], dict[str, Any] | None] = {}
    terminal_sessions = _live_terminal_session_names(runner)
    for run in runs:
        stored_pid = getattr(run, "pid", None)
        fallback_pid = stored_pid if isinstance(stored_pid, int) and stored_pid > 0 else None
        session_name = getattr(run, "tmux_session_name", None)
        if not isinstance(session_name, str):
            run_id = getattr(run, "id", None)
            if isinstance(run_id, str):
                session_name = terminal_sessions.get(run_id)
        if not isinstance(session_name, str):
            if fallback_pid is not None:
                pids.add(fallback_pid)
            continue
        metadata = getattr(run, "resume_metadata_json", None) or {}
        socket_name = metadata.get("tmux_socket_name")
        socket_path = metadata.get("tmux_socket_path")
        key = (
            socket_name if isinstance(socket_name, str) and socket_name else None,
            socket_path if isinstance(socket_path, str) and socket_path else None,
        )
        if key not in listings:
            listings[key] = await _agent_live_sessions_by_name(*key)
        live_by_name = listings[key]
        if live_by_name is None:
            if fallback_pid is not None:
                pids.add(fallback_pid)
            continue
        live = live_by_name.get(session_name)
        pane_pid = getattr(live, "pane_pid", None)
        if isinstance(pane_pid, int) and pane_pid > 0:
            pids.add(pane_pid)
        elif live is not None and fallback_pid is not None:
            pids.add(fallback_pid)
    return pids


def _live_terminal_session_names(runner: GobbyRunner) -> dict[str, str]:
    """Map agent_run_id → session/spawn name from pending|live terminal rows."""
    manager = getattr(runner, "terminal_manager", None)
    list_live = getattr(manager, "list_live_by_machine", None)
    if not callable(list_live):
        return {}
    try:
        from gobby.utils.machine_id import require_machine_id

        rows = list_live(require_machine_id())
    except Exception:
        logger.debug("Failed to list terminal rows for shutdown preservation", exc_info=True)
        return {}
    names: dict[str, str] = {}
    for row in rows:
        run_id = getattr(row, "agent_run_id", None)
        name = getattr(row, "session_name", None) or getattr(row, "spawn_key", None)
        if isinstance(run_id, str) and isinstance(name, str):
            names[run_id] = name
    return names


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
                "Force-killing %d child process(es) still alive after graceful shutdown",
                len(alive),
            )
            for child in alive:
                try:
                    child.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
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
