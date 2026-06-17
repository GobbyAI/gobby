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
from gobby.memory.dream.truth_digest import build_current_truth_digest

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
    # When True, ignore the rolling redream cooldown and sweep every active
    # in-scope memory once (cutoff = run_start). The scheduled nightly run sets
    # this so its coverage is deterministic regardless of off-schedule stamping;
    # manual/ad-hoc runs leave it False to stay cooldown-throttled.
    full_sweep: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "skip_consolidation": self.skip_consolidation,
            "memory_type": self.memory_type,
            "project_id": self.project_id,
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
    ) -> None:
        self.memory_manager = memory_manager
        self.dream_config = dream_config or MemoryDreamConfig()
        self.llm_service = llm_service
        self._daemon_config = daemon_config
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

    async def _ensure_schema_async(self) -> None:
        if self._schema_ready:
            return
        await asyncio.to_thread(self.store.ensure_schema)
        self._schema_ready = True

    async def start_async(self, options: DreamRunOptions) -> dict[str, Any]:
        if not self.dream_config.enabled:
            return {"success": False, "error": "memory dream is disabled"}
        await self._ensure_schema_async()
        run_id = await asyncio.to_thread(
            self.store.create_run,
            project_id=options.project_id,
            dry_run=options.dry_run,
            options=options.to_dict(),
        )
        return {"success": True, "run_id": run_id}

    def start(self, options: DreamRunOptions) -> dict[str, Any]:
        if not self.dream_config.enabled:
            return {"success": False, "error": "memory dream is disabled"}
        self.store.ensure_schema()
        self._schema_ready = True
        run_id = self.store.create_run(
            project_id=options.project_id,
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
            include_global = bool(getattr(self.dream_config, "include_global_memories", True))
            # full_sweep pins the cutoff to run_start so the cooldown excludes
            # only rows stamped during this run; the page loop still drains via
            # per-page stamping. Cannot be expressed as redream_after_hours=0 —
            # _positive_int coerces values < 1 back to the default.
            redream_cutoff = (
                run_started.isoformat()
                if options.full_sweep
                else (run_started - timedelta(hours=redream_hours)).isoformat()
            )
            digest = build_current_truth_digest(self._daemon_config)

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
    full_sweep: bool = False,
) -> dict[str, Any]:
    service = MemoryDreamService(
        memory_manager=memory_manager,
        dream_config=dream_config,
        llm_service=llm_service,
        daemon_config=daemon_config,
    )
    return await service.run(
        DreamRunOptions(
            dry_run=dry_run,
            skip_consolidation=skip_consolidation,
            memory_type=memory_type,
            project_id=project_id,
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


def _decode_raw_plan_metadata(raw_plan: Any) -> dict[str, Any]:
    if isinstance(raw_plan, str):
        try:
            decoded = json.loads(raw_plan)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return raw_plan if isinstance(raw_plan, dict) else {}
