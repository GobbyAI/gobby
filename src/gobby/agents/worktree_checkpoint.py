"""Atomic Git checkpoints for terminal agent worktree recovery."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError
from gobby.tasks.state_semantics import get_claimed_session_id, is_task_closed
from gobby.utils.daemon_git import GitOk, GitResult, daemon_git, parse_porcelain_v1_z
from gobby.workflows.commit_guard import (
    DirtyEditOwnershipInspectionError,
    inspect_checkout_path_ownership_async,
)
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.task_claim_state import (
    normalize_task_edited_path,
    task_edited_file_set_for_checkout,
)
from gobby.worktrees.git import WorktreeInfo

if TYPE_CHECKING:
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.tasks import LocalTaskManager
    from gobby.storage.worktrees import LocalWorktreeManager

_ACTIVE_RUN_STATUSES = {"pending", "running"}
logger = logging.getLogger(__name__)

CHECKPOINT_AUTHOR_NAME = "Gobby Coordinator"
CHECKPOINT_AUTHOR_EMAIL = "gobby-coordinator@localhost"
_GIT_TIMEOUT_SECONDS = 30


class WorktreeCheckpointError(RuntimeError):
    """A checkpoint could not be created without changing the checkout."""

    def __init__(self, message: str, *, error_code: str = "checkpoint_failed") -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class WorktreeCheckpoint:
    """Committed recovery boundary for one terminal agent run."""

    commit_sha: str
    included_paths: tuple[str, ...]
    message: str


def checkpoint_commit_message(*, task_seq_num: int, run_id: str) -> str:
    """Return the stable task-tagged message for a recovery checkpoint."""
    return (
        f"[gobby-#{task_seq_num}] chore: checkpoint terminal agent worktree\n\nAgent-Run: {run_id}"
    )


async def checkpoint_worktree(
    *,
    worktree_path: str | Path,
    expected_paths: Collection[str],
    task_seq_num: int,
    run_id: str,
) -> WorktreeCheckpoint:
    """Commit the complete authorized dirty set without exposing partial staging state."""
    path = Path(worktree_path).resolve()
    normalized_expected = {
        normalized
        for item in expected_paths
        if (normalized := normalize_task_edited_path(item)) is not None
    }
    observed_paths = await _status_paths(path)
    if not observed_paths:
        raise WorktreeCheckpointError(
            "The terminal agent worktree is already clean",
            error_code="worktree_clean",
        )
    if observed_paths != normalized_expected:
        raise WorktreeCheckpointError(
            "The worktree dirty set changed after ownership validation",
            error_code="dirty_set_changed",
        )

    branch_ref = await _git_output(path, ["symbolic-ref", "--quiet", "HEAD"])
    old_head = await _git_output(path, ["rev-parse", "HEAD"])
    index_path = Path(
        await _git_output(path, ["rev-parse", "--path-format=absolute", "--git-path", "index"])
    )
    message = checkpoint_commit_message(task_seq_num=task_seq_num, run_id=run_id)

    with tempfile.TemporaryDirectory(prefix="gobby-checkpoint-", dir=index_path.parent) as temp_dir:
        temp_root = Path(temp_dir)
        alternate_index = temp_root / "index"
        index_backup = temp_root / "original-index"
        original_index_exists = index_path.exists()
        if original_index_exists:
            shutil.copy2(index_path, index_backup)

        alternate_env = {**os.environ, "GIT_INDEX_FILE": str(alternate_index)}
        await _git_checked(path, ["read-tree", old_head], env=alternate_env)
        await _git_checked(path, ["add", "-A", "--", "."], env=alternate_env)
        staged_paths = await _status_paths(path, env=alternate_env)
        if staged_paths != normalized_expected:
            raise WorktreeCheckpointError(
                "The staged checkpoint does not match the authorized dirty set",
                error_code="dirty_set_changed",
            )

        tree_sha = await _git_output(path, ["write-tree"], env=alternate_env)
        commit_env = {
            **alternate_env,
            "GIT_AUTHOR_NAME": CHECKPOINT_AUTHOR_NAME,
            "GIT_AUTHOR_EMAIL": CHECKPOINT_AUTHOR_EMAIL,
            "GIT_COMMITTER_NAME": CHECKPOINT_AUTHOR_NAME,
            "GIT_COMMITTER_EMAIL": CHECKPOINT_AUTHOR_EMAIL,
        }
        commit_sha = await _git_output(
            path,
            ["commit-tree", tree_sha, "-p", old_head],
            env=commit_env,
            input_text=f"{message}\n",
        )

        ref_updated = False
        try:
            await _git_checked(path, ["update-ref", branch_ref, commit_sha, old_head])
            ref_updated = True
            await _git_checked(path, ["reset", "--mixed", "--quiet", commit_sha])
            remaining_paths = await _status_paths(path)
            if remaining_paths:
                raise WorktreeCheckpointError(
                    "The checkpoint commit did not leave the worktree clean",
                    error_code="checkpoint_not_clean",
                )
        except Exception as exc:
            rollback_error = await _rollback_checkpoint(
                path=path,
                branch_ref=branch_ref,
                old_head=old_head,
                commit_sha=commit_sha,
                ref_updated=ref_updated,
                index_path=index_path,
                index_backup=index_backup,
                original_index_exists=original_index_exists,
            )
            if rollback_error is not None:
                raise WorktreeCheckpointError(
                    f"Checkpoint failed and rollback failed: {rollback_error}",
                    error_code="checkpoint_rollback_failed",
                ) from exc
            if isinstance(exc, WorktreeCheckpointError):
                raise
            raise WorktreeCheckpointError(str(exc)) from exc

    return WorktreeCheckpoint(
        commit_sha=commit_sha,
        included_paths=tuple(sorted(normalized_expected)),
        message=message,
    )


async def _rollback_checkpoint(
    *,
    path: Path,
    branch_ref: str,
    old_head: str,
    commit_sha: str,
    ref_updated: bool,
    index_path: Path,
    index_backup: Path,
    original_index_exists: bool,
) -> str | None:
    errors: list[str] = []
    if ref_updated:
        result = await _run_git(path, ["update-ref", branch_ref, old_head, commit_sha])
        if not isinstance(result, GitOk):
            errors.append(_git_failure(result))
    try:
        if original_index_exists:
            os.replace(index_backup, index_path)
        else:
            index_path.unlink(missing_ok=True)
    except OSError as exc:
        errors.append(f"could not restore Git index: {exc}")
    return "; ".join(errors) or None


async def _status_paths(path: Path, *, env: Mapping[str, str] | None = None) -> set[str]:
    result = await daemon_git.status(path, timeout=_GIT_TIMEOUT_SECONDS, env=env)
    if not isinstance(result, GitOk):
        raise WorktreeCheckpointError(_git_failure(result))

    paths: set[str] = set()
    for entry in parse_porcelain_v1_z(result.stdout):
        normalized = normalize_task_edited_path(entry.path)
        if normalized is not None:
            paths.add(normalized)
        if entry.original_path is not None:
            original = normalize_task_edited_path(entry.original_path)
            if original is not None:
                paths.add(original)
    return paths


async def _git_output(
    path: Path,
    arguments: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
) -> str:
    result = await _run_git(path, arguments, env=env, input_text=input_text)
    if not isinstance(result, GitOk):
        raise WorktreeCheckpointError(_git_failure(result))
    output = result.stdout.strip()
    if not output:
        raise WorktreeCheckpointError(f"git {arguments[0]} returned no output")
    return output


async def _git_checked(
    path: Path,
    arguments: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
) -> None:
    result = await _run_git(path, arguments, env=env)
    if not isinstance(result, GitOk):
        raise WorktreeCheckpointError(_git_failure(result))


async def _run_git(
    path: Path,
    arguments: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
) -> GitResult:
    return await daemon_git.run(
        arguments,
        cwd=path,
        env=env,
        input_text=input_text,
        timeout=_GIT_TIMEOUT_SECONDS,
    )


def _git_failure(result: GitResult) -> str:
    if result.status == "timeout":
        detail = f"timed out after {result.timeout:g}s"
    else:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
    return f"git {result.argv[1]} failed: {detail}"


async def checkpoint_agent_worktree(
    *,
    agent_run_manager: LocalAgentRunManager,
    task_manager: LocalTaskManager | None,
    worktree_storage: LocalWorktreeManager | None,
    db: HubDatabase | None = None,
    allowed_paths: Collection[str] | None = None,
    run_id: str,
    caller_session_id: str,
) -> dict[str, Any]:
    """Checkpoint a terminal child's authorized worktree without releasing its task.

    Callers may further restrict the dirty set with ``allowed_paths``; this never
    replaces the original-parent, isolation, active-writer, or attribution checks.
    """
    if task_manager is None or worktree_storage is None:
        return _error("Task and worktree services are required", "checkpoint_unavailable")

    run = agent_run_manager.get(run_id)
    if run is None:
        return _error(f"Agent run {run_id} not found", "run_not_found")
    if run.status in _ACTIVE_RUN_STATUSES:
        return _error(
            f"Agent run {run_id} is still {run.status}; terminate it before checkpointing",
            "run_active",
        )
    if run.parent_session_id != caller_session_id:
        return _error(
            "Only the terminal run's parent coordinator may checkpoint its worktree",
            "coordinator_required",
        )
    if not run.task_id or not run.child_session_id:
        return _error("Agent run has no task or child session boundary", "invalid_agent_boundary")
    if not run.worktree_id:
        return _error(
            "Agent run did not use an isolated worktree",
            "isolated_worktree_required",
        )

    task = task_manager.get_task(run.task_id)
    if task is None:
        return _error(f"Task {run.task_id} not found", "task_not_found")
    if task.seq_num is None:
        return _error("Task has no project sequence number", "task_not_found")
    if is_task_closed(task):
        return _error("Closed tasks cannot receive recovery checkpoints", "task_closed")
    task_owner = get_claimed_session_id(task)
    allowed_task_owners = {None, caller_session_id, run.child_session_id}
    if task_owner not in allowed_task_owners:
        return _error(
            "The task is owned by a session outside this coordinator recovery boundary",
            "foreign_task_owner",
        )

    active_task_run = agent_run_manager.get_active_run_for_task(run.task_id)
    if active_task_run is not None:
        return _error(
            f"Task already has active agent run {active_task_run.id}",
            "run_active",
        )
    active_worktree_run = agent_run_manager.get_active_run_for_worktree(run.worktree_id)
    if active_worktree_run is not None:
        return _error(
            f"Worktree already has active agent run {active_worktree_run.id}",
            "run_active",
        )

    worktree = worktree_storage.get(run.worktree_id)
    if worktree is None:
        return _error(f"Worktree {run.worktree_id} not found", "worktree_not_found")
    if (
        worktree.task_id != run.task_id
        or worktree.project_id != task.project_id
        or worktree.workspace_role != "task"
        or worktree.status != "active"
    ):
        return _error(
            "Agent run is not bound to an active task-isolation worktree",
            "isolated_worktree_required",
        )
    if run.machine_id != worktree.machine_id:
        return _error("Agent run and worktree belong to different machines", "foreign_worktree")

    try:
        inspected = await _inspect_linked_worktree(worktree.worktree_path)
    except (OSError, RuntimeError, ValueError) as exc:
        return _error(f"Worktree inspection failed: {exc}", "isolated_worktree_required")
    if (
        Path(inspected.path).resolve() != Path(worktree.worktree_path).resolve()
        or inspected.branch is None
        or inspected.branch != worktree.branch_name
        or inspected.is_bare
        or inspected.is_detached
        or inspected.prunable
    ):
        return _error(
            "Registered worktree does not match its linked branch checkout",
            "isolated_worktree_required",
        )

    allowed_worktree_owners = tuple(
        owner for owner in (caller_session_id, run.child_session_id) if owner is not None
    )
    try:
        claimed = worktree_storage.claim_if_available(
            worktree.id,
            caller_session_id,
            allowed_existing_session_ids=allowed_worktree_owners,
        )
    except MachineOwnershipMismatchError as exc:
        return exc.to_dict()
    if claimed is None:
        return _error(
            "Worktree is owned by a different session",
            "foreign_worktree_owner",
        )

    checkpoint = None
    failure: dict[str, Any] | None = None
    release_error: str | None = None
    try:
        db = db or task_manager.db
        checkout_root = str(Path(worktree.worktree_path).resolve())
        ownership = await inspect_checkout_path_ownership_async(
            db,
            project_id=worktree.project_id,
            checkout_root=checkout_root,
        )
        dirty_paths = {item.path for item in ownership}
        task_ref = f"#{task.seq_num}"
        foreign_paths = sorted(
            item.path
            for item in ownership
            if any(owner.task_ref != task_ref for owner in item.owners)
        )
        outside_allowed_paths = sorted(dirty_paths - set(allowed_paths or ()))
        if allowed_paths is not None and outside_allowed_paths:
            failure = _error(
                "Worktree contains paths outside the requested checkpoint boundary",
                "unexpected_checkpoint_paths",
                paths=outside_allowed_paths,
            )
        elif foreign_paths:
            failure = _error(
                f"Worktree contains foreign-attributed paths: {', '.join(foreign_paths)}",
                "foreign_attributed_paths",
                paths=foreign_paths,
            )
        else:
            authorized_paths = _authorized_task_paths(
                db,
                task_id=task.id,
                checkout_root=checkout_root,
                session_ids={caller_session_id, run.child_session_id},
                legacy_child_session_id=run.child_session_id if task_owner is None else None,
                legacy_dirty_paths=dirty_paths,
                legacy_started_at=getattr(run, "started_at", None),
                legacy_completed_at=getattr(run, "completed_at", None),
            )
            unattributed_paths = sorted(dirty_paths - authorized_paths)
            if unattributed_paths:
                failure = _error(
                    f"Worktree contains unattributed paths: {', '.join(unattributed_paths)}",
                    "unattributed_paths",
                    paths=unattributed_paths,
                )
            else:
                checkpoint = await checkpoint_worktree(
                    worktree_path=checkout_root,
                    expected_paths=dirty_paths,
                    task_seq_num=task.seq_num,
                    run_id=run.id,
                )
    except DirtyEditOwnershipInspectionError as exc:
        failure = _error(f"Path ownership inspection failed: {exc}", "ownership_inspection_failed")
    except WorktreeCheckpointError as exc:
        failure = _error(str(exc), exc.error_code)
    except (OSError, RuntimeError, ValueError) as exc:
        failure = _error(f"Checkpoint failed: {exc}", "checkpoint_failed")
    finally:
        try:
            released = worktree_storage.release(worktree.id)
        except Exception as exc:
            logger.exception("Failed to release checkpoint claim for worktree %s", worktree.id)
            released = None
            release_error = str(exc)

    if released is None:
        response = _error(
            "Checkpoint finished but the coordinator worktree claim could not be released"
            + (f": {release_error}" if release_error else ""),
            "worktree_release_failed",
        )
        if checkpoint is not None:
            response["commit_sha"] = checkpoint.commit_sha
            response["included_paths"] = list(checkpoint.included_paths)
        return response
    if failure is not None:
        return failure
    assert checkpoint is not None
    return {
        "success": True,
        "run_id": run.id,
        "task_id": task.id,
        "task_ref": task_ref,
        "worktree_id": worktree.id,
        "worktree_path": str(Path(worktree.worktree_path).resolve()),
        "branch_name": worktree.branch_name,
        "commit_sha": checkpoint.commit_sha,
        "included_paths": list(checkpoint.included_paths),
        "commit_message": checkpoint.message,
        "worktree_released": True,
    }


def _authorized_task_paths(
    db: Any,
    *,
    task_id: str,
    checkout_root: str,
    session_ids: set[str],
    legacy_child_session_id: str | None,
    legacy_dirty_paths: set[str],
    legacy_started_at: datetime | None,
    legacy_completed_at: datetime | None,
) -> set[str]:
    """Return task paths, including the narrow pre-#21897 terminal recovery case.

    Legacy terminal cleanup erased all task ledgers after releasing the child's
    claim, but retained that child's session edit ledger. Shell edits missing from
    that ledger are bounded by the run's persisted start and completion timestamps
    using both file modification and inode change times. The caller supplies the
    child only for an unclaimed recovered task; current or still-owned task states
    continue to require checkout-scoped task attribution.
    """
    variable_manager = SessionVariableManager(db)
    variables_by_session: dict[str, dict[str, Any]] = {}
    authorized: set[str] = set()
    for session_id in session_ids:
        variables = variable_manager.get_variables(session_id)
        variables_by_session[session_id] = variables
        authorized.update(task_edited_file_set_for_checkout(variables, task_id, checkout_root))

    if legacy_child_session_id is not None:
        legacy_variables = variables_by_session.get(legacy_child_session_id)
        if legacy_variables is None:
            legacy_variables = variable_manager.get_variables(legacy_child_session_id)
        task_ledgers = (
            legacy_variables.get("task_edited_files"),
            legacy_variables.get("task_edited_file_checkouts"),
            legacy_variables.get("task_edited_file_times"),
        )
        attribution_was_cleared = all(
            not isinstance(ledger, dict) or task_id not in ledger for ledger in task_ledgers
        )
        raw_session_paths = legacy_variables.get("session_edited_files")
        if attribution_was_cleared and isinstance(raw_session_paths, list):
            authorized.update(
                path
                for value in raw_session_paths
                if (path := normalize_task_edited_path(value)) is not None
            )
        if (
            attribution_was_cleared
            and legacy_started_at is not None
            and legacy_completed_at is not None
        ):
            started_timestamp = legacy_started_at.timestamp()
            completed_timestamp = legacy_completed_at.timestamp()
            for value in legacy_dirty_paths:
                path = normalize_task_edited_path(value)
                if path is None:
                    continue
                try:
                    path_stat = (Path(checkout_root) / path).lstat()
                except OSError:
                    continue
                changed_timestamp = max(path_stat.st_mtime, path_stat.st_ctime)
                if started_timestamp <= changed_timestamp <= completed_timestamp:
                    authorized.add(path)
    return authorized


async def _inspect_linked_worktree(worktree_path: str) -> WorktreeInfo:
    """Verify a registered path is a live linked checkout without blocking a worker."""
    canonical_path = Path(worktree_path).expanduser().resolve(strict=True)
    if not canonical_path.is_dir():
        raise ValueError(f"Worktree path is not a directory: {canonical_path}")

    async def output(*args: str) -> str:
        result = await daemon_git.run(args, cwd=canonical_path, timeout=10)
        if not isinstance(result, GitOk):
            detail = result.stderr.strip() or result.stdout.strip() or result.status
            raise ValueError(f"git {' '.join(args)} failed: {detail}")
        return result.stdout.strip()

    top_level, branch, git_dir, common_dir, is_bare = await asyncio.gather(
        output("rev-parse", "--show-toplevel"),
        output("branch", "--show-current"),
        output("rev-parse", "--path-format=absolute", "--git-dir"),
        output("rev-parse", "--path-format=absolute", "--git-common-dir"),
        output("rev-parse", "--is-bare-repository"),
    )
    if Path(top_level).resolve() != canonical_path:
        raise ValueError(f"Path is not a Git checkout root: {canonical_path}")
    if Path(git_dir).resolve() == Path(common_dir).resolve():
        raise ValueError(f"Primary checkout cannot be adopted: {canonical_path}")
    return WorktreeInfo(
        path=str(canonical_path),
        branch=branch or None,
        commit="",
        is_bare=is_bare == "true",
        is_detached=not branch,
        locked=False,
        prunable=False,
    )


def _error(message: str, error_code: str, **details: Any) -> dict[str, Any]:
    return {"success": False, "error": message, "error_code": error_code, **details}
