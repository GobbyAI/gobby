"""Shared helpers for task expansion compile/apply internals."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from jinja2 import Environment, StrictUndefined

from gobby.plans.parser import (
    PlanDocument,
    PlanSection,
    extract_section_dependencies,
    resolve_plan_id,
    strip_section_dependencies,
)
from gobby.prompts.models import parse_frontmatter
from gobby.storage.tasks import Task
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.tasks.categories import DEVELOPMENT_FORWARD_LEAF_CATEGORIES

_DEFAULT_AGENT = "backend-developer"
_DEFAULT_PHASE_ID = "phase-1"
_EXPANSION_STAGES = frozenset({"planning", "expansion", "development", "holistic_qa", "pr"})
_FRONTEND_SIGNALS = frozenset(
    {
        "accessibility",
        "browser",
        "client",
        "component",
        "css",
        "eslint",
        "frontend",
        "jsx",
        "lighthouse",
        "playwright",
        "react",
        "routing",
        "storybook",
        "svelte",
        "tailwind",
        "tsx",
        "typescript",
        "ui",
        "vite",
        "vue",
        "web",
        "webpack",
    }
)
_DETERMINISTIC_FRONTEND_SIGNAL_RE = re.compile(
    r"\b(?:accessibility|browser|client|component|css|eslint|frontend|lighthouse|"
    r"jsx|next\.?js|playwright|react|routing|storybook|svelte|tailwind|tsx|"
    r"typescript|ui|vite|vue|web|webpack)\b|(?:^|[\\/])web[\\/]|app\.tsx",
    flags=re.IGNORECASE,
)
_DETERMINISTIC_AGENT_BY_CATEGORY = {
    "code": "backend-developer",
    "refactor": "backend-developer",
    "test": "backend-developer",
    "config": "backend-developer",
    "docs": "tech-writer",
}
_BACKEND_SIGNALS = frozenset(
    {
        "api",
        "backend",
        "cli",
        "daemon",
        "database",
        "mcp",
        "migration",
        "mypy",
        "pytest",
        "ruff",
        "scheduler",
        "server",
        "storage",
        "workflow",
        "worker",
    }
)


def _stage_name(stage: Any) -> str | None:
    if isinstance(stage, dict):
        value = stage.get("stage_name", stage.get("name"))
    else:
        value = getattr(stage, "stage_name", getattr(stage, "name", None))
    return value if isinstance(value, str) and value else None


def _manifest_stage_names(task: Task) -> tuple[str, ...]:
    """Return stage names from the persisted manifest attached to a task."""
    return tuple(
        stage_name for stage in task.stages if (stage_name := _stage_name(stage)) is not None
    )


def _dev_is_only_enabled_stage(task: Task) -> bool:
    enabled = set(_manifest_stage_names(task))
    return bool(enabled) and enabled & _EXPANSION_STAGES == {"development"}


def _append_agent_selection_marker(description: str) -> str:
    """Record deterministic fallback agent selection in the leaf description."""
    marker = (
        "## Agent Selection\n"
        "Defaulted to `backend-developer` because no registry agent selection was provided."
    )
    if "## Agent Selection" in description:
        return description
    return f"{description.rstrip()}\n\n{marker}".strip()


def _additional_skills(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(skill) for skill in value]
    return [str(value)]


def _leaf_signal_text(task_item: dict[str, Any]) -> str:
    values = [
        task_item.get("title"),
        task_item.get("description"),
        task_item.get("category"),
        " ".join(str(label) for label in task_item.get("labels") or []),
        " ".join(str(file_path) for file_path in task_item.get("affected_files") or []),
    ]
    return " ".join(str(value).lower() for value in values if value)


def _available_agent_names(agent_definitions: list[dict[str, Any]]) -> set[str]:
    return {
        str(agent["name"])
        for agent in agent_definitions
        if agent.get("enabled", True) and agent.get("name")
    }


def list_agent_definitions(
    def_manager: LocalWorkflowDefinitionManager,
    enabled: bool | None = None,
    project_id: str | None = None,
    surface_filter: str | None = None,
) -> dict[str, Any]:
    """List agent definitions for expansion without importing the MCP tool layer."""
    rows = def_manager.list_all(workflow_type="agent", enabled=enabled, project_id=project_id)
    agents: list[dict[str, Any]] = []
    for row in rows:
        try:
            body = json.loads(row.definition_json)
        except json.JSONDecodeError:
            continue
        surfaces = body.get("surfaces", ["spawn"])
        agent = {
            "id": row.id,
            "name": row.name,
            "description": row.description or body.get("description"),
            "surfaces": surfaces,
            "enabled": row.enabled,
            "source": row.source,
            "project_id": row.project_id,
        }
        if surface_filter and surface_filter not in surfaces:
            continue
        agents.append(agent)
    return {"success": True, "agents": agents, "count": len(agents)}


def _select_agent_from_registry(
    task_item: dict[str, Any],
    agent_definitions: list[dict[str, Any]],
) -> str | None:
    available = _available_agent_names(agent_definitions)
    if not available:
        return None

    signal_text = _leaf_signal_text(task_item)
    category = str(task_item.get("category", "code"))
    if category == "docs" and "tech-writer" in available:
        return "tech-writer"
    frontend_score = sum(1 for signal in _FRONTEND_SIGNALS if signal in signal_text)
    backend_score = sum(1 for signal in _BACKEND_SIGNALS if signal in signal_text)

    if "frontend-developer" in available and frontend_score > backend_score:
        return "frontend-developer"
    if "backend-developer" in available and backend_score > 0:
        return "backend-developer"
    return None


def _agent_selection_fields(
    task_item: dict[str, Any],
    agent_definitions: list[dict[str, Any]],
) -> tuple[str | None, list[str] | None, str]:
    """Normalize expansion agent-selection fields for an emitted leaf task."""
    category = str(task_item.get("category", "code"))
    description = str(task_item.get("description") or "")
    if category not in DEVELOPMENT_FORWARD_LEAF_CATEGORIES:
        return None, None, description

    available = _available_agent_names(agent_definitions)
    assigned_agent = task_item.get("assigned_agent")
    if assigned_agent and str(assigned_agent) in available:
        return (
            str(assigned_agent),
            _additional_skills(task_item.get("additional_skills")),
            description,
        )

    selected_agent = _select_agent_from_registry(task_item, agent_definitions)
    if selected_agent and selected_agent in available:
        return selected_agent, _additional_skills(task_item.get("additional_skills")), description

    return _DEFAULT_AGENT, [], _append_agent_selection_marker(description)


_CONTRACT_PHASE_ID_RE = re.compile(r"^P(?P<number>\d+)$")
_CATEGORY_RE = re.compile(r"\[category:\s*(?P<category>[a-z_]+)\]", flags=re.IGNORECASE)


def _clean_contract_section_title(title: str) -> str:
    """Remove plan-contract metadata annotations from a section title."""
    title = _CATEGORY_RE.sub("", title)
    title = strip_section_dependencies(title)
    return re.sub(r"\s+", " ", title).strip()


def _contract_section_category(section: PlanSection) -> str:
    match = _CATEGORY_RE.search(section.title)
    if match is None:
        return "code"
    return match.group("category").lower()


def _contract_section_depends(section: PlanSection) -> list[str]:
    return list(extract_section_dependencies(section.title))


def _contract_phase_number(phase_id: str) -> int:
    match = _CONTRACT_PHASE_ID_RE.match(phase_id)
    if match is None:
        return 1
    return int(match.group("number"))


def _contract_phase_spec_id(phase_id: str) -> str:
    if phase_id == _DEFAULT_PHASE_ID:
        return _DEFAULT_PHASE_ID
    return f"phase-{phase_id.lower()}"


def _contract_plan_id(plan_doc: PlanDocument) -> str:
    return resolve_plan_id(plan_doc.plan_id)


def _contract_covers_labels(plan_id: str, section: PlanSection) -> list[str]:
    return [
        f"covers:{plan_id}:{section.section_id}:{item.item_id}" for item in section.acceptance_items
    ]


def _contract_acceptance_lines(section: PlanSection) -> list[str]:
    return [f"- {item.item_id}: {item.prose}" for item in section.acceptance_items]


def _contract_artifact_summary(section: PlanSection) -> str:
    refs = [f"{item.artifact_kind.value}: {item.artifact_ref}" for item in section.acceptance_items]
    return "; ".join(refs) if refs else "the documented acceptance artifacts"


def _contract_affected_files(section: PlanSection) -> list[str]:
    file_refs = [
        item.artifact_ref
        for item in section.acceptance_items
        if item.artifact_kind.value in {"file", "test"}
    ]
    return sorted(dict.fromkeys(file_refs))


def _contract_section_body(plan_doc: PlanDocument, section: PlanSection) -> str:
    lines = plan_doc.source_path.read_text(encoding="utf-8").splitlines()
    start_line, end_line = section.source_span
    raw_lines = lines[start_line:end_line]
    body_lines: list[str] = []
    for line in raw_lines:
        stripped = line.strip()
        if stripped.startswith("`kind:") and stripped.endswith("`"):
            continue
        if stripped == "**Acceptance:**":
            break
        body_lines.append(line)
    return "\n".join(body_lines).strip()


def _contract_agent_fields(
    *,
    category: str,
    title: str,
    description: str,
) -> tuple[str, list[str], str]:
    signal_text = f"{title}\n{description}".lower()
    if _DETERMINISTIC_FRONTEND_SIGNAL_RE.search(signal_text):
        return "frontend-developer", [], description
    assigned_agent = _DETERMINISTIC_AGENT_BY_CATEGORY.get(category)
    if assigned_agent is not None:
        return assigned_agent, [], description
    raise ValueError(
        f"contract category {category!r} has no specialist agent and is not eligible for "
        f"automated leaf creation; valid categories: {sorted(_DETERMINISTIC_AGENT_BY_CATEGORY)}"
    )


def _contract_single_task_id(section_id: str) -> str:
    return f"{section_id}::single"


def _contract_deferral_record(section: PlanSection) -> dict[str, Any] | None:
    if section.deferral is None:
        return None
    return {
        "section_id": section.section_id,
        "task_ref": section.deferral.task_ref,
        "reason": section.deferral.reason,
        "owner": section.deferral.owner,
        "original_acceptance_items": [
            {
                "item_id": item.item_id,
                "artifact_kind": item.artifact_kind.value,
                "artifact_ref": item.artifact_ref,
            }
            for item in section.deferral.original_acceptance_items
        ],
    }


def _strip_frontmatter(markdown: str) -> str:
    """Strip YAML frontmatter from a bundled prompt file."""
    _frontmatter, body = parse_frontmatter(markdown)
    return body


def _read_text_if_exists(path: Path, *, max_chars: int | None = None) -> str | None:
    """Read UTF-8 text from a file when it exists."""
    if not path.exists() or not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if max_chars is not None:
        return text[:max_chars]
    return text


def _find_test_files(files: Iterable[str]) -> list[str]:
    """Extract likely test file paths from a file list."""
    test_files: list[str] = []
    for file_path in files:
        lowered = file_path.lower()
        if "/tests/" in lowered or lowered.startswith("tests/") or "test_" in lowered:
            test_files.append(file_path)
    return sorted(set(test_files))


def _render_template(template_str: str, context: dict[str, Any]) -> str:
    """Render a Jinja template string."""
    env = Environment(autoescape=False, undefined=StrictUndefined)
    env.filters["default"] = lambda value, default="": default if value is None else value
    return str(env.from_string(template_str).render(**context))


def _dedupe_dependencies(dependencies: list[dict[str, str]]) -> list[dict[str, str]]:
    """Deduplicate dependency edges while preserving order."""
    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for edge in dependencies:
        key = (edge["task_id"], edge["depends_on"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append({"task_id": edge["task_id"], "depends_on": edge["depends_on"]})
    return deduped
