"""Base merges a definition may commit without a task claim.

A coordinator merges the integration branch, or an epic's main branch, into its
registered task or epic worktree. A definition declares the `merge_base_commits`
capability to take that step without a claim. This module admits only the merge
the capability covers, and reads nothing else from the definition:

- one lone `git merge <ref>` whose head is an admitted landing tip, from a
  worktree with no tracked changes;
- while such a merge is in progress (`MERGE_HEAD` is an admitted tip), the
  `git commit` or `git merge --continue` that completes it, when every staged
  path is one the merge brings in, and `git merge --abort` to unwind;
- a `git add` of paths that the same in-progress merge left conflicted;
- a structured write to one of those paths. A shell command never earns a path
  grant, because its output and its side effects are not named.

The git call must match the closed grammar in `_FORMS` on its raw command text, as one
line, before any shell lexing reads it; anything else is not admitted. A `-C <path>`
names the worktree the call runs in, and that worktree is the merge root.

Admitted tips are the integration branch of the project's main checkout and the
landing branch of the worktree's task (an epic's branch for a leaf). A merge is not
a claim. This module writes no claim, attribution, or step record, so the merge
makes its session neither a claimant nor an author for `land_commit`.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg_pool import PoolTimeout

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_resolution import is_session_uuid
from gobby.storage.tasks import LocalTaskManager, Task, TaskNotFoundError
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.daemon_git import GitOk, daemon_git
from gobby.workflows.agent_resolver import session_has_capability
from gobby.workflows.enforcement.blocking import get_touched_file_paths
from gobby.workflows.git_utils import resolve_git_worktree_root_async

logger = logging.getLogger(__name__)

CAPABILITY = "merge_base_commits"
_GIT_TIMEOUT = 10.0
_MERGE_FLAGS = frozenset({"--no-edit", "--no-ff"})
_COMMIT_VALUE_FLAGS = frozenset({"-m"})
_PATH = r"(?!-)[A-Za-z0-9._/-]+"
_MESSAGE = r"[A-Za-z0-9 ._:#()/,-]+"
_GIT = rf"git(?: -C (?P<target>{_PATH}))? "
# The closed grammar. Each form must match the whole raw command text on one line.
_FORMS = tuple(
    re.compile(form)
    for form in (
        rf"{_GIT}(?P<args>merge(?: --no-ff)?(?: --no-edit)? {_PATH})",
        rf"{_GIT}(?P<args>merge --(?:continue|abort))",
        rf"{_GIT}(?P<args>add -- {_PATH}(?: {_PATH})*)",
        rf"{_GIT}(?P<args>commit --no-edit)",
        rf"{_GIT}(?P<args>commit(?: --no-edit)? -m) '(?P<message>{_MESSAGE})'",
    )
)


@dataclass(frozen=True)
class BaseMergeAdmission:
    """Which steps of one tool call the merge capability admits."""

    git_step: bool = False
    write: bool = False


NOT_ADMITTED = BaseMergeAdmission()


async def base_merge_admission(
    db: HubDatabase,
    *,
    session_id: str,
    project_id: str,
    project_path: str,
    cwd: str | None,
    command: str | None,
    tool_kind: str | None,
    tool_input: Any,
) -> BaseMergeAdmission:
    """Admit this tool call's base-merge steps, or admit nothing.

    A missing grant, an unregistered worktree, an unresolved tip, or a git failure
    returns `NOT_ADMITTED`, which leaves the claim and review gates in force.
    """
    if not is_session_uuid(session_id) or not session_has_capability(db, session_id, CAPABILITY):
        return NOT_ADMITTED
    call = _lone_git_command(command)
    if command and call is None:
        return NOT_ADMITTED
    anchor = cwd
    if call is not None and call.target is not None:
        # `-C` names the worktree the call runs in. Its root is the merge root, with no
        # fallback to the tool's cwd: a missing or non-git target is refused.
        anchor = str(Path(cwd or project_path) / call.target)
        root = await resolve_git_worktree_root_async(anchor)
    else:
        root = await resolve_git_worktree_root_async(cwd, project_path)
    if root is None:
        return NOT_ADMITTED
    try:
        worktree = await asyncio.to_thread(LocalWorktreeManager(db).get_by_path, root)
        if worktree is None or not worktree.task_id:
            return NOT_ADMITTED
        task = await asyncio.to_thread(LocalTaskManager(db).get_task, worktree.task_id)
        tips = await _admitted_tips(db, task, root)
    except (psycopg.Error, PoolTimeout, RuntimeError, ValueError, OSError, TaskNotFoundError):
        logger.warning("Could not admit a base merge step", exc_info=True)
        return NOT_ADMITTED
    if tips is None:
        return NOT_ADMITTED

    merge_head = await _git(root, "rev-parse", "-q", "--verify", "MERGE_HEAD")
    tokens = call.tokens if call is not None else None
    if merge_head is not None:
        if merge_head not in tips:
            return NOT_ADMITTED
        git_step = tokens is not None and (
            await _completes_admitted_merge(root, merge_head, tokens)
            or await _stages_conflicted_paths(root, tokens, anchor)
        )
        # Only a structured write earns a path grant. A shell command's output is unnamed.
        write = (
            not command
            and tool_kind == "write"
            and await _writes_conflicted_paths(root, get_touched_file_paths(tool_input))
        )
        return BaseMergeAdmission(git_step=git_step, write=write)
    git_step = tokens is not None and await _starts_admitted_merge(root, tokens, tips)
    return BaseMergeAdmission(git_step=git_step)


async def _admitted_tips(db: HubDatabase, task: Task, root: str) -> frozenset[str] | None:
    """Commits of the integration branch and of the task's landing branch."""
    from gobby.tasks.land_commit import main_checkout_target, task_landing_target

    integration = await main_checkout_target(db, task.project_id)
    landing = await task_landing_target(db, task)
    tips: set[str] = set()
    for target in (integration, landing):
        if target is None:
            return None
        _path, branch = target
        tip = await _git(root, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}")
        if tip is None:
            return None
        tips.add(tip)
    return frozenset(tips)


@dataclass(frozen=True)
class _GitCall:
    """A git call the closed grammar admits: its `-C` path and its argument tokens."""

    target: str | None
    tokens: list[str]


def _lone_git_command(command: str | None) -> _GitCall | None:
    """The one git call a raw command spells in the closed grammar, or None.

    The whole text must fit one form. A second segment, a `cd`, a global option, a
    redirection, a quote, a comment, a heredoc or a substitution fails the match, so
    no shell reading decides admission. A `-C` path is the call's target.
    """
    if not isinstance(command, str):
        return None
    for form in _FORMS:
        match = form.fullmatch(command)
        if match is None:
            continue
        tokens = ["git", *(token for token in match["args"].split(" ") if token != "--")]
        message = match.groupdict().get("message")
        if message is not None:
            tokens.append(message)
        return _GitCall(target=match["target"], tokens=tokens)
    return None


def _merge_head_arg(args: Sequence[str]) -> str | None:
    """The one ref a plain merge names, or None when another operand or flag appears."""
    heads: list[str] = []
    for token in args:
        if token in _MERGE_FLAGS:
            continue
        if token.startswith("-"):
            return None
        heads.append(token)
    return heads[0] if len(heads) == 1 else None


def _plain_commit_args(args: Sequence[str]) -> bool:
    """Whether commit options leave the staged set as it is: no pathspec, no `-a`."""
    index = 0
    while index < len(args):
        token = args[index]
        index += 1
        if token == "--no-edit":
            continue
        if token in _COMMIT_VALUE_FLAGS and index < len(args):
            index += 1
            continue
        return False
    return True


async def _starts_admitted_merge(root: str, tokens: list[str], tips: frozenset[str]) -> bool:
    if tokens[1] != "merge":
        return False
    head = _merge_head_arg(tokens[2:])
    if head is None:
        return False
    tip = await _git(root, "rev-parse", "--verify", "--end-of-options", f"{head}^{{commit}}")
    if tip is None or tip not in tips:
        return False
    status = await _git_raw(root, "status", "--porcelain", "--untracked-files=no")
    return status is not None and not status.strip()


async def _completes_admitted_merge(root: str, merge_head: str, tokens: list[str]) -> bool:
    """Whether this git call completes or unwinds the in-progress merge."""
    subcommand, args = tokens[1], tokens[2:]
    if subcommand == "merge" and args == ["--abort"]:
        return True
    if subcommand == "commit":
        if not _plain_commit_args(args):
            return False
    elif subcommand == "merge":
        if args != ["--continue"]:
            return False
    else:
        return False
    base = await _git(root, "merge-base", "HEAD", merge_head)
    if base is None:
        return False
    incoming = await _paths(root, base, merge_head)
    staged = await _paths(root, "--cached")
    return incoming is not None and staged is not None and staged <= incoming


async def _stages_conflicted_paths(root: str, tokens: list[str], cwd: str | None = None) -> bool:
    """Whether `git add` names only paths the in-progress merge left conflicted.

    Operands resolve against the directory the command runs in, so `git add shared.txt`
    from a subdirectory names that subdirectory's file, not the conflicted one.
    """
    if tokens[1] != "add":
        return False
    operands = tokens[2:]
    if any(token.startswith("-") for token in operands):
        return False
    base = cwd or root
    return await _writes_conflicted_paths(root, [str(Path(base) / token) for token in operands])


async def _writes_conflicted_paths(root: str, touched: Sequence[str]) -> bool:
    """Whether every touched path is one the in-progress merge left conflicted."""
    if not touched:
        return False
    conflicted = await _paths(root, "--diff-filter=U")
    if not conflicted:
        return False
    base = Path(root).resolve()
    for raw in touched:
        path = Path(raw)
        if not path.is_absolute():
            path = base / path
        try:
            relative = path.resolve().relative_to(base).as_posix()
        except ValueError:
            return False
        if relative not in conflicted:
            return False
    return True


async def _paths(root: str, *diff_args: str) -> frozenset[str] | None:
    """Repository-relative paths one `git diff` lists, or None when git refuses."""
    raw = await _git_raw(root, "diff", "-z", "--name-only", *diff_args)
    if raw is None:
        return None
    return frozenset(path for path in raw.split("\0") if path)


async def _git(cwd: str, *args: str) -> str | None:
    """Stripped stdout of one git call, or None when git refuses it or prints nothing."""
    raw = await _git_raw(cwd, *args)
    if raw is None or not raw.strip():
        return None
    return raw.strip()


async def _git_raw(cwd: str, *args: str) -> str | None:
    result = await daemon_git.run(list(args), cwd=cwd, timeout=_GIT_TIMEOUT)
    return result.stdout if isinstance(result, GitOk) else None
