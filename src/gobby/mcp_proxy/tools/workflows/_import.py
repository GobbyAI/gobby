"""
Import and cache tools for workflows.
"""

import logging
import re
import shutil
from pathlib import Path
from typing import Any

import yaml

from gobby.utils.project_context import get_workflow_project_path
from gobby.workflows.loader import WorkflowLoader

logger = logging.getLogger(__name__)


def import_workflow(
    loader: WorkflowLoader,
    source_path: str,
    workflow_name: str | None = None,
    is_global: bool = False,
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    Import a workflow from a file.

    Args:
        loader: WorkflowLoader instance
        source_path: Path to the workflow YAML file
        workflow_name: Override the workflow name (defaults to name in file)
        is_global: Install to global ~/.gobby/workflows instead of project
        project_path: Project directory path. Auto-discovered from cwd if not provided.

    Returns:
        Success status and destination path
    """
    source = Path(source_path)
    if not source.exists():
        return {"success": False, "error": f"File not found: {source_path}"}

    if source.suffix != ".yaml":
        return {"success": False, "error": "Workflow file must have .yaml extension"}

    try:
        with open(source, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not data or "name" not in data:
            return {"success": False, "error": "Invalid workflow: missing 'name' field"}

    except yaml.YAMLError as e:
        return {"success": False, "error": f"Invalid YAML: {e}"}

    raw_name = workflow_name or data.get("name", source.stem)
    # Sanitize name to prevent path traversal: strip path components, allow only safe chars
    safe_name = Path(raw_name).name  # Strip any path components
    safe_name = re.sub(r"[^a-zA-Z0-9_\-.]", "_", safe_name)  # Replace unsafe chars
    safe_name = safe_name.strip("._")  # Remove leading/trailing dots and underscores
    if not safe_name:
        safe_name = source.stem  # Fallback to source filename
    filename = f"{safe_name}.yaml"

    if is_global:
        dest_dir = Path.home() / ".gobby" / "workflows"
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
        dest_dir = proj / ".gobby" / "workflows"

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / filename

    shutil.copy(source, dest_path)

    # Clear loader cache so new workflow is discoverable
    loader.clear_cache()

    return {
        "success": True,
        "workflow_name": safe_name,
        "destination": str(dest_path),
        "is_global": is_global,
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
            ("workflows", "gobby.workflows.sync", "sync_bundled_workflows"),
            ("rules", "gobby.workflows.sync", "sync_bundled_rules"),
            ("variables", "gobby.workflows.sync", "sync_bundled_variables"),
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
