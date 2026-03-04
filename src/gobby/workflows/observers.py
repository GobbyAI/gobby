"""Observer detection functions for task claims, plan mode, and MCP call tracking.

These functions populate session variables that rule engine conditions
depend on (e.g., mcp_called(), mcp_result_is_null(), task_claimed).
They run BEFORE rule evaluation in the hook handler's _evaluate_rules path.
"""

import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.hooks.events import HookEvent
    from gobby.storage.tasks import LocalTaskManager
    from gobby.tasks.session_tasks import SessionTaskManager

logger = logging.getLogger(__name__)


_MODE_LEVEL_MAP = {"plan": 0, "accept_edits": 1, "normal": 1, "bypass": 2}


def compute_mode_level(chat_mode: str) -> int:
    """Derive numeric mode_level from chat_mode.

    Returns 0 (Plan), 1 (Act), or 2 (Full Auto).
    """
    return _MODE_LEVEL_MAP.get(chat_mode, 2)


# =============================================================================
# Detection functions — operate on plain dict variables
# =============================================================================


def detect_task_claim(
    event: "HookEvent",
    variables: dict[str, Any],
    session_id: str,
    session_task_manager: "SessionTaskManager | None" = None,
    task_manager: "LocalTaskManager | None" = None,
) -> None:
    """Detect gobby-tasks calls that claim or release a task for this session.

    Sets ``task_claimed: true`` in variables when the agent successfully
    creates a task or updates a task to in_progress status.

    Clears ``task_claimed: false`` when the agent closes a task, requiring
    them to claim another task before making further file modifications.

    Args:
        event: The AFTER_TOOL hook event
        variables: Session variables dict (modified in place)
        session_id: The platform session ID
        session_task_manager: Optional manager for auto-linking tasks to sessions
        task_manager: Optional manager for resolving task refs to UUIDs
    """
    if not event.data:
        return

    tool_input = event.data.get("tool_input", {}) or {}
    tool_output = event.data.get("tool_output") or {}

    server_name = event.data.get("mcp_server", "")
    if server_name != "gobby-tasks":
        return

    inner_tool_name = event.data.get("mcp_tool", "")

    # Handle close_task
    if inner_tool_name == "close_task":
        if not tool_output:
            return
        if isinstance(tool_output, dict):
            if tool_output.get("error") or tool_output.get("status") == "error":
                return
            result = tool_output.get("result", {})
            if isinstance(result, dict) and result.get("error"):
                return

        # Resolve closed task UUID from tool arguments
        arguments = tool_input.get("arguments", {}) or {}
        closed_task_id: str | None = None
        raw_close_id = arguments.get("task_id")
        if raw_close_id and task_manager:
            from gobby.storage.tasks import TaskNotFoundError

            try:
                closed_task = task_manager.get_task(raw_close_id)
                if closed_task:
                    closed_task_id = closed_task.id
            except (ValueError, KeyError, TaskNotFoundError) as e:
                logger.warning(f"Cannot resolve closed task ref '{raw_close_id}': {e}")
                return

        if closed_task_id:
            from gobby.workflows.task_claim_state import remove_claimed_task

            merge = remove_claimed_task(variables, closed_task_id)
            variables.update(merge)
            logger.info(
                f"Session {session_id}: removed {closed_task_id} from claimed_tasks "
                f"(task_claimed={merge['task_claimed']})"
            )
        else:
            logger.warning(
                f"Session {session_id}: could not resolve closed task ref — "
                f"skipping claimed_tasks update"
            )
        return

    if inner_tool_name not in ("create_task", "update_task", "claim_task"):
        return

    # For update_task, only count if status is being set to in_progress
    if inner_tool_name == "update_task":
        arguments = tool_input.get("arguments", {}) or {}
        if arguments.get("status") != "in_progress":
            return

    # Check if the call succeeded
    if isinstance(tool_output, dict):
        if tool_output.get("error") or tool_output.get("status") == "error":
            return
        result = tool_output.get("result", {})
        if isinstance(result, dict) and result.get("error"):
            return

    # Extract task_id — MUST resolve to UUID
    arguments = tool_input.get("arguments", {}) or {}
    task_id: str | None = None

    if inner_tool_name in ("update_task", "claim_task"):
        raw_task_id = arguments.get("task_id")
        if raw_task_id and task_manager:
            try:
                task = task_manager.get_task(raw_task_id)
                if task:
                    task_id = task.id
                else:
                    logger.warning(
                        f"Cannot resolve task ref '{raw_task_id}' to UUID - task not found"
                    )
            except Exception as e:
                logger.warning(f"Cannot resolve task ref '{raw_task_id}' to UUID: {e}")
        elif raw_task_id and not task_manager:
            logger.warning(f"Cannot resolve task ref '{raw_task_id}' to UUID - no task_manager")
    elif inner_tool_name == "create_task":
        create_args = tool_input.get("arguments", {}) or {}
        if not create_args.get("claim"):
            return
        result = tool_output.get("result", {}) if isinstance(tool_output, dict) else {}
        task_id = result.get("id") if isinstance(result, dict) else None
        if not task_id:
            return

    if not task_id:
        logger.debug(f"Skipping task claim state update - no valid UUID for {inner_tool_name}")
        return

    from gobby.workflows.task_claim_state import add_claimed_task

    # Resolve ref for display
    ref = task_id
    if task_manager:
        try:
            task_obj = task_manager.get_task(task_id)
            if task_obj and task_obj.seq_num:
                ref = f"#{task_obj.seq_num}"
        except Exception as e:
            logger.debug("Failed to resolve task ref for %s: %s", task_id, e)
    merge = add_claimed_task(variables, task_id, ref)
    variables.update(merge)
    variables["session_had_task"] = True
    logger.info(f"Session {session_id}: added {task_id} to claimed_tasks (via {inner_tool_name})")

    # Auto-link task to session
    if inner_tool_name in ("update_task", "claim_task"):
        if task_id and session_task_manager:
            try:
                session_task_manager.link_task(session_id, task_id, "worked_on")
                logger.info(f"Auto-linked task {task_id} to session {session_id}")
            except Exception as e:
                logger.warning(f"Failed to auto-link task {task_id}: {e}")


def detect_plan_mode_from_context(prompt: str, variables: dict[str, Any], session_id: str) -> None:
    """Detect plan mode from system reminders injected by Claude Code.

    IMPORTANT: Only matches indicators within <system-reminder> tags to avoid
    false positives from handoff context or user messages that mention plan mode.

    Args:
        prompt: The user prompt text (may contain system reminders)
        variables: Session variables dict (modified in place)
        session_id: The platform session ID (for logging)
    """
    if not prompt:
        return

    cleaned = re.sub(
        r"<conversation-history>.*?</conversation-history>", "", prompt, flags=re.DOTALL
    )

    system_reminders = re.findall(r"<system-reminder>(.*?)</system-reminder>", cleaned, re.DOTALL)
    reminder_text = " ".join(system_reminders)

    plan_mode_indicators = [
        "Plan mode is active",
        "Plan mode still active",
        "You are in plan mode",
    ]

    for indicator in plan_mode_indicators:
        if indicator in reminder_text:
            if variables.get("mode_level") != 0:
                variables["mode_level"] = 0
                logger.info(
                    f"Session {session_id}: mode_level=0 (plan) "
                    f"(detected from system reminder: '{indicator}')"
                )
            return

    exit_indicators = [
        "Exited Plan Mode",
        "Plan mode exited",
    ]

    for indicator in exit_indicators:
        if indicator in reminder_text:
            if variables.get("mode_level") == 0:
                chat_mode = variables.get("chat_mode", "bypass")
                variables["mode_level"] = compute_mode_level(chat_mode)
                logger.info(
                    f"Session {session_id}: mode_level={variables['mode_level']} "
                    f"(detected from system reminder: '{indicator}')"
                )
            return


def detect_mcp_call(event: "HookEvent", variables: dict[str, Any], session_id: str) -> None:
    """Track MCP tool calls by server/tool for rule engine conditions.

    Populates variables["mcp_calls"] and variables["mcp_results"] so that
    rule conditions like ``mcp_called('gobby-memory', 'recall')`` and
    ``mcp_result_is_null(...)`` evaluate correctly.

    Args:
        event: The AFTER_TOOL hook event
        variables: Session variables dict (modified in place)
        session_id: The platform session ID (for logging)
    """
    if not event.data:
        return

    server_name = event.data.get("mcp_server", "")
    inner_tool = event.data.get("mcp_tool", "")

    if not server_name or not inner_tool:
        return

    tool_output = event.data.get("tool_output") or {}

    _track_mcp_call(variables, server_name, inner_tool, tool_output, session_id)


def _track_mcp_call(
    variables: dict[str, Any],
    server_name: str,
    inner_tool: str,
    tool_output: dict[str, Any] | Any,
    session_id: str,
) -> bool:
    """Track a successful MCP call in session variables.

    Returns True if call succeeded (was tracked), False if it failed.
    """
    result = None
    is_error = False
    if isinstance(tool_output, dict):
        if tool_output.get("error") or tool_output.get("status") == "error":
            is_error = True
        else:
            result = tool_output.get("result")
            if isinstance(result, dict) and result.get("error"):
                is_error = True

    if is_error:
        return False

    mcp_calls = variables.setdefault("mcp_calls", {})
    server_calls = mcp_calls.setdefault(server_name, [])
    if inner_tool not in server_calls:
        server_calls.append(inner_tool)

    mcp_results = variables.setdefault("mcp_results", {})
    server_results = mcp_results.setdefault(server_name, {})
    server_results[inner_tool] = result

    logger.debug(
        f"Session {session_id}: MCP call tracked {server_name}/{inner_tool} "
        f"(result={'present' if result is not None else 'null'})"
    )
    return True
