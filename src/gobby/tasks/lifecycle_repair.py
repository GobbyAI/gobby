"""Scoped repair helpers for historical lifecycle manifests."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from gobby.storage.tasks import LocalTaskManager, StageManifestSpec, StageState, Task
from gobby.storage.tasks._stage_manifest import derive_child_manifest_specs

RepairAction = Literal["remove_unused_manifest", "reseed_expansion_manifest"]


@dataclass(slots=True)
class LifecycleRepairCandidate:
    task_id: str
    ref: str
    title: str
    action: RepairAction
    reason: str
    current_manifest: list[dict[str, Any]]
    desired_manifest: list[dict[str, Any]]
    skipped: bool = False
    skip_reason: str | None = None
    applied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "ref": self.ref,
            "title": self.title,
            "action": self.action,
            "reason": self.reason,
            "current_manifest": self.current_manifest,
            "desired_manifest": self.desired_manifest,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "applied": self.applied,
        }


@dataclass(slots=True)
class LifecycleRepairResult:
    apply: bool
    scope: str
    candidates: list[LifecycleRepairCandidate]

    def to_dict(self) -> dict[str, Any]:
        return {
            "apply": self.apply,
            "scope": self.scope,
            "count": len(self.candidates),
            "applied_count": sum(1 for item in self.candidates if item.applied),
            "skipped_count": sum(1 for item in self.candidates if item.skipped),
            "candidates": [item.to_dict() for item in self.candidates],
        }


class LifecycleRepair:
    """Find and optionally repair scoped historical lifecycle manifest drift."""

    def __init__(self, task_manager: LocalTaskManager) -> None:
        self.task_manager = task_manager

    def run(
        self,
        *,
        task_id: str | None = None,
        provenance: str | None = None,
        apply: bool = False,
        force: bool = False,
    ) -> LifecycleRepairResult:
        if task_id is None and provenance is None:
            raise ValueError("repair-lifecycle requires --task or --provenance")
        if task_id is not None and provenance is not None:
            raise ValueError("repair-lifecycle accepts only one of --task or --provenance")
        if force and task_id is None:
            raise ValueError("--force is only allowed with --task")

        tasks = self._scoped_tasks(task_id=task_id, provenance=provenance)
        candidates = [
            candidate
            for task in tasks
            if (
                candidate := self._candidate_for_task(
                    task, task_scoped=task_id is not None, force=force
                )
            )
            is not None
        ]
        if apply:
            for candidate in candidates:
                if not candidate.skipped:
                    self._apply_candidate(candidate, force=force and task_id is not None)
        scope = f"task:{task_id}" if task_id else f"provenance:{provenance}"
        return LifecycleRepairResult(apply=apply, scope=scope, candidates=candidates)

    def _scoped_tasks(self, *, task_id: str | None, provenance: str | None) -> list[Task]:
        if task_id is not None:
            return [self.task_manager.get_task(task_id)]
        assert provenance is not None
        return self._tasks_with_label(provenance)

    def _tasks_with_label(self, label: str) -> list[Task]:
        rows = self.task_manager.db.fetchall(
            """
            SELECT id, labels
              FROM tasks
             WHERE labels LIKE ?
             ORDER BY seq_num, created_at
            """,
            (f"%{label}%",),
        )
        task_ids: list[str] = []
        for row in rows:
            labels = _decode_labels(row["labels"])
            if label in labels:
                task_ids.append(row["id"])
        return [self.task_manager.get_task(task_id) for task_id in task_ids]

    def _candidate_for_task(
        self,
        task: Task,
        *,
        task_scoped: bool,
        force: bool,
    ) -> LifecycleRepairCandidate | None:
        if _has_expansion_provenance(task):
            return self._expansion_candidate(task, task_scoped=task_scoped, force=force)
        return self._unused_auto_seed_candidate(task)

    def _unused_auto_seed_candidate(self, task: Task) -> LifecycleRepairCandidate | None:
        rows = self.task_manager.stage_states.list_for_task(task.id)
        if not rows or task.task_type == "review_anchor":
            return None
        if not _is_pristine_manifest(rows):
            return None
        if _has_build_event(self.task_manager, task.id) or _has_expansion_provenance(task):
            return None
        if not _is_metadata_only(self.task_manager, task):
            return None
        return LifecycleRepairCandidate(
            task_id=task.id,
            ref=_task_ref(task),
            title=task.title,
            action="remove_unused_manifest",
            reason="pristine metadata-only task has auto-seeded lifecycle rows",
            current_manifest=_rows_payload(rows),
            desired_manifest=[],
        )

    def _expansion_candidate(
        self,
        task: Task,
        *,
        task_scoped: bool,
        force: bool,
    ) -> LifecycleRepairCandidate | None:
        if task.parent_task_id is None:
            return None
        parent_rows = self.task_manager.stage_states.list_for_task(task.parent_task_id)
        rows = self.task_manager.stage_states.list_for_task(task.id)
        desired = derive_child_manifest_specs(
            parent_rows,
            include_holistic_qa=task.task_type == "epic",
        )
        if (
            desired
            and _first_stage(rows) == "development"
            and _first_stage(desired) != "development"
        ):
            return None
        if not desired:
            desired = _historical_development_first_specs(
                rows,
                include_holistic_qa=task.task_type == "epic",
            )
            if not desired:
                return None

        if _manifest_signature(rows) == _manifest_signature(desired):
            return None

        skipped = bool(rows) and not _is_pristine_manifest(rows) and not (force and task_scoped)
        return LifecycleRepairCandidate(
            task_id=task.id,
            ref=_task_ref(task),
            title=task.title,
            action="reseed_expansion_manifest",
            reason="expansion child manifest differs from parent-derived lifecycle scope",
            current_manifest=_rows_payload(rows),
            desired_manifest=_specs_payload(desired),
            skipped=skipped,
            skip_reason="active_lifecycle_rows" if skipped else None,
        )

    def _apply_candidate(self, candidate: LifecycleRepairCandidate, *, force: bool) -> None:
        if candidate.action == "remove_unused_manifest":
            self.task_manager.db.execute(
                "DELETE FROM task_stage_states WHERE task_id = ?",
                (candidate.task_id,),
            )
            self._record(candidate, "repair-lifecycle:remove-unused-manifest")
            candidate.applied = True
            return

        specs = [
            StageManifestSpec(
                stage_name=item["stage_name"],
                position=int(item["position"]),
                max_work_attempts=item.get("max_work_attempts"),
                max_review_rounds=item.get("max_review_rounds"),
            )
            for item in candidate.desired_manifest
        ]
        if force:
            self.task_manager.db.execute(
                "DELETE FROM task_stage_states WHERE task_id = ?",
                (candidate.task_id,),
            )
        self.task_manager.stage_states.initialize_manifest(
            candidate.task_id,
            specs,
            by_session_id=None,
        )
        self._record(candidate, "repair-lifecycle:reseed-expansion-manifest")
        candidate.applied = True

    def _record(self, candidate: LifecycleRepairCandidate, reason: str) -> None:
        self.task_manager.lifecycle_events.record_lifecycle_event(
            candidate.task_id,
            from_state=_manifest_display(candidate.current_manifest),
            to_state=_manifest_display(candidate.desired_manifest),
            reason=reason,
            by_actor="repair-lifecycle",
        )


def _decode_labels(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(decoded, list):
        return []
    return [str(item) for item in decoded]


def _has_expansion_provenance(task: Task) -> bool:
    return any(label.startswith("expansion-run:") for label in task.labels or [])


def _has_build_event(task_manager: LocalTaskManager, task_id: str) -> bool:
    return bool(
        task_manager.db.fetchone(
            """
            SELECT 1
              FROM task_lifecycle_events
             WHERE task_id = ?
               AND reason = 'gobby build'
             LIMIT 1
            """,
            (task_id,),
        )
    )


def _has_non_manifest_lifecycle_event(task_manager: LocalTaskManager, task_id: str) -> bool:
    return bool(
        task_manager.db.fetchone(
            """
            SELECT 1
              FROM task_lifecycle_events
             WHERE task_id = ?
               AND reason != 'initialize_manifest'
             LIMIT 1
            """,
            (task_id,),
        )
    )


def _is_metadata_only(task_manager: LocalTaskManager, task: Task) -> bool:
    if task.claimed_by_session_id or task.closed_at or task.escalated_at or task.is_escalated:
        return False
    if _has_non_manifest_lifecycle_event(task_manager, task.id):
        return False
    artifacts = task_manager.artifacts.get_artifacts(task.id)
    return not any(
        (
            artifacts.plan_file_path,
            artifacts.plan_file_hash,
            artifacts.worktree_path,
            artifacts.worktree_id,
            artifacts.clone_path,
            artifacts.clone_id,
            artifacts.base_commit_sha,
            artifacts.target_branch,
            artifacts.expansion_run_id,
            artifacts.expansion_attempts,
        )
    )


def _is_pristine_manifest(rows: list[StageState]) -> bool:
    return all(
        row.state == "ready"
        and row.entered_at is None
        and row.completed_at is None
        and row.work_attempt_count == 0
        and row.review_round_count == 0
        and row.artifact_refs is None
        and row.notes is None
        for row in rows
    )


def _first_stage(rows: Sequence[StageState | StageManifestSpec]) -> str | None:
    if not rows:
        return None
    return min(rows, key=lambda item: item.position).stage_name


def _historical_development_first_specs(
    rows: list[StageState],
    *,
    include_holistic_qa: bool,
) -> list[StageManifestSpec]:
    if _first_stage(rows) == "development":
        return []
    by_name = {row.stage_name: row for row in rows}
    inherited = [stage_name for stage_name in ("pr", "merge") if stage_name in by_name]
    stage_names = ["development"]
    if include_holistic_qa and "holistic_qa" in by_name:
        stage_names.append("holistic_qa")
    stage_names.extend(inherited)
    return [
        StageManifestSpec(
            stage_name=stage_name,
            position=position,
            max_work_attempts=getattr(by_name.get(stage_name), "max_work_attempts", None),
            max_review_rounds=getattr(by_name.get(stage_name), "max_review_rounds", None),
        )
        for position, stage_name in enumerate(stage_names)
    ]


def _manifest_signature(
    rows: Sequence[StageState | StageManifestSpec],
) -> list[tuple[str, int | None, int | None]]:
    return [
        (row.stage_name, row.max_work_attempts, row.max_review_rounds)
        for row in sorted(rows, key=lambda item: item.position)
    ]


def _rows_payload(rows: list[StageState]) -> list[dict[str, Any]]:
    return [
        {
            "stage_name": row.stage_name,
            "position": row.position,
            "state": row.state,
            "max_work_attempts": row.max_work_attempts,
            "max_review_rounds": row.max_review_rounds,
        }
        for row in sorted(rows, key=lambda item: item.position)
    ]


def _specs_payload(specs: list[StageManifestSpec]) -> list[dict[str, Any]]:
    return [
        {
            "stage_name": row.stage_name,
            "position": row.position,
            "max_work_attempts": row.max_work_attempts,
            "max_review_rounds": row.max_review_rounds,
        }
        for row in sorted(specs, key=lambda item: item.position)
    ]


def _manifest_display(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "manifest:empty"
    return "manifest:" + ",".join(str(row["stage_name"]) for row in rows)


def _task_ref(task: Task) -> str:
    return f"#{task.seq_num}" if task.seq_num else task.id
