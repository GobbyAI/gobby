"""
Query tools for workflows.
"""

import logging
from pathlib import Path
from typing import Any

import yaml

from gobby.mcp_proxy.tools.workflows._resolution import resolve_session_id
from gobby.storage.sessions import LocalSessionManager
from gobby.utils.project_context import get_workflow_project_path
from gobby.workflows.definitions import WorkflowDefinition
from gobby.workflows.loader import WorkflowLoader
from gobby.workflows.state_manager import (
    SessionVariableManager,
    WorkflowInstanceManager,
)

logger = logging.getLogger(__name__)


async def get_workflow(
    loader: WorkflowLoader,
    name: str,
    project_path: str | None = None,
) -> dict[str, Any]:
    """
    Get workflow details including steps, triggers, and settings.

    Args:
        loader: WorkflowLoader instance
        name: Workflow name (without .yaml extension)
        project_path: Project directory path. Auto-discovered from cwd if not provided.

    Returns:
        Workflow definition details
    """
    # Auto-discover project path if not provided
    if not project_path:
        discovered = get_workflow_project_path()
        if discovered:
            project_path = str(discovered)

    proj = Path(project_path) if project_path else None
    definition = await loader.load_workflow(name, proj)

    if not definition:
        return {"success": False, "error": f"Workflow '{name}' not found"}

    # Handle WorkflowDefinition vs PipelineDefinition
    if isinstance(definition, WorkflowDefinition):
        return {
            "success": True,
            "name": definition.name,
            "enabled": definition.enabled,
            "description": definition.description,
            "version": definition.version,
            "steps": (
                [
                    {
                        "name": s.name,
                        "description": s.description,
                        "allowed_tools": s.allowed_tools,
                        "blocked_tools": s.blocked_tools,
                    }
                    for s in definition.steps
                ]
                if definition.steps
                else []
            ),
            "triggers": {},
            "settings": definition.settings,
        }
    else:
        # PipelineDefinition
        return {
            "success": True,
            "name": definition.name,
            "type": "pipeline",
            "description": definition.description,
            "version": definition.version,
            "steps": (
                [{"id": s.id, "exec": s.exec, "prompt": s.prompt} for s in definition.steps]
                if definition.steps
                else []
            ),
            "triggers": {},
            "settings": {},
        }


def list_workflows(
    loader: WorkflowLoader,
    project_path: str | None = None,
    workflow_type: str | None = None,
    global_only: bool = False,
    db: Any = None,
) -> dict[str, Any]:
    """
    List available workflows.

    Queries DB-stored definitions first, then merges with filesystem discovery.
    DB entries take precedence for same-name workflows. Falls back to filesystem
    when DB has no results or DB is unavailable.

    Args:
        loader: WorkflowLoader instance
        project_path: Project directory path. Auto-discovered from cwd if not provided.
        workflow_type: Filter by type ("step" or "lifecycle")
        global_only: If True, only show global workflows (ignore project)
        db: Optional database for querying stored definitions

    Returns:
        List of workflows with name, type, description, and source
    """
    from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

    # Auto-discover project path if not provided
    if not project_path:
        discovered = get_workflow_project_path()
        if discovered:
            project_path = str(discovered)

    workflows: list[dict[str, Any]] = []
    seen_names: set[str] = set()

    # Query DB first — DB definitions take precedence
    if db is not None:
        try:
            mgr = LocalWorkflowDefinitionManager(db)
            db_rows = mgr.list_all(workflow_type=workflow_type)
            for row in db_rows:
                if row.name in seen_names:
                    continue
                workflows.append(
                    {
                        "name": row.name,
                        "type": row.workflow_type,
                        "description": row.description or "",
                        "source": row.source,
                        "enabled": row.enabled,
                        "priority": row.priority,
                    }
                )
                seen_names.add(row.name)
        except Exception as e:
            logger.warning("DB workflow query failed, falling back to filesystem: %s", e)

    # Merge with filesystem discovery
    search_dirs = list(loader.global_dirs)
    proj = Path(project_path) if project_path else None

    # Include project workflows unless global_only (project searched first to shadow global)
    if not global_only and proj:
        project_dir = proj / ".gobby" / "workflows"
        if project_dir.exists():
            search_dirs.insert(0, project_dir)

    for search_dir in search_dirs:
        if not search_dir.exists():
            continue

        is_project = proj and search_dir == (proj / ".gobby" / "workflows")

        for yaml_path in search_dir.glob("*.yaml"):
            name = yaml_path.stem
            if name in seen_names:
                continue

            try:
                with open(yaml_path, encoding="utf-8") as f:
                    data = yaml.safe_load(f)

                if not data:
                    continue

                wf_type = data.get("type", "step")

                if workflow_type and wf_type != workflow_type:
                    continue

                workflows.append(
                    {
                        "name": name,
                        "type": wf_type,
                        "description": data.get("description", ""),
                        "source": "project" if is_project else "installed",
                    }
                )
                seen_names.add(name)

            except (yaml.YAMLError, OSError, UnicodeDecodeError) as e:
                logger.debug(
                    "Skipping invalid workflow file %s: %s",
                    yaml_path,
                    e,
                    exc_info=True,
                )

    return {"success": True, "workflows": workflows, "count": len(workflows)}


def get_workflow_status(
    session_manager: LocalSessionManager,
    session_id: str | None = None,
    instance_manager: WorkflowInstanceManager | None = None,
    session_var_manager: SessionVariableManager | None = None,
) -> dict[str, Any]:
    """
    Get current workflow status for a session.

    Returns all active workflow instances with per-workflow variables
    and session variables separately.

    Args:
        session_manager: LocalSessionManager instance
        session_id: Session reference (accepts #N, N, UUID, or prefix)
        instance_manager: Optional WorkflowInstanceManager for multi-workflow status
        session_var_manager: Optional SessionVariableManager for session variables

    Returns:
        Workflow state including per-instance details and session variables
    """
    # Require explicit session_id to prevent cross-session bleed
    if not session_id:
        return {
            "success": False,
            "has_workflow": False,
            "error": "session_id is required. Pass the session ID explicitly to prevent cross-session variable bleed.",
        }

    # Resolve session_id to UUID (accepts #N, N, UUID, or prefix)
    try:
        resolved_session_id = resolve_session_id(session_manager, session_id)
    except ValueError as e:
        return {"success": False, "has_workflow": False, "error": str(e)}

    session_vars = (
        session_var_manager.get_variables(resolved_session_id) if session_var_manager else {}
    )

    workflows = []
    if instance_manager:
        instances = instance_manager.get_active_instances(resolved_session_id)
        workflows = [
            {
                "workflow_name": inst.workflow_name,
                "enabled": inst.enabled,
                "priority": inst.priority,
                "current_step": inst.current_step,
                "variables": inst.variables,
            }
            for inst in instances
        ]

    return {
        "success": True,
        "has_workflow": len(workflows) > 0,
        "session_id": resolved_session_id,
        "workflows": workflows,
        "session_variables": session_vars,
    }
