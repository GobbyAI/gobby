"""Claim-authority guard for cross-session task mutations (#20821).

Interim MCP-layer enforcement of the task-transition authority audit
(docs/design/task-authority-audit.md). Storage APIs stay open for daemon
internals; this guards the agent boundary only.
"""

import logging
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.tasks._errors import TaskToolErrorCode, task_error
from gobby.storage.sessions import SessionManager
from gobby.tasks.lifecycle_access import is_operator_coordinator
from gobby.tasks.state_semantics import get_claimed_session_id
from gobby.utils.session_context import get_current_session_id
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


def is_orchestrator_coordinator(task_manager: "LocalTaskManager", caller_session_id: str) -> bool:
    """True when the caller is the enabled Orchestrator's local root session.

    Josh, 2026-10-21: the Orchestrator is exempt from claim refusals. The
    Assistant, though also an operator coordinator, is not. Lookup failures
    fail closed.
    """
    try:
        caller = SessionManager(task_manager.db).get(caller_session_id)
        if caller is None:
            return False
        role = SessionVariableManager(task_manager.db).get_variable_subset(
            caller.id, ("_agent_type",)
        )
        if role.get("_agent_type") != "orchestrator":
            return False
        return is_operator_coordinator(task_manager, caller)
    except Exception as e:
        logger.debug("Orchestrator authority lookup failed: %s", e)
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
    if is_orchestrator_coordinator(task_manager, caller_session_id):
        return None
    return task_error(
        f"Cannot {action}: task {task_ref} is claimed by session '{owner_session_id}'.",
        TaskToolErrorCode.TASK_CLAIM_CONFLICT,
        claimed_by=owner_session_id,
        message=SANCTIONED_PATH,
    )


CONSOLIDATOR_ROLE = "consolidator"
# The Consolidator applies Orchestrator-directed folds (superseded) and retirements (moot) only.
CONSOLIDATOR_CLOSE_REASONS = frozenset({"moot", "superseded"})
# Josh's operator subtrees: #22978 Triage, and #22225 Annotate inside it.
OPERATOR_SUBTREE_SEQ_NUMS = frozenset({22978, 22225})


def session_agent_type(db: "HubDatabase", session_id: str) -> str | None:
    """The ``_agent_type`` the session was launched with, or None."""
    role = SessionVariableManager(db).get_variable_subset(session_id, ("_agent_type",))
    return role.get("_agent_type")


def _in_operator_subtree(task_manager: "LocalTaskManager", task: "Task") -> bool:
    """True when the task or one of its ancestors is an operator subtree root."""
    seen: set[str] = set()
    node: Task | None = task
    while node is not None and node.id not in seen:
        if node.seq_num in OPERATOR_SUBTREE_SEQ_NUMS:
            return True
        seen.add(node.id)
        node = task_manager.get_task(node.parent_task_id) if node.parent_task_id else None
    return False


def consolidator_protection(
    task_manager: "LocalTaskManager", task: "Task", caller_session_id: str
) -> str | None:
    """Why the Consolidator may not edit or close ``task``; None when it may.

    Josh, 2026-10-27: the Consolidator leaves alone tasks another session claims,
    tasks labeled deferred-by-josh, and the #22978 and #22225 operator subtrees.
    """
    ref = f"#{task.seq_num}" if task.seq_num else task.id
    owner = get_claimed_session_id(task)
    if owner and owner != caller_session_id:
        return f"task {ref} is claimed by session '{owner}'"
    if "deferred-by-josh" in (task.labels or []):
        return f"task {ref} is labeled deferred-by-josh"
    if _in_operator_subtree(task_manager, task):
        return f"task {ref} sits under an operator subtree (#22978 or #22225)"
    return None


def consolidator_close_refusal(
    task_manager: "LocalTaskManager", task: "Task", reason: str, caller_session_id: str
) -> tuple[str, str] | None:
    """The (code, message) refusal when the Consolidator may not close ``task`` as ``reason``."""
    if reason.casefold() not in CONSOLIDATOR_CLOSE_REASONS:
        return (
            "consolidator_close_reason_refused",
            f"The Consolidator closes a task only as superseded or moot, not {reason}.",
        )
    protected = consolidator_protection(task_manager, task, caller_session_id)
    if protected:
        return (
            "consolidator_task_protected",
            f"The Consolidator may not close this task: {protected}.",
        )
    return None


def consolidator_update_error(task_manager: "LocalTaskManager", task: "Task") -> str | None:
    """Refusal text when the calling session is a Consolidator and may not edit ``task``."""
    caller = get_current_session_id()
    if not caller or session_agent_type(task_manager.db, caller) != CONSOLIDATOR_ROLE:
        return None
    protected = consolidator_protection(task_manager, task, caller)
    return f"The Consolidator may not update this task: {protected}." if protected else None
