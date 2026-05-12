"""Plan-Coverage Contract compilation: turns a parsed plan document into a compiled expansion spec."""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

from gobby.plans.parser import (
    Kind,
    ManifestEntry,
    PlanDocument,
    PlanParseError,
    PlanSection,
    parse_plan,
)
from gobby.storage.expansion_runs import ExpansionRun
from gobby.storage.plans import LocalPlanManager
from gobby.storage.tasks import Task
from gobby.tasks.expansion._common import (
    _CONTRACT_PHASE_ID_RE,
    _DEFAULT_PHASE_ID,
    _contract_acceptance_lines,
    _contract_affected_files,
    _contract_deferral_record,
    _contract_phase_number,
    _contract_phase_spec_id,
    _contract_plan_id,
    _contract_section_body,
    _contract_single_task_id,
    _contract_task_ids,
    _dedupe_dependencies,
    _find_test_files,
    _stable_ref_id,
    _stable_test_id,
)

logger = logging.getLogger(__name__)


def compile_plan_to_spec(self: Any, plan_doc: PlanDocument, task: Task) -> dict[str, Any]:
    """Compile a Plan-Coverage Contract document into a compiled expansion spec.

    TDD shape (per ``docs/guides/tdd-enforcement.md``): each phase containing
    TDD-eligible (``tdd: true``) manifest entries is wrapped with a single
    phase-level ``[TEST] Phase N: Write failing tests`` task at the start
    and a single ``[REF] Phase N: Refactor with green tests`` task at the
    end. Per-entry ``[IMPL]`` tasks sit in the middle. Non-TDD entries
    emit single tasks without a prefix. Cross-phase: phase ``N+1``'s
    ``[TEST]`` depends on phase ``N``'s ``[REF]``. Cross-deliverable
    ``depends_on`` edges link IMPL/single tasks directly.
    """
    plan_id = _contract_plan_id(plan_doc)
    section_by_id = {section.section_id: section for section in plan_doc.sections}
    phase_by_section_id = self._contract_phase_index(plan_doc)
    manifest_entry_by_section = self._validate_contract_manifest(plan_doc, section_by_id)

    phases: list[dict[str, Any]] = []
    phase_by_id: dict[str, dict[str, Any]] = {}
    tasks: list[dict[str, Any]] = []
    dependencies: list[dict[str, str]] = []
    deferrals = self._contract_deferrals(plan_doc)

    # Group manifest entries by phase id, preserving declaration order.
    entries_by_phase_id: dict[str, list[ManifestEntry]] = {}
    phase_section_by_phase_id: dict[str, PlanSection | None] = {}
    phase_id_order: list[str] = []
    for entry in plan_doc.manifest_entries:
        phase_section = phase_by_section_id.get(entry.source_section)
        phase_source_id = (
            phase_section.section_id if phase_section is not None else _DEFAULT_PHASE_ID
        )
        phase_id = _contract_phase_spec_id(phase_source_id)
        if phase_id not in entries_by_phase_id:
            entries_by_phase_id[phase_id] = []
            phase_section_by_phase_id[phase_id] = phase_section
            phase_id_order.append(phase_id)
        entries_by_phase_id[phase_id].append(entry)

    # entry.source_section -> id of the per-entry IMPL or single task.
    # Used to wire cross-deliverable depends_on edges below.
    entry_work_task_id: dict[str, str] = {}
    tdd_phase_chain: list[tuple[str, str]] = []

    for phase_id in phase_id_order:
        phase_section = phase_section_by_phase_id[phase_id]
        phase = self._ensure_contract_phase(
            phases=phases,
            phase_by_id=phase_by_id,
            phase_section=phase_section,
            task=task,
        )
        phase_entries = entries_by_phase_id[phase_id]
        tdd_entries = [e for e in phase_entries if e.tdd]
        non_tdd_entries = [e for e in phase_entries if not e.tdd]

        # Aggregate phase test_intent from TDD entries before emitting the
        # phase-level [TEST] task so its description / validation can cite
        # cross-section behaviors.
        for entry in tdd_entries:
            section = section_by_id[entry.source_section]
            phase["test_intent"]["behaviors"].extend(
                item.prose for item in section.acceptance_items
            )
            phase["test_intent"]["suggested_test_files"] = sorted(
                set(phase["test_intent"]["suggested_test_files"])
                | set(_find_test_files(_contract_affected_files(section)))
            )

        phase_number = _contract_phase_number(
            phase_section.section_id if phase_section is not None else _DEFAULT_PHASE_ID
        )

        if tdd_entries:
            # Phase-level [TEST] at the start.
            phase_test_id = _stable_test_id(phase_id)
            phase_test_task = self._build_contract_phase_sandwich_task(
                kind="test",
                phase_id=phase_id,
                phase=phase,
                phase_number=phase_number,
                tdd_entries=tdd_entries,
                section_by_id=section_by_id,
            )
            tasks.append(phase_test_task)
            phase["task_ids"].append(phase_test_id)
            phase["tdd_sandwich_emitted"] = True

            # Per-entry [IMPL] tasks.
            impl_ids: list[str] = []
            for entry in tdd_entries:
                section = section_by_id[entry.source_section]
                impl_task = self._build_contract_entry_work_task(
                    plan_doc=plan_doc,
                    entry=entry,
                    section=section,
                    phase_id=phase_id,
                    title_prefix="IMPL",
                )
                tasks.append(impl_task)
                phase["task_ids"].append(impl_task["id"])
                dependencies.append({"task_id": impl_task["id"], "depends_on": phase_test_id})
                entry_work_task_id[entry.source_section] = impl_task["id"]
                impl_ids.append(impl_task["id"])

            # Phase-level [REF] at the end.
            phase_ref_id = _stable_ref_id(phase_id)
            phase_ref_task = self._build_contract_phase_sandwich_task(
                kind="ref",
                phase_id=phase_id,
                phase=phase,
                phase_number=phase_number,
                tdd_entries=tdd_entries,
                section_by_id=section_by_id,
            )
            tasks.append(phase_ref_task)
            phase["task_ids"].append(phase_ref_id)
            for impl_id in impl_ids:
                dependencies.append({"task_id": phase_ref_id, "depends_on": impl_id})
            tdd_phase_chain.append((phase_test_id, phase_ref_id))
        else:
            phase["tdd_sandwich_emitted"] = False

        # Non-TDD entries: emit a single task each (no prefix).
        for entry in non_tdd_entries:
            section = section_by_id[entry.source_section]
            single_task = self._build_contract_entry_work_task(
                plan_doc=plan_doc,
                entry=entry,
                section=section,
                phase_id=phase_id,
                title_prefix=None,
            )
            tasks.append(single_task)
            phase["task_ids"].append(single_task["id"])
            entry_work_task_id[entry.source_section] = single_task["id"]

    # Cross-deliverable dependencies — wire IMPL→IMPL or single→IMPL across
    # the graph using each entry's manifest depends_on list.
    for entry in plan_doc.manifest_entries:
        for dep_section in entry.depends_on:
            blocker = section_by_id.get(dep_section)
            blocker_entry = manifest_entry_by_section.get(dep_section)
            if blocker is None or blocker.kind is not Kind.deliverable or blocker_entry is None:
                raise ValueError(
                    f"manifest entry source_section={entry.source_section!r} depends on "
                    f"{dep_section!r}, which has no manifest entry"
                )
            dependencies.append(
                {
                    "task_id": entry_work_task_id[entry.source_section],
                    "depends_on": entry_work_task_id[dep_section],
                }
            )

    # Implicit phase sequencing is a default, not a stronger requirement than
    # explicit manifest dependencies. Some plans intentionally have an earlier
    # phase task depend on a later phase prerequisite; adding a phase N+1 TEST →
    # phase N REF edge in that case would create a cycle and hide the real
    # dependency order expressed by the manifest.
    for (prior_test_id, prior_ref_id), (phase_test_id, _phase_ref_id) in zip(
        tdd_phase_chain, tdd_phase_chain[1:], strict=False
    ):
        if _dependency_path_exists(
            dependencies,
            start_task_id=prior_ref_id,
            target_task_id=phase_test_id,
        ):
            logger.info(
                "Skipping implicit phase dependency that conflicts with manifest edges",
                extra={
                    "task_id": phase_test_id,
                    "depends_on": prior_ref_id,
                    "prior_phase_test_id": prior_test_id,
                },
            )
            continue
        dependencies.append({"task_id": phase_test_id, "depends_on": prior_ref_id})

    return {
        "version": 1,
        "parent_task_id": task.id,
        "plan_file": str(plan_doc.source_path),
        "phases": phases,
        "tasks": tasks,
        "dependencies": _dedupe_dependencies(dependencies),
        "execution_groups": [],
        "deferrals": deferrals,
        "contract_plan": True,
        "plan_id": plan_id,
        "deliverable_count": len(plan_doc.manifest_entries),
    }


def _validate_contract_manifest(
    self: Any,
    plan_doc: PlanDocument,
    section_by_id: dict[str, PlanSection],
) -> dict[str, ManifestEntry]:
    manifest_entry_by_section = {entry.source_section: entry for entry in plan_doc.manifest_entries}
    for entry in plan_doc.manifest_entries:
        section = section_by_id.get(entry.source_section)
        if section is None or section.kind is not Kind.deliverable:
            raise ValueError(
                f"manifest entry source_section={entry.source_section!r} "
                "does not resolve to a kind: deliverable section"
            )

    deliverable_ids = {
        section.section_id for section in plan_doc.sections if section.kind is Kind.deliverable
    }
    orphan_deliverables = sorted(deliverable_ids - set(manifest_entry_by_section))
    if orphan_deliverables:
        raise ValueError(
            f"kind: deliverable sections without manifest entries: {', '.join(orphan_deliverables)}"
        )
    return manifest_entry_by_section


def _contract_deferrals(self: Any, plan_doc: PlanDocument) -> list[dict[str, Any]]:
    deferrals: list[dict[str, Any]] = []
    for section in plan_doc.sections:
        if section.kind is not Kind.deferred:
            continue
        record = _contract_deferral_record(section)
        if record is not None:
            deferrals.append(record)
    return deferrals


def _ensure_contract_phase(
    self: Any,
    *,
    phases: list[dict[str, Any]],
    phase_by_id: dict[str, dict[str, Any]],
    phase_section: PlanSection | None,
    task: Task,
) -> dict[str, Any]:
    phase_source_id = phase_section.section_id if phase_section is not None else _DEFAULT_PHASE_ID
    phase_id = _contract_phase_spec_id(phase_source_id)
    phase = phase_by_id.get(phase_id)
    if phase is not None:
        return phase

    phase_number = _contract_phase_number(phase_source_id)
    phase = {
        "id": phase_id,
        "title": phase_section.title if phase_section is not None else task.title,
        "summary": phase_section.title if phase_section is not None else task.description or "",
        "test_intent": {
            "summary": f"Write failing tests for Phase {phase_number}.",
            "behaviors": [],
            "suggested_test_files": [],
            "entry_criteria": ["Tests should fail before implementation begins."],
        },
        "task_ids": [],
        "tdd_sandwich_emitted": True,
    }
    phases.append(phase)
    phase_by_id[phase_id] = phase
    return phase


def _build_contract_phase_sandwich_task(
    self: Any,
    *,
    kind: str,
    phase_id: str,
    phase: dict[str, Any],
    phase_number: int,
    tdd_entries: list[ManifestEntry],
    section_by_id: dict[str, PlanSection],
) -> dict[str, Any]:
    """Build a phase-level ``[TEST]`` (kind=``"test"``) or ``[REF]`` (kind=``"ref"``) task.

    Aggregates labels, affected-file hints, and the assigned agent across
    the TDD entries so the phase wrapper carries the union of provenance.
    """
    is_test = kind == "test"
    title = (
        f"[TEST] Phase {phase_number}: Write failing tests"
        if is_test
        else f"[REF] Phase {phase_number}: Refactor with green tests"
    )
    description = (
        self._build_phase_test_description(phase, phase_number)
        if is_test
        else self._build_phase_refactor_description(phase, phase_number)
    )
    validation = (
        self._build_phase_test_validation(phase)
        if is_test
        else "All tests remain green after refactoring."
    )

    labels: set[str] = set()
    affected_files: set[str] = set()
    for entry in tdd_entries:
        labels.update(entry.labels)
        section = section_by_id[entry.source_section]
        section_files = _contract_affected_files(section)
        if is_test:
            affected_files.update(_find_test_files(section_files))
        else:
            affected_files.update(section_files)

    assigned_agent = tdd_entries[0].assigned_agent if tdd_entries else None

    task_id = _stable_test_id(phase_id) if is_test else _stable_ref_id(phase_id)
    return {
        "id": task_id,
        "task_id": task_id,
        "phase_id": phase_id,
        "title": title,
        "description": description,
        "priority": 2,
        "task_type": "task",
        "category": "test" if is_test else "refactor",
        "validation": validation,
        "affected_files": sorted(affected_files),
        "labels": sorted(labels),
        "assigned_agent": assigned_agent,
        "additional_skills": [],
        "source_section_id": None,
    }


def _build_contract_entry_work_task(
    self: Any,
    *,
    plan_doc: PlanDocument,
    entry: ManifestEntry,
    section: PlanSection,
    phase_id: str,
    title_prefix: str | None,
) -> dict[str, Any]:
    """Build the per-entry work task — ``[IMPL]``-prefixed for TDD entries
    (``title_prefix="IMPL"``) or unprefixed for non-TDD entries
    (``title_prefix=None``)."""
    body = _contract_section_body(plan_doc, section)
    affected_files = _contract_affected_files(section)
    acceptance_lines = _contract_acceptance_lines(section)
    description = f"Plan section `{section.section_id}`.\n\n{body}".strip()
    if acceptance_lines:
        description = f"{description}\n\nAcceptance items:\n" + "\n".join(acceptance_lines)
    validation = _contract_validation_criteria(entry, section)

    if title_prefix is None:
        task_id = _contract_single_task_id(section.section_id)
        title = entry.title
    else:
        _test_id, impl_id, _ref_id = _contract_task_ids(section.section_id)
        task_id = impl_id
        title = f"[{title_prefix}] {entry.title}"

    return {
        "id": task_id,
        "task_id": task_id,
        "phase_id": phase_id,
        "title": title,
        "description": description,
        "priority": 2,
        "task_type": entry.task_type,
        "category": entry.category,
        "validation": validation,
        "affected_files": affected_files,
        "labels": list(entry.labels),
        "assigned_agent": entry.assigned_agent,
        "additional_skills": [],
        "source_section_id": section.section_id,
    }


def _contract_validation_criteria(entry: ManifestEntry, section: PlanSection) -> str:
    lines = [entry.validation_criteria.strip()]
    artifact_lines = [
        f"- {item.item_id}: {item.artifact_kind.value}: `{item.artifact_ref}`"
        for item in section.acceptance_items
    ]
    if artifact_lines:
        lines.append("Acceptance artifacts:")
        lines.extend(artifact_lines)
    return "\n".join(line for line in lines if line)


def _contract_phase_index(self: Any, plan_doc: PlanDocument) -> dict[str, PlanSection]:
    section_by_id = {section.section_id: section for section in plan_doc.sections}
    phase_by_section_id: dict[str, PlanSection] = {}
    for section in plan_doc.sections:
        current = section
        while current.parent_id is not None:
            parent = section_by_id.get(current.parent_id)
            if parent is None:
                break
            if _CONTRACT_PHASE_ID_RE.match(parent.section_id):
                phase_by_section_id[section.section_id] = parent
                break
            current = parent
    return phase_by_section_id


def _parse_contract_plan(self: Any, run: ExpansionRun, task: Task) -> PlanDocument | None:
    if not run.plan_file:
        return None
    plan_path = Path(run.plan_file)
    repo_path = None
    if not plan_path.is_absolute():
        repo_path = self._resolve_repo_path(task)
        if repo_path is not None:
            plan_path = repo_path / plan_path
    else:
        repo_path = self._resolve_repo_path(task)
    registry_plan_id = _registry_plan_id_for_run(self, run, task, plan_path, repo_path)
    root_fallback = str(task.seq_num) if task.seq_num is not None else None
    candidate_plan_ids = _candidate_plan_ids(registry_plan_id, root_fallback)
    first_error: OSError | PlanParseError | None = None
    manifest_marker_present = _plan_has_manifest_marker(plan_path)

    for plan_id_override in candidate_plan_ids:
        try:
            draft_doc = parse_plan(
                plan_path,
                parse_mode="draft",
                plan_id_override=plan_id_override,
            )
        except (OSError, PlanParseError) as exc:
            first_error = first_error or exc
            continue

        if not draft_doc.manifest_entries:
            if manifest_marker_present:
                first_error = first_error or PlanParseError(
                    [(max(len(draft_doc.source_lines), 1), "missing manifest entries")],
                    plan_path,
                )
                continue
            return None

        try:
            return parse_plan(
                plan_path,
                parse_mode="expansion",
                plan_id_override=plan_id_override,
            )
        except (OSError, PlanParseError) as exc:
            first_error = first_error or exc
            continue

    if not manifest_marker_present:
        return None

    assert first_error is not None
    _log_contract_parse_failure(self, run, plan_path, first_error)
    raise ValueError(
        f"Plan file must conform to the Plan-Coverage Contract: {plan_path}"
    ) from first_error


def _candidate_plan_ids(*values: str | None) -> list[str | None]:
    candidates: list[str | None] = []
    for value in (*values, None):
        if value in candidates:
            continue
        candidates.append(value)
    return candidates


def _plan_has_manifest_marker(plan_path: Path) -> bool:
    try:
        return "kind: manifest" in plan_path.read_text(encoding="utf-8")
    except OSError:
        return True


def _registry_plan_id_for_run(
    self: Any,
    run: ExpansionRun,
    task: Task,
    plan_path: Path,
    repo_path: Path | None,
) -> str | None:
    manager = LocalPlanManager(self.db)
    root_ref = f"#{task.seq_num}" if task.seq_num is not None else None
    try:
        records = manager.list_plans(state="active", project_id=task.project_id)
    except Exception:
        return None
    relative_plan_path = _relative_plan_path(plan_path, repo_path)
    run_plan_path = Path(run.plan_file) if run.plan_file else None
    for record in records:
        record_path = Path(record.plan_path)
        if relative_plan_path is not None and record_path == relative_plan_path:
            return record.plan_id
        if run_plan_path is not None and record_path == run_plan_path:
            return record.plan_id
    if root_ref is not None:
        for record in records:
            if record.root_task_ref == root_ref:
                return record.plan_id
    return None


def _relative_plan_path(plan_path: Path, repo_path: Path | None) -> Path | None:
    if repo_path is None:
        return None
    try:
        return plan_path.relative_to(repo_path)
    except ValueError:
        return None


def _log_contract_parse_failure(
    self: Any,
    run: ExpansionRun,
    plan_path: Path,
    exc: OSError | PlanParseError,
    *,
    first_error: OSError | PlanParseError | None = None,
) -> None:
    extra: dict[str, Any] = {"plan_file": str(plan_path), "error": str(exc)}
    if first_error is not None:
        extra["first_error"] = str(first_error)
    self.run_manager.append_log(
        run.id,
        level="error",
        message="Plan file did not parse as Plan-Coverage Contract",
        extra=extra,
    )


def _dependency_path_exists(
    dependencies: list[dict[str, str]],
    *,
    start_task_id: str,
    target_task_id: str,
) -> bool:
    """Return whether start_task_id already reaches target_task_id through blockers."""
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in dependencies:
        task_id = edge.get("task_id")
        depends_on = edge.get("depends_on")
        if task_id and depends_on:
            adjacency[task_id].append(depends_on)

    seen: set[str] = set()
    stack = [start_task_id]
    while stack:
        task_id = stack.pop()
        if task_id == target_task_id:
            return True
        if task_id in seen:
            continue
        seen.add(task_id)
        stack.extend(adjacency.get(task_id, []))
    return False
