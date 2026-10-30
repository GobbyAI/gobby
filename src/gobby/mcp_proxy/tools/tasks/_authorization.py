"""Claim-authority guard for cross-session task mutations (#20821).

Interim MCP-layer enforcement of the task-transition authority audit
(docs/design/task-authority-audit.md). Storage APIs stay open for daemon
internals; this guards the agent boundary only.
"""

import logging
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.tasks._errors import TaskToolErrorCode, task_error
from gobby.storage.sessions import SessionManager
from gobby.tasks.lifecycle_access import is_operator_coordinator, task_protection_refusal
from gobby.tasks.state_semantics import get_claimed_session_id
from gobby.utils.session_context import get_current_session_id
from gobby.workflows.agent_resolver import is_operator_session, session_agent_definition
from gobby.workflows.state_manager import SessionVariableManager

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.tasks import LocalTaskManager, Task

logger = logging.getLogger(__name__)

SANCTIONED_PATH = (
    "Message the owner via gobby-agents:send_message; claim_task if the claim is stale."
)


def has_delegated_agent_run(
    db: "HubDatabase",
    *,
    caller_session_id: str,
    task_id: str,
    owner_session_id: str | None,
) -> bool:
    """True when an active agent run links caller and owner for this task.

    Checks the task's delegation lineage in either direction: the caller is a
    child spawned by the owner, or the parent that spawned the owner.
    """
    if not owner_session_id or caller_session_id == owner_session_id:
        return False

    try:
        row = db.fetchone(
            """
            SELECT id FROM agent_runs
            WHERE task_id = %s
              AND status IN ('pending', 'running')
              AND ((child_session_id = %s AND parent_session_id = %s)
                OR (child_session_id = %s AND parent_session_id = %s))
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (
                task_id,
                caller_session_id,
                owner_session_id,
                owner_session_id,
                caller_session_id,
            ),
        )
    except Exception as e:
        logger.debug("Delegated claim lookup failed: %s", e)
        return False

    try:
        run_id = row["id"] if row is not None else None
    except (KeyError, TypeError, IndexError):
        return False
    return isinstance(run_id, str) and bool(run_id)


def can_manage_task_claims(task_manager: "LocalTaskManager", caller_session_id: str) -> bool:
    """Check the installed claim-management grant on a local root; fail closed."""
    try:
        caller = SessionManager(task_manager.db).get(caller_session_id)
        if caller is None:
            return False
        return is_operator_coordinator(task_manager, caller, capability="manage_task_claims")
    except Exception as e:
        logger.debug("Claim-management authority lookup failed: %s", e)
        return False


def require_claim_authority(
    task_manager: "LocalTaskManager",
    task: "Task",
    action: str,
) -> dict[str, Any] | None:
    """Refuse a mutation of a task claimed by another session.

    Returns None when authorized: the task is unclaimed, the caller is the
    claiming session, caller and owner share the task's agent-run
    delegation lineage, or the caller is the Orchestrator. A claimed task
    with no session context is refused; unclaimed tasks stay open to
    operator and scripted flows that run without session context.
    """
    owner_session_id = get_claimed_session_id(task)
    if not owner_session_id:
        return None

    task_ref = f"#{task.seq_num}" if task.seq_num else task.id
    caller_session_id = get_current_session_id()
    if not caller_session_id:
        return task_error(
            f"Cannot {action}: task {task_ref} is claimed by session "
            f"'{owner_session_id}' and no session context is available.",
            TaskToolErrorCode.SESSION_REQUIRED,
            claimed_by=owner_session_id,
        )
    if caller_session_id == owner_session_id:
        return None
    if has_delegated_agent_run(
        task_manager.db,
        caller_session_id=caller_session_id,
        task_id=task.id,
        owner_session_id=owner_session_id,
    ):
        return None
    if can_manage_task_claims(task_manager, caller_session_id):
        return None
    return task_error(
        f"Cannot {action}: task {task_ref} is claimed by session '{owner_session_id}'.",
        TaskToolErrorCode.TASK_CLAIM_CONFLICT,
        claimed_by=owner_session_id,
        message=SANCTIONED_PATH,
    )


def task_mutation_error(task_manager: "LocalTaskManager", task: "Task") -> str | None:
    """Apply the caller definition's mutation policy to a task or proposed parent."""
    caller = get_current_session_id()
    if not caller:
        return None
    session = SessionManager(task_manager.db).get(caller)
    if session is None:
        return "Task mutation requires an active session."
    role = SessionVariableManager(task_manager.db).get_variable_subset(caller, ("_agent_type",))
    if is_operator_session(session, role.get("_agent_type")):
        return None
    definition = session_agent_definition(task_manager.db, session)
    if definition is None:
        return "Task mutation requires an enabled, valid active definition."
    if not definition[0].respect_task_protections:
        return None
    try:
        protected = task_protection_refusal(task_manager, task, caller)
    except (OSError, ValueError) as exc:
        return f"Task protection policy unavailable: {exc}"
    return f"Task mutation refused: {protected}." if protected else None
