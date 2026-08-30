"""Unified spawn executor: row-owning TerminalRuntime dispatch."""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from gobby.agents.sandbox_resolvers import get_sandbox_resolver as get_sandbox_resolver
from gobby.agents.spawn import (
    prepare_terminal_spawn as prepare_terminal_spawn,
)
from gobby.agents.spawn_cache_policy import (
    sandbox_config_for_spawn as _sandbox_config_for_spawn,
)
from gobby.agents.spawn_executor_providers import (
    _CLAUDE_MANAGED_AGENT_DISALLOWED_TOOLS,
    _NATIVE_SUBAGENT_RESEARCH_AGENTS,
    ProviderSpawnPlan,
    _prepare_managed_code_index,
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
from gobby.agents.srt_runtime import SandboxLaunch
from gobby.agents.trust import pre_approve_directory as pre_approve_directory
from gobby.config.terminals import TerminalConfig
from gobby.providers import AGY_UNAVAILABLE_REASON
from gobby.providers.capabilities.apply import speed_result
from gobby.storage.terminals import Terminal, TerminalManager, mint_terminal_id
from gobby.terminals import TerminalRuntimeRegistry, UnregisteredBackendError
from gobby.terminals.host_client import HostCommandError
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
    "_sandbox_config_for_spawn",
]

_COMPAT_PRIVATE_EXPORTS = (
    _CODEX_GOBBY_MCP_TOOL_TIMEOUT_SEC,
    _CODEX_PREAPPROVED_GOBBY_TOOLS,
    _CLAUDE_MANAGED_AGENT_DISALLOWED_TOOLS,
    _prepare_managed_code_index,
    _apply_extra_env,
    _record_resume_launch_details,
    _sandbox_config_for_spawn,
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


def _spawn_in_doubt_seconds(request: SpawnRequest) -> float:
    config = getattr(request.daemon_config, "terminals", None)
    if isinstance(config, TerminalConfig):
        return float(config.spawn_in_doubt_seconds)
    return 150.0


async def execute_spawn(request: SpawnRequest) -> SpawnResult:
    """Unified spawn dispatch — all agents spawn via TerminalRuntime."""
    result = _unsupported_sandbox_request_error(request)
    if result is None:
        if request.provider == "claude" and request.agent_name in _NATIVE_SUBAGENT_RESEARCH_AGENTS:
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
            from gobby.providers.version_gate import ensure_agy_support

            record = await ensure_agy_support()
            result = SpawnResult(
                success=False,
                run_id=request.run_id,
                child_session_id=None,
                status="failed",
                error=record.reason if not record.supported else AGY_UNAVAILABLE_REASON,
            )
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

    if request.speed_resolution is not None:
        result = replace(result, speed=speed_result(request.speed_resolution))
    return result


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

            SessionVariableManager(request.session_manager._storage.db).merge_variables(
                plan.child_session_id,
                {"_agent_context_injected": True},
            )
        coordinator = request.write_coordinator
        manager = request.terminal_manager
        if result.terminal_id and coordinator is not None and manager is not None:
            terminal = manager.get(result.terminal_id)
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


async def _runtime_spawn(request: SpawnRequest, plan: ProviderSpawnPlan) -> SpawnResult:
    """Sole pending-row owner: wrap, create/retry, prepare_spawn, promote_to_live."""
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
        existing = manager.get(request.retry_terminal_id)
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
        bumped = manager.bump_attempt_generation(existing.id)
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
        manager.create_pending(
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

    if request.cancel_event is not None and request.cancel_event.is_set():
        manager.fail_pending(terminal_id)
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
            manager.fail_pending(terminal_id)
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
        except HostCommandError as exc:
            manager.fail_pending(terminal_id)
            return SpawnResult(
                success=False,
                run_id=plan.agent_run_id,
                child_session_id=plan.child_session_id,
                status="failed",
                error=str(exc),
                terminal_id=terminal_id,
            )
        spawn_request.reservation_id = reservation.get("reservation_id")
        spawn_request.reserve_key = reservation.get("reserve_key")

    prepare_task = asyncio.create_task(runtime.prepare_spawn(spawn_request))
    timeout = request.timeout_seconds
    try:
        if timeout is not None:
            prepared = await asyncio.wait_for(asyncio.shield(prepare_task), timeout=timeout)
        else:
            prepared = await asyncio.shield(prepare_task)
    except TimeoutError:
        await kill_spawn_key(runtime, spawn_key, pending=manager.get(terminal_id))
        return SpawnResult(
            success=False,
            run_id=plan.agent_run_id,
            child_session_id=plan.child_session_id,
            status="failed",
            error="spawn timed out",
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
        manager.fail_pending(terminal_id)
        return SpawnResult(
            success=False,
            run_id=plan.agent_run_id,
            child_session_id=plan.child_session_id,
            status="failed",
            error=str(exc),
            terminal_id=terminal_id,
        )
    except Exception as exc:
        logger.exception("Backend spawn raised for terminal %s", terminal_id)
        return SpawnResult(
            success=False,
            run_id=plan.agent_run_id,
            child_session_id=plan.child_session_id,
            status="failed",
            error=str(exc),
            terminal_id=terminal_id,
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
) -> SpawnResult:
    if prepared.process is not None:
        manager.record_process(
            terminal_id,
            {"pgid": prepared.process.pgid, "start_time": prepared.process.start_time},
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
            await kill_spawn_key(
                runtime,
                spawn_key,
                pending=manager.get(terminal_id),
                host_terminal_id=prepared.host_terminal_id,
            )
            manager.fail_pending(terminal_id)
            return SpawnResult(
                success=False,
                run_id=plan.agent_run_id,
                child_session_id=plan.child_session_id,
                status="failed",
                error=str(exc),
                terminal_id=terminal_id,
            )
    try:
        handle = await runtime.commit_spawn(prepared)
    except CommitSpawnRefusedError as exc:
        return SpawnResult(
            success=False,
            run_id=plan.agent_run_id,
            child_session_id=plan.child_session_id,
            status="failed",
            error=str(exc),
            terminal_id=terminal_id,
        )

    promoted = manager.promote_to_live(
        terminal_id,
        locator=stored,
        locator_key=locator_key,
        session_name=spawn_key if backend == "tmux" else None,
        title=plan.title,
        host_epoch=None if backend == "tmux" else getattr(handle.locator, "frame_host_epoch", None),
    )
    if promoted is None:
        current = manager.get(terminal_id)
        if _same_live_identity(current, backend, locator_key):
            promoted = current
        else:
            await kill_spawn_key(runtime, spawn_key, pending=current)
            return SpawnResult(
                success=False,
                run_id=plan.agent_run_id,
                child_session_id=plan.child_session_id,
                status="failed",
                error="lost_cas_conflict",
                terminal_id=terminal_id,
            )

    pid = prepared.pid
    process = prepared.process
    if pid is None and process is not None:
        pid = process.pgid
    if request.run_manager is not None:
        try:
            request.run_manager.update_runtime(
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
        terminal_type=backend,
        terminal_id=terminal_id,
        locator=handle.locator,
        tmux_session_name=spawn_key if backend == "tmux" else None,
        message=f"{plan.auth_cli} agent spawned with session {plan.child_session_id}",
    )


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
) -> None:
    terminal = _terminal_for_spawn_key(runtime.backend, spawn_key, pending)
    if host_terminal_id:
        terminal.locator = {**(terminal.locator or {}), "host_terminal_id": host_terminal_id}
    try:
        await runtime.terminate(terminal, 1.0)
    except Exception:
        logger.debug("spawn_key terminate failed for %s", spawn_key, exc_info=True)


async def reap_stale_pending_terminals(
    manager: TerminalManager,
    runtime: TerminalRuntime,
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
        if row.spawn_key:
            try:
                if await runtime.is_live(row):
                    continue
            except Exception:
                logger.debug("is_live failed during reap of %s", row.id, exc_info=True)
        result = manager.fail_pending_attempt(
            row.id,
            attempt_generation=row.attempt_generation,
            attempt_started_at=row.attempt_started_at,
        )
        if result is not None:
            reaped.append(row.id)
    return reaped
