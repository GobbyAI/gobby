"""Unified spawn executor: row-owning TerminalRuntime dispatch."""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import TYPE_CHECKING, cast
from uuid import UUID, uuid4

from gobby.agents.spawn_executor_providers import (
    _CLAUDE_MANAGED_AGENT_DISALLOWED_TOOLS,
    _NATIVE_SUBAGENT_RESEARCH_AGENTS,
    ProviderSpawnPlan,
    _prepare_managed_code_index,
    agy_support_refusal,
    prepare_agy_spawn,
    prepare_claude_spawn,
    prepare_codex_spawn,
    prepare_droid_spawn,
    prepare_grok_spawn,
    prepare_qwen_spawn,
)
from gobby.agents.spawn_executor_support import (
    _CODEX_GOBBY_MCP_TOOL_TIMEOUT_SEC,
    _CODEX_PREAPPROVED_GOBBY_TOOLS,
    _apply_extra_env,
    _record_resume_launch_details,
    _unsupported_sandbox_request_error,
    schedule_codex_prompt_delivery,
)
from gobby.agents.spawn_models import SpawnRequest, SpawnResult
from gobby.agents.spawn_timing import (
    complete_spawn_phase_timings,
    finish_spawn_phase,
    start_spawn_phase,
)
from gobby.agents.srt_runtime import SandboxLaunch
from gobby.config.terminals import TerminalConfig
from gobby.storage.terminals import Terminal, TerminalManager, mint_terminal_id
from gobby.terminals import TerminalRuntimeRegistry, UnregisteredBackendError
from gobby.terminals.host_client import HostUnavailableError
from gobby.terminals.native_runtime import HostEpochMismatch, classify_native_spawn_failure
from gobby.terminals.runtime import (
    CommitSpawnRefusedError,
    TerminalRuntime,
    TerminalSpawnFailed,
    TerminalSpawnRequest,
    can_reserve_observer,
)
from gobby.terminals.runtime import (
    PreparedSpawn as RuntimePreparedSpawn,
)
from gobby.utils.datetime import utc_now

if TYPE_CHECKING:
    from gobby.agents.tmux.session_manager import TmuxSessionManager

logger = logging.getLogger(__name__)
_TIMEOUT_CLEANUP_TASKS: set[asyncio.Task[None]] = set()

__all__ = [
    "SpawnRequest",
    "SpawnResult",
    "execute_spawn",
    "reap_stale_pending_terminals",
    "wrap_provider_command",
    "_CLAUDE_MANAGED_AGENT_DISALLOWED_TOOLS",
    "_CODEX_PREAPPROVED_GOBBY_TOOLS",
    "_apply_extra_env",
    "_prepare_managed_code_index",
    "_record_resume_launch_details",
]

_COMPAT_PRIVATE_EXPORTS = (
    _CODEX_GOBBY_MCP_TOOL_TIMEOUT_SEC,
    _CODEX_PREAPPROVED_GOBBY_TOOLS,
    _CLAUDE_MANAGED_AGENT_DISALLOWED_TOOLS,
    _prepare_managed_code_index,
    _apply_extra_env,
    _record_resume_launch_details,
)


def wrap_provider_command(launch: SandboxLaunch, command: list[str]) -> list[str]:
    """Apply SRT wrap once, immediately before backend dispatch."""
    return launch.wrap(command)


def derive_spawn_key(backend: str, terminal_id: str) -> str:
    """Caller-owned backend identity. Native uses the UUID; tmux prefixes it."""
    if backend == "native":
        return terminal_id
    return f"gobby-{terminal_id}"


def resolve_terminal_services(
    request: SpawnRequest,
) -> tuple[TerminalManager, TerminalRuntimeRegistry, TerminalRuntime, str]:
    """Resolve composition-root services, falling back to a tmux singleton registry."""
    backend = request.terminal_backend
    manager = request.terminal_manager
    registry = request.terminal_runtime_registry
    if manager is None:
        db = getattr(getattr(request.session_manager, "_storage", None), "db", None)
        if db is None:
            raise RuntimeError("terminal_manager is required for spawn")
        manager = TerminalManager(db)
    if registry is None:
        from gobby.agents.tmux import get_tmux_session_manager
        from gobby.terminals.tmux_runtime import TmuxTerminalRuntime

        registry = TerminalRuntimeRegistry()
        registry.register(TmuxTerminalRuntime(get_tmux_session_manager()))
    runtime = registry.resolve(backend)
    return manager, registry, runtime, backend


def _default_backend(request: SpawnRequest) -> str:
    config = getattr(request.daemon_config, "terminals", None)
    if isinstance(config, TerminalConfig):
        return config.default_backend
    return TerminalConfig().default_backend


async def _settle_native_spawn_failure(
    *,
    manager: TerminalManager,
    runtime: TerminalRuntime,
    terminal_id: str,
    spawn_key: str,
    exc: BaseException,
    host_terminal_id: str | None = None,
    host_epoch: str | None = None,
) -> tuple[str, str | None]:
    code, detail, settlement = classify_native_spawn_failure(exc)
    if settlement == "fail_pending_kill":
        pending = await asyncio.to_thread(manager.get, terminal_id)
        mismatch = await kill_spawn_key(
            runtime,
            spawn_key,
            pending=pending,
            host_terminal_id=host_terminal_id,
            host_epoch=host_epoch,
        )
        if mismatch is not None:
            code, detail = "host_epoch_changed", str(mismatch)
    if settlement != "pending":
        await asyncio.to_thread(manager.fail_pending, terminal_id)
    return code, detail


def _spawn_in_doubt_seconds(request: SpawnRequest) -> float:
    config = getattr(request.daemon_config, "terminals", None)
    if isinstance(config, TerminalConfig):
        return float(config.spawn_in_doubt_seconds)
    return 150.0


async def execute_spawn(request: SpawnRequest) -> SpawnResult:
    """Unified spawn dispatch — all agents spawn via TerminalRuntime."""
    try:
        result = _unsupported_sandbox_request_error(request)
        if result is None:
            if (
                request.provider == "claude"
                and request.agent_name in _NATIVE_SUBAGENT_RESEARCH_AGENTS
            ):
                logger.warning(
                    "Agent %s requests provider-native internal subagents, but the managed "
                    "Claude runtime strips the native Task facility; internal research lanes "
                    "will be unavailable",
                    request.agent_name,
                )

            if request.provider == "grok":
                result = await _spawn_grok_terminal(request)
            elif request.provider == "qwen":
                result = await _spawn_qwen_terminal(request)
            elif request.provider == "codex":
                result = await _spawn_codex_terminal(request)
            elif request.provider == "droid":
                result = await _spawn_droid_terminal(request)
            elif request.provider == "agy":
                result = await _spawn_agy_terminal(request)
            elif request.provider == "claude":
                result = await _spawn_claude_terminal(request)
            else:
                result = SpawnResult(
                    success=False,
                    run_id=request.run_id,
                    child_session_id=None,
                    status="failed",
                    error=f"Unsupported spawn provider: {request.provider}",
                )

        return result
    finally:
        logger.info(
            "Spawn phase timings",
            extra={
                "run_id": request.run_id,
                "provider": request.provider,
                "phase_timings_ms": complete_spawn_phase_timings(request.phase_timings_ms),
            },
        )


async def _spawn_claude_terminal(request: SpawnRequest) -> SpawnResult:
    plan = await prepare_claude_spawn(request)
    if isinstance(plan, SpawnResult):
        return plan
    return await _runtime_spawn(request, plan)


async def _spawn_qwen_terminal(request: SpawnRequest) -> SpawnResult:
    plan = await prepare_qwen_spawn(request)
    if isinstance(plan, SpawnResult):
        return plan
    return await _runtime_spawn(request, plan)


async def _spawn_grok_terminal(request: SpawnRequest) -> SpawnResult:
    plan = await prepare_grok_spawn(request)
    if isinstance(plan, SpawnResult):
        return plan
    return await _runtime_spawn(request, plan)


async def _spawn_codex_terminal(request: SpawnRequest) -> SpawnResult:
    plan = await prepare_codex_spawn(request)
    if isinstance(plan, SpawnResult):
        return plan
    result = await _runtime_spawn(request, plan)
    if result.success and plan.codex_prompt:
        if plan.inject_persona and request.session_manager is not None:
            from gobby.workflows.state_manager import SessionVariableManager

            await asyncio.to_thread(
                SessionVariableManager(request.session_manager._storage.db).merge_variables,
                plan.child_session_id,
                {"_agent_context_injected": True},
            )
        coordinator = request.write_coordinator
        manager = request.terminal_manager
        if result.terminal_id and coordinator is not None and manager is not None:
            terminal = await asyncio.to_thread(manager.get, result.terminal_id)
            if terminal is not None:
                schedule_codex_prompt_delivery(
                    coordinator,
                    terminal,
                    plan.codex_prompt,
                    plan.agent_run_id,
                    request.run_manager,
                )
    return result


async def _spawn_droid_terminal(request: SpawnRequest) -> SpawnResult:
    if shutil.which("droid") is None:
        return SpawnResult(
            success=False,
            run_id=request.run_id,
            child_session_id=None,
            status="failed",
            error=(
                "droid CLI not found in PATH. Install droid first: "
                "see docs/cli-integrations/droid.md"
            ),
        )
    plan = await prepare_droid_spawn(request)
    if isinstance(plan, SpawnResult):
        return plan
    return await _runtime_spawn(request, plan)


async def _spawn_agy_terminal(request: SpawnRequest) -> SpawnResult:
    """Dispatch a supported AGY spawn; refuse from the 2.5 record before any side effect."""
    from gobby.providers.version_gate import ensure_agy_support

    record = await ensure_agy_support()
    if not record.supported:
        return SpawnResult(
            success=False,
            run_id=request.run_id,
            child_session_id=None,
            status="failed",
            error=agy_support_refusal(record),
        )
    plan = await prepare_agy_spawn(request)
    if isinstance(plan, SpawnResult):
        return plan
    return await _runtime_spawn(request, plan)


def _tmux_sessions_from_request(request: SpawnRequest) -> TmuxSessionManager | None:
    registry = request.terminal_runtime_registry
    if registry is None:
        return None
    try:
        runtime = registry.resolve("tmux")
    except UnregisteredBackendError:
        return None
    sessions = getattr(runtime, "_sessions", None)
    return sessions


def _persist_spawn_workspace(request: SpawnRequest, session_id: str) -> None:
    """Record the spawn cwd as the child session's canonical workspace identity."""
    storage = getattr(request.session_manager, "_storage", None)
    if storage is None or not request.cwd:
        return
    current = storage.get(session_id) if hasattr(storage, "get") else None
    existing = getattr(current, "workspace_path", None) if current is not None else None
    if existing == request.cwd:
        return
    generation = 0
    if current is not None:
        generation = int(getattr(current, "workspace_generation", 0) or 0)
    storage.update(
        session_id,
        workspace_path=request.cwd,
        workspace_generation=generation + 1,
    )


async def _runtime_spawn(request: SpawnRequest, plan: ProviderSpawnPlan) -> SpawnResult:
    """Sole pending-row owner: wrap, create/retry, prepare_spawn, promote_to_live."""
    if request.managed_runtime_profile is not None:
        await asyncio.to_thread(
            request.managed_runtime_profile.validate_launch,
            backend=plan.launch.backend,
            enforced=plan.launch.enforced,
            provider_executable=plan.launch.provider_executable,
            runtime_version=plan.launch.runtime_version,
            policy_schema_version=plan.launch.policy_schema_version,
            policy_hash=plan.launch.policy_hash,
            policy_path=plan.launch.policy_path,
            environment={**plan.env, **plan.launch.provider_env},
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
        if existing.state != "pending":
            return SpawnResult(
                success=False,
                run_id=plan.agent_run_id,
                child_session_id=plan.child_session_id,
                status="failed",
                error="retry_terminal_not_pending",
                terminal_id=existing.id,
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
                terminal_id=existing.id,
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


async def settle_promotion(
    manager: TerminalManager,
    terminal_id: str,
    *,
    locator: Mapping[str, object],
    locator_key: str,
    host_epoch: str | None = None,
    session_name: str | None = None,
    window_id: str | None = None,
    title: str | None = None,
) -> Terminal | None:
    """Serialize promotion with exit settlement for one durable row."""
    async with manager.settle_lock(terminal_id):
        return await asyncio.to_thread(
            manager.promote_to_live,
            terminal_id,
            locator=locator,
            locator_key=locator_key,
            host_epoch=host_epoch,
            session_name=session_name,
            window_id=window_id,
            title=title,
        )


def _tmux_duplicate_session_error(exc: BaseException) -> bool:
    message = str(exc).casefold()
    return "duplicate" in message or "already exists" in message


async def _cleanup_timed_out_prepare(
    prepare_task: asyncio.Task[RuntimePreparedSpawn],
    *,
    manager: TerminalManager,
    runtime: TerminalRuntime,
    backend: str,
    terminal_id: str,
    spawn_key: str,
    attempt_generation: int,
    attempt_started_at: datetime,
) -> None:
    try:
        prepared = prepare_task.result()
    except asyncio.CancelledError:
        return
    except Exception as exc:
        if backend == "tmux" and _tmux_duplicate_session_error(exc):
            pending = await asyncio.to_thread(manager.get, terminal_id)
            await kill_spawn_key(runtime, spawn_key, pending=pending)
        await asyncio.to_thread(
            manager.fail_pending_attempt,
            terminal_id,
            attempt_generation=attempt_generation,
            attempt_started_at=attempt_started_at,
        )
        return

    if backend == "native":
        host_terminal_id = prepared.host_terminal_id
        if host_terminal_id is not None:
            try:
                await kill_spawn_key(
                    runtime,
                    spawn_key,
                    pending=None,
                    host_terminal_id=host_terminal_id,
                    host_epoch=prepared.locator.frame_host_epoch if prepared.locator else None,
                )
            except HostUnavailableError:
                return
    else:
        pending = await asyncio.to_thread(manager.get, terminal_id)
        await kill_spawn_key(runtime, spawn_key, pending=pending)
    await asyncio.to_thread(
        manager.fail_pending_attempt,
        terminal_id,
        attempt_generation=attempt_generation,
        attempt_started_at=attempt_started_at,
    )


def _schedule_timeout_cleanup(
    prepare_task: asyncio.Task[RuntimePreparedSpawn],
    *,
    manager: TerminalManager,
    runtime: TerminalRuntime,
    backend: str,
    terminal_id: str,
    spawn_key: str,
    attempt_generation: int,
    attempt_started_at: datetime,
) -> None:
    def schedule(completed: asyncio.Task[RuntimePreparedSpawn]) -> None:
        task = asyncio.create_task(
            _cleanup_timed_out_prepare(
                completed,
                manager=manager,
                runtime=runtime,
                backend=backend,
                terminal_id=terminal_id,
                spawn_key=spawn_key,
                attempt_generation=attempt_generation,
                attempt_started_at=attempt_started_at,
            )
        )
        _TIMEOUT_CLEANUP_TASKS.add(task)
        task.add_done_callback(_finish_timeout_cleanup)

    prepare_task.add_done_callback(schedule)


def _finish_timeout_cleanup(task: asyncio.Task[None]) -> None:
    _TIMEOUT_CLEANUP_TASKS.discard(task)
    if task.cancelled():
        return
    try:
        task.result()
    except Exception:
        logger.warning("Timed-out terminal cleanup failed", exc_info=True)


def _same_live_identity(
    current: Terminal | None,
    backend: str,
    locator_key: str,
) -> bool:
    if current is None or current.state != "live":
        return False
    return current.backend == backend and current.locator_key == locator_key


def _terminal_for_spawn_key(
    backend: str,
    spawn_key: str,
    pending: Terminal | None,
) -> Terminal:
    if pending is not None and pending.spawn_key == spawn_key and pending.state == "pending":
        return pending
    now = utc_now()
    return Terminal(
        id=str(uuid4()),
        backend=backend,
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
        session_name=spawn_key if backend == "tmux" else None,
    )


async def kill_spawn_key(
    runtime: TerminalRuntime,
    spawn_key: str,
    *,
    pending: Terminal | None,
    host_terminal_id: str | None = None,
    host_epoch: str | None = None,
) -> HostEpochMismatch | None:
    if runtime.backend == "native" and host_terminal_id is not None:
        terminate_host_id = getattr(runtime, "terminate_host_id", None)
        if not callable(terminate_host_id):
            raise RuntimeError("native runtime does not support terminate_host_id")
        terminate = cast(
            Callable[[str, str | None], Awaitable[HostEpochMismatch | None]],
            terminate_host_id,
        )
        return await terminate(host_terminal_id, host_epoch)
    terminal = _terminal_for_spawn_key(runtime.backend, spawn_key, pending)
    if host_terminal_id:
        terminal.locator = {**(terminal.locator or {}), "host_terminal_id": host_terminal_id}
    try:
        await runtime.terminate(terminal, 1.0)
    except Exception:
        logger.debug("spawn_key terminate failed for %s", spawn_key, exc_info=True)
    return None


async def reap_stale_pending_terminals(
    manager: TerminalManager,
    runtime_registry: TerminalRuntimeRegistry,
    *,
    in_doubt_seconds: float,
    now: datetime | None = None,
) -> list[str]:
    """Reap pending rows older than the in-doubt deadline with no backend resource.

    `now` is accepted so tests can name the observed clock; selection uses
    attempt_started_at via TerminalManager.list_stale_pending.
    """
    del now
    reaped: list[str] = []
    for row in manager.list_stale_pending(in_doubt_seconds):
        runtime = runtime_registry.resolve(row.backend)
        if row.backend == "native":
            process = row.process or {}
            host_terminal_id = process.get("host_terminal_id")
            if not isinstance(host_terminal_id, str) or not host_terminal_id:
                result = manager.fail_pending_attempt(
                    row.id,
                    attempt_generation=row.attempt_generation,
                    attempt_started_at=row.attempt_started_at,
                )
                if result is not None:
                    reaped.append(row.id)
                continue
            try:
                await kill_spawn_key(
                    runtime,
                    row.spawn_key or row.id,
                    pending=row,
                    host_terminal_id=host_terminal_id,
                    host_epoch=row.host_epoch,
                )
            except HostUnavailableError:
                continue
        elif row.spawn_key:
            await kill_spawn_key(runtime, row.spawn_key, pending=row)
        result = manager.fail_pending_attempt(
            row.id,
            attempt_generation=row.attempt_generation,
            attempt_started_at=row.attempt_started_at,
        )
        if result is not None:
            reaped.append(row.id)
    return reaped
