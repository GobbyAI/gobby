"""Cross-session Git commit ownership guard."""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
from collections.abc import Callable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg_pool import PoolTimeout

from gobby.terminal_ownership import TERMINAL_OWNER_STATUSES
from gobby.workflows.observer_utils import _extract_shell_command
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.task_claim_state import (
    normalize_task_edited_path,
    task_edited_file_set_for_checkout,
)

if TYPE_CHECKING:
    from gobby.hooks.events import HookEvent
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)

_GIT_GLOBAL_OPTIONS_WITH_VALUE = {
    "-C",
    "-c",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--exec-path",
}
_SHELL_CONTROL_TOKENS = frozenset({"&&", "||", ";", "|", "&"})

# A heredoc body is unquoted shell text, so `shlex.split` turns every word in it
# into a token. A commit message delivered that way therefore reaches the
# invocation parser as argv: one bare `--` line reads as git's pathspec
# delimiter and the prose after it becomes pathspecs. Strip bodies before
# tokenizing. `-m "..."` needs no such handling because shlex keeps a quoted
# argument whole.
_HEREDOC_BODY_RE = re.compile(
    r"""<<-?\s*(?P<quote>['"]?)(?P<tag>[A-Za-z_][A-Za-z0-9_]*)(?P=quote)"""
    r".*?^[ \t]*(?P=tag)[ \t]*$",
    re.DOTALL | re.MULTILINE,
)

# `shlex.split` discards newlines, so newline-separated commands run together
# into one token stream and the segment scan never terminates. Two chained
# commits then parse as one invocation carrying the *later* command's
# pathspecs, dropping the earlier unscoped commit's full-staged-set check.
# Fold real separators into `;` so the scan ends where the command does; a
# newline inside a quoted argument survives as ordinary token content.
_LINE_CONTINUATION_RE = re.compile(r"\\\r?\n")


def _normalize_shell_command(command: str) -> str:
    command = _HEREDOC_BODY_RE.sub(" ", command)
    command = _LINE_CONTINUATION_RE.sub(" ", command)
    return command.replace("\n", " ; ")


@dataclass(frozen=True)
class GitCommitInvocation:
    """One Git commit invocation and its explicit pathspecs."""

    pathspecs: tuple[str, ...]

    @property
    def is_path_scoped(self) -> bool:
        return bool(self.pathspecs)


@dataclass(frozen=True)
class ForeignPathOwner:
    """Active foreign task/session attribution for one repo path."""

    path: str
    session_ref: str
    task_ref: str


@dataclass(frozen=True)
class CheckoutPathOwnership:
    """Dirty checkout path plus every active task attribution for it."""

    path: str
    dirty: bool
    staged: bool
    owners: tuple[ForeignPathOwner, ...]


class DirtyEditOwnershipInspectionError(RuntimeError):
    """Expected Git or database failure while inspecting dirty edit ownership."""


def parse_git_commit_invocations(command: str) -> tuple[GitCommitInvocation, ...]:
    """Parse Git commit invocations from one shell command."""
    if not command.strip():
        return ()
    command = _normalize_shell_command(command)
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    invocations: list[GitCommitInvocation] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.rsplit("/", maxsplit=1)[-1] != "git":
            index += 1
            continue

        commit_index = _skip_git_global_options(tokens, index + 1)
        if commit_index >= len(tokens) or tokens[commit_index] != "commit":
            index += 1
            continue

        segment_end = commit_index + 1
        while segment_end < len(tokens) and tokens[segment_end] not in _SHELL_CONTROL_TOKENS:
            segment_end += 1
        segment = tokens[commit_index + 1 : segment_end]
        delimiter_index = segment.index("--") if "--" in segment else -1
        pathspecs = (
            tuple(segment[delimiter_index + 1 :])
            if delimiter_index >= 0 and delimiter_index + 1 < len(segment)
            else ()
        )
        invocations.append(GitCommitInvocation(pathspecs=pathspecs))
        index = segment_end + 1

    return tuple(invocations)


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


def foreign_staged_commit_conflict(
    db: HubDatabase,
    event: HookEvent,
    *,
    session_id: str,
    project_id: str,
    project_path: str,
) -> str:
    """Return an actionable block reason when a commit would capture foreign paths."""
    invocations = parse_git_commit_invocations(_extract_shell_command(event))
    if not invocations:
        return ""

    try:
        try:
            owners = _active_foreign_path_owners(
                db,
                session_id=session_id,
                project_id=project_id,
                checkout_root=project_path,
            )
        except (psycopg.OperationalError, PoolTimeout) as exc:
            raise DirtyEditOwnershipInspectionError("database ownership inspection failed") from exc
        if not owners:
            return ""

        conflicts: set[ForeignPathOwner] = set()
        staged_paths: set[str] | None = None
        for invocation in invocations:
            if invocation.is_path_scoped:
                candidate_paths = _git_paths(
                    project_path,
                    "ls-files",
                    "-z",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                    "--",
                    *invocation.pathspecs,
                )
            else:
                if staged_paths is None:
                    staged_paths = _git_paths(
                        project_path,
                        "diff",
                        "--cached",
                        "--name-only",
                        "-z",
                        "--diff-filter=ACDMRTUXB",
                    )
                candidate_paths = staged_paths

            for path in candidate_paths:
                conflicts.update(owners.get(path, ()))

        if not conflicts:
            return ""
        return _format_conflict_reason(conflicts)
    except DirtyEditOwnershipInspectionError:
        logger.warning(
            "Cross-session commit ownership inspection failed",
            extra={"session_id": session_id, "project_id": project_id},
            exc_info=True,
        )
        return (
            "Commit blocked: Gobby could not verify staged-path ownership. "
            "Retry after the daemon and repository are available."
        )


def foreign_dirty_edit_conflict(
    db: HubDatabase,
    event: HookEvent,
    *,
    session_id: str,
    project_id: str,
    project_path: str,
    dirty_files: Callable[[], AbstractSet[str]],
) -> str:
    """Return an actionable block reason for edits to dirty foreign-owned paths."""
    data = event.data if isinstance(event.data, dict) else {}
    if data.get("canonical_repo_mutation") is not True:
        return ""

    try:
        mutation_paths = _canonical_mutation_paths(event, project_path)
        if not mutation_paths:
            return ""

        normalized_dirty = {
            normalized
            for path in dirty_files()
            if (normalized := normalize_task_edited_path(path)) is not None
        }
        candidate_paths = mutation_paths & normalized_dirty
        if not candidate_paths:
            return ""

        try:
            owners = _active_foreign_path_owners(
                db,
                session_id=session_id,
                project_id=project_id,
                checkout_root=project_path,
            )
        except (psycopg.OperationalError, PoolTimeout) as exc:
            raise DirtyEditOwnershipInspectionError("database ownership inspection failed") from exc
        conflicts = {owner for path in candidate_paths for owner in owners.get(path, ())}
        if not conflicts:
            return ""
        return _format_dirty_edit_reason(conflicts)
    except DirtyEditOwnershipInspectionError:
        logger.warning(
            "Cross-session dirty edit ownership inspection failed",
            extra={"session_id": session_id, "project_id": project_id},
            exc_info=True,
        )
        return ""


def _canonical_mutation_paths(event: HookEvent, project_path: str) -> set[str]:
    data = event.data if isinstance(event.data, dict) else {}
    raw_paths = data.get("canonical_file_paths")
    if not isinstance(raw_paths, list):
        single_path = data.get("canonical_file_path")
        raw_paths = [single_path] if isinstance(single_path, str) else []

    repository_root = Path(project_path).resolve()
    raw_cwd = data.get("cwd")
    tool_cwd = Path(raw_cwd) if isinstance(raw_cwd, str) and raw_cwd else Path(event.cwd or "")
    if not tool_cwd.is_absolute():
        tool_cwd = repository_root / tool_cwd
    tool_cwd = tool_cwd.resolve()

    paths: set[str] = set()
    for raw_path in raw_paths:
        if not isinstance(raw_path, str) or not raw_path:
            continue
        path = Path(raw_path)
        if not path.is_absolute():
            path = tool_cwd / path
        try:
            relative_path = path.resolve().relative_to(repository_root).as_posix()
        except ValueError:
            continue
        normalized = normalize_task_edited_path(relative_path)
        if normalized is not None:
            paths.add(normalized)
    return paths


def _active_path_owners(
    db: HubDatabase,
    *,
    project_id: str,
    checkout_root: str,
    exclude_session_id: str | None = None,
) -> dict[str, tuple[ForeignPathOwner, ...]]:
    rows = db.fetchall(
        """
        SELECT
            tasks.id AS task_id,
            tasks.seq_num AS task_seq_num,
            sessions.id AS session_id,
            sessions.seq_num AS session_seq_num
        FROM tasks
        JOIN sessions ON sessions.id = tasks.claimed_by_session_id
        WHERE tasks.project_id = %s
          AND tasks.claimed_by_session_id IS NOT NULL
          AND tasks.closed_at IS NULL
          AND sessions.id IS DISTINCT FROM %s
          AND sessions.status = ANY(%s)
        ORDER BY sessions.seq_num, tasks.seq_num
        """,
        (project_id, exclude_session_id, list(TERMINAL_OWNER_STATUSES)),
    )
    variable_manager = SessionVariableManager(db)
    variables_by_session: dict[str, dict[str, Any]] = {}
    owners: dict[str, list[ForeignPathOwner]] = {}

    for row in rows:
        owner_session_id = str(row["session_id"])
        variables = variables_by_session.get(owner_session_id)
        if variables is None:
            variables = variable_manager.get_variables(owner_session_id)
            variables_by_session[owner_session_id] = variables
        task_id = str(row["task_id"])
        paths = task_edited_file_set_for_checkout(variables, task_id, checkout_root)

        session_ref = _format_ref(row["session_seq_num"], owner_session_id)
        task_ref = _format_ref(row["task_seq_num"], task_id)
        for path in paths:
            owners.setdefault(path, []).append(
                ForeignPathOwner(
                    path=path,
                    session_ref=session_ref,
                    task_ref=task_ref,
                )
            )

    return {path: tuple(path_owners) for path, path_owners in owners.items()}


def _active_foreign_path_owners(
    db: HubDatabase,
    *,
    session_id: str,
    project_id: str,
    checkout_root: str,
) -> dict[str, tuple[ForeignPathOwner, ...]]:
    return _active_path_owners(
        db,
        project_id=project_id,
        checkout_root=checkout_root,
        exclude_session_id=session_id,
    )


def foreign_owned_dirty_paths(
    db: HubDatabase,
    *,
    session_id: str,
    project_id: str,
    checkout_root: str,
    paths: AbstractSet[str],
) -> dict[str, tuple[ForeignPathOwner, ...]]:
    """Resolve which of the given paths carry another active session's open-task attribution.

    Raises DirtyEditOwnershipInspectionError for expected infrastructure failures so
    callers choose their own fail-open or fail-closed posture at the boundary.
    """
    try:
        owners = _active_foreign_path_owners(
            db,
            session_id=session_id,
            project_id=project_id,
            checkout_root=checkout_root,
        )
    except (psycopg.OperationalError, PoolTimeout) as exc:
        raise DirtyEditOwnershipInspectionError("database ownership inspection failed") from exc
    return {path: owners[path] for path in paths if path in owners}


def inspect_checkout_path_ownership(
    db: HubDatabase,
    *,
    project_id: str,
    checkout_root: str,
) -> tuple[CheckoutPathOwnership, ...]:
    """Return every dirty or staged path with active ownership, including gaps."""
    try:
        owners = _active_path_owners(
            db,
            project_id=project_id,
            checkout_root=checkout_root,
        )
        states = _git_status_path_states(checkout_root)
    except (psycopg.OperationalError, PoolTimeout) as exc:
        raise DirtyEditOwnershipInspectionError("database ownership inspection failed") from exc
    return tuple(
        CheckoutPathOwnership(
            path=path,
            dirty=True,
            staged=staged,
            owners=owners.get(path, ()),
        )
        for path, staged in sorted(states.items())
    )


def _git_status_path_states(project_path: str) -> dict[str, bool]:
    """Map porcelain-status paths to whether each path has an index change."""
    try:
        result = subprocess.run(  # Hardcoded git command. # nosec B603 B607
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=Path(project_path),
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DirtyEditOwnershipInspectionError("git ownership inspection failed") from exc
    if result.returncode != 0:
        stderr = os.fsdecode(result.stderr).strip()
        raise DirtyEditOwnershipInspectionError(f"git status failed: {stderr}")

    states: dict[str, bool] = {}
    records = iter(result.stdout.split(b"\0"))
    for record in records:
        if len(record) < 4:
            continue
        status = record[:2]
        staged = status[:1] not in {b" ", b"?"}
        path = normalize_task_edited_path(os.fsdecode(record[3:]))
        if path is not None:
            states[path] = states.get(path, False) or staged
        if b"R" in status or b"C" in status:
            original = normalize_task_edited_path(os.fsdecode(next(records, b"")))
            if original is not None:
                states[original] = states.get(original, False) or staged
    return states


def _git_paths(project_path: str, *args: str) -> set[str]:
    try:
        result = subprocess.run(  # Hardcoded git command. # nosec B603 B607
            ["git", *args],
            cwd=Path(project_path),
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DirtyEditOwnershipInspectionError("git ownership inspection failed") from exc
    if result.returncode != 0:
        stderr = os.fsdecode(result.stderr).strip()
        raise DirtyEditOwnershipInspectionError(f"git {' '.join(args[:2])} failed: {stderr}")
    return {
        path
        for raw_path in result.stdout.split(b"\0")
        if (path := normalize_task_edited_path(os.fsdecode(raw_path))) is not None
    }


def _format_ref(seq_num: object, fallback_id: str) -> str:
    return f"#{seq_num}" if isinstance(seq_num, int) else fallback_id


def _format_conflict_reason(conflicts: set[ForeignPathOwner]) -> str:
    ordered_conflicts = sorted(
        conflicts,
        key=lambda item: (item.path, item.session_ref, item.task_ref),
    )
    lines = [
        "Commit blocked: staged path(s) belong to another active task/session:",
        *[
            f"- {owner.path} — session {owner.session_ref}, task {owner.task_ref}"
            for owner in ordered_conflicts
        ],
        "Ask each owner with `gobby-agents.send_message` to verify its work is committed, "
        "then run its matching release call:",
        *[
            f"- session {owner.session_ref}: "
            f"`gobby-tasks.release_task_paths(task_id={json.dumps(owner.task_ref)}, "
            f"paths=[{json.dumps(owner.path)}])`"
            for owner in ordered_conflicts
        ],
        "Commit only your paths with `git commit --only -- <owned paths>`; "
        "foreign staged entries will remain intact.",
    ]
    return "\n".join(lines)


def _format_dirty_edit_reason(conflicts: set[ForeignPathOwner]) -> str:
    ordered_conflicts = sorted(
        conflicts,
        key=lambda item: (item.path, item.session_ref, item.task_ref),
    )
    return "\n".join(
        [
            "Edit blocked: dirty path(s) belong to another active task/session:",
            *[
                f"- {owner.path} — session {owner.session_ref}, task {owner.task_ref}"
                for owner in ordered_conflicts
            ],
            "Ask each owner with `gobby-agents.send_message` to make a buildable WIP "
            "commit before you continue.",
            "For work that cannot be committed, migrate it through `gobby-worktrees`.",
            "For a stale owner, reclaim its task with:",
            *[
                f"- `gobby-tasks.claim_task(task_id={json.dumps(task_ref)}, force=true)`"
                for task_ref in dict.fromkeys(owner.task_ref for owner in ordered_conflicts)
            ],
        ]
    )
