"""Validation helpers for plan files and compiled expansion specs."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from gobby.plans.parser import Kind, PlanParseError, parse_plan
from gobby.plans.semantic_lint import lint_plan_document
from gobby.tasks.expansion._common import (
    _CONTRACT_PHASE_ID_RE,
    AUTOMATED_LEAF_CATEGORIES,
    _clean_contract_section_title,
    _contract_phase_number,
)


def validate_plan_file(self: Any, plan_path: Path) -> dict[str, Any]:
    """Validate a plan file against the Plan-Coverage Contract."""
    if not plan_path.exists():
        return {"valid": False, "errors": [f"Plan file not found: {plan_path}"]}
    try:
        plan_doc = parse_plan(plan_path, parse_mode="draft")
    except (OSError, PlanParseError) as exc:
        return {"valid": False, "errors": [f"Plan file is not contract-conforming: {exc}"]}
    deliverables = [section for section in plan_doc.sections if section.kind is Kind.deliverable]
    if not deliverables:
        return {
            "valid": False,
            "errors": [f"Plan file has no kind: deliverable sections: {plan_path}"],
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
                "src/gobby/install/shared/skills/plan-draft/SKILL.md "
                "§ 'Phase Heading Syntax'."
            ],
        }
    semantic_lint = lint_plan_document(plan_doc)
    if not semantic_lint.valid:
        return {
            "valid": False,
            "errors": semantic_lint.errors,
            "semantic_lint": semantic_lint.to_dict(),
        }
    return {
        "valid": True,
        "path": str(plan_path),
        "phase_count": len(phases),
        "phases": phases,
        "deliverable_count": len(deliverables),
        "contract_plan": True,
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
        if task_item.get("phase_id") not in valid_phase_ids:
            errors.append(
                f"Task {task_item.get('id')} references unknown phase {task_item.get('phase_id')}"
            )
        if not task_item.get("title"):
            errors.append(f"Task {task_item.get('id')} is missing a title")
        category = str(task_item.get("category", "code"))
        if category not in AUTOMATED_LEAF_CATEGORIES:
            errors.append(f"Task {task_item.get('id')} has unsupported category:{category}")

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
