"""Cross-session Git commit ownership guard."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg_pool import PoolTimeout

from gobby.hooks.normalization import is_shell_tool, tool_input_error
from gobby.storage.sessions._constants import LIVE_SESSION_STATUS_ORDER
from gobby.utils.daemon_git import GitFailed, GitOk, GitResult, daemon_git, parse_porcelain_v1_z
from gobby.workflows.commit_guard_conflicts import (
    ForeignPathOwner as ForeignPathOwner,
)
from gobby.workflows.commit_guard_conflicts import (
    format_conflict_reason,
    format_dirty_edit_reason,
    format_session_ref,
    format_unverified_dirty_edit_reason,
    scope_foreign_owners,
)
from gobby.workflows.git_commit_parsing import (
    GitCommitInvocation,
    parse_git_commit_invocations,
    resolve_commit_inspect_cwd,
)
from gobby.workflows.ledger_reconcile import (
    SESSION_DIRTY_FILE_CHECKOUTS_VARIABLE,
    SESSION_DIRTY_FILES_VARIABLE,
    session_dirty_file_set_for_checkout,
)
from gobby.workflows.observer_utils import _extract_shell_command
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.task_claim_state import (
    effective_tool_cwd,
    normalize_task_edited_path,
    overlapping_task_edit_paths,
    resolve_edit_pair,
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
    return effective_tool_cwd(event.cwd, event.data) or project_path


def _missing_commit_directory(invocation: GitCommitInvocation, command_cwd: str) -> str | None:
    """Return the directory this commit navigates through that does not exist, if any."""
    if invocation.chdir is None:
        return None
    if not os.path.isabs(invocation.chdir) and not os.path.isdir(command_cwd):
        return command_cwd
    target = os.path.normpath(os.path.join(command_cwd, invocation.chdir))
    return None if os.path.isdir(target) else target


async def _resolve_commit_checkout_root(
    invocation: GitCommitInvocation,
    *,
    command_cwd: str,
) -> str | None:
    """Return the invocation's Git top-level, or ``None`` for a non-repository cwd."""
    result = await daemon_git.run(
        [*invocation.root_options, "-c", "core.fsmonitor=false", "rev-parse", "--show-toplevel"],
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
            missing_directory = _missing_commit_directory(invocation, command_cwd)
            if missing_directory is not None:
                return (
                    f"Commit blocked: its working directory {missing_directory} does not "
                    "exist, so Gobby cannot resolve the repository it targets. Run the "
                    "commit from an existing directory, creating a new repository in an "
                    "earlier command."
                )
            # A vanished caller cwd leaves Git nothing to start from. An absolute
            # target ignores it, and a cwd-relative commit can only reach the
            # session's checkout, so inspect from there.
            git_cwd = command_cwd if os.path.isdir(command_cwd) else project_path
            checkout_root = await _resolve_commit_checkout_root(
                invocation,
                command_cwd=git_cwd,
            )
            if checkout_root is None:
                continue

            if checkout_root not in owners_by_checkout:
                try:
                    owners_by_checkout[checkout_root] = await asyncio.to_thread(
                        _live_path_owners,
                        db,
                        exclude_session_id=session_id,
                        checkout_root=checkout_root,
                    )
                except psycopg.errors.QueryCanceled as exc:
                    raise DirtyEditOwnershipInspectionError("workflow evaluation timeout") from exc
                except (psycopg.OperationalError, PoolTimeout) as exc:
                    raise DirtyEditOwnershipInspectionError(
                        "database ownership inspection failed"
                    ) from exc
            owners = owners_by_checkout[checkout_root]
            if not owners:
                continue

            inspect_cwd = resolve_commit_inspect_cwd(
                invocation,
                event_cwd=git_cwd,
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
                _current_owned_paths, db, session_id, checkout_root
            )
            owned_candidates = {
                path
                for path in owned_candidates
                if not any(task_edit_path_covers(own, path) for own in current_owned)
            }
            owned_candidates -= await _operation_paths(checkout_root)
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
    db: HubDatabase, event: HookEvent, *, session_id: str, project_id: str, project_path: str
) -> str:
    """Refuse first-touch writes to dirt, regardless of task claim ownership."""
    data = event.data if isinstance(event.data, dict) else {}
    if data.get("canonical_tool_kind") != "write":
        return ""
    try:
        variables = await asyncio.to_thread(SessionVariableManager(db).get_variables, session_id)
        roots: dict[str, set[str]] = {}
        for path in _write_paths(data):
            pair = await asyncio.to_thread(
                resolve_edit_pair,
                path,
                effective_tool_cwd(event.cwd, data),
                project_id=project_id,
                db=db,
                variables=variables,
            )
            if pair is not None:
                root, rel = pair
                roots.setdefault(root, set()).add(rel)
        conflicts: set[ForeignPathOwner] = set()
        unowned: set[str] = set()
        for root, rels in roots.items():
            own = session_dirty_file_set_for_checkout(variables, root)
            candidates = {
                rel for rel in rels if not any(task_edit_path_covers(p, rel) for p in own)
            }
            if not candidates:
                continue
            operation_paths = await _operation_paths(root)
            candidates -= operation_paths
            if not candidates:
                continue
            dirty = await task_dirty_paths_async(candidates, root)
            if dirty is None:
                return format_unverified_dirty_edit_reason(candidates)
            owners = await asyncio.to_thread(
                _live_path_owners,
                db,
                checkout_root=root,
                paths=candidates,
                exclude_session_id=session_id,
            )
            scoped = scope_foreign_owners(owners, candidates, own)
            clean = {
                path
                for path in candidates | set(scoped)
                if not any(task_edit_path_covers(path, child) for child in dirty)
            }
            await asyncio.to_thread(_release_clean_ledger_entries, db, scoped, clean, root)
            for path in dirty:
                if path in operation_paths or any(task_edit_path_covers(p, path) for p in own):
                    continue
                holders = tuple(
                    owner
                    for ledger in overlapping_task_edit_paths(scoped, [path])
                    for owner in scoped[ledger]
                )
                if holders:
                    conflicts.update(replace(owner, path=path) for owner in holders)
                else:
                    unowned.add(path)
        return format_dirty_edit_reason(conflicts, unowned) if conflicts or unowned else ""
    except (psycopg.OperationalError, PoolTimeout) as exc:
        return (
            f"Edit blocked: Gobby could not verify dirty-path ownership "
            f"(database ownership inspection failed: {exc}). "
            "Fix that cause, or retry once the daemon and repository are available."
        )
    except DirtyEditOwnershipInspectionError as exc:
        logger.warning("Cross-session dirty edit ownership inspection failed", exc_info=True)
        return (
            f"Edit blocked: Gobby could not verify dirty-path ownership ({exc}). "
            "Fix that cause, or retry once the daemon and repository are available."
        )


def _write_paths(event_data: dict[str, Any]) -> list[str]:
    if "canonical_write_file_paths" in event_data:
        raw = event_data["canonical_write_file_paths"]
    else:
        raw = event_data.get("canonical_file_paths") or event_data.get("canonical_file_path")
    if isinstance(raw, str):
        return [raw]
    return [path for path in raw if isinstance(path, str)] if isinstance(raw, list) else []


async def _operation_paths(checkout_root: str) -> set[str]:
    """Paths an in-progress merge, cherry-pick, revert or rebase applied or left unmerged."""

    async def git(*args: str) -> GitResult:
        return await daemon_git.run(
            ["-c", "core.fsmonitor=false", *args], cwd=checkout_root, timeout=10.0
        )

    async def git_ok(*args: str) -> GitOk | None:
        result = await git(*args)
        return result if isinstance(result, GitOk) else None

    paths: set[str] = set()
    in_progress = False
    if await git_ok("rev-parse", "-q", "--verify", "MERGE_HEAD"):
        in_progress = True
        merge_base = await git("merge-base", "HEAD", "MERGE_HEAD")
        if isinstance(merge_base, GitOk):
            merge_args = ("diff", "--name-only", "--no-renames", "-z", "HEAD...MERGE_HEAD")
        elif isinstance(merge_base, GitFailed) and merge_base.returncode == 1:
            # Unrelated histories share no base, so the merge brings all of MERGE_HEAD.
            merge_args = ("ls-tree", "-r", "--name-only", "-z", "MERGE_HEAD")
        else:
            detail = merge_base.stderr.strip() or merge_base.status
            raise DirtyEditOwnershipInspectionError(f"git merge-base failed: {detail}")
        paths |= await _git_paths_async(checkout_root, *merge_args)
    picked = ["CHERRY_PICK_HEAD", "REVERT_HEAD"]
    # REBASE_HEAD outlives a finished rebase, so only its state directory proves one.
    state_dirs = await git_ok(
        "rev-parse", "--git-path", "rebase-merge", "--git-path", "rebase-apply"
    )
    if state_dirs and any(
        os.path.exists(os.path.join(checkout_root, state_dir))
        for state_dir in state_dirs.stdout.splitlines()
    ):
        picked.append("REBASE_HEAD")
    for ref in picked:
        if await git_ok("rev-parse", "-q", "--verify", ref):
            in_progress = True
            paths |= await _git_paths_async(
                checkout_root,
                "diff-tree",
                "--root",
                "--no-commit-id",
                "--name-only",
                "-r",
                "--no-renames",
                "-z",
                "--diff-merges=first-parent",
                ref,
            )
    if in_progress:
        # A rename-following conflict lands on a path the picked commit never named.
        paths |= await _git_paths_async(
            checkout_root, "diff", "--name-only", "-z", "--diff-filter=U"
        )
    return paths


def _live_path_owners(
    db: HubDatabase,
    *,
    checkout_root: str,
    paths: AbstractSet[str] | None = None,
    exclude_session_id: str | None = None,
) -> dict[str, tuple[ForeignPathOwner, ...]]:
    rows = db.fetchall(
        """
        SELECT sessions.id AS session_id, sessions.seq_num AS session_seq_num,
               sessions.project_id AS session_project_id, projects.name AS project_name
        FROM sessions
        LEFT JOIN projects ON projects.id = sessions.project_id
        WHERE sessions.id IS DISTINCT FROM %s AND sessions.status = ANY(%s)
        ORDER BY sessions.seq_num
        """,
        (exclude_session_id, list(LIVE_SESSION_STATUS_ORDER)),
    )
    variable_manager = SessionVariableManager(db)
    owners: dict[str, list[ForeignPathOwner]] = {}
    for row in rows:
        owner_session_id = str(row["session_id"])
        variables = variable_manager.get_variable_subset(
            owner_session_id,
            (
                SESSION_DIRTY_FILES_VARIABLE,
                SESSION_DIRTY_FILE_CHECKOUTS_VARIABLE,
                "claimed_tasks",
                "task_edited_file_checkouts",
            ),
        )
        held = session_dirty_file_set_for_checkout(variables, checkout_root)
        if paths is not None:
            held = overlapping_task_edit_paths(held, paths)
        claims = variables.get("claimed_tasks")
        claimed_tasks = claims if isinstance(claims, dict) else {}
        session_ref = format_session_ref(
            row["session_seq_num"],
            owner_session_id,
            project_name=row["project_name"],
            project_id=row["session_project_id"],
        )
        for path in held:
            task_id = next(
                (
                    task
                    for task in claimed_tasks
                    if path in task_edited_file_set_for_checkout(variables, task, checkout_root)
                ),
                None,
            )
            task_ref = str(claimed_tasks[task_id]) if task_id is not None else ""
            owners.setdefault(path, []).append(
                ForeignPathOwner(
                    path=path,
                    session_ref=session_ref,
                    task_ref=task_ref,
                    owner_session_id=owner_session_id,
                    owner_task_id=task_id,
                )
            )
    return {path: tuple(holders) for path, holders in owners.items()}


def _current_owned_paths(db: HubDatabase, session_id: str, checkout_root: str) -> set[str]:
    try:
        variables = SessionVariableManager(db).get_variables(session_id)
    except (psycopg.OperationalError, PoolTimeout) as exc:
        raise DirtyEditOwnershipInspectionError(
            f"database ownership inspection failed: {exc}"
        ) from exc
    return session_dirty_file_set_for_checkout(variables, checkout_root)


def _release_clean_ledger_entries(
    db: HubDatabase,
    owners: Mapping[str, tuple[ForeignPathOwner, ...]],
    clean_paths: AbstractSet[str],
    checkout_root: str,
) -> None:
    variable_manager = SessionVariableManager(db)
    paths_by_owner: dict[str, list[str]] = {}
    for path in sorted(clean_paths):
        for owner in owners.get(path, ()):
            paths_by_owner.setdefault(owner.owner_session_id, []).append(path)
    for owner_session_id, paths in paths_by_owner.items():
        try:
            variable_manager.release_session_dirty_files(
                owner_session_id,
                paths,
                checkout_root=checkout_root,
            )
        except (psycopg.OperationalError, PoolTimeout):
            logger.warning(
                "Could not release stale clean ledger entries for session %s",
                owner_session_id,
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
        owners = _live_path_owners(
            db,
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
            _live_path_owners,
            db,
            exclude_session_id=session_id,
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
            _live_path_owners,
            db,
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
        ["--literal-pathspecs", "-c", "core.fsmonitor=false", *args],
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
