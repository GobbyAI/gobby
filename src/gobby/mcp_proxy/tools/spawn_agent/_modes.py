"""Self-mode handler for spawn_agent.

Activates a workflow on the calling session instead of spawning a new agent.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from gobby.workflows.loader import WorkflowLoader

logger = logging.getLogger(__name__)


async def _handle_self_mode(
    workflow: str | None,
    parent_session_id: str,
    step_variables: dict[str, Any] | None,
    initial_step: str | None,
    workflow_loader: WorkflowLoader | None,
    state_manager: Any | None,
    session_manager: Any | None,
    db: Any | None,
    project_path: str | None,
) -> dict[str, Any]:
    """
    Activate workflow on calling session instead of spawning a new agent.

    This is the implementation for mode=self, which activates a workflow
    on the parent session rather than creating a new child session.

    Args:
        workflow: Workflow name to activate
        parent_session_id: Session to activate workflow on (the caller)
        step_variables: Initial variables for the workflow
        initial_step: Optional starting step (defaults to first step)
        workflow_loader: WorkflowLoader instance
        state_manager: WorkflowStateManager instance (or created from db if None)
        session_manager: LocalSessionManager instance
        db: Database instance
        project_path: Project path for workflow lookup

    Returns:
        Dict with success status and activation details
    """
    if not workflow:
        return {"success": False, "error": "mode: self requires a workflow to activate"}

    # Create state_manager from db if not provided
    effective_state_manager = state_manager
    if effective_state_manager is None and db is not None:
        from gobby.workflows.state_manager import WorkflowStateManager

        effective_state_manager = WorkflowStateManager(db)

    if not workflow_loader or not effective_state_manager or not session_manager or not db:
        return {
            "success": False,
            "error": "mode: self requires workflow_loader, state_manager (or db), session_manager, and db",
        }

    # Pre-check for active step workflow before attempting activation
    existing_state = effective_state_manager.get_state(parent_session_id)
    if existing_state and existing_state.workflow_name not in ("__lifecycle__", "__ended__"):
        # Check if existing workflow is a lifecycle type (they coexist with step workflows)
        existing_def = await workflow_loader.load_workflow(
            existing_state.workflow_name,
            Path(project_path) if project_path else None,
        )
        if not existing_def or existing_def.type != "lifecycle":
            return {
                "success": False,
                "error": (
                    f"Session already has step workflow '{existing_state.workflow_name}' active. "
                    f"Call end_workflow(session_id='{parent_session_id}') first before activating a new workflow."
                ),
            }

    # Import and call the existing activate_workflow function
    from gobby.mcp_proxy.tools.workflows._lifecycle import activate_workflow

    result = await activate_workflow(
        loader=workflow_loader,
        state_manager=effective_state_manager,
        session_manager=session_manager,
        db=db,
        name=workflow,
        session_id=parent_session_id,
        initial_step=initial_step,
        variables=step_variables,
        project_path=project_path,
    )

    if not result.get("success"):
        return result

    return {
        "success": True,
        "mode": "self",
        "workflow_activated": workflow,
        "session_id": parent_session_id,
        "step": result.get("step"),
        "steps": result.get("steps"),
        "message": f"Workflow '{workflow}' activated on session {parent_session_id}",
    }


async def _handle_self_persona(
    agent_body: Any,  # AgentDefinitionBody
    agent_name: str,
    parent_session_id: str,
    session_manager: Any,
    db: Any,
) -> dict[str, Any]:
    """
    Activate persona on calling session for mode=self without a workflow.

    Args:
        agent_body: The resolved AgentDefinitionBody
        agent_name: The name of the agent
        parent_session_id: Session to apply the persona to
        session_manager: LocalSessionManager instance
        db: DatabaseProtocol instance

    Returns:
        Dict with success status and activation details
    """
    changes: dict[str, Any] = {
        "_agent_type": agent_name,
    }

    # Preset variables (skip _-prefixed reserved keys)
    if agent_body.workflows and agent_body.workflows.variables:
        for key, value in agent_body.workflows.variables.items():
            if key.startswith("_"):
                logger.warning("Skipping reserved variable %r from agent definition", key)
                continue
            changes[key] = value

    if db:
        from gobby.skills.manager import SkillManager
        from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
        from gobby.workflows.selectors import resolve_rules_for_agent, resolve_skills_for_agent

        def_manager = LocalWorkflowDefinitionManager(db)
        all_rules = def_manager.list_all(workflow_type="rule", enabled=True)

        active_rules = resolve_rules_for_agent(agent_body, all_rules)
        changes["_active_rule_names"] = list(active_rules)

        # Load skills to resolve selectors
        skill_mgr = SkillManager(db)
        all_skills = skill_mgr.list_skills()

        active_skills = resolve_skills_for_agent(agent_body, all_skills)
        if active_skills is not None:
            changes["_active_skill_names"] = list(active_skills)

        if agent_body.workflows and agent_body.workflows.skill_format:
            changes["_skill_format"] = agent_body.workflows.skill_format

    if db:
        from gobby.workflows.state_manager import SessionVariableManager

        SessionVariableManager(db).merge_variables(parent_session_id, changes)

    return {
        "success": True,
        "mode": "self",
        "persona_applied": agent_name,
        "message": f"Agent persona '{agent_name}' applied to session {parent_session_id}",
    }
