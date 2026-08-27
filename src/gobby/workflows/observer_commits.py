"""Commit-detection observers for workflow session variables."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from gobby.workflows.commit_guard import parse_git_commit_invocations
from gobby.workflows.git_utils import get_dirty_files_categorized
from gobby.workflows.observer_utils import (
    _extract_shell_command,
    _extract_shell_output_text,
    _shell_tool_succeeded,
    _successful_close_result,
)
from gobby.workflows.task_claim_state import (
    active_task_id_for_edit,
    task_edited_file_set,
    task_edited_file_set_for_checkout,
)

if TYPE_CHECKING:
    from gobby.hooks.events import HookEvent
    from gobby.workflows.state_manager import SessionVariableManager

logger = logging.getLogger("gobby.workflows.observers")

# Pattern matching git's commit success output: [branch hash] message.
# Branch labels can contain spaces for detached HEAD output.
_GIT_COMMIT_RE = re.compile(r"^\[[^\]\n]+ [a-f0-9]{7,}\]", re.MULTILINE)


def _is_git_commit_command(command: str) -> bool:
    """Check if a command string contains a ``git commit`` invocation."""
    return bool(parse_git_commit_invocations(command))


def _looks_like_commit_success(output: str) -> bool:
    """Check that shell output doesn't indicate a failed/no-op commit."""
    if not output:
        return False
    if "nothing to commit" in output or "nothing added to commit" in output:
        return False
    return True


def detect_commit_link(event: HookEvent, variables: dict[str, Any], session_id: str) -> None:
    """Detect when a commit is linked to a task in this session.

    Sets ``task_has_commits: true`` when ``link_commit`` succeeds or
    ``close_task`` succeeds with a ``commit_sha`` argument.  Multiple
    rules depend on this variable (require-error-triage, require-commit-
    before-close, block-skip-validation-with-commit, require-memory-review).
    """
    if not event.data:
        return

    server_name = event.data.get("mcp_server", "")
    if server_name != "gobby-tasks":
        return

    inner_tool = event.data.get("mcp_tool", "")
    if inner_tool not in ("link_commit", "close_task", "auto_link_commits"):
        return

    if inner_tool == "close_task":
        tool_input = event.data.get("tool_input", {}) or {}
        arguments = tool_input.get("arguments", {}) or {}
        if not arguments.get("commit_sha"):
            return

    tool_output = event.data.get("tool_output") or {}
    if isinstance(tool_output, dict):
        if tool_output.get("error") or tool_output.get("status") == "error":
            return
        result = tool_output.get("result")
        if isinstance(result, dict) and result.get("error"):
            return
    if inner_tool == "close_task" and _successful_close_result(tool_output) is None:
        return

    variables["task_has_commits"] = True
    variables["_found_work_fix_commit_turn"] = True
    logger.debug("Session %s: task_has_commits=true (via %s)", session_id, inner_tool)


def detect_bash_commit(event: HookEvent, variables: dict[str, Any], session_id: str) -> bool:
    """Detect git commit success output from shell tool invocations."""
    if not event.data:
        return False

    command = _extract_shell_command(event)
    outcome = _shell_tool_succeeded(event)
    if not _is_git_commit_command(command) or outcome is False:
        return False

    raw_output = event.data.get("tool_output")
    output = _extract_shell_output_text(raw_output)
    if not output:
        if raw_output:
            logger.debug(
                "Session %s: detect_bash_commit - unrecognized tool_output type %s",
                session_id,
                type(raw_output).__name__,
            )
        return False

    if _GIT_COMMIT_RE.search(output):
        variables["task_has_commits"] = True
        variables["_found_work_fix_commit_turn"] = True
        logger.debug("Session %s: task_has_commits=true (Bash git commit output)", session_id)
        return True

    if outcome is True and _looks_like_commit_success(output):
        variables["task_has_commits"] = True
        variables["_found_work_fix_commit_turn"] = True
        logger.debug(
            "Session %s: task_has_commits=true (Bash git commit command fallback)",
            session_id,
        )
        return True
    return False


def release_clean_task_paths_after_commit(
    event: HookEvent,
    variables: dict[str, Any],
    session_id: str,
    *,
    variable_manager: SessionVariableManager,
    project_path: str,
) -> list[str]:
    """Release active-task paths made clean by a successful owner commit."""
    task_id = active_task_id_for_edit(variables)
    if task_id is None:
        return []
    attributed = task_edited_file_set_for_checkout(variables, task_id, project_path)
    if not attributed:
        attributed = task_edited_file_set(variables, task_id)
    if not attributed:
        return []

    dirty_paths = get_dirty_files_categorized(project_path).all
    clean_paths = sorted(attributed - dirty_paths)
    if not clean_paths:
        return []
    released, _remaining = variable_manager.release_task_edited_files(
        session_id,
        task_id,
        clean_paths,
        checkout_root=project_path,
    )
    if not released:
        return []

    refreshed = variable_manager.get_variables(session_id)
    for key in (
        "task_edited_files",
        "task_edited_file_times",
        "task_edited_file_checkouts",
    ):
        variables[key] = refreshed.get(key, {})
    return released
