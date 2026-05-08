"""QA checks for compiled expansion specs."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ExpansionQaResult:
    """Result for one or more expansion QA checks."""

    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the result for run checkpoints or QA output."""
        return {
            "valid": self.valid,
            "passed": self.valid,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "details": dict(self.details),
        }


def check_shape(compiled_spec: Mapping[str, Any]) -> ExpansionQaResult:
    """Check that an expansion spec has the minimum harness shape."""
    errors: list[str] = []
    tasks = compiled_spec.get("tasks")
    phases = compiled_spec.get("phases")
    dependencies = compiled_spec.get("dependencies", [])

    if not isinstance(tasks, list) or not tasks:
        errors.append("Compiled spec must include a non-empty tasks list")
    elif any(not isinstance(task, Mapping) for task in tasks):
        errors.append("Compiled spec tasks must be objects")

    if not isinstance(phases, list) or not phases:
        errors.append("Compiled spec must include a non-empty phases list")
    elif any(not isinstance(phase, Mapping) for phase in phases):
        errors.append("Compiled spec phases must be objects")

    if not isinstance(dependencies, list):
        errors.append("Compiled spec dependencies must be a list when present")
    elif any(not isinstance(dependency, Mapping) for dependency in dependencies):
        errors.append("Compiled spec dependencies must be objects")

    return ExpansionQaResult(valid=not errors, errors=errors)


def check_manifest_coverage(
    *,
    manifest_entries: Iterable[Mapping[str, Any]],
    compiled_tasks: Iterable[Mapping[str, Any]],
) -> ExpansionQaResult:
    """Check every manifest entry has a compiled leaf task."""
    expected_sections = _non_empty_values(manifest_entries, "source_section")
    actual_sections = _non_empty_values(compiled_tasks, "source_section_id")

    missing = sorted(expected_sections - actual_sections, key=_section_sort_key)
    extra = sorted(actual_sections - expected_sections, key=_section_sort_key)

    errors = [f"Manifest section {section} has no compiled task" for section in missing]
    warnings = [f"Compiled task references non-manifest section {section}" for section in extra]
    return ExpansionQaResult(
        valid=not errors,
        errors=errors,
        warnings=warnings,
        details={
            "manifest_section_count": len(expected_sections),
            "compiled_section_count": len(actual_sections),
            "missing_sections": missing,
            "extra_sections": extra,
        },
    )


def check_routing(
    *,
    compiled_tasks: Iterable[Mapping[str, Any]],
    known_agents: set[str],
) -> ExpansionQaResult:
    """Check compiled tasks do not route to unknown agents."""
    errors: list[str] = []
    checked = 0

    for task in compiled_tasks:
        agent = _string_value(task.get("assigned_agent"))
        if agent is None:
            continue
        checked += 1
        if agent not in known_agents:
            task_id = _string_value(task.get("id")) or "<unknown>"
            errors.append(f"Task {task_id} routes to unknown agent {agent}")

    return ExpansionQaResult(
        valid=not errors,
        errors=errors,
        details={"checked_task_count": checked, "known_agents": sorted(known_agents)},
    )


def run_expansion_qa(
    *,
    compiled_spec: Mapping[str, Any],
    manifest_entries: Iterable[Mapping[str, Any]] = (),
    known_agents: set[str] | None = None,
) -> ExpansionQaResult:
    """Run all expansion QA checks and return one aggregate result."""
    tasks = _mapping_items(compiled_spec.get("tasks"))
    results = [check_shape(compiled_spec)]
    if manifest_entries:
        results.append(
            check_manifest_coverage(manifest_entries=manifest_entries, compiled_tasks=tasks)
        )
    if known_agents is not None:
        results.append(check_routing(compiled_tasks=tasks, known_agents=known_agents))
    return _merge_results(results)


def _merge_results(results: Iterable[ExpansionQaResult]) -> ExpansionQaResult:
    errors: list[str] = []
    warnings: list[str] = []
    details: dict[str, Any] = {}
    for index, result in enumerate(results):
        errors.extend(result.errors)
        warnings.extend(result.warnings)
        details[f"check_{index}"] = result.to_dict()
    return ExpansionQaResult(valid=not errors, errors=errors, warnings=warnings, details=details)


def _mapping_items(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _non_empty_values(
    items: Iterable[Mapping[str, Any]],
    field_name: str,
) -> set[str]:
    values: set[str] = set()
    for item in items:
        value = _string_value(item.get(field_name))
        if value is not None:
            values.add(value)
    return values


def _string_value(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _section_sort_key(section: str) -> tuple[int | str, ...]:
    parts: list[int | str] = []
    for part in section.split("."):
        parts.append(int(part) if part.isdigit() else part)
    return tuple(parts)
