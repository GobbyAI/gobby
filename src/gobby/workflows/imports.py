"""Import filesystem workflow definitions into the runtime database."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from gobby.paths import get_global_workflows_dir
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.definitions.pipelines import PipelineDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.definitions.variables import SessionVariableDefaultManager
from gobby.storage.projects import LocalProjectManager
from gobby.workflows.agent_models import AgentDefinitionBody
from gobby.workflows.definitions import (
    PipelineDefinition,
    VariableDefinitionBody,
    normalize_workflow_definition_enabled,
    validate_workflow_definition_data,
)

logger = logging.getLogger(__name__)

_KIND_TABLES = ("agent", "rule", "variable", "pipeline")


def _lookup_existing_kind(db: Any, name: str, project_id: str | None) -> str | None:
    """Return the typed table that already owns this name, if any."""
    if AgentDefinitionManager(db).get_by_name(name, project_id=project_id) is not None:
        return "agent"
    if RuleDefinitionManager(db).get_by_name(name, project_id=project_id) is not None:
        return "rule"
    if SessionVariableDefaultManager(db).get_by_name(name, project_id=project_id) is not None:
        return "variable"
    if PipelineDefinitionManager(db).get_by_name(name, project_id=project_id) is not None:
        return "pipeline"
    return None


def _refuse_kind_change(name: str, existing: str | None, declared: str) -> None:
    if existing is not None and existing != declared:
        raise ValueError(
            f"Cannot change imported definition '{name}' from {existing!r} to {declared!r}"
        )


def _upsert_agent(db: Any, data: dict[str, Any], project_id: str | None) -> Any:
    AgentDefinitionBody.model_validate(data)
    manager = AgentDefinitionManager(db)
    name = str(data["name"])
    existing = manager.get_by_name(name, project_id=project_id)
    fields = {
        "definition_json": data,
        "description": data.get("description", ""),
        "enabled": normalize_workflow_definition_enabled(data),
    }
    if existing is not None:
        return manager.update(existing.id, **fields)
    return manager.create(name=name, project_id=project_id, source="installed", **fields)


def _upsert_rule(db: Any, data: dict[str, Any], project_id: str | None) -> Any:
    from gobby.workflows.definitions import RuleDefinitionBody

    body = {key: data[key] for key in RuleDefinitionBody.model_fields if key in data}
    RuleDefinitionBody.model_validate(body)
    manager = RuleDefinitionManager(db)
    name = str(data["name"])
    existing = manager.get_by_name(name, project_id=project_id)
    fields = {
        "definition_json": data,
        "description": data.get("description", ""),
        "enabled": normalize_workflow_definition_enabled(data),
        "priority": data.get("priority", 100),
        "sources": data.get("sources"),
    }
    if existing is not None:
        return manager.update(existing.id, **fields)
    return manager.create(name=name, project_id=project_id, source="installed", **fields)


def _upsert_variable(db: Any, data: dict[str, Any], project_id: str | None) -> Any:
    name = str(data.get("variable") or data["name"])
    value = data.get("value")
    description = data.get("description")
    VariableDefinitionBody(variable=name, value=value, description=description)
    manager = SessionVariableDefaultManager(db)
    existing = manager.get_by_name(name, project_id=project_id)
    fields = {
        "default_value": value,
        "description": description,
        "enabled": bool(data.get("enabled", True)),
    }
    if existing is not None:
        return manager.update(existing.id, **fields)
    return manager.create(
        name=name,
        default_value=value,
        project_id=project_id,
        description=description,
        enabled=bool(data.get("enabled", True)),
        source="installed",
    )


def _upsert_pipeline(db: Any, data: dict[str, Any], project_id: str | None) -> Any:
    PipelineDefinition.model_validate(data)
    manager = PipelineDefinitionManager(db)
    name = str(data["name"])
    existing = manager.get_by_name(name, project_id=project_id)
    fields = {
        "definition_json": data,
        "description": data.get("description", ""),
        "version": str(data.get("version", "1.0")),
        "enabled": normalize_workflow_definition_enabled(data),
    }
    if existing is not None:
        return manager.update(existing.id, **fields)
    return manager.create(name=name, project_id=project_id, source="installed", **fields)


def sync_imported_definition(
    db: Any,
    data: dict[str, Any],
    project_id: str | None,
) -> Any:
    """Validate and upsert an imported definition into the matching typed table."""
    declared_type = data.get("type")
    if declared_type in {"step", "lifecycle", "workflow"}:
        raise ValueError(
            "Standalone step/lifecycle definitions are no longer imported; "
            "use an agent step_workflow or a typed domain kind"
        )
    if declared_type not in _KIND_TABLES:
        declared_type = validate_workflow_definition_data(data)

    if declared_type not in _KIND_TABLES:
        raise ValueError(
            f"Imported definition '{data.get('name', '<unknown>')}' has unsupported "
            f"type {declared_type!r}; expected one of {sorted(_KIND_TABLES)}"
        )

    name = str(data.get("variable") if declared_type == "variable" else data["name"])
    _refuse_kind_change(name, _lookup_existing_kind(db, name, project_id), declared_type)

    if declared_type == "agent":
        return _upsert_agent(db, data, project_id)
    if declared_type == "rule":
        return _upsert_rule(db, data, project_id)
    if declared_type == "variable":
        return _upsert_variable(db, data, project_id)
    return _upsert_pipeline(db, data, project_id)


def sync_imported_workflow_file(
    db: Any,
    path: Path,
    project_id: str | None,
) -> Any:
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
