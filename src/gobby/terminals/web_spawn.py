"""Row-owning spawn primitive for web-created terminals (no agent run)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

from gobby.agents.spawn_executor import (
    _schedule_timeout_cleanup,
    derive_spawn_key,
    kill_spawn_key,
    settle_promotion,
)
from gobby.storage.terminals import TerminalManager, mint_terminal_id
from gobby.terminals.dimensions import validate_dimensions
from gobby.terminals.native_runtime import classify_native_spawn_failure
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
    error_detail: str | None = None

    def __post_init__(self) -> None:
        if self.error is not None:
            self.error = self.error[:128]


async def _settle_native_failure(
    *,
    manager: TerminalManager,
    runtime: TerminalRuntime,
    terminal_id: str,
    spawn_key: str,
    exc: BaseException,
    host_terminal_id: str | None = None,
    host_epoch: str | None = None,
) -> WebSpawnResult:
    code, detail, settlement = classify_native_spawn_failure(exc)
    if settlement == "fail_pending_kill":
        mismatch = await kill_spawn_key(
            runtime,
            spawn_key,
            pending=manager.get(terminal_id),
            host_terminal_id=host_terminal_id,
            host_epoch=host_epoch,
        )
        if mismatch is not None:
            code, detail = "host_epoch_changed", str(mismatch)
    if settlement != "pending":
        manager.fail_pending(terminal_id)
    return WebSpawnResult(False, terminal_id, code, detail)


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
    attempt = manager.create_pending(
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
        except Exception as exc:
            return await _settle_native_failure(
                manager=manager,
                runtime=runtime,
                terminal_id=terminal_id,
                spawn_key=spawn_key,
                exc=exc,
            )
        request.reservation_id = reservation.get("reservation_id")
        request.reserve_key = reservation.get("reserve_key")
    prepare_task = asyncio.create_task(runtime.prepare_spawn(request))
    try:
        if timeout_seconds is not None:
            prepared = await asyncio.wait_for(asyncio.shield(prepare_task), timeout=timeout_seconds)
        else:
            prepared = await asyncio.shield(prepare_task)
    except TimeoutError:
        _schedule_timeout_cleanup(
            prepare_task,
            manager=manager,
            runtime=runtime,
            backend=runtime.backend,
            terminal_id=terminal_id,
            spawn_key=spawn_key,
            attempt_generation=attempt.attempt_generation,
            attempt_started_at=attempt.attempt_started_at,
        )
        return WebSpawnResult(False, terminal_id, "spawn_timeout", "spawn timed out")
    except asyncio.CancelledError:
        manager.fail_pending(terminal_id)
        return WebSpawnResult(False, terminal_id, "cancelled")
    except TerminalSpawnFailed as exc:
        if runtime.backend == "native":
            return await _settle_native_failure(
                manager=manager,
                runtime=runtime,
                terminal_id=terminal_id,
                spawn_key=spawn_key,
                exc=exc,
            )
        manager.fail_pending(terminal_id)
        return WebSpawnResult(False, terminal_id, str(exc), str(exc))
    except Exception as exc:
        if runtime.backend == "native":
            return await _settle_native_failure(
                manager=manager,
                runtime=runtime,
                terminal_id=terminal_id,
                spawn_key=spawn_key,
                exc=exc,
            )
        manager.fail_pending(terminal_id)
        return WebSpawnResult(False, terminal_id, str(exc), str(exc))

    stored = prepared.stored_locator or {}
    locator_key = prepared.locator_key or ""
    if runtime.backend == "native" and prepared.host_terminal_id is not None:
        process: dict[str, object] = {"host_terminal_id": prepared.host_terminal_id}
        if prepared.process is not None:
            process.update(
                {"pgid": prepared.process.pgid, "start_time": prepared.process.start_time}
            )
        manager.record_process(
            terminal_id,
            process,
            attempt_generation=attempt.attempt_generation,
            attempt_started_at=attempt.attempt_started_at,
        )
    prepared.acknowledge_persist()
    if runtime.backend == "native":
        bind = getattr(runtime, "bind_observer", None)
        try:
            if callable(bind) and request.reservation_id:
                await bind(prepared, request.reservation_id)
            else:
                prepared.acknowledge_observer()
        except Exception as exc:
            await kill_spawn_key(
                runtime,
                spawn_key,
                pending=manager.get(terminal_id),
                host_terminal_id=prepared.host_terminal_id,
                host_epoch=prepared.locator.frame_host_epoch if prepared.locator else None,
            )
            manager.fail_pending(terminal_id)
            code, detail, _settlement = classify_native_spawn_failure(exc)
            return WebSpawnResult(False, terminal_id, code, detail)
    try:
        handle = await runtime.commit_spawn(prepared)
    except asyncio.CancelledError as exc:
        if runtime.backend == "native":
            await _settle_native_failure(
                manager=manager,
                runtime=runtime,
                terminal_id=terminal_id,
                spawn_key=spawn_key,
                exc=exc,
                host_terminal_id=prepared.host_terminal_id,
                host_epoch=prepared.locator.frame_host_epoch if prepared.locator else None,
            )
        raise
    except CommitSpawnRefusedError as exc:
        if runtime.backend == "native":
            return await _settle_native_failure(
                manager=manager,
                runtime=runtime,
                terminal_id=terminal_id,
                spawn_key=spawn_key,
                exc=exc,
                host_terminal_id=prepared.host_terminal_id,
                host_epoch=prepared.locator.frame_host_epoch if prepared.locator else None,
            )
        manager.fail_pending(terminal_id)
        return WebSpawnResult(False, terminal_id, str(exc), str(exc))
    except Exception as exc:
        if runtime.backend != "native":
            raise
        return await _settle_native_failure(
            manager=manager,
            runtime=runtime,
            terminal_id=terminal_id,
            spawn_key=spawn_key,
            exc=exc,
            host_terminal_id=prepared.host_terminal_id,
            host_epoch=prepared.locator.frame_host_epoch if prepared.locator else None,
        )
    promoted = await settle_promotion(
        manager,
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
        await kill_spawn_key(
            runtime,
            spawn_key,
            pending=current,
            host_terminal_id=prepared.host_terminal_id,
            host_epoch=prepared.locator.frame_host_epoch if prepared.locator else None,
        )
        manager.fail_pending_attempt(
            terminal_id,
            attempt_generation=attempt.attempt_generation,
            attempt_started_at=attempt.attempt_started_at,
        )
        return WebSpawnResult(False, terminal_id, "lost_cas_conflict")
    if prepared.rows is not None and prepared.cols is not None:
        manager.set_dims(terminal_id, prepared.rows, prepared.cols)
    return WebSpawnResult(True, terminal_id)
