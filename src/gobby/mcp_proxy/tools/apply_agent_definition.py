"""Shared full agent-definition activation."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import AgentDefinitionBody
from gobby.workflows.reserved_variables import is_reserved_workflow_variable
from gobby.workflows.variable_defaults import (
    load_variable_defaults,
    resolve_session_project_id,
)

logger = logging.getLogger(__name__)


def build_definition_changes(
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
    changes["_active_skill_names"] = sorted(active_skills) if active_skills is not None else None
    changes["_skill_format"] = agent_body.workflows.skill_format if agent_body.workflows else None

    definition_keys: set[str] = set()
    if agent_body.workflows and agent_body.workflows.variables:
        for key, value in agent_body.workflows.variables.items():
            if key.startswith("_"):
                logger.warning("Skipping reserved variable %r from agent definition", key)
                continue
            changes[key] = value
            definition_keys.add(key)

    defaults = load_variable_defaults(db, project_id)
    active_variable_names = resolve_variables_for_agent(agent_body, enabled_variables)
    for name, value in defaults.items():
        if name in changes:
            continue
        if active_variable_names is None or name in active_variable_names:
            changes[name] = value
            definition_keys.add(name)

    changes["_agent_blocked_tools"] = agent_body.blocked_tools or []
    changes["_agent_blocked_mcp_tools"] = agent_body.blocked_mcp_tools or []

    if agent_body.step_workflow and agent_body.step_workflow.steps:
        changes["step_workflow_complete"] = False
        definition_keys.add("step_workflow_complete")

    changes["_agent_definition_hash"] = definition_pin(agent_body)
    changes["_agent_definition_keys"] = sorted(definition_keys)
    return changes, active_rules, active_skills


def definition_pin(agent_body: AgentDefinitionBody) -> str:
    """Hash the resolved definition used by both activation entry points."""
    from gobby.storage.definitions import compute_definition_hash

    return compute_definition_hash(agent_body.model_dump_json())


def definition_drift_line(existing: Mapping[str, Any], agent: str, new_pin: str) -> str | None:
    """Describe a changed pin for the same seat, excluding first activation."""
    old_pin = existing.get("_agent_definition_hash")
    if existing.get("_agent_type") != agent or old_pin is None or old_pin == new_pin:
        return None
    return (
        f"Definition `{agent}` changed since this session activated it (`{old_pin[:12]}` → "
        f"`{new_pin[:12]}`); the current definition now applies."
    )


def build_persona_prompt_context(
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


def colliding_definition_variable_error(
    caller: dict[str, Any] | None,
    *,
    changes: dict[str, Any],
    extra_vars: dict[str, Any],
) -> str | None:
    """Reject caller keys that collide with definition/task overlays or reserved names."""
    if not caller:
        return None
    reserved = sorted(key for key in caller if is_reserved_workflow_variable(key))
    colliding = sorted(set(caller) & (set(changes) | set(extra_vars)))
    blocked = sorted(set(reserved) | set(colliding))
    if not blocked:
        return None
    return "Caller variables collide with reserved or definition-owned keys: " + ", ".join(blocked)


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


def is_spawned_session(variables: dict[str, Any], session: Any) -> bool:
    """Use the same spawn identity for refusal and definition-key cleanup."""
    from gobby.hooks.session_activation import _session_is_spawned

    return variables.get("is_spawned_agent") is True or _session_is_spawned(session)


def is_role_change(db: HubDatabase, variables: dict[str, Any], agent: str) -> bool:
    """A first activation may replace the configured base, but a seat needs relaunch."""
    current = variables.get("_agent_type")
    if not current or current == "default" or current == agent:
        return False
    from gobby.storage.config_repository import ConfigRepository

    base = ConfigRepository(db).read(resolve_secrets=False).values.get("default_agent")
    return bool(current != base)


def activation_decision(
    db: HubDatabase,
    variables: dict[str, Any],
    agent: str,
    pin: str,
    *,
    relaunch: bool,
    same_pin_noop: bool,
) -> str:
    if (
        same_pin_noop
        and variables.get("_agent_type") == agent
        and variables.get("_agent_definition_hash") == pin
    ):
        return "unchanged"
    if not relaunch and is_role_change(db, variables, agent):
        return "role_change_requires_relaunch"
    return "apply"


def commit_definition_changes(
    db: HubDatabase,
    session_id: str,
    agent: str,
    changes: dict[str, Any],
    *,
    expected_agent_type: Any,
    relaunch: bool,
    same_pin_noop: bool,
    definition_variable_names: set[str] | None = None,
    overlays: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Recheck identity under the step lock and commit exactly one variable merge."""
    from gobby.storage.hub.protocol import AgentStepInstanceMutation
    from gobby.storage.sessions import SessionManager
    from gobby.workflows.state_manager import SessionVariableManager

    manager = SessionVariableManager(db)
    with db.transaction_immediate(AgentStepInstanceMutation(session_id=session_id)):
        current = manager.get_variable_subset(
            session_id,
            (
                *changes,
                "_agent_type",
                "_agent_definition_hash",
                "_agent_definition_keys",
                "is_spawned_agent",
                "_persona_name",
            ),
        )
        current_agent = current.get("_agent_type")
        if current_agent != expected_agent_type:
            return {"status": "superseded", "agent": current_agent}
        decision = activation_decision(
            db,
            current,
            agent,
            changes["_agent_definition_hash"],
            relaunch=relaunch,
            same_pin_noop=same_pin_noop,
        )
        if decision != "apply":
            return {"status": decision, "agent": current_agent}
        always_reapply = {
            "_agent_type",
            "_active_rule_names",
            "_active_skill_names",
            "_skill_format",
            "_agent_blocked_tools",
            "_agent_blocked_mcp_tools",
            "is_spawned_agent",
            "_agent_definition_hash",
            "_agent_definition_keys",
        } | (definition_variable_names or set())
        delta = {
            key: value
            for key, value in changes.items()
            if key in always_reapply or key not in current
        }
        previous_keys = set(current.get("_agent_definition_keys") or [])
        new_keys = set(changes["_agent_definition_keys"])
        identity_change = (
            bool(current_agent)
            and current_agent != agent
            and not is_spawned_session(current, SessionManager(db).get(session_id))
        )
        if identity_change:
            from gobby.workflows.step_instances import AgentStepInstanceManager

            AgentStepInstanceManager(db).delete_for_session(session_id)
            delta.update(dict.fromkeys(previous_keys - new_keys))
            if "step_workflow_complete" in changes:
                delta["step_workflow_complete"] = changes["step_workflow_complete"]
        else:
            new_keys |= previous_keys
        delta["_agent_definition_keys"] = sorted(new_keys)
        delta.update(overlays or {})
        if current.get("_persona_name") and "_persona_name" not in (overlays or {}):
            # SessionStart refreshes seat enforcement while keeping the live persona.
            delta.pop("_active_skill_names", None)
            delta.pop("_skill_format", None)
        drift = definition_drift_line(current, agent, changes["_agent_definition_hash"])
        if drift:
            delta["_agent_definition_drift"] = drift
            delta["_agent_identity_reinject"] = True
        elif identity_change:
            delta["_agent_definition_drift"] = None
        manager.merge_variables(session_id, delta)
        merged = manager.get_variable_subset(session_id, ("_agent_type", "_active_skill_names"))
        return {"status": "applied", "agent": agent, "variables": merged}


def _refusal(code: str, error: str) -> dict[str, Any]:
    return {"success": False, "error_code": code, "error": error}


def _activation_receipt(status: str, current: Any, agent: str) -> dict[str, Any]:
    if status == "unchanged":
        return {"success": True, "status": status, "agent": agent}
    if status == "superseded":
        return _refusal("activation_superseded", f"Activation superseded by agent '{current}'")
    return _refusal(
        "role_change_requires_relaunch",
        f"Agent '{current}' cannot switch to '{agent}' without a relaunch",
    )


def _resolve_task_variables(
    task_id: str | None, task_manager: Any, project_id: str | None
) -> dict[str, Any]:
    if not task_id:
        return {}
    if task_manager is None or project_id is None:
        raise ValueError(f"Task {task_id} could not be resolved without task/project context")
    from gobby.mcp_proxy.tools.tasks import resolve_task_id_for_mcp

    resolved_id = resolve_task_id_for_mcp(task_manager, task_id, project_id)
    task = task_manager.get_task(resolved_id)
    if task is None:
        raise ValueError(f"Task {task_id} not found")
    return {"assigned_task_id": f"#{task.seq_num}" if task.seq_num else resolved_id}


async def apply_agent_definition_impl(
    agent: str,
    db: HubDatabase | None = None,
    session_id: str | None = None,
    variables: dict[str, Any] | None = None,
    task_id: str | None = None,
    task_manager: Any | None = None,
    cli_source: str | None = None,
    *,
    relaunch: bool = False,
) -> dict[str, Any]:
    """Activate a complete definition; a seat identity change requires relaunch."""
    if db is None:
        return _refusal("database_unavailable", "Database not available")
    if session_id is None:
        from gobby.utils.session_context import get_session_context

        context = get_session_context()
        session_id = context.session_id if context else None
    if not session_id:
        return _refusal("session_unavailable", "No session context")
    from gobby.storage.sessions import SessionManager
    from gobby.workflows.agent_resolver import resolve_agent_with_row
    from gobby.workflows.state_manager import SessionVariableManager

    stored = SessionVariableManager(db).get_variable_subset(
        session_id, ("_agent_type", "_agent_definition_hash", "is_spawned_agent")
    )
    session = SessionManager(db).get(session_id)
    if is_spawned_session(stored, session):
        return _refusal("spawned_session_definition_fixed", "Spawned session definition is fixed")
    project_id, source = _resolve_session_identity(db, session_id, cli_source)
    resolved = resolve_agent_with_row(agent, db, cli_source=source, project_id=project_id)
    if resolved is None:
        return _refusal("unknown_agent_definition", f"Agent definition '{agent}' not found")
    body, _ = resolved
    if not body.supports_surface("persona"):
        return _refusal("persona_surface_missing", f"Agent '{agent}' lacks the persona surface")
    if body.workflows.pipeline:
        return _refusal("pipeline_requires_spawn", f"Agent '{agent}' requires a pipeline spawn")
    pin = definition_pin(body)
    decision = activation_decision(db, stored, agent, pin, relaunch=relaunch, same_pin_noop=True)
    if decision != "apply":
        return _activation_receipt(decision, stored.get("_agent_type"), agent)
    try:
        extra = _resolve_task_variables(task_id, task_manager, project_id)
    except Exception as exc:
        logger.warning("Failed to resolve activation task %s: %s", task_id, exc)
        return _refusal("task_unresolved", str(exc))
    from gobby.skills.manager import SkillManager

    all_skills = SkillManager(db).list_skills()
    changes, rules, skills = build_definition_changes(
        body, session_id, db, all_skills=all_skills, is_spawned=False
    )
    overlays = {
        "_persona_name": None,
        "_agent_context_injected": False,
        "_agent_identity_reinject": True,
        **extra,
    }
    collision = colliding_definition_variable_error(variables, changes=changes, extra_vars=overlays)
    if collision:
        return _refusal("variable_collision", collision)
    committed = commit_definition_changes(
        db,
        session_id,
        agent,
        changes,
        expected_agent_type=stored.get("_agent_type"),
        relaunch=relaunch,
        same_pin_noop=True,
        definition_variable_names=set(body.workflows.variables),
        overlays={**overlays, **(variables or {})},
    )
    if committed["status"] != "applied":
        return _activation_receipt(committed["status"], committed["agent"], agent)
    from gobby.hooks.session_activation import _ensure_step_instance
    from gobby.workflows.step_instances import AgentStepInstanceManager

    step_workflow = None
    step_workflow_pending = False
    try:
        _ensure_step_instance(db, session_id, committed["variables"], session)
        instance = AgentStepInstanceManager(db).get_for_session(session_id)
        if instance is not None:
            step_workflow = {
                "agent_name": instance.agent_name,
                "current_step": instance.current_step,
            }
    except Exception as exc:
        logger.warning("Step workflow pending for session %s agent %s: %s", session_id, agent, exc)
        step_workflow_pending = True
    return {
        "success": True,
        "status": "applied",
        "agent": agent,
        "definition_hash": pin,
        "rules_count": len(rules),
        "skills_count": len(skills) if skills is not None else len(all_skills),
        "blocked_tools_count": len(body.blocked_tools or []) + len(body.blocked_mcp_tools or []),
        "step_workflow": step_workflow,
        **({"step_workflow_pending": True} if step_workflow_pending else {}),
    }
