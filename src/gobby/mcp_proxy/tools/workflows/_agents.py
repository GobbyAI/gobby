"""
MCP tools for agent definition CRUD operations.

Wraps AgentDefinitionManager with a nested step_workflow surface.
Provides list, get, toggle, create, and delete operations for agent definitions.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from gobby.storage.definitions import AgentDefinitionManager, AgentDefinitionRow
from gobby.workflows.definitions import AgentDefinitionBody, AgentStepWorkflowBody

logger = logging.getLogger(__name__)


def _row_body(row: AgentDefinitionRow) -> dict[str, Any]:
    raw = row.definition_json
    try:
        parsed: object = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    if isinstance(parsed, dict):
        return dict(parsed)
    return {}


def _export_row(row: AgentDefinitionRow) -> Any:
    return SimpleNamespace(
        name=row.name,
        tags=row.tags,
        definition_json=json.dumps(_row_body(row)),
    )


def _agent_summary(row: AgentDefinitionRow) -> dict[str, Any]:
    """Build a summary dict for an agent definition row."""
    body = _row_body(row)
    nested = body.get("step_workflow") or {}
    steps = nested.get("steps") if isinstance(nested, dict) else None
    if not isinstance(steps, list):
        steps = []
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "provider": body.get("provider"),
        "mode": body.get("mode"),
        "model": body.get("model"),
        "isolation": body.get("isolation"),
        "surfaces": body.get("surfaces", ["spawn"]),
        "has_steps": bool(steps),
        "step_count": len(steps),
        "enabled": row.enabled,
        "source": row.source,
        "project_id": row.project_id,
    }


def _agent_detail(row: AgentDefinitionRow) -> dict[str, Any]:
    """Build a detailed dict for an agent definition row, including full definition."""
    raw_body = _row_body(row)
    raw_body.setdefault("name", row.name)
    body = AgentDefinitionBody.model_validate(raw_body).model_dump(mode="json")
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "provider": body.get("provider"),
        "model": body.get("model"),
        "mode": raw_body.get("mode"),
        "isolation": body.get("isolation"),
        "surfaces": body.get("surfaces", ["spawn"]),
        "base_branch": body.get("base_branch"),
        "timeout": body.get("timeout"),
        "prompts": body.get("prompts"),
        "workflows": body.get("workflows"),
        "step_workflow": body.get("step_workflow"),
        "enabled": row.enabled,
        "source": row.source,
        "project_id": row.project_id,
    }


def list_agent_definitions(
    def_manager: AgentDefinitionManager,
    enabled: bool | None = None,
    project_id: str | None = None,
    surface_filter: str | None = None,
) -> dict[str, Any]:
    """
    List agent definitions with optional filters.

    Args:
        def_manager: Definition storage manager
        enabled: Filter by enabled status
        project_id: Filter by project ID

    Returns:
        Dict with success, agents list, and count
    """
    rows = def_manager.list_all(enabled=enabled, project_id=project_id)
    agents = [_agent_summary(r) for r in rows]
    if surface_filter:
        agents = [agent for agent in agents if surface_filter in agent.get("surfaces", ["spawn"])]
    return {"success": True, "agents": agents, "count": len(agents)}


def get_agent_definition(
    def_manager: AgentDefinitionManager,
    name: str,
) -> dict[str, Any]:
    """
    Get an agent definition by name via direct DB lookup.

    Args:
        def_manager: Definition storage manager
        name: Agent name

    Returns:
        Dict with success and full agent detail, or error if not found
    """
    row = def_manager.get_by_name(name)
    if row is None:
        return {"success": False, "error": f"Agent definition '{name}' not found"}

    try:
        body = _row_body(row)
        if "name" not in body:
            body["name"] = row.name
        AgentDefinitionBody.model_validate(body)
    except Exception as e:
        return {"success": False, "error": f"Failed to parse agent definition: {e}"}

    detail = _agent_detail(row)
    # Normalize provider for display
    if detail.get("provider") in (None, "inherit"):
        detail["provider"] = "claude"

    return {"success": True, "agent": detail}


def create_agent_definition(
    def_manager: AgentDefinitionManager,
    name: str,
    definition: dict[str, Any],
    *,
    project_path: Path | None = None,
    make_global_template: bool = False,
) -> dict[str, Any]:
    """
    Create a new agent definition.

    Validates the definition with AgentDefinitionBody before inserting.
    Auto-exports to YAML for persistence.

    Args:
        def_manager: Definition storage manager
        name: Agent name (must be unique)
        definition: Agent definition dict

    Returns:
        Dict with success and created agent, or error
    """
    # Ensure name is in definition for validation
    definition["name"] = name

    # Validate with Pydantic
    try:
        body = AgentDefinitionBody.model_validate(definition)
    except Exception as e:
        return {"success": False, "error": f"Validation failed: {e}"}
    validated_definition = body.model_dump(mode="json")

    # Check for duplicate name
    existing = def_manager.get_by_name(name)
    if existing is not None:
        return {"success": False, "error": f"Agent definition '{name}' already exists"}

    row = def_manager.upsert_with_steps(
        name,
        validated_definition,
        validated_definition.get("step_workflow"),
        description=validated_definition.get("description"),
        enabled=validated_definition.get("enabled", True),
        source="installed",
        tags=["user"],
    )
    logger.info("Created agent definition '%s' (id=%s)", name, row.id)

    # Auto-export to YAML for persistence
    try:
        from gobby.mcp_proxy.tools.workflows._auto_export import auto_export_definition

        auto_export_definition(
            _export_row(row), project_path, kind="agent", make_global=make_global_template
        )
    except Exception as e:
        logger.warning("Failed to auto-export agent '%s': %s", name, e)

    return {"success": True, "agent": _agent_detail(row)}


def toggle_agent_definition(
    def_manager: AgentDefinitionManager,
    name: str,
    enabled: bool,
) -> dict[str, Any]:
    """
    Toggle an agent definition's enabled state.

    Args:
        def_manager: Definition storage manager
        name: Agent name
        enabled: New enabled state

    Returns:
        Dict with success and updated agent, or error if not found
    """
    row = def_manager.get_by_name(name)
    if row is None:
        return {"success": False, "error": f"Agent definition '{name}' not found"}

    updated = def_manager.update(row.id, enabled=enabled)
    logger.info("Toggled agent definition '%s' enabled=%s", name, enabled)

    return {"success": True, "agent": _agent_detail(updated)}


def delete_agent_definition(
    def_manager: AgentDefinitionManager,
    name: str,
    force: bool = False,
    *,
    project_path: Path | None = None,
) -> dict[str, Any]:
    """
    Delete an agent definition by name (soft-delete).

    Template agents are protected unless force=True.

    Args:
        def_manager: Definition storage manager
        name: Agent name
        force: Override template protection

    Returns:
        Dict with success, or error if not found/protected
    """
    row = def_manager.get_by_name(name)
    if row is None:
        return {"success": False, "error": f"Agent definition '{name}' not found"}

    if "gobby" in (row.tags or []) and not force:
        return {
            "success": False,
            "error": (
                f"Agent definition '{name}' is bundled and will be re-created on restart. "
                "Use force=True to delete anyway."
            ),
        }

    deleted = def_manager.delete(row.id)
    if not deleted:
        return {"success": False, "error": f"Failed to delete agent definition '{name}'"}

    # Remove YAML template file if it exists
    try:
        from gobby.mcp_proxy.tools.workflows._auto_export import auto_delete_definition

        is_user = bool(row.tags and "user" in row.tags)
        auto_delete_definition(name, project_path, kind="agent", delete_global=is_user)
    except Exception as e:
        logger.warning("Failed to delete agent template '%s': %s", name, e)

    logger.info("Deleted agent definition '%s' (id=%s)", name, row.id)
    return {"success": True, "deleted": {"id": row.id, "name": row.name}}


def update_agent_rules(
    def_manager: AgentDefinitionManager,
    name: str,
    add: list[str] | None = None,
    remove: list[str] | None = None,
    *,
    project_path: Path | None = None,
    make_global_template: bool = False,
) -> dict[str, Any]:
    """
    Add or remove rules from an agent definition's workflows.rules list.

    Args:
        def_manager: Definition storage manager
        name: Agent name
        add: Rule names to add
        remove: Rule names to remove
        project_path: Project root for auto-export
        make_global_template: If True, export to ~/.gobby/workflows/ instead

    Returns:
        Dict with success and updated rules list
    """
    row = def_manager.get_by_name(name)
    if row is None:
        return {"success": False, "error": f"Agent definition '{name}' not found"}

    body = _row_body(row)
    workflows = body.get("workflows", {})
    rules: list[str] = list(workflows.get("rules", []))

    if remove:
        rules = [r for r in rules if r not in remove]
    if add:
        for rule in add:
            if rule not in rules:
                rules.append(rule)

    workflows["rules"] = rules
    body["workflows"] = workflows

    updated = def_manager.update(row.id, definition_json=json.dumps(body))
    logger.info("Updated rules for agent '%s': %s", name, rules)

    try:
        from gobby.mcp_proxy.tools.workflows._auto_export import auto_export_definition

        auto_export_definition(
            _export_row(updated), project_path, kind="agent", make_global=make_global_template
        )
    except Exception as e:
        logger.warning("Failed to auto-export agent '%s': %s", name, e)

    return {"success": True, "rules": rules}


def update_agent_variables(
    def_manager: AgentDefinitionManager,
    name: str,
    set_vars: dict[str, Any] | None = None,
    remove: list[str] | None = None,
    *,
    project_path: Path | None = None,
    make_global_template: bool = False,
) -> dict[str, Any]:
    """
    Set or remove variables from an agent definition's workflows.variables dict.

    Args:
        def_manager: Definition storage manager
        name: Agent name
        set_vars: Variables to set (key-value pairs)
        remove: Variable keys to remove
        project_path: Project root for auto-export
        make_global_template: If True, export to ~/.gobby/workflows/ instead

    Returns:
        Dict with success and updated variables dict
    """
    row = def_manager.get_by_name(name)
    if row is None:
        return {"success": False, "error": f"Agent definition '{name}' not found"}

    body = _row_body(row)
    workflows = body.get("workflows", {})
    variables: dict[str, Any] = dict(workflows.get("variables", {}))

    if remove:
        for key in remove:
            variables.pop(key, None)
    if set_vars:
        variables.update(set_vars)

    workflows["variables"] = variables
    body["workflows"] = workflows

    updated = def_manager.update(row.id, definition_json=json.dumps(body))
    logger.info("Updated variables for agent '%s': %s", name, list(variables.keys()))

    try:
        from gobby.mcp_proxy.tools.workflows._auto_export import auto_export_definition

        auto_export_definition(
            _export_row(updated), project_path, kind="agent", make_global=make_global_template
        )
    except Exception as e:
        logger.warning("Failed to auto-export agent '%s': %s", name, e)

    return {"success": True, "variables": variables}


def update_agent_step_workflow(
    def_manager: AgentDefinitionManager,
    name: str,
    step_workflow: dict[str, Any] | None = None,
    *,
    project_path: Path | None = None,
    make_global_template: bool = False,
) -> dict[str, Any]:
    """Replace an agent's nested step workflow, or clear it when None."""
    row = def_manager.get_by_name(name)
    if row is None:
        return {"success": False, "error": f"Agent definition '{name}' not found"}

    validated: dict[str, Any] | None = None
    if step_workflow is not None:
        try:
            validated = AgentStepWorkflowBody.model_validate(step_workflow).model_dump(mode="json")
        except Exception as e:
            return {"success": False, "error": f"Validation failed: {e}"}

    updated = def_manager.set_step_workflow(row.id, validated)
    steps = None if validated is None else validated.get("steps") or []
    logger.info(
        "Updated step_workflow for agent '%s': %s steps",
        name,
        0 if steps is None else len(steps),
    )

    try:
        from gobby.mcp_proxy.tools.workflows._auto_export import auto_export_definition

        auto_export_definition(
            _export_row(updated), project_path, kind="agent", make_global=make_global_template
        )
    except Exception as e:
        logger.warning("Failed to auto-export agent '%s': %s", name, e)

    return {
        "success": True,
        "step_workflow": validated,
        "step_count": 0 if steps is None else len(steps),
    }
