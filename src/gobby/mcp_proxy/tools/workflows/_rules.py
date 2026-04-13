"""
MCP tools for rule CRUD operations.

Wraps LocalWorkflowDefinitionManager with workflow_type='rule' filtering.
Provides list, get, toggle, create, and delete operations for standalone rules.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from gobby.storage.workflow_definitions import (
    LocalWorkflowDefinitionManager,
    WorkflowDefinitionRow,
)
from gobby.workflows.definitions import RuleDefinitionBody

logger = logging.getLogger(__name__)


def _rule_brief(row: WorkflowDefinitionRow) -> dict[str, Any]:
    """Build a minimal dict for a rule row — just enough to identify and filter."""
    body = json.loads(row.definition_json)
    return {
        "name": row.name,
        "event": body.get("event"),
        "group": body.get("group"),
        "enabled": row.enabled,
    }


def _rule_summary(row: WorkflowDefinitionRow) -> dict[str, Any]:
    """Build a summary dict for a rule row, including parsed definition fields."""
    from gobby.workflows.template_hashes import get_template_hash_cache

    body = json.loads(row.definition_json)
    cache = get_template_hash_cache()
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "event": body.get("event"),
        "effects": body.get("effects") or ([body["effect"]] if body.get("effect") else None),
        "group": body.get("group"),
        "when": body.get("when"),
        "agent_scope": body.get("agent_scope"),
        "enabled": row.enabled,
        "priority": row.priority,
        "source": row.source,
        "tags": row.tags,
        "project_id": row.project_id,
        "has_template_update": cache.has_drift(row),
    }


def _rule_detail(row: WorkflowDefinitionRow) -> dict[str, Any]:
    """Build a detailed dict for a rule row, including full definition."""
    body = json.loads(row.definition_json)
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "event": body.get("event"),
        "group": body.get("group"),
        "when": body.get("when"),
        "match": body.get("match"),
        "effects": body.get("effects") or ([body["effect"]] if body.get("effect") else None),
        "agent_scope": body.get("agent_scope"),
        "enabled": row.enabled,
        "priority": row.priority,
        "source": row.source,
        "tags": row.tags,
        "project_id": row.project_id,
    }


def list_rules(
    def_manager: LocalWorkflowDefinitionManager,
    event: str | None = None,
    group: str | None = None,
    enabled: bool | None = None,
    project_id: str | None = None,
    brief: bool = False,
) -> dict[str, Any]:
    """
    List rules with optional filters.

    Dispatches to event/group-specific queries when those filters are provided,
    otherwise uses list_all with workflow_type='rule'.

    Args:
        def_manager: Definition storage manager
        event: Filter by event type (e.g. 'before_tool', 'stop')
        group: Filter by group name
        enabled: Filter by enabled status
        project_id: Filter by project ID
        brief: If True, return minimal fields (name, event, group, enabled)

    Returns:
        Dict with success, rules list, and count
    """
    if event:
        rows = def_manager.list_rules_by_event(event, project_id=project_id, enabled=enabled)
    elif group:
        rows = def_manager.list_rules_by_group(group, project_id=project_id, enabled=enabled)
    else:
        rows = def_manager.list_all(workflow_type="rule", enabled=enabled, project_id=project_id)

    formatter = _rule_brief if brief else _rule_summary
    rules = [formatter(r) for r in rows]
    return {"success": True, "rules": rules, "count": len(rules)}


def get_rule(
    def_manager: LocalWorkflowDefinitionManager,
    name: str,
) -> dict[str, Any]:
    """
    Get a rule by name.

    Args:
        def_manager: Definition storage manager
        name: Rule name

    Returns:
        Dict with success and full rule detail, or error if not found
    """
    row = def_manager.get_by_name(name)
    if row is None or row.workflow_type != "rule":
        return {"success": False, "error": f"Rule '{name}' not found"}

    return {"success": True, "rule": _rule_detail(row)}


def toggle_rule(
    def_manager: LocalWorkflowDefinitionManager,
    name: str,
    enabled: bool,
) -> dict[str, Any]:
    """
    Toggle a rule's enabled state.

    Args:
        def_manager: Definition storage manager
        name: Rule name
        enabled: New enabled state

    Returns:
        Dict with success and updated rule, or error if not found
    """
    row = def_manager.get_by_name(name)
    if row is None or row.workflow_type != "rule":
        return {"success": False, "error": f"Rule '{name}' not found"}

    updated = def_manager.update(row.id, enabled=enabled)
    logger.info(f"Toggled rule '{name}' enabled={enabled}")

    return {"success": True, "rule": _rule_detail(updated)}


def update_rule(
    def_manager: LocalWorkflowDefinitionManager,
    name: str,
    *,
    definition: dict[str, Any] | None = None,
    description: str | None = None,
    enabled: bool | None = None,
    priority: int | None = None,
    tags: list[str] | None = None,
    project_path: Path | None = None,
    make_global_template: bool = False,
) -> dict[str, Any]:
    """
    Update fields on an existing standalone rule.

    Mirrors the HTTP PUT /api/rules/{name} endpoint: pass any subset of
    definition / description / enabled / priority / tags. When ``definition``
    is provided it replaces the rule body and is validated with
    ``RuleDefinitionBody``; row-level metadata embedded in the body is
    hoisted onto the row unless an explicit value was also passed.

    Args:
        def_manager: Definition storage manager
        name: Rule name
        definition: Full replacement rule body (validated)
        description: New description
        enabled: New enabled state
        priority: New priority
        tags: New tags
        project_path: Project root for auto-export
        make_global_template: If True, export to ~/.gobby/workflows/ instead

    Returns:
        Dict with success and updated rule, or error if not found / invalid
    """
    row = def_manager.get_by_name(name)
    if row is None or row.workflow_type != "rule":
        return {"success": False, "error": f"Rule '{name}' not found"}

    fields: dict[str, Any] = {}
    if description is not None:
        fields["description"] = description
    if enabled is not None:
        fields["enabled"] = enabled
    if priority is not None:
        fields["priority"] = priority
    if tags is not None:
        fields["tags"] = tags

    if definition is not None:
        try:
            RuleDefinitionBody.model_validate(definition)
        except ValidationError as e:
            return {"success": False, "error": f"Invalid rule definition: {e}"}

        # Shallow-copy to avoid mutating the caller's dict via pop().
        local_def = dict(definition)
        for key in ("description", "enabled", "priority", "tags"):
            if key not in fields and key in local_def:
                fields[key] = local_def.pop(key)

        local_def.pop("name", None)
        fields["definition_json"] = json.dumps(local_def)

    if not fields:
        return {"success": False, "error": "No fields to update"}

    updated = def_manager.update(row.id, **fields)
    logger.info(f"Updated rule '{name}' (fields={list(fields)})")

    try:
        from gobby.mcp_proxy.tools.workflows._auto_export import auto_export_definition

        auto_export_definition(updated, project_path, make_global=make_global_template)
    except Exception as e:
        logger.warning(f"Failed to auto-export updated rule '{name}': {e}")

    return {"success": True, "rule": _rule_detail(updated)}


def create_rule(
    def_manager: LocalWorkflowDefinitionManager,
    name: str,
    definition: dict[str, Any],
    *,
    project_path: Path | None = None,
    make_global_template: bool = False,
) -> dict[str, Any]:
    """
    Create a new rule.

    Validates the definition with RuleDefinitionBody before inserting.
    Auto-exports to YAML for persistence (unless in dev mode).

    Args:
        def_manager: Definition storage manager
        name: Rule name (must be unique)
        definition: Rule definition dict (event, effect, optional when/group/match)
        project_path: Project root for auto-export
        make_global_template: If True, export to ~/.gobby/workflows/ instead

    Returns:
        Dict with success and created rule, or error
    """
    # Validate with Pydantic
    try:
        RuleDefinitionBody.model_validate(definition)
    except ValidationError as e:
        return {"success": False, "error": f"Validation failed: {e}"}

    # Name collision check: reject user rules that shadow gobby templates
    from gobby.mcp_proxy.tools.workflows._auto_export import has_gobby_name_collision

    if has_gobby_name_collision(def_manager.db, name):
        return {
            "success": False,
            "error": f"Rule '{name}' conflicts with a bundled gobby template. Choose a different name.",
        }

    # Check for duplicate name
    existing = def_manager.get_by_name(name)
    if existing is not None:
        return {"success": False, "error": f"Rule '{name}' already exists"}

    # Hard-delete any soft-deleted rule that would block the UNIQUE constraint
    deleted_row = def_manager.get_by_name(name, include_deleted=True)
    if deleted_row is not None and deleted_row.deleted_at and deleted_row.workflow_type == "rule":
        def_manager.hard_delete(deleted_row.id)

    tags = definition.get("tags") or ["user"]

    row = def_manager.create(
        name=name,
        definition_json=json.dumps(definition),
        workflow_type="rule",
        enabled=True,
        source="installed",
        tags=tags,
    )
    logger.info(f"Created rule '{name}' (id={row.id})")

    # Auto-export to YAML for persistence
    try:
        from gobby.mcp_proxy.tools.workflows._auto_export import auto_export_definition

        auto_export_definition(row, project_path, make_global=make_global_template)
    except Exception as e:
        logger.warning(f"Failed to auto-export rule '{name}': {e}")

    return {"success": True, "rule": _rule_detail(row)}


def delete_rule(
    def_manager: LocalWorkflowDefinitionManager,
    name: str,
    force: bool = False,
    *,
    project_path: Path | None = None,
) -> dict[str, Any]:
    """
    Delete a rule by name (soft-delete).

    Bundled rules are protected unless force=True.
    Also removes the YAML template file if it exists.

    Args:
        def_manager: Definition storage manager
        name: Rule name
        force: Override bundled protection
        project_path: Project root for YAML cleanup

    Returns:
        Dict with success, or error if not found/protected
    """
    row = def_manager.get_by_name(name)
    if row is None or row.workflow_type != "rule":
        return {"success": False, "error": f"Rule '{name}' not found"}

    if "gobby" in (row.tags or []) and not force:
        return {
            "success": False,
            "error": (
                f"Rule '{name}' is bundled and will be re-created on restart. "
                "Use force=True to delete anyway."
            ),
        }

    deleted = def_manager.delete(row.id)
    if not deleted:
        return {"success": False, "error": f"Failed to delete rule '{name}'"}

    # Remove YAML template file if it exists
    try:
        from gobby.mcp_proxy.tools.workflows._auto_export import auto_delete_definition

        is_user = bool(row.tags and "user" in row.tags)
        auto_delete_definition(
            name,
            "rule",
            project_path,
            delete_global=is_user,
        )
    except Exception as e:
        logger.warning(f"Failed to delete rule template '{name}': {e}")

    logger.info(f"Deleted rule '{name}' (id={row.id})")
    return {"success": True, "deleted": {"id": row.id, "name": row.name}}
