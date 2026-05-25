"""Apply path for compiled task expansion specs."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, cast

from gobby.storage.expansion_runs import ExpansionRun
from gobby.storage.hub.protocol import TaskSeqAllocation
from gobby.storage.tasks import Task
from gobby.storage.tasks._creation import _create_task_in_transaction
from gobby.storage.tasks._stage_manifest import derive_child_manifest_specs
from gobby.tasks.expansion._common import _manifest_stage_names


def _parent_target_branch(self: Any, parent_task_id: str) -> str | None:
    artifacts = self.task_manager.artifacts.get_artifacts(parent_task_id)
    target_branch = artifacts.integration_branch or artifacts.target_branch
    return str(target_branch) if target_branch else None


def _copy_target_branch_to_leaf(
    self: Any,
    *,
    task_id: str,
    target_branch: str | None,
) -> None:
    if target_branch:
        self.task_manager.artifacts.set_artifact(task_id, "target_branch", target_branch)


def _inherit_build_state(
    self: Any,
    *,
    parent: Task,
    task_id: str,
    target_branch: str | None,
) -> None:
    _copy_target_branch_to_leaf(self, task_id=task_id, target_branch=target_branch)
    if parent.allow_automation:
        self.task_manager.update_task(
            task_id,
            allow_automation=True,
            unattended=parent.unattended,
            isolation=parent.isolation.value,
        )


def _complete_dev_only_run(self: Any, run_id: str, task: Task) -> ExpansionRun:
    """Complete dev-only builds without creating expansion children."""
    self._complete_parent_expansion_stage_if_current(task.id, session_id=None)
    self.run_manager.append_log(
        run_id,
        level="info",
        message="Skipping expansion because dev is the only enabled stage",
        extra={"enabled_stages": sorted(_manifest_stage_names(task))},
    )
    run = self.run_manager.save_apply_result(
        run_id,
        task_id_map={task.id: task.id},
        created_task_ids=[],
        checkpoints={"dev_only_skip": True},
        completed=True,
    )
    if run is None:
        raise RuntimeError(f"Expansion run {run_id} disappeared after dev-only completion")
    return cast(ExpansionRun, run)


def apply_run(self: Any, run_id: str, *, session_id: str | None) -> ExpansionRun:
    """Apply a compiled expansion spec to the task tree."""
    run = self.run_manager.get(run_id)
    if run is None:
        raise ValueError(f"Expansion run {run_id} not found")
    if not run.compiled_spec:
        raise ValueError(f"Expansion run {run_id} has no compiled spec")

    task = self.task_manager.get_task(run.parent_task_id)
    if task is None:
        raise ValueError(f"Parent task {run.parent_task_id} not found")
    parent_manifest = self.task_manager.stage_states.list_for_task(task.id)
    phase_manifest_specs = derive_child_manifest_specs(
        parent_manifest,
        include_holistic_qa=True,
    )
    leaf_manifest_specs = derive_child_manifest_specs(
        parent_manifest,
        include_holistic_qa=False,
    )

    spec = run.compiled_spec
    validation = self.validate_compiled_spec(spec)
    if not validation["valid"]:
        errors = "; ".join(validation["errors"])
        raise ValueError(f"Cannot apply invalid compiled spec: {errors}")
    existing_output = self.find_apply_blocking_expansion_output(task.id)
    if existing_output is not None:
        raise ValueError(
            "Expansion output already exists for this task. "
            "Reset expansion output before applying a new run."
        )

    phase_list = spec["phases"]
    tasks = spec["tasks"]
    dependency_edges = spec["dependencies"]
    multi_phase = len(phase_list) > 1
    epic_validation = "All expanded child tasks must be completed."
    plan_ref_block = ""
    if run.plan_file:
        plan_ref_block = (
            f"> **Plan reference:** `{run.plan_file}`\n"
            "> Your task description below is authoritative; the plan is supporting context only.\n\n"
        )

    phase_parent_map: dict[str, str] = {}
    created_task_map: dict[str, str] = {}
    phase_child_ids: dict[str, list[str]] = defaultdict(list)
    task_label_map = {}
    target_branch = _parent_target_branch(self, task.id)
    provenance_label = f"expansion-run:{run_id}"
    for task_item in tasks:
        labels = list(task_item.get("labels") or [])
        if task_item.get("execution_group"):
            labels.append(f"parallel:{task_item['execution_group']}")
        labels.append(provenance_label)
        task_label_map[task_item["id"]] = labels or None

    with self.db.transaction_immediate(TaskSeqAllocation(project_id=task.project_id)) as conn:
        self.run_manager.mark_applying(run_id)
        self.run_manager.append_log(run_id, level="info", message="Applying compiled expansion")

        # Create phase subepics first for genuinely multi-phase expansions.
        if multi_phase:
            for phase in phase_list:
                created_id = _create_task_in_transaction(
                    self.db,
                    conn,
                    project_id=task.project_id,
                    title=phase["title"],
                    task_type="epic",
                    parent_task_id=task.id,
                    category="planning",
                    validation_criteria=epic_validation,
                    created_in_session_id=session_id,
                    description=phase.get("summary"),
                    labels=[provenance_label],
                )
                phase_parent_map[phase["id"]] = created_id
                if phase_manifest_specs:
                    self.task_manager.stage_states.insert_new_task_manifest_in_transaction(
                        conn,
                        created_id,
                        phase_manifest_specs,
                        by_session_id=session_id,
                    )
                _inherit_build_state(
                    self,
                    parent=task,
                    task_id=created_id,
                    target_branch=target_branch,
                )
        else:
            phase_parent_map = {phase["id"]: task.id for phase in phase_list}

        tasks_by_phase: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for task_item in tasks:
            tasks_by_phase[task_item["phase_id"]].append(task_item)

        for phase in phase_list:
            phase_id = phase["id"]
            parent_id = phase_parent_map[phase_id]

            for task_item in tasks_by_phase[phase_id]:
                raw_description = task_item.get("description") or ""
                description = (
                    f"{plan_ref_block}{raw_description}" if plan_ref_block else raw_description
                )
                created_id = _create_task_in_transaction(
                    self.db,
                    conn,
                    project_id=task.project_id,
                    title=task_item["title"],
                    description=description or None,
                    priority=task_item.get("priority", 2),
                    task_type=task_item.get("task_type", "task"),
                    parent_task_id=parent_id,
                    category=task_item.get("category"),
                    validation_criteria=task_item.get("validation"),
                    created_in_session_id=session_id,
                    labels=task_label_map.get(task_item["id"]),
                    assigned_agent=task_item.get("assigned_agent"),
                    implementation_domain=task_item.get("implementation_domain"),
                    additional_skills=task_item.get("additional_skills"),
                )
                created_task_map[task_item["id"]] = created_id
                phase_child_ids[phase_id].append(created_id)
                if leaf_manifest_specs:
                    self.task_manager.stage_states.insert_new_task_manifest_in_transaction(
                        conn,
                        created_id,
                        leaf_manifest_specs,
                        by_session_id=session_id,
                    )
                _inherit_build_state(
                    self,
                    parent=task,
                    task_id=created_id,
                    target_branch=target_branch,
                )
                affected_files = task_item.get("affected_files") or []
                if affected_files:
                    self.af_manager.set_files(created_id, affected_files, "expansion")

        deps_by_task: dict[str, list[str]] = defaultdict(list)
        for edge in dependency_edges:
            deps_by_task[edge["task_id"]].append(edge["depends_on"])

        for task_item in tasks:
            stable_id = task_item["id"]
            created_id = created_task_map[stable_id]
            blockers = deps_by_task.get(stable_id, [])
            for blocker_id in blockers:
                blocker_created = created_task_map.get(blocker_id)
                if blocker_created:
                    self._add_dependency(created_id, blocker_created)

        if multi_phase:
            for phase in phase_list:
                subepic_id = phase_parent_map[phase["id"]]
                for child_id in phase_child_ids.get(phase["id"], []):
                    self._add_dependency(subepic_id, child_id)
                self._add_dependency(task.id, subepic_id)
        else:
            for child_id in phase_child_ids.get(phase_list[0]["id"], []):
                self._add_dependency(task.id, child_id)

        created_ids = list(
            dict.fromkeys(
                [
                    *(
                        phase_parent_map[phase["id"]]
                        for phase in phase_list
                        if phase_parent_map[phase["id"]] != task.id
                    ),
                    *created_task_map.values(),
                ]
            )
        )
        self.task_manager.artifacts.set_artifact(task.id, "expansion_run_id", run_id)
        self._complete_parent_expansion_stage_if_current(task.id, session_id=session_id)
        run = self.run_manager.save_apply_result(
            run_id,
            task_id_map=created_task_map,
            created_task_ids=created_ids,
            checkpoints={
                "phase_parent_map": phase_parent_map,
                "apply_validation": self.validate_applied_run(
                    run_id, compiled_spec=spec, task_id_map=created_task_map
                ),
            },
            completed=True,
        )
        conn.after_commit(self.task_manager._notify_listeners)
    self.run_manager.append_log(
        run_id,
        level="info",
        message="Expansion apply completed",
        extra={"created_count": len(created_ids), "multi_phase": multi_phase},
    )
    if run is None:
        raise RuntimeError(f"Expansion run {run_id} disappeared after apply")
    return cast(ExpansionRun, run)


def validate_applied_run(
    self: Any,
    run_id: str,
    *,
    compiled_spec: dict[str, Any] | None = None,
    task_id_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Validate that an applied run created the expected task mapping."""
    run = self.run_manager.get(run_id)
    spec = compiled_spec or (run.compiled_spec if run else None)
    mapping = task_id_map or (run.task_id_map if run else None)
    errors: list[str] = []
    if spec is None:
        return {"valid": False, "errors": ["Expansion run has no compiled spec"]}
    if mapping is None:
        return {"valid": False, "errors": ["Expansion run has no applied task mapping"]}

    for task_item in spec["tasks"]:
        stable_id = task_item["id"]
        if stable_id not in mapping:
            errors.append(f"Missing created task mapping for {stable_id}")
            continue
        try:
            self.task_manager.get_task(mapping[stable_id])
        except Exception:
            errors.append(f"Created task {mapping[stable_id]} for {stable_id} no longer exists")

    return {"valid": not errors, "errors": errors}


def _get_expansion_config(self: Any) -> Any | None:
    """Return task expansion config when available."""
    if self.config is None:
        return None
    return self.config.get_gobby_tasks_config().expansion


def _add_dependency(self: Any, task_id: str, depends_on: str) -> None:
    """Best-effort dependency creation that ignores duplicates."""
    try:
        self.dep_manager.add_dependency(task_id=task_id, depends_on=depends_on, dep_type="blocks")
    except ValueError:
        pass
