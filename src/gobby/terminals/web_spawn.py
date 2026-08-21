"""Row-owning spawn primitive for web-created terminals (no agent run)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID, uuid4

from gobby.agents.spawn_executor import derive_spawn_key
from gobby.storage.terminals import Terminal, TerminalManager
from gobby.terminals.dimensions import validate_dimensions
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
    terminal_id = str(uuid4())
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
        reservation = await runtime.reserve_observer(UUID(terminal_id))
        request.reservation_id = reservation.get("reservation_id")
        request.reserve_key = reservation.get("reserve_key")
    prepare_task = asyncio.create_task(runtime.prepare_spawn(request))
    try:
        if timeout_seconds is not None:
            prepared = await asyncio.wait_for(asyncio.shield(prepare_task), timeout=timeout_seconds)
        else:
            prepared = await asyncio.shield(prepare_task)
    except TimeoutError:
        await _kill(runtime, spawn_key, manager.get(terminal_id))
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
        await _kill(runtime, spawn_key, current)
        manager.fail_pending(terminal_id)
        return WebSpawnResult(False, terminal_id, "lost_cas_conflict")
    return WebSpawnResult(True, terminal_id)


async def _kill(runtime: TerminalRuntime, spawn_key: str, pending: Terminal | None) -> None:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    terminal = pending or Terminal(
        id=str(uuid4()),
        backend=runtime.backend,
        ownership="gobby",
        state="pending",
        machine_id=str(uuid4()),
        project_id=str(uuid4()),
        created_at=now,
        updated_at=now,
        attempt_generation=1,
        attempt_started_at=now,
        unresolved_writes={},
        spawn_key=spawn_key,
    )
    try:
        await runtime.terminate(terminal, 1.0)
    except Exception:
        return
