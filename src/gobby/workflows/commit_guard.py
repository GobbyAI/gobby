"""Cross-session Git commit ownership guard."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg_pool import PoolTimeout

from gobby.hooks.normalization import is_shell_tool, tool_input_error
from gobby.terminal_ownership import TERMINAL_OWNER_STATUSES
from gobby.utils.daemon_git import GitOk, daemon_git, parse_porcelain_v1_z
from gobby.workflows.commit_guard_conflicts import (
    ForeignPathOwner as ForeignPathOwner,
)
from gobby.workflows.commit_guard_conflicts import (
    format_conflict_reason,
    format_dirty_edit_reason,
    format_ref,
    format_session_ref,
    format_unverified_dirty_edit_reason,
    scope_foreign_owners,
)
from gobby.workflows.commit_guard_shell import (
    GitCommitInvocation,
    parse_git_commit_invocations,
    resolve_commit_inspect_cwd,
)
from gobby.workflows.observer_utils import _extract_shell_command
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.task_claim_state import (
    normalize_task_edited_path,
    overlapping_task_edit_paths,
    task_edit_path_covers,
    task_edited_file_set_for_checkout,
)
from gobby.workflows.task_dirty_state import task_dirty_paths_async

if TYPE_CHECKING:
    from gobby.hooks.events import HookEvent
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CheckoutPathOwnership:
    """Dirty checkout path plus every active task attribution for it."""

    path: str
    dirty: bool
    staged: bool
    owners: tuple[ForeignPathOwner, ...]


class DirtyEditOwnershipInspectionError(RuntimeError):
    """Expected Git or database failure while inspecting dirty edit ownership."""


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
    if not is_shell_tool(event.data.get("tool_name")):
        return ""
    if tool_input_error(event.data) is not None:
        # An undecodable shell input may hide a commit, so it can't pass as none.
        return (
            "Shell tool input arrived as undecodable JSON, so Gobby can't check it for a "
            "git commit that would record another session's staged files. Retry the command."
        )
    invocations = parse_git_commit_invocations(_extract_shell_command(event))
    if not invocations:
        return ""
    if any(invocation.cwd_unverified for invocation in invocations):
        # Known unprovable input is a policy refusal, not an operational Git
        # failure. Do not inspect a literal shell variable or page the operator.
        return (
            "Commit blocked: Gobby cannot prove this command's working directory or affected paths. "
            "Use valid shell syntax, a literal workdir and literal paths without patterns "
            "so checkout ownership can be verified."
        )

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
                candidate_paths = set()
            if not invocation.is_path_scoped or invocation.include_staged:
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
                candidate_paths.update(staged_paths)
            if invocation.all_tracked:
                candidate_paths.update(
                    await _git_paths_async(
                        checkout_root,
                        "diff",
                        "HEAD",
                        "--name-only",
                        "-z",
                        "--diff-filter=ACDMRTUXB",
                    )
                )
            candidates_by_checkout.setdefault(checkout_root, set()).update(candidate_paths)

        # A pathspec lists clean tracked files too, so ownership alone is not a
        # conflict: only a candidate that still differs from HEAD is foreign work.
        conflicts: set[ForeignPathOwner] = set()
        for checkout_root, owned_candidates in candidates_by_checkout.items():
            owners = owners_by_checkout[checkout_root]
            current_owned = await asyncio.to_thread(
                _active_current_owned_paths, db, session_id, project_id, checkout_root
            )
            scoped_owners = scope_foreign_owners(owners, owned_candidates, current_owned)
            dirty_paths = await _dirty_owned_paths_releasing_clean(
                db,
                scoped_owners,
                set(scoped_owners),
                checkout_root=checkout_root,
            )
            if dirty_paths is None:
                raise DirtyEditOwnershipInspectionError("git status unavailable for owned paths")
            conflicts.update(owner for path in dirty_paths for owner in scoped_owners.get(path, ()))
        return format_conflict_reason(conflicts) if conflicts else ""
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
            raise DirtyEditOwnershipInspectionError(
                f"database ownership inspection failed: {exc}"
            ) from exc
        candidate_paths = overlapping_task_edit_paths(owners.keys(), mutation_paths)
        if not candidate_paths:
            return ""

        current_owned = await asyncio.to_thread(
            _active_current_owned_paths, db, session_id, project_id, project_path
        )
        scoped_owners = scope_foreign_owners(owners, mutation_paths, current_owned)

        dirty_paths = await _dirty_owned_paths_releasing_clean(
            db,
            scoped_owners,
            set(scoped_owners),
            checkout_root=project_path,
        )
        if dirty_paths is None:
            return format_unverified_dirty_edit_reason(set(scoped_owners))
        conflicts = {owner for path in dirty_paths for owner in scoped_owners.get(path, ())}
        if not conflicts:
            return ""
        return format_dirty_edit_reason(conflicts)
    except DirtyEditOwnershipInspectionError as exc:
        logger.warning(
            "Cross-session dirty edit ownership inspection failed",
            extra={"session_id": session_id, "project_id": project_id},
            exc_info=True,
        )
        return (
            f"Edit blocked: Gobby could not verify dirty-path ownership ({exc}). "
            "Fix that cause, or retry once the daemon and repository are available."
        )


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
            variables = variable_manager.get_variable_subset(
                owner_session_id, ("task_edited_file_checkouts",)
            )
            variables_by_session[owner_session_id] = variables
        task_id = str(row["task_id"])
        paths = task_edited_file_set_for_checkout(variables, task_id, checkout_root)

        session_ref = format_session_ref(
            row["session_seq_num"],
            owner_session_id,
            project_name=row["project_name"],
            project_id=row["session_project_id"],
        )
        task_ref = format_ref(row["task_seq_num"], task_id)
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


def _active_current_owned_paths(
    db: HubDatabase, session_id: str, project_id: str, checkout_root: str
) -> set[str]:
    try:
        owners = _active_path_owners(db, project_id=project_id, checkout_root=checkout_root)
    except (psycopg.OperationalError, PoolTimeout) as exc:
        raise DirtyEditOwnershipInspectionError("database ownership inspection failed") from exc
    return {
        path
        for path, path_owners in owners.items()
        if any(owner.owner_session_id == session_id for owner in path_owners)
    }


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
    normalized_dirty = {
        normalized
        for path in dirty_paths
        if (normalized := normalize_task_edited_path(path)) is not None
    }
    dirty = {
        path
        for path in candidates
        if any(task_edit_path_covers(path, child) for child in normalized_dirty)
    }
    clean = candidates - dirty
    if clean:
        await asyncio.to_thread(
            _release_clean_ledger_entries,
            db,
            owners,
            clean,
            checkout_root,
        )
    return dirty


def _owned_dirty_paths(
    db: HubDatabase,
    *,
    project_id: str,
    checkout_root: str,
    paths: AbstractSet[str],
    exclude_session_id: str | None,
) -> dict[str, tuple[ForeignPathOwner, ...]]:
    try:
        owners = _active_path_owners(
            db,
            project_id=project_id,
            checkout_root=checkout_root,
            exclude_session_id=exclude_session_id,
        )
    except (psycopg.OperationalError, PoolTimeout) as exc:
        raise DirtyEditOwnershipInspectionError("database ownership inspection failed") from exc
    return {
        path: tuple(
            owner
            for ledger in overlapping_task_edit_paths(owners.keys(), [path])
            for owner in owners[ledger]
        )
        for path in paths
        if overlapping_task_edit_paths(owners.keys(), [path])
    }


def active_owned_dirty_paths(
    db: HubDatabase,
    *,
    project_id: str,
    checkout_root: str,
    paths: AbstractSet[str],
) -> dict[str, tuple[ForeignPathOwner, ...]]:
    """Resolve all active task-session attribution for the given dirty paths."""
    return _owned_dirty_paths(
        db,
        project_id=project_id,
        checkout_root=checkout_root,
        paths=paths,
        exclude_session_id=None,
    )


def foreign_owned_dirty_paths(
    db: HubDatabase,
    *,
    session_id: str,
    project_id: str,
    checkout_root: str,
    paths: AbstractSet[str],
) -> dict[str, tuple[ForeignPathOwner, ...]]:
    """Resolve active task-session attribution excluding the caller.

    Raises DirtyEditOwnershipInspectionError for expected infrastructure failures so
    callers choose their own fail-open or fail-closed posture at the boundary.
    """
    return _owned_dirty_paths(
        db,
        project_id=project_id,
        checkout_root=checkout_root,
        paths=paths,
        exclude_session_id=session_id,
    )


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
    candidates = set(owners) if paths is None else overlapping_task_edit_paths(owners.keys(), paths)
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
            owners=tuple(
                owner
                for ledger in overlapping_task_edit_paths(owners.keys(), [path])
                for owner in owners[ledger]
            ),
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
