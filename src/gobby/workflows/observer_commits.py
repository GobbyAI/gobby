"""Commit-detection observers for workflow session variables."""

from __future__ import annotations

import logging
import re
import shlex
from typing import TYPE_CHECKING, Any

from gobby.hooks.normalization import _SHELL_TOOLS
from gobby.workflows.observer_utils import _extract_shell_output_text

if TYPE_CHECKING:
    from gobby.hooks.events import HookEvent

logger = logging.getLogger("gobby.workflows.observers")

# Pattern matching git's commit success output: [branch hash] message.
# Branch labels can contain spaces for detached HEAD output.
_GIT_COMMIT_RE = re.compile(r"^\[[^\]\n]+ [a-f0-9]{7,}\]", re.MULTILINE)

_GIT_GLOBAL_OPTIONS_WITH_VALUE = {
    "-C",
    "-c",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--exec-path",
}


def _is_git_commit_command(command: str) -> bool:
    """Check if a command string contains a ``git commit`` invocation."""
    if not command.strip():
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    for index, token in enumerate(tokens):
        if token.rsplit("/", maxsplit=1)[-1] != "git":
            continue
        next_index = _skip_git_global_options(tokens, index + 1)
        if next_index < len(tokens) and tokens[next_index] == "commit":
            return True
    return False


def _skip_git_global_options(tokens: list[str], index: int) -> int:
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return index + 1
        if token in _GIT_GLOBAL_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if any(token.startswith(f"{option}=") for option in _GIT_GLOBAL_OPTIONS_WITH_VALUE):
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        return index
    return index


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
    if variables.get("task_has_commits"):
        return

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
        result = tool_output.get("result", {})
        if isinstance(result, dict) and result.get("error"):
            return

    variables["task_has_commits"] = True
    logger.info("Session %s: task_has_commits=true (via %s)", session_id, inner_tool)


def detect_bash_commit(event: HookEvent, variables: dict[str, Any], session_id: str) -> None:
    """Detect git commit success output from shell tool invocations."""
    if variables.get("task_has_commits"):
        return

    if not event.data:
        return

    tool_name = event.data.get("tool_name", "")
    if tool_name not in _SHELL_TOOLS:
        return

    if event.data.get("is_error"):
        return

    raw_output = event.data.get("tool_output")
    output = _extract_shell_output_text(raw_output)
    if not output:
        if raw_output:
            logger.debug(
                "Session %s: detect_bash_commit - unrecognized tool_output type %s",
                session_id,
                type(raw_output).__name__,
            )
        return

    if _GIT_COMMIT_RE.search(output):
        variables["task_has_commits"] = True
        logger.info("Session %s: task_has_commits=true (Bash git commit output)", session_id)
        return

    tool_input = event.data.get("tool_input") or {}
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    if command and _is_git_commit_command(command) and _looks_like_commit_success(output):
        variables["task_has_commits"] = True
        logger.info(
            "Session %s: task_has_commits=true (Bash git commit command fallback)",
            session_id,
        )
