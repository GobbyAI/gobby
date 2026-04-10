"""Observer detection functions for task claims, plan mode, and MCP call tracking.

These functions populate session variables that rule engine conditions
depend on (e.g., mcp_called(), mcp_result_is_null(), task_claimed).
They run BEFORE rule evaluation in the hook handler's _evaluate_rules path.
"""

import logging
import re
from typing import TYPE_CHECKING, Any

from gobby.hooks.normalization import _SHELL_TOOLS

if TYPE_CHECKING:
    from gobby.hooks.events import HookEvent
    from gobby.storage.tasks import LocalTaskManager
    from gobby.tasks.session_tasks import SessionTaskManager

logger = logging.getLogger(__name__)


_MODE_LEVEL_MAP = {"plan": 0, "accept_edits": 1, "normal": 1, "bypass": 2}

# Pattern matching git's commit success output: [branch hash] message
# e.g., "[main abc1234] Fix the bug" or "[feat/login 9a3b2c1e] Add auth"
_GIT_COMMIT_RE = re.compile(r"^\[[\w/.#-]+ [a-f0-9]{7,}\]", re.MULTILINE)

# Pattern matching git commit commands in Bash tool_input.command
_GIT_COMMIT_CMD_RE = re.compile(r"\bgit\s+commit\b")


def _extract_shell_output_text(tool_output: Any) -> str:
    """Extract text content from tool_output, handling both str and dict forms.

    After normalization, ``tool_output`` may be:
    - A plain string (e.g., ``"[main abc1234] Fix bug\\n 1 file changed"``)
    - A dict parsed from JSON (e.g., ``{"output": "...", "exitCode": 0}``)

    Returns the extracted text, or empty string if nothing usable found.
    """
    if isinstance(tool_output, str):
        return tool_output
    if isinstance(tool_output, dict):
        for key in ("output", "stdout", "content"):
            val = tool_output.get(key)
            if isinstance(val, str):
                return val
    return ""


def _is_git_commit_command(command: str) -> bool:
    """Check if a command string contains a ``git commit`` invocation."""
    return bool(_GIT_COMMIT_CMD_RE.search(command))


def _looks_like_commit_success(output: str) -> bool:
    """Check that shell output doesn't indicate a failed/no-op commit."""
    if not output:
        return False
    if "nothing to commit" in output or "nothing added to commit" in output:
        return False
    return True


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
    project_id: str | None = None,
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
                closed_task = task_manager.get_task(raw_close_id, project_id=project_id)
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
                task = task_manager.get_task(raw_task_id, project_id=project_id)
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
            task_obj = task_manager.get_task(task_id, project_id=project_id)
            if task_obj and task_obj.seq_num:
                ref = f"#{task_obj.seq_num}"
        except Exception as e:
            logger.debug(f"Failed to resolve task ref for {task_id}: {e}")
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


def detect_commit_link(event: "HookEvent", variables: dict[str, Any], session_id: str) -> None:
    """Detect when a commit is linked to a task in this session.

    Sets ``task_has_commits: true`` when ``link_commit`` succeeds or
    ``close_task`` succeeds with a ``commit_sha`` argument.  Multiple
    rules depend on this variable (require-error-triage, require-commit-
    before-close, block-skip-validation-with-commit, require-memory-review).

    Args:
        event: The AFTER_TOOL hook event
        variables: Session variables dict (modified in place)
        session_id: The platform session ID (for logging)
    """
    if variables.get("task_has_commits"):
        return  # Already set, no need to re-check

    if not event.data:
        return

    server_name = event.data.get("mcp_server", "")
    if server_name != "gobby-tasks":
        return

    inner_tool = event.data.get("mcp_tool", "")
    if inner_tool not in ("link_commit", "close_task", "auto_link_commits"):
        return

    # For close_task, only count if commit_sha was provided
    if inner_tool == "close_task":
        tool_input = event.data.get("tool_input", {}) or {}
        arguments = tool_input.get("arguments", {}) or {}
        if not arguments.get("commit_sha"):
            return

    # Verify the call succeeded
    tool_output = event.data.get("tool_output") or {}
    if isinstance(tool_output, dict):
        if tool_output.get("error") or tool_output.get("status") == "error":
            return
        result = tool_output.get("result", {})
        if isinstance(result, dict) and result.get("error"):
            return

    variables["task_has_commits"] = True
    logger.info(f"Session {session_id}: task_has_commits=true (via {inner_tool})")


def detect_bash_commit(event: "HookEvent", variables: dict[str, Any], session_id: str) -> None:
    """Detect git commit success output from Bash tool invocations.

    Sets ``task_has_commits: true`` when the Bash tool output contains
    git's commit success pattern (``[branch hash]``), e.g.::

        [main abc1234] Fix the bug

    This complements :func:`detect_commit_link` which only fires on
    explicit MCP tool calls (``link_commit``, ``close_task``).

    Args:
        event: The AFTER_TOOL hook event
        variables: Session variables dict (modified in place)
        session_id: The platform session ID (for logging)
    """
    if variables.get("task_has_commits"):
        return  # Already set

    if not event.data:
        return

    tool_name = event.data.get("tool_name", "")
    if tool_name not in _SHELL_TOOLS:
        return

    if event.data.get("is_error"):
        return  # Failed commands don't count

    raw_output = event.data.get("tool_output")
    output = _extract_shell_output_text(raw_output)
    if not output:
        if raw_output:
            logger.debug(
                f"Session {session_id}: detect_bash_commit - unrecognized tool_output type {type(raw_output).__name__}",
            )
        return

    if _GIT_COMMIT_RE.search(output):
        variables["task_has_commits"] = True
        logger.info(f"Session {session_id}: task_has_commits=true (Bash git commit output)")
        return

    # Fallback: command is git commit and output doesn't indicate failure
    tool_input = event.data.get("tool_input") or {}
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    if command and _is_git_commit_command(command) and _looks_like_commit_success(output):
        variables["task_has_commits"] = True
        logger.info(
            f"Session {session_id}: task_has_commits=true (Bash git commit command fallback)"
        )


def detect_plan_mode_from_context(prompt: str, variables: dict[str, Any], session_id: str) -> None:
    """Detect plan mode from system reminders or CLI-specific markers.

    Detection runs three passes on the prompt (after stripping conversation
    history to avoid false positives from prior turns):

    1. **Claude Code** — indicators inside ``<system-reminder>`` tags.
    2. **Gemini CLI** — markdown-formatted plan mode headers/bold text
       searched in the cleaned prompt directly.
    3. **Gobby ``<plan-mode>``** — Gobby's own plan-mode tags (injected
       by ``_consume_plan_mode_context``), for CLIs where Gobby manages
       plan mode natively.

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

    # --- Pass 1: Claude Code system-reminder indicators ---
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
            if not variables.get("plan_mode"):
                variables["plan_mode"] = True
                logger.info(f"Session {session_id}: plan_mode=True")
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
            if variables.get("plan_mode"):
                variables["plan_mode"] = False
                variables["plan_skill_loaded"] = False
                logger.info(f"Session {session_id}: plan_mode=False")
            return

    # --- Pass 2: Gemini CLI markdown indicators ---
    gemini_plan_indicators = [
        "# Active Approval Mode: Plan",
        "You are operating in **Plan Mode**",
    ]

    for indicator in gemini_plan_indicators:
        if indicator in cleaned:
            if variables.get("mode_level") != 0:
                variables["mode_level"] = 0
                logger.info(
                    f"Session {session_id}: mode_level=0 (plan) "
                    f"(detected from Gemini marker: '{indicator}')"
                )
            return

    gemini_exit_indicators = [
        "Exited Plan Mode",
        "# Active Approval Mode: Execute",
    ]

    for indicator in gemini_exit_indicators:
        if indicator in cleaned:
            if variables.get("mode_level") == 0:
                chat_mode = variables.get("chat_mode", "bypass")
                variables["mode_level"] = compute_mode_level(chat_mode)
                logger.info(
                    f"Session {session_id}: mode_level={variables['mode_level']} "
                    f"(detected from Gemini marker: '{indicator}')"
                )
            return

    # --- Pass 3: Gobby <plan-mode> tags ---
    if '<plan-mode status="active">' in cleaned:
        if variables.get("mode_level") != 0:
            variables["mode_level"] = 0
            logger.info(
                f"Session {session_id}: mode_level=0 (plan) "
                f'(detected from <plan-mode status="active">)'
            )
        return

    if '<plan-mode status="approved">' in cleaned:
        if variables.get("mode_level") == 0:
            chat_mode = variables.get("chat_mode", "bypass")
            variables["mode_level"] = compute_mode_level(chat_mode)
            logger.info(
                f"Session {session_id}: mode_level={variables['mode_level']} "
                f'(detected from <plan-mode status="approved">)'
            )
        return

    # --- No plan-mode markers found: heal stale state ---
    # If mode_level is 0 (plan) but no CLI injected plan-mode indicators,
    # the value is stale from a previous session (survived clear/compact).
    # Reset based on chat_mode, which is always fresh in-memory.
    if variables.get("mode_level") == 0:
        chat_mode = variables.get("chat_mode", "bypass")
        new_level = compute_mode_level(chat_mode)
        if new_level != 0:
            variables["mode_level"] = new_level
            logger.info(
                f"Session {session_id}: mode_level={new_level} "
                f"(healed stale plan mode — no markers found, chat_mode='{chat_mode}')"
            )


def reconcile_claimed_tasks(
    variables: dict[str, Any],
    session_id: str,
    task_manager: "LocalTaskManager | None" = None,
) -> None:
    """Reconcile claimed_tasks against DB, then derive task_claimed from it.

    task_claimed is always computed from claimed_tasks — never trusted as a
    stored value.  This eliminates the class of bugs where the boolean and
    dict get out of sync.

    Must run BEFORE rule evaluation on semantic turn-end so turn-end gates
    see accurate claimed-task state across CLIs.
    """
    claimed_tasks: dict[str, str] = dict(variables.get("claimed_tasks") or {})

    if not task_manager:
        # Can't verify DB — derive from what we have and move on
        if not claimed_tasks and variables.get("task_claimed"):
            logger.debug(
                f"Session {session_id}: reconcile — no task_manager, "
                f"clearing task_claimed (empty dict)"
            )
        variables["task_claimed"] = bool(claimed_tasks)
        return

    from gobby.storage.tasks import TaskNotFoundError

    if claimed_tasks:
        # Prune entries that are no longer in_progress or assigned to us
        pruned: list[str] = []
        for task_uuid, ref in list(claimed_tasks.items()):
            try:
                task = task_manager.get_task(task_uuid)
            except (TaskNotFoundError, ValueError, KeyError):
                task = None

            if task is None or task.status != "in_progress" or task.assignee != session_id:
                pruned.append(f"{ref}({task_uuid[:8]})")
                del claimed_tasks[task_uuid]

        if pruned:
            logger.info(
                f"Session {session_id}: reconcile — pruned stale claims: {', '.join(pruned)}"
            )
    else:
        # Dict is empty — check DB for tasks we might have lost track of
        try:
            db_tasks = task_manager.list_tasks(assignee=session_id, status="in_progress")
        except Exception as e:
            logger.warning(f"Session {session_id}: failed to list in-progress tasks: {e}")
            db_tasks = []

        if db_tasks:
            for t in db_tasks:
                claimed_tasks[t.id] = f"#{t.seq_num}" if t.seq_num else t.id[:8]
            logger.info(
                f"Session {session_id}: reconcile — rebuilt claimed_tasks from DB: {claimed_tasks}"
            )

    # Single source of truth: dict drives boolean
    variables["claimed_tasks"] = claimed_tasks
    variables["task_claimed"] = bool(claimed_tasks)


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
