"""
MCP tools for agent definition CRUD operations.

Wraps LocalWorkflowDefinitionManager with workflow_type='agent' filtering.
Provides list, get, toggle, create, and delete operations for agent definitions.
"""

import json
import logging
from typing import Any

from gobby.storage.workflow_definitions import (
    LocalWorkflowDefinitionManager,
    WorkflowDefinitionRow,
)
from gobby.workflows.definitions import AgentDefinitionBody

logger = logging.getLogger(__name__)


def _agent_summary(row: WorkflowDefinitionRow) -> dict[str, Any]:
    """Build a summary dict for an agent definition row."""
    body = json.loads(row.definition_json)
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "provider": body.get("provider"),
        "mode": body.get("mode"),
        "model": body.get("model"),
        "isolation": body.get("isolation"),
        "has_steps": bool(body.get("steps")),
        "step_count": len(body.get("steps") or []),
        "enabled": row.enabled,
        "source": row.source,
        "project_id": row.project_id,
    }


def _agent_detail(row: WorkflowDefinitionRow) -> dict[str, Any]:
    """Build a detailed dict for an agent definition row, including full definition."""
    body = json.loads(row.definition_json)
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "provider": body.get("provider"),
        "model": body.get("model"),
        "mode": body.get("mode"),
        "isolation": body.get("isolation"),
        "base_branch": body.get("base_branch"),
        "timeout": body.get("timeout"),
        "max_turns": body.get("max_turns"),
        "role": body.get("role"),
        "goal": body.get("goal"),
        "personality": body.get("personality"),
        "instructions": body.get("instructions"),
        "workflows": body.get("workflows"),
        "steps": body.get("steps"),
        "step_variables": body.get("step_variables"),
        "exit_condition": body.get("exit_condition"),
        "enabled": row.enabled,
        "source": row.source,
        "project_id": row.project_id,
    }


def list_agent_definitions(
    def_manager: LocalWorkflowDefinitionManager,
    enabled: bool | None = None,
    project_id: str | None = None,
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
    rows = def_manager.list_all(workflow_type="agent", enabled=enabled, project_id=project_id)
    agents = [_agent_summary(r) for r in rows]
    return {"success": True, "agents": agents, "count": len(agents)}


def get_agent_definition(
    def_manager: LocalWorkflowDefinitionManager,
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
    row = def_manager.get_by_name(name) or def_manager.get_by_name(name, include_templates=True)
    if row is None or row.workflow_type != "agent":
        return {"success": False, "error": f"Agent definition '{name}' not found"}

    try:
        body = json.loads(row.definition_json)
        if "name" not in body:
            body["name"] = row.name
        # Validate
        AgentDefinitionBody.model_validate(body)
    except Exception as e:
        return {"success": False, "error": f"Failed to parse agent definition: {e}"}

    detail = _agent_detail(row)
    # Normalize provider for display
    if detail.get("provider") in (None, "inherit"):
        detail["provider"] = "claude"

    return {"success": True, "agent": detail}


def create_agent_definition(
    def_manager: LocalWorkflowDefinitionManager,
    name: str,
    definition: dict[str, Any],
) -> dict[str, Any]:
    """
    Create a new agent definition.

    Validates the definition with AgentDefinitionBody before inserting.

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
        AgentDefinitionBody.model_validate(definition)
    except Exception as e:
        return {"success": False, "error": f"Validation failed: {e}"}

    # Check for duplicate name (including templates)
    existing = def_manager.get_by_name(name) or def_manager.get_by_name(
        name, include_templates=True
    )
    if existing is not None:
        return {"success": False, "error": f"Agent definition '{name}' already exists"}

    row = def_manager.create(
        name=name,
        definition_json=json.dumps(definition),
        workflow_type="agent",
        description=definition.get("description"),
        enabled=definition.get("enabled", True),
        source="installed",
    )
    logger.info("Created agent definition '%s' (id=%s)", name, row.id)

    return {"success": True, "agent": _agent_detail(row)}


def toggle_agent_definition(
    def_manager: LocalWorkflowDefinitionManager,
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
    row = def_manager.get_by_name(name) or def_manager.get_by_name(name, include_templates=True)
    if row is None or row.workflow_type != "agent":
        return {"success": False, "error": f"Agent definition '{name}' not found"}

    updated = def_manager.update(row.id, enabled=enabled)
    logger.info("Toggled agent definition '%s' enabled=%s", name, enabled)

    return {"success": True, "agent": _agent_detail(updated)}


def delete_agent_definition(
    def_manager: LocalWorkflowDefinitionManager,
    name: str,
    force: bool = False,
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
    row = def_manager.get_by_name(name) or def_manager.get_by_name(name, include_templates=True)
    if row is None or row.workflow_type != "agent":
        return {"success": False, "error": f"Agent definition '{name}' not found"}

    if row.source in ("bundled", "template") and not force:
        return {
            "success": False,
            "error": (
                f"Agent definition '{name}' is a template and will be re-created on restart. "
                "Use force=True to delete anyway."
            ),
        }

    deleted = def_manager.delete(row.id)
    if not deleted:
        return {"success": False, "error": f"Failed to delete agent definition '{name}'"}

    logger.info("Deleted agent definition '%s' (id=%s)", name, row.id)
    return {"success": True, "deleted": {"id": row.id, "name": row.name}}


def update_agent_rules(
    def_manager: LocalWorkflowDefinitionManager,
    name: str,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> dict[str, Any]:
    """
    Add or remove rules from an agent definition's workflows.rules list.

    Args:
        def_manager: Definition storage manager
        name: Agent name
        add: Rule names to add
        remove: Rule names to remove

    Returns:
        Dict with success and updated rules list
    """
    row = def_manager.get_by_name(name) or def_manager.get_by_name(name, include_templates=True)
    if row is None or row.workflow_type != "agent":
        return {"success": False, "error": f"Agent definition '{name}' not found"}

    body = json.loads(row.definition_json)
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

    def_manager.update(row.id, definition_json=json.dumps(body))
    logger.info("Updated rules for agent '%s': %s", name, rules)

    return {"success": True, "rules": rules}


def update_agent_variables(
    def_manager: LocalWorkflowDefinitionManager,
    name: str,
    set_vars: dict[str, Any] | None = None,
    remove: list[str] | None = None,
) -> dict[str, Any]:
    """
    Set or remove variables from an agent definition's workflows.variables dict.

    Args:
        def_manager: Definition storage manager
        name: Agent name
        set_vars: Variables to set (key-value pairs)
        remove: Variable keys to remove

    Returns:
        Dict with success and updated variables dict
    """
    row = def_manager.get_by_name(name) or def_manager.get_by_name(name, include_templates=True)
    if row is None or row.workflow_type != "agent":
        return {"success": False, "error": f"Agent definition '{name}' not found"}

    body = json.loads(row.definition_json)
    workflows = body.get("workflows", {})
    variables: dict[str, Any] = dict(workflows.get("variables", {}))

    if remove:
        for key in remove:
            variables.pop(key, None)
    if set_vars:
        variables.update(set_vars)

    workflows["variables"] = variables
    body["workflows"] = workflows

    def_manager.update(row.id, definition_json=json.dumps(body))
    logger.info("Updated variables for agent '%s': %s", name, list(variables.keys()))

    return {"success": True, "variables": variables}


def update_agent_steps(
    def_manager: LocalWorkflowDefinitionManager,
    name: str,
    steps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Replace an agent's steps entirely.

    Args:
        def_manager: Definition storage manager
        name: Agent name
        steps: New steps list (or None to clear)

    Returns:
        Dict with success and updated steps info
    """
    row = def_manager.get_by_name(name) or def_manager.get_by_name(name, include_templates=True)
    if row is None or row.workflow_type != "agent":
        return {"success": False, "error": f"Agent definition '{name}' not found"}

    body = json.loads(row.definition_json)
    body["steps"] = steps

    # Validate the full model
    try:
        AgentDefinitionBody.model_validate(body)
    except Exception as e:
        return {"success": False, "error": f"Validation failed: {e}"}

    def_manager.update(row.id, definition_json=json.dumps(body))
    logger.info("Updated steps for agent '%s': %d steps", name, len(steps or []))

    return {"success": True, "steps": steps, "step_count": len(steps or [])}
