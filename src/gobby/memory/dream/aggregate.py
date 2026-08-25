"""Aggregate memory dream scheduling and lifecycle persistence."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from gobby.config.persistence import MemoryDreamConfig
from gobby.memory.dream.models import DreamCheckpoint
from gobby.memory.dream.options import DreamRunOptions
from gobby.memory.dream.orchestrator import (
    DreamDependencyError,
    DreamSweepOrchestrator,
    WorkUnitOutcome,
    _positive_int,
)
from gobby.memory.dream.protocols import MemoryDreamManagerProtocol
from gobby.memory.dream.related import RelatedEvidenceSession
from gobby.memory.dream.storage import MemoryDreamStore
from gobby.memory.dream.storage_runs import INTERRUPTED_CANCELLED_ERROR
from gobby.storage.memories_scope import MemoryScope, MemoryScopeKind

logger = logging.getLogger(__name__)

DEFAULT_REDREAM_AFTER_HOURS = 20
ALL_MEMORIES_CUTOFF = "9999-12-31T23:59:59+00:00"


@dataclass
class _ScopeSweep:
    """One due scope's child run row and its long-lived orchestrator."""

    scope: MemoryScope
    options: DreamRunOptions
    run_id: str
    orchestrator: DreamSweepOrchestrator
    related_session: RelatedEvidenceSession
    unit_size: int
    units: int = 0


class _AggregateDreamHost(Protocol):
    """Service dependencies and monkeypatch seams used by the aggregate runner."""

    @property
    def memory_manager(self) -> MemoryDreamManagerProtocol: ...

    dream_config: MemoryDreamConfig
    store: MemoryDreamStore

    async def run_all_due_projects(
        self,
        *,
        dry_run: bool = False,
        skip_consolidation: bool = False,
        memory_type: str | None = None,
        full_sweep: bool = False,
        run_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def _truth_changed_project_ids(
        self, scopes: list[MemoryScope] | None = None
    ) -> list[str]: ...

    async def _apply_truth_change_triggers(self) -> None: ...

    async def _run_nested_target(self, options: DreamRunOptions) -> dict[str, Any]: ...

    def _max_runtime_seconds(self) -> int: ...

    def _admission_window_open(self, deadline: float) -> bool: ...

    async def _build_orchestrator(
        self,
        run_id: str,
        options: DreamRunOptions,
        related_session: RelatedEvidenceSession,
        *,
        admission_deadline: float | None = None,
    ) -> DreamSweepOrchestrator: ...

    async def _open_scope_sweep(
        self, scope: MemoryScope, *, memory_type: str | None, full_sweep: bool
    ) -> _ScopeSweep: ...

    async def _run_scope_unit(self, sweep: _ScopeSweep) -> WorkUnitOutcome: ...

    async def _close_scope_sweep(
        self, sweep: _ScopeSweep, *, status: str, error: str | None = None
    ) -> dict[str, Any]: ...


class _AggregateDreamRunner:
    """Run preview fan-out and fair mutating sweeps across every due scope."""

    def __init__(self, host: _AggregateDreamHost) -> None:
        self._host = host

    async def run_all_due_projects_locked(
        self,
        *,
        dry_run: bool = False,
        skip_consolidation: bool = False,
        memory_type: str | None = None,
        full_sweep: bool = False,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Sweep every scope with due memories under a bounded admission window.

        Mutating sweeps run round-robin passes: each pass executes one work
        unit for every currently due project/global scope in stable order, then
        re-enumerates the backlog and starts another pass while the
        ``max_runtime_seconds`` admission window stays open. Preview runs
        (dry-run and inventory) keep the serial per-target lifecycle — they are
        bounded by ``dry_run_max_candidates`` and stamp nothing, so fairness
        does not apply. ``run_id`` is the aggregate run row that receives
        coordinator checkpoints when provided.
        """
        if not self._host.dream_config.enabled:
            return {
                "success": False,
                "error": "memory dream is disabled",
                "targets": 0,
                "completed": 0,
                "failed": 0,
                "mutations": 0,
                "runs": [],
            }

        redream_hours = _positive_int(
            getattr(
                self._host.dream_config,
                "redream_after_hours",
                DEFAULT_REDREAM_AFTER_HOURS,
            ),
            DEFAULT_REDREAM_AFTER_HOURS,
        )
        cutoff = (
            ALL_MEMORIES_CUTOFF
            if full_sweep
            else (datetime.now(UTC) - timedelta(hours=redream_hours)).isoformat()
        )
        deadline = asyncio.get_running_loop().time() + self._host._max_runtime_seconds()
        if dry_run or skip_consolidation:
            return await self._run_preview_targets(
                dry_run=dry_run,
                skip_consolidation=skip_consolidation,
                memory_type=memory_type,
                full_sweep=full_sweep,
                cutoff=cutoff,
                deadline=deadline,
            )
        return await self._run_round_robin(
            memory_type=memory_type,
            full_sweep=full_sweep,
            cutoff=cutoff,
            deadline=deadline,
            run_id=run_id,
        )

    async def _run_preview_targets(
        self,
        *,
        dry_run: bool,
        skip_consolidation: bool,
        memory_type: str | None,
        full_sweep: bool,
        cutoff: str,
        deadline: float,
    ) -> dict[str, Any]:
        """Serial per-target preview sweep (dry-run and inventory runs).

        The truth-change trigger mutates cooldown/hash state, so preview runs
        only observe which projects would trigger instead of applying it.
        """
        targets = await asyncio.to_thread(
            self._host.memory_manager.list_dream_scopes, redream_cutoff=cutoff
        )
        truth_triggered_targets = await self._host._truth_changed_project_ids(targets)
        for target_project_id in truth_triggered_targets:
            target_scope = MemoryScope.project_only(target_project_id)
            if target_scope not in targets:
                targets.append(target_scope)

        runs: list[dict[str, Any]] = []
        completed = 0
        failed = 0
        mutations = 0
        stop_reason = "drained"
        for target_scope in targets:
            if not self._host._admission_window_open(deadline):
                stop_reason = "window_exhausted"
                break
            scope_project_id = target_scope.project_id
            try:
                options = _scope_run_options(
                    target_scope,
                    dry_run=dry_run,
                    skip_consolidation=skip_consolidation,
                    memory_type=memory_type,
                    full_sweep=full_sweep,
                )
                result = await self._host._run_nested_target(options)
                target_mutations = _completed_mutation_count(result)
                mutations += target_mutations
                completed += 1
                runs.append(
                    {
                        "project_id": scope_project_id,
                        "is_global": target_scope.kind is MemoryScopeKind.GLOBAL_ONLY,
                        "success": True,
                        "run_id": _result_run_id(result),
                        "mutations": target_mutations,
                    }
                )
            except Exception as exc:
                logger.exception(
                    "memory dream failed for target %s (project_id=%s)",
                    target_scope.kind.value,
                    scope_project_id,
                )
                failed += 1
                runs.append(
                    {
                        "project_id": scope_project_id,
                        "is_global": target_scope.kind is MemoryScopeKind.GLOBAL_ONLY,
                        "success": False,
                        "error": str(exc),
                    }
                )

        all_failed = failed > 0 and completed == 0
        return {
            "success": not all_failed,
            "targets": len(targets),
            "completed": completed,
            "failed": failed,
            "mutations": mutations,
            "runs": runs,
            "passes": 1,
            "stop_reason": stop_reason,
        }

    async def _run_round_robin(
        self,
        *,
        memory_type: str | None,
        full_sweep: bool,
        cutoff: str,
        deadline: float,
        run_id: str | None,
    ) -> dict[str, Any]:
        """Fair mutating sweep: one work unit per due scope per pass.

        Backlog counts refresh between passes via re-enumeration; per-unit
        cooldown stamping shrinks them. A scope whose unit yields no progress
        drops out because scope enumeration can include scopes that candidate
        listing excludes. A dependency failure stops the whole coordinator;
        completed checkpoints stay durable and untouched candidates remain
        due. Structural per-scope failures are isolated and logged at ERROR.
        """
        await self._host._apply_truth_change_triggers()

        sweeps: dict[str, _ScopeSweep] = {}
        entries: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        done: set[str] = set()
        stop_reason = "drained"
        dependency_failure: str | None = None
        passes = 0
        completed = 0
        failed = 0
        remaining_scopes = 0
        try:
            stopping = False
            while not stopping:
                targets = await asyncio.to_thread(
                    self._host.memory_manager.list_dream_scopes, redream_cutoff=cutoff
                )
                due = [scope for scope in targets if _scope_sweep_key(scope) not in done]
                if not due:
                    break
                passes += 1
                for scope in due:
                    key = _scope_sweep_key(scope)
                    if not self._host._admission_window_open(deadline):
                        stop_reason = "window_exhausted"
                        remaining_scopes = len([s for s in due if _scope_sweep_key(s) not in done])
                        stopping = True
                        break
                    sweep = sweeps.get(key)
                    if sweep is None:
                        try:
                            sweep = await self._host._open_scope_sweep(
                                scope, memory_type=memory_type, full_sweep=full_sweep
                            )
                        except Exception as exc:
                            logger.exception(
                                "memory dream failed for target %s (project_id=%s)",
                                scope.kind.value,
                                scope.project_id,
                            )
                            failed += 1
                            done.add(key)
                            order.append(key)
                            entries[key] = {
                                "project_id": scope.project_id,
                                "is_global": scope.kind is MemoryScopeKind.GLOBAL_ONLY,
                                "success": False,
                                "error": str(exc),
                            }
                            continue
                        sweeps[key] = sweep
                        order.append(key)
                    try:
                        outcome = await self._host._run_scope_unit(sweep)
                    except DreamDependencyError as exc:
                        dependency_failure = str(exc)
                        stop_reason = "dependency_failure"
                        failed += 1
                        done.add(key)
                        entries[key] = await self._host._close_scope_sweep(
                            sweep, status="failed", error=str(exc)
                        )
                        remaining_scopes = len([s for s in due if _scope_sweep_key(s) not in done])
                        stopping = True
                        break
                    except Exception as exc:
                        logger.exception(
                            "memory dream failed for target %s (project_id=%s)",
                            scope.kind.value,
                            scope.project_id,
                        )
                        failed += 1
                        done.add(key)
                        entries[key] = await self._host._close_scope_sweep(
                            sweep, status="failed", error=str(exc)
                        )
                        continue
                    if outcome.candidates and not outcome.no_progress:
                        sweep.units += 1
                    if outcome.drained(sweep.unit_size):
                        done.add(key)
                        entries[key] = await self._host._close_scope_sweep(
                            sweep, status="completed"
                        )
                        completed += 1
                if run_id is not None:
                    await self._persist_aggregate_checkpoint(
                        run_id,
                        sweeps=sweeps,
                        passes=passes,
                        remaining=remaining_scopes,
                        stop_reason=stop_reason if stopping else None,
                        dependency_failure=dependency_failure,
                    )
        except asyncio.CancelledError:
            completed_ts = datetime.now(UTC).isoformat()
            for key in order:
                if key in done or key not in sweeps:
                    continue
                sweep = sweeps[key]
                with suppress(Exception):
                    await asyncio.to_thread(
                        self._host.store.update_run,
                        sweep.run_id,
                        status="interrupted",
                        completed_at=completed_ts,
                        error=INTERRUPTED_CANCELLED_ERROR,
                    )
                with suppress(Exception):
                    await sweep.related_session.aclose()
            raise

        for key in order:
            if key in done or key not in sweeps:
                continue
            sweep = sweeps[key]
            sweep.orchestrator.stop_reason = stop_reason
            entries[key] = await self._host._close_scope_sweep(sweep, status="partial")
            done.add(key)

        runs = [entries[key] for key in order if key in entries]
        mutations = sum(int(entry.get("mutations", 0)) for entry in runs)
        units = sum(sweep.units for sweep in sweeps.values())
        if stop_reason == "dependency_failure":
            logger.warning(
                "Memory dream coordinator stopped: %s. Completed %s work unit(s) across "
                "%s scope(s) with %s mutation(s) applied; %s due scope(s) were not "
                "finished and their candidates remain due for the next run.",
                dependency_failure,
                units,
                len(order),
                mutations,
                remaining_scopes,
            )
        elif stop_reason == "window_exhausted":
            logger.info(
                "Memory dream admission window closed after %s pass(es): %s scope(s) "
                "drained, %s work unit(s), %s mutation(s); %s due scope(s) remain "
                "eligible for the next run.",
                passes,
                completed,
                units,
                mutations,
                remaining_scopes,
            )

        all_failed = failed > 0 and completed == 0 and stop_reason == "drained"
        result: dict[str, Any] = {
            "success": not all_failed,
            "targets": len(order),
            "completed": completed,
            "failed": failed,
            "mutations": mutations,
            "runs": runs,
            "passes": passes,
            "stop_reason": stop_reason,
        }
        if dependency_failure is not None:
            result["dependency_failure"] = dependency_failure
        return result

    async def open_scope_sweep(
        self, scope: MemoryScope, *, memory_type: str | None, full_sweep: bool
    ) -> _ScopeSweep:
        """Create a scope's child run row and its long-lived orchestrator."""
        options = _scope_run_options(
            scope,
            dry_run=False,
            skip_consolidation=False,
            memory_type=memory_type,
            full_sweep=full_sweep,
        )
        run_id = await asyncio.to_thread(
            self._host.store.create_run,
            project_id=None if options.global_only else options.project_id,
            dry_run=False,
            options=options.to_dict(),
            status="started",
        )
        related_session = RelatedEvidenceSession()
        orchestrator = await self._host._build_orchestrator(run_id, options, related_session)
        return _ScopeSweep(
            scope=scope,
            options=options,
            run_id=run_id,
            orchestrator=orchestrator,
            related_session=related_session,
            unit_size=orchestrator.unit_size,
        )

    async def run_scope_unit(self, sweep: _ScopeSweep) -> WorkUnitOutcome:
        """Execute one coordinator work unit."""
        return await sweep.orchestrator.run_unit()

    async def close_scope_sweep(
        self, sweep: _ScopeSweep, *, status: str, error: str | None = None
    ) -> dict[str, Any]:
        """Finalize a scope's child run row and return its aggregate entry."""
        mutations = 0
        plan: dict[str, Any] | None = None
        summary: dict[str, Any] | None = None
        try:
            totals = await sweep.orchestrator.finalize_sweep()
            plan, summary = totals.to_plan(), totals.to_summary()
            mutations = totals.mutations
        except Exception:
            logger.warning(
                "Failed to finalize memory dream scope sweep %s", sweep.run_id, exc_info=True
            )
        with suppress(Exception):
            await sweep.related_session.aclose()
        try:
            await asyncio.to_thread(
                self._host.store.update_run,
                sweep.run_id,
                status=status,
                completed_at=datetime.now(UTC).isoformat(),
                plan=plan,
                summary=summary,
                error=error,
            )
        except Exception:
            logger.warning(
                "Failed to persist memory dream scope run %s", sweep.run_id, exc_info=True
            )
        entry: dict[str, Any] = {
            "project_id": sweep.scope.project_id,
            "is_global": sweep.scope.kind is MemoryScopeKind.GLOBAL_ONLY,
            "success": status != "failed",
            "run_id": sweep.run_id,
            "mutations": mutations,
            "status": status,
        }
        if error is not None:
            entry["error"] = error
        return entry

    async def _persist_aggregate_checkpoint(
        self,
        run_id: str,
        *,
        sweeps: dict[str, _ScopeSweep],
        passes: int,
        remaining: int,
        stop_reason: str | None,
        dependency_failure: str | None,
    ) -> None:
        """Best-effort durable coordinator progress on the aggregate run row."""
        checkpoint = DreamCheckpoint(
            phase="coordinator",
            scope="all-due",
            pass_number=passes,
            batch_number=sum(sweep.units for sweep in sweeps.values()),
            completed=sum(
                sweep.orchestrator.totals.candidates_reviewed for sweep in sweeps.values()
            ),
            mutations=sum(sweep.orchestrator.totals.mutations for sweep in sweeps.values()),
            remaining=remaining,
            stop_reason=stop_reason,
            last_dependency_failure=dependency_failure,
        )
        try:
            await asyncio.to_thread(
                self._host.store.update_run, run_id, checkpoint=checkpoint.to_dict()
            )
        except Exception:
            logger.warning(
                "Failed to persist memory dream coordinator checkpoint %s",
                run_id,
                exc_info=True,
            )

    async def execute_all_due_projects_run(
        self,
        run_id: str,
        *,
        dry_run: bool = False,
        skip_consolidation: bool = False,
        memory_type: str | None = None,
        full_sweep: bool = False,
    ) -> dict[str, Any]:
        """Execute an admitted aggregate run and persist its terminal state."""
        try:
            aggregate = await self._host.run_all_due_projects(
                dry_run=dry_run,
                skip_consolidation=skip_consolidation,
                memory_type=memory_type,
                full_sweep=full_sweep,
                run_id=run_id,
            )
            completed_ts = datetime.now(UTC).isoformat()
            stop_reason = aggregate.get("stop_reason") or "drained"
            if not aggregate.get("success"):
                status = "failed"
                error = aggregate.get("error", "aggregate failed")
            elif stop_reason in ("window_exhausted", "dependency_failure"):
                status = "partial"
                error = aggregate.get("dependency_failure")
            else:
                status = "completed"
                error = None
            run = await asyncio.to_thread(
                self._host.store.update_run,
                run_id,
                status=status,
                completed_at=completed_ts,
                plan={"aggregate": True, "runs": aggregate.get("runs", [])},
                summary={
                    "targets": aggregate.get("targets", 0),
                    "completed": aggregate.get("completed", 0),
                    "failed": aggregate.get("failed", 0),
                    "mutations": aggregate.get("mutations", 0),
                    "passes": aggregate.get("passes", 0),
                    "stop_reason": stop_reason,
                },
                error=error,
            )
            return {
                "success": bool(aggregate.get("success")),
                "run_id": run_id,
                "run": run,
                "aggregate": aggregate,
                "status": status,
                **({"error": error} if error else {}),
            }
        except asyncio.CancelledError:
            completed_ts = datetime.now(UTC).isoformat()
            try:
                await asyncio.to_thread(
                    self._host.store.update_run,
                    run_id,
                    status="interrupted",
                    completed_at=completed_ts,
                    error=INTERRUPTED_CANCELLED_ERROR,
                )
            except Exception:
                logger.warning(
                    "Failed to persist interrupted aggregate memory dream run %s",
                    run_id,
                    exc_info=True,
                )
            raise
        except Exception as exc:  # Persist every terminal failure on the aggregate run.
            completed_ts = datetime.now(UTC).isoformat()
            run = await asyncio.to_thread(
                self._host.store.update_run,
                run_id,
                status="failed",
                completed_at=completed_ts,
                error=str(exc),
            )
            return {
                "success": False,
                "run_id": run_id,
                "run": run,
                "error": str(exc),
            }


def _scope_sweep_key(scope: MemoryScope) -> str:
    return "global" if scope.kind is MemoryScopeKind.GLOBAL_ONLY else f"project:{scope.project_id}"


def _scope_run_options(
    scope: MemoryScope,
    *,
    dry_run: bool,
    skip_consolidation: bool,
    memory_type: str | None,
    full_sweep: bool,
) -> DreamRunOptions:
    """Build per-scope options with the global bucket selected exactly once."""
    if scope.kind is MemoryScopeKind.GLOBAL_ONLY:
        return DreamRunOptions(
            dry_run=dry_run,
            skip_consolidation=skip_consolidation,
            memory_type=memory_type,
            global_only=True,
            full_sweep=full_sweep,
        )
    return DreamRunOptions(
        dry_run=dry_run,
        skip_consolidation=skip_consolidation,
        memory_type=memory_type,
        project_id=scope.project_id,
        include_global=False,
        full_sweep=full_sweep,
    )


def _result_run_id(result: object) -> str | None:
    if not isinstance(result, dict):
        return None
    value = result.get("run_id")
    if value is None:
        run = result.get("run")
        if isinstance(run, dict):
            value = run.get("id")
    return str(value) if value is not None else None


def _completed_mutation_count(result: object) -> int:
    if not isinstance(result, dict):
        raise RuntimeError("memory dream returned non-object result")
    if not result.get("success"):
        raise RuntimeError(str(result.get("error", "memory dream failed")))
    raw_run = result.get("run")
    run = raw_run if isinstance(raw_run, dict) else {}
    raw_summary = run.get("summary")
    summary = raw_summary if isinstance(raw_summary, dict) else {}
    run_id = result.get("run_id")
    if run_id is None:
        run_id = run.get("id")
    if run_id is None:
        raise RuntimeError("memory dream completed without run_id")
    raw_mutations = summary.get("mutations", 0)
    try:
        mutations = int(raw_mutations)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid memory dream mutation count: value=%r type=%s",
            raw_mutations,
            type(raw_mutations).__name__,
        )
        mutations = 0
    return mutations
