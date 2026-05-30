"""Default-agent activation helpers for session-start handlers."""

from __future__ import annotations

import time
from typing import Any

from .types import AgentActivationResult


def resolve_agent_name(
    handler: Any,
    session_id: str,
    agent_name_override: str | None,
) -> str:
    """Determine which agent to activate."""
    if handler._session_manager is None:
        raise RuntimeError("session storage is not initialized")
    if agent_name_override:
        return agent_name_override

    from gobby.workflows.state_manager import SessionVariableManager

    sv_mgr = SessionVariableManager(handler._session_manager.db)
    existing_vars = sv_mgr.get_variables(session_id)
    existing_agent_type = existing_vars.get("_agent_type") if existing_vars else None

    if existing_agent_type and existing_agent_type != "default":
        return str(existing_agent_type)

    from gobby.storage.config_store import ConfigStore

    config_store = ConfigStore(handler._session_manager.db)
    return config_store.get("default_agent") or "default"


def build_agent_changes(
    handler: Any,
    agent_body: Any,
    session_id: str,
    enabled_rules: list[Any],
    all_skills: list[Any],
    enabled_variables: list[Any],
) -> tuple[dict[str, Any], set[str], set[str] | None]:
    """Build session variable changes from agent definition, rules, skills, and variables."""
    if handler._session_manager is None:
        raise RuntimeError("session storage is not initialized")

    from gobby.mcp_proxy.tools.apply_persona import build_persona_changes

    session = handler._session_manager.get(session_id)
    is_spawned = bool(session and (session.agent_run_id or session.agent_depth))

    return build_persona_changes(
        agent_body=agent_body,
        session_id=session_id,
        db=handler._session_manager.db,
        enabled_rules=enabled_rules,
        all_skills=all_skills,
        enabled_variables=enabled_variables,
        is_spawned=is_spawned,
    )


def setup_code_index(handler: Any, session_id: str | None, project_id: str | None) -> None:
    """Set code_index_available session variable if the project has an index."""
    if not session_id or not project_id or not handler._session_manager:
        return
    try:
        from gobby.code_index.storage import CodeIndexStorage
        from gobby.workflows.state_manager import SessionVariableManager

        cis = CodeIndexStorage(handler._session_manager.db)
        stats = cis.get_project_stats(project_id)
        if stats and stats.total_symbols > 0:
            sv_mgr = SessionVariableManager(handler._session_manager.db)
            sv_mgr.set_variable(session_id, "code_index_available", True)
    except Exception as e:
        handler.logger.debug(f"Could not check code index availability: {e}")


def _seed_memory_recall_vars(handler: Any, session_id: str) -> None:
    """Seed parent turn tracking before activation guards run."""
    from gobby.workflows.state_manager import SessionVariableManager

    sv_mgr = SessionVariableManager(handler._session_manager.db)

    existing = sv_mgr.get_variables(session_id)
    if "parent_turn_seq" not in (existing or {}):
        sv_mgr.merge_variables(session_id, {"parent_turn_seq": 0})


def activate_default_agent(
    handler: Any,
    session_id: str,
    cli_source: str,
    project_id: str | None,
    agent_name_override: str | None = None,
) -> AgentActivationResult | None:
    """Activate the default agent for a session, merging its properties."""
    if handler._session_manager is None:
        return None

    _ta0 = time.monotonic()
    default_agent_name = handler._resolve_agent_name(session_id, agent_name_override)
    if default_agent_name == "none":
        return None

    _ta_resolve = time.monotonic()
    from gobby.workflows.agent_resolver import AgentResolutionError, resolve_agent

    try:
        agent_body = resolve_agent(
            default_agent_name,
            handler._session_manager.db,
            project_id=project_id,
        )
    except AgentResolutionError as e:
        handler.logger.error(f"Failed to resolve default agent '{default_agent_name}': {e}")
        return None

    if not agent_body:
        handler.logger.debug(f"Default agent '{default_agent_name}' not found in DB")
        return None

    _ta_queries = time.monotonic()
    from gobby.skills.manager import SkillManager
    from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

    def_manager = LocalWorkflowDefinitionManager(handler._session_manager.db)
    enabled_rules = [r for r in def_manager.list_all(workflow_type="rule") if r.enabled]
    enabled_variables = [v for v in def_manager.list_all(workflow_type="variable") if v.enabled]
    all_skills = SkillManager(handler._session_manager.db).list_skills()

    _ta_build = time.monotonic()
    changes, active_rules, _ = handler._build_agent_changes(
        agent_body,
        session_id,
        enabled_rules,
        all_skills,
        enabled_variables,
    )

    from gobby.workflows.state_manager import SessionVariableManager

    sv_mgr = SessionVariableManager(handler._session_manager.db)

    internal_keys = {
        "_agent_type",
        "_active_rule_names",
        "_active_skill_names",
        "_skill_format",
        "_agent_blocked_tools",
        "_agent_blocked_mcp_tools",
        "is_spawned_agent",
    }
    variables_count = len([k for k in changes if k not in internal_keys])

    existing = sv_mgr.get_variables(session_id)
    if existing:
        always_reapply = {
            "_agent_type",
            "_active_rule_names",
            "_active_skill_names",
            "_skill_format",
            "_agent_blocked_tools",
            "_agent_blocked_mcp_tools",
            "is_spawned_agent",
        }
        changes = {k: v for k, v in changes.items() if k in always_reapply or k not in existing}

    _ta_vars = time.monotonic()
    sv_mgr.merge_variables(session_id, changes)

    _ta_format = time.monotonic()
    identity_parts: list[str] = []
    if agent_body.role:
        identity_parts.append(f"## Role\n{agent_body.role}")
    if agent_body.personality:
        identity_parts.append(f"## Personality\n{agent_body.personality}")

    skills_count = 0
    injected_names: list[str] = []

    def _ms(a: float, b: float) -> int:
        return int((b - a) * 1000)

    _ta_end = time.monotonic()
    handler.logger.info(
        "_activate_default_agent timing: "
        f"resolve_name={_ms(_ta0, _ta_resolve)}ms "
        f"resolve_agent={_ms(_ta_resolve, _ta_queries)}ms "
        f"db_queries={_ms(_ta_queries, _ta_build)}ms "
        f"build_changes={_ms(_ta_build, _ta_vars)}ms "
        f"merge_vars={_ms(_ta_vars, _ta_format)}ms "
        f"format={_ms(_ta_format, _ta_end)}ms "
        f"total={_ms(_ta0, _ta_end)}ms",
    )

    return AgentActivationResult(
        context="\n\n".join(identity_parts) if identity_parts else None,
        agent_name=agent_body.name,
        description=agent_body.description,
        role=agent_body.role,
        goal=agent_body.goal,
        rules_count=len(active_rules),
        skills_count=skills_count,
        variables_count=variables_count,
        injected_skill_names=injected_names,
    )
