"""
Query tools for workflows.
"""

import json
import logging
from pathlib import Path
from typing import Any, Literal

import yaml

from gobby.mcp_proxy.tools.workflows._resolution import resolve_session_id
from gobby.storage.sessions import SessionManager
from gobby.utils.project_context import get_workflow_project_path
from gobby.workflows.pipeline_loader import PipelineLoader
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.step_instances import AgentStepInstanceManager

logger = logging.getLogger(__name__)

WorkflowKind = Literal["step", "lifecycle"]
_WORKFLOW_KINDS = frozenset({"step", "lifecycle"})


async def get_workflow(
    loader: PipelineLoader,
    name: str,
    project_id: str | None = None,
) -> dict[str, Any]:
    """
    Get workflow details including steps, triggers, and settings.

    Args:
        loader: PipelineLoader instance
        name: Workflow name (without .yaml extension)
        project_id: Project UUID for scoped lookup.

    Returns:
        Workflow definition details
    """
    definition = await loader.load_pipeline(name, project_id)

    if not definition:
        return {"success": False, "error": f"Workflow '{name}' not found"}

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
    loader: PipelineLoader,
    project_path: str | None = None,
    workflow_type: WorkflowKind | None = None,
    global_only: bool = False,
    db: Any = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """
    List available workflows.

    Queries DB-stored definitions first, then merges with filesystem discovery.
    DB entries take precedence for same-name workflows. Falls back to filesystem
    when DB has no results or DB is unavailable.

    Args:
        loader: PipelineLoader instance
        project_path: Project directory path. Auto-discovered from cwd if not provided.
        workflow_type: Filter by workflow kind ("step" or "lifecycle")
        global_only: If True, only show global workflows (ignore project)
        db: Optional database for querying stored definitions
        project_id: Caller project UUID used to scope DB definitions

    Returns:
        List of workflows with name, type, description, and source
    """
    from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

    if workflow_type is not None and workflow_type not in _WORKFLOW_KINDS:
        return {
            "success": False,
            "error": "workflow_type must be 'step' or 'lifecycle'",
        }

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
            db_rows = mgr.list_all(
                project_id=project_id if not global_only else None,
                workflow_type="workflow",
            )
            if global_only or project_id is None:
                db_rows = [row for row in db_rows if row.project_id is None]
            else:
                db_rows = [row for row in db_rows if row.project_id in {None, project_id}]
                db_rows.sort(key=lambda row: row.project_id is None)
            for row in db_rows:
                if row.workflow_type != "workflow":
                    continue
                if row.name in seen_names:
                    continue
                seen_names.add(row.name)
                try:
                    definition_data = json.loads(row.definition_json)
                except (json.JSONDecodeError, TypeError):
                    logger.warning("Skipping malformed workflow definition row %s", row.id)
                    continue
                if not isinstance(definition_data, dict):
                    logger.warning("Skipping non-object workflow definition row %s", row.id)
                    continue
                definition_type = definition_data.get("type", "step")
                if not isinstance(definition_type, str) or definition_type not in _WORKFLOW_KINDS:
                    continue
                if workflow_type and definition_type != workflow_type:
                    continue
                workflows.append(
                    {
                        "name": row.name,
                        "type": definition_type,
                        "description": row.description or "",
                        "source": row.source,
                        "enabled": row.enabled,
                        "priority": row.priority,
                    }
                )
        except Exception as e:
            logger.warning(
                "DB workflow query failed, falling back to filesystem: %s", e, exc_info=True
            )

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

                if not isinstance(wf_type, str) or wf_type not in _WORKFLOW_KINDS:
                    continue
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


def get_step_status(
    session_manager: SessionManager,
    session_id: str | None = None,
    instance_manager: AgentStepInstanceManager | None = None,
    session_var_manager: SessionVariableManager | None = None,
) -> dict[str, Any]:
    """Report the session's typed agent-step instance and session variables."""
    if not session_id:
        return {
            "success": False,
            "has_workflow": False,
            "error": "session_id is required. Pass the session ID explicitly to prevent cross-session variable bleed.",
        }

    try:
        resolved_session_id = resolve_session_id(session_manager, session_id)
    except ValueError as e:
        return {"success": False, "has_workflow": False, "error": str(e)}

    session_vars = (
        session_var_manager.get_variables(resolved_session_id) if session_var_manager else {}
    )

    if instance_manager is None:
        instance = None
    else:
        instance = instance_manager.get_for_session(resolved_session_id)

    if instance is None:
        return {
            "success": True,
            "has_workflow": False,
            "session_id": resolved_session_id,
            "session_variables": session_vars,
        }

    return {
        "success": True,
        "has_workflow": True,
        "session_id": resolved_session_id,
        "agent_name": instance.agent_name,
        "current_step": instance.current_step,
        "steps": [step.name for step in instance.snapshot.steps],
        "exit_condition": instance.snapshot.exit_condition,
        "variables": instance.variables,
        "session_variables": session_vars,
    }


get_workflow_status = get_step_status
