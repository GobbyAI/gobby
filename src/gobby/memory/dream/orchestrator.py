"""Bounded work-unit orchestration for memory dream runs.

A work unit is the atomic execution step of a dream run: select at most
``WORK_UNIT_MAX_CANDIDATES`` eligible candidates from one explicit scope,
gather required related evidence, call the planner serially, validate the
returned actions, apply them, then persist the run checkpoint. The service
(``gobby.memory.dream.service``) stays the facade for admission, status,
revert, and execution entry points; this module owns the long-running loop
bodies for mutating sweeps, dry runs, and skip-consolidation inventories.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from gobby.config.persistence import MemoryDreamConfig
from gobby.memory.dream.apply import apply_dream_plan
from gobby.memory.dream.candidates import list_sweep_candidates
from gobby.memory.dream.models import DreamAction, DreamCandidate, DreamCheckpoint
from gobby.memory.dream.options import DreamRunOptions, dream_scope_key
from gobby.memory.dream.plan import validate_dream_plan
from gobby.memory.dream.planner import build_raw_plan
from gobby.memory.dream.protocols import MemoryDreamLLMProtocol, MemoryDreamManagerProtocol
from gobby.memory.dream.related import (
    RelatedEvidenceError,
    RelatedEvidenceSession,
    gather_related_evidence,
)
from gobby.memory.dream.storage import MemoryDreamStore
from gobby.storage.memories_scope import MemoryScope

logger = logging.getLogger(__name__)

WORK_UNIT_MAX_CANDIDATES = 25
WORK_UNIT_DEADLINE_SECONDS = 1500.0
MAX_ACTION_SAMPLE = 50
MAX_ERROR_DETAILS = 50
MAX_PLANNER_ERRORS = 50


class DreamDependencyError(RuntimeError):
    """A required work-unit dependency failed.

    Raised for planner absence, invalid terminal planner output, exhausted
    provider fallback, or a work unit exceeding ``WORK_UNIT_DEADLINE_SECONDS``.
    The failed unit's candidates keep their cooldown cursors untouched — no
    implicit keep actions, no stamps — so they remain due for a later run.
    """


def _positive_float(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return float(value) if value > 0 else default


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


@dataclass
class SweepTotals:
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


@dataclass
class WorkUnitOutcome:
    """Result of one work unit: what was selected, planned, and applied."""

    candidates: list[DreamCandidate]
    actions: list[DreamAction]
    page_summary: dict[str, Any]
    raw_plan_metadata: dict[str, Any]
    no_progress: bool = False

    def drained(self, unit_size: int) -> bool:
        """True when this unit ended its scope: empty, stuck, or a short page."""
        return not self.candidates or self.no_progress or len(self.candidates) < unit_size


class DreamSweepOrchestrator:
    """Run-scoped executor for sweep, dry-run, and inventory work units."""

    def __init__(
        self,
        *,
        memory_manager: MemoryDreamManagerProtocol,
        store: MemoryDreamStore,
        dream_config: MemoryDreamConfig,
        llm_service: MemoryDreamLLMProtocol | None,
        run_id: str,
        options: DreamRunOptions,
        include_global: bool,
        redream_cutoff: str,
        truth_digest: str,
        run_started: datetime,
        related_session: RelatedEvidenceSession,
        admission_deadline: float | None = None,
    ) -> None:
        self.memory_manager = memory_manager
        self.store = store
        self.dream_config = dream_config
        self.llm_service = llm_service
        self.run_id = run_id
        self.options = options
        self.include_global = include_global
        self.redream_cutoff = redream_cutoff
        self.truth_digest = truth_digest
        self.run_started = run_started
        self.related_session = related_session
        # Monotonic (event-loop clock) instant after which no new unit starts.
        self.admission_deadline = admission_deadline
        self.totals = SweepTotals()
        self.stop_reason = "drained"
        self._scope_key = dream_scope_key(options.to_dict())
        self._previous_ids: set[str] | None = None
        self._planned = 0
        self._applied_actions = 0
        self._last_dependency_failure: str | None = None
        self._drained = False

    @property
    def unit_size(self) -> int:
        configured = _positive_int(
            getattr(self.dream_config, "planner_batch_size", WORK_UNIT_MAX_CANDIDATES),
            WORK_UNIT_MAX_CANDIDATES,
        )
        return min(configured, WORK_UNIT_MAX_CANDIDATES)

    def window_open(self) -> bool:
        """Whether the admission window still allows starting a new unit."""
        if self.admission_deadline is None:
            return True
        return asyncio.get_running_loop().time() < self.admission_deadline

    async def run_sweep(self) -> SweepTotals:
        """Drain the run's scope in bounded work units, checkpointing each."""
        while not self._drained:
            if not self.window_open():
                self.stop_reason = "window_exhausted"
                break
            await self.run_unit()
        return await self.finalize_sweep()

    async def run_unit(self) -> WorkUnitOutcome:
        """Execute one bounded work unit and fold it into the running totals.

        A dependency failure persists its checkpoint and re-raises; callers own
        the run-status transition. All other outcomes checkpoint progress and
        mark the sweep drained once the scope stops yielding full units.
        """
        try:
            outcome = await self._run_unit(previous_ids=self._previous_ids)
        except DreamDependencyError as exc:
            self._last_dependency_failure = str(exc)
            self.stop_reason = "dependency_failure"
            await self._persist_checkpoint(self._checkpoint(stop_reason=self.stop_reason))
            raise
        if not outcome.candidates:
            self._drained = True
            return outcome
        if outcome.no_progress:
            # Cursor failed to advance (e.g. persistent stamp failure); stop
            # rather than loop forever.
            logger.warning("Memory dream sweep made no progress on run %s; stopping", self.run_id)
            self.stop_reason = "no_progress"
            self._drained = True
            return outcome
        self._previous_ids = {candidate.id for candidate in outcome.candidates}
        self.totals.add_page(
            len(outcome.candidates),
            outcome.actions,
            outcome.page_summary,
            outcome.raw_plan_metadata,
        )
        self._planned += len(outcome.actions)
        self._applied_actions += sum(
            int(count) for count in outcome.page_summary.get("actions", {}).values()
        )
        await self._persist_checkpoint(self._checkpoint(selected=len(outcome.candidates)))
        if len(outcome.candidates) < self.unit_size:
            self._drained = True
        return outcome

    async def finalize_sweep(self) -> SweepTotals:
        """Persist the final checkpoint and reconcile applied mutations."""
        await self._persist_checkpoint(self._checkpoint(stop_reason=self.stop_reason))
        if self.totals.mutations and self.dream_config.reconcile_after_apply:
            await self._reconcile(self.totals)
        return self.totals

    def _checkpoint(self, *, selected: int = 0, stop_reason: str | None = None) -> DreamCheckpoint:
        return DreamCheckpoint(
            phase="sweep",
            scope=self._scope_key,
            batch_number=self.totals.pages,
            selected=selected,
            completed=self.totals.candidates_reviewed,
            planned=self._planned,
            actions=self._applied_actions,
            mutations=self.totals.mutations,
            stop_reason=stop_reason,
            last_dependency_failure=self._last_dependency_failure,
        )

    async def run_dry_run(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Preview the run over an immutable ordered eligible-ID snapshot."""
        candidate_limit = self.dream_config.dry_run_max_candidates
        candidate_ids = await asyncio.to_thread(
            self.memory_manager.list_dream_candidate_ids,
            redream_cutoff=self.redream_cutoff,
            scope=self._scope(),
            memory_type=self.options.memory_type,
            limit=candidate_limit + 1,
        )
        candidates_truncated = len(candidate_ids) > candidate_limit
        candidate_ids = candidate_ids[:candidate_limit]
        totals = self.totals
        for offset in range(0, len(candidate_ids), self.unit_size):
            if not self.window_open():
                self.stop_reason = "window_exhausted"
                break
            unit_ids = candidate_ids[offset : offset + self.unit_size]
            try:
                outcome = await self._run_unit(candidate_ids=unit_ids, dry_run=True)
            except DreamDependencyError as exc:
                self._last_dependency_failure = str(exc)
                self.stop_reason = "dependency_failure"
                await self._persist_checkpoint(self._checkpoint(stop_reason=self.stop_reason))
                raise
            if not outcome.candidates:
                continue
            totals.add_page(
                len(outcome.candidates),
                outcome.actions,
                outcome.page_summary,
                outcome.raw_plan_metadata,
            )
            self._planned += len(outcome.actions)
            await self._persist_checkpoint(self._checkpoint(selected=len(outcome.candidates)))
        # Terminal rows carry a complete checkpoint even for preview runs.
        await self._persist_checkpoint(self._checkpoint(stop_reason=self.stop_reason))
        action_count = sum(totals.action_counts.values())
        summary = totals.to_summary()
        summary["dry_run"] = True
        summary["planned_actions"] = totals.action_sample
        summary["planned_action_count"] = action_count
        summary["candidates_truncated"] = candidates_truncated
        summary["candidate_limit"] = candidate_limit
        plan = totals.to_plan()
        plan.update(
            {
                "dry_run": True,
                "actions": totals.action_sample,
                "action_count": action_count,
                "candidates_truncated": candidates_truncated,
                "candidate_limit": candidate_limit,
            }
        )
        return plan, summary

    async def run_inventory(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Materialize the eligible-ID inventory for skip-consolidation runs.

        Performs zero evidence, planner, action, snapshot, mutation, or cursor
        calls; inventory candidates remain due for a later ordinary run.
        """
        candidate_ids = await asyncio.to_thread(
            self.memory_manager.list_dream_candidate_ids,
            redream_cutoff=self.redream_cutoff,
            scope=self._scope(),
            memory_type=self.options.memory_type,
        )
        count = len(candidate_ids)
        plan = {
            "skip_consolidation": True,
            "candidate_count": count,
            "candidate_ids": candidate_ids[:MAX_ACTION_SAMPLE],
            "candidate_ids_truncated": count > MAX_ACTION_SAMPLE,
        }
        summary = {
            "dry_run": self.options.dry_run,
            "skip_consolidation": True,
            "candidates_eligible": count,
            "actions": {},
            "mutations": 0,
            "snapshots": 0,
            "errors": 0,
        }
        return plan, summary

    async def _run_unit(
        self,
        *,
        candidate_ids: list[str] | None = None,
        dry_run: bool = False,
        previous_ids: set[str] | None = None,
    ) -> WorkUnitOutcome:
        deadline = _positive_float(
            getattr(self.dream_config, "work_unit_timeout_seconds", None),
            WORK_UNIT_DEADLINE_SECONDS,
        )
        try:
            async with asyncio.timeout(deadline):
                return await self._run_unit_inner(
                    candidate_ids=candidate_ids,
                    dry_run=dry_run,
                    previous_ids=previous_ids,
                )
        except TimeoutError as exc:
            raise DreamDependencyError(
                f"memory dream work unit exceeded {deadline:g}s deadline"
            ) from exc

    async def _run_unit_inner(
        self,
        *,
        candidate_ids: list[str] | None,
        dry_run: bool,
        previous_ids: set[str] | None,
    ) -> WorkUnitOutcome:
        candidates = await list_sweep_candidates(
            self.memory_manager,
            limit=self.unit_size,
            redream_cutoff=self.redream_cutoff,
            scope=self._scope(),
            memory_type=self.options.memory_type,
            candidate_ids=candidate_ids,
            now=self.run_started,
        )
        if not candidates:
            return WorkUnitOutcome(candidates=[], actions=[], page_summary={}, raw_plan_metadata={})
        if previous_ids is not None and {candidate.id for candidate in candidates} == previous_ids:
            return WorkUnitOutcome(
                candidates=candidates,
                actions=[],
                page_summary={},
                raw_plan_metadata={},
                no_progress=True,
            )
        if self.llm_service is None:
            raise DreamDependencyError(
                "memory dream planner unavailable: no LLM service configured"
            )
        try:
            candidates = await self._attach_related_evidence(candidates)
        except RelatedEvidenceError as exc:
            raise DreamDependencyError(f"memory dream evidence failed: {exc}") from exc
        stamp = datetime.now(UTC).isoformat()
        raw_plan = await build_raw_plan(
            candidates=candidates,
            dream_config=self.dream_config,
            llm_service=self.llm_service,
            db=self.memory_manager.db,
            project_id=self.options.project_id,
            skip_consolidation=self.options.skip_consolidation,
            truth_digest=self.truth_digest,
        )
        raw_plan_metadata = _decode_raw_plan_metadata(raw_plan)
        planner_errors = raw_plan_metadata.get("planner_errors") or []
        if planner_errors:
            raise DreamDependencyError(f"memory dream planner failed: {planner_errors[-1]}")
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
            run_id=self.run_id,
            actions=actions,
            candidates=candidates,
            dry_run=dry_run,
            reconcile_after_apply=False,
            when=None if dry_run else stamp,
        )
        return WorkUnitOutcome(
            candidates=candidates,
            actions=actions,
            page_summary=page_summary,
            raw_plan_metadata=raw_plan_metadata,
        )

    async def _attach_related_evidence(
        self, candidates: list[DreamCandidate]
    ) -> list[DreamCandidate]:
        if not self.dream_config.related_evidence_enabled:
            return candidates
        retrieval_scope = self.options.retrieval_scope(include_global=self.include_global)
        if retrieval_scope is None:
            return candidates
        return await gather_related_evidence(
            candidates,
            db=self.memory_manager.db,
            vector_store=getattr(self.memory_manager, "_vector_store", None),
            dream_config=self.dream_config,
            session=self.related_session,
            scope=retrieval_scope,
        )

    async def _persist_checkpoint(self, checkpoint: DreamCheckpoint) -> None:
        await asyncio.to_thread(self.store.update_run, self.run_id, checkpoint=checkpoint.to_dict())

    async def _reconcile(self, totals: SweepTotals) -> None:
        try:
            totals.reconcile = await self.memory_manager.reconcile_stores(dry_run=False)
        except Exception as exc:  # Reconciliation must preserve visibility of applied mutations.
            totals.reconcile = {"error": str(exc)}
            logger.warning("Memory dream reconcile failed: %s", exc, exc_info=True)

    def _scope(self) -> MemoryScope:
        return self.options.memory_scope(include_global=self.include_global)
