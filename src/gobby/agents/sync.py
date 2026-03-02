"""Agent definition synchronization for bundled agents.

This module provides sync_bundled_agents() which loads agent definitions from the
bundled install/shared/agents/ directory and syncs them to workflow_definitions
with workflow_type='agent'.
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


def get_bundled_agents_path() -> Path:
    """Get the path to bundled agents directory.

    Returns:
        Path to src/gobby/install/shared/agents/
    """
    from gobby.paths import get_install_dir

    return get_install_dir() / "shared" / "agents"


def sync_bundled_agents(db: DatabaseProtocol) -> dict[str, Any]:
    """Sync bundled agent definitions from install/shared/agents/ to workflow_definitions.

    This function:
    1. Walks all .yaml files in the bundled agents directory
    2. Parses each directly as AgentDefinitionBody
    3. Creates new records or updates changed content (idempotent)
    4. All records are stored with workflow_type='agent' and source='template'

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

    for yaml_file in sorted(agents_path.glob("*.yaml")):
        try:
            raw_content = yaml_file.read_text(encoding="utf-8")
            data = yaml.safe_load(raw_content)

            if not isinstance(data, dict):
                logger.warning(f"Skipping non-dict YAML file: {yaml_file}")
                continue

            name = data.get("name", yaml_file.stem)
            data["name"] = name

            # Parse directly as AgentDefinitionBody
            body = AgentDefinitionBody.model_validate(data)
            body_json = body.model_dump_json()

            # Check if agent already exists in workflow_definitions
            existing = manager.get_by_name(name, include_deleted=True, include_templates=True)

            if existing is not None and existing.workflow_type != "agent":
                logger.debug(
                    f"Agent '{name}' conflicts with existing {existing.workflow_type} "
                    f"definition, skipping"
                )
                result["skipped"] += 1
                continue

            if existing is not None and existing.workflow_type == "agent":
                # If user soft-deleted it, respect their intent — skip sync
                if existing.deleted_at is not None:
                    logger.debug(f"Agent definition '{name}' is soft-deleted, skipping sync")
                    result["skipped"] += 1
                    continue

                # Compare definition_json to detect changes
                needs_update = existing.definition_json != body_json

                if needs_update:
                    try:
                        manager.update(
                            existing.id,
                            definition_json=body_json,
                            description=body.description,
                            tags=["gobby"],
                        )
                        # Propagate definition changes to installed copy
                        _propagate_to_installed(manager, name, body_json)
                        logger.info(f"Updated bundled agent definition: {name}")
                        result["updated"] += 1
                    except Exception as e:
                        logger.error(f"Failed to update bundled agent '{name}': {e}")
                        result["errors"].append(f"Failed to update '{name}': {e}")
                else:
                    logger.debug(f"Agent definition '{name}' already up to date, skipping")
                    result["skipped"] += 1
                continue

            # Create the agent definition in workflow_definitions
            manager.create(
                name=name,
                definition_json=body_json,
                workflow_type="agent",
                description=body.description,
                source="template",
                enabled=body.enabled,
                tags=["gobby"],
            )
            logger.info(f"Synced bundled agent definition: {name}")
            result["synced"] += 1

        except Exception as e:
            error_msg = f"Failed to sync agent definition '{yaml_file}': {e}"
            logger.error(error_msg)
            result["errors"].append(error_msg)

    # Ensure all template/installed agents have the "gobby" tag
    _ensure_gobby_tag_on_installed(manager)

    total = result["synced"] + result["updated"] + result["skipped"]
    logger.info(
        f"Agent definition sync complete: {result['synced']} synced, "
        f"{result['updated']} updated, {result['skipped']} skipped, {total} total"
    )

    return result


def _propagate_to_installed(
    manager: LocalWorkflowDefinitionManager,
    agent_name: str,
    definition_json: str,
) -> None:
    """Propagate definition_json changes from a template to its installed copy.

    Preserves the installed copy's enabled state.
    """
    from gobby.storage.workflow_definitions import WorkflowDefinitionRow

    installed_row = manager.db.fetchone(
        "SELECT * FROM workflow_definitions "
        "WHERE name = ? AND source = 'installed' AND deleted_at IS NULL",
        (agent_name,),
    )
    if installed_row:
        installed = WorkflowDefinitionRow.from_row(installed_row)
        if installed.definition_json != definition_json:
            manager.update(
                installed.id,
                definition_json=definition_json,
            )
            logger.info(f"Propagated agent definition change to installed copy: {agent_name}")


def _ensure_gobby_tag_on_installed(
    manager: LocalWorkflowDefinitionManager,
) -> None:
    """Ensure all template/installed agent rows have the 'gobby' tag."""
    rows = manager.list_all(workflow_type="agent", include_deleted=False)
    for row in rows:
        if row.source in ("template", "installed"):
            tags = row.tags or []
            if "gobby" not in tags:
                manager.update(row.id, tags=[*tags, "gobby"])
