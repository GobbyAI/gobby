"""
CRUD tools for workflow/pipeline definitions stored in the database.

Provides create, update, delete, and export operations on the
workflow_definitions table via LocalWorkflowDefinitionManager.
"""

import json
import logging
from pathlib import Path
from typing import Any

import yaml

from gobby.storage.workflow_definitions import (
    LocalWorkflowDefinitionManager,
    WorkflowDefinitionRow,
)
from gobby.workflows.loader import WorkflowLoader

logger = logging.getLogger(__name__)


def _resolve_definition(
    def_manager: LocalWorkflowDefinitionManager,
    name: str | None = None,
    definition_id: str | None = None,
    include_deleted: bool = False,
) -> WorkflowDefinitionRow:
    """Resolve a definition by name or ID. Raises ValueError if not found."""
    if definition_id:
        return def_manager.get(definition_id, include_deleted=include_deleted)
    if name:
        row = def_manager.get_by_name(name, include_deleted=include_deleted)
        if row is None:
            raise ValueError(f"Workflow definition '{name}' not found")
        return row
    raise ValueError("Either 'name' or 'definition_id' is required")


def _validate_yaml(yaml_content: str) -> dict[str, Any]:
    """Parse and validate YAML content. Returns parsed data dict."""
    from gobby.workflows.definitions import PipelineDefinition, WorkflowDefinition

    data = yaml.safe_load(yaml_content)
    if not isinstance(data, dict) or "name" not in data:
        raise ValueError("Invalid YAML: must be a mapping with a 'name' field")

    yaml_type = data.get("type", "step")
    if yaml_type == "pipeline":
        PipelineDefinition.model_validate(data)
    else:
        WorkflowDefinition.model_validate(data)

    return data


def create_workflow_definition(
    def_manager: LocalWorkflowDefinitionManager,
    loader: WorkflowLoader,
    yaml_content: str,
    project_id: str | None = None,
    *,
    project_path: Path | None = None,
    make_global_template: bool = False,
) -> dict[str, Any]:
    """
    Create a workflow/pipeline definition from YAML content.

    Validates YAML with Pydantic models before inserting into the database.
    Checks for name conflicts first.

    Args:
        def_manager: Definition storage manager
        loader: WorkflowLoader (cache is cleared after creation)
        yaml_content: Full YAML definition content
        project_id: Optional project scope
        project_path: Project root for auto-export
        make_global_template: If True, export to ~/.gobby/workflows/ instead

    Returns:
        Dict with success status and created definition metadata
    """
    try:
        data = _validate_yaml(yaml_content)
    except yaml.YAMLError as e:
        return {"success": False, "error": f"YAML parse error: {e}"}
    except (ValueError, TypeError) as e:
        return {"success": False, "error": f"Validation failed: {e}"}

    name = data["name"]
    existing = def_manager.get_by_name(name, project_id=project_id)
    if existing:
        return {
            "success": False,
            "error": f"Definition '{name}' already exists (id={existing.id}). Use update_workflow to modify it.",
        }

    try:
        row = def_manager.import_from_yaml(yaml_content, project_id=project_id)
    except Exception as e:
        return {"success": False, "error": f"Import failed: {e}"}

    # Tag as user-created if no tags set
    if not row.tags:
        def_manager.update(row.id, tags=["user"])
        row = def_manager.get(row.id)

    loader.clear_cache()
    logger.info(f"Created workflow definition '{row.name}' (id={row.id})")

    # Auto-export to YAML for persistence
    try:
        from gobby.mcp_proxy.tools.workflows._auto_export import auto_export_definition

        auto_export_definition(row, project_path, make_global=make_global_template)
    except Exception as e:
        logger.warning(f"Failed to auto-export definition '{row.name}': {e}")

    return {
        "success": True,
        "definition": {
            "id": row.id,
            "name": row.name,
            "workflow_type": row.workflow_type,
            "description": row.description,
            "version": row.version,
            "enabled": row.enabled,
            "priority": row.priority,
            "source": row.source,
        },
    }


def update_workflow_definition(
    def_manager: LocalWorkflowDefinitionManager,
    loader: WorkflowLoader,
    name: str | None = None,
    definition_id: str | None = None,
    description: str | None = None,
    enabled: bool | None = None,
    priority: int | None = None,
    version: str | None = None,
    tags: list[str] | None = None,
    yaml_content: str | None = None,
    *,
    project_path: Path | None = None,
    make_global_template: bool = False,
) -> dict[str, Any]:
    """
    Update a workflow/pipeline definition by name or ID.

    Accepts individual field updates and/or full YAML replacement.
    YAML content is validated with Pydantic before applying.

    Args:
        def_manager: Definition storage manager
        loader: WorkflowLoader (cache is cleared after update)
        name: Resolve definition by name
        definition_id: Resolve definition by ID
        description: New description
        enabled: New enabled state
        priority: New priority value
        version: New version string
        tags: New tags list
        yaml_content: Full YAML replacement (validated before applying)
        project_path: Project root for auto-export
        make_global_template: If True, export to ~/.gobby/workflows/ instead

    Returns:
        Dict with success status and updated definition metadata
    """
    try:
        row = _resolve_definition(def_manager, name, definition_id)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    fields: dict[str, Any] = {}

    if yaml_content is not None:
        try:
            data = _validate_yaml(yaml_content)
        except Exception as e:
            return {"success": False, "error": f"YAML validation failed: {e}"}
        fields["definition_json"] = json.dumps(data)
        # Sync top-level metadata from YAML
        if "description" in data:
            fields["description"] = data["description"]
        _VALID_WORKFLOW_TYPES = {"rule", "variable", "agent", "pipeline"}
        yaml_type = data.get("type")
        if yaml_type in _VALID_WORKFLOW_TYPES:
            fields["workflow_type"] = yaml_type
        elif yaml_type is not None:
            return {
                "success": False,
                "error": f"Invalid type '{yaml_type}'. Valid types: {', '.join(sorted(_VALID_WORKFLOW_TYPES))}",
            }
        # If yaml_type is absent, preserve existing workflow_type
        if "version" in data:
            fields["version"] = str(data["version"])
        if "enabled" in data:
            fields["enabled"] = bool(data["enabled"])
        if "priority" in data:
            fields["priority"] = data["priority"]

    # Explicit field overrides take precedence over YAML-derived values
    if description is not None:
        fields["description"] = description
    if enabled is not None:
        fields["enabled"] = enabled
    if priority is not None:
        fields["priority"] = priority
    if version is not None:
        fields["version"] = version
    if tags is not None:
        fields["tags"] = tags

    if not fields:
        return {"success": False, "error": "No fields to update"}

    try:
        updated = def_manager.update(row.id, **fields)
    except Exception as e:
        return {"success": False, "error": f"Update failed: {e}"}

    loader.clear_cache()
    logger.info(f"Updated workflow definition '{updated.name}' (id={updated.id})")

    # Auto-export to YAML for persistence
    try:
        from gobby.mcp_proxy.tools.workflows._auto_export import auto_export_definition

        auto_export_definition(updated, project_path, make_global=make_global_template)
    except Exception as e:
        logger.warning(f"Failed to auto-export definition '{updated.name}': {e}")

    return {
        "success": True,
        "definition": {
            "id": updated.id,
            "name": updated.name,
            "workflow_type": updated.workflow_type,
            "description": updated.description,
            "version": updated.version,
            "enabled": updated.enabled,
            "priority": updated.priority,
            "tags": updated.tags,
        },
    }


def delete_workflow_definition(
    def_manager: LocalWorkflowDefinitionManager,
    loader: WorkflowLoader,
    name: str | None = None,
    definition_id: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """
    Delete a workflow/pipeline definition by name or ID.

    Template definitions (source='template') are protected unless force=True,
    since they'll be re-created on daemon restart.

    Args:
        def_manager: Definition storage manager
        loader: WorkflowLoader (cache is cleared after deletion)
        name: Resolve definition by name
        definition_id: Resolve definition by ID
        force: Override bundled protection

    Returns:
        Dict with success status
    """
    try:
        row = _resolve_definition(def_manager, name, definition_id)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    if "gobby" in (row.tags or []) and not force:
        return {
            "success": False,
            "error": (
                f"Definition '{row.name}' is bundled and will be re-created on restart. "
                "Use force=True to delete anyway."
            ),
        }

    deleted = def_manager.delete(row.id)
    if not deleted:
        return {"success": False, "error": f"Failed to delete definition '{row.name}'"}

    # Remove YAML template file if it exists
    try:
        from pathlib import Path

        from gobby.mcp_proxy.tools.workflows._auto_export import auto_delete_definition

        is_user = bool(row.tags and "user" in row.tags)
        auto_delete_definition(row.name, row.workflow_type, Path.cwd(), delete_global=is_user)
    except Exception as e:
        logger.warning(f"Failed to delete template '{row.name}': {e}")

    loader.clear_cache()
    logger.info(f"Deleted workflow definition '{row.name}' (id={row.id})")

    return {"success": True, "deleted": {"id": row.id, "name": row.name}}


def restore_workflow_definition(
    def_manager: LocalWorkflowDefinitionManager,
    loader: WorkflowLoader,
    name: str | None = None,
    definition_id: str | None = None,
) -> dict[str, Any]:
    """
    Restore a soft-deleted workflow/pipeline definition.

    Args:
        def_manager: Definition storage manager
        loader: WorkflowLoader (cache is cleared after restore)
        name: Resolve definition by name
        definition_id: Resolve definition by ID

    Returns:
        Dict with success status and restored definition metadata
    """
    try:
        row = _resolve_definition(def_manager, name, definition_id, include_deleted=True)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    if row.deleted_at is None:
        return {"success": False, "error": f"Definition '{row.name}' is not deleted"}

    try:
        restored = def_manager.restore(row.id)
    except Exception as e:
        return {"success": False, "error": f"Restore failed: {e}"}

    loader.clear_cache()
    logger.info(f"Restored workflow definition '{restored.name}' (id={restored.id})")

    return {
        "success": True,
        "definition": {
            "id": restored.id,
            "name": restored.name,
            "workflow_type": restored.workflow_type,
            "description": restored.description,
            "version": restored.version,
            "enabled": restored.enabled,
        },
    }


def export_workflow_definition(
    def_manager: LocalWorkflowDefinitionManager,
    name: str | None = None,
    definition_id: str | None = None,
) -> dict[str, Any]:
    """
    Export a workflow/pipeline definition as YAML.

    Args:
        def_manager: Definition storage manager
        name: Resolve definition by name
        definition_id: Resolve definition by ID

    Returns:
        Dict with success status and YAML content string
    """
    try:
        row = _resolve_definition(def_manager, name, definition_id)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    try:
        yaml_content = def_manager.export_to_yaml(row.id)
    except Exception as e:
        return {"success": False, "error": f"Export failed: {e}"}

    return {
        "success": True,
        "name": row.name,
        "workflow_type": row.workflow_type,
        "yaml_content": yaml_content,
    }
