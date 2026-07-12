"""
Import and cache tools for workflows.
"""

import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any

import yaml

from gobby.paths import get_global_workflows_dir
from gobby.storage.workflow_definitions import (
    LocalWorkflowDefinitionManager,
    WorkflowDefinitionRow,
)
from gobby.utils.project_context import get_workflow_project_path
from gobby.workflows.definitions import WorkflowDefinition, validate_workflow_definition_data
from gobby.workflows.loader import WorkflowLoader

logger = logging.getLogger(__name__)

_WORKFLOW_KINDS = frozenset({"step", "lifecycle"})


def _sync_imported_definition(
    db: Any,
    data: dict[str, Any],
    project_id: str | None,
) -> WorkflowDefinitionRow:
    """Validate and upsert an imported definition into the runtime database."""
    declared_type = data.get("type")
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
        "enabled": bool(data.get("enabled", False)),
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


def import_workflow(
    loader: WorkflowLoader,
    source_path: str,
    workflow_name: str | None = None,
    is_global: bool = False,
    project_path: str | None = None,
    *,
    db: Any | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """
    Import a workflow from a file.

    Args:
        loader: WorkflowLoader instance
        source_path: Path to the workflow YAML file
        workflow_name: Override the workflow name (defaults to name in file)
        is_global: Install to global ~/.gobby/workflows instead of project
        project_path: Project directory path. Auto-discovered from cwd if not provided.
        db: Runtime database that receives the imported definition.
        project_id: Caller project UUID for project-scoped imports.

    Returns:
        Success status and destination path
    """
    source = Path(source_path)
    if not source.exists():
        return {"success": False, "error": f"File not found: {source_path}"}

    if source.suffix != ".yaml":
        return {"success": False, "error": "Workflow file must have .yaml extension"}

    if db is None:
        return {"success": False, "error": "Workflow database is not configured"}

    try:
        with open(source, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict) or "name" not in data:
            return {"success": False, "error": "Invalid workflow: missing 'name' field"}

    except yaml.YAMLError as e:
        return {"success": False, "error": f"Invalid YAML: {e}"}

    source_name = str(data["name"])
    raw_name = workflow_name or source_name
    # Sanitize name to prevent path traversal: strip path components, allow only safe chars
    safe_name = Path(raw_name).name  # Strip any path components
    safe_name = re.sub(r"[^a-zA-Z0-9_\-.]", "_", safe_name)  # Replace unsafe chars
    safe_name = safe_name.strip("._")  # Remove leading/trailing dots and underscores
    if not safe_name:
        safe_name = source.stem  # Fallback to source filename
    data["name"] = safe_name
    filename = f"{safe_name}.yaml"

    if is_global:
        dest_dir = get_global_workflows_dir()
    else:
        # Auto-discover project path if not provided
        if not project_path:
            discovered = get_workflow_project_path()
            if discovered:
                project_path = str(discovered)

        proj = Path(project_path) if project_path else None
        if not proj:
            return {
                "success": False,
                "error": "project_path required when not using is_global (could not auto-discover)",
            }
        if not project_id:
            return {
                "success": False,
                "error": "project_id required when not using is_global",
            }
        dest_dir = proj / ".gobby" / "workflows"

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / filename

    previous_contents = dest_path.read_bytes() if dest_path.exists() else None
    if safe_name == source_name:
        shutil.copy(source, dest_path)
    else:
        dest_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    try:
        row = _sync_imported_definition(db, data, None if is_global else project_id)
    except Exception as e:
        if previous_contents is None:
            dest_path.unlink(missing_ok=True)
        else:
            dest_path.write_bytes(previous_contents)
        return {"success": False, "error": f"Failed to import workflow: {e}"}

    # Clear loader cache so new workflow is discoverable
    loader.clear_cache()

    return {
        "success": True,
        "workflow_name": safe_name,
        "destination": str(dest_path),
        "is_global": is_global,
        "definition_id": row.id,
    }


def reload_cache(
    loader: WorkflowLoader,
    db: Any | None = None,
) -> dict[str, Any]:
    """
    Clear the workflow loader cache and optionally re-sync bundled definitions to the DB.

    This forces the daemon to re-read workflow YAML files from disk
    on the next access. When *db* is provided, also re-syncs bundled
    workflows, rules, agents, and variables from disk YAML into the database.

    Args:
        loader: WorkflowLoader instance whose cache to clear.
        db: Optional database instance. If provided, bundled definitions
            are re-synced to the DB after clearing the cache.

    Returns:
        Success status with optional sync counts.
    """
    loader.clear_cache()
    logger.info("Workflow cache cleared via reload_cache tool")

    result: dict[str, Any] = {"success": True, "message": "Workflow cache cleared"}

    if db is not None:
        sync_targets: list[tuple[str, str, str]] = [
            ("pipelines", "gobby.workflows.sync_pipelines", "sync_bundled_pipelines"),
            ("rules", "gobby.workflows.sync_rules", "sync_bundled_rules"),
            ("variables", "gobby.workflows.sync_variables", "sync_bundled_variables"),
            ("agents", "gobby.agents.sync", "sync_bundled_agents"),
        ]
        total_synced = 0
        for content_type, module_path, func_name in sync_targets:
            try:
                module = __import__(module_path, fromlist=[func_name])
                sync_fn = getattr(module, func_name)
                sync_result = sync_fn(db)
                synced = sync_result.get("synced", 0) + sync_result.get("updated", 0)
                result[f"{content_type}_synced"] = synced
                total_synced += synced
                if synced > 0:
                    logger.info(f"Re-synced {synced} bundled {content_type} to DB")
            except Exception as e:
                logger.warning(f"Failed to re-sync bundled {content_type}: {e}")
                result[f"{content_type}_sync_error"] = str(e)

        if total_synced > 0:
            result["message"] += f", {total_synced} definitions re-synced to DB"

    return result
