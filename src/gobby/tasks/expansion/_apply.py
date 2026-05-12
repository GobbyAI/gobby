"""Apply path for compiled task expansion specs."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, cast

from gobby.storage.expansion_runs import ExpansionRun
from gobby.storage.tasks import Task
from gobby.storage.tasks._stage_manifest import derive_child_manifest_specs
from gobby.tasks.expansion._common import (
    _TDD_CATEGORIES,
    _manifest_stage_names,
    _stable_ref_id,
    _stable_test_id,
)


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
    phase_index_by_id = {phase["id"]: i + 1 for i, phase in enumerate(phase_list)}
    phase_has_tdd = {
        phase["id"]: (
            not phase.get("tdd_sandwich_emitted")
            and any(
                task_item.get("category") in _TDD_CATEGORIES
                for task_item in tasks
                if task_item["phase_id"] == phase["id"]
            )
        )
        for phase in phase_list
    }

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

    with self.db.transaction():
        self.run_manager.mark_applying(run_id)
        self.run_manager.append_log(run_id, level="info", message="Applying compiled expansion")

        # Create phase subepics first for genuinely multi-phase expansions.
        if multi_phase:
            for phase in phase_list:
                result = self.task_manager.create_task_with_decomposition(
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
                phase_parent_map[phase["id"]] = result["task"]["id"]
                if phase_manifest_specs:
                    self.task_manager.stage_states.initialize_manifest(
                        result["task"]["id"],
                        phase_manifest_specs,
                        by_session_id=session_id,
                    )
                _inherit_build_state(
                    self,
                    parent=task,
                    task_id=result["task"]["id"],
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
            phase_number = phase_index_by_id[phase_id]
            tdd_enabled = phase_has_tdd[phase_id]

            if tdd_enabled:
                test_description = self._build_phase_test_description(phase, phase_number)
                test_validation = self._build_phase_test_validation(phase)
                test_result = self.task_manager.create_task_with_decomposition(
                    project_id=task.project_id,
                    title=f"[TEST] Phase {phase_number}: Write failing tests",
                    description=f"{plan_ref_block}{test_description}"
                    if plan_ref_block
                    else test_description,
                    priority=self._phase_priority(tasks_by_phase[phase_id]),
                    task_type="task",
                    parent_task_id=parent_id,
                    category="test",
                    validation_criteria=test_validation,
                    created_in_session_id=session_id,
                )
                created_task_map[_stable_test_id(phase_id)] = test_result["task"]["id"]
                phase_child_ids[phase_id].append(test_result["task"]["id"])
                if leaf_manifest_specs:
                    self.task_manager.stage_states.initialize_manifest(
                        test_result["task"]["id"],
                        leaf_manifest_specs,
                        by_session_id=session_id,
                    )
                _inherit_build_state(
                    self,
                    parent=task,
                    task_id=test_result["task"]["id"],
                    target_branch=target_branch,
                )
                suggested_tests = phase.get("test_intent", {}).get("suggested_test_files") or []
                if suggested_tests:
                    self.af_manager.set_files(
                        test_result["task"]["id"], suggested_tests, "expansion"
                    )

            for task_item in tasks_by_phase[phase_id]:
                raw_description = task_item.get("description") or ""
                description = (
                    f"{plan_ref_block}{raw_description}" if plan_ref_block else raw_description
                )
                create_result = self.task_manager.create_task_with_decomposition(
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
                    additional_skills=task_item.get("additional_skills"),
                )
                created_id = create_result["task"]["id"]
                created_task_map[task_item["id"]] = created_id
                phase_child_ids[phase_id].append(created_id)
                if leaf_manifest_specs:
                    self.task_manager.stage_states.initialize_manifest(
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

            if tdd_enabled:
                ref_description = self._build_phase_refactor_description(phase, phase_number)
                ref_result = self.task_manager.create_task_with_decomposition(
                    project_id=task.project_id,
                    title=f"[REF] Phase {phase_number}: Refactor with green tests",
                    description=f"{plan_ref_block}{ref_description}"
                    if plan_ref_block
                    else ref_description,
                    priority=self._phase_priority(tasks_by_phase[phase_id]),
                    task_type="task",
                    parent_task_id=parent_id,
                    category="refactor",
                    validation_criteria="All tests remain green after refactoring.",
                    created_in_session_id=session_id,
                )
                created_task_map[_stable_ref_id(phase_id)] = ref_result["task"]["id"]
                phase_child_ids[phase_id].append(ref_result["task"]["id"])
                if leaf_manifest_specs:
                    self.task_manager.stage_states.initialize_manifest(
                        ref_result["task"]["id"],
                        leaf_manifest_specs,
                        by_session_id=session_id,
                    )
                _inherit_build_state(
                    self,
                    parent=task,
                    task_id=ref_result["task"]["id"],
                    target_branch=target_branch,
                )

        deps_by_task: dict[str, list[str]] = defaultdict(list)
        external_phase_deps: dict[str, set[str]] = defaultdict(set)
        for edge in dependency_edges:
            deps_by_task[edge["task_id"]].append(edge["depends_on"])

        for task_item in tasks:
            phase_id = task_item["phase_id"]
            stable_id = task_item["id"]
            created_id = created_task_map[stable_id]
            blockers = deps_by_task.get(stable_id, [])
            is_tdd_task = task_item.get("category") in _TDD_CATEGORIES and phase_has_tdd[phase_id]
            if is_tdd_task:
                # All implementation tasks in a TDD phase depend on the phase's failing-test task.
                self._add_dependency(created_id, created_task_map[_stable_test_id(phase_id)])
            for blocker_id in blockers:
                blocker_task = next((item for item in tasks if item["id"] == blocker_id), None)
                if blocker_task is None:
                    continue
                blocker_phase_id = blocker_task["phase_id"]
                if is_tdd_task and blocker_phase_id != phase_id:
                    external_phase_deps[phase_id].add(
                        self._external_blocker_id(blocker_task, phase_has_tdd)
                    )
                    continue
                blocker_created = self._resolve_created_blocker(
                    blocker_id,
                    tasks_by_id={item["id"]: item for item in tasks},
                    created_task_map=created_task_map,
                    phase_has_tdd=phase_has_tdd,
                )
                if blocker_created:
                    self._add_dependency(created_id, blocker_created)

        for phase in phase_list:
            phase_id = phase["id"]
            if phase_has_tdd[phase_id]:
                test_id = created_task_map[_stable_test_id(phase_id)]
                for blocker_stable in sorted(external_phase_deps.get(phase_id, set())):
                    blocker_created = created_task_map.get(blocker_stable)
                    if blocker_created:
                        self._add_dependency(test_id, blocker_created)
                ref_id = created_task_map[_stable_ref_id(phase_id)]
                for task_item in tasks_by_phase[phase_id]:
                    if task_item.get("category") in _TDD_CATEGORIES:
                        self._add_dependency(ref_id, created_task_map[task_item["id"]])

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


def _phase_priority(self: Any, phase_tasks: list[dict[str, Any]]) -> int:
    """Use the highest-priority task in a phase as the sandwich task priority."""
    priorities = [int(task_item.get("priority", 2)) for task_item in phase_tasks]
    return min(priorities) if priorities else 2


def _build_phase_test_description(self: Any, phase: dict[str, Any], phase_number: int) -> str:
    """Create deterministic test-task description from explicit phase metadata."""
    test_intent = phase.get("test_intent") or {}
    lines = [test_intent.get("summary") or f"Write failing tests for Phase {phase_number}."]
    behaviors = list(test_intent.get("behaviors") or [])
    if behaviors:
        lines.append("")
        lines.append("Behaviors to verify:")
        lines.extend(f"- {behavior}" for behavior in behaviors)
    suggested_test_files = list(test_intent.get("suggested_test_files") or [])
    if suggested_test_files:
        lines.append("")
        lines.append("Suggested test files:")
        lines.extend(f"- {file_path}" for file_path in suggested_test_files)
    entry_criteria = list(test_intent.get("entry_criteria") or [])
    if entry_criteria:
        lines.append("")
        lines.append("Entry criteria:")
        lines.extend(f"- {criterion}" for criterion in entry_criteria)
    return "\n".join(lines)


def _build_phase_test_validation(self: Any, phase: dict[str, Any]) -> str:
    """Create deterministic test-task validation text."""
    test_intent = phase.get("test_intent") or {}
    behaviors = list(test_intent.get("behaviors") or [])
    if behaviors:
        return (
            "Failing tests exist for the phase behaviors and fail for expected reasons: "
            + "; ".join(behaviors)
        )
    return "Failing tests exist for the phase behavior and fail for expected reasons."


def _build_phase_refactor_description(self: Any, phase: dict[str, Any], phase_number: int) -> str:
    """Create deterministic refactor-task description from phase metadata."""
    summary = phase.get("summary") or phase.get("title") or f"Phase {phase_number}"
    return (
        f"Refactor {summary} while keeping the new tests green.\n\n"
        "Review for duplication, naming, dead code, and unnecessarily complex flows."
    )


def _external_blocker_id(
    self: Any, blocker_task: dict[str, Any], phase_has_tdd: dict[str, bool]
) -> str:
    """Map an external blocker to the effective created task that should gate downstream work."""
    blocker_phase_id = blocker_task["phase_id"]
    if phase_has_tdd.get(blocker_phase_id) and blocker_task.get("category") in _TDD_CATEGORIES:
        return _stable_ref_id(blocker_phase_id)
    return cast(str, blocker_task["id"])


def _resolve_created_blocker(
    self: Any,
    blocker_id: str,
    *,
    tasks_by_id: dict[str, dict[str, Any]],
    created_task_map: dict[str, str],
    phase_has_tdd: dict[str, bool],
) -> str | None:
    """Resolve a stable blocker ID to the created task that should enforce it."""
    blocker_task = tasks_by_id.get(blocker_id)
    if blocker_task is None:
        return None
    phase_id = blocker_task["phase_id"]
    if phase_has_tdd.get(phase_id) and blocker_task.get("category") in _TDD_CATEGORIES:
        return created_task_map.get(blocker_id)
    return created_task_map.get(blocker_id)


def _add_dependency(self: Any, task_id: str, depends_on: str) -> None:
    """Best-effort dependency creation that ignores duplicates."""
    try:
        self.dep_manager.add_dependency(task_id=task_id, depends_on=depends_on, dep_type="blocks")
    except ValueError:
        pass
