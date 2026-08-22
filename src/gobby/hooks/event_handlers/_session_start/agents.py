"""Default-agent activation helpers for session-start handlers."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from gobby.utils.wiki_vault import existing_vault_dir

from .types import AgentActivationResult

SLOW_AGENT_ACTIVATION_THRESHOLD_MS = 1000


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

    from gobby.storage.config_repository import ConfigRepository

    values = ConfigRepository(handler._session_manager.db).read(resolve_secrets=False).values
    return str(values.get("default_agent") or "default")


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

    from gobby.hooks.session_activation import _session_is_spawned
    from gobby.mcp_proxy.tools.apply_persona import build_persona_changes

    session = handler._session_manager.get(session_id)
    is_spawned = _session_is_spawned(session)

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
        handler.logger.debug("Could not check code index availability: %s", e)


def _seed_memory_recall_vars(handler: Any, session_id: str) -> None:
    """Seed parent turn tracking before activation guards run."""
    from gobby.workflows.state_manager import SessionVariableManager

    sv_mgr = SessionVariableManager(handler._session_manager.db)

    existing = sv_mgr.get_variables(session_id)
    if "parent_turn_seq" not in (existing or {}):
        sv_mgr.merge_variables(session_id, {"parent_turn_seq": 0})


_WIKI_OVERVIEW_WORD_CAP = 500
_INJECTED_CONTEXT_MARKERS = (
    "<!-- gobby:injected-context:begin -->",
    "<!-- gobby:injected-context:end -->",
)


def sanitize_wiki_overview(overview: str) -> str:
    """Remove context-boundary controls before overview text is injected."""
    sanitized = overview
    for marker in _INJECTED_CONTEXT_MARKERS:
        sanitized = sanitized.replace(marker, "")
    sanitized = "".join(char for char in sanitized if char in "\n\t" or ord(char) >= 32)
    lines = [line for line in sanitized.splitlines() if not line.strip().startswith("<!-- gobby:")]
    return "\n".join(lines).strip()


def load_wiki_overview(project_root: Path) -> str | None:
    """Extract the vault ``_index.md`` ``## Overview`` block, word-capped.

    Uses the shared ``gobby.utils.wiki_vault`` resolver and reads only an
    initialized vault. Returns ``None`` when no vault, no index, or no
    Overview content exists.
    """
    vault = existing_vault_dir(project_root)
    if vault is None:
        return None
    index_path = vault / "_index.md"
    try:
        index_text = index_path.read_text(encoding="utf-8")
    except OSError:
        return None
    overview_lines: list[str] = []
    in_overview = False
    for line in index_text.splitlines():
        stripped = line.strip()
        if stripped == "## Overview":
            in_overview = True
            continue
        if in_overview and stripped.startswith("## "):
            break
        if in_overview:
            overview_lines.append(line)
    overview = "\n".join(overview_lines).strip()
    if not overview:
        return None
    words = overview.split()
    if len(words) > _WIKI_OVERVIEW_WORD_CAP:
        overview = (
            " ".join(words[:_WIKI_OVERVIEW_WORD_CAP])
            + " ... (truncated; full overview in wiki _index.md)"
        )
    sanitized = sanitize_wiki_overview(overview)
    return sanitized or None


def _seed_wiki_overview_var(handler: Any, session_id: str, project_id: str | None) -> None:
    """Seed ``wiki_overview`` from the vault index Overview block, best-effort."""
    if not project_id or handler._session_manager is None:
        return
    try:
        from gobby.storage.projects import LocalProjectManager
        from gobby.workflows.state_manager import SessionVariableManager

        project = LocalProjectManager(handler._session_manager.db).get(project_id)
        repo_path = project.repo_path if project else None
        if not repo_path:
            return
        overview = load_wiki_overview(Path(repo_path))
        if not overview:
            return
        SessionVariableManager(handler._session_manager.db).merge_variables(
            session_id, {"wiki_overview": overview}
        )
    except Exception as e:
        handler.logger.debug("Could not seed wiki overview: %s", e)


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
    from gobby.workflows.variable_defaults import resolve_session_project_id

    if project_id is None:
        project_id = resolve_session_project_id(handler._session_manager.db, session_id)

    try:
        agent_body = resolve_agent(
            default_agent_name,
            handler._session_manager.db,
            project_id=project_id,
        )
    except AgentResolutionError as e:
        handler.logger.error("Failed to resolve default agent '%s': %s", default_agent_name, e)
        return None

    if not agent_body:
        handler.logger.debug("Default agent '%s' not found in DB", default_agent_name)
        return None

    _ta_queries = time.monotonic()
    from gobby.skills.manager import SkillManager
    from gobby.storage.definitions.rules import RuleDefinitionManager
    from gobby.storage.definitions.variables import SessionVariableDefaultManager

    enabled_rules = RuleDefinitionManager(handler._session_manager.db).list_all(
        enabled=True,
        project_id=project_id,
    )
    enabled_variables = SessionVariableDefaultManager(handler._session_manager.db).list_all(
        project_id=project_id,
        enabled=True,
    )
    if project_id is None:
        enabled_rules = [row for row in enabled_rules if row.project_id is None]
        enabled_variables = [row for row in enabled_variables if row.project_id is None]
    all_skills = SkillManager(handler._session_manager.db).list_skills()

    _ta_build = time.monotonic()
    changes, active_rules, active_skills = handler._build_agent_changes(
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
    if active_skills is None:
        injected_names = sorted(
            skill.name
            for skill in all_skills
            if isinstance(getattr(skill, "name", None), str) and skill.name
        )
    else:
        injected_names = sorted(active_skills)
    skills_count = len(injected_names)

    def _ms(a: float, b: float) -> int:
        return int((b - a) * 1000)

    _ta_end = time.monotonic()
    timings = {
        "resolve_name": _ms(_ta0, _ta_resolve),
        "resolve_agent": _ms(_ta_resolve, _ta_queries),
        "db_queries": _ms(_ta_queries, _ta_build),
        "build_changes": _ms(_ta_build, _ta_vars),
        "merge_vars": _ms(_ta_vars, _ta_format),
        "format": _ms(_ta_format, _ta_end),
        "total": _ms(_ta0, _ta_end),
    }
    timing_message = "_activate_default_agent timing: " + " ".join(
        f"{name}={duration}ms" for name, duration in timings.items()
    )
    slow_component, slow_ms = max(
        ((name, duration) for name, duration in timings.items() if name != "total"),
        key=lambda item: item[1],
    )
    if timings["total"] >= SLOW_AGENT_ACTIVATION_THRESHOLD_MS:
        handler.logger.info(
            "Default agent activation slow: component=%s duration=%sms total=%sms session=%s agent=%s",
            slow_component,
            slow_ms,
            timings["total"],
            session_id,
            agent_body.name,
        )
    else:
        handler.logger.debug(timing_message)

    return AgentActivationResult(
        agent_name=agent_body.name,
        description=agent_body.description,
        rules_count=len(active_rules),
        skills_count=skills_count,
        variables_count=variables_count,
        injected_skill_names=injected_names,
    )
