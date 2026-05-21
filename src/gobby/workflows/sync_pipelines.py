"""Pipeline definition synchronization from bundled YAML templates.

Single-row model: templates live on disk only. The DB holds installed rows
directly. Installed rows are overwritten when the template changes
(preserving the user's enabled toggle). Soft-deleted rows are restored only
when a managed bundled definition reappears with different content.
"""

import json
import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from gobby.storage.database import DatabaseProtocol
from gobby.storage.sql_dialect import json_array_contains_condition
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager, WorkflowDefinitionRow
from gobby.workflows.definitions import PipelineDefinition, WorkflowDefinition

logger = logging.getLogger(__name__)

VALID_WORKFLOW_TYPES = {"rule", "variable", "agent", "pipeline"}


def get_bundled_pipelines_path() -> Path:
    """Get the path to bundled pipelines directory."""
    from gobby.paths import get_install_dir

    return get_install_dir() / "shared" / "workflows" / "pipelines"


def get_bundled_workflows_path() -> Path:
    """Get the path to the root bundled workflows directory."""
    return get_bundled_pipelines_path().parent


def iter_bundled_pipeline_files(pipelines_path: Path) -> list[Path]:
    """Return bundled pipeline YAML files from root workflows and pipelines dirs."""
    workflows_path = pipelines_path.parent
    root_files = sorted(workflows_path.glob("*.yaml")) if workflows_path.exists() else []
    pipeline_files = sorted(pipelines_path.glob("*.yaml")) if pipelines_path.exists() else []
    return root_files + pipeline_files


def _is_sync_managed_bundled_pipeline(existing: WorkflowDefinitionRow) -> bool:
    """Return whether an existing row is safe for bundled pipeline sync to own."""
    return (
        existing.project_id is None
        and existing.source in {"installed", "template"}
        and "gobby" in (existing.tags or [])
    )


def _build_pipeline_update_fields(
    existing: WorkflowDefinitionRow,
    *,
    definition_json: str,
    description: str,
    version: str,
    enabled: bool,
    priority: int,
    sources: list[str] | None,
    workflow_type: str,
    restore: bool = False,
) -> dict[str, Any]:
    """Build changed fields for a managed bundled pipeline row."""
    fields: dict[str, Any] = {}
    if existing.definition_json != definition_json:
        fields.update(
            {
                "definition_json": definition_json,
                "description": description,
                "version": version,
                "priority": priority,
                "sources": sources,
                "workflow_type": workflow_type,
            }
        )
    if existing.source != "installed":
        logger.debug(
            "Migrating bundled pipeline to installed source",
            extra={
                "name": existing.name,
                "from_source": existing.source,
                "yaml_enabled": enabled,
                "existing_enabled": existing.enabled,
            },
        )
        fields["source"] = "installed"
        fields["tags"] = ["gobby"]
        fields["enabled"] = enabled
    elif restore:
        logger.debug(
            "Restoring soft-deleted bundled pipeline; re-applying yaml enabled flag",
            extra={
                "name": existing.name,
                "yaml_enabled": enabled,
                "existing_enabled": existing.enabled,
            },
        )
        fields["enabled"] = enabled
    return fields


def sync_bundled_pipelines(db: DatabaseProtocol) -> dict[str, Any]:
    """Sync bundled pipeline definitions from install/shared/workflows/ to the database.

    Creates installed rows directly from template files. Installed rows are
    overwritten when the template changes (preserving the user's enabled toggle).
    Root workflow YAML files such as dev.yaml and qa.yaml are pipelines too and
    sync alongside files in workflows/pipelines/.

    Args:
        db: Database connection

    Returns:
        Dict with success status and counts
    """
    workflows_path = get_bundled_pipelines_path()

    result: dict[str, Any] = {
        "success": True,
        "synced": 0,
        "updated": 0,
        "skipped": 0,
        "errors": [],
    }

    if not workflows_path.exists():
        logger.warning("Bundled workflows path not found", extra={"path": str(workflows_path)})
        result["errors"].append(f"Workflows path not found: {workflows_path}")
        return result

    manager = LocalWorkflowDefinitionManager(db)
    on_disk: set[str] = set()

    for yaml_file in iter_bundled_pipeline_files(workflows_path):
        try:
            raw_content = yaml_file.read_text(encoding="utf-8")
            data = yaml.safe_load(raw_content)

            if not isinstance(data, dict):
                logger.warning("Skipping non-dict YAML file", extra={"workflow": str(yaml_file)})
                continue

            if "name" not in data:
                logger.warning(
                    "Skipping YAML without 'name' field", extra={"workflow": str(yaml_file)}
                )
                continue

            # Validate against Pydantic schema
            schema_cls = (
                PipelineDefinition if data.get("type") == "pipeline" else WorkflowDefinition
            )
            try:
                schema_cls(**data)
            except ValidationError as ve:
                logger.warning(
                    "Skipping invalid workflow",
                    extra={"workflow": str(yaml_file), "error": str(ve)},
                )
                continue

            name = data["name"]
            on_disk.add(name)
            definition_json = json.dumps(data)

            yaml_type = data.get("type", "")
            workflow_type = yaml_type if yaml_type in VALID_WORKFLOW_TYPES else "pipeline"
            description = data.get("description", "")
            version = str(data.get("version", "1.0"))
            enabled = bool(data.get("enabled", False))
            priority = data.get("priority", 100)
            sources_list = data.get("sources")

            # Check if pipeline already exists (any source, including soft-deleted)
            existing = manager.get_by_name(name, include_deleted=True)

            if existing is not None:
                if existing.workflow_type != workflow_type:
                    logger.debug(
                        "Skipping bundled pipeline due to workflow_type conflict",
                        extra={
                            "name": existing.name,
                            "id": existing.id,
                            "existing_workflow_type": existing.workflow_type,
                            "yaml_workflow_type": workflow_type,
                        },
                    )
                    result["skipped"] += 1
                    continue

                if existing.deleted_at is not None:
                    if (
                        _is_sync_managed_bundled_pipeline(existing)
                        and existing.definition_json != definition_json
                    ):
                        manager.restore(existing.id)
                        manager.update(
                            existing.id,
                            **_build_pipeline_update_fields(
                                existing,
                                definition_json=definition_json,
                                description=description,
                                version=version,
                                enabled=enabled,
                                priority=priority,
                                sources=sources_list,
                                workflow_type=workflow_type,
                                restore=True,
                            ),
                        )
                        result["updated"] += 1
                        continue
                    result["skipped"] += 1
                    continue

                if _is_sync_managed_bundled_pipeline(existing):
                    update_fields = _build_pipeline_update_fields(
                        existing,
                        definition_json=definition_json,
                        description=description,
                        version=version,
                        enabled=enabled,
                        priority=priority,
                        sources=sources_list,
                        workflow_type=workflow_type,
                    )
                    if update_fields:
                        manager.update(existing.id, **update_fields)
                        result["updated"] += 1
                        continue

                result["skipped"] += 1
                continue

            # Create new installed row directly
            manager.create(
                name=name,
                definition_json=definition_json,
                workflow_type=workflow_type,
                project_id=None,
                description=description,
                version=version,
                enabled=enabled,
                priority=priority,
                sources=sources_list,
                source="installed",
                tags=["gobby"],
            )
            logger.info("Synced bundled workflow definition", extra={"workflow": name})
            result["synced"] += 1

        except Exception as e:
            error_msg = f"Failed to sync workflow definition '{yaml_file}': {e}"
            logger.error(
                "Failed to sync workflow definition",
                extra={"workflow": str(yaml_file), "error": str(e)},
            )
            result["errors"].append(error_msg)

    # Orphan cleanup: soft-delete pipeline rows whose YAML was removed.
    # Only touch gobby-tagged pipeline-type rows.
    tag_condition, tag_params = json_array_contains_condition(db, "tags", "gobby")
    orphan_rows = db.fetchall(
        "SELECT id, name FROM workflow_definitions "
        "WHERE workflow_type = 'pipeline' "
        f"AND {tag_condition} AND deleted_at IS NULL",
        tag_params,
    )
    result["orphaned"] = 0
    for row in orphan_rows:
        if row["name"] not in on_disk:
            manager.delete(row["id"])
            logger.info("Soft-deleted orphaned bundled workflow", extra={"workflow": row["name"]})
            result["orphaned"] += 1

    total = result["synced"] + result["updated"] + result["skipped"]
    logger.info(
        "Workflow definition sync complete",
        extra={
            "synced": result["synced"],
            "updated": result["updated"],
            "skipped": result["skipped"],
            "orphaned": result.get("orphaned", 0),
            "total": total,
        },
    )

    return result
