"""Dispatcher action for merging task workspaces into integration workspaces."""

from __future__ import annotations

import os
import subprocess  # nosec B404 # fixed git commands.
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from gobby.dispatch.actions import MergeWorkspaceAction
from gobby.storage.clones import LocalCloneManager
from gobby.storage.database import DatabaseProtocol
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks._artifacts import TaskArtifactManager
from gobby.storage.tasks._lifecycle_events import TaskLifecycleEventManager
from gobby.storage.tasks._stage_states import StageStatesManager
from gobby.storage.worktrees import LocalWorktreeManager

WorkspaceBackend = Literal["worktree", "clone"]
MERGE_HOLDER = "dispatcher-merge"
MERGE_TTL_SECONDS = 600


@dataclass(frozen=True)
class _WorkspacePaths:
    source_path: str
    target_path: str
    source_branch: str
    source_id: str
    target_id: str


def execute_merge_workspace(
    action: MergeWorkspaceAction,
    *,
    db: DatabaseProtocol,
    services: object | None = None,
) -> str | None:
    """Merge source workspace into the target integration workspace."""
    key = f"{action.backend}:{action.target_branch}"
    if not _acquire_integration_mutex(db, key):
        raise RuntimeError(f"integration workspace is busy: {action.target_branch}")
    try:
        paths = _resolve_paths(action, db=db, services=services)
        source_branch = action.source_branch or paths.source_branch
        _ensure_branch(paths.source_path, source_branch, "source")
        _ensure_branch(paths.target_path, action.target_branch, "target")
        _ensure_clean(paths.target_path, "target integration workspace")
        source_commit = _git_stdout(paths.source_path, ["rev-parse", "HEAD"])
        if _is_ancestor(paths.target_path, source_commit):
            _complete_merge_stage(db, action.task_id, source_commit)
            _mark_source_merged(action, db=db, source_id=paths.source_id)
            return source_commit

        merge_ref = source_commit
        if action.backend == "clone":
            _git_ok(paths.target_path, ["fetch", paths.source_path, source_branch])
            merge_ref = "FETCH_HEAD"
        result = _git(Path(paths.target_path), ["merge", "--no-ff", "--no-edit", merge_ref])
        if result.returncode != 0:
            conflicted = _conflicted_files(paths.target_path)
            _abort_merge(paths.target_path)
            detail = "\n".join(conflicted) if conflicted else result.stderr.strip()
            _fail_merge_stage(db, action.task_id, f"merge_conflict:{detail or 'unknown'}")
            return None

        merge_sha = _git_stdout(paths.target_path, ["rev-parse", "HEAD"])
        if action.backend == "clone":
            _sync_source_repo_branch(db, action.task_id, paths.target_path, action.target_branch)
        _mark_source_merged(action, db=db, source_id=paths.source_id)
        _complete_merge_stage(db, action.task_id, merge_sha)
        return merge_sha
    finally:
        _release_integration_mutex(db, key)


def _resolve_paths(
    action: MergeWorkspaceAction,
    *,
    db: DatabaseProtocol,
    services: object | None,
) -> _WorkspacePaths:
    artifacts = TaskArtifactManager(db).get_artifacts(action.task_id)
    project_id = _project_id_for_task(db, action.task_id)
    if action.backend == "worktree":
        storage = cast(
            LocalWorktreeManager,
            getattr(services, "worktree_storage", None) or LocalWorktreeManager(db),
        )
        source_id = action.source_workspace_id or artifacts.worktree_id
        if source_id is None:
            source_id = artifacts.integration_workspace_id
        if source_id is None:
            raise RuntimeError("source worktree artifact is missing")
        worktree_source = storage.get(source_id)
        worktree_target = storage.get_by_branch(project_id, action.target_branch)
        if worktree_source is None or worktree_target is None:
            raise RuntimeError("source or target worktree metadata is missing")
        _require_integration_target(worktree_target.workspace_role)
        return _WorkspacePaths(
            worktree_source.worktree_path,
            worktree_target.worktree_path,
            worktree_source.branch_name,
            worktree_source.id,
            worktree_target.id,
        )

    clone_storage = cast(
        LocalCloneManager,
        getattr(services, "clone_storage", None) or LocalCloneManager(db),
    )
    source_id = action.source_clone_id or artifacts.clone_id
    if source_id is None:
        source_id = artifacts.integration_clone_id
    if source_id is None:
        raise RuntimeError("source clone artifact is missing")
    clone_source = clone_storage.get(source_id)
    clone_target = clone_storage.get_by_branch(project_id, action.target_branch)
    if clone_source is None or clone_target is None:
        raise RuntimeError("source or target clone metadata is missing")
    _require_integration_target(clone_target.workspace_role)
    return _WorkspacePaths(
        clone_source.clone_path,
        clone_target.clone_path,
        clone_source.branch_name,
        clone_source.id,
        clone_target.id,
    )


def _project_id_for_task(db: DatabaseProtocol, task_id: str) -> str:
    row = db.fetchone("SELECT project_id FROM tasks WHERE id = ?", (task_id,))
    if row is None:
        raise RuntimeError(f"task not found: {task_id}")
    return str(row["project_id"])


def _require_integration_target(role: str) -> None:
    if role != "integration":
        raise RuntimeError("target workspace is not an integration workspace")


def _ensure_branch(path: str, expected: str, label: str) -> None:
    current = _git_stdout(path, ["rev-parse", "--abbrev-ref", "HEAD"])
    if current != expected:
        raise RuntimeError(f"{label} workspace branch mismatch: {current} != {expected}")


def _ensure_clean(path: str, label: str) -> None:
    status = _git_stdout(path, ["status", "--porcelain"])
    if status:
        raise RuntimeError(f"{label} is dirty")


def _is_ancestor(target_path: str, commit_sha: str) -> bool:
    result = _git(Path(target_path), ["merge-base", "--is-ancestor", commit_sha, "HEAD"])
    return result.returncode == 0


def _conflicted_files(path: str) -> list[str]:
    result = _git(Path(path), ["diff", "--name-only", "--diff-filter=U"])
    return [line for line in result.stdout.splitlines() if line.strip()]


def _abort_merge(path: str) -> None:
    _git(Path(path), ["merge", "--abort"])


def _complete_merge_stage(db: DatabaseProtocol, task_id: str, commit_sha: str) -> None:
    StageStatesManager(db, TaskLifecycleEventManager(db)).complete_stage(
        task_id,
        "merge",
        by_session_id="dispatcher",
        commit_sha=commit_sha,
        artifact_updates={"integration_merge_sha": commit_sha},
    )


def _fail_merge_stage(db: DatabaseProtocol, task_id: str, reason: str) -> None:
    StageStatesManager(db, TaskLifecycleEventManager(db)).fail_stage(
        task_id,
        "merge",
        reason=reason,
        needs_human=True,
        by_session_id="dispatcher",
    )


def _mark_source_merged(
    action: MergeWorkspaceAction,
    *,
    db: DatabaseProtocol,
    source_id: str,
) -> None:
    if action.backend == "worktree":
        LocalWorktreeManager(db).mark_merged(source_id)
        return
    cleanup_after = (datetime.now(UTC) + timedelta(days=7)).isoformat()
    LocalCloneManager(db).mark_merged(source_id, cleanup_after=cleanup_after)


def _sync_source_repo_branch(
    db: DatabaseProtocol,
    task_id: str,
    target_path: str,
    target_branch: str,
) -> None:
    repo_path = _repo_path_for_task(db, task_id)
    _git_ok(repo_path, ["fetch", target_path, f"{target_branch}:{target_branch}"])


def _repo_path_for_task(db: DatabaseProtocol, task_id: str) -> Path:
    project_id = _project_id_for_task(db, task_id)
    project = LocalProjectManager(db).get(project_id)
    if project is None or not project.repo_path:
        raise RuntimeError("project repo_path is required for clone integration sync")
    return Path(project.repo_path)


def _acquire_integration_mutex(db: DatabaseProtocol, key: str) -> bool:
    now = datetime.now(UTC)
    until = now + timedelta(seconds=MERGE_TTL_SECONDS)
    with db.transaction_immediate() as conn:
        row = conn.execute(
            "SELECT lease_until, lease_holder FROM integration_workspace_mutex WHERE integration_key = ?",
            (key,),
        ).fetchone()
        if row is not None and row["lease_until"] and row["lease_until"] >= now.isoformat():
            return False
        conn.execute(
            """
            INSERT INTO integration_workspace_mutex (
                integration_key, lease_until, lease_holder, updated_at
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(integration_key) DO UPDATE SET
                lease_until = excluded.lease_until,
                lease_holder = excluded.lease_holder,
                updated_at = excluded.updated_at
            """,
            (key, until.isoformat(), MERGE_HOLDER, now.isoformat()),
        )
        return True


def _release_integration_mutex(db: DatabaseProtocol, key: str) -> None:
    with db.transaction() as conn:
        conn.execute(
            "DELETE FROM integration_workspace_mutex WHERE integration_key = ? AND lease_holder = ?",
            (key, MERGE_HOLDER),
        )


def _git_stdout(path: str | Path, args: list[str]) -> str:
    result = _git_ok(path, args)
    return result.stdout.strip()


def _git_ok(path: str | Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    result = _git(Path(path), args)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return result


def _git(path: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 # git args are fixed by callers.
        ["git", *args],
        cwd=path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={**os.environ, "GOBBY_MERGE": "1"},
    )
