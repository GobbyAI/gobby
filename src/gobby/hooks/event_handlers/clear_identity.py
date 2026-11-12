"""Semantic identity reset at confirmed operator clear boundaries."""

import asyncio
import logging
from typing import Any

from gobby.storage.hub.protocol import AgentStepInstanceMutation, HubDatabase
from gobby.workflows.reserved_variables import RESERVED_WORKFLOW_VARIABLES
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.step_instances import AgentStepInstanceManager
from gobby.workflows.task_claim_state import TASK_EDIT_LEDGER_KEYS
from gobby.workflows.variable_defaults import load_variable_defaults, resolve_session_project_id

CLEAR_IDENTITY_VARIABLE = "_clear_identity_reset"
logger = logging.getLogger(__name__)


def stop_spawned_clear(session: Any) -> bool:
    """Schedule the existing cancellation lifecycle; a spawned clear has no successor.

    Hook workers cannot wait for teardown of the process sending their hook. The
    daemon loop owns cancellation and its shielded terminal delivery instead.
    """
    run_id = session.agent_run_id
    depth = getattr(session, "agent_depth", 0)
    spawner = getattr(session, "spawned_by_agent_id", None)
    if not (
        (isinstance(run_id, str) and run_id)
        or (isinstance(depth, int) and depth > 0)
        or (isinstance(spawner, str) and spawner)
    ):
        return False
    from gobby.app_context import get_app_context

    services = get_app_context()
    if services is None or services.agent_runner is None or services.main_loop is None:
        raise RuntimeError("Spawned clear requires the agent cancellation service")
    if not isinstance(run_id, str) or not run_id:
        raise RuntimeError("Spawned clear has no bound agent run")
    from gobby.agents.kill import kill_agent
    from gobby.mcp_proxy.tools.agent_cancellation import stop_agent_run
    from gobby.mcp_proxy.tools.agents_termination import _cleanup_terminal_artifacts
    from gobby.storage.agents import LocalAgentRunManager

    async def stop() -> None:
        try:
            result = await stop_agent_run(
                run_id=run_id,
                runner=services.agent_runner,
                agent_run_manager=LocalAgentRunManager(services.database),
                db=services.database,
                lifecycle_monitor=services.agent_lifecycle_monitor,
                completion_registry=services.completion_registry,
                task_manager=services.task_manager,
                session_manager=services.session_manager,
                kill_agent_process=kill_agent,
                cleanup_terminal_artifacts=_cleanup_terminal_artifacts,
            )
            if not result.get("success"):
                logger.warning("Spawned clear cancellation for %s: %s", run_id, result)
        except Exception:
            logger.exception("Spawned clear cancellation failed for %s", run_id)

    asyncio.run_coroutine_threadsafe(stop(), services.main_loop)
    return True


def reset_operator_identity(db: HubDatabase, session_id: str) -> None:
    """Remove definition-owned state while preserving provider and task identity.

    The reset marker prevents ambient recovery from selecting a configured previous
    role. Explicit non-default activation clears the marker. It is not a completion
    marker: a persona overlay may have changed semantic state since the last clear.
    """
    manager = SessionVariableManager(db)
    with db.transaction_immediate(AgentStepInstanceMutation(session_id=session_id)):
        current = manager.get_variables(session_id)
        project_id = resolve_session_project_id(db, session_id)
        defaults = load_variable_defaults(db, project_id)
        semantic = set(current.get("_agent_definition_explicit_keys") or [])
        # Activation's key inventory also includes initialized variable defaults.
        # Those are mutable runtime state, except values explicitly set by this role.
        protected = (
            (set(defaults) - semantic)
            | set(TASK_EDIT_LEDGER_KEYS)
            | RESERVED_WORKFLOW_VARIABLES
            | {"task_claimed", "claimed_tasks", "active_task_id"}
        )
        owned = set(current.get("_agent_definition_keys") or []) - protected
        owned.update(
            key
            for key in current
            if key.startswith(("_persona_", "_agent_definition_", "_agent_workspace_"))
        )
        AgentStepInstanceManager(db).delete_for_session(session_id)
        manager.merge_variables(
            session_id,
            {
                **{key: defaults.get(key) for key in owned},
                "_agent_type": "default",
                "_agent_definition_keys": [],
                "_agent_definition_hash": None,
                "_active_rule_names": [],
                "_active_skill_names": None,
                "_skill_format": None,
                "_agent_blocked_tools": [],
                "_agent_blocked_mcp_tools": [],
                "_agent_context_injected": False,
                "_standing_directives_injected": False,
                "_agent_identity_reinject": True,
                "is_spawned_agent": False,
                "step_workflow_complete": None,
                CLEAR_IDENTITY_VARIABLE: True,
            },
        )
