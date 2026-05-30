"""Agent definition synchronization for bundled agents.

Single-row model: templates live on disk only. The DB holds installed rows
directly. Installed rows are overwritten when the template changes
(preserving the user's enabled toggle). Soft-deleted rows are restored only
when a managed bundled definition reappears with different content.
"""

import logging
from pathlib import Path
from typing import Any

import yaml

from gobby.agents.step_workflow import register_agent_step_workflow
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sql_dialect import json_array_contains_condition
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager, WorkflowDefinitionRow
from gobby.utils.json_helpers import json_equal
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


def _is_sync_managed_bundled_agent(existing: WorkflowDefinitionRow) -> bool:
    """Return whether an existing row is safe for bundled agent sync to own."""
    return (
        existing.project_id is None
        and existing.source in {"installed", "template"}
        and "gobby" in (existing.tags or [])
    )


def _definition_json_equal(existing_json: Any, desired_json: str) -> bool:
    """Compare definition JSON semantically across text and Postgres JSONB formats."""
    return json_equal(existing_json, desired_json)


def _build_agent_update_fields(
    existing: WorkflowDefinitionRow,
    *,
    body: AgentDefinitionBody,
    body_json: str,
    force_enable: bool = False,
    restore: bool = False,
) -> dict[str, Any]:
    """Build changed fields for a managed bundled agent row."""
    fields: dict[str, Any] = {}
    if not _definition_json_equal(existing.definition_json, body_json):
        fields["definition_json"] = body_json
        fields["description"] = body.description
    if existing.source != "installed":
        fields["source"] = "installed"
        fields["tags"] = ["gobby"]
        fields["enabled"] = body.enabled
    elif force_enable or restore:
        fields["enabled"] = body.enabled
    return fields


def get_bundled_agents_path() -> Path:
    """Get the path to bundled agents directory.

    Returns:
        Path to src/gobby/install/shared/workflows/agents/
    """
    from gobby.paths import get_install_dir

    return get_install_dir() / "shared" / "workflows" / "agents"


def _refresh_step_workflow(body: AgentDefinitionBody, db: HubDatabase) -> None:
    """Refresh the generated step workflow row for agents with inline steps."""
    if body.steps:
        register_agent_step_workflow(body, db)


def sync_bundled_agents(db: HubDatabase) -> dict[str, Any]:
    """Sync bundled agent definitions from install/shared/workflows/agents/ to the database.

    Creates installed rows directly from template files. Installed rows are
    overwritten when the template changes (preserving the user's enabled toggle,
    except for legacy disabled discovery placeholders that are upgraded to real
    enabled agents).

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

            raw_name = data.get("name")
            stripped_name = raw_name.strip() if isinstance(raw_name, str) else ""
            name = stripped_name or yaml_file.stem
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
                    if _is_sync_managed_bundled_agent(existing) and not _definition_json_equal(
                        existing.definition_json, body_json
                    ):
                        with db.transaction():
                            manager.restore(existing.id)
                            manager.update(
                                existing.id,
                                **_build_agent_update_fields(
                                    existing,
                                    body=body,
                                    body_json=body_json,
                                    restore=True,
                                ),
                            )
                        _refresh_step_workflow(body, db)
                        result["updated"] += 1
                        continue
                    result["skipped"] += 1
                    continue

                force_enable = _is_legacy_discovery_placeholder(
                    name,
                    existing.definition_json,
                    existing.enabled,
                )
                if _is_sync_managed_bundled_agent(existing):
                    update_fields = _build_agent_update_fields(
                        existing,
                        body=body,
                        body_json=body_json,
                        force_enable=force_enable,
                    )
                    if update_fields:
                        manager.update(existing.id, **update_fields)
                        _refresh_step_workflow(body, db)
                        result["updated"] += 1
                        logger.debug(
                            "Updated bundled agent definition %s (%s)",
                            existing.id,
                            existing.description or body.description,
                        )
                        continue

                if _is_sync_managed_bundled_agent(existing):
                    _refresh_step_workflow(body, db)
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
            _refresh_step_workflow(body, db)
            logger.info(f"Synced bundled agent definition: {name}")
            result["synced"] += 1

        except Exception as e:
            error_msg = f"Failed to sync agent definition '{yaml_file}': {e}"
            logger.error(error_msg)
            result["errors"].append(error_msg)

    # Orphan cleanup: soft-delete agent rows whose YAML was removed.
    # Only touch gobby-tagged agent rows.
    tag_condition, tag_params = json_array_contains_condition(db, "tags", "gobby")
    orphan_rows = db.fetchall(
        "SELECT id, name FROM workflow_definitions "
        "WHERE workflow_type = 'agent' "
        f"AND {tag_condition} AND deleted_at IS NULL",
        tag_params,
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
