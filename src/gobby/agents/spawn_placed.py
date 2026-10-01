"""Placed runtime spawn: bind before exec, one in-doubt owner per attempt.

The terminal id is claimed in the in-doubt registry before its row is written.
From then on every exit either settles the row inline and releases the claim
(nothing can exist on the host before ``prepare_spawn`` is dispatched) or hands
the claim and the live stage task to exactly one owner task, which settles the
row only on proof.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any
from uuid import UUID

from gobby.agents.spawn_executor_providers import ProviderSpawnPlan
from gobby.agents.spawn_in_doubt_owner import (
    InDoubtAttempt,
    OwnerStage,
    confirm_exited,
    release_claim,
)
from gobby.agents.spawn_models import SpawnRequest, SpawnResult, is_infrastructure_spawn_error
from gobby.agents.spawn_timing import finish_spawn_phase, start_spawn_phase
from gobby.storage.terminals import Terminal, TerminalManager, mint_terminal_id
from gobby.terminals.in_doubt import in_doubt_spawns
from gobby.terminals.runtime import PreparedSpawn as RuntimePreparedSpawn
from gobby.terminals.runtime import (
    TerminalRuntime,
    TerminalSpawnRequest,
    can_reserve_observer,
)

logger = logging.getLogger(__name__)


async def _placed_runtime_spawn(
    request: SpawnRequest,
    plan: ProviderSpawnPlan,
    *,
    binder: Callable[[str], Awaitable[None]] | None,
    manager: TerminalManager,
    runtime: TerminalRuntime,
    backend: str,
    command: list[str],
    existing: Terminal | None,
) -> SpawnResult:
    from gobby.agents.spawn_executor import derive_spawn_key

    if existing is not None:
        terminal_id = existing.id
        spawn_key = existing.spawn_key or derive_spawn_key(backend, terminal_id)
        prior = (existing.attempt_generation, existing.attempt_started_at)
    else:
        terminal_id = mint_terminal_id()
        spawn_key = derive_spawn_key(backend, terminal_id)
        prior = None
    if not in_doubt_spawns.claim(terminal_id):
        # Another owner holds this id; nothing of this attempt was written.
        return SpawnResult(
            success=False,
            run_id=plan.agent_run_id,
            child_session_id=plan.child_session_id,
            status="failed",
            error="terminal_in_doubt",
        )
    placed = _PlacedAttempt(
        request,
        plan,
        InDoubtAttempt(
            manager=manager,
            runtime=runtime,
            backend=backend,
            terminal_id=terminal_id,
            spawn_key=spawn_key,
            prior_attempt=prior,
        ),
    )
    return await placed.run(binder, command, existing)


class _PlacedAttempt:
    """The ownership boundary of one placed attempt or existing-terminal retry."""

    def __init__(self, request: SpawnRequest, plan: ProviderSpawnPlan, attempt: InDoubtAttempt):
        self.request = request
        self.plan = plan
        self.attempt = attempt
        self.stage: OwnerStage = "create"
        self.task: asyncio.Future[Any] | None = None
        # Set once the claim was released inline or handed to the owner.
        self.owned = False

    async def run(
        self,
        binder: Callable[[str], Awaitable[None]] | None,
        command: list[str],
        existing: Terminal | None,
    ) -> SpawnResult:
        try:
            return await self._run(binder, command, existing)
        finally:
            if not self.owned:
                self._hand_off()

    def _hand_off(self) -> None:
        """Give the claim and the live stage task to one owner; never awaits."""
        from gobby.agents.spawn_executor import _schedule_timeout_cleanup

        attempt = self.attempt
        pair = attempt.pair
        _schedule_timeout_cleanup(
            self.task,
            manager=attempt.manager,
            runtime=attempt.runtime,
            backend=attempt.backend,
            terminal_id=attempt.terminal_id,
            spawn_key=attempt.spawn_key,
            attempt_generation=None if pair is None else pair[0],
            attempt_started_at=None if pair is None else pair[1],
            in_doubt_owner=True,
            stage=self.stage,
            prepared=attempt.prepared,
            prior_attempt=attempt.prior_attempt,
        )
        self.owned = True

    def _start(self, stage: OwnerStage, work: Awaitable[Any]) -> asyncio.Future[Any]:
        self.stage = stage
        self.task = asyncio.ensure_future(work)
        return self.task

    async def _settle_inline(self, write: Coroutine[Any, Any, object]) -> None:
        """Settle before any prepare; an unconfirmed write goes to the owner instead."""
        task = self._start(self.stage, write)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.warning(
                "Inline settlement of placed terminal %s raised; handing it to the owner",
                self.attempt.terminal_id,
                exc_info=True,
            )
            return
        try:
            confirmed = await confirm_exited(self.attempt)
        except asyncio.CancelledError:
            return
        except Exception:
            return
        if confirmed:
            self.owned = True
            await release_claim(self.attempt.terminal_id, run_deferred=True)

    def _failed(
        self,
        error: str,
        *,
        status: str = "failed",
        detail: str | None = None,
        terminal: bool = True,
        retryable: bool = False,
    ) -> SpawnResult:
        return SpawnResult(
            success=False,
            run_id=self.plan.agent_run_id,
            child_session_id=self.plan.child_session_id,
            status=status,
            error=error,
            error_detail=detail,
            terminal_id=self.attempt.terminal_id if terminal else None,
            retryable_infrastructure=retryable,
            prior_attempt=self.attempt.prior_attempt,
        )

    def _cancelled(self) -> SpawnResult:
        return self._failed("cancelled", status="cancelled")

    async def _run(
        self,
        binder: Callable[[str], Awaitable[None]] | None,
        command: list[str],
        existing: Terminal | None,
    ) -> SpawnResult:
        from gobby.agents.spawn_executor import _settle_native_spawn_failure
        from gobby.agents.spawn_executor_runtime import _deferred_failure_code, _promote_prepared

        attempt, request, plan = self.attempt, self.request, self.plan
        manager, runtime, backend = attempt.manager, attempt.runtime, attempt.backend
        terminal_id, spawn_key = attempt.terminal_id, attempt.spawn_key

        if existing is not None:
            create = asyncio.to_thread(
                manager.retry_attempt_unsettled, terminal_id, existing.attempt_generation
            )
        else:
            create = asyncio.to_thread(
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
        try:
            row = await asyncio.shield(self._start("create", create))
        except asyncio.CancelledError:
            return self._cancelled()
        if row is None:
            # The bump's CAS matched nothing, so no row of this attempt exists.
            self.owned = True
            await release_claim(terminal_id, run_deferred=True)
            return self._failed("retry_generation_cas_failed", terminal=False)
        pair = attempt.pair = (row.attempt_generation, row.attempt_started_at)
        self.stage = "bind"

        if request.cancel_event is not None and request.cancel_event.is_set():
            await self._settle_inline(asyncio.to_thread(manager.fail_pending, terminal_id))
            return self._cancelled()

        if binder is not None:
            try:
                await asyncio.shield(self._start("bind", binder(terminal_id)))
            except asyncio.CancelledError:
                return self._cancelled()
            except Exception as exc:
                logger.warning("Placement bind failed for terminal %s", terminal_id, exc_info=True)
                code, detail = _deferred_failure_code(backend, exc)
                await self._settle_inline(self._fail_unprepared(exc, _settle_native_spawn_failure))
                return self._failed(code, detail=detail)

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
            self.stage = "reserve"
            if not can_reserve_observer(runtime):
                await self._settle_inline(asyncio.to_thread(manager.fail_pending, terminal_id))
                return self._failed("native_reserve_unavailable")
            try:
                reservation = await asyncio.shield(
                    self._start("reserve", runtime.reserve_observer(UUID(terminal_id)))
                )
            except asyncio.CancelledError:
                return self._cancelled()
            except Exception as exc:
                code, detail = _deferred_failure_code(backend, exc)
                await self._settle_inline(self._fail_unprepared(exc, _settle_native_spawn_failure))
                return self._failed(code, detail=detail)
            spawn_request.reservation_id = reservation.get("reservation_id")
            spawn_request.reserve_key = reservation.get("reserve_key")

        # From here on a failure is never proof: every outcome but a completed
        # promotion goes to the owner.
        prepare_started = start_spawn_phase()
        prepare = self._start("prepare", runtime.prepare_spawn(spawn_request))
        timeout = request.timeout_seconds
        try:
            if timeout is not None:
                prepared: RuntimePreparedSpawn = await asyncio.wait_for(
                    asyncio.shield(prepare), timeout=timeout
                )
            else:
                prepared = await asyncio.shield(prepare)
        except TimeoutError:
            native = backend == "native"
            return self._failed(
                "spawn_timeout" if native else "spawn timed out",
                detail="spawn timed out" if native else None,
                retryable=True,
            )
        except asyncio.CancelledError:
            return self._cancelled()
        except Exception as exc:
            logger.warning(
                "Placed backend spawn raised for terminal %s", terminal_id, exc_info=True
            )
            code, detail = _deferred_failure_code(backend, exc)
            return self._failed(code, detail=detail, retryable=is_infrastructure_spawn_error(exc))
        finally:
            finish_spawn_phase(request.phase_timings_ms, "runtime_prepare_spawn", prepare_started)

        attempt.prepared = prepared
        promotion = self._start(
            "promote",
            _promote_prepared(
                request,
                plan,
                manager=manager,
                runtime=runtime,
                backend=backend,
                terminal_id=terminal_id,
                spawn_key=spawn_key,
                prepared=prepared,
                reservation_id=spawn_request.reservation_id,
                attempt_generation=pair[0],
                attempt_started_at=pair[1],
                defer_failure=True,
            ),
        )
        try:
            result: SpawnResult = await asyncio.shield(promotion)
        except asyncio.CancelledError:
            return self._cancelled()
        if not result.success:
            result.prior_attempt = attempt.prior_attempt
            return result
        self.owned = True
        await release_claim(terminal_id, run_deferred=True)
        return result

    async def _fail_unprepared(
        self,
        exc: Exception,
        settle_native: Callable[..., Awaitable[tuple[str, str | None]]],
    ) -> None:
        attempt = self.attempt
        if attempt.backend == "native":
            await settle_native(
                manager=attempt.manager,
                runtime=attempt.runtime,
                terminal_id=attempt.terminal_id,
                spawn_key=attempt.spawn_key,
                exc=exc,
            )
            return
        pair = attempt.pair
        if pair is not None:
            await asyncio.to_thread(
                attempt.manager.fail_pending_attempt,
                attempt.terminal_id,
                attempt_generation=pair[0],
                attempt_started_at=pair[1],
            )
