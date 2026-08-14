"""Import filesystem workflow definitions into the runtime database."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import yaml

from gobby.paths import get_global_workflows_dir
from gobby.storage.projects import LocalProjectManager
from gobby.storage.workflow_definitions import (
    LocalWorkflowDefinitionManager,
    WorkflowDefinitionRow,
)
from gobby.workflows.definitions import (
    WorkflowDefinition,
    normalize_workflow_definition_enabled,
    validate_workflow_definition_data,
)

logger = logging.getLogger(__name__)

_WORKFLOW_KINDS = frozenset({"step", "lifecycle"})


def sync_imported_definition(
    db: Any,
    data: dict[str, Any],
    project_id: str | None,
) -> WorkflowDefinitionRow:
    """Validate and upsert an imported definition into the runtime database."""
    declared_type = data.get("type")
    if declared_type == "agent":
        raise ValueError("Agent definitions use the agent import path, not generic workflow import")
    if declared_type == "rule":
        raise ValueError("Rule definitions use the rule tools, not generic workflow import")
    if declared_type == "variable":
        raise ValueError(
            "Variable definitions use the variable domain MCP tools, not generic workflow import"
        )
    if declared_type in _WORKFLOW_KINDS:
        WorkflowDefinition.model_validate(data)
        workflow_type = "workflow"
    else:
        workflow_type = validate_workflow_definition_data(data)

    manager = LocalWorkflowDefinitionManager(db)
    name = str(data["name"])
    existing = manager.get_by_name(name, project_id=project_id)
    if existing is not None and existing.project_id != project_id:
        existing = None
    if existing is not None and existing.workflow_type != workflow_type:
        raise ValueError(
            f"Cannot change imported definition '{name}' from "
            f"{existing.workflow_type!r} to {workflow_type!r}"
        )

    fields: dict[str, Any] = {
        "definition_json": json.dumps(data),
        "description": data.get("description", ""),
        "version": str(data.get("version", "1.0")),
        "enabled": normalize_workflow_definition_enabled(data),
        "priority": data.get("priority", 100),
        "sources": data.get("sources"),
        "source": "installed",
    }
    if existing is not None:
        return manager.update(existing.id, **fields)
    return manager.create(
        name=name,
        workflow_type=workflow_type,
        project_id=project_id,
        **fields,
    )


def sync_imported_workflow_file(
    db: Any,
    path: Path,
    project_id: str | None,
) -> WorkflowDefinitionRow:
    """Load one workflow YAML file and upsert it into the runtime database."""
    with path.open(encoding="utf-8") as file:
        data = yaml.safe_load(file)
    if not isinstance(data, dict) or "name" not in data:
        raise ValueError("Invalid workflow: missing 'name' field")
    return sync_imported_definition(db, data, project_id)


def sync_imported_workflows(
    db: Any,
    *,
    project_path: str | Path | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Sync global and project workflow YAML files into the runtime database."""
    roots: list[tuple[Path, str | None]] = [(get_global_workflows_dir(), None)]
    if project_path is not None:
        if project_id is None:
            raise ValueError("project_id is required with project_path")
        roots.append((Path(project_path) / ".gobby" / "workflows", project_id))
    else:
        for project in LocalProjectManager(db).list():
            if project.repo_path:
                roots.append((Path(project.repo_path) / ".gobby" / "workflows", project.id))

    synced = 0
    errors: list[str] = []
    for root, scope_id in roots:
        if not root.is_dir():
            continue
        for path in sorted((*root.glob("*.yaml"), *root.glob("*.yml"))):
            try:
                sync_imported_workflow_file(db, path, scope_id)
                synced += 1
            except Exception as exc:
                message = f"Failed to sync imported workflow {path}: {exc}"
                logger.warning(message)
                errors.append(message)

    return {"synced": synced, "errors": errors}
