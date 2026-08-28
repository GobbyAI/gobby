"""Row-owning spawn primitive for web-created terminals (no agent run)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

from gobby.agents.spawn_executor import derive_spawn_key, kill_spawn_key
from gobby.storage.terminals import TerminalManager, mint_terminal_id
from gobby.terminals.dimensions import validate_dimensions
from gobby.terminals.host_client import HostCommandError
from gobby.terminals.runtime import (
    CommitSpawnRefusedError,
    TerminalRuntime,
    TerminalSpawnFailed,
    TerminalSpawnRequest,
    can_reserve_observer,
)


@dataclass
class WebSpawnResult:
    """Outcome of a terminal-only spawn."""

    success: bool
    terminal_id: str
    error: str | None = None


async def spawn_web_terminal(
    *,
    manager: TerminalManager,
    runtime: TerminalRuntime,
    project_id: str,
    session_id: str | None,
    rows: object,
    cols: object,
    cwd: str | None,
    command: list[str],
    timeout_seconds: float | None = None,
    cancel_event: asyncio.Event | None = None,
) -> WebSpawnResult:
    """Create a pending row, prepare, and promote — same CAS matrix as execute_spawn."""
    validated = validate_dimensions(rows, cols)
    terminal_id = mint_terminal_id()
    spawn_key = derive_spawn_key(runtime.backend, terminal_id)
    manager.create_pending(
        terminal_id,
        project_id,
        runtime.backend,
        "gobby",
        spawn_key,
        session_id=session_id,
        rows=validated[0],
        cols=validated[1],
    )
    if cancel_event is not None and cancel_event.is_set():
        manager.fail_pending(terminal_id)
        return WebSpawnResult(False, terminal_id, "cancelled")
    request = TerminalSpawnRequest(
        terminal_id=UUID(terminal_id),
        spawn_key=spawn_key,
        command=command,
        cwd=cwd,
        rows=validated[0],
        cols=validated[1],
    )
    if runtime.backend == "native":
        if not can_reserve_observer(runtime):
            manager.fail_pending(terminal_id)
            return WebSpawnResult(False, terminal_id, "native_reserve_unavailable")
        try:
            reservation = await runtime.reserve_observer(UUID(terminal_id))
        except HostCommandError as exc:
            manager.fail_pending(terminal_id)
            return WebSpawnResult(False, terminal_id, str(exc))
        request.reservation_id = reservation.get("reservation_id")
        request.reserve_key = reservation.get("reserve_key")
    prepare_task = asyncio.create_task(runtime.prepare_spawn(request))
    try:
        if timeout_seconds is not None:
            prepared = await asyncio.wait_for(asyncio.shield(prepare_task), timeout=timeout_seconds)
        else:
            prepared = await asyncio.shield(prepare_task)
    except TimeoutError:
        await kill_spawn_key(runtime, spawn_key, pending=manager.get(terminal_id))
        manager.fail_pending(terminal_id)
        return WebSpawnResult(False, terminal_id, "spawn timed out")
    except asyncio.CancelledError:
        manager.fail_pending(terminal_id)
        return WebSpawnResult(False, terminal_id, "cancelled")
    except TerminalSpawnFailed as exc:
        manager.fail_pending(terminal_id)
        return WebSpawnResult(False, terminal_id, str(exc))
    except Exception as exc:
        manager.fail_pending(terminal_id)
        return WebSpawnResult(False, terminal_id, str(exc))

    stored = prepared.stored_locator or {}
    locator_key = prepared.locator_key or ""
    prepared.acknowledge_persist()
    if runtime.backend == "native":
        bind = getattr(runtime, "bind_observer", None)
        try:
            if callable(bind) and request.reservation_id:
                await bind(prepared, request.reservation_id)
            else:
                prepared.acknowledge_observer()
        except Exception as exc:
            await kill_spawn_key(runtime, spawn_key, pending=manager.get(terminal_id))
            manager.fail_pending(terminal_id)
            return WebSpawnResult(False, terminal_id, str(exc))
    try:
        handle = await runtime.commit_spawn(prepared)
    except CommitSpawnRefusedError as exc:
        manager.fail_pending(terminal_id)
        return WebSpawnResult(False, terminal_id, str(exc))
    promoted = manager.promote_to_live(
        terminal_id,
        locator=stored,
        locator_key=locator_key,
        session_name=spawn_key if runtime.backend == "tmux" else None,
        host_epoch=None
        if runtime.backend == "tmux"
        else getattr(handle.locator, "frame_host_epoch", None),
    )
    if promoted is None:
        current = manager.get(terminal_id)
        await kill_spawn_key(runtime, spawn_key, pending=current)
        manager.fail_pending(terminal_id)
        return WebSpawnResult(False, terminal_id, "lost_cas_conflict")
    return WebSpawnResult(True, terminal_id)
