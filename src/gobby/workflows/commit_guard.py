"""Cross-session Git commit ownership guard."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg_pool import PoolTimeout

from gobby.terminal_ownership import TERMINAL_OWNER_STATUSES
from gobby.utils.daemon_git import GitOk, daemon_git, parse_porcelain_v1_z
from gobby.workflows.observer_utils import _extract_shell_command
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.task_claim_state import (
    normalize_task_edited_path,
    task_edited_file_set_for_checkout,
)
from gobby.workflows.task_dirty_state import task_dirty_paths_async

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
    chdir: str | None = None
    work_tree: str | None = None
    root_options: tuple[str, ...] = ()

    @property
    def is_path_scoped(self) -> bool:
        return bool(self.pathspecs)


@dataclass(frozen=True)
class ForeignPathOwner:
    """Active foreign task/session attribution for one repo path."""

    path: str
    session_ref: str
    task_ref: str
    owner_session_id: str
    owner_task_id: str


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

        commit_index, chdir, work_tree, root_options = _skip_git_global_options(tokens, index + 1)
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
        invocations.append(
            GitCommitInvocation(
                pathspecs=pathspecs,
                chdir=chdir,
                work_tree=work_tree,
                root_options=root_options,
            )
        )
        index = segment_end + 1

    return tuple(invocations)


def _skip_git_global_options(
    tokens: list[str], index: int
) -> tuple[int, str | None, str | None, tuple[str, ...]]:
    chdir: str | None = None
    work_tree: str | None = None
    root_options: list[str] = []
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return index + 1, chdir, work_tree, tuple(root_options)
        if token == "-C" and index + 1 < len(tokens):
            chdir = _join_chdir(chdir, tokens[index + 1])
            root_options.extend(tokens[index : index + 2])
            index += 2
            continue
        if token.startswith("-C="):
            chdir = _join_chdir(chdir, token[3:])
            root_options.append(token)
            index += 1
            continue
        if token == "--work-tree" and index + 1 < len(tokens):
            work_tree = tokens[index + 1]
            root_options.extend(tokens[index : index + 2])
            index += 2
            continue
        if token.startswith("--work-tree="):
            work_tree = token.split("=", 1)[1]
            root_options.append(token)
            index += 1
            continue
        if token in _GIT_GLOBAL_OPTIONS_WITH_VALUE:
            if token == "--git-dir":
                root_options.extend(tokens[index : index + 2])
            index += 2
            continue
        if any(token.startswith(f"{option}=") for option in _GIT_GLOBAL_OPTIONS_WITH_VALUE):
            if token.startswith("--git-dir="):
                root_options.append(token)
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        return index, chdir, work_tree, tuple(root_options)
    return index, chdir, work_tree, tuple(root_options)


def _join_chdir(current: str | None, nxt: str) -> str:
    if not current:
        return nxt
    nxt_path = Path(nxt)
    if nxt_path.is_absolute():
        return nxt
    return str(Path(current) / nxt_path)


def resolve_commit_inspect_cwd(
    invocation: GitCommitInvocation,
    *,
    event_cwd: str | None,
    project_path: str,
) -> str:
    """Working tree git should inspect for this commit, not the project default."""
    base = event_cwd or project_path
    if invocation.chdir:
        chdir = Path(invocation.chdir)
        base = str(chdir if chdir.is_absolute() else Path(base) / chdir)
    if invocation.work_tree:
        work_tree = Path(invocation.work_tree)
        base = str(work_tree if work_tree.is_absolute() else Path(base) / work_tree)
    return os.path.normpath(base)


def _normalized_command_cwd(event: HookEvent, project_path: str) -> str:
    """Resolve relative tool paths from the cwd used by the tool invocation."""
    raw_event_cwd = event.cwd if isinstance(event.cwd, str) and event.cwd.strip() else project_path
    event_cwd = Path(raw_event_cwd).expanduser()
    if not event_cwd.is_absolute():
        event_cwd = Path(project_path) / event_cwd

    data = event.data if isinstance(event.data, dict) else {}
    tool_input = data.get("tool_input")
    if isinstance(tool_input, Mapping):
        for key in ("workdir", "cwd"):
            raw_tool_cwd = tool_input.get(key)
            if not isinstance(raw_tool_cwd, str) or not raw_tool_cwd.strip():
                continue
            tool_cwd = Path(raw_tool_cwd).expanduser()
            if not tool_cwd.is_absolute():
                tool_cwd = event_cwd / tool_cwd
            return os.path.normpath(str(tool_cwd))
    return os.path.normpath(str(event_cwd))


async def _resolve_commit_checkout_root(
    invocation: GitCommitInvocation,
    *,
    command_cwd: str,
) -> str | None:
    """Return the invocation's Git top-level, or ``None`` for a non-repository cwd."""
    result = await daemon_git.run(
        [*invocation.root_options, "rev-parse", "--show-toplevel"],
        cwd=command_cwd,
        timeout=10.0,
    )
    if isinstance(result, GitOk):
        checkout_root = result.stdout.strip()
        if checkout_root:
            return os.path.normpath(checkout_root)
        raise DirtyEditOwnershipInspectionError("git rev-parse returned no worktree root")

    detail = result.stderr.strip() or result.status
    if "not a git repository" in detail.lower():
        return None
    raise DirtyEditOwnershipInspectionError(f"git rev-parse --show-toplevel failed: {detail}")


async def foreign_staged_commit_conflict(
    db: HubDatabase,
    event: HookEvent,
    *,
    session_id: str,
    project_id: str,
    project_path: str,
) -> str:
    """Inspect commit ownership without parking a workflow worker on Git."""
    invocations = parse_git_commit_invocations(_extract_shell_command(event))
    if not invocations:
        return ""

    try:
        owners_by_checkout: dict[str, dict[str, tuple[ForeignPathOwner, ...]]] = {}
        candidates_by_checkout: dict[str, set[str]] = {}
        staged_paths_by_checkout: dict[str, set[str]] = {}
        command_cwd = _normalized_command_cwd(event, project_path)
        for invocation in invocations:
            checkout_root = await _resolve_commit_checkout_root(
                invocation,
                command_cwd=command_cwd,
            )
            if checkout_root is None:
                continue

            if checkout_root not in owners_by_checkout:
                try:
                    owners_by_checkout[checkout_root] = await asyncio.to_thread(
                        _active_foreign_path_owners,
                        db,
                        session_id=session_id,
                        project_id=project_id,
                        checkout_root=checkout_root,
                    )
                except (psycopg.OperationalError, PoolTimeout) as exc:
                    raise DirtyEditOwnershipInspectionError(
                        "database ownership inspection failed"
                    ) from exc
            owners = owners_by_checkout[checkout_root]
            if not owners:
                continue

            inspect_cwd = resolve_commit_inspect_cwd(
                invocation,
                event_cwd=command_cwd,
                project_path=project_path,
            )
            if invocation.is_path_scoped:
                candidate_paths = await _git_paths_async(
                    inspect_cwd,
                    "ls-files",
                    "-z",
                    "--full-name",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                    "--",
                    *invocation.pathspecs,
                )
            else:
                staged_paths = staged_paths_by_checkout.get(checkout_root)
                if staged_paths is None:
                    staged_paths = await _git_paths_async(
                        checkout_root,
                        "diff",
                        "--cached",
                        "--name-only",
                        "-z",
                        "--diff-filter=ACDMRTUXB",
                    )
                    staged_paths_by_checkout[checkout_root] = staged_paths
                candidate_paths = staged_paths
            candidates_by_checkout.setdefault(checkout_root, set()).update(
                path for path in candidate_paths if path in owners
            )

        # A pathspec lists clean tracked files too, so ownership alone is not a
        # conflict: only a candidate that still differs from HEAD is foreign work.
        conflicts: set[ForeignPathOwner] = set()
        for checkout_root, owned_candidates in candidates_by_checkout.items():
            owners = owners_by_checkout[checkout_root]
            dirty_paths = await _dirty_owned_paths_releasing_clean(
                db,
                owners,
                owned_candidates,
                checkout_root=checkout_root,
            )
            if dirty_paths is None:
                raise DirtyEditOwnershipInspectionError("git status unavailable for owned paths")
            conflicts.update(owner for path in dirty_paths for owner in owners.get(path, ()))
        return _format_conflict_reason(conflicts) if conflicts else ""
    except DirtyEditOwnershipInspectionError as exc:
        logger.warning(
            "Cross-session commit ownership inspection failed",
            extra={"session_id": session_id, "project_id": project_id},
            exc_info=True,
        )
        # The cause decides what to do next: a malformed pathspec is the
        # caller's to fix, a pool timeout is worth retrying. Dropping it left
        # "retry" as the only advice, which is wrong half the time.
        return (
            f"Commit blocked: Gobby could not verify staged-path ownership ({exc}). "
            "Fix that cause, or retry once the daemon and repository are available."
        )


async def foreign_dirty_edit_conflict(
    db: HubDatabase,
    event: HookEvent,
    *,
    session_id: str,
    project_id: str,
    project_path: str,
) -> str:
    """Return an actionable block reason for edits to dirty foreign-owned paths.

    Candidates are the edit's paths that another active session's task ledger
    claims; only those are verified against git, with a pathspec-limited
    status. An unverifiable candidate blocks rather than guesses.
    """
    data = event.data if isinstance(event.data, dict) else {}
    if data.get("canonical_repo_mutation") is not True:
        return ""

    try:
        mutation_paths = _canonical_mutation_paths(event, project_path)
        if not mutation_paths:
            return ""

        try:
            owners = await asyncio.to_thread(
                _active_foreign_path_owners,
                db,
                session_id=session_id,
                project_id=project_id,
                checkout_root=project_path,
            )
        except (psycopg.OperationalError, PoolTimeout) as exc:
            raise DirtyEditOwnershipInspectionError("database ownership inspection failed") from exc
        candidate_paths = mutation_paths & owners.keys()
        if not candidate_paths:
            return ""

        dirty_paths = await _dirty_owned_paths_releasing_clean(
            db,
            owners,
            candidate_paths,
            checkout_root=project_path,
        )
        if dirty_paths is None:
            return _format_unverified_dirty_edit_reason(candidate_paths)
        conflicts = {owner for path in dirty_paths for owner in owners.get(path, ())}
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
    tool_cwd = Path(_normalized_command_cwd(event, project_path)).resolve()

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
            sessions.seq_num AS session_seq_num,
            sessions.project_id AS session_project_id,
            projects.name AS project_name
        FROM tasks
        JOIN sessions ON sessions.id = tasks.claimed_by_session_id
        LEFT JOIN projects ON projects.id = sessions.project_id
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

        session_ref = _format_session_ref(
            row["session_seq_num"],
            owner_session_id,
            project_name=row["project_name"],
            project_id=row["session_project_id"],
        )
        task_ref = _format_ref(row["task_seq_num"], task_id)
        for path in paths:
            owners.setdefault(path, []).append(
                ForeignPathOwner(
                    path=path,
                    session_ref=session_ref,
                    task_ref=task_ref,
                    owner_session_id=owner_session_id,
                    owner_task_id=task_id,
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


def _release_clean_ledger_entries(
    db: HubDatabase,
    owners: Mapping[str, tuple[ForeignPathOwner, ...]],
    clean_paths: AbstractSet[str],
    checkout_root: str,
) -> None:
    """Drop the ledger entries that claim ``clean_paths`` in ``checkout_root``."""
    variable_manager = SessionVariableManager(db)
    paths_by_owner: dict[tuple[str, str], list[str]] = {}
    for path in sorted(clean_paths):
        for owner in owners.get(path, ()):
            paths_by_owner.setdefault((owner.owner_session_id, owner.owner_task_id), []).append(
                path
            )
    for (owner_session_id, owner_task_id), paths in paths_by_owner.items():
        try:
            variable_manager.release_task_edited_files(
                owner_session_id,
                owner_task_id,
                paths,
                checkout_root=checkout_root,
            )
        except (psycopg.OperationalError, PoolTimeout):
            # Releasing a stale entry is ledger hygiene, not part of the ownership
            # decision: git already proved the path clean, so a database hiccup
            # leaves the entry for a later check instead of failing the caller.
            logger.warning(
                "Could not release stale clean ledger entries for session %s task %s",
                owner_session_id,
                owner_task_id,
                exc_info=True,
            )


async def _dirty_owned_paths_releasing_clean(
    db: HubDatabase,
    owners: Mapping[str, tuple[ForeignPathOwner, ...]],
    candidate_paths: AbstractSet[str],
    *,
    checkout_root: str,
) -> set[str] | None:
    """Split owned candidates into real dirt and released stale entries.

    A ledger entry whose path has no index or worktree difference from HEAD in
    the checkout that recorded it describes committed work. It is released from
    that owner's checkout-scoped ledger and dropped from the returned dirt, so
    the path stops counting as owned even once the next session's own edit
    dirties it. ``None`` means git could not verify the candidates: nothing is
    released and every entry stands.
    """
    candidates = set(candidate_paths)
    if not candidates:
        return set()
    dirty_paths = await task_dirty_paths_async(candidates, checkout_root)
    if dirty_paths is None:
        return None
    dirty = candidates & {
        normalized
        for path in dirty_paths
        if (normalized := normalize_task_edited_path(path)) is not None
    }
    clean = candidates - dirty
    if clean:
        await asyncio.to_thread(_release_clean_ledger_entries, db, owners, clean, checkout_root)
    return dirty


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


async def foreign_owned_dirty_paths_async(
    db: HubDatabase,
    *,
    session_id: str,
    project_id: str,
    checkout_root: str,
    paths: AbstractSet[str] | None = None,
) -> set[str]:
    """Return foreign-attributed paths that are dirty in ``checkout_root``.

    Verifies only the paths another active session's open task claims, narrowed
    to ``paths`` when one is given, so git never walks the tree. Entries whose
    path is clean there are released as stale. Raises
    DirtyEditOwnershipInspectionError when the ownership query or the bounded
    status is unavailable.
    """
    try:
        owners = await asyncio.to_thread(
            _active_foreign_path_owners,
            db,
            session_id=session_id,
            project_id=project_id,
            checkout_root=checkout_root,
        )
    except (psycopg.OperationalError, PoolTimeout) as exc:
        raise DirtyEditOwnershipInspectionError("database ownership inspection failed") from exc
    candidates = set(owners) if paths is None else {path for path in paths if path in owners}
    dirty_paths = await _dirty_owned_paths_releasing_clean(
        db,
        owners,
        candidates,
        checkout_root=checkout_root,
    )
    if dirty_paths is None:
        raise DirtyEditOwnershipInspectionError("git status unavailable for owned paths")
    return dirty_paths


async def inspect_checkout_path_ownership_async(
    db: HubDatabase,
    *,
    project_id: str,
    checkout_root: str,
) -> tuple[CheckoutPathOwnership, ...]:
    """Inspect dirty ownership without blocking a daemon worker on Git."""
    try:
        owners = await asyncio.to_thread(
            _active_path_owners,
            db,
            project_id=project_id,
            checkout_root=checkout_root,
        )
    except (psycopg.OperationalError, PoolTimeout) as exc:
        raise DirtyEditOwnershipInspectionError("database ownership inspection failed") from exc
    states = await _git_status_path_states_async(checkout_root)
    return tuple(
        CheckoutPathOwnership(
            path=path,
            dirty=True,
            staged=staged,
            owners=owners.get(path, ()),
        )
        for path, staged in sorted(states.items())
    )


async def _git_status_path_states_async(project_path: str) -> dict[str, bool]:
    """Map porcelain status paths to staged state through the daemon Git service."""
    result = await daemon_git.status(project_path, timeout=10.0)
    if not isinstance(result, GitOk):
        detail = result.stderr.strip() or result.status
        raise DirtyEditOwnershipInspectionError(f"git status failed: {detail}")

    states: dict[str, bool] = {}
    for entry in parse_porcelain_v1_z(result.stdout):
        staged = entry.code[0] not in {" ", "?"}
        for raw_path in (entry.path, entry.original_path):
            path = normalize_task_edited_path(raw_path) if raw_path is not None else None
            if path is not None:
                states[path] = states.get(path, False) or staged
    return states


async def _git_paths_async(project_path: str, *args: str) -> set[str]:
    result = await daemon_git.run(
        ["--literal-pathspecs", *args],
        cwd=project_path,
        timeout=10.0,
    )
    if not isinstance(result, GitOk):
        detail = result.stderr.strip() or result.status
        raise DirtyEditOwnershipInspectionError(f"git {' '.join(args[:2])} failed: {detail}")
    return {
        path
        for raw_path in result.stdout.split("\0")
        if (path := normalize_task_edited_path(raw_path)) is not None
    }


def _format_ref(seq_num: object, fallback_id: str) -> str:
    return f"#{seq_num}" if isinstance(seq_num, int) else fallback_id


def _format_session_ref(
    seq_num: object,
    session_id: str,
    *,
    project_name: object,
    project_id: object,
) -> str:
    """Project-qualified session reference, matching ``Session.ref``."""
    if not isinstance(seq_num, int):
        return session_id
    name = project_name.strip() if isinstance(project_name, str) else ""
    project = name or (str(project_id) if project_id else "")
    return f"{project}#{seq_num}"


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


def _format_unverified_dirty_edit_reason(paths: AbstractSet[str]) -> str:
    listed = ", ".join(sorted(paths))
    return (
        f"Edit blocked: Gobby could not verify whether {listed} carries another "
        "session's uncommitted work (git status unavailable). Retry once the "
        "repository is responsive."
    )


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
