"""Live prompt and skill overlays, independent of seat activation."""

from __future__ import annotations

import logging
from typing import Any

from gobby.mcp_proxy.tools.apply_agent_definition import (
    _resolve_session_identity,
    _resolve_task_variables,
)
from gobby.mcp_proxy.tools.apply_agent_definition import (
    build_persona_prompt_context as build_session_persona_context,
)
from gobby.mcp_proxy.tools.apply_agent_definition import (
    colliding_definition_variable_error as colliding_persona_variable_error,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import AgentDefinitionBody

__all__ = ["apply_persona_impl", "build_session_persona_changes", "build_session_persona_context"]

logger = logging.getLogger(__name__)


def build_session_persona_changes(
    agent_body: AgentDefinitionBody,
    db: HubDatabase,
) -> tuple[dict[str, Any], set[str] | None]:
    """Compute only prompt identity, selected skills and reinjection markers."""
    _, active_skills = build_session_persona_context(agent_body, db, cli_source="")
    return {
        "_persona_name": agent_body.name,
        "_active_skill_names": sorted(active_skills) if active_skills is not None else None,
        "_skill_format": agent_body.workflows.skill_format,
        "_agent_context_injected": False,
        "_agent_identity_reinject": True,
    }, active_skills


async def apply_persona_impl(
    agent: str,
    db: HubDatabase | None = None,
    session_id: str | None = None,
    variables: dict[str, Any] | None = None,
    task_id: str | None = None,
    task_manager: Any | None = None,
    cli_source: str | None = None,
) -> dict[str, Any]:
    """Switch prompt and skills live, preserving seat enforcement and step state."""
    if db is None:
        return {"success": False, "error": "Database not available"}
    if session_id is None:
        from gobby.utils.session_context import get_session_context

        context = get_session_context()
        session_id = context.session_id if context else None
    if not session_id:
        return {"success": False, "error": "No session context — cannot apply persona"}

    from gobby.workflows.agent_resolver import resolve_agent_with_row
    from gobby.workflows.state_manager import SessionVariableManager

    manager = SessionVariableManager(db)
    clearing = agent == "default"
    if clearing:
        stored = manager.get_variables(session_id)
        agent = stored.get("_agent_type") or "default"
    project_id, source = _resolve_session_identity(db, session_id, cli_source)
    resolved = resolve_agent_with_row(agent, db, cli_source=source, project_id=project_id)
    if resolved is None:
        return {"success": False, "error": f"Agent definition '{agent}' not found"}
    body, _ = resolved
    if not clearing and not body.supports_surface("persona"):
        return {
            "success": False,
            "error": f"Agent definition '{body.name}' does not support the 'persona' surface",
        }
    try:
        if task_id and project_id is None:
            from gobby.utils.project_context import get_project_context

            project_context = get_project_context()
            project_id = project_context.get("id") if project_context else None
        extra = _resolve_task_variables(task_id, task_manager, project_id)
    except Exception as exc:
        logger.warning("Failed to resolve persona task %s: %s", task_id, exc)
        return {"success": False, "error": str(exc)}
    changes, skills = build_session_persona_changes(body, db)
    if clearing:
        changes["_persona_name"] = None
    collision = colliding_persona_variable_error(variables, changes=changes, extra_vars=extra)
    if collision:
        return {"success": False, "error": collision}
    manager.merge_variables(session_id, {**changes, **extra, **(variables or {})})
    return {
        "success": True,
        "mode": "persona",
        "persona_applied": body.name,
        "active_skills": len(skills) if skills is not None else "all",
        "message": f"Persona '{body.name}' will be injected on the next user turn.",
    }
