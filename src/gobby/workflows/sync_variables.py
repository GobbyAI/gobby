"""Variable definition synchronization from bundled YAML templates.

Single-row model: templates live on disk only. The DB holds installed rows
directly. Sync-managed rows are refreshed or restored from their on-disk YAML,
while user/custom rows remain protected.
"""

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from gobby.storage.definitions.variables import (
    SessionVariableDefaultManager,
    SessionVariableDefaultRow,
)
from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)


def get_bundled_variables_path() -> Path:
    """Get the path to bundled variables directory.

    Returns:
        Path to src/gobby/install/shared/workflows/variables/
    """
    from gobby.paths import get_install_dir

    return get_install_dir() / "shared" / "workflows" / "variables"


def sync_bundled_variables(
    db: HubDatabase,
    variables_path: Path | list[Path] | None = None,
    tag: str = "gobby",
) -> dict[str, Any]:
    """Sync variable definitions from YAML files to session_variable_defaults.

    Creates installed rows directly from template files. Existing bundled
    rows are refreshed when the YAML changes, preserving a pinned enabled
    toggle. User/custom rows are not overwritten.

    Args:
        db: Database connection.
        variables_path: Complete variable roots to scan, or one root for bundled
            variables. Defaults to the bundled root. A single non-gobby root is
            a partial scan and does not prune orphans.
        tag: Tag to apply. Defaults to "gobby" for bundled.

    Returns:
        Dict with success status and counts.
    """
    if variables_path is None:
        variables_paths = [get_bundled_variables_path()]
        scan_is_authoritative = True
    elif isinstance(variables_path, Path):
        variables_paths = [variables_path]
        scan_is_authoritative = tag == "gobby"
    else:
        variables_paths = variables_path
        scan_is_authoritative = True

    result: dict[str, Any] = {
        "success": True,
        "synced": 0,
        "updated": 0,
        "skipped": 0,
        "errors": [],
    }

    existing_paths = [path for path in variables_paths if path.exists()]
    if not existing_paths:
        logger.debug(
            "Variables paths not found", extra={"paths": [str(path) for path in variables_paths]}
        )
        return result

    manager = SessionVariableDefaultManager(db)
    on_disk: set[str] = set()

    variable_files = sorted(
        yaml_file for path in existing_paths for yaml_file in path.glob("*.yaml")
    )
    for yaml_file in variable_files:
        try:
            raw_content = yaml_file.read_text(encoding="utf-8")
            data = yaml.safe_load(raw_content)

            if not isinstance(data, dict):
                logger.warning("Skipping non-dict YAML", extra={"file": str(yaml_file)})
                continue

            variables_dict = data.get("variables")
            if not isinstance(variables_dict, dict):
                logger.debug("No 'variables' key in YAML, skipping", extra={"file": str(yaml_file)})
                result["skipped"] += 1
                continue

            file_tags = data.get("tags") or []
            if tag not in file_tags:
                file_tags = [*file_tags, tag]

            for var_name, var_data in variables_dict.items():
                if not isinstance(var_data, dict):
                    result["errors"].append(
                        f"Variable '{var_name}' in {yaml_file.name} is not a dict"
                    )
                    continue

                on_disk.add(var_name)

                try:
                    _sync_single_variable(
                        manager=manager,
                        var_name=var_name,
                        var_data=var_data,
                        file_tags=file_tags,
                        sync_tag=tag,
                        result=result,
                    )
                except Exception as e:
                    error_msg = f"Failed to sync variable '{var_name}' from {yaml_file.name}: {e}"
                    logger.warning(error_msg)
                    result["errors"].append(error_msg)

        except Exception as e:
            error_msg = f"Failed to parse variable file '{yaml_file}': {e}"
            logger.error(error_msg)
            result["errors"].append(error_msg)

    result["orphaned"] = 0
    if scan_is_authoritative and variable_files and not result["errors"]:
        for row in manager.list_all():
            if _is_sync_managed_variable(row, tag) and row.name not in on_disk:
                manager.delete(row.id)
                logger.debug("Soft-deleted orphaned bundled variable", extra={"variable": row.name})
                result["orphaned"] += 1

    result["success"] = not result["errors"]

    total = result["synced"] + result["updated"] + result["skipped"]
    logger.debug(
        "Variable definition sync complete",
        extra={
            "synced": result["synced"],
            "updated": result["updated"],
            "skipped": result["skipped"],
            "orphaned": result.get("orphaned", 0),
            "total": total,
        },
    )

    return result


def _sync_single_variable(
    manager: SessionVariableDefaultManager,
    var_name: str,
    var_data: dict[str, Any],
    file_tags: list[str] | None,
    sync_tag: str,
    result: dict[str, Any],
) -> None:
    """Sync a single variable to session_variable_defaults."""
    from gobby.workflows.definitions import VariableDefinitionBody

    default_value = var_data.get("value")
    description = var_data.get("description")
    enabled = bool(var_data.get("enabled", True))

    try:
        VariableDefinitionBody(variable=var_name, value=default_value, description=description)
    except ValidationError as ve:
        raise ValueError(f"Invalid variable definition: {ve}") from ve

    existing = manager.get_by_name(var_name, include_deleted=True)

    if existing is not None:
        if existing.deleted_at is not None:
            if _is_sync_managed_variable(existing, sync_tag):
                manager.restore(existing.id)
                update_fields = _build_variable_update_fields(
                    existing=existing,
                    default_value=default_value,
                    description=description,
                    enabled=enabled,
                    tags=file_tags,
                )
                if update_fields:
                    manager.update_from_sync(existing.id, **update_fields)
                result["updated"] += 1
                return
            result["skipped"] += 1
            return

        if _is_sync_managed_variable(existing, sync_tag):
            update_fields = _build_variable_update_fields(
                existing=existing,
                default_value=default_value,
                description=description,
                enabled=enabled,
                tags=file_tags,
            )
            if update_fields:
                manager.update_from_sync(existing.id, **update_fields)
                result["updated"] += 1
                return

        result["skipped"] += 1
        return

    manager.create(
        name=var_name,
        default_value=default_value,
        project_id=None,
        description=description,
        enabled=enabled,
        source="installed",
        tags=file_tags,
    )
    result["synced"] += 1


def _is_sync_managed_variable(existing: SessionVariableDefaultRow, sync_tag: str) -> bool:
    """Return whether an existing row is safe for template sync to manage."""
    return (
        existing.source == "installed"
        and existing.project_id is None
        and sync_tag in (existing.tags or [])
    )


def _build_variable_update_fields(
    *,
    existing: SessionVariableDefaultRow,
    default_value: Any,
    description: str | None,
    enabled: bool,
    tags: list[str] | None,
) -> dict[str, Any]:
    """Build the minimal field set needed to refresh a bundled variable row."""
    update_fields: dict[str, Any] = {}
    if existing.default_value != default_value:
        update_fields["default_value"] = default_value
    if existing.description != description:
        update_fields["description"] = description
    if not existing.enabled_pinned and existing.enabled != enabled:
        update_fields["enabled"] = enabled
    if existing.tags != tags:
        update_fields["tags"] = tags
    return update_fields
