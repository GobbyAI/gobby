"""Generic capability comparisons for agent launch policy conditions."""

from pathlib import Path
from typing import Any

from gobby.storage.sessions import SessionManager
from gobby.utils.project_context import get_project_context
from gobby.workflows.agent_models import AgentDefinitionBody
from gobby.workflows.agent_resolver import (
    resolve_agent,
    resolve_agent_with_row,
    session_agent_definition,
)


def _context_project_id(context: dict[str, Any] | None) -> str | None:
    for key in ("id", "project_id"):
        value = context.get(key) if context is not None else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _target_project_id(
    sessions: SessionManager,
    project_path: Any,
    parent_session_id: Any,
    caller_project_id: str | None,
) -> str | None:
    """Resolve launch identity without depending on terminal/path placement.

    With no explicit path or parent, the launch targets the verified caller's
    project. Rule evaluation has no per-call project context to fall back on.
    """
    if isinstance(project_path, str) and not project_path.strip():
        project_path = None
    if parent_session_id == "":
        parent_session_id = None
    if project_path is not None:
        if not isinstance(project_path, str) or not project_path.strip():
            raise ValueError("Invalid spawn project path")
        context = get_project_context(Path(project_path.strip()).expanduser())
        if context is not None:
            return _context_project_id(context)
    if parent_session_id is not None:
        if not isinstance(parent_session_id, str) or not parent_session_id:
            raise ValueError("Invalid spawn parent reference")
        parent = sessions.get(sessions.resolve_session_reference(parent_session_id))
        if parent is None:
            raise ValueError("Spawn parent not found")
        return parent.project_id
    if project_path is not None:
        return None
    return caller_project_id


def _resolve_hop(
    session_manager: SessionManager, target: str, project_id: str | None
) -> tuple[AgentDefinitionBody, bool] | None:
    """One launch hop's definition and whether its stored row is enabled; None when unresolved."""
    found = resolve_agent_with_row(target, session_manager.db, project_id=project_id)
    if found is not None:
        body, row = found
        return body, row.enabled
    fallback = resolve_agent(target, session_manager.db, project_id=project_id)
    return None if fallback is None else (fallback, True)


def spawn_capabilities_allowed(
    session_manager: SessionManager | None,
    caller_ref: Any,
    agent: Any,
    project_path: Any = None,
    parent_session_id: Any = None,
    authority_capability: str | None = None,
) -> bool:
    """Whether an unbounded launch stays within caller capabilities.

    YAML selects this restriction and any operator exemption. Caller capabilities
    always come from its enabled stored definition, never the declared parent.
    The target lookup follows launch project precedence and the bounded fallback
    walk; unresolved identities fail closed. Finite spawn lists grant authority
    through the separately enforced target scope, including its fallback walk.
    YAML may name a stored launch authority; it never permits default or a
    fallback to default, even when that definition has no extra capabilities.
    """
    from gobby.workflows.condition_helpers_sessions import _FALLBACK_CHAIN_MAX_HOPS, _spawn_caller

    if session_manager is None:
        raise RuntimeError("Spawn capability comparison needs a session manager")
    caller, _ = _spawn_caller(session_manager, caller_ref)
    resolved = session_agent_definition(session_manager.db, caller)
    if resolved is None:
        return False
    project_id = _target_project_id(
        session_manager, project_path, parent_session_id, caller.project_id
    )
    if project_id is None:
        return False
    target = "default" if agent is None else agent
    if not isinstance(target, str) or not target:
        return False
    capabilities = set(resolved[0].capabilities)
    launch_authority = authority_capability is not None and authority_capability in capabilities
    spawn_scope = resolved[0].spawnable_agents
    compare_capabilities = (not spawn_scope or "*" in spawn_scope) and not launch_authority
    visited: set[str] = set()
    for _ in range(_FALLBACK_CHAIN_MAX_HOPS + 1):
        if launch_authority and target == "default":
            return False
        if target in visited:
            return True
        visited.add(target)
        hop = _resolve_hop(session_manager, target, project_id)
        if hop is None or not hop[1]:
            return False
        body = hop[0]
        if not body.admits_spawner(capabilities):
            return False
        if compare_capabilities and not set(body.capabilities) <= capabilities:
            return False
        if not body.fallback_agent:
            return True
        target = body.fallback_agent
    return False


def spawner_admitted(
    session_manager: SessionManager | None,
    caller_ref: Any,
    agent: Any,
    project_path: Any = None,
    parent_session_id: Any = None,
) -> bool:
    """Whether every hop of the launch admits the caller's capabilities.

    A target's spawner_capabilities binds every caller, including the unspawned
    operator and default sessions that the rule's operator exemption otherwise admits.
    A caller with no stored definition holds no capabilities. An unresolved project
    resolves its hops against global definitions, as the launch does. A hop that cannot
    be resolved declares no requirement here; spawn_capabilities_allowed refuses it for
    every caller it does not exempt. A chain too long to finish walking fails closed.
    """
    from gobby.workflows.condition_helpers_sessions import _FALLBACK_CHAIN_MAX_HOPS, _spawn_caller

    if session_manager is None:
        raise RuntimeError("Spawner admission needs a session manager")
    caller, _ = _spawn_caller(session_manager, caller_ref)
    stored = session_agent_definition(session_manager.db, caller)
    capabilities = set(stored[0].capabilities) if stored is not None else set()
    project_id = _target_project_id(
        session_manager, project_path, parent_session_id, caller.project_id
    )
    target = "default" if agent is None else agent
    if not isinstance(target, str) or not target:
        return True
    visited: set[str] = set()
    for _ in range(_FALLBACK_CHAIN_MAX_HOPS + 1):
        if target in visited:
            return True
        visited.add(target)
        hop = _resolve_hop(session_manager, target, project_id)
        if hop is None:
            return True
        body = hop[0]
        if not body.admits_spawner(capabilities):
            return False
        if not body.fallback_agent:
            return True
        target = body.fallback_agent
    return False
