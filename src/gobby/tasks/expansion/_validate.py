"""Validation helpers for plan files and compiled expansion specs."""

from __future__ import annotations

import re
import subprocess
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

import psycopg

from gobby.plans.parser import Kind, ParseMode, PlanDocument, PlanParseError, parse_plan
from gobby.plans.semantic_lint import (
    collect_description_target_inventory,
    lint_plan_document,
    normalize_target_path,
)
from gobby.plans.symbol_targets import (
    CONSUMER_COVERAGE,
    skipped_symbol_validation,
    validate_symbol_targets,
)
from gobby.storage.tasks import Task
from gobby.tasks.categories import DEVELOPMENT_FORWARD_LEAF_CATEGORIES, IMPLEMENTATION_DOMAINS
from gobby.tasks.expansion._common import (
    _CONTRACT_PHASE_ID_RE,
    _clean_contract_section_title,
    _contract_phase_number,
    validate_contract_manifest,
    validate_contract_test_artifacts,
)
from gobby.tasks.task_types import VALID_TASK_TYPES
from gobby.utils import spawn


class CompletedSectionExemptionsUnavailable(ValueError):
    """Task completion could not be resolved; dependent lint cannot run safely."""


class CompletionTaskLookup(Protocol):
    """Task reads that resolve completed-section owners by coverage label."""

    def list_tasks(
        self, *, project_id: str, label: str, limit: int, offset: int, sort_by: str
    ) -> list[Task]:
        """Return up to ``limit`` project tasks carrying ``label``."""
        ...


def _task_has_landed_commit(task: Task, project_root: Path) -> bool:
    """Require Git evidence in both the shared checkout and the validated checkout."""
    try:
        common_dir = spawn.run(
            [
                "git",
                "-C",
                str(project_root),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        if not Path(common_dir).is_dir():
            return False
        commits = list(task.commits or [])
        if task.seq_num is not None:
            history = spawn.run(
                [
                    "git",
                    "--git-dir",
                    common_dir,
                    "log",
                    "HEAD",
                    "--format=%H%x00%s",
                    "--fixed-strings",
                    f"--grep=-#{task.seq_num}]",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout
            marker = re.compile(rf"^\[[^\]]+-#{task.seq_num}\]\s")
            for line in history.splitlines():
                sha, _, subject = line.partition("\0")
                if marker.match(subject):
                    commits.append(sha)
        for sha in commits:
            if all(
                spawn.run(
                    ["git", *scope, "merge-base", "--is-ancestor", sha, "HEAD"],
                    capture_output=True,
                    timeout=5,
                ).returncode
                == 0
                for scope in (["--git-dir", common_dir], ["-C", str(project_root)])
            ):
                return True
    except (OSError, subprocess.SubprocessError):
        pass
    return False


def _completed_plan_sections(
    plan_doc: PlanDocument,
    task_manager: CompletionTaskLookup | None,
    project_context: Mapping[str, Any] | None,
    project_root: Path | None,
) -> tuple[frozenset[str], frozenset[str]]:
    """Resolve symbol exemptions and fully executed sections from coverage owners."""
    project_id = project_context.get("id") if project_context is not None else None
    if task_manager is None:
        raise CompletedSectionExemptionsUnavailable("no task manager")
    if not isinstance(project_id, str) or not project_id or not plan_doc.plan_id:
        raise CompletedSectionExemptionsUnavailable("no project or plan identity")
    completed: set[str] = set()
    executed: set[str] = set()
    for section in plan_doc.sections:
        if section.kind is not Kind.deliverable or not section.acceptance_items:
            continue
        labels = [
            f"covers:{plan_doc.plan_id}:{section.section_id}:{item.item_id}"
            for item in section.acceptance_items
        ]
        try:
            owners: list[list[Task]] = []
            for label in labels:
                tasks: list[Task] = []
                while True:
                    page = task_manager.list_tasks(
                        project_id=project_id,
                        label=label,
                        limit=50,
                        offset=len(tasks),
                        sort_by="created_at",
                    )
                    tasks.extend(page)
                    if len(page) < 50:
                        break
                owners.append(tasks)
        except psycopg.Error as exc:
            raise CompletedSectionExemptionsUnavailable(
                f"task lookup failed for section {section.section_id}: {exc}"
            ) from exc
        if all(owners) and all(
            all(
                task.project_id == project_id
                and label in (task.labels or [])
                and task.task_type != "epic"
                and task.closed_at is not None
                for task in tasks
            )
            and any(task.closed_reason in {"completed", "already_implemented"} for task in tasks)
            for label, tasks in zip(labels, owners, strict=True)
        ):
            executed.add(section.section_id)
        if any(len(tasks) != 1 for tasks in owners):
            continue
        task = owners[0][0]
        if any(tasks[0].id != task.id for tasks in owners):
            continue
        if task.project_id != project_id or not set(labels).issubset(task.labels or []):
            continue
        if task.closed_reason in {"duplicate", "wont_fix", "obsolete", "out_of_repo"}:
            continue
        delivered = task.closed_at and task.closed_reason in {"completed", "already_implemented"}
        if delivered or (project_root and _task_has_landed_commit(task, project_root)):
            completed.add(section.section_id)
    return frozenset(completed), frozenset(executed)


def validate_plan_file(
    self: Any,
    plan_path: Path,
    *,
    project_context: Mapping[str, Any] | None = None,
    expected_project_id: str | None = None,
    code_index: Any | None = None,
    task_manager: CompletionTaskLookup | None = None,
    require_symbol_validation: bool = False,
    consumer_coverage_blocking: bool = False,
    plan_document: PlanDocument | None = None,
    parse_mode: ParseMode = "draft",
) -> dict[str, Any]:
    """Validate a plan file against the Plan-Coverage Contract.

    Semantic and symbol checks require project-scoped completion lookup, even
    offline: guessing that no sections are complete can produce false findings.
    Missing lookup returns ``completed_section_exemptions_unavailable`` after
    structural validation, before any completion-dependent checks.
    """
    project_path = project_context.get("project_path") if project_context is not None else None
    project_root = Path(project_path) if isinstance(project_path, str) and project_path else None
    skipped_symbols = skipped_symbol_validation().to_dict()
    if plan_document is None and not plan_path.exists():
        return {
            "valid": False,
            "errors": [f"Plan file not found: {plan_path}"],
            "warnings": [],
            "symbol_validation": skipped_symbols,
        }
    try:
        plan_doc = plan_document or parse_plan(plan_path, parse_mode=parse_mode)
    except (OSError, PlanParseError) as exc:
        return {
            "valid": False,
            "errors": [f"Plan file is not contract-conforming: {exc}"],
            "warnings": [],
            "symbol_validation": skipped_symbols,
        }
    warnings = list(plan_doc.warnings)
    if warnings:
        return {
            "valid": False,
            "errors": warnings,
            "warnings": warnings,
            "symbol_validation": skipped_symbols,
        }
    deliverables = [section for section in plan_doc.sections if section.kind is Kind.deliverable]
    if not deliverables:
        return {
            "valid": False,
            "errors": [f"Plan file has no kind: deliverable sections: {plan_path}"],
            "warnings": warnings,
            "symbol_validation": skipped_symbols,
        }
    phases = {
        _contract_phase_number(section.section_id): _clean_contract_section_title(section.title)
        for section in plan_doc.sections
        if _CONTRACT_PHASE_ID_RE.match(section.section_id)
    }
    if not phases:
        return {
            "valid": False,
            "errors": [
                "Plan has deliverable sections but no phase sections. Phases must "
                "use canonical IDs matching ^P\\d+$ (e.g. `## P1: Setup`). "
                "Headings like `## Phase 1: Setup` or `## 1: Setup` are silently "
                "dropped by the parser and cannot anchor expansion. See "
                "gobby:references/plan/coverage.md."
            ],
            "warnings": warnings,
            "symbol_validation": skipped_symbols,
        }
    try:
        # The compiler may synthesize a missing manifest after a run is created.
        # Check acceptance artifacts before that point, regardless of manifest state.
        if plan_doc.manifest_entries:
            validate_contract_manifest(plan_doc)
        else:
            validate_contract_test_artifacts(plan_doc)
    except ValueError as exc:
        return {
            "valid": False,
            "errors": [str(exc)],
            "warnings": warnings,
            "symbol_validation": skipped_symbols,
        }
    try:
        completed_section_ids, executed_section_ids = _completed_plan_sections(
            plan_doc,
            task_manager if task_manager is not None else getattr(self, "task_manager", None),
            project_context,
            project_root,
        )
    except CompletedSectionExemptionsUnavailable as exc:
        return {
            "valid": False,
            "condition": "completed_section_exemptions_unavailable",
            "errors": [f"completed-section exemptions unavailable: {exc}"],
            "warnings": warnings,
            "symbol_validation": skipped_symbols,
        }
    semantic_lint = lint_plan_document(
        plan_doc, project_root=project_root, completed_section_ids=executed_section_ids
    )
    warnings.extend(semantic_lint.warnings)
    if not semantic_lint.valid:
        return {
            "valid": False,
            "errors": semantic_lint.errors,
            "warnings": warnings,
            "semantic_lint": semantic_lint.to_dict(),
            "symbol_validation": skipped_symbols,
        }
    symbol_validation = validate_symbol_targets(
        plan_doc,
        project_context=project_context,
        expected_project_id=expected_project_id,
        code_index=code_index,
        required=require_symbol_validation,
        consumer_coverage_blocking=consumer_coverage_blocking or bool(plan_doc.manifest_entries),
        completed_section_ids=completed_section_ids | executed_section_ids,
    )
    consumer_warnings = [
        issue.message
        for issue in symbol_validation.issues
        if issue.code == CONSUMER_COVERAGE and not issue.blocking
    ]
    validation_warnings = [*warnings, *consumer_warnings]
    if symbol_validation.errors:
        return {
            "valid": False,
            "errors": symbol_validation.errors,
            "warnings": validation_warnings,
            "semantic_lint": semantic_lint.to_dict(),
            "symbol_validation": symbol_validation.to_dict(),
        }
    return {
        "valid": True,
        "path": str(plan_path),
        "phase_count": len(phases),
        "phases": phases,
        "deliverable_count": len(deliverables),
        "contract_plan": True,
        "warnings": validation_warnings,
        "symbol_validation": symbol_validation.to_dict(),
    }


def validate_compiled_spec(self: Any, compiled_spec: dict[str, Any]) -> dict[str, Any]:
    """Validate compiled-spec structure and dependency integrity."""
    errors: list[str] = []
    tasks = compiled_spec.get("tasks") or []
    phases = compiled_spec.get("phases") or []
    dependencies = compiled_spec.get("dependencies") or []

    if not tasks:
        errors.append("Compiled spec contains no tasks")
    if not phases:
        errors.append("Compiled spec contains no phases")

    task_ids = [task["id"] for task in tasks if task.get("id")]
    phase_ids = [phase["id"] for phase in phases if phase.get("id")]

    if len(task_ids) != len(set(task_ids)):
        errors.append("Task IDs must be unique")
    if len(phase_ids) != len(set(phase_ids)):
        errors.append("Phase IDs must be unique")

    valid_task_ids = set(task_ids)
    valid_phase_ids = set(phase_ids)
    for task_item in tasks:
        targets = collect_description_target_inventory(task_item.get("description"))
        scope = {
            path.rstrip("/")
            for entry in task_item.get("affected_files") or []
            if (path := normalize_target_path(entry))
        }
        missing_targets = sorted(
            target
            for target in targets
            if not any(
                target.rstrip("/") == entry or target.startswith(f"{entry}/") for entry in scope
            )
        )
        if missing_targets:
            errors.append(
                f"Task {task_item.get('id')} Targets missing from affected_files: "
                f"{', '.join(missing_targets)}. Add these paths to affected_files before applying."
            )
        if task_item.get("phase_id") not in valid_phase_ids:
            errors.append(
                f"Task {task_item.get('id')} references unknown phase {task_item.get('phase_id')}"
            )
        if not task_item.get("title"):
            errors.append(f"Task {task_item.get('id')} is missing a title")
        category = str(task_item.get("category", "code"))
        if category not in DEVELOPMENT_FORWARD_LEAF_CATEGORIES:
            errors.append(f"Task {task_item.get('id')} has unsupported category:{category}")
        if category in {"planning", "research"}:
            errors.append(
                f"Task {task_item.get('id')} cannot be a {category} expansion leaf; "
                "plan expansion output must be development-forward"
            )
        task_type = task_item.get("task_type", "task")
        if not isinstance(task_type, str) or task_type not in VALID_TASK_TYPES:
            errors.append(f"Task {task_item.get('id')} has unsupported task_type:{task_type}")
        implementation_domain = task_item.get("implementation_domain")
        if implementation_domain is not None and (
            not isinstance(implementation_domain, str)
            or implementation_domain not in IMPLEMENTATION_DOMAINS
        ):
            errors.append(
                f"Task {task_item.get('id')} has unsupported "
                f"implementation_domain:{implementation_domain}"
            )

    for phase in phases:
        phase_task_ids = phase.get("task_ids") or []
        if not phase_task_ids:
            errors.append(f"Phase {phase.get('id')} has no task_ids")
        for stable_id in phase_task_ids:
            if stable_id not in valid_task_ids:
                errors.append(f"Phase {phase.get('id')} references unknown task {stable_id}")

    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in dependencies:
        task_id = edge.get("task_id")
        depends_on = edge.get("depends_on")
        if task_id not in valid_task_ids:
            errors.append(f"Dependency references unknown task {task_id}")
            continue
        if depends_on not in valid_task_ids:
            errors.append(f"Dependency {task_id} -> {depends_on} references unknown blocker")
            continue
        if task_id == depends_on:
            errors.append(f"Task {task_id} cannot depend on itself")
            continue
        adjacency[task_id].append(depends_on)

    visiting: set[str] = set()
    visited: set[str] = set()

    def _detect_cycle(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        for blocker in adjacency.get(node, []):
            if _detect_cycle(blocker):
                return True
        visiting.remove(node)
        visited.add(node)
        return False

    for task_id in valid_task_ids:
        if _detect_cycle(task_id):
            errors.append("Compiled spec dependency graph contains a cycle")
            break

    return {
        "valid": not errors,
        "errors": errors,
        "task_count": len(tasks),
        "phase_count": len(phases),
        "plan_file": compiled_spec.get("plan_file"),
    }
