"""Plan-Coverage Contract compilation: turns a parsed plan document into a compiled expansion spec."""

from __future__ import annotations

import logging
import sqlite3
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
from gobby.tasks.categories import AGENT_BY_IMPLEMENTATION_DOMAIN
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
    _dedupe_dependencies,
)

logger = logging.getLogger(__name__)


def compile_plan_to_spec(self: Any, plan_doc: PlanDocument, task: Task) -> dict[str, Any]:
    """Compile a Plan-Coverage Contract document into a compiled expansion spec.

    Each manifest entry emits exactly one implementation-forward leaf. TDD is
    represented as task metadata and validation evidence requirements.
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

    # entry.source_section -> id of the per-entry implementation task.
    # Used to wire cross-deliverable depends_on edges below.
    entry_work_task_id: dict[str, str] = {}

    for phase_id in phase_id_order:
        phase_section = phase_section_by_phase_id[phase_id]
        phase = self._ensure_contract_phase(
            phases=phases,
            phase_by_id=phase_by_id,
            phase_section=phase_section,
            task=task,
        )
        phase_entries = entries_by_phase_id[phase_id]

        for entry in phase_entries:
            section = section_by_id[entry.source_section]
            single_task = self._build_contract_entry_work_task(
                plan_doc=plan_doc,
                entry=entry,
                section=section,
                phase_id=phase_id,
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
        "tdd_mode": "skill_backed",
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
            "summary": f"Validate Phase {phase_number} leaves with focused evidence.",
            "behaviors": [],
            "suggested_test_files": [],
            "entry_criteria": [
                "TDD-required leaves provide red, green, refactor/final-green, "
                "exact command, and test-quality evidence."
            ],
        },
        "task_ids": [],
    }
    phases.append(phase)
    phase_by_id[phase_id] = phase
    return phase


def _build_contract_entry_work_task(
    self: Any,
    *,
    plan_doc: PlanDocument,
    entry: ManifestEntry,
    section: PlanSection,
    phase_id: str,
) -> dict[str, Any]:
    """Build the per-entry work task."""
    body = _contract_section_body(plan_doc, section)
    affected_files = _contract_affected_files(section)
    acceptance_lines = _contract_acceptance_lines(section)
    description = f"Plan section `{section.section_id}`.\n\n{body}".strip()
    if acceptance_lines:
        description = f"{description}\n\nAcceptance items:\n" + "\n".join(acceptance_lines)
    validation = _contract_validation_criteria(entry, section)

    labels = list(entry.labels)
    additional_skills: list[str] = []
    if entry.tdd:
        labels.append("tdd:required")
        additional_skills.append("test-driven-development")

    return {
        "id": _contract_single_task_id(section.section_id),
        "task_id": _contract_single_task_id(section.section_id),
        "phase_id": phase_id,
        "title": entry.title,
        "description": description,
        "priority": 2,
        "task_type": entry.task_type,
        "category": entry.category,
        "validation": validation,
        "affected_files": affected_files,
        "labels": labels,
        "assigned_agent": _assigned_agent_for_entry(entry),
        "implementation_domain": entry.implementation_domain,
        "additional_skills": additional_skills,
        "tdd_required": entry.tdd,
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
    if entry.tdd:
        lines.extend(
            [
                "TDD evidence required:",
                "- Red evidence: failing test output captured before implementation.",
                "- Green evidence: minimal implementation made the new test pass.",
                "- Refactor/final-green evidence: final validation after cleanup.",
                "- Exact test command used for red and green/final checks.",
                "- Test-quality audit output for touched test paths, or a documented reason it was not applicable.",
            ]
        )
    return "\n".join(line for line in lines if line)


def _assigned_agent_for_entry(entry: ManifestEntry) -> str | None:
    if entry.assigned_agent:
        return entry.assigned_agent
    if entry.implementation_domain:
        agent = AGENT_BY_IMPLEMENTATION_DOMAIN.get(entry.implementation_domain)
        if agent is None:
            raise ValueError(
                f"Unsupported implementation_domain {entry.implementation_domain!r} "
                f"for manifest entry source_section={entry.source_section!r}"
            )
        return agent
    return None


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
    except (sqlite3.Error, LookupError, KeyError, TypeError, ValueError) as exc:
        logger.debug(
            "Could not resolve plan registry id for expansion run",
            extra={
                "task_id": task.id,
                "project_id": task.project_id,
                "plan_file": str(plan_path),
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
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
