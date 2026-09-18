"""Agent definition synchronization for bundled agents.

Single-row model: templates live on disk only. The DB holds installed rows
directly. Installed rows are overwritten when the template changes
(preserving the user's enabled toggle). The orphan sweep marks the rows it
removes with ``SYNC_ORPHAN_TAG``, so a returning template restores them even
when their content is unchanged; a deliberate deletion carries no marker and
stays sticky across syncs.
"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from gobby.storage.definitions import AgentDefinitionManager, AgentDefinitionRow
from gobby.storage.definitions.agents import SYNC_ORPHAN_TAG
from gobby.storage.definitions.agents import parent_body as agent_parent_body
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sql_dialect import json_array_contains_condition
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

# One-time repair: the sweep removed these rows in one batch on 2026-09-06
# before it recorded its origin, so they carry no marker and no sync can tell
# them apart from a deliberate deletion. The cutoff bounds the repair to that
# batch; every sweep deletion since marks itself.
_PRE_MARKER_SWEPT_AGENTS = frozenset(
    {
        "analyst",
        "researcher",
        "architect",
        "product-manager",
    }
)
_PRE_MARKER_SWEEP_CUTOFF = datetime(2026, 9, 7, tzinfo=UTC)


def _is_legacy_discovery_placeholder(name: str, definition_json: str, enabled: bool) -> bool:
    """Return True for bundled disabled discovery placeholders that must become live."""
    if name not in _DISCOVERY_PLACEHOLDER_AGENTS or enabled:
        return False
    return "PLACEHOLDER" in definition_json or "placeholder_agent:" in definition_json


def _is_sync_managed_bundled_agent(existing: AgentDefinitionRow) -> bool:
    """Return whether an existing row is safe for bundled agent sync to own."""
    return (
        existing.project_id is None
        and existing.source == "installed"
        and "gobby" in (existing.tags or [])
    )


def _definition_json_equal(existing_json: Any, desired_json: Any) -> bool:
    """Compare definition JSON semantically across text and Postgres JSONB formats."""
    return json_equal(existing_json, desired_json)


def _was_swept_before_marker(existing: AgentDefinitionRow) -> bool:
    """Return whether this row predates the sweep marker and was swept away."""
    return (
        existing.name in _PRE_MARKER_SWEPT_AGENTS
        and existing.deleted_at is not None
        and existing.deleted_at < _PRE_MARKER_SWEEP_CUTOFF
    )


def _should_restore_swept_row(existing: AgentDefinitionRow, parent_body: dict[str, Any]) -> bool:
    """Return whether a soft-deleted managed row is a removal sync may undo."""
    if SYNC_ORPHAN_TAG in (existing.tags or []):
        return True
    if not _definition_json_equal(agent_parent_body(existing.definition_json), parent_body):
        return True
    return _was_swept_before_marker(existing)


def _build_agent_update_fields(
    existing: AgentDefinitionRow,
    *,
    body: AgentDefinitionBody,
    parent_body: dict[str, Any],
    force_enable: bool = False,
    restore: bool = False,
) -> dict[str, Any]:
    """Build changed fields for a managed bundled agent row."""
    fields: dict[str, Any] = {}
    existing_parent = agent_parent_body(existing.definition_json)
    if not _definition_json_equal(existing_parent, parent_body):
        fields["definition_json"] = parent_body
        fields["description"] = body.description
    if existing.source != "installed":
        fields["tags"] = ["gobby"]
        fields["enabled"] = body.enabled
    elif force_enable or restore:
        fields["enabled"] = body.enabled
    elif not existing.enabled_pinned and existing.enabled != body.enabled:
        fields["enabled"] = body.enabled
    return fields


def get_bundled_agents_path() -> Path:
    """Get the path to bundled agents directory.

    Returns:
        Path to src/gobby/install/shared/workflows/agents/
    """
    from gobby.paths import get_install_dir

    return get_install_dir() / "shared" / "workflows" / "agents"


def sync_bundled_agents(db: HubDatabase) -> dict[str, Any]:
    """Sync bundled agent definitions from install/shared/workflows/agents/ to the database.

    Creates installed rows directly from template files. Installed rows are
    overwritten when the template changes (preserving the user's enabled toggle,
    except for legacy disabled discovery placeholders that are upgraded to real
    enabled agents). Rows the orphan sweep removed are restored once their
    template returns; deliberately deleted rows stay deleted.

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
        "shadowed": 0,
        "errors": [],
    }

    if not agents_path.exists():
        logger.warning("Bundled agents path not found: %s", agents_path)
        result["errors"].append(f"Agents path not found: {agents_path}")
        return result

    manager = AgentDefinitionManager(db)
    on_disk: set[str] = set()

    for yaml_file in sorted(agents_path.glob("*.yaml")):
        try:
            raw_content = yaml_file.read_text(encoding="utf-8")
            data = yaml.safe_load(raw_content)

            if not isinstance(data, dict):
                logger.warning("Skipping non-dict YAML file: %s", yaml_file)
                continue

            raw_name = data.get("name")
            stripped_name = raw_name.strip() if isinstance(raw_name, str) else ""
            name = stripped_name or yaml_file.stem
            on_disk.add(name)
            data["name"] = name

            body = AgentDefinitionBody.model_validate(data)
            dumped = body.model_dump(mode="json")
            parent_body = agent_parent_body(dumped)
            step_workflow = dumped.get("step_workflow")
            existing = manager.get_by_name(name, include_deleted=True)
            existing_json = ""
            if existing is not None:
                existing_json = (
                    existing.definition_json
                    if isinstance(existing.definition_json, str)
                    else json.dumps(existing.definition_json)
                )

            if existing is not None:
                if not _is_sync_managed_bundled_agent(existing):
                    state = "soft-deleted " if existing.deleted_at is not None else ""
                    error_msg = (
                        f"Bundled agent '{name}' is shadowed by an unmanaged {state}row "
                        f"(source={existing.source}, project_id={existing.project_id}, "
                        f"tags={existing.tags}); bundled template changes will not sync. "
                        "Rename or delete the row to restore sync management."
                    )
                    logger.error(error_msg)
                    result["success"] = False
                    result["errors"].append(error_msg)
                    result["shadowed"] += 1
                    result["skipped"] += 1
                    continue

                if existing.deleted_at is not None:
                    if _should_restore_swept_row(existing, parent_body):
                        manager.upsert_from_sync(
                            name,
                            parent_body,
                            step_workflow,
                            source="installed",
                            enabled=body.enabled,
                            tags=["gobby"],
                            description=body.description,
                            restore=True,
                        )
                        result["updated"] += 1
                        continue
                    result["skipped"] += 1
                    continue

                force_enable = _is_legacy_discovery_placeholder(
                    name,
                    existing_json,
                    existing.enabled,
                )
                update_fields = _build_agent_update_fields(
                    existing,
                    body=body,
                    parent_body=parent_body,
                    force_enable=force_enable,
                )
                if update_fields:
                    manager.upsert_from_sync(
                        name,
                        parent_body,
                        step_workflow,
                        source="installed",
                        enabled=update_fields.get("enabled", existing.enabled),
                        tags=update_fields.get("tags", existing.tags),
                        description=update_fields.get("description", existing.description),
                    )
                    result["updated"] += 1
                    logger.debug(
                        "Updated bundled agent definition %s (%s)",
                        existing.id,
                        existing.description or body.description,
                    )
                    continue

                if step_workflow is not None:
                    manager.set_step_workflow(existing.id, step_workflow)
                result["skipped"] += 1
                continue

            manager.upsert_with_steps(
                name,
                parent_body,
                step_workflow,
                source="installed",
                enabled=body.enabled,
                tags=["gobby"],
                description=body.description,
            )
            logger.debug("Synced bundled agent definition: %s", name)
            result["synced"] += 1

        except Exception as e:
            error_msg = f"Failed to sync agent definition '{yaml_file}': {e}"
            logger.error(error_msg)
            result["errors"].append(error_msg)

    tag_condition, tag_params = json_array_contains_condition(db, "tags", "gobby")
    orphan_rows = db.fetchall(
        "SELECT id, project_id, name, source, tags, deleted_at FROM agent_definitions "
        f"WHERE {tag_condition} AND deleted_at IS NULL",
        tag_params,
    )
    result["orphaned"] = 0
    for row in orphan_rows:
        existing = manager.get(str(row["id"]))
        if existing.name not in on_disk and _is_sync_managed_bundled_agent(existing):
            manager.delete(existing.id, sync_orphan=True)
            logger.debug("Soft-deleted orphaned bundled agent: %s", existing.name)
            result["orphaned"] += 1

    total = result["synced"] + result["updated"] + result["skipped"]
    logger.debug(
        "Agent definition sync complete: %s synced, %s updated, %s skipped, %s orphaned, %s total",
        result["synced"],
        result["updated"],
        result["skipped"],
        result.get("orphaned", 0),
        total,
    )

    return result
