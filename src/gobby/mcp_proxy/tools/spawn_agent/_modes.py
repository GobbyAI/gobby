"""Self-mode handler for spawn_agent.

Applies agent persona on the calling session (mode=self).
If the agent has inline steps, creates a WorkflowInstance for step enforcement.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)


async def _handle_self_persona(
    agent_body: Any,  # AgentDefinitionBody
    agent_name: str,
    parent_session_id: str,
    session_manager: Any,
    db: Any,
) -> dict[str, Any]:
    """
    Activate persona on calling session for mode=self.

    If the agent has inline steps, creates a WorkflowInstance on the parent
    session so the step enforcement engine can enforce tool restrictions.

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

        # Create WorkflowInstance for inline step workflow
        if agent_body.steps:
            from gobby.workflows.definitions import WorkflowInstance
            from gobby.workflows.state_manager import WorkflowInstanceManager

            step_wf_name = f"{agent_name}-steps"
            step_instance = WorkflowInstance(
                id=str(uuid.uuid4()),
                session_id=parent_session_id,
                workflow_name=step_wf_name,
                enabled=True,
                priority=10,
                current_step=agent_body.steps[0].name,
                variables=dict(agent_body.step_variables),
            )
            WorkflowInstanceManager(db).save_instance(step_instance)
            changes["_step_workflow_name"] = step_wf_name
            logger.info(
                "Created step workflow instance %s for session %s (agent=%s, step=%s)",
                step_wf_name,
                parent_session_id,
                agent_name,
                agent_body.steps[0].name,
            )

    if db:
        from gobby.workflows.state_manager import SessionVariableManager

        SessionVariableManager(db).merge_variables(parent_session_id, changes)

    return {
        "success": True,
        "mode": "self",
        "persona_applied": agent_name,
        "has_steps": bool(agent_body.steps),
        "message": f"Agent persona '{agent_name}' applied to session {parent_session_id}",
    }
