"""Session activation reconciliation backstop."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from gobby.hooks.events import HookEvent

logger = logging.getLogger(__name__)

SESSION_ACTIVATION_CONTRACT_VERSION = 1
SESSION_ACTIVATION_INVARIANTS = (
    "platform_session_resolves",
    "agent_identity_variables",
    "agent_rule_skill_variables",
    "agent_tool_block_variables",
    "spawned_agent_variables",
    "step_workflow_instance",
    "baseline_dirty_tracking",
    "terminal_pickup_metadata",
)
SESSION_ACTIVATION_CONTRACT_HASH = hashlib.sha256(
    json.dumps(SESSION_ACTIVATION_INVARIANTS, separators=(",", ":")).encode()
).hexdigest()

MARKER_COMPLETED = "_session_activation_completed"
MARKER_VERSION = "_session_activation_contract_version"
MARKER_HASH = "_session_activation_contract_hash"

_AGENT_KEYS = (
    "_agent_type",
    "_active_rule_names",
    "_active_skill_names",
    "_skill_format",
    "_agent_blocked_tools",
    "_agent_blocked_mcp_tools",
    "is_spawned_agent",
)
_AGENT_RUN_ROW_KEYS = ("id", "workflow_name", "agent_name", "prompt")
_ACTIVE_RULE_NAMES_CACHE_TTL_SECONDS = 5.0
_ACTIVE_RULE_NAMES_CACHE_MAX_ENTRIES = 256
_ACTIVE_RULE_NAMES_CACHE: dict[tuple[str, str | None], tuple[float, set[str]]] = {}
_ACTIVE_RULE_NAMES_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True)
class ActivationReconciliationResult:
    """Outcome of a session activation reconciliation pass."""

    changed: bool
    missing: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class _AgentRunRecovery:
    id: str
    workflow_name: str | None
    agent_name: str | None
    prompt: str | None


def clear_active_rule_names_cache() -> None:
    """Clear cached active-rule selector resolution."""
    with _ACTIVE_RULE_NAMES_CACHE_LOCK:
        _ACTIVE_RULE_NAMES_CACHE.clear()


def _purge_expired_active_rule_names_cache(now: float) -> None:
    expired = [
        cache_key
        for cache_key, (cached_at, _) in _ACTIVE_RULE_NAMES_CACHE.items()
        if now - cached_at >= _ACTIVE_RULE_NAMES_CACHE_TTL_SECONDS
    ]
    for cache_key in expired:
        _ACTIVE_RULE_NAMES_CACHE.pop(cache_key, None)


def _evict_active_rule_names_cache_to_limit(*, incoming: int = 0) -> None:
    excess_count = len(_ACTIVE_RULE_NAMES_CACHE) + incoming - _ACTIVE_RULE_NAMES_CACHE_MAX_ENTRIES
    if excess_count <= 0:
        return
    oldest = sorted(
        _ACTIVE_RULE_NAMES_CACHE.items(),
        key=lambda item: item[1][0],
    )
    for cache_key, _ in oldest[:excess_count]:
        _ACTIVE_RULE_NAMES_CACHE.pop(cache_key, None)


def reconcile_session_activation(
    event: HookEvent,
    handler: Any,
    *,
    logger: logging.Logger | None = None,
) -> ActivationReconciliationResult:
    """Repair cheap durable activation invariants before rule evaluation.

    This is a fail-open backstop. It only writes durable state that is safe to
    merge idempotently and avoids replaying prompt/context side effects.
    """
    log = logger or logging.getLogger(__name__)
    try:
        return _reconcile_session_activation(event, handler, log)
    except Exception as exc:
        log.warning("Session activation reconciliation failed open: %s", exc, exc_info=True)
        return ActivationReconciliationResult(changed=False, reason=f"failed_open:{exc}")


def _reconcile_session_activation(
    event: HookEvent,
    handler: Any,
    log: logging.Logger,
) -> ActivationReconciliationResult:
    session_id = event.metadata.get("_platform_session_id")
    if not isinstance(session_id, str) or not session_id:
        return ActivationReconciliationResult(
            changed=False,
            missing=("_platform_session_id",),
            reason="missing_platform_session_id",
        )

    session_manager = getattr(handler, "_session_manager", None)
    if session_manager is None:
        return ActivationReconciliationResult(changed=False, reason="session_manager_unavailable")

    session = session_manager.get(session_id)
    if session is None or not isinstance(getattr(session, "id", None), str):
        return ActivationReconciliationResult(
            changed=False,
            missing=("stored_session",),
            reason="session_not_found",
        )

    db = getattr(session_manager, "db", None)
    if db is None:
        return ActivationReconciliationResult(changed=False, reason="database_unavailable")

    from gobby.workflows.state_manager import SessionVariableManager

    sv_mgr = SessionVariableManager(db)
    variables = sv_mgr.get_variables(session_id)
    missing = _missing_marker_keys(variables)

    agent_run = (
        _recover_agent_run(db, session, event)
        if missing or _needs_agent_run(session, variables, event)
        else None
    )
    refreshed = _backfill_terminal_pickup(session_manager, session, agent_run)
    if refreshed is not None and refreshed is not session:
        session = refreshed
        variables = sv_mgr.get_variables(session_id)
        missing.append("terminal_pickup_metadata")

    activation_missing = _missing_agent_keys(variables)
    missing.extend(activation_missing)

    step_missing = _missing_step_workflow(db, session_id, variables, session, agent_run)
    missing.extend(step_missing)

    if activation_missing or step_missing:
        override = _activation_agent_name(variables, agent_run, step_missing)
        if _activate_agent(handler, session_id, session, override, log):
            variables = sv_mgr.get_variables(session_id)
            step_missing = _missing_step_workflow(db, session_id, variables, session, agent_run)
            missing.extend(step_missing)

    updates = _fallback_agent_updates(variables, session)
    active_rule_updates = _active_rule_name_updates(db, variables, session)
    if active_rule_updates:
        updates.update(active_rule_updates)
        missing.append("_active_rule_names")
    missing.extend(_missing_baseline_keys(variables))
    updates.update(_baseline_updates(event, variables, log))
    updates.update(_step_completion_updates(variables))
    if _ensure_step_workflow_from_definition(db, session_id, variables, session):
        variables = sv_mgr.get_variables(session_id)
        missing = [m for m in missing if m != "step_workflow_instance"]

    updates.update(_marker_updates(variables))
    if updates:
        sv_mgr.merge_variables(session_id, updates)
        variables = {**variables, **updates}

    unresolved = tuple(
        dict.fromkeys(_missing_agent_keys(variables) + _missing_marker_keys(variables))
    )
    if unresolved:
        log.warning(
            "Session activation reconciliation left unresolved invariants for %s: %s",
            session_id,
            ", ".join(unresolved),
        )
    changed = bool(updates or missing)
    reason = "repaired" if changed else "current"
    return ActivationReconciliationResult(
        changed=changed,
        missing=tuple(dict.fromkeys(missing)),
        reason=reason,
    )


def _missing_marker_keys(variables: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if variables.get(MARKER_COMPLETED) is not True:
        missing.append(MARKER_COMPLETED)
    if variables.get(MARKER_VERSION) != SESSION_ACTIVATION_CONTRACT_VERSION:
        missing.append(MARKER_VERSION)
    if variables.get(MARKER_HASH) != SESSION_ACTIVATION_CONTRACT_HASH:
        missing.append(MARKER_HASH)
    return missing


def _missing_agent_keys(variables: dict[str, Any]) -> list[str]:
    return [key for key in _AGENT_KEYS if key not in variables]


def _missing_baseline_keys(variables: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if "baseline_dirty_files" not in variables:
        missing.append("baseline_dirty_files")
    if "session_edited_files" not in variables:
        missing.append("session_edited_files")
    return missing


def _marker_updates(variables: dict[str, Any]) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if variables.get(MARKER_COMPLETED) is not True:
        updates[MARKER_COMPLETED] = True
    if variables.get(MARKER_VERSION) != SESSION_ACTIVATION_CONTRACT_VERSION:
        updates[MARKER_VERSION] = SESSION_ACTIVATION_CONTRACT_VERSION
    if variables.get(MARKER_HASH) != SESSION_ACTIVATION_CONTRACT_HASH:
        updates[MARKER_HASH] = SESSION_ACTIVATION_CONTRACT_HASH
    return updates


def _fallback_agent_updates(variables: dict[str, Any], session: Any) -> dict[str, Any]:
    spawned = bool(getattr(session, "agent_run_id", None) or getattr(session, "agent_depth", 0))
    defaults: dict[str, Any] = {
        "_agent_type": "default",
        "_active_rule_names": None,
        "_active_skill_names": None,
        "_skill_format": None,
        "_agent_blocked_tools": [],
        "_agent_blocked_mcp_tools": [],
        "is_spawned_agent": spawned,
    }
    return {key: value for key, value in defaults.items() if key not in variables}


def _active_rule_name_updates(db: Any, variables: dict[str, Any], session: Any) -> dict[str, Any]:
    agent_name = variables.get("_agent_type")
    if not isinstance(agent_name, str) or not agent_name:
        return {}

    active_rules = _resolve_active_rule_names(
        db,
        agent_name,
        getattr(session, "project_id", None),
    )
    if active_rules is None:
        return {}

    current = variables.get("_active_rule_names")
    if isinstance(current, list) and set(current) == active_rules:
        return {}

    return {"_active_rule_names": sorted(active_rules)}


def _resolve_active_rule_names(
    db: Any,
    agent_name: str,
    project_id: str | None,
) -> set[str] | None:
    from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
    from gobby.workflows.definitions import AgentDefinitionBody
    from gobby.workflows.selectors import resolve_rules_for_agent

    cache_key = (agent_name, project_id)
    now = time.monotonic()
    with _ACTIVE_RULE_NAMES_CACHE_LOCK:
        cached = _ACTIVE_RULE_NAMES_CACHE.get(cache_key)
        if cached is not None:
            cached_at, active_rules = cached
            if now - cached_at < _ACTIVE_RULE_NAMES_CACHE_TTL_SECONDS:
                return set(active_rules)

    manager = LocalWorkflowDefinitionManager(db)
    row = manager.get_by_name(agent_name, project_id=project_id)
    if row is None or row.workflow_type != "agent" or not row.definition_json:
        return None

    try:
        data = json.loads(row.definition_json)
        if isinstance(data, dict):
            data.setdefault("name", row.name)
        agent = AgentDefinitionBody.model_validate(data)
    except TypeError as exc:
        # json.loads plus Pydantic validation should report data issues through
        # JSONDecodeError/ValidationError. TypeError here signals an unexpected
        # programming or schema regression, so keep it visible while failing open.
        logger.error(
            "Unexpected TypeError refreshing active rules for agent %s via "
            "json.loads/AgentDefinitionBody.model_validate: %s",
            agent_name,
            exc,
            exc_info=True,
        )
        return None
    except (json.JSONDecodeError, KeyError, ValidationError) as exc:
        logger.debug(
            "Failed to refresh active rules for agent %s: %s",
            agent_name,
            exc,
            exc_info=True,
        )
        return None

    rules = manager.list_all(project_id=project_id, workflow_type="rule", enabled=True)
    active_rules = resolve_rules_for_agent(agent, rules)
    now = time.monotonic()
    with _ACTIVE_RULE_NAMES_CACHE_LOCK:
        _purge_expired_active_rule_names_cache(now)
        incoming = 0 if cache_key in _ACTIVE_RULE_NAMES_CACHE else 1
        _evict_active_rule_names_cache_to_limit(incoming=incoming)
        _ACTIVE_RULE_NAMES_CACHE[cache_key] = (now, set(active_rules))
    return active_rules


def _baseline_updates(
    event: HookEvent,
    variables: dict[str, Any],
    log: logging.Logger,
) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if "baseline_dirty_files" not in variables:
        try:
            from gobby.workflows.git_utils import get_dirty_files_categorized

            project_path = _project_path(event)
            updates["baseline_dirty_files"] = sorted(get_dirty_files_categorized(project_path).all)
        except Exception as exc:
            log.debug("Could not initialize baseline dirty files: %s", exc)
            updates["baseline_dirty_files"] = []
    if "session_edited_files" not in variables:
        updates["session_edited_files"] = []
    return updates


def _step_completion_updates(variables: dict[str, Any]) -> dict[str, Any]:
    if (
        isinstance(variables.get("_step_workflow_name"), str)
        and "step_workflow_complete" not in variables
    ):
        return {"step_workflow_complete": False}
    return {}


def _project_path(event: HookEvent) -> str | None:
    for value in (
        event.data.get("cwd"),
        event.cwd,
        event.metadata.get("project_path"),
        event.data.get("project_path"),
    ):
        if isinstance(value, str) and value:
            return value
    return None


def _recover_agent_run(db: Any, session: Any, event: HookEvent) -> _AgentRunRecovery | None:
    run_id = getattr(session, "agent_run_id", None) or _terminal_context_value(
        event,
        "agent_run_id",
        "gobby_agent_run_id",
    )
    if isinstance(run_id, str) and run_id:
        row = db.fetchone(
            "SELECT id, workflow_name, agent_name, prompt FROM agent_runs WHERE id = %s",
            (run_id,),
        )
        return _agent_run_from_row(row)

    row = db.fetchone(
        """
        SELECT id, workflow_name, agent_name, prompt
        FROM agent_runs
        WHERE child_session_id = %s OR claimed_session_id = %s
        ORDER BY updated_at DESC, created_at DESC
        LIMIT 1
        """,
        (session.id, session.id),
    )
    return _agent_run_from_row(row)


def _agent_run_from_row(row: Any) -> _AgentRunRecovery | None:
    if not row:
        return None
    try:
        values = dict(row)
    except (TypeError, ValueError):
        try:
            values = {key: row[key] for key in _AGENT_RUN_ROW_KEYS}
        except (KeyError, IndexError, TypeError):
            return None
    if any(key not in values for key in _AGENT_RUN_ROW_KEYS):
        return None
    run_id = values["id"]
    if not isinstance(run_id, str) or not run_id:
        return None
    workflow_name = values["workflow_name"]
    agent_name = values["agent_name"]
    prompt = values["prompt"]
    if not all(
        isinstance(value, str) or value is None for value in (workflow_name, agent_name, prompt)
    ):
        return None
    return _AgentRunRecovery(
        id=run_id,
        workflow_name=workflow_name,
        agent_name=agent_name,
        prompt=prompt,
    )


def _terminal_context_value(event: HookEvent, *keys: str) -> str | None:
    terminal_context = event.data.get("terminal_context")
    if not isinstance(terminal_context, dict):
        return None
    for key in keys:
        value = terminal_context.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _needs_agent_run(session: Any, variables: dict[str, Any], event: HookEvent) -> bool:
    return bool(
        getattr(session, "agent_run_id", None)
        or getattr(session, "agent_depth", 0)
        or variables.get("is_spawned_agent")
        or _terminal_context_value(event, "agent_run_id", "gobby_agent_run_id")
    )


def _backfill_terminal_pickup(
    session_manager: Any,
    session: Any,
    agent_run: _AgentRunRecovery | None,
) -> Any:
    if agent_run is None or not hasattr(session_manager, "update_terminal_pickup_metadata"):
        return session
    workflow_name = None if getattr(session, "workflow_name", None) else agent_run.workflow_name
    agent_run_id = None if getattr(session, "agent_run_id", None) else agent_run.id
    original_prompt = None if getattr(session, "original_prompt", None) else agent_run.prompt
    if not workflow_name and not agent_run_id and not original_prompt:
        return session
    return session_manager.update_terminal_pickup_metadata(
        session.id,
        workflow_name=workflow_name,
        agent_run_id=agent_run_id,
        original_prompt=original_prompt,
    )


def _activation_agent_name(
    variables: dict[str, Any],
    agent_run: _AgentRunRecovery | None,
    missing: list[str],
) -> str | None:
    agent_type = variables.get("_agent_type")
    if isinstance(agent_type, str) and agent_type and agent_type != "default":
        return agent_type
    if agent_run and agent_run.agent_name:
        return agent_run.agent_name
    step_name = variables.get("_step_workflow_name")
    if isinstance(step_name, str) and step_name.endswith("-steps"):
        return step_name[: -len("-steps")]
    for item in missing:
        if item.endswith("-steps"):
            return item[: -len("-steps")]
    return None


def _activate_agent(
    handler: Any,
    session_id: str,
    session: Any,
    agent_name_override: str | None,
    log: logging.Logger,
) -> bool:
    activate = getattr(handler, "_activate_default_agent", None)
    if not callable(activate):
        return False
    try:
        activate(
            session_id,
            getattr(session, "source", None) or "claude",
            getattr(session, "project_id", None),
            agent_name_override=agent_name_override,
        )
        return True
    except Exception as exc:
        log.warning("Could not repair session agent activation for %s: %s", session_id, exc)
        return False


def _missing_step_workflow(
    db: Any,
    session_id: str,
    variables: dict[str, Any],
    session: Any,
    agent_run: _AgentRunRecovery | None,
) -> list[str]:
    spawned = bool(variables.get("is_spawned_agent") or getattr(session, "agent_run_id", None))
    if not spawned:
        return []

    step_name = variables.get("_step_workflow_name")
    if not isinstance(step_name, str) or not step_name:
        if (
            agent_run
            and agent_run.agent_name
            and _workflow_definition_exists(db, agent_run.agent_name)
        ):
            return [f"{agent_run.agent_name}-steps"]
        return []

    from gobby.workflows.state_manager import WorkflowInstanceManager

    if WorkflowInstanceManager(db).get_instance(session_id, step_name) is None:
        return ["step_workflow_instance"]
    if "step_workflow_complete" not in variables:
        return ["step_workflow_complete"]
    return []


def _workflow_definition_exists(db: Any, agent_name: str) -> bool:
    from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

    return LocalWorkflowDefinitionManager(db).get_by_name(f"{agent_name}-steps") is not None


def _ensure_step_workflow_from_definition(
    db: Any,
    session_id: str,
    variables: dict[str, Any],
    session: Any,
) -> bool:
    step_name = variables.get("_step_workflow_name")
    if not isinstance(step_name, str) or not step_name:
        return False

    from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
    from gobby.workflows.definitions import WorkflowInstance
    from gobby.workflows.state_manager import WorkflowInstanceManager

    instance_mgr = WorkflowInstanceManager(db)
    if instance_mgr.get_instance(session_id, step_name) is not None:
        return False

    row = LocalWorkflowDefinitionManager(db).get_by_name(
        step_name,
        project_id=getattr(session, "project_id", None),
    )
    if row is None:
        return False
    try:
        definition = json.loads(row.definition_json)
    except (json.JSONDecodeError, TypeError):
        return False
    steps = definition.get("steps") or []
    first = steps[0] if steps else {}
    first_step = first.get("name") if isinstance(first, dict) else None
    if not first_step:
        return False

    instance_mgr.save_instance(
        WorkflowInstance(
            id=str(uuid.uuid4()),
            session_id=session_id,
            workflow_name=step_name,
            enabled=True,
            priority=10,
            current_step=first_step,
            step_entered_at=datetime.now(UTC),
            variables=definition.get("variables") or {},
        )
    )
    return True
