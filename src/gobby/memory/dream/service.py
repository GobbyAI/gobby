"""Memory dream service orchestration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from gobby.config.persistence import MemoryDreamConfig
from gobby.memory.dream.apply import apply_dream_plan, revert_dream_run
from gobby.memory.dream.candidates import discover_stale_candidates
from gobby.memory.dream.duplicates import find_duplicate_groups
from gobby.memory.dream.plan import validate_dream_plan
from gobby.memory.dream.planner import build_raw_plan
from gobby.memory.dream.storage import MemoryDreamStore


@dataclass(frozen=True)
class DreamRunOptions:
    dry_run: bool = False
    skip_consolidation: bool = False
    memory_type: str | None = None
    project_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "skip_consolidation": self.skip_consolidation,
            "memory_type": self.memory_type,
            "project_id": self.project_id,
        }


class MemoryDreamService:
    """Coordinate candidate discovery, planning, apply, status, and revert."""

    def __init__(
        self,
        *,
        memory_manager: Any,
        dream_config: MemoryDreamConfig | None = None,
        llm_service: Any | None = None,
    ) -> None:
        self.memory_manager = memory_manager
        self.dream_config = dream_config or MemoryDreamConfig()
        self.llm_service = llm_service
        self.store = MemoryDreamStore(memory_manager.db)

    async def run(self, options: DreamRunOptions) -> dict[str, Any]:
        if not self.dream_config.enabled:
            return {"success": False, "error": "memory dream is disabled"}

        run_id = self.start(options)["run_id"]
        return await self.execute_run(run_id, options)

    def start(self, options: DreamRunOptions) -> dict[str, Any]:
        if not self.dream_config.enabled:
            return {"success": False, "error": "memory dream is disabled"}
        run_id = self.store.create_run(
            project_id=options.project_id,
            dry_run=options.dry_run,
            options=options.to_dict(),
        )
        return {"success": True, "run_id": run_id}

    def record_run_failure(self, run_id: str, error: str) -> dict[str, Any] | None:
        """Persist a failed status unless the run already reached a terminal state."""
        run = self.store.get_run(run_id)
        if run is None or run.get("status") in {"completed", "failed", "reverted"}:
            return run
        return self.store.update_run(
            run_id,
            status="failed",
            completed_at=datetime.now(UTC).isoformat(),
            error=error,
        )

    async def execute_run(self, run_id: str, options: DreamRunOptions) -> dict[str, Any]:
        try:
            candidates = discover_stale_candidates(
                self.memory_manager,
                self.dream_config,
                project_id=options.project_id,
                memory_type=options.memory_type,
                now=datetime.now(UTC),
            )
            duplicates = [] if options.skip_consolidation else find_duplicate_groups(candidates)
            raw_plan = await build_raw_plan(
                candidates=candidates,
                duplicate_groups=duplicates,
                dream_config=self.dream_config,
                llm_service=self.llm_service,
                db=self.memory_manager.db,
                project_id=options.project_id,
                skip_consolidation=options.skip_consolidation,
            )
            raw_plan_metadata = _decode_raw_plan_metadata(raw_plan)
            actions = validate_dream_plan(
                raw_plan,
                candidates,
                min_action_confidence=self.dream_config.min_action_confidence,
                min_delete_confidence=self.dream_config.min_delete_confidence,
            )
            plan = {
                "candidate_count": len(candidates),
                "duplicate_group_count": len(duplicates),
                "planner_errors": raw_plan_metadata.get("planner_errors", []),
                "actions": [action.to_dict() for action in actions],
            }
            self.store.update_run(run_id, plan=plan)

            summary = await apply_dream_plan(
                memory_manager=self.memory_manager,
                store=self.store,
                run_id=run_id,
                actions=actions,
                candidates=candidates,
                dry_run=options.dry_run,
                reconcile_after_apply=self.dream_config.reconcile_after_apply,
            )
            summary["candidates_reviewed"] = len(candidates)
            summary["duplicate_groups"] = len(duplicates)
            completed_ts = datetime.now(UTC).isoformat()
            run = self.store.update_run(
                run_id,
                status="completed",
                completed_at=completed_ts,
                summary=summary,
            )
            return {"success": True, "run_id": run_id, "run": run}
        except Exception as exc:  # noqa: BLE001 - run status must capture failure
            completed_ts = datetime.now(UTC).isoformat()
            run = self.store.update_run(
                run_id,
                status="failed",
                completed_at=completed_ts,
                error=str(exc),
            )
            return {"success": False, "run_id": run_id, "run": run, "error": str(exc)}

    def status(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
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
    memory_manager: Any,
    dream_config: MemoryDreamConfig | None = None,
    llm_service: Any | None = None,
    dry_run: bool = False,
    skip_consolidation: bool = False,
    memory_type: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    service = MemoryDreamService(
        memory_manager=memory_manager,
        dream_config=dream_config,
        llm_service=llm_service,
    )
    return await service.run(
        DreamRunOptions(
            dry_run=dry_run,
            skip_consolidation=skip_consolidation,
            memory_type=memory_type,
            project_id=project_id,
        )
    )


def _decode_raw_plan_metadata(raw_plan: Any) -> dict[str, Any]:
    if isinstance(raw_plan, str):
        try:
            decoded = json.loads(raw_plan)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return raw_plan if isinstance(raw_plan, dict) else {}
