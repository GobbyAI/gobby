"""
Import and cache tools for workflows.
"""

import logging
import re
import shutil
from pathlib import Path
from typing import Any

import yaml

from gobby.agents.detection.registry import DetectionManifestRegistry
from gobby.paths import get_global_workflows_dir
from gobby.utils.project_context import get_workflow_project_path
from gobby.workflows.imports import sync_imported_definition, sync_imported_workflows
from gobby.workflows.pipeline_loader import PipelineLoader

logger = logging.getLogger(__name__)


def import_workflow(
    loader: PipelineLoader,
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
        loader: PipelineLoader instance
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
        row = sync_imported_definition(db, data, None if is_global else project_id)
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
    loader: PipelineLoader,
    db: Any | None = None,
    *,
    project_path: str | None = None,
    project_id: str | None = None,
    detection_registry: DetectionManifestRegistry | None = None,
) -> dict[str, Any]:
    """
    Clear the cache and optionally re-sync imported and bundled definitions to the DB.

    This forces the daemon to re-read workflow definitions on the next access.
    When *db* is provided, project/global workflow imports and bundled rules,
    pipelines, agents, and variables are re-synced into the database.

    Args:
        loader: PipelineLoader instance whose cache to clear.
        db: Optional database instance. If provided, bundled definitions
            are re-synced to the DB after clearing the cache.

    Returns:
        Success status with optional sync counts.
    """
    loader.clear_cache()
    logger.info("Workflow cache cleared via reload_cache tool")

    result: dict[str, Any] = {"success": True, "message": "Workflow cache cleared"}

    if db is not None:
        imported = sync_imported_workflows(
            db,
            project_path=project_path,
            project_id=project_id,
        )
        result["imported_workflows_synced"] = imported["synced"]
        if imported["errors"]:
            result["imported_workflow_sync_errors"] = imported["errors"]

        sync_targets: list[tuple[str, str, str]] = [
            ("pipelines", "gobby.workflows.sync_pipelines", "sync_bundled_pipelines"),
            ("rules", "gobby.workflows.sync_rules", "sync_bundled_rules"),
            ("variables", "gobby.workflows.sync_variables", "sync_bundled_variables"),
            ("agents", "gobby.agents.sync", "sync_bundled_agents"),
            (
                "detection_manifests",
                "gobby.agents.detection.registry",
                "sync_bundled_detection_manifests",
            ),
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
                    logger.info("Re-synced %s bundled %s to DB", synced, content_type)
            except Exception as e:
                logger.warning("Failed to re-sync bundled %s: %s", content_type, e)
                result[f"{content_type}_sync_error"] = str(e)

        if total_synced > 0:
            result["message"] += f", {total_synced} definitions re-synced to DB"

    if detection_registry is not None:
        result["detection_manifests_reloaded"] = detection_registry.reload()

    return result
