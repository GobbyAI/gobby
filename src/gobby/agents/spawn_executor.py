"""Unified spawn executor: row-owning TerminalRuntime dispatch."""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from gobby.agents.spawn_executor_codex import _spawn_codex_terminal
from gobby.agents.spawn_executor_providers import (
    _prepare_managed_code_index,
    agy_support_refusal,
    prepare_agy_spawn,
    prepare_claude_spawn,
    prepare_droid_spawn,
    prepare_grok_spawn,
    prepare_qwen_spawn,
)

# Explicit re-exports: the facade stays the patch and import target for both.
from gobby.agents.spawn_executor_runtime import _promote_prepared as _promote_prepared
from gobby.agents.spawn_executor_runtime import _runtime_spawn as _runtime_spawn
from gobby.agents.spawn_executor_support import (
    _CODEX_GOBBY_MCP_TOOL_TIMEOUT_SEC,
    _CODEX_PREAPPROVED_GOBBY_TOOLS,
    _apply_extra_env,
    _record_resume_launch_details,
    _unsupported_sandbox_request_error,
)
from gobby.agents.spawn_models import SpawnRequest, SpawnResult
from gobby.agents.spawn_timing import (
    complete_spawn_phase_timings,
)
from gobby.agents.srt_runtime import SandboxLaunch
from gobby.config.terminals import TerminalConfig
from gobby.storage.terminals import Terminal, TerminalManager
from gobby.terminals import TerminalRuntimeRegistry
from gobby.terminals.host_client import HostUnavailableError
from gobby.terminals.host_reap import reap_recorded_group_proven_dead
from gobby.terminals.in_doubt import in_doubt_spawns
from gobby.terminals.native_runtime import HostEpochMismatch, classify_native_spawn_failure
from gobby.terminals.runtime import (
    PreparedSpawn as RuntimePreparedSpawn,
)
from gobby.terminals.runtime import (
    TerminalRuntime,
)
from gobby.utils.datetime import utc_now

if TYPE_CHECKING:
    from gobby.agents.spawn_in_doubt_owner import OwnerStage

logger = logging.getLogger(__name__)
_TIMEOUT_CLEANUP_TASKS: set[asyncio.Task[None]] = set()
# Stale-pending reaps that own an in-doubt claim until their kill settles.
_REAP_TASKS: set[asyncio.Task[bool]] = set()
_SLOW_SPAWN_THRESHOLD_MS = 1_000.0

__all__ = [
    "SpawnRequest",
    "SpawnResult",
    "execute_spawn",
    "reap_stale_pending_terminals",
    "wrap_provider_command",
    "_CODEX_PREAPPROVED_GOBBY_TOOLS",
    "_apply_extra_env",
    "_prepare_managed_code_index",
    "_record_resume_launch_details",
]

_COMPAT_PRIVATE_EXPORTS = (
    _CODEX_GOBBY_MCP_TOOL_TIMEOUT_SEC,
    _CODEX_PREAPPROVED_GOBBY_TOOLS,
    _prepare_managed_code_index,
    _apply_extra_env,
    _record_resume_launch_details,
)


def wrap_provider_command(launch: SandboxLaunch, command: list[str]) -> list[str]:
    """Apply SRT wrap once, immediately before backend dispatch."""
    return launch.wrap(command)


def resolve_terminal_services(
    request: SpawnRequest,
) -> tuple[TerminalManager, TerminalRuntimeRegistry, TerminalRuntime, str]:
    """Resolve the composition-root services for the requested backend."""
    backend = request.terminal_backend
    manager = request.terminal_manager
    registry = request.terminal_runtime_registry
    if manager is None:
        db = getattr(getattr(request.session_manager, "_storage", None), "db", None)
        if db is None:
            raise RuntimeError("terminal_manager is required for spawn")
        manager = TerminalManager(db)
    if registry is None:
        raise RuntimeError("terminal_runtime_registry is required for spawn")
    runtime = registry.resolve(backend)
    return manager, registry, runtime, backend


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
    started_at = time.perf_counter()
    try:
        result = _unsupported_sandbox_request_error(request)
        if result is None:
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
        phase_timings_ms = complete_spawn_phase_timings(request.phase_timings_ms)
        level = (
            logging.INFO
            if (time.perf_counter() - started_at) * 1000 >= _SLOW_SPAWN_THRESHOLD_MS
            else logging.DEBUG
        )
        logger.log(
            level,
            "Spawn phase timings",
            extra={
                "run_id": request.run_id,
                "provider": request.provider,
                "phase_timings_ms": phase_timings_ms,
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


async def _cleanup_timed_out_prepare(
    prepare_task: asyncio.Future[Any],
    *,
    manager: TerminalManager,
    runtime: TerminalRuntime,
    terminal_id: str,
    spawn_key: str,
    attempt_generation: int,
    attempt_started_at: datetime,
) -> None:
    try:
        prepared = prepare_task.result()
    except asyncio.CancelledError:
        return
    except Exception:
        await asyncio.to_thread(
            manager.fail_pending_attempt,
            terminal_id,
            attempt_generation=attempt_generation,
            attempt_started_at=attempt_started_at,
        )
        return

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
    await asyncio.to_thread(
        manager.fail_pending_attempt,
        terminal_id,
        attempt_generation=attempt_generation,
        attempt_started_at=attempt_started_at,
    )


def _schedule_timeout_cleanup(
    prepare_task: asyncio.Future[Any] | None,
    *,
    manager: TerminalManager,
    runtime: TerminalRuntime,
    backend: str,
    terminal_id: str,
    spawn_key: str,
    attempt_generation: int | None,
    attempt_started_at: datetime | None,
    in_doubt_owner: bool = False,
    stage: OwnerStage = "prepare",
    prepared: RuntimePreparedSpawn | None = None,
    prior_attempt: tuple[int, datetime] | None = None,
) -> None:
    """Hand a spawn's unresolved work to its late cleanup.

    Unplaced spawns settle the prepare from a done-callback. A placed spawn hands
    its held in-doubt claim to one owner task, created here synchronously and
    retained until it settles, together with the live task for ``stage``.
    """
    if in_doubt_owner:
        from gobby.agents.spawn_in_doubt_owner import InDoubtAttempt, run_in_doubt_owner

        pair = (
            None
            if attempt_generation is None or attempt_started_at is None
            else (attempt_generation, attempt_started_at)
        )
        attempt = InDoubtAttempt(
            manager=manager,
            runtime=runtime,
            backend=backend,
            terminal_id=terminal_id,
            spawn_key=spawn_key,
            pair=pair,
            prior_attempt=prior_attempt,
            prepared=prepared,
        )
        owner = asyncio.create_task(run_in_doubt_owner(attempt, stage, prepare_task))
        _TIMEOUT_CLEANUP_TASKS.add(owner)
        owner.add_done_callback(_finish_timeout_cleanup)
        return
    if prepare_task is None or attempt_generation is None or attempt_started_at is None:
        raise ValueError("unplaced timeout cleanup needs the prepare task and its attempt")
    generation, started_at = attempt_generation, attempt_started_at

    def schedule(completed: asyncio.Future[Any]) -> None:
        task = asyncio.create_task(
            _cleanup_timed_out_prepare(
                completed,
                manager=manager,
                runtime=runtime,
                terminal_id=terminal_id,
                spawn_key=spawn_key,
                attempt_generation=generation,
                attempt_started_at=started_at,
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


async def _stale_pending_absent(
    runtime: TerminalRuntime, row: Terminal, *, terminate: bool = True
) -> bool:
    """Prove the retained attempt absent, optionally killing its still-pending session."""
    from gobby.agents.capture import backend_session_present

    spawn_key = row.spawn_key or row.id
    if row.backend == "native":
        host_terminal_id = (row.process or {}).get("host_terminal_id")
        if terminate and isinstance(host_terminal_id, str) and host_terminal_id:
            await kill_spawn_key(
                runtime,
                spawn_key,
                pending=row,
                host_terminal_id=host_terminal_id,
                host_epoch=row.host_epoch,
            )
        find = getattr(runtime, "find_host_terminal", None)
        if not callable(find) or await find(row.id, spawn_key) is not None:
            return False
        # A strict host miss still needs the recorded group itself dead.
        return await asyncio.to_thread(
            reap_recorded_group_proven_dead, row.process, grace_seconds=0.05
        )
    if terminate:
        await kill_spawn_key(runtime, spawn_key, pending=row)
    return not await backend_session_present(runtime, replace(row, session_name=spawn_key))


async def reap_stale_pending_terminals(
    manager: TerminalManager,
    runtime_registry: TerminalRuntimeRegistry,
    *,
    in_doubt_seconds: float,
    now: datetime | None = None,
) -> list[str]:
    """Reap pending rows older than the in-doubt deadline once their absence is proven.

    The reaper claims each id for its whole kill and proof, so a held id belongs
    to its in-doubt owner and a placed retry cannot claim one mid-reap. After the
    claim the row is read again and reaped only while it is still the listed
    pending attempt. It settles only on proof: a tmux session gone after the
    kill, or a native row missing from the strict host listing with its recorded
    group dead. A present session, a listed row or a raised probe leaves the row
    pending for the next pass. `now` is accepted so tests can name the observed
    clock; selection uses attempt_started_at via list_stale_pending.
    """
    del now
    reaped: list[str] = []
    suspended = in_doubt_spawns.suspended()
    for listed in suspended:
        if await _reap_stale_row(manager, runtime_registry, listed):
            reaped.append(listed.id)
    suspended_ids = {row.id for row in suspended}
    for listed in manager.list_stale_pending(in_doubt_seconds):
        if listed.id in suspended_ids:
            continue
        if await _reap_stale_row(manager, runtime_registry, listed):
            reaped.append(listed.id)
    return reaped


async def _reap_stale_row(
    manager: TerminalManager, runtime_registry: TerminalRuntimeRegistry, listed: Terminal
) -> bool:
    # Naming the listed attempt resumes a claim an earlier reap of it suspended.
    if not in_doubt_spawns.claim(
        listed.id, attempt=(listed.attempt_generation, listed.attempt_started_at)
    ):
        return False
    # The claim moves to a retained settlement task that releases it only after
    # its kill, proof and settlement finish: cancelling the caller (monitor stop)
    # leaves that work, and the claim, with the task until it settles.
    task = asyncio.create_task(_reap_claimed_row(manager, runtime_registry, listed))
    _REAP_TASKS.add(task)
    task.add_done_callback(_REAP_TASKS.discard)
    return await asyncio.shield(task)


async def _reap_claimed_row(
    manager: TerminalManager, runtime_registry: TerminalRuntimeRegistry, listed: Terminal
) -> bool:
    from gobby.agents.spawn_in_doubt_owner import release_claim

    settled = False
    pair = (listed.attempt_generation, listed.attempt_started_at)
    retained = replace(listed, process=dict(listed.process) if listed.process is not None else None)
    try:
        row = manager.get(listed.id)
        same_pending = (
            row is not None
            and row.state == "pending"
            and (row.attempt_generation, row.attempt_started_at) == pair
        )
        if same_pending and row is not None:
            retained = replace(row, process=dict(row.process) if row.process is not None else None)
        try:
            runtime = runtime_registry.resolve(retained.backend)
            if same_pending:
                absent = await _stale_pending_absent(runtime, retained)
            else:
                # The key may now name a different attempt. Probe the retained
                # identity, but never kill a moved-on row's session by that key.
                absent = await _stale_pending_absent(runtime, retained, terminate=False)
        except Exception as exc:
            logger.warning(
                "Stale pending terminal %s kept: absence unproven (%s)",
                listed.id,
                type(exc).__name__,
            )
            return False
        if not absent:
            return False
        if not same_pending:
            settled = True
            return False
        result = manager.fail_pending_attempt(
            listed.id,
            attempt_generation=pair[0],
            attempt_started_at=pair[1],
        )
        # A CAS miss does not erase the old attempt's death proof. Its run
        # compensation stays protected by the held claim while it executes.
        settled = True
        return result is not None
    finally:
        # Deferred compensation runs only after a proven settle. An unproven or
        # failed settle keeps the row pending, so its process may still use it:
        # the claim stays suspended with its steps until a later reap of this
        # attempt settles it. Suspended snapshots stay reachable from the sweep
        # after a database transition, deletion or attempt change.
        if settled:
            await release_claim(listed.id, proven=True)
        else:
            in_doubt_spawns.suspend(retained)
