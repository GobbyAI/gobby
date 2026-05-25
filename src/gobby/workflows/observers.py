"""Observer detection functions for task claims, plan mode, and MCP call tracking.

This module remains the compatibility surface for callers that import observer
helpers from ``gobby.workflows.observers``. Focused observer implementations live
in sibling ``observer_*`` modules.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from gobby.sessions.handoff_identity import sessions_have_continuous_terminal_context
from gobby.tasks.state_semantics import (
    ACTIVE_STAGE_STATES,
    get_claimed_session_id,
    is_task_actively_claimed,
)
from gobby.workflows.observer_commits import (
    _is_git_commit_command,
    _looks_like_commit_success,
    detect_bash_commit,
    detect_commit_link,
)
from gobby.workflows.observer_mcp import (
    _extract_loaded_skill_name,
    _track_mcp_call,
    detect_mcp_call,
)
from gobby.workflows.observer_plan_mode import compute_mode_level, detect_plan_mode_from_context
from gobby.workflows.observer_utils import (
    _extract_shell_command,
    _extract_shell_output_text,
    _json_safe,
    _shell_tool_succeeded,
)
from gobby.workflows.observer_verification import detect_verification_evidence

if TYPE_CHECKING:
    from gobby.hooks.events import HookEvent
    from gobby.storage.session_tasks import SessionTaskManager
    from gobby.storage.sessions import SessionManager
    from gobby.storage.tasks import LocalTaskManager

logger = logging.getLogger(__name__)

__all__ = [
    "_extract_loaded_skill_name",
    "_extract_shell_command",
    "_extract_shell_output_text",
    "_is_git_commit_command",
    "_json_safe",
    "_looks_like_commit_success",
    "_shell_tool_succeeded",
    "_track_mcp_call",
    "compute_mode_level",
    "detect_bash_commit",
    "detect_commit_link",
    "detect_mcp_call",
    "detect_plan_mode_from_context",
    "detect_task_claim",
    "detect_verification_evidence",
    "reconcile_claimed_tasks",
]

_UNRESOLVED_CLOSE_REF_LOG_THRESHOLD = 10
_unresolved_close_ref_count = 0
_unresolved_close_ref_lock = threading.Lock()


def _record_unresolved_close_ref(session_id: str, task_ref: object) -> None:
    """Track unresolved close_task refs without logging every occurrence."""
    global _unresolved_close_ref_count

    with _unresolved_close_ref_lock:
        _unresolved_close_ref_count += 1
        if _unresolved_close_ref_count % _UNRESOLVED_CLOSE_REF_LOG_THRESHOLD != 0:
            return
        count = _unresolved_close_ref_count
    logger.debug(
        "Unresolved close_task refs reached %s",
        count,
        extra={
            "unresolved_close_ref_count": count,
            "latest_ref": task_ref,
            "session_id": session_id,
        },
    )


def _claimed_task_id_for_ref(variables: dict[str, Any], task_ref: object) -> str | None:
    """Return a claimed task UUID already tracked for *task_ref*, if any."""
    raw_ref = str(task_ref)
    aliases = {raw_ref}
    if raw_ref.isdigit():
        aliases.add(f"#{raw_ref}")
    claimed_tasks = variables.get("claimed_tasks") or {}
    if not isinstance(claimed_tasks, dict):
        return None
    if raw_ref in claimed_tasks:
        return raw_ref
    for task_id, display_ref in claimed_tasks.items():
        if str(display_ref) in aliases:
            return str(task_id)
    return None


def detect_task_claim(
    event: HookEvent,
    variables: dict[str, Any],
    session_id: str,
    session_task_manager: SessionTaskManager | None = None,
    task_manager: LocalTaskManager | None = None,
    project_id: str | None = None,
) -> None:
    """Detect gobby-tasks calls that claim or release a task for this session."""
    if not event.data:
        return

    tool_input = event.data.get("tool_input", {}) or {}
    tool_output = event.data.get("tool_output") or {}

    server_name = event.data.get("mcp_server", "")
    if server_name != "gobby-tasks":
        return

    inner_tool_name = event.data.get("mcp_tool", "")

    if inner_tool_name == "close_task":
        if not tool_output:
            return
        if isinstance(tool_output, dict):
            if tool_output.get("error") or tool_output.get("status") == "error":
                return
            result = tool_output.get("result", {})
            if isinstance(result, dict) and result.get("error"):
                return

        arguments = tool_input.get("arguments", {}) or {}
        closed_task_id: str | None = None
        raw_close_id = arguments.get("task_id")
        if raw_close_id:
            closed_task_id = _claimed_task_id_for_ref(variables, raw_close_id)
        if raw_close_id and not closed_task_id and task_manager:
            from gobby.storage.tasks import TaskNotFoundError

            try:
                closed_task = task_manager.get_task(raw_close_id, project_id=project_id)
                if closed_task:
                    closed_task_id = closed_task.id
            except (ValueError, KeyError, TaskNotFoundError):
                logger.debug(
                    "Skipping unresolved closed task ref",
                    extra={"task_ref": raw_close_id, "session_id": session_id},
                    exc_info=True,
                )

        if closed_task_id:
            from gobby.workflows.task_claim_state import remove_claimed_task

            merge = remove_claimed_task(variables, closed_task_id)
            variables.update(merge)
            logger.info(
                "Session %s: removed %s from claimed_tasks (task_claimed=%s)",
                session_id,
                closed_task_id,
                merge["task_claimed"],
            )
        else:
            _record_unresolved_close_ref(session_id, raw_close_id)
            logger.debug(
                "Session %s: could not resolve closed task ref; skipping claimed_tasks update",
                session_id,
                extra={"task_ref": raw_close_id},
            )
        return

    if inner_tool_name not in ("create_task", "claim_task", "update_task"):
        return

    if isinstance(tool_output, dict):
        if tool_output.get("error") or tool_output.get("status") == "error":
            return
        result = tool_output.get("result", {})
        if isinstance(result, dict) and result.get("error"):
            return

    arguments = tool_input.get("arguments", {}) or {}
    task_id: str | None = None

    if inner_tool_name == "claim_task":
        raw_task_id = arguments.get("task_id")
        if raw_task_id and task_manager:
            try:
                task = task_manager.get_task(raw_task_id, project_id=project_id)
                if task:
                    task_id = task.id
                else:
                    logger.warning(
                        "Cannot resolve task ref %r to UUID - task not found",
                        raw_task_id,
                    )
            except Exception as e:
                logger.warning(
                    "Cannot resolve task ref %r to UUID: %s",
                    raw_task_id,
                    e,
                    exc_info=True,
                )
        elif raw_task_id and not task_manager:
            logger.warning("Cannot resolve task ref %r to UUID - no task_manager", raw_task_id)
    elif inner_tool_name == "create_task":
        create_args = tool_input.get("arguments", {}) or {}
        if not create_args.get("claim"):
            return
        result = tool_output.get("result", {}) if isinstance(tool_output, dict) else {}
        task_id = result.get("id") if isinstance(result, dict) else None
        if not task_id:
            return
    elif inner_tool_name == "update_task":
        update_args = tool_input.get("arguments", {}) or {}
        if update_args.get("status") != "in_progress":
            return
        raw_task_id = update_args.get("task_id")
        if raw_task_id and task_manager:
            try:
                task = task_manager.get_task(raw_task_id, project_id=project_id)
                if task:
                    task_id = task.id
            except Exception as e:
                logger.warning(
                    "Cannot resolve task ref %r to UUID: %s",
                    raw_task_id,
                    e,
                    exc_info=True,
                )

    if not task_id:
        logger.debug("Skipping task claim state update - no valid UUID for %s", inner_tool_name)
        return

    from gobby.workflows.task_claim_state import add_claimed_task

    ref = task_id
    if task_manager:
        try:
            task_obj = task_manager.get_task(task_id, project_id=project_id)
            if task_obj and task_obj.seq_num:
                ref = f"#{task_obj.seq_num}"
        except Exception as e:
            logger.debug(
                "Failed to resolve task ref for %s: %s",
                task_id,
                e,
                exc_info=True,
            )
    merge = add_claimed_task(variables, task_id, ref)
    variables.update(merge)
    variables["session_had_task"] = True
    logger.info(
        "Session %s: added %s to claimed_tasks (via %s)", session_id, task_id, inner_tool_name
    )

    if inner_tool_name == "claim_task" and task_id and session_task_manager:
        try:
            session_task_manager.link_task(session_id, task_id, "worked_on")
            logger.info("Auto-linked task %s to session %s", task_id, session_id)
        except Exception as e:
            logger.warning("Failed to auto-link task %s: %s", task_id, e)


def reconcile_claimed_tasks(
    variables: dict[str, Any],
    session_id: str,
    task_manager: LocalTaskManager | None = None,
    session_manager: SessionManager | None = None,
    session_task_manager: SessionTaskManager | None = None,
) -> None:
    """Reconcile claimed_tasks against DB, then derive task_claimed from it."""
    claimed_tasks: dict[str, str] = dict(variables.get("claimed_tasks") or {})

    if not task_manager:
        if not claimed_tasks and variables.get("task_claimed"):
            logger.debug(
                "Session %s: reconcile - no task_manager, clearing task_claimed (empty dict)",
                session_id,
            )
        variables["task_claimed"] = bool(claimed_tasks)
        return

    from gobby.storage.tasks import TaskNotFoundError

    if claimed_tasks:
        pruned: list[str] = []
        for task_uuid, ref in list(claimed_tasks.items()):
            try:
                task = task_manager.get_task(task_uuid)
            except (TaskNotFoundError, ValueError, KeyError):
                task = None

            if not is_task_actively_claimed(task, session_id):
                if _preserve_lineage_claim(
                    task,
                    task_uuid,
                    ref,
                    session_id,
                    task_manager,
                    session_manager,
                    session_task_manager,
                ):
                    continue
                pruned.append(f"{ref}({task_uuid[:8]})")
                del claimed_tasks[task_uuid]

        if pruned:
            logger.info(
                "Session %s: reconcile - pruned stale claims: %s",
                session_id,
                ", ".join(pruned),
            )
    else:
        try:
            db_tasks = task_manager.list_tasks(
                claimed_by_session_id=session_id,
                current_stage_state=list(ACTIVE_STAGE_STATES),
            )
        except Exception as e:
            logger.warning("Session %s: failed to list claimed tasks: %s", session_id, e)
            db_tasks = []

        if db_tasks:
            for task in db_tasks:
                claimed_tasks[task.id] = f"#{task.seq_num}" if task.seq_num else task.id[:8]
            logger.info(
                "Session %s: reconcile - rebuilt claimed_tasks from DB: %s",
                session_id,
                claimed_tasks,
            )

    variables["claimed_tasks"] = claimed_tasks
    variables["task_claimed"] = bool(claimed_tasks)


def _preserve_lineage_claim(
    task: Any,
    task_uuid: str,
    ref: str,
    session_id: str,
    task_manager: LocalTaskManager,
    session_manager: SessionManager | None,
    session_task_manager: SessionTaskManager | None,
) -> bool:
    owner_session_id = get_claimed_session_id(task)
    if not owner_session_id or not is_task_actively_claimed(task, owner_session_id):
        return False
    if not _sessions_share_lineage(session_manager, owner_session_id, session_id):
        return False

    try:
        task_manager.claim_task(
            task_uuid,
            session_id=session_id,
            force=owner_session_id != session_id,
        )
    except Exception as e:
        logger.warning(
            "Session %s: reconcile - preserved %s(%s) but failed to repair owner from %s: %s",
            session_id,
            ref,
            task_uuid[:8],
            owner_session_id,
            e,
            exc_info=True,
        )
    else:
        logger.info(
            "Session %s: reconcile - repaired lineage claim %s(%s) from %s",
            session_id,
            ref,
            task_uuid[:8],
            owner_session_id,
        )

    if session_task_manager:
        try:
            session_task_manager.link_task(session_id, task_uuid, "claimed")
        except Exception as e:
            logger.debug(
                "Session %s: failed to link preserved claim %s(%s): %s",
                session_id,
                ref,
                task_uuid[:8],
                e,
            )
    return True


def _sessions_share_lineage(
    session_manager: SessionManager | None,
    owner_session_id: str,
    session_id: str,
) -> bool:
    if owner_session_id == session_id:
        return True
    if session_manager is None:
        return False

    related_by_lineage = False
    is_ancestor = getattr(session_manager, "is_ancestor", None)
    if callable(is_ancestor):
        try:
            owner_is_ancestor = is_ancestor(owner_session_id, session_id)
            session_is_ancestor = is_ancestor(session_id, owner_session_id)
            if owner_is_ancestor is True or session_is_ancestor is True:
                related_by_lineage = True
        except Exception as e:
            logger.debug(
                "Failed to compare session lineage for %s and %s: %s",
                owner_session_id,
                session_id,
                e,
            )

    try:
        current_session = session_manager.get(session_id)
        owner_session = session_manager.get(owner_session_id)
    except Exception as e:
        logger.debug(
            "Failed to load sessions for lineage comparison %s/%s: %s",
            owner_session_id,
            session_id,
            e,
            exc_info=True,
        )
        return False

    related_by_lineage = related_by_lineage or (
        getattr(current_session, "parent_session_id", None) == owner_session_id
        or getattr(owner_session, "parent_session_id", None) == session_id
    )
    return related_by_lineage and sessions_have_continuous_terminal_context(
        current_session,
        owner_session,
    )
