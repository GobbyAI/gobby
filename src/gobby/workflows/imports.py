"""Import filesystem workflow definitions into the runtime database."""

from __future__ import annotations

import logging
from pathlib import Path
from stat import S_ISREG
from typing import Any

import yaml

from gobby.paths import get_global_workflows_dir
from gobby.storage.definitions.agents import (
    AgentDefinitionManager,
)
from gobby.storage.definitions.agents import (
    parent_body as agent_parent_body,
)
from gobby.storage.definitions.pipelines import PipelineDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.definitions.variables import SessionVariableDefaultManager
from gobby.storage.project_checkouts import CheckoutNotFoundError, require_root
from gobby.storage.projects import CHECKOUT_FREE_PROJECT_IDS, LocalProjectManager
from gobby.storage.workspace_machine_scope import require_local_machine_id
from gobby.workflows.agent_models import AgentDefinitionBody
from gobby.workflows.definitions import (
    PipelineDefinition,
    VariableDefinitionBody,
    normalize_workflow_definition_enabled,
    validate_workflow_definition_data,
)
from gobby.workflows.sync_rules import sync_rule_file

logger = logging.getLogger(__name__)

_KIND_TABLES = ("agent", "rule", "variable", "pipeline")
_KIND_SUBDIRS = ("rules", "agents", "pipelines", "variables")
_PROJECT_IMPORT_TAG = "project-workflow-import"
_KIND_MANAGERS: dict[
    str,
    type[AgentDefinitionManager]
    | type[RuleDefinitionManager]
    | type[SessionVariableDefaultManager]
    | type[PipelineDefinitionManager],
] = {
    "agent": AgentDefinitionManager,
    "rule": RuleDefinitionManager,
    "variable": SessionVariableDefaultManager,
    "pipeline": PipelineDefinitionManager,
}


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


def _imported_definition_name(data: dict[str, Any], declared_type: str) -> str:
    """Return the logical name used for kind lookup and upsert."""
    if declared_type == "variable":
        return str(data.get("variable") or data["name"])
    return str(data["name"])


def _upsert_agent(db: Any, data: dict[str, Any], project_id: str | None) -> Any:
    body = AgentDefinitionBody.model_validate(data)
    dumped = body.model_dump(mode="json")
    parent_body = agent_parent_body(dumped)
    step_workflow = dumped.get("step_workflow")
    manager = AgentDefinitionManager(db)
    name = str(data["name"])
    existing = manager.get_by_name(name, project_id=project_id)
    description = data.get("description", "")
    enabled = normalize_workflow_definition_enabled(data)
    if existing is not None and existing.project_id == project_id:
        manager.update(
            existing.id,
            definition_json=parent_body,
            description=description,
            enabled=enabled,
        )
        return manager.set_step_workflow(existing.id, step_workflow)
    return manager.upsert_with_steps(
        name,
        parent_body,
        step_workflow,
        source="installed",
        project_id=project_id,
        enabled=enabled,
        description=description,
    )


def _upsert_rule(db: Any, data: dict[str, Any], project_id: str | None) -> Any:
    from gobby.workflows.definitions import RuleDefinitionBody
    from gobby.workflows.delivery_disposition import prepare_rule_definition_for_persist

    body = {key: data[key] for key in RuleDefinitionBody.model_fields if key in data}
    RuleDefinitionBody.model_validate(body)
    manager = RuleDefinitionManager(db)
    name = str(data["name"])
    data = prepare_rule_definition_for_persist(name, data)
    existing = manager.get_by_name(name, project_id=project_id)
    fields = {
        "definition_json": data,
        "description": data.get("description", ""),
        "enabled": normalize_workflow_definition_enabled(data),
        "priority": data.get("priority", 100),
        "sources": data.get("sources"),
        "tags": data.get("tags"),
    }
    if existing is not None and existing.project_id == project_id:
        return manager.update(existing.id, **fields)
    return manager.create(name=name, project_id=project_id, source="installed", **fields)


def _upsert_variable(db: Any, data: dict[str, Any], project_id: str | None) -> Any:
    name = _imported_definition_name(data, "variable")
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
    if existing is not None and existing.project_id == project_id:
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
    definition = PipelineDefinition.model_validate(data)
    if "gobby" in definition.tags:
        raise ValueError("The 'gobby' pipeline tag is reserved for bundled definitions")
    manager = PipelineDefinitionManager(db)
    name = str(data["name"])
    existing = manager.get_by_name(name, project_id=project_id)
    fields = {
        "definition_json": data,
        "description": data.get("description", ""),
        "version": str(data.get("version", "1.0")),
        "enabled": normalize_workflow_definition_enabled(data),
        "tags": definition.tags,
    }
    if existing is not None and existing.project_id == project_id:
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

    name = _imported_definition_name(data, declared_type)
    _refuse_kind_change(name, _lookup_existing_kind(db, name, project_id), declared_type)

    if declared_type == "agent":
        return _upsert_agent(db, data, project_id)
    if declared_type == "rule":
        return _upsert_rule(db, data, project_id)
    if declared_type == "variable":
        return _upsert_variable(db, data, project_id)
    return _upsert_pipeline(db, data, project_id)


def _mark_project_import(db: Any, kind: str, name: str, project_id: str | None) -> None:
    """Record filesystem ownership separately from the shared installed source."""
    if project_id is None:
        return
    manager = _KIND_MANAGERS[kind](db)
    row = manager.get_by_name(name, project_id=project_id)
    if row is None or row.project_id != project_id or row.source != "installed":
        return
    tags = row.tags or []
    if "gobby" not in tags and _PROJECT_IMPORT_TAG not in tags:
        manager.update_from_sync(row.id, tags=[*tags, _PROJECT_IMPORT_TAG])


def sync_imported_workflow_file(
    db: Any,
    path: Path,
    project_id: str | None,
    *,
    on_disk: set[tuple[str, str]] | None = None,
) -> Any:
    """Load one workflow YAML file and upsert it into the runtime database."""
    with path.open(encoding="utf-8") as file:
        data = yaml.safe_load(file)
    if isinstance(data, dict) and "name" not in data and isinstance(data.get("rules"), dict):
        # A rule file in the `gobby rules import` format: file-level group and
        # tags over a `rules` mapping. Import it through that same path.
        result = sync_rule_file(db, rule_file=path, project_id=project_id)
        if on_disk is not None:
            for name in data["rules"]:
                if isinstance(name, str):
                    _mark_project_import(db, "rule", name, project_id)
        if result["errors"]:
            raise ValueError("; ".join(result["errors"]))
        if on_disk is not None:
            on_disk.update(("rule", name) for name in data["rules"])
        return result
    if not isinstance(data, dict) or "name" not in data:
        raise ValueError("Invalid workflow: missing 'name' field")
    result = sync_imported_definition(db, data, project_id)
    if on_disk is not None:
        kind = data.get("type")
        if kind not in _KIND_TABLES:
            kind = validate_workflow_definition_data(data)
        name = _imported_definition_name(data, kind)
        _mark_project_import(db, kind, name, project_id)
        on_disk.add((kind, name))
    return result


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
            if project.id in CHECKOUT_FREE_PROJECT_IDS:
                continue
            try:
                machine_id = require_local_machine_id(
                    None, resource_kind="project_checkout", resource_id=project.id
                )
                checkout_root = require_root(db, project.id, machine_id)
            except CheckoutNotFoundError:
                continue
            roots.append((Path(checkout_root) / ".gobby" / "workflows", project.id))

    synced = 0
    orphaned = 0
    errors: list[str] = []
    for root, scope_id in roots:
        # An absent workflows directory in a live checkout is an empty scan.
        # An unavailable checkout must never authorize deleting its definitions.
        if scope_id is not None and not root.parent.is_dir():
            continue
        on_disk: set[tuple[str, str]] = set()
        scan_complete = True
        try:
            paths = _iter_kind_yaml(root)
        except OSError as exc:
            message = f"Failed to scan imported workflows {root}: {exc}"
            logger.warning(message)
            errors.append(message)
            continue
        for path in paths:
            try:
                sync_imported_workflow_file(db, path, scope_id, on_disk=on_disk)
                synced += 1
            except Exception as exc:
                message = f"Failed to sync imported workflow {path}: {exc}"
                logger.warning(message)
                errors.append(message)
                scan_complete = False

        if scope_id is not None and scan_complete:
            for kind, manager_type in _KIND_MANAGERS.items():
                manager = manager_type(db)
                for row in manager.list_all(project_id=scope_id):
                    if (
                        row.project_id == scope_id
                        and row.source == "installed"
                        and "gobby" not in (row.tags or [])
                        # Pipeline DB writers use custom source; installed also
                        # identifies older file imports (including crew-*).
                        # Other kinds share installed source with DB-only writers.
                        and (kind == "pipeline" or _PROJECT_IMPORT_TAG in (row.tags or []))
                        and (kind, row.name) not in on_disk
                    ):
                        manager.delete(row.id)
                        orphaned += 1
                        logger.debug("Retired imported %s %s from %s", kind, row.name, root)

    return {"synced": synced, "orphaned": orphaned, "errors": errors}


def _iter_kind_yaml(root: Path) -> list[Path]:
    """Yield YAML files under per-kind workflow subdirectories."""
    paths: list[Path] = []
    for kind in _KIND_SUBDIRS:
        kind_dir = root / kind
        try:
            entries = list(kind_dir.iterdir())
        except (FileNotFoundError, NotADirectoryError):
            continue
        # iterdir surfaces access failures; glob suppresses them, which would
        # turn an unreadable directory into an authoritative empty scan.
        for path in sorted(entries):
            if path.suffix not in {".yaml", ".yml"}:
                continue
            try:
                if S_ISREG(path.stat().st_mode):
                    paths.append(path)
            except (FileNotFoundError, NotADirectoryError):
                continue
    return paths
