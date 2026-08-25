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
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from weakref import WeakKeyDictionary

from gobby.config.persistence import MemoryDreamConfig
from gobby.memory.dream.aggregate import (
    ALL_MEMORIES_CUTOFF,
    _AggregateDreamRunner,
    _ScopeSweep,
)
from gobby.memory.dream.apply import revert_dream_run
from gobby.memory.dream.options import DreamRunOptions
from gobby.memory.dream.orchestrator import (
    DreamSweepOrchestrator,
    SweepTotals,
    WorkUnitOutcome,
    _positive_int,
)
from gobby.memory.dream.protocols import MemoryDreamLLMProtocol, MemoryDreamManagerProtocol
from gobby.memory.dream.related import RelatedEvidenceSession
from gobby.memory.dream.storage import MemoryDreamStore
from gobby.memory.dream.storage_runs import (
    INTERRUPTED_CANCELLED_ERROR,
    RUN_TERMINAL_STATUSES,
    DreamAdmission,
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
        capture_bundle: Callable[[], Any] | None = None,
    ) -> None:
        self._seed_memory_manager = memory_manager
        self.dream_config = dream_config or MemoryDreamConfig()
        self._seed_llm_service = llm_service
        self._capture_bundle = capture_bundle
        self._daemon_config = daemon_config
        self.current_project_id = current_project_id
        self.store = MemoryDreamStore(memory_manager.db)
        self._aggregate_runner = _AggregateDreamRunner(self)

    @property
    def memory_manager(self) -> MemoryDreamManagerProtocol:
        """Resolve the current runtime epoch's memory manager per use."""
        if self._capture_bundle is not None:
            service = self._capture_bundle().services.get("memory_services")
            manager = getattr(service, "memory_manager", None)
            if manager is not None:
                return cast(MemoryDreamManagerProtocol, manager)
        return self._seed_memory_manager

    @property
    def llm_service(self) -> MemoryDreamLLMProtocol | None:
        """Resolve the current runtime epoch's LLM service per use."""
        if self._capture_bundle is not None:
            service = self._capture_bundle().services.get("ai_services")
            resolved = getattr(service, "llm_service", None)
            if resolved is not None:
                return cast(MemoryDreamLLMProtocol, resolved)
        return self._seed_llm_service

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
            return await self._aggregate_runner.run_all_due_projects_locked(
                dry_run=dry_run,
                skip_consolidation=skip_consolidation,
                memory_type=memory_type,
                full_sweep=full_sweep,
                run_id=run_id,
            )

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
        return await self._aggregate_runner.open_scope_sweep(
            scope, memory_type=memory_type, full_sweep=full_sweep
        )

    async def _run_scope_unit(self, sweep: _ScopeSweep) -> WorkUnitOutcome:
        return await self._aggregate_runner.run_scope_unit(sweep)

    async def _close_scope_sweep(
        self, sweep: _ScopeSweep, *, status: str, error: str | None = None
    ) -> dict[str, Any]:
        return await self._aggregate_runner.close_scope_sweep(sweep, status=status, error=error)

    async def _truth_changed_project_ids(
        self, scopes: list[MemoryScope] | None = None
    ) -> list[str]:
        if scopes is None:
            scopes = await asyncio.to_thread(
                self.memory_manager.list_dream_scopes,
                redream_cutoff=ALL_MEMORIES_CUTOFF,
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

    async def start_async(self, options: DreamRunOptions) -> dict[str, Any]:
        if not self.dream_config.enabled:
            return {"success": False, "error": "memory dream is disabled"}
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
        return await self._aggregate_runner.execute_all_due_projects_run(
            run_id,
            dry_run=dry_run,
            skip_consolidation=skip_consolidation,
            memory_type=memory_type,
            full_sweep=full_sweep,
        )

    def start(self, options: DreamRunOptions) -> dict[str, Any]:
        if not self.dream_config.enabled:
            return {"success": False, "error": "memory dream is disabled"}
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
        except Exception as exc:  # Persist every terminal failure on the dream run.
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
        run = await asyncio.to_thread(self.store.get_run, run_id)
        if run is None:
            return {"success": False, "error": f"Dream run not found: {run_id}"}
        return {"success": True, "run": run}

    async def revert(self, run_id: str) -> dict[str, Any]:
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
