"""Runtime spawn path: prepare, promote and settle one terminal attempt."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from uuid import UUID

from gobby.agents.spawn_executor_providers import ProviderSpawnPlan
from gobby.agents.spawn_models import SpawnRequest, SpawnResult, is_infrastructure_spawn_error
from gobby.agents.spawn_timing import finish_spawn_phase, start_spawn_phase
from gobby.storage.terminals import TerminalManager, mint_terminal_id
from gobby.terminals import UnregisteredBackendError
from gobby.terminals.native_runtime import classify_native_spawn_failure
from gobby.terminals.runtime import (
    CommitSpawnRefusedError,
    TerminalRuntime,
    TerminalSpawnFailed,
    TerminalSpawnRequest,
    can_reserve_observer,
)
from gobby.terminals.runtime import PreparedSpawn as RuntimePreparedSpawn

logger = logging.getLogger(__name__)


async def _runtime_spawn(request: SpawnRequest, plan: ProviderSpawnPlan) -> SpawnResult:
    """Sole pending-row owner: wrap, create/retry, prepare_spawn, promote_to_live."""
    from gobby.agents.spawn_executor import (
        _persist_spawn_workspace,
        _schedule_timeout_cleanup,
        _settle_native_spawn_failure,
        _tmux_duplicate_session_error,
        derive_spawn_key,
        kill_spawn_key,
        resolve_terminal_services,
        wrap_provider_command,
    )

    await asyncio.to_thread(_persist_spawn_workspace, request, plan.child_session_id)
    command = wrap_provider_command(plan.launch, plan.command)
    try:
        manager, _registry, runtime, backend = resolve_terminal_services(request)
    except (RuntimeError, UnregisteredBackendError) as exc:
        return SpawnResult(
            success=False,
            run_id=plan.agent_run_id,
            child_session_id=plan.child_session_id,
            status="failed",
            error=str(exc),
        )

    if request.cancel_event is not None and request.cancel_event.is_set():
        return SpawnResult(
            success=False,
            run_id=plan.agent_run_id,
            child_session_id=plan.child_session_id,
            status="cancelled",
            error="cancelled",
        )

    if request.retry_terminal_id:
        existing = await asyncio.to_thread(manager.get, request.retry_terminal_id)
        if existing is None:
            return SpawnResult(
                success=False,
                run_id=plan.agent_run_id,
                child_session_id=plan.child_session_id,
                status="failed",
                error="retry_terminal_missing",
            )
        # A refused retry never controlled the row, so it reports no terminal:
        # failure cleanup and the run row must not claim another attempt's terminal.
        if existing.state != "pending":
            return SpawnResult(
                success=False,
                run_id=plan.agent_run_id,
                child_session_id=plan.child_session_id,
                status="failed",
                error="retry_terminal_not_pending",
            )
        bumped = await asyncio.to_thread(
            manager.retry_attempt_unsettled,
            existing.id,
            existing.attempt_generation,
        )
        if bumped is None:
            return SpawnResult(
                success=False,
                run_id=plan.agent_run_id,
                child_session_id=plan.child_session_id,
                status="failed",
                error="retry_generation_cas_failed",
            )
        terminal_id = existing.id
        spawn_key = existing.spawn_key or derive_spawn_key(backend, terminal_id)
    else:
        terminal_id = mint_terminal_id()
        spawn_key = derive_spawn_key(backend, terminal_id)
        bumped = await asyncio.to_thread(
            manager.create_pending,
            terminal_id,
            request.project_id,
            backend,
            "gobby",
            spawn_key,
            machine_id=request.machine_id,
            session_id=plan.child_session_id,
            agent_run_id=plan.agent_run_id,
            title=plan.title,
        )

    attempt_generation = bumped.attempt_generation
    attempt_started_at = bumped.attempt_started_at

    if request.cancel_event is not None and request.cancel_event.is_set():
        await asyncio.to_thread(manager.fail_pending, terminal_id)
        return SpawnResult(
            success=False,
            run_id=plan.agent_run_id,
            child_session_id=plan.child_session_id,
            status="cancelled",
            error="cancelled",
            terminal_id=terminal_id,
        )

    spawn_request = TerminalSpawnRequest(
        terminal_id=UUID(terminal_id),
        spawn_key=spawn_key,
        command=command,
        cwd=request.cwd,
        env=plan.env,
        title=plan.title,
        auth_cli=plan.auth_cli,
    )
    if backend == "native":
        if not can_reserve_observer(runtime):
            await asyncio.to_thread(manager.fail_pending, terminal_id)
            return SpawnResult(
                success=False,
                run_id=plan.agent_run_id,
                child_session_id=plan.child_session_id,
                status="failed",
                error="native_reserve_unavailable",
                terminal_id=terminal_id,
            )
        try:
            reservation = await runtime.reserve_observer(UUID(terminal_id))
        except Exception as exc:
            code, detail = await _settle_native_spawn_failure(
                manager=manager,
                runtime=runtime,
                terminal_id=terminal_id,
                spawn_key=spawn_key,
                exc=exc,
            )
            return SpawnResult(
                success=False,
                run_id=plan.agent_run_id,
                child_session_id=plan.child_session_id,
                status="failed",
                error=code,
                error_detail=detail,
                terminal_id=terminal_id,
            )
        spawn_request.reservation_id = reservation.get("reservation_id")
        spawn_request.reserve_key = reservation.get("reserve_key")

    runtime_prepare_started = start_spawn_phase()
    prepare_task = asyncio.create_task(runtime.prepare_spawn(spawn_request))
    timeout = request.timeout_seconds
    try:
        if timeout is not None:
            prepared = await asyncio.wait_for(asyncio.shield(prepare_task), timeout=timeout)
        else:
            prepared = await asyncio.shield(prepare_task)
    except TimeoutError:
        _schedule_timeout_cleanup(
            prepare_task,
            manager=manager,
            runtime=runtime,
            backend=backend,
            terminal_id=terminal_id,
            spawn_key=spawn_key,
            attempt_generation=attempt_generation,
            attempt_started_at=attempt_started_at,
        )
        return SpawnResult(
            success=False,
            run_id=plan.agent_run_id,
            child_session_id=plan.child_session_id,
            status="failed",
            error="spawn_timeout" if backend == "native" else "spawn timed out",
            retryable_infrastructure=True,
            error_detail="spawn timed out" if backend == "native" else None,
            terminal_id=terminal_id,
        )
    except asyncio.CancelledError:
        if not prepare_task.done():
            try:
                await asyncio.shield(prepare_task)
            except Exception:
                logger.debug("Post-dispatch cancel left spawn pending", exc_info=True)
        return SpawnResult(
            success=False,
            run_id=plan.agent_run_id,
            child_session_id=plan.child_session_id,
            status="cancelled",
            error="cancelled",
            terminal_id=terminal_id,
        )
    except TerminalSpawnFailed as exc:
        if backend == "native":
            code, detail = await _settle_native_spawn_failure(
                manager=manager,
                runtime=runtime,
                terminal_id=terminal_id,
                spawn_key=spawn_key,
                exc=exc,
            )
        else:
            if request.retry_terminal_id and _tmux_duplicate_session_error(exc):
                pending = await asyncio.to_thread(manager.get, terminal_id)
                await kill_spawn_key(runtime, spawn_key, pending=pending)
            await asyncio.to_thread(
                manager.fail_pending_attempt,
                terminal_id,
                attempt_generation=attempt_generation,
                attempt_started_at=attempt_started_at,
            )
            code, detail = str(exc), None
        return SpawnResult(
            success=False,
            run_id=plan.agent_run_id,
            child_session_id=plan.child_session_id,
            status="failed",
            error=code,
            error_detail=detail,
            terminal_id=terminal_id,
            retryable_infrastructure=is_infrastructure_spawn_error(exc),
        )
    except Exception as exc:
        logger.exception("Backend spawn raised for terminal %s", terminal_id)
        if backend == "native":
            code, detail = await _settle_native_spawn_failure(
                manager=manager,
                runtime=runtime,
                terminal_id=terminal_id,
                spawn_key=spawn_key,
                exc=exc,
            )
        else:
            code, detail = str(exc), None
        return SpawnResult(
            success=False,
            run_id=plan.agent_run_id,
            child_session_id=plan.child_session_id,
            status="failed",
            error=code,
            error_detail=detail,
            terminal_id=terminal_id,
            retryable_infrastructure=is_infrastructure_spawn_error(exc),
        )
    finally:
        finish_spawn_phase(
            request.phase_timings_ms,
            "runtime_prepare_spawn",
            runtime_prepare_started,
        )

    return await _promote_prepared(
        request,
        plan,
        manager=manager,
        runtime=runtime,
        backend=backend,
        terminal_id=terminal_id,
        spawn_key=spawn_key,
        prepared=prepared,
        reservation_id=spawn_request.reservation_id,
        attempt_generation=attempt_generation,
        attempt_started_at=attempt_started_at,
    )


async def _promote_prepared(
    request: SpawnRequest,
    plan: ProviderSpawnPlan,
    *,
    manager: TerminalManager,
    runtime: TerminalRuntime,
    backend: str,
    terminal_id: str,
    spawn_key: str,
    prepared: RuntimePreparedSpawn,
    reservation_id: str | None = None,
    attempt_generation: int,
    attempt_started_at: datetime,
) -> SpawnResult:
    from gobby.agents.spawn_executor import (
        _same_live_identity,
        _settle_native_spawn_failure,
        kill_spawn_key,
        settle_promotion,
    )

    if backend == "native" and prepared.host_terminal_id is not None:
        process_record: dict[str, object] = {"host_terminal_id": prepared.host_terminal_id}
        if prepared.process is not None:
            process_record.update(
                {"pgid": prepared.process.pgid, "start_time": prepared.process.start_time}
            )
        await asyncio.to_thread(
            manager.record_process,
            terminal_id,
            process_record,
            attempt_generation=attempt_generation,
            attempt_started_at=attempt_started_at,
        )
    stored = prepared.stored_locator or {}
    locator_key = prepared.locator_key or ""
    prepared.acknowledge_persist()
    if backend == "native":
        bind = getattr(runtime, "bind_observer", None)
        try:
            if callable(bind) and reservation_id:
                await bind(prepared, reservation_id)
            else:
                prepared.acknowledge_observer()
        except Exception as exc:
            pending = await asyncio.to_thread(manager.get, terminal_id)
            await kill_spawn_key(
                runtime,
                spawn_key,
                pending=pending,
                host_terminal_id=prepared.host_terminal_id,
                host_epoch=prepared.locator.frame_host_epoch if prepared.locator else None,
            )
            await asyncio.to_thread(manager.fail_pending, terminal_id)
            code, detail, _settlement = classify_native_spawn_failure(exc)
            return SpawnResult(
                success=False,
                run_id=plan.agent_run_id,
                child_session_id=plan.child_session_id,
                status="failed",
                error=code,
                error_detail=detail,
                terminal_id=terminal_id,
            )
    try:
        handle = await runtime.commit_spawn(prepared)
    except asyncio.CancelledError as exc:
        if backend == "native":
            await _settle_native_spawn_failure(
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
        if backend == "native":
            code, detail = await _settle_native_spawn_failure(
                manager=manager,
                runtime=runtime,
                terminal_id=terminal_id,
                spawn_key=spawn_key,
                exc=exc,
                host_terminal_id=prepared.host_terminal_id,
                host_epoch=prepared.locator.frame_host_epoch if prepared.locator else None,
            )
        else:
            await asyncio.to_thread(manager.fail_pending, terminal_id)
            code, detail = str(exc), None
        return SpawnResult(
            success=False,
            run_id=plan.agent_run_id,
            child_session_id=plan.child_session_id,
            status="failed",
            error=code,
            error_detail=detail,
            terminal_id=terminal_id,
        )
    except Exception as exc:
        if backend != "native":
            raise
        code, detail = await _settle_native_spawn_failure(
            manager=manager,
            runtime=runtime,
            terminal_id=terminal_id,
            spawn_key=spawn_key,
            exc=exc,
            host_terminal_id=prepared.host_terminal_id,
            host_epoch=prepared.locator.frame_host_epoch if prepared.locator else None,
        )
        return SpawnResult(
            success=False,
            run_id=plan.agent_run_id,
            child_session_id=plan.child_session_id,
            status="failed",
            error=code,
            error_detail=detail,
            terminal_id=terminal_id,
        )

    promoted = await settle_promotion(
        manager,
        terminal_id,
        locator=stored,
        locator_key=locator_key,
        session_name=spawn_key if backend == "tmux" else None,
        title=plan.title,
        host_epoch=None if backend == "tmux" else getattr(handle.locator, "frame_host_epoch", None),
    )
    if promoted is None:
        current = await asyncio.to_thread(manager.get, terminal_id)
        if _same_live_identity(current, backend, locator_key):
            promoted = current
        else:
            await kill_spawn_key(
                runtime,
                spawn_key,
                pending=current,
                host_terminal_id=prepared.host_terminal_id,
                host_epoch=prepared.locator.frame_host_epoch if prepared.locator else None,
            )
            return SpawnResult(
                success=False,
                run_id=plan.agent_run_id,
                child_session_id=plan.child_session_id,
                status="failed",
                error="lost_cas_conflict",
                terminal_id=terminal_id,
            )

    if prepared.rows is not None and prepared.cols is not None:
        await asyncio.to_thread(manager.set_dims, terminal_id, prepared.rows, prepared.cols)

    pid = prepared.pid
    process = prepared.process
    if pid is None and process is not None:
        pid = process.pgid
    if request.run_manager is not None:
        try:
            await asyncio.to_thread(
                request.run_manager.update_runtime,
                plan.agent_run_id,
                pid=pid,
                terminal_id=terminal_id,
            )
        except Exception:
            logger.warning(
                "Failed to persist terminal_id for run %s", plan.agent_run_id, exc_info=True
            )
    return SpawnResult(
        success=True,
        run_id=plan.agent_run_id,
        child_session_id=plan.child_session_id,
        status="pending",
        pid=pid,
        backend=backend,
        terminal_id=terminal_id,
        locator=handle.locator,
        message=f"{plan.auth_cli} agent spawned with session {plan.child_session_id}",
    )
