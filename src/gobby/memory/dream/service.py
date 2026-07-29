"""Memory dream service facade.

The service owns admission, status, revert, and execution entry points. The
run bodies — mutating sweeps, dry runs, and skip-consolidation inventories —
execute as bounded work units in ``gobby.memory.dream.orchestrator``: each
unit selects at most 25 candidates from one scope, gathers required evidence,
calls the planner serially, validates, applies, and checkpoints. The cooldown
cursor advances per applied unit so a sweep drains to zero and an immediate
re-run is a no-op. Dry runs write no memory, snapshot, or stamp mutations;
they materialize an immutable candidate-ID snapshot and hydrate it unit by
unit so every start-of-run candidate is reviewed at most once.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from weakref import WeakKeyDictionary

from gobby.config.persistence import MemoryDreamConfig
from gobby.memory.dream.apply import revert_dream_run
from gobby.memory.dream.models import DreamCheckpoint
from gobby.memory.dream.options import DreamRunOptions
from gobby.memory.dream.orchestrator import (
    DreamDependencyError,
    DreamSweepOrchestrator,
    SweepTotals,
    WorkUnitOutcome,
    _positive_int,
)
from gobby.memory.dream.protocols import MemoryDreamLLMProtocol, MemoryDreamManagerProtocol
from gobby.memory.dream.related import RelatedEvidenceSession
from gobby.memory.dream.storage import (
    INTERRUPTED_CANCELLED_ERROR,
    RUN_TERMINAL_STATUSES,
    DreamAdmission,
    MemoryDreamStore,
)
from gobby.memory.dream.truth_digest import (
    build_current_truth_digest,
    build_project_truth_digest_async,
)
from gobby.storage.memories_scope import MemoryScope, MemoryScopeKind
from gobby.storage.projects import LocalProjectManager

logger = logging.getLogger(__name__)

_EXECUTION_LOCKS: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = WeakKeyDictionary()


def _execution_lock() -> asyncio.Lock:
    """Return the daemon-wide dream lock for the current event loop."""
    loop = asyncio.get_running_loop()
    lock = _EXECUTION_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _EXECUTION_LOCKS[loop] = lock
    return lock


DEFAULT_REDREAM_AFTER_HOURS = 20
DEFAULT_MAX_RUNTIME_SECONDS = 14400

# A cutoff past every possible ``last_dreamed_at`` makes the due predicate match
# every live memory, so ``list_dream_scopes`` returns every scope that has
# memories — including ones fully within the cooldown window. The truth-change
# trigger needs that full set so a digest change on a cooled project is caught.
_ALL_MEMORIES_CUTOFF = "9999-12-31T23:59:59+00:00"


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
    """Per-scope options: the global bucket runs global-only exactly once."""
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


class MemoryDreamService:
    """Coordinate candidate discovery, planning, apply, status, and revert."""

    def __init__(
        self,
        *,
        memory_manager: MemoryDreamManagerProtocol,
        dream_config: MemoryDreamConfig | None = None,
        llm_service: MemoryDreamLLMProtocol | None = None,
        daemon_config: Any = None,
        current_project_id: str | None = None,
    ) -> None:
        self.memory_manager = memory_manager
        self.dream_config = dream_config or MemoryDreamConfig()
        self.llm_service = llm_service
        self._daemon_config = daemon_config
        self.current_project_id = current_project_id
        self.store = MemoryDreamStore(memory_manager.db)
        self._schema_ready = False

    async def run(self, options: DreamRunOptions) -> dict[str, Any]:
        async with _execution_lock():
            return await self._run_without_execution_lock(options)

    async def _run_without_execution_lock(self, options: DreamRunOptions) -> dict[str, Any]:
        if not self.dream_config.enabled:
            return {"success": False, "error": "memory dream is disabled"}

        started = await self.start_async(options)
        if not started.get("success") or started.get("coalesced"):
            # Coalesced: an equivalent or covering run is already active, so
            # the request is satisfied by observing it rather than executing.
            return started
        run_id = str(started["run_id"])
        return await self._execute_run_locked(run_id, options)

    async def _run_nested_target(self, options: DreamRunOptions) -> dict[str, Any]:
        """Execute one aggregate fan-out target under the caller's admission.

        The aggregate caller holds (or is queued behind) the sole admitted
        'running' row, so per-target rows record execution without competing
        for admission: they are created at 'started' and move straight to a
        terminal status.
        """
        if not self.dream_config.enabled:
            return {"success": False, "error": "memory dream is disabled"}
        run_project_id = None if options.global_only else options.project_id
        run_id = await asyncio.to_thread(
            self.store.create_run,
            project_id=run_project_id,
            dry_run=options.dry_run,
            options=options.to_dict(),
            status="started",
        )
        return await self._execute_run_locked(run_id, options)

    async def run_all_due_projects(
        self,
        *,
        dry_run: bool = False,
        skip_consolidation: bool = False,
        memory_type: str | None = None,
        full_sweep: bool = False,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        async with _execution_lock():
            return await self._run_all_due_projects_locked(
                dry_run=dry_run,
                skip_consolidation=skip_consolidation,
                memory_type=memory_type,
                full_sweep=full_sweep,
                run_id=run_id,
            )

    async def _run_all_due_projects_locked(
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
        if not self.dream_config.enabled:
            return {
                "success": False,
                "error": "memory dream is disabled",
                "targets": 0,
                "completed": 0,
                "failed": 0,
                "mutations": 0,
                "runs": [],
            }

        await self._ensure_schema_async()
        redream_hours = _positive_int(
            getattr(self.dream_config, "redream_after_hours", DEFAULT_REDREAM_AFTER_HOURS),
            DEFAULT_REDREAM_AFTER_HOURS,
        )
        cutoff = (
            _ALL_MEMORIES_CUTOFF
            if full_sweep
            else (datetime.now(UTC) - timedelta(hours=redream_hours)).isoformat()
        )
        deadline = asyncio.get_running_loop().time() + self._max_runtime_seconds()
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
            self.memory_manager.list_dream_scopes, redream_cutoff=cutoff
        )
        truth_triggered_targets = await self._truth_changed_project_ids(targets)
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
            if not self._admission_window_open(deadline):
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
                result = await self._run_nested_target(options)
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
        drops out (scope enumeration lacks the ``review-lesson`` exclusion that
        candidate listing applies, so a scope can be due yet yield zero
        candidates — dropping it out prevents an infinite pass loop). A
        dependency failure stops the whole coordinator; completed checkpoints
        stay durable and untouched candidates remain due. Structural per-scope
        failures are isolated and logged at ERROR.
        """
        # Non-preview sweeps clear the cooldown for truth-shifted projects
        # before the first enumeration so they are re-judged this run.
        await self._apply_truth_change_triggers()

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
                    self.memory_manager.list_dream_scopes, redream_cutoff=cutoff
                )
                due = [scope for scope in targets if _scope_sweep_key(scope) not in done]
                if not due:
                    break
                passes += 1
                for scope in due:
                    key = _scope_sweep_key(scope)
                    if not self._admission_window_open(deadline):
                        stop_reason = "window_exhausted"
                        remaining_scopes = len([s for s in due if _scope_sweep_key(s) not in done])
                        stopping = True
                        break
                    sweep = sweeps.get(key)
                    if sweep is None:
                        try:
                            sweep = await self._open_scope_sweep(
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
                        outcome = await self._run_scope_unit(sweep)
                    except DreamDependencyError as exc:
                        dependency_failure = str(exc)
                        stop_reason = "dependency_failure"
                        failed += 1
                        done.add(key)
                        entries[key] = await self._close_scope_sweep(
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
                        entries[key] = await self._close_scope_sweep(
                            sweep, status="failed", error=str(exc)
                        )
                        continue
                    if outcome.candidates and not outcome.no_progress:
                        sweep.units += 1
                    if outcome.drained(sweep.unit_size):
                        done.add(key)
                        entries[key] = await self._close_scope_sweep(sweep, status="completed")
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
                        self.store.update_run,
                        sweep.run_id,
                        status="interrupted",
                        completed_at=completed_ts,
                        error=INTERRUPTED_CANCELLED_ERROR,
                    )
                with suppress(Exception):
                    await sweep.related_session.aclose()
            raise

        # Close scopes the stop cut off mid-backlog: their applied units stand,
        # their child rows record partial, and their candidates remain due.
        for key in order:
            if key in done or key not in sweeps:
                continue
            sweep = sweeps[key]
            sweep.orchestrator.stop_reason = stop_reason
            entries[key] = await self._close_scope_sweep(sweep, status="partial")
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

    def _max_runtime_seconds(self) -> int:
        return _positive_int(
            getattr(self.dream_config, "max_runtime_seconds", DEFAULT_MAX_RUNTIME_SECONDS),
            DEFAULT_MAX_RUNTIME_SECONDS,
        )

    def _admission_window_open(self, deadline: float) -> bool:
        """Whether the coordinator may still admit a new work unit."""
        return asyncio.get_running_loop().time() < deadline

    async def _open_scope_sweep(
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
            self.store.create_run,
            project_id=None if options.global_only else options.project_id,
            dry_run=False,
            options=options.to_dict(),
            status="started",
        )
        related_session = RelatedEvidenceSession()
        orchestrator = await self._build_orchestrator(run_id, options, related_session)
        return _ScopeSweep(
            scope=scope,
            options=options,
            run_id=run_id,
            orchestrator=orchestrator,
            related_session=related_session,
            unit_size=orchestrator.unit_size,
        )

    async def _run_scope_unit(self, sweep: _ScopeSweep) -> WorkUnitOutcome:
        """Execution seam for one coordinator work unit; tests intercept it."""
        return await sweep.orchestrator.run_unit()

    async def _close_scope_sweep(
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
                self.store.update_run,
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
            await asyncio.to_thread(self.store.update_run, run_id, checkpoint=checkpoint.to_dict())
        except Exception:
            logger.warning(
                "Failed to persist memory dream coordinator checkpoint %s", run_id, exc_info=True
            )

    async def _truth_changed_project_ids(
        self, scopes: list[MemoryScope] | None = None
    ) -> list[str]:
        if scopes is None:
            scopes = await asyncio.to_thread(
                self.memory_manager.list_dream_scopes,
                redream_cutoff=_ALL_MEMORIES_CUTOFF,
            )
        changed: list[str] = []
        for scope in scopes:
            project_id = scope.project_id
            if (
                scope.kind is not MemoryScopeKind.PROJECT_ONLY
                or project_id is None
                or self._is_current_daemon_project(project_id)
            ):
                continue
            try:
                repo_path = self._resolve_repo_path(project_id)
                if not repo_path:
                    continue
                digest = await build_project_truth_digest_async(repo_path)
                if not digest:
                    continue
                digest_hash = hashlib.sha256(digest.encode("utf-8")).hexdigest()
                previous = await asyncio.to_thread(self.store.get_truth_digest_hash, project_id)
                if previous != digest_hash:
                    changed.append(project_id)
            except Exception:
                logger.exception(
                    "memory dream: truth-change detection failed for project %s",
                    project_id,
                )
        return changed

    async def _apply_truth_change_triggers(self) -> None:
        """Clear the cooldown for projects whose codewiki truth digest changed.

        A project whose memories are all within the cooldown window is skipped by
        the due enumeration, so a stack change captured by a codewiki refresh
        would otherwise wait a full cooldown cycle before being re-judged. For
        every memory-bearing project this compares the current rendered truth
        digest against the last-seen hash; on a change it clears that project's
        cooldown cursor so the upcoming sweep re-judges its memories against the
        new stack, then records the new hash. Globally visible memories and the
        daemon's own project use platform truth rather than a per-project
        codewiki digest and are skipped. Per-project failures are isolated.
        """
        await self._apply_platform_truth_change_trigger()
        for project_id in await self._truth_changed_project_ids():
            try:
                repo_path = self._resolve_repo_path(project_id)
                if not repo_path:
                    continue
                digest = await build_project_truth_digest_async(repo_path)
                if not digest:
                    continue
                digest_hash = hashlib.sha256(digest.encode("utf-8")).hexdigest()
                previous = await asyncio.to_thread(self.store.get_truth_digest_hash, project_id)
                if previous == digest_hash:
                    continue
                reset = await asyncio.to_thread(
                    self.memory_manager.mark_project_memories_due, project_id
                )
                await asyncio.to_thread(self.store.set_truth_digest_hash, project_id, digest_hash)
                logger.info(
                    "memory dream: truth digest changed for project %s; "
                    "cleared cooldown for %d memory(ies)",
                    project_id,
                    reset,
                )
            except Exception:
                logger.exception(
                    "memory dream: truth-change trigger failed for project %s",
                    project_id,
                )

    async def _apply_platform_truth_change_trigger(self) -> None:
        try:
            digest = build_current_truth_digest(self._daemon_config)
            digest_hash = hashlib.sha256(digest.encode("utf-8")).hexdigest()
            previous = await asyncio.to_thread(self.store.get_platform_truth_digest_hash)
            if previous == digest_hash:
                return
            global_reset = await asyncio.to_thread(self.memory_manager.mark_global_memories_due)
            project_reset = 0
            if self.current_project_id:
                project_reset = await asyncio.to_thread(
                    self.memory_manager.mark_project_memories_due,
                    self.current_project_id,
                )
            await asyncio.to_thread(self.store.set_platform_truth_digest_hash, digest_hash)
            logger.info(
                "memory dream: platform truth digest changed; "
                "cleared cooldown for %d global and %d current-project memory(ies)",
                global_reset,
                project_reset,
            )
        except Exception:
            logger.exception("memory dream: platform truth-change trigger failed")

    async def _ensure_schema_async(self) -> None:
        if self._schema_ready:
            return
        await asyncio.to_thread(self.store.ensure_schema)
        self._schema_ready = True

    async def start_async(self, options: DreamRunOptions) -> dict[str, Any]:
        if not self.dream_config.enabled:
            return {"success": False, "error": "memory dream is disabled"}
        await self._ensure_schema_async()
        run_project_id = None if options.global_only else options.project_id
        admission = await asyncio.to_thread(
            self.store.admit_run,
            project_id=run_project_id,
            dry_run=options.dry_run,
            options=options.to_dict(),
        )
        return _admission_payload(admission)

    async def start_all_due_projects_async(
        self,
        *,
        dry_run: bool = False,
        skip_consolidation: bool = False,
        memory_type: str | None = None,
        full_sweep: bool = False,
    ) -> dict[str, Any]:
        if not self.dream_config.enabled:
            return {"success": False, "error": "memory dream is disabled"}
        await self._ensure_schema_async()
        options = {
            "aggregate": True,
            "dry_run": dry_run,
            "skip_consolidation": skip_consolidation,
            "memory_type": memory_type,
            "full_sweep": full_sweep,
        }
        admission = await asyncio.to_thread(
            self.store.admit_run,
            project_id=None,
            dry_run=dry_run,
            options=options,
        )
        return _admission_payload(admission)

    async def execute_all_due_projects_run(
        self,
        run_id: str,
        *,
        dry_run: bool = False,
        skip_consolidation: bool = False,
        memory_type: str | None = None,
        full_sweep: bool = False,
    ) -> dict[str, Any]:
        await self._ensure_schema_async()
        try:
            aggregate = await self.run_all_due_projects(
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
                # An early stop with durable completed work is a normal partial
                # outcome; remaining candidates stay due for the next run.
                status = "partial"
                error = aggregate.get("dependency_failure")
            else:
                status = "completed"
                error = None
            run = await asyncio.to_thread(
                self.store.update_run,
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
                    self.store.update_run,
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
        except Exception as exc:  # noqa: BLE001 - failure must be persisted on the run
            completed_ts = datetime.now(UTC).isoformat()
            run = await asyncio.to_thread(
                self.store.update_run,
                run_id,
                status="failed",
                completed_at=completed_ts,
                error=str(exc),
            )
            return {"success": False, "run_id": run_id, "run": run, "error": str(exc)}

    def start(self, options: DreamRunOptions) -> dict[str, Any]:
        if not self.dream_config.enabled:
            return {"success": False, "error": "memory dream is disabled"}
        self.store.ensure_schema()
        self._schema_ready = True
        run_project_id = None if options.global_only else options.project_id
        admission = self.store.admit_run(
            project_id=run_project_id,
            dry_run=options.dry_run,
            options=options.to_dict(),
        )
        return _admission_payload(admission)

    def record_run_failure(self, run_id: str, error: str) -> dict[str, Any] | None:
        """Persist a failed status unless the run already reached a terminal state."""
        run = self.store.get_run(run_id)
        if run is None or run.get("status") in RUN_TERMINAL_STATUSES:
            return run
        return self.store.update_run(
            run_id,
            status="failed",
            completed_at=datetime.now(UTC).isoformat(),
            error=error,
        )

    async def _build_truth_digest_async(self, options: DreamRunOptions) -> str:
        if options.global_only:
            return build_current_truth_digest(self._daemon_config)
        if options.project_id and self._is_current_daemon_project(options.project_id):
            platform_digest = build_current_truth_digest(self._daemon_config)
            project_digest = await build_project_truth_digest_async(
                self._resolve_repo_path(options.project_id)
            )
            return "\n\n".join(part for part in (platform_digest, project_digest) if part)
        if options.project_id:
            return await build_project_truth_digest_async(
                self._resolve_repo_path(options.project_id)
            )
        raise ValueError(
            "memory dream sweep requires global_only or a project_id; "
            "unscoped runs must fan out via run_all_due_projects"
        )

    def _is_current_daemon_project(self, project_id: str | None) -> bool:
        return project_id is not None and project_id == self.current_project_id

    def _resolve_repo_path(self, project_id: str) -> str | None:
        project = LocalProjectManager(self.memory_manager.db).get(project_id)
        return project.repo_path if project is not None else None

    async def execute_run(self, run_id: str, options: DreamRunOptions) -> dict[str, Any]:
        async with _execution_lock():
            return await self._execute_run_locked(run_id, options)

    async def _build_orchestrator(
        self,
        run_id: str,
        options: DreamRunOptions,
        related_session: RelatedEvidenceSession,
        *,
        admission_deadline: float | None = None,
    ) -> DreamSweepOrchestrator:
        run_started = datetime.now(UTC)
        redream_hours = _positive_int(
            getattr(self.dream_config, "redream_after_hours", DEFAULT_REDREAM_AFTER_HOURS),
            DEFAULT_REDREAM_AFTER_HOURS,
        )
        include_global = (
            options.include_global
            if options.include_global is not None
            else bool(getattr(self.dream_config, "include_global_memories", True))
        )
        # full_sweep pins the cutoff to run_start so the cooldown excludes
        # only rows stamped during this run; the unit loop still drains via
        # per-unit stamping. Cannot be expressed as redream_after_hours=0 —
        # _positive_int coerces values < 1 back to the default.
        redream_cutoff = (
            run_started.isoformat()
            if options.full_sweep
            else (run_started - timedelta(hours=redream_hours)).isoformat()
        )
        digest = await self._build_truth_digest_async(options)
        return DreamSweepOrchestrator(
            memory_manager=self.memory_manager,
            store=self.store,
            dream_config=self.dream_config,
            llm_service=self.llm_service,
            run_id=run_id,
            options=options,
            include_global=include_global,
            redream_cutoff=redream_cutoff,
            truth_digest=digest,
            run_started=run_started,
            related_session=related_session,
            admission_deadline=admission_deadline,
        )

    async def _execute_run_locked(self, run_id: str, options: DreamRunOptions) -> dict[str, Any]:
        await self._ensure_schema_async()
        related_session = RelatedEvidenceSession()
        try:
            # The admission window bounds every run regardless of trigger.
            deadline = asyncio.get_running_loop().time() + self._max_runtime_seconds()
            orchestrator = await self._build_orchestrator(
                run_id, options, related_session, admission_deadline=deadline
            )

            if options.skip_consolidation:
                plan, summary = await orchestrator.run_inventory()
            elif options.dry_run:
                plan, summary = await orchestrator.run_dry_run()
            else:
                totals = await self._stream_sweep(orchestrator)
                plan, summary = totals.to_plan(), totals.to_summary()
            completed_ts = datetime.now(UTC).isoformat()
            if orchestrator.stop_reason == "window_exhausted":
                status = "partial"
                logger.info(
                    "Memory dream run %s stopped at its admission window; "
                    "remaining candidates stay due for the next run.",
                    run_id,
                )
            else:
                status = "completed"
            run = await asyncio.to_thread(
                self.store.update_run,
                run_id,
                status=status,
                completed_at=completed_ts,
                plan=plan,
                summary=summary,
            )
            return {"success": True, "run_id": run_id, "run": run}
        except asyncio.CancelledError:
            # Mark the run interrupted (not failed) so a cancellation — daemon
            # shutdown, timeout — is distinct from a genuine failure and agrees
            # with the startup reconciliation of hard-crash orphans. Best effort:
            # if the executor cannot run during loop teardown, startup
            # reconciliation still recovers the row on next boot.
            completed_ts = datetime.now(UTC).isoformat()
            try:
                await asyncio.to_thread(
                    self.store.update_run,
                    run_id,
                    status="interrupted",
                    completed_at=completed_ts,
                    error=INTERRUPTED_CANCELLED_ERROR,
                )
            except Exception:
                logger.warning(
                    "Failed to persist interrupted memory dream run %s",
                    run_id,
                    exc_info=True,
                )
            raise
        except Exception as exc:  # noqa: BLE001 - failure must be persisted on the run
            completed_ts = datetime.now(UTC).isoformat()
            run = await asyncio.to_thread(
                self.store.update_run,
                run_id,
                status="failed",
                completed_at=completed_ts,
                error=str(exc),
            )
            return {"success": False, "run_id": run_id, "run": run, "error": str(exc)}
        finally:
            await related_session.aclose()

    async def _stream_sweep(self, orchestrator: DreamSweepOrchestrator) -> SweepTotals:
        """Execution seam for the mutating sweep; lock/cancel tests intercept it."""
        return await orchestrator.run_sweep()

    async def status(self, run_id: str) -> dict[str, Any]:
        await self._ensure_schema_async()
        run = await asyncio.to_thread(self.store.get_run, run_id)
        if run is None:
            return {"success": False, "error": f"Dream run not found: {run_id}"}
        return {"success": True, "run": run}

    async def revert(self, run_id: str) -> dict[str, Any]:
        await self._ensure_schema_async()
        return await revert_dream_run(
            store=self.store,
            run_id=run_id,
            memory_manager=self.memory_manager,
            reconcile_after_revert=self.dream_config.reconcile_after_revert,
        )


async def run_memory_dream(
    *,
    memory_manager: MemoryDreamManagerProtocol,
    dream_config: MemoryDreamConfig | None = None,
    llm_service: MemoryDreamLLMProtocol | None = None,
    daemon_config: Any = None,
    dry_run: bool = False,
    skip_consolidation: bool = False,
    memory_type: str | None = None,
    project_id: str | None = None,
    global_only: bool = False,
    include_global: bool | None = None,
    full_sweep: bool = False,
    current_project_id: str | None = None,
) -> dict[str, Any]:
    service = MemoryDreamService(
        memory_manager=memory_manager,
        dream_config=dream_config,
        llm_service=llm_service,
        daemon_config=daemon_config,
        current_project_id=current_project_id,
    )
    return await service.run(
        DreamRunOptions(
            dry_run=dry_run,
            skip_consolidation=skip_consolidation,
            memory_type=memory_type,
            project_id=project_id,
            global_only=global_only,
            include_global=include_global,
            full_sweep=full_sweep,
        )
    )


def _admission_payload(admission: DreamAdmission) -> dict[str, Any]:
    """Translate a store admission outcome into the start-result contract."""
    if admission.outcome == "admitted":
        return {"success": True, "run_id": admission.run_id}
    if admission.outcome == "coalesced":
        return {
            "success": True,
            "run_id": admission.run_id,
            "coalesced": True,
            "active": admission.active,
        }
    return {
        "success": False,
        "error": "a memory dream run is already active with incompatible options",
        "conflict": admission.active,
    }


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
