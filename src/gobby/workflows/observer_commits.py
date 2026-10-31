"""Commit-detection observers for workflow session variables."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.utils.git import run_git_command
from gobby.workflows.git_commit_parsing import (
    parse_git_commit_invocations,
    resolve_commit_inspect_cwd,
)
from gobby.workflows.git_lone_command import lone_git_command
from gobby.workflows.observer_utils import (
    _extract_shell_command,
    _extract_shell_output_text,
    _shell_tool_succeeded,
    _successful_close_result,
)
from gobby.workflows.reserved_variables import AUTHORED_MERGES_VARIABLE

if TYPE_CHECKING:
    from gobby.hooks.events import HookEvent

logger = logging.getLogger("gobby.workflows.observers")

# Pattern matching git's commit success output: [branch hash] message.
# Branch labels can contain spaces for detached HEAD output.
_GIT_COMMIT_RE = re.compile(r"^\[[^\]\n]+ [a-f0-9]{7,}\]", re.MULTILINE)
# git prints this at the start of a line when a merge it ran committed cleanly.
_MERGE_MADE_RE = re.compile(r"^Merge made by ", re.MULTILINE)
# The ledger keeps the newest merges; reserved_variables.AUTHORED_MERGES_VARIABLE names it.
_AUTHORED_MERGES_LIMIT = 50


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


def commit_link_succeeded(event: HookEvent) -> bool:
    """Return whether this event linked a commit to a task through gobby-tasks.

    True when ``link_commit`` or ``auto_link_commits`` succeeded, or
    ``close_task`` succeeded with a ``commit_sha`` argument.
    """
    if not event.data:
        return False

    server_name = event.data.get("mcp_server", "")
    if server_name != "gobby-tasks":
        return False

    inner_tool = event.data.get("mcp_tool", "")
    if inner_tool not in ("link_commit", "close_task", "auto_link_commits"):
        return False

    if inner_tool == "close_task":
        tool_input = event.data.get("tool_input", {}) or {}
        arguments = tool_input.get("arguments", {}) or {}
        if not arguments.get("commit_sha"):
            return False

    tool_output = event.data.get("tool_output") or {}
    if isinstance(tool_output, dict):
        if tool_output.get("error") or tool_output.get("status") == "error":
            return False
        result = tool_output.get("result")
        if isinstance(result, dict) and result.get("error"):
            return False
    return inner_tool != "close_task" or _successful_close_result(tool_output) is not None


def detect_commit_link(event: HookEvent, variables: dict[str, Any], session_id: str) -> None:
    """Detect when a commit is linked to a task in this session.

    Sets ``task_has_commits: true`` when ``link_commit`` succeeds or
    ``close_task`` succeeds with a ``commit_sha`` argument.  Multiple
    rules depend on this variable (require-error-triage, require-commit-
    before-close, block-skip-validation-with-commit, require-memory-review).
    """
    if not commit_link_succeeded(event):
        return
    inner_tool = (event.data or {}).get("mcp_tool", "")

    variables["task_has_commits"] = True
    variables["_found_work_fix_commit_turn"] = True
    logger.debug("Session %s: task_has_commits=true (via %s)", session_id, inner_tool)


def _record_authored_merge(repo: Path, variables: dict[str, Any]) -> None:
    """Record HEAD of ``repo`` when that HEAD is a merge commit this session created."""
    head = run_git_command(["git", "rev-parse", "--verify", "-q", "HEAD^{commit}"], repo)
    if (
        head is None
        or run_git_command(["git", "rev-parse", "--verify", "-q", "HEAD^2"], repo) is None
    ):
        return
    recorded = [
        sha for sha in variables.get(AUTHORED_MERGES_VARIABLE) or [] if isinstance(sha, str)
    ]
    variables[AUTHORED_MERGES_VARIABLE] = list(dict.fromkeys([*recorded, head]))[
        -_AUTHORED_MERGES_LIMIT:
    ]


def _record_commit_merges(event: HookEvent, command: str, variables: dict[str, Any]) -> None:
    """Record HEAD of each repository a commit in this command landed in.

    ``git -C <path>`` and ``cd`` name the repository a commit landed in, so a commit made
    elsewhere is not credited to the event's working directory. A commit whose directory
    git cannot verify is not credited at all.
    """
    cwd = event.cwd
    if not cwd:
        return
    for invocation in parse_git_commit_invocations(command):
        if invocation.cwd_unverified:
            continue
        repo = resolve_commit_inspect_cwd(invocation, event_cwd=cwd, project_path=cwd)
        _record_authored_merge(Path(repo), variables)


def _record_clean_merge(
    event: HookEvent, command: str, output: str, variables: dict[str, Any]
) -> None:
    """Record the merge a plain ``git merge`` committed cleanly, in the repository it named.

    git prints ``Merge made by`` only when it creates a merge commit, so a merge that is
    already up to date is never credited.
    """
    cwd = event.cwd
    call = lone_git_command(command)
    if not cwd or call is None or call.tokens[1:2] != ["merge"]:
        return
    if _MERGE_MADE_RE.search(output):
        _record_authored_merge(Path(cwd) / (call.target or "."), variables)


def detect_bash_commit(event: HookEvent, variables: dict[str, Any], session_id: str) -> bool:
    """Detect git commit success output from shell tool invocations."""
    if not event.data:
        return False

    command = _extract_shell_command(event)
    outcome = _shell_tool_succeeded(event)
    if outcome is False:
        return False

    raw_output = event.data.get("tool_output")
    output = _extract_shell_output_text(raw_output)
    if not _is_git_commit_command(command):
        _record_clean_merge(event, command, output, variables)
        return False

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
        _record_commit_merges(event, command, variables)
        logger.debug("Session %s: task_has_commits=true (Bash git commit output)", session_id)
        return True

    if outcome is True and _looks_like_commit_success(output):
        variables["task_has_commits"] = True
        variables["_found_work_fix_commit_turn"] = True
        _record_commit_merges(event, command, variables)
        logger.debug(
            "Session %s: task_has_commits=true (Bash git commit command fallback)",
            session_id,
        )
        return True
    return False
