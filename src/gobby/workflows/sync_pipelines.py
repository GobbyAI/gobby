"""Pipeline definition synchronization from bundled YAML templates.

Single-row model: templates live on disk only. The DB holds installed rows
directly. Installed rows are overwritten when the template changes
(preserving a pinned enabled toggle). Soft-deleted rows are restored only
when a managed bundled definition reappears with different content.
"""

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from gobby.storage.definitions.pipelines import (
    PipelineDefinitionManager,
    PipelineDefinitionRow,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import (
    PipelineDefinition,
    normalize_workflow_definition_enabled,
)

logger = logging.getLogger(__name__)


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


def _is_sync_managed_bundled_pipeline(existing: PipelineDefinitionRow) -> bool:
    """Return whether an existing row is safe for bundled pipeline sync to own."""
    return (
        existing.project_id is None
        and existing.source in {"installed", "template"}
        and "gobby" in (existing.tags or [])
    )


def _build_pipeline_update_fields(
    existing: PipelineDefinitionRow,
    *,
    definition_json: dict[str, Any],
    description: str,
    version: str,
    enabled: bool,
    restore: bool = False,
) -> dict[str, Any]:
    """Build changed fields for a managed bundled pipeline row."""
    fields: dict[str, Any] = {}
    if existing.definition_json != definition_json:
        fields["definition_json"] = definition_json
        fields["description"] = description
        fields["version"] = version
    if existing.source != "installed":
        logger.debug(
            "Migrating bundled pipeline to installed source",
            extra={
                "pipeline_name": existing.name,
                "from_source": existing.source,
                "yaml_enabled": enabled,
                "existing_enabled": existing.enabled,
            },
        )
        fields["source"] = "installed"
        fields["tags"] = ["gobby"]
        if not existing.enabled_pinned:
            fields["enabled"] = enabled
    elif restore:
        logger.debug(
            "Restoring soft-deleted bundled pipeline; re-applying yaml enabled flag",
            extra={
                "pipeline_name": existing.name,
                "yaml_enabled": enabled,
                "existing_enabled": existing.enabled,
            },
        )
        if not existing.enabled_pinned:
            fields["enabled"] = enabled
    elif not existing.enabled_pinned and existing.enabled != enabled:
        fields["enabled"] = enabled
    if existing.description != description:
        fields["description"] = description
    if existing.version != version:
        fields["version"] = version
    return fields


def sync_bundled_pipelines(db: HubDatabase) -> dict[str, Any]:
    """Sync bundled pipeline definitions from install/shared/workflows/ to the database."""
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

    manager = PipelineDefinitionManager(db)
    on_disk: set[str] = set()
    yaml_files = iter_bundled_pipeline_files(workflows_path)
    scan_complete = bool(yaml_files)

    for yaml_file in yaml_files:
        try:
            raw_content = yaml_file.read_text(encoding="utf-8")
            data = yaml.safe_load(raw_content)

            if not isinstance(data, dict):
                logger.warning("Skipping non-dict YAML file", extra={"workflow": str(yaml_file)})
                scan_complete = False
                continue

            if data.get("type") != "pipeline":
                logger.warning(
                    "Skipping non-pipeline YAML file",
                    extra={"workflow": str(yaml_file), "workflow_type": data.get("type")},
                )
                scan_complete = False
                continue

            if "name" not in data:
                logger.warning(
                    "Skipping YAML without 'name' field", extra={"workflow": str(yaml_file)}
                )
                scan_complete = False
                continue

            try:
                PipelineDefinition(**data)
            except ValidationError as ve:
                logger.warning(
                    "Skipping invalid workflow",
                    extra={"workflow": str(yaml_file), "error": str(ve)},
                )
                scan_complete = False
                continue

            name = data["name"]
            on_disk.add(name)
            description = data.get("description", "")
            version = str(data.get("version", "1.0"))
            enabled = normalize_workflow_definition_enabled(data)

            existing = manager.get_by_name(name, include_deleted=True)

            if existing is not None:
                if existing.deleted_at is not None:
                    if _is_sync_managed_bundled_pipeline(existing) and (
                        existing.definition_json != data
                    ):
                        manager.restore(existing.id)
                        update_fields = _build_pipeline_update_fields(
                            existing,
                            definition_json=data,
                            description=description,
                            version=version,
                            enabled=enabled,
                            restore=True,
                        )
                        if update_fields.pop("source", None) is not None:
                            manager.move_to_global(existing.id)
                        if update_fields:
                            manager.update_from_sync(existing.id, **update_fields)
                        result["updated"] += 1
                        continue
                    result["skipped"] += 1
                    continue

                if _is_sync_managed_bundled_pipeline(existing):
                    update_fields = _build_pipeline_update_fields(
                        existing,
                        definition_json=data,
                        description=description,
                        version=version,
                        enabled=enabled,
                    )
                    if update_fields:
                        if update_fields.pop("source", None) is not None:
                            manager.move_to_global(existing.id)
                        manager.update_from_sync(existing.id, **update_fields)
                        result["updated"] += 1
                        continue

                result["skipped"] += 1
                continue

            manager.create(
                name=name,
                definition_json=data,
                project_id=None,
                description=description,
                version=version,
                enabled=enabled,
                source="installed",
                tags=["gobby"],
            )
            logger.debug("Synced bundled workflow definition", extra={"workflow": name})
            result["synced"] += 1

        except Exception as e:
            error_msg = f"Failed to sync workflow definition '{yaml_file}': {e}"
            logger.error(
                "Failed to sync workflow definition",
                extra={"workflow": str(yaml_file), "error": str(e)},
            )
            result["errors"].append(error_msg)
            scan_complete = False

    result["orphaned"] = 0
    if scan_complete:
        for row in manager.list_all():
            if (
                row.source == "installed"
                and "gobby" in (row.tags or [])
                and row.name not in on_disk
            ):
                manager.delete(row.id)
                logger.debug("Soft-deleted orphaned bundled workflow", extra={"workflow": row.name})
                result["orphaned"] += 1
    else:
        logger.warning(
            "Skipping bundled workflow orphan cleanup because the template scan was incomplete"
        )

    total = result["synced"] + result["updated"] + result["skipped"]
    logger.debug(
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
