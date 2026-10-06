"""Session-scope condition helpers bound to the rule engine's session manager."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.terminals.actor_scope import SESSION_ACTOR_PREFIX, ActorScopeError, resolve_actor_scope
from gobby.workflows.agent_models import AgentDefinitionBody
from gobby.workflows.agent_resolver import resolve_agent

if TYPE_CHECKING:
    from gobby.storage.session_models import Session
    from gobby.storage.sessions import SessionManager


def send_keys_target_in_scope(
    session_manager: SessionManager | None, caller_ref: Any, target_ref: Any
) -> bool:
    """False only when a resolvable caller provably cannot reach a terminal tool's target.

    It scopes ``send_keys`` and ``capture_output``. The caller and target resolve
    exactly as ``send_keys`` resolves them, and the decision is ``ActorScope.admits``:
    the caller's own session, its project, and its agent tree in either direction.
    A missing, unresolvable or autonomous caller, and an unresolvable target, pass
    here. ``send_keys`` refuses each with its own error. For ``capture_output``, a
    request with no caller is the operator's, agent enforcement refuses autonomous
    callers before rules run, and the target lookup takes a session UUID, which
    resolves here by primary key. Any other failure raises, and a raising block
    condition fails closed.
    """
    if session_manager is None:
        raise RuntimeError("send_keys target scope needs a session manager")
    if not isinstance(caller_ref, str) or not caller_ref:
        return True
    if not isinstance(target_ref, str) or not target_ref:
        return True
    try:
        scope = resolve_actor_scope(session_manager, f"{SESSION_ACTOR_PREFIX}{caller_ref}")
    except ActorScopeError:
        return True
    caller = scope.caller
    if caller is None:
        return True
    try:
        target_id = session_manager.resolve_session_reference(target_ref, caller.project_id)
    except ValueError:
        return True
    target = session_manager.get(target_id)
    if target is None:
        return True
    return scope.admits(project_id=target.project_id, session_id=target_id)


def send_message_target_allowed(
    session_manager: SessionManager | None, caller_ref: Any, agent_type: Any, target: Any
) -> bool:
    """Whether the caller's agent definition admits this send_message target mode.

    A session that is not a spawned agent may use every mode. A spawned agent may
    use only the modes in its definition's ``send_message_targets``; an omitted
    target is ``parent``. An unknown caller or definition raises, and a raising
    block condition fails closed.
    """
    if session_manager is None:
        raise RuntimeError("send_message target modes need a session manager")
    if not isinstance(caller_ref, str) or not caller_ref:
        raise ValueError("send_message caller is unknown")
    caller = session_manager.get(session_manager.resolve_session_reference(caller_ref))
    if caller is None:
        raise ValueError(f"send_message caller {caller_ref} is not registered")
    if not caller.agent_run_id and caller.agent_depth == 0:
        return True
    if not isinstance(agent_type, str) or not agent_type:
        raise ValueError(f"spawned session {caller.id} has no agent definition")
    row = AgentDefinitionManager(session_manager.db).get_by_name(agent_type, caller.project_id)
    if row is None:
        raise ValueError(f"agent definition {agent_type} is not registered")
    body = AgentDefinitionBody.model_validate({"name": row.name, **row.definition_json})
    return (target or "parent") in body.send_message_targets


# The ``agent`` defaults of the gobby-agents spawn tools, for calls that omit it.
_SPAWN_TOOL_DEFAULT_AGENT = {"spawn_agent": "default", "dispatch_batch": "developer"}
# spawn_agent walks at most this many fallback_agent hops (spawn_agent/_factory.py).
_FALLBACK_CHAIN_MAX_HOPS = 5


def _spawn_targets(tool_name: str, agent: Any, suggestions: Any) -> list[str]:
    """The agent each spawn of the call starts, before any fallback.

    A dispatch_batch suggestion's own non-blank ``agent`` overrides the
    top-level one, as dispatch_batch resolves it.
    """
    requested = agent or _SPAWN_TOOL_DEFAULT_AGENT[tool_name]
    if tool_name != "dispatch_batch":
        return [requested]
    if not isinstance(suggestions, list) or not all(isinstance(s, dict) for s in suggestions):
        raise ValueError("dispatch_batch suggestions must be a list of objects")
    targets: list[str] = []
    for suggestion in suggestions:
        own = suggestion.get("agent")
        targets.append(own.strip() if isinstance(own, str) and own.strip() else requested)
    return targets


def _fallback_chain(target: str, db: Any, project_id: str | None) -> list[str]:
    """``target`` and every fallback agent spawn_agent may start in its place."""
    chain = [target]
    body = resolve_agent(target, db, project_id=project_id)
    for _ in range(_FALLBACK_CHAIN_MAX_HOPS):
        candidate = None if body is None else body.fallback_agent
        if not candidate or candidate in chain:
            break
        body = resolve_agent(candidate, db, project_id=project_id)
        if body is None:
            break
        chain.append(candidate)
    return chain


def _spawn_caller(session_manager: SessionManager, caller_ref: Any) -> tuple[Session, str | None]:
    """The verified caller session and its agent run's definition name, None for a root.

    A root session has no agent run and depth 0. A spawned caller's name comes from
    its agent run record, never from a caller-supplied parent. A run-less session
    below the root, a missing run, or a run that names no definition raises.
    """
    if not isinstance(caller_ref, str) or not caller_ref:
        raise ValueError("spawn scope needs the caller session")
    caller = session_manager.get(session_manager.resolve_session_reference(caller_ref))
    if caller is None:
        raise ValueError(f"Caller session {caller_ref} not found")
    if caller.agent_run_id is None and caller.agent_depth == 0:
        return caller, None
    if caller.agent_run_id is None:
        raise ValueError(f"Spawned session {caller.id} has no agent run")
    run = LocalAgentRunManager(session_manager.db).get(caller.agent_run_id)
    if run is None or not run.agent_name:
        raise ValueError(f"Agent run {caller.agent_run_id} names no agent definition")
    return caller, run.agent_name


def spawn_target_allowed(
    session_manager: SessionManager | None,
    caller_ref: Any,
    tool_name: Any,
    agent: Any,
    suggestions: Any = None,
) -> bool:
    """Whether the caller may start every agent the gobby-agents ``tool_name`` call can.

    A root session (no agent run, depth 0) spawns anything. A spawned caller's
    definition comes from its agent run record, and its ``spawnable_agents``
    decides: any agent, the listed agents, or none. Every effective target must
    be allowed: each dispatch_batch suggestion's agent and every agent in a
    target's fallback_agent chain. Anything unresolvable raises, and a raising
    block condition fails closed.
    """
    if session_manager is None:
        raise RuntimeError("spawn target scope needs a session manager")
    caller, agent_name = _spawn_caller(session_manager, caller_ref)
    if agent_name is None:
        return True
    body = resolve_agent(agent_name, session_manager.db, project_id=caller.project_id)
    if body is None:
        raise ValueError(f"Agent definition {agent_name!r} not found")
    return all(
        body.may_spawn(name)
        for target in _spawn_targets(tool_name, agent, suggestions)
        for name in _fallback_chain(target, session_manager.db, caller.project_id)
    )


# Spawned definitions that may choose a spawn's network profile; a root session always may.
_NETWORK_OVERRIDE_AGENTS = frozenset({"default", "orchestrator"})


def network_override_allowed(session_manager: SessionManager | None, caller_ref: Any) -> bool:
    """Whether the caller may pass an explicit ``network`` profile to a spawn.

    A root session may. A spawned caller may only when its agent run names
    ``default`` or ``orchestrator``; other spawned callers still spawn with the
    inherited profile. Identity comes from the verified caller session, never a
    caller-supplied ``parent_session_id``. Anything unresolvable raises, and a
    raising block condition fails closed.
    """
    if session_manager is None:
        raise RuntimeError("network override scope needs a session manager")
    _, agent_name = _spawn_caller(session_manager, caller_ref)
    return agent_name is None or agent_name in _NETWORK_OVERRIDE_AGENTS


def session_condition_helpers(
    session_manager: SessionManager | None,
) -> dict[str, Callable[..., Any]]:
    """Bind the session-scope helpers to the evaluator's session manager."""
    return {
        "send_keys_target_in_scope": lambda caller_ref, target_ref: send_keys_target_in_scope(
            session_manager, caller_ref, target_ref
        ),
        "send_message_target_allowed": lambda caller_ref, agent_type, target: (
            send_message_target_allowed(session_manager, caller_ref, agent_type, target)
        ),
        "spawn_target_allowed": lambda caller_ref, tool_name, agent, suggestions=None: (
            spawn_target_allowed(session_manager, caller_ref, tool_name, agent, suggestions)
        ),
        "network_override_allowed": lambda caller_ref: network_override_allowed(
            session_manager, caller_ref
        ),
    }
