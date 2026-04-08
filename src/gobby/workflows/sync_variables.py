"""Variable definition synchronization from bundled YAML templates.

Single-row model: templates live on disk only. The DB holds installed rows
directly. Installed rows are overwritten when the template changes
(preserving the user's enabled toggle). Soft-deleted rows are not restored.
"""

import json
import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from gobby.storage.database import DatabaseProtocol
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

logger = logging.getLogger(__name__)


def get_bundled_variables_path() -> Path:
    """Get the path to bundled variables directory.

    Returns:
        Path to src/gobby/install/shared/workflows/variables/
    """
    from gobby.paths import get_install_dir

    return get_install_dir() / "shared" / "workflows" / "variables"


def sync_bundled_variables(
    db: DatabaseProtocol,
    variables_path: Path | None = None,
    tag: str = "gobby",
) -> dict[str, Any]:
    """Sync variable definitions from YAML files to the database.

    Creates installed rows directly from template files. Installed rows are
    overwritten when the template changes (preserving the user's enabled toggle).
    Soft-deleted rows are not restored.

    Args:
        db: Database connection.
        variables_path: Path to variables directory. Defaults to bundled path.
        tag: Tag to apply. Defaults to "gobby" for bundled.

    Returns:
        Dict with success status and counts.
    """
    if variables_path is None:
        variables_path = get_bundled_variables_path()

    result: dict[str, Any] = {
        "success": True,
        "synced": 0,
        "updated": 0,
        "skipped": 0,
        "errors": [],
    }

    if not variables_path.exists():
        logger.debug("Variables path not found", extra={"path": str(variables_path)})
        return result

    manager = LocalWorkflowDefinitionManager(db)
    on_disk: set[str] = set()

    for yaml_file in sorted(variables_path.glob("*.yaml")):
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

    # Orphan cleanup: soft-delete variable rows whose YAML was removed.
    # Only touch rows with matching tag.
    tag_filter = f'%"{tag}"%'
    orphan_rows = db.fetchall(
        "SELECT id, name FROM workflow_definitions "
        "WHERE workflow_type = 'variable' "
        "AND tags LIKE ? AND deleted_at IS NULL",
        (tag_filter,),
    )
    result["orphaned"] = 0
    for row in orphan_rows:
        if row["name"] not in on_disk:
            manager.delete(row["id"])
            logger.info("Soft-deleted orphaned bundled variable", extra={"variable": row["name"]})
            result["orphaned"] += 1

    total = result["synced"] + result["updated"] + result["skipped"]
    logger.info(
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
    manager: LocalWorkflowDefinitionManager,
    var_name: str,
    var_data: dict[str, Any],
    file_tags: list[str] | None,
    result: dict[str, Any],
) -> None:
    """Sync a single variable to workflow_definitions.

    Creates an installed row if none exists. Overwrites existing installed
    rows when the template changes. Soft-deleted rows are not restored.
    """
    from gobby.workflows.definitions import VariableDefinitionBody

    body_dict: dict[str, Any] = {
        "variable": var_name,
        "value": var_data.get("value"),
    }
    if var_data.get("description"):
        body_dict["description"] = var_data["description"]

    try:
        VariableDefinitionBody(**body_dict)
    except ValidationError as ve:
        raise ValueError(f"Invalid variable definition: {ve}") from ve

    definition_json = json.dumps(body_dict)
    description = var_data.get("description")

    existing = manager.get_by_name(var_name, include_deleted=True)

    if existing is not None:
        if existing.deleted_at is not None:
            result["skipped"] += 1
            return

        # Row exists and is active — skip (no overwrite).
        # Drift between the on-disk template and the DB row is detected
        # via hash comparison at runtime, not corrected during sync.
        result["skipped"] += 1
        return

    manager.create(
        name=var_name,
        definition_json=definition_json,
        workflow_type="variable",
        project_id=None,
        description=description,
        enabled=True,
        priority=100,
        source="installed",
        tags=file_tags,
    )
    result["synced"] += 1
