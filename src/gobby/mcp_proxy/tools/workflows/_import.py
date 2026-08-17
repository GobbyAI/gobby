"""Cache reload for bundled and imported definitions."""

import logging
from typing import Any

from gobby.agents.detection.registry import DetectionManifestRegistry
from gobby.sync_registry import sync_bundled_content_to_db
from gobby.workflows.imports import sync_imported_workflows
from gobby.workflows.pipeline_loader import PipelineLoader

logger = logging.getLogger(__name__)

_RELOAD_ONLY = frozenset({"rules", "agents", "pipelines", "variables", "detection_manifests"})


def reload_cache(
    loader: PipelineLoader,
    db: Any | None = None,
    *,
    project_path: str | None = None,
    project_id: str | None = None,
    detection_registry: DetectionManifestRegistry | None = None,
) -> dict[str, Any]:
    """Clear the pipeline cache and re-sync imported plus selected bundled types."""
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

        sync_result = sync_bundled_content_to_db(db, only=_RELOAD_ONLY)
        total_synced = 0
        for content_type, detail in sync_result["details"].items():
            if not isinstance(detail, dict):
                continue
            if detail.get("skipped"):
                continue
            if "error" in detail:
                result[f"{content_type}_sync_error"] = str(detail["error"])
                continue
            synced = int(detail.get("synced", 0)) + int(detail.get("updated", 0))
            result[f"{content_type}_synced"] = synced
            total_synced += synced
            if synced > 0:
                logger.info("Re-synced %s bundled %s to DB", synced, content_type)
        if sync_result["errors"]:
            result["bundled_sync_errors"] = list(sync_result["errors"])
        for error in sync_result["errors"]:
            logger.warning("%s", error)
        if total_synced > 0:
            result["message"] += f", {total_synced} definitions re-synced to DB"

    if detection_registry is not None:
        result["detection_manifests_reloaded"] = detection_registry.reload()

    return result
