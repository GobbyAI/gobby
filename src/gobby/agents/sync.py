"""Agent definition synchronization for bundled agents.

Single-row model: templates live on disk only. The DB holds installed rows
directly. Installed rows are overwritten when the template changes
(preserving the user's enabled toggle). Soft-deleted rows are not restored.
"""

import logging
from pathlib import Path
from typing import Any

import yaml

from gobby.storage.database import DatabaseProtocol
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import AgentDefinitionBody

__all__ = ["get_bundled_agents_path", "sync_bundled_agents"]

logger = logging.getLogger(__name__)

_DISCOVERY_PLACEHOLDER_AGENTS = frozenset(
    {
        "analyst",
        "researcher",
        "architect",
        "product-manager",
    }
)


def _is_legacy_discovery_placeholder(name: str, definition_json: str, enabled: bool) -> bool:
    """Return True for bundled disabled discovery placeholders that must become live."""
    if name not in _DISCOVERY_PLACEHOLDER_AGENTS or enabled:
        return False
    return "PLACEHOLDER" in definition_json or "placeholder_agent:" in definition_json


def get_bundled_agents_path() -> Path:
    """Get the path to bundled agents directory.

    Returns:
        Path to src/gobby/install/shared/workflows/agents/
    """
    from gobby.paths import get_install_dir

    return get_install_dir() / "shared" / "workflows" / "agents"


def sync_bundled_agents(db: DatabaseProtocol) -> dict[str, Any]:
    """Sync bundled agent definitions from install/shared/workflows/agents/ to the database.

    Creates installed rows directly from template files. Installed rows are
    overwritten when the template changes (preserving the user's enabled toggle,
    except for legacy disabled discovery placeholders that are upgraded to real
    enabled agents). Soft-deleted rows are not restored.

    Args:
        db: Database connection

    Returns:
        Dict with success status and counts
    """
    agents_path = get_bundled_agents_path()

    result: dict[str, Any] = {
        "success": True,
        "synced": 0,
        "updated": 0,
        "skipped": 0,
        "errors": [],
    }

    if not agents_path.exists():
        logger.warning(f"Bundled agents path not found: {agents_path}")
        result["errors"].append(f"Agents path not found: {agents_path}")
        return result

    manager = LocalWorkflowDefinitionManager(db)
    on_disk: set[str] = set()

    for yaml_file in sorted(agents_path.glob("*.yaml")):
        try:
            raw_content = yaml_file.read_text(encoding="utf-8")
            data = yaml.safe_load(raw_content)

            if not isinstance(data, dict):
                logger.warning(f"Skipping non-dict YAML file: {yaml_file}")
                continue

            name = data.get("name", yaml_file.stem)
            on_disk.add(name)
            data["name"] = name

            # Parse through Pydantic for validation + consistent serialization
            body = AgentDefinitionBody.model_validate(data)
            body_json = body.model_dump_json()

            # Check if agent already exists (any source, including soft-deleted)
            existing = manager.get_by_name(name, include_deleted=True)

            if existing is not None:
                if existing.workflow_type != "agent":
                    logger.debug(
                        f"Agent '{name}' conflicts with existing {existing.workflow_type} "
                        f"definition, skipping"
                    )
                    result["skipped"] += 1
                    continue

                if existing.deleted_at is not None:
                    result["skipped"] += 1
                    continue

                force_enable = _is_legacy_discovery_placeholder(
                    name,
                    existing.definition_json,
                    existing.enabled,
                )
                if existing.source == "installed" and (
                    existing.definition_json != body_json or force_enable
                ):
                    update_fields: dict[str, Any] = {
                        "definition_json": body_json,
                        "description": body.description,
                    }
                    if force_enable:
                        update_fields["enabled"] = body.enabled
                    manager.update(
                        existing.id,
                        **update_fields,
                    )
                    result["updated"] += 1
                    logger.debug(
                        "Updated bundled agent definition %s (%s); installed definition_json changed",
                        existing.id,
                        existing.description or body.description,
                    )
                    continue

                result["skipped"] += 1
                continue

            # Create new installed row directly
            manager.create(
                name=name,
                definition_json=body_json,
                workflow_type="agent",
                description=body.description,
                source="installed",
                enabled=body.enabled,
                tags=["gobby"],
            )
            logger.info(f"Synced bundled agent definition: {name}")
            result["synced"] += 1

        except Exception as e:
            error_msg = f"Failed to sync agent definition '{yaml_file}': {e}"
            logger.error(error_msg)
            result["errors"].append(error_msg)

    # Orphan cleanup: soft-delete agent rows whose YAML was removed.
    # Only touch gobby-tagged agent rows.
    tag_filter = '%"gobby"%'
    orphan_rows = db.fetchall(
        "SELECT id, name FROM workflow_definitions "
        "WHERE workflow_type = 'agent' "
        "AND tags LIKE ? AND deleted_at IS NULL",
        (tag_filter,),
    )
    result["orphaned"] = 0
    for row in orphan_rows:
        if row["name"] not in on_disk:
            manager.delete(row["id"])
            logger.info(f"Soft-deleted orphaned bundled agent: {row['name']}")
            result["orphaned"] += 1

    total = result["synced"] + result["updated"] + result["skipped"]
    logger.info(
        f"Agent definition sync complete: {result['synced']} synced, "
        f"{result['updated']} updated, {result['skipped']} skipped, "
        f"{result.get('orphaned', 0)} orphaned, {total} total"
    )

    return result
