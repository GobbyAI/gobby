"""Memory dream service orchestration.

The nightly run is a streaming page-and-apply sweep over *active* memories. Each
page is fetched by the storage cooldown query, planned against the current-truth
digest, validated (failures degrade to visible keep), applied, and stamped — so
the cooldown cursor advances and the loop drains to zero. An immediate re-run is
a no-op because every row was just stamped inside the cooldown window. A dry-run
is a single bounded preview pass that writes nothing (no memory, snapshot, or
stamp mutations).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from gobby.config.persistence import MemoryDreamConfig
from gobby.memory.dream.apply import apply_dream_plan, revert_dream_run
from gobby.memory.dream.candidates import list_sweep_candidates
from gobby.memory.dream.plan import validate_dream_plan
from gobby.memory.dream.planner import build_raw_plan
from gobby.memory.dream.protocols import MemoryDreamLLMProtocol, MemoryDreamManagerProtocol
from gobby.memory.dream.storage import INTERRUPTED_CANCELLED_ERROR, MemoryDreamStore
from gobby.memory.dream.truth_digest import build_current_truth_digest, build_project_truth_digest
from gobby.storage.projects import LocalProjectManager

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 200
DEFAULT_REDREAM_AFTER_HOURS = 20
MAX_ACTION_SAMPLE = 50
MAX_ERROR_DETAILS = 50
MAX_PLANNER_ERRORS = 50


@dataclass(frozen=True)
class DreamRunOptions:
    dry_run: bool = False
    # Vestigial: cross-memory consolidation is out of scope for the GC sweep, so
    # the planner always runs (merge is suppressed by passing no duplicate groups).
    skip_consolidation: bool = False
    memory_type: str | None = None
    project_id: str | None = None
    global_only: bool = False
    # None uses the dream config default. Scheduled per-project runs set this to
    # False so the NULL/global bucket is swept by its own target exactly once.
    include_global: bool | None = None
    # When True, ignore the rolling redream cooldown and sweep every active
    # in-scope memory once (cutoff = run_start). Scheduled nightly runs leave
    # this False so project coverage stays cooldown-throttled.
    full_sweep: bool = False

    def __post_init__(self) -> None:
        if self.global_only and self.project_id is not None:
            raise ValueError("global_only and project_id are mutually exclusive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "skip_consolidation": self.skip_consolidation,
            "memory_type": self.memory_type,
            "project_id": self.project_id,
            "global_only": self.global_only,
            "include_global": self.include_global,
            "full_sweep": self.full_sweep,
        }


@dataclass
class _SweepTotals:
    """Bounded accumulator for a streaming sweep's summary and run record."""

    candidates_reviewed: int = 0
    pages: int = 0
    mutations: int = 0
    snapshots: int = 0
    errors: int = 0
    action_counts: dict[str, int] = field(default_factory=dict)
    error_details: list[dict[str, Any]] = field(default_factory=list)
    planner_errors: list[str] = field(default_factory=list)
    action_sample: list[dict[str, Any]] = field(default_factory=list)
    reconcile: dict[str, Any] | None = None

    def add_page(
        self,
        candidate_count: int,
        actions: list[Any],
        page_summary: dict[str, Any],
        raw_plan_metadata: dict[str, Any],
    ) -> None:
        self.candidates_reviewed += candidate_count
        self.pages += 1
        self.mutations += int(page_summary.get("mutations", 0))
        self.snapshots += int(page_summary.get("snapshots", 0))
        self.errors += int(page_summary.get("errors", 0))
        for name, count in page_summary.get("actions", {}).items():
            self.action_counts[name] = self.action_counts.get(name, 0) + int(count)
        for detail in page_summary.get("error_details", []):
            if len(self.error_details) < MAX_ERROR_DETAILS:
                self.error_details.append(detail)
        for err in raw_plan_metadata.get("planner_errors", []):
            if len(self.planner_errors) < MAX_PLANNER_ERRORS:
                self.planner_errors.append(str(err))
        for action in actions:
            if len(self.action_sample) < MAX_ACTION_SAMPLE:
                self.action_sample.append(action.to_dict())

    def to_summary(self) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "dry_run": False,
            "actions": dict(self.action_counts),
            "mutations": self.mutations,
            "snapshots": self.snapshots,
            "errors": self.errors,
            "error_details": self.error_details,
            "candidates_reviewed": self.candidates_reviewed,
            "pages": self.pages,
            "planner_errors": self.planner_errors,
        }
        if self.reconcile is not None:
            summary["reconcile"] = self.reconcile
        return summary

    def to_plan(self) -> dict[str, Any]:
        return {
            "candidate_count": self.candidates_reviewed,
            "pages": self.pages,
            "action_counts": dict(self.action_counts),
            "planner_errors": self.planner_errors,
            "action_sample": self.action_sample,
        }


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
        if not self.dream_config.enabled:
            return {"success": False, "error": "memory dream is disabled"}

        started = await self.start_async(options)
        if not started.get("success"):
            return started
        run_id = str(started["run_id"])
        return await self.execute_run(run_id, options)

    async def run_all_due_projects(
        self,
        *,
        dry_run: bool = False,
        skip_consolidation: bool = False,
        memory_type: str | None = None,
        full_sweep: bool = False,
    ) -> dict[str, Any]:
        """Sweep every project with due memories, each under its own truth digest.

        Enumerates targets via the cooldown-due predicate, then runs one sweep
        per target: the NULL/global bucket runs ``global_only`` and real projects
        run scoped with ``include_global=False`` so the global bucket is swept
        exactly once. Per-target failures are isolated. Returns an aggregate; the
        caller decides whether an all-failed batch is an error.
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

        redream_hours = _positive_int(
            getattr(self.dream_config, "redream_after_hours", DEFAULT_REDREAM_AFTER_HOURS),
            DEFAULT_REDREAM_AFTER_HOURS,
        )
        cutoff = (datetime.now(UTC) - timedelta(hours=redream_hours)).isoformat()
        targets = await asyncio.to_thread(
            self.memory_manager.list_dream_project_ids, redream_cutoff=cutoff
        )

        runs: list[dict[str, Any]] = []
        completed = 0
        failed = 0
        mutations = 0
        for target_project_id in targets:
            try:
                if target_project_id is None:
                    options = DreamRunOptions(
                        dry_run=dry_run,
                        skip_consolidation=skip_consolidation,
                        memory_type=memory_type,
                        global_only=True,
                        full_sweep=full_sweep,
                    )
                else:
                    options = DreamRunOptions(
                        dry_run=dry_run,
                        skip_consolidation=skip_consolidation,
                        memory_type=memory_type,
                        project_id=target_project_id,
                        include_global=False,
                        full_sweep=full_sweep,
                    )
                result = await self.run(options)
                target_mutations = _completed_mutation_count(result)
                mutations += target_mutations
                completed += 1
                runs.append(
                    {
                        "project_id": target_project_id,
                        "success": True,
                        "run_id": _result_run_id(result),
                        "mutations": target_mutations,
                    }
                )
            except Exception as exc:
                logger.exception("memory dream failed for target %s", target_project_id)
                failed += 1
                runs.append({"project_id": target_project_id, "success": False, "error": str(exc)})

        all_failed = failed > 0 and completed == 0
        return {
            "success": not all_failed,
            "targets": len(targets),
            "completed": completed,
            "failed": failed,
            "mutations": mutations,
            "runs": runs,
        }

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
        run_id = await asyncio.to_thread(
            self.store.create_run,
            project_id=run_project_id,
            dry_run=options.dry_run,
            options=options.to_dict(),
        )
        return {"success": True, "run_id": run_id}

    def start(self, options: DreamRunOptions) -> dict[str, Any]:
        if not self.dream_config.enabled:
            return {"success": False, "error": "memory dream is disabled"}
        self.store.ensure_schema()
        self._schema_ready = True
        run_project_id = None if options.global_only else options.project_id
        run_id = self.store.create_run(
            project_id=run_project_id,
            dry_run=options.dry_run,
            options=options.to_dict(),
        )
        return {"success": True, "run_id": run_id}

    def record_run_failure(self, run_id: str, error: str) -> dict[str, Any] | None:
        """Persist a failed status unless the run already reached a terminal state."""
        run = self.store.get_run(run_id)
        if run is None or run.get("status") in {
            "completed",
            "failed",
            "reverted",
            "revert_failed",
            "interrupted",
        }:
            return run
        return self.store.update_run(
            run_id,
            status="failed",
            completed_at=datetime.now(UTC).isoformat(),
            error=error,
        )

    def _build_truth_digest(self, options: DreamRunOptions) -> str:
        if options.global_only:
            return build_current_truth_digest(self._daemon_config)
        if options.project_id and self._is_current_daemon_project(options.project_id):
            return build_current_truth_digest(self._daemon_config)
        if options.project_id:
            return build_project_truth_digest(self._resolve_repo_path(options.project_id))
        # Every sweep must carry an explicit scope: a concrete project_id or
        # global_only. Unscoped manual triggers fan out through
        # run_all_due_projects, so this branch is unreachable. Guard it instead
        # of silently judging all projects' memories against the gobby platform
        # truth digest — that cross-project contamination is the bug this path
        # used to cause.
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
        await self._ensure_schema_async()
        try:
            run_started = datetime.now(UTC)
            page_size = _positive_int(
                getattr(self.dream_config, "page_size", DEFAULT_PAGE_SIZE), DEFAULT_PAGE_SIZE
            )
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
            # only rows stamped during this run; the page loop still drains via
            # per-page stamping. Cannot be expressed as redream_after_hours=0 —
            # _positive_int coerces values < 1 back to the default.
            redream_cutoff = (
                run_started.isoformat()
                if options.full_sweep
                else (run_started - timedelta(hours=redream_hours)).isoformat()
            )
            digest = self._build_truth_digest(options)

            if options.dry_run:
                return await self._execute_dry_run(
                    run_id, options, redream_cutoff, digest, page_size, include_global, run_started
                )

            totals = await self._stream_sweep(
                run_id, options, redream_cutoff, digest, page_size, include_global, run_started
            )
            if totals.mutations and self.dream_config.reconcile_after_apply:
                await self._reconcile(totals)
            completed_ts = datetime.now(UTC).isoformat()
            run = await asyncio.to_thread(
                self.store.update_run,
                run_id,
                status="completed",
                completed_at=completed_ts,
                plan=totals.to_plan(),
                summary=totals.to_summary(),
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

    async def _stream_sweep(
        self,
        run_id: str,
        options: DreamRunOptions,
        redream_cutoff: str,
        digest: str,
        page_size: int,
        include_global: bool,
        run_started: datetime,
    ) -> _SweepTotals:
        totals = _SweepTotals()
        previous_ids: set[str] | None = None
        while True:
            candidates = await list_sweep_candidates(
                self.memory_manager,
                limit=page_size,
                redream_cutoff=redream_cutoff,
                project_id=options.project_id,
                memory_type=options.memory_type,
                include_global=include_global,
                global_only=options.global_only,
                now=run_started,
            )
            if not candidates:
                break
            page_ids = {candidate.id for candidate in candidates}
            if previous_ids is not None and page_ids == previous_ids:
                # Cursor failed to advance (e.g. persistent stamp failure); stop
                # rather than loop forever.
                logger.warning("Memory dream sweep made no progress on run %s; stopping", run_id)
                break
            previous_ids = page_ids
            stamp = datetime.now(UTC).isoformat()
            raw_plan = await build_raw_plan(
                candidates=candidates,
                duplicate_groups=[],
                dream_config=self.dream_config,
                llm_service=self.llm_service,
                db=self.memory_manager.db,
                project_id=options.project_id,
                skip_consolidation=False,
                truth_digest=digest,
            )
            raw_plan_metadata = _decode_raw_plan_metadata(raw_plan)
            actions = validate_dream_plan(
                raw_plan,
                candidates,
                min_action_confidence=self.dream_config.min_action_confidence,
                min_delete_confidence=self.dream_config.min_delete_confidence,
                min_rescope_confidence=self.dream_config.min_rescope_confidence,
            )
            page_summary = await apply_dream_plan(
                memory_manager=self.memory_manager,
                store=self.store,
                run_id=run_id,
                actions=actions,
                candidates=candidates,
                dry_run=False,
                reconcile_after_apply=False,
                when=stamp,
            )
            totals.add_page(len(candidates), actions, page_summary, raw_plan_metadata)
            if len(candidates) < page_size:
                break
        return totals

    async def _execute_dry_run(
        self,
        run_id: str,
        options: DreamRunOptions,
        redream_cutoff: str,
        digest: str,
        page_size: int,
        include_global: bool,
        run_started: datetime,
    ) -> dict[str, Any]:
        candidates = await list_sweep_candidates(
            self.memory_manager,
            limit=page_size,
            redream_cutoff=redream_cutoff,
            project_id=options.project_id,
            memory_type=options.memory_type,
            include_global=include_global,
            global_only=options.global_only,
            now=run_started,
        )
        raw_plan = await build_raw_plan(
            candidates=candidates,
            duplicate_groups=[],
            dream_config=self.dream_config,
            llm_service=self.llm_service,
            db=self.memory_manager.db,
            project_id=options.project_id,
            skip_consolidation=False,
            truth_digest=digest,
        )
        raw_plan_metadata = _decode_raw_plan_metadata(raw_plan)
        actions = validate_dream_plan(
            raw_plan,
            candidates,
            min_action_confidence=self.dream_config.min_action_confidence,
            min_delete_confidence=self.dream_config.min_delete_confidence,
            min_rescope_confidence=self.dream_config.min_rescope_confidence,
        )
        summary = await apply_dream_plan(
            memory_manager=self.memory_manager,
            store=self.store,
            run_id=run_id,
            actions=actions,
            candidates=candidates,
            dry_run=True,
            reconcile_after_apply=False,
        )
        planner_errors = [str(err) for err in raw_plan_metadata.get("planner_errors", [])][
            :MAX_PLANNER_ERRORS
        ]
        summary["candidates_reviewed"] = len(candidates)
        summary["pages"] = 1 if candidates else 0
        summary["planner_errors"] = planner_errors
        plan = {
            "candidate_count": len(candidates),
            "dry_run": True,
            "planner_errors": planner_errors,
            "action_sample": [action.to_dict() for action in actions][:MAX_ACTION_SAMPLE],
        }
        completed_ts = datetime.now(UTC).isoformat()
        run = await asyncio.to_thread(
            self.store.update_run,
            run_id,
            status="completed",
            completed_at=completed_ts,
            plan=plan,
            summary=summary,
        )
        return {"success": True, "run_id": run_id, "run": run}

    async def _reconcile(self, totals: _SweepTotals) -> None:
        try:
            totals.reconcile = await self.memory_manager.reconcile_stores(dry_run=False)
        except Exception as exc:  # noqa: BLE001 - reconcile must not hide applied mutations
            totals.reconcile = {"error": str(exc)}
            logger.warning("Memory dream reconcile failed: %s", exc, exc_info=True)

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


def _positive_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 1 else default


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


def _decode_raw_plan_metadata(raw_plan: Any) -> dict[str, Any]:
    if isinstance(raw_plan, str):
        try:
            decoded = json.loads(raw_plan)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return raw_plan if isinstance(raw_plan, dict) else {}
