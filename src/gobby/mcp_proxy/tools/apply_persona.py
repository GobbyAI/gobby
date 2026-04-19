"""Session persona helpers and agent activation helpers.

This module intentionally carries two related but distinct behaviors:

- Agent activation helpers for session-start / spawned-agent flows. These are
  the existing heavy-weight helpers that merge rules, variables, tool blocks,
  and step workflows into a session.
- Session persona switching for the public ``apply_persona`` tool. This is the
  lighter-weight path used when a human wants to talk to a persona inside an
  existing session without changing provider/model/isolation or spawning a
  child agent.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from gobby.storage.database import DatabaseProtocol
from gobby.workflows.definitions import AgentDefinitionBody

logger = logging.getLogger(__name__)


def build_persona_changes(
    agent_body: AgentDefinitionBody,
    session_id: str,
    db: DatabaseProtocol,
    *,
    enabled_rules: list[Any] | None = None,
    all_skills: list[Any] | None = None,
    enabled_variables: list[Any] | None = None,
    is_spawned: bool = False,
) -> tuple[dict[str, Any], set[str], set[str] | None]:
    """Compute full activation changes for an agent-backed session."""
    from gobby.skills.manager import SkillManager
    from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
    from gobby.workflows.selectors import (
        resolve_rules_for_agent,
        resolve_skills_for_agent,
        resolve_variables_for_agent,
    )

    if enabled_rules is None:
        def_manager = LocalWorkflowDefinitionManager(db)
        enabled_rules = def_manager.list_all(workflow_type="rule", enabled=True)

    if all_skills is None:
        skill_mgr = SkillManager(db)
        all_skills = skill_mgr.list_skills()

    if enabled_variables is None:
        def_manager = LocalWorkflowDefinitionManager(db)
        enabled_variables = def_manager.list_all(workflow_type="variable", enabled=True)

    active_rules = resolve_rules_for_agent(agent_body, enabled_rules)

    changes: dict[str, Any] = {
        "_agent_type": agent_body.name,
        "_active_rule_names": list(active_rules),
        "is_spawned_agent": is_spawned,
    }

    active_skills = resolve_skills_for_agent(agent_body, all_skills)
    if active_skills is not None:
        changes["_active_skill_names"] = list(active_skills)

    if agent_body.workflows and agent_body.workflows.skill_format:
        changes["_skill_format"] = agent_body.workflows.skill_format

    if agent_body.workflows and agent_body.workflows.variables:
        for key, value in agent_body.workflows.variables.items():
            if key.startswith("_"):
                logger.warning(f"Skipping reserved variable {key!r} from agent definition")
                continue
            changes[key] = value

    active_variable_names = resolve_variables_for_agent(agent_body, enabled_variables)
    for var_row in enabled_variables:
        if active_variable_names is None or var_row.name in active_variable_names:
            try:
                var_body = json.loads(var_row.definition_json)
                if var_row.name not in changes:
                    changes[var_row.name] = var_body.get("value")
            except json.JSONDecodeError:
                logger.debug(f"Failed to parse variable definition for {var_row.name}")

    if agent_body.blocked_tools:
        changes["_agent_blocked_tools"] = agent_body.blocked_tools
    if agent_body.blocked_mcp_tools:
        changes["_agent_blocked_mcp_tools"] = agent_body.blocked_mcp_tools

    if agent_body.steps:
        from gobby.workflows.definitions import WorkflowInstance
        from gobby.workflows.state_manager import WorkflowInstanceManager

        step_wf_name = f"{agent_body.name}-steps"
        step_instance = WorkflowInstance(
            id=str(uuid.uuid4()),
            session_id=session_id,
            workflow_name=step_wf_name,
            enabled=True,
            priority=10,
            current_step=agent_body.steps[0].name,
            variables=dict(agent_body.step_variables),
        )
        WorkflowInstanceManager(db).save_instance(step_instance)
        changes["_step_workflow_name"] = step_wf_name
        changes["step_workflow_complete"] = False
        logger.info(
            "Created step workflow instance %s for session %s (agent=%s, step=%s)",
            step_wf_name,
            session_id,
            agent_body.name,
            agent_body.steps[0].name,
        )

    return changes, active_rules, active_skills


def build_session_persona_changes(
    agent_body: AgentDefinitionBody,
    db: DatabaseProtocol,
) -> tuple[dict[str, Any], set[str] | None]:
    """Compute the narrow session-variable delta for a persona switch."""
    from gobby.skills.manager import SkillManager
    from gobby.workflows.selectors import resolve_skills_for_agent

    all_skills = SkillManager(db).list_skills()
    active_skills = resolve_skills_for_agent(agent_body, all_skills)

    changes: dict[str, Any] = {
        "_agent_type": agent_body.name,
        "_active_skill_names": list(active_skills) if active_skills is not None else None,
        "_skill_format": (
            agent_body.workflows.skill_format if agent_body.workflows else None
        ),
        "_agent_context_injected": False,
        "_agent_identity_reinject": True,
    }

    return changes, active_skills


def build_session_persona_context(
    agent_body: AgentDefinitionBody,
    db: DatabaseProtocol,
    *,
    cli_source: str,
    identity_only: bool = False,
) -> tuple[str | None, set[str] | None]:
    """Build prompt context for a persona-capable agent definition."""
    from gobby.hooks.event_handlers._session_start import select_and_format_agent_skills
    from gobby.skills.manager import SkillManager
    from gobby.workflows.selectors import resolve_skills_for_agent

    parts: list[str] = []
    if agent_body.role:
        parts.append(f"## Role\n{agent_body.role}")
    if agent_body.goal:
        parts.append(f"## Goal\n{agent_body.goal}")
    if agent_body.personality:
        parts.append(f"## Personality\n{agent_body.personality}")

    if not identity_only and agent_body.instructions:
        parts.append(f"## Instructions\n{agent_body.instructions}")

    all_skills = SkillManager(db).list_skills()
    active_skills = resolve_skills_for_agent(agent_body, all_skills)
    if not identity_only:
        formatted, _, _ = select_and_format_agent_skills(
            agent_body,
            all_skills,
            active_skills,
            cli_source,
        )
        if formatted:
            parts.append(formatted)

    return ("\n\n".join(parts) if parts else None), active_skills


def _resolve_session_identity(
    db: DatabaseProtocol,
    session_id: str,
    cli_source: str | None,
) -> tuple[str | None, str | None]:
    """Resolve project and source metadata for a session when available."""
    try:
        from gobby.storage.sessions import LocalSessionManager

        session_row = LocalSessionManager(db).get(session_id)
    except Exception as e:
        logger.debug("Failed to resolve session metadata for %s: %s", session_id, e)
        return None, cli_source

    if session_row is None:
        return None, cli_source

    session_source = cli_source or getattr(session_row, "source", None)
    return getattr(session_row, "project_id", None), session_source


async def apply_persona_impl(
    agent: str,
    db: DatabaseProtocol | None = None,
    session_id: str | None = None,
    variables: dict[str, Any] | None = None,
    task_id: str | None = None,
    task_manager: Any | None = None,
    cli_source: str | None = None,
) -> dict[str, Any]:
    """Apply a session persona to the current session.

    This path updates prompt-facing persona state only:
    - current persona / agent name
    - skill selection
    - deferred context reinjection flags

    It intentionally does not change provider/model/isolation, merge agent
    variables, change active rules, create step workflows, or apply tool
    restrictions.
    """
    if db is None:
        return {"success": False, "error": "Database not available"}

    if session_id is None:
        from gobby.utils.session_context import get_session_context

        ctx = get_session_context()
        session_id = ctx.session_id if ctx else None

    if not session_id:
        return {"success": False, "error": "No session context — cannot apply persona"}

    project_id, resolved_cli_source = _resolve_session_identity(db, session_id, cli_source)

    from gobby.workflows.agent_resolver import resolve_agent

    agent_body = resolve_agent(
        agent,
        db,
        cli_source=resolved_cli_source,
        project_id=project_id,
    )
    if agent_body is None:
        return {
            "success": False,
            "error": f"Agent definition '{agent}' not found",
        }

    if not agent_body.supports_surface("persona"):
        return {
            "success": False,
            "error": f"Agent definition '{agent_body.name}' is not persona-capable",
        }

    extra_vars: dict[str, Any] = {}
    if task_id and task_manager:
        try:
            from gobby.utils.project_context import get_project_context

            proj_ctx = get_project_context()
            context_project_id = proj_ctx.get("id") if proj_ctx else None
            if context_project_id:
                from gobby.mcp_proxy.tools.tasks import resolve_task_id_for_mcp

                resolved_id = resolve_task_id_for_mcp(task_manager, task_id, context_project_id)
                task = task_manager.get_task(resolved_id)
                if task:
                    task_ref = f"#{task.seq_num}" if task.seq_num else resolved_id
                    extra_vars["assigned_task_id"] = task_ref
                    extra_vars["session_task"] = task_ref
        except Exception as e:
            logger.warning(f"Failed to resolve task_id {task_id}: {e}")

    changes, active_skills = build_session_persona_changes(agent_body, db)
    changes.update(extra_vars)
    if variables:
        changes.update(variables)

    from gobby.workflows.state_manager import SessionVariableManager

    SessionVariableManager(db).merge_variables(session_id, changes)

    return {
        "success": True,
        "mode": "persona",
        "persona_applied": agent_body.name,
        "active_skills": len(active_skills) if active_skills is not None else "all",
        "message": (
            f"Persona '{agent_body.name}' applied to session {session_id}. "
            "The updated persona will be injected on the next user turn."
        ),
    }
