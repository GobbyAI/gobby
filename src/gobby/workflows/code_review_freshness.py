"""Recognize foreign managed-branch landings for code-review freshness."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence

import psycopg
from psycopg_pool import PoolTimeout

from gobby.config.shell_lexing import shell_command_segments
from gobby.storage.clones import LocalCloneManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.worktrees import LocalWorktreeManager

logger = logging.getLogger(__name__)

_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_COMMAND_PREFIXES = frozenset({"sudo", "command", "do", "then", "else"})
_GIT_GLOBAL_VALUE_OPTIONS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
)
_GIT_GLOBAL_FLAGS = frozenset(
    {"--no-pager", "--paginate", "-p", "--bare", "--literal-pathspecs", "--no-optional-locks"}
)
_MERGE_VALUE_OPTIONS = frozenset(
    {"-m", "--message", "-s", "--strategy", "-X", "--strategy-option", "--cleanup", "--into-name"}
)
_MERGE_CONTROL_OPTIONS = frozenset({"--abort", "--continue", "--quit"})
_REVIEW_GATED_SUBCOMMANDS = frozenset({"commit", "merge", "cherry-pick", "revert"})


def _strip_command_prefixes(tokens: Sequence[str]) -> list[str]:
    remaining = list(tokens)
    while remaining and _ENV_ASSIGNMENT_RE.match(remaining[0]):
        remaining.pop(0)
    while remaining and remaining[0] in _COMMAND_PREFIXES:
        remaining.pop(0)
        while remaining and _ENV_ASSIGNMENT_RE.match(remaining[0]):
            remaining.pop(0)
    return remaining


def _git_subcommand(tokens: Sequence[str]) -> tuple[str, list[str]] | None:
    command = _strip_command_prefixes(tokens)
    if not command or command[0].rsplit("/", maxsplit=1)[-1] != "git":
        return None

    index = 1
    while index < len(command):
        token = command[index]
        if token in _GIT_GLOBAL_VALUE_OPTIONS:
            index += 2
            continue
        if token in _GIT_GLOBAL_FLAGS or any(
            token.startswith(f"{option}=") for option in _GIT_GLOBAL_VALUE_OPTIONS
        ):
            index += 1
            continue
        break
    if index >= len(command):
        return None
    return command[index], command[index + 1 :]


def _merge_heads(tokens: Sequence[str]) -> tuple[str, ...] | None:
    parsed = _git_subcommand(tokens)
    if parsed is None or parsed[0] != "merge":
        return None

    command = parsed[1]
    index = 0
    heads: list[str] = []
    end_of_options = False
    while index < len(command):
        token = command[index]
        index += 1
        if end_of_options:
            heads.append(token)
            continue
        if token == "--":
            end_of_options = True
            continue
        if token in _MERGE_CONTROL_OPTIONS:
            return ()
        if token in _MERGE_VALUE_OPTIONS:
            if index >= len(command):
                return ()
            index += 1
            continue
        if any(token.startswith(f"{option}=") for option in _MERGE_VALUE_OPTIONS):
            continue
        if token.startswith("-"):
            continue
        heads.append(token)
    return tuple(heads)


def merge_branch_names(command: object) -> tuple[str, ...]:
    """Return the head from a lone review-gated merge, else no heads."""
    if not isinstance(command, str) or not command.strip():
        return ()
    segments = shell_command_segments(command)
    gated_subcommands = [
        subcommand
        for segment in segments
        if (parsed := _git_subcommand(segment)) is not None
        and (subcommand := parsed[0]) in _REVIEW_GATED_SUBCOMMANDS
    ]
    if gated_subcommands != ["merge"]:
        return ()
    merge_segments = [heads for segment in segments if (heads := _merge_heads(segment)) is not None]
    if len(merge_segments) != 1 or len(merge_segments[0]) != 1:
        return ()
    return merge_segments[0]


def _branch_candidates(branch: str) -> tuple[str, ...]:
    if branch.startswith("refs/heads/"):
        return (branch, branch.removeprefix("refs/heads/"))
    return (branch,)


def is_foreign_landing_merge(
    db: HubDatabase,
    command: object,
    *,
    session_id: str,
    project_id: str,
) -> bool:
    """Whether a merge lands a managed branch owned by another session.

    Any parse, registry, or ownership ambiguity returns false so review
    freshness is spent. The exemption is deliberately narrower than `git
    merge`: it requires one registered worktree/clone branch and a distinct,
    known owner session.
    """
    branches = merge_branch_names(command)
    if len(branches) != 1:
        return False

    try:
        worktrees = LocalWorktreeManager(db)
        clones = LocalCloneManager(db)
        for candidate in _branch_candidates(branches[0]):
            worktree = worktrees.get_by_branch(project_id, candidate)
            if worktree is not None:
                owner = worktree.agent_session_id
                return bool(owner and owner != session_id)
            clone = clones.get_by_branch(project_id, candidate)
            if clone is not None:
                owner = clone.agent_session_id
                return bool(owner and owner != session_id)
    except (psycopg.Error, PoolTimeout, RuntimeError):
        logger.warning("Could not inspect managed merge ownership", exc_info=True)
    return False
