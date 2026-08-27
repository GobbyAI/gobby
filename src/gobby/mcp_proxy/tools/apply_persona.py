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

import logging
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import AgentDefinitionBody
from gobby.workflows.reserved_variables import is_reserved_workflow_variable
from gobby.workflows.variable_defaults import (
    load_variable_defaults,
    resolve_session_project_id,
)

logger = logging.getLogger(__name__)


def _session_has_assigned_or_active_task(db: HubDatabase, session_id: str) -> bool:
    from gobby.workflows.state_manager import SessionVariableManager

    variables = SessionVariableManager(db).get_variables(session_id)
    return any(
        isinstance(value, str) and bool(value.strip())
        for value in (variables.get("assigned_task_id"), variables.get("active_task_id"))
    )


def build_persona_changes(
    agent_body: AgentDefinitionBody,
    session_id: str,
    db: HubDatabase,
    *,
    enabled_rules: list[Any] | None = None,
    all_skills: list[Any] | None = None,
    enabled_variables: list[Any] | None = None,
    is_spawned: bool = False,
) -> tuple[dict[str, Any], set[str], set[str] | None]:
    """Compute full activation changes for an agent-backed session."""
    from gobby.skills.manager import SkillManager
    from gobby.workflows.selectors import (
        resolve_rules_for_agent,
        resolve_skills_for_agent,
        resolve_variables_for_agent,
    )

    if enabled_rules is None:
        from gobby.storage.definitions.rules import RuleDefinitionManager

        enabled_rules = RuleDefinitionManager(db).list_all(enabled=True)

    if all_skills is None:
        skill_mgr = SkillManager(db)
        all_skills = skill_mgr.list_skills()

    project_id = resolve_session_project_id(db, session_id)
    if enabled_variables is None:
        from gobby.storage.definitions.variables import SessionVariableDefaultManager

        enabled_variables = SessionVariableDefaultManager(db).list_all(
            project_id=project_id,
            enabled=True,
        )
        if project_id is None:
            enabled_variables = [row for row in enabled_variables if row.project_id is None]

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
                logger.warning("Skipping reserved variable %r from agent definition", key)
                continue
            changes[key] = value

    defaults = load_variable_defaults(db, project_id)
    active_variable_names = resolve_variables_for_agent(agent_body, enabled_variables)
    for name, value in defaults.items():
        if name in changes:
            continue
        if active_variable_names is None or name in active_variable_names:
            changes[name] = value

    if agent_body.blocked_tools:
        changes["_agent_blocked_tools"] = agent_body.blocked_tools
    if agent_body.blocked_mcp_tools:
        changes["_agent_blocked_mcp_tools"] = agent_body.blocked_mcp_tools

    if agent_body.step_workflow and is_spawned:
        if _session_has_assigned_or_active_task(db, session_id):
            changes["step_workflow_complete"] = False

    return changes, active_rules, active_skills


def build_session_persona_changes(
    agent_body: AgentDefinitionBody,
    db: HubDatabase,
) -> tuple[dict[str, Any], set[str] | None]:
    """Compute the narrow session-variable delta for a persona switch."""
    from gobby.skills.manager import SkillManager
    from gobby.workflows.selectors import resolve_skills_for_agent

    all_skills = SkillManager(db).list_skills()
    active_skills = resolve_skills_for_agent(agent_body, all_skills)

    changes: dict[str, Any] = {
        "_persona_name": agent_body.name,
        "_active_skill_names": list(active_skills) if active_skills is not None else None,
        "_skill_format": (agent_body.workflows.skill_format if agent_body.workflows else None),
        "_agent_context_injected": False,
        "_agent_identity_reinject": True,
    }

    return changes, active_skills


def build_session_persona_context(
    agent_body: AgentDefinitionBody,
    db: HubDatabase,
    *,
    cli_source: str,
) -> tuple[str | None, set[str] | None]:
    """Build prompt context for a persona-capable agent definition."""
    from gobby.skills.manager import SkillManager
    from gobby.workflows.selectors import resolve_skills_for_agent

    all_skills = SkillManager(db).list_skills()
    active_skills = resolve_skills_for_agent(agent_body, all_skills)

    return agent_body.prompt_for("persona"), active_skills


def colliding_persona_variable_error(
    caller: dict[str, Any] | None,
    *,
    changes: dict[str, Any],
    extra_vars: dict[str, Any],
) -> str | None:
    """Reject caller keys that collide with persona/task overlays or reserved names."""
    if not caller:
        return None
    reserved = sorted(key for key in caller if is_reserved_workflow_variable(key))
    colliding = sorted(set(caller) & (set(changes) | set(extra_vars)))
    blocked = sorted(set(reserved) | set(colliding))
    if not blocked:
        return None
    return "Caller variables collide with reserved or persona-owned keys: " + ", ".join(blocked)


def _resolve_session_identity(
    db: HubDatabase,
    session_id: str,
    cli_source: str | None,
) -> tuple[str | None, str | None]:
    """Resolve project and source metadata for a session when available."""
    try:
        from gobby.storage.sessions import SessionManager

        session_row = SessionManager(db).get(session_id)
    except Exception as e:
        logger.debug("Failed to resolve session metadata for %s: %s", session_id, e)
        return None, cli_source

    if session_row is None:
        return None, cli_source

    session_source = cli_source or getattr(session_row, "source", None)
    return getattr(session_row, "project_id", None), session_source


async def apply_persona_impl(
    agent: str,
    db: HubDatabase | None = None,
    session_id: str | None = None,
    variables: dict[str, Any] | None = None,
    task_id: str | None = None,
    task_manager: Any | None = None,
    cli_source: str | None = None,
) -> dict[str, Any]:
    """Apply a session persona to the current session.

    This path updates prompt-facing persona state only:
    - persona prompt identity
    - skill selection
    - deferred context reinjection flags

    It intentionally does not change provider/model/isolation, merge agent
    variables, change active rules, or apply tool restrictions. It never
    reads or writes ``agent_step_instances``: step workflows belong to spawned
    agent runs, and a persona switch must neither install one nor disturb
    one that a spawn already created.
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

    from gobby.workflows.agent_resolver import resolve_agent_with_row

    resolved = resolve_agent_with_row(
        agent,
        db,
        cli_source=resolved_cli_source,
        project_id=project_id,
    )
    if resolved is None:
        return {
            "success": False,
            "error": f"Agent definition '{agent}' not found",
        }
    agent_body, _ = resolved

    if not agent_body.supports_surface("persona"):
        return {
            "success": False,
            "error": (
                f"Agent definition '{agent_body.name}' does not support the 'persona' surface"
            ),
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
        except Exception as e:
            logger.warning("Failed to resolve task_id %s: %s", task_id, e)

    changes, active_skills = build_session_persona_changes(agent_body, db)
    collision = colliding_persona_variable_error(
        variables,
        changes=changes,
        extra_vars=extra_vars,
    )
    if collision:
        return {"success": False, "error": collision}

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
