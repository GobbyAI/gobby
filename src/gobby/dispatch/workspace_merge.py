"""Dispatcher action for merging task workspaces into integration workspaces."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess  # nosec B404 # fixed git commands.
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from gobby.build.controls import cleanup_successful_merge_artifacts
from gobby.build.workspaces import ensure_task_parent_integration_workspace
from gobby.dispatch.actions import MergeWorkspaceAction
from gobby.storage.clones import LocalCloneManager
from gobby.storage.database import DatabaseProtocol
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._artifacts import TaskArtifactManager
from gobby.storage.tasks._lifecycle_events import TaskLifecycleEventManager
from gobby.storage.tasks._stage_states import StageStatesManager
from gobby.storage.worktrees import LocalWorktreeManager

WorkspaceBackend = Literal["worktree", "clone"]
MERGE_HOLDER = "dispatcher-merge"
MERGE_TTL_SECONDS = 600
WORKTREE_LOCAL_PROJECT_KEYS = frozenset({"parent_project_id", "parent_project_path"})
WORKTREE_LOCAL_METADATA_CONFLICTS = frozenset({".gobby/project.json"})
DOCS_GUIDES_README = "docs/guides/README.md"
GUIDE_ROW_RE = re.compile(
    r"^\| (?P<link>\[[^\]]+\]\((?P<target>[^)]+\.md)\)) \| (?P<description>.*?) \|$"
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _WorkspacePaths:
    source_path: str
    target_path: str
    source_branch: str
    source_id: str
    target_id: str | None
    target_is_local: bool = False


@dataclass(frozen=True)
class _GuideRow:
    index: int
    link: str
    description: str
    line: str


async def execute_merge_workspace(
    action: MergeWorkspaceAction,
    *,
    db: DatabaseProtocol,
    services: object | None = None,
) -> str | None:
    """Merge source workspace into the target integration workspace."""
    return await asyncio.to_thread(
        _execute_merge_workspace_sync,
        action,
        db=db,
        services=services,
    )


def _execute_merge_workspace_sync(
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
        try:
            _ensure_branch(paths.source_path, source_branch, "source")
            _ensure_branch(paths.target_path, action.target_branch, "target")
            source_commit = _git_stdout(paths.source_path, ["rev-parse", "HEAD"])
            _ensure_target_merge_safe(
                paths.target_path, source_commit, "target integration workspace"
            )
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
                remaining = _resolve_worktree_local_conflicts(paths.target_path, conflicted)
                if conflicted and not remaining:
                    commit_result = _git(Path(paths.target_path), ["commit", "--no-edit"])
                    if commit_result.returncode == 0:
                        merge_sha = _git_stdout(paths.target_path, ["rev-parse", "HEAD"])
                        if action.backend == "clone" and not paths.target_is_local:
                            _sync_source_repo_branch(
                                db,
                                action.task_id,
                                paths.target_path,
                                action.target_branch,
                            )
                        _mark_source_merged(action, db=db, source_id=paths.source_id)
                        _complete_merge_stage(db, action.task_id, merge_sha)
                        return merge_sha
                _abort_merge(paths.target_path)
                detail_files = remaining or conflicted
                detail = "\n".join(detail_files) if detail_files else result.stderr.strip()
                _fail_merge_stage(db, action.task_id, f"merge_conflict:{detail or 'unknown'}")
                return None

            merge_sha = _git_stdout(paths.target_path, ["rev-parse", "HEAD"])
            if action.backend == "clone" and not paths.target_is_local:
                _sync_source_repo_branch(
                    db,
                    action.task_id,
                    paths.target_path,
                    action.target_branch,
                )
            _mark_source_merged(action, db=db, source_id=paths.source_id)
            _complete_merge_stage(db, action.task_id, merge_sha)
            return merge_sha
        except RuntimeError as exc:
            reason = f"workspace_merge_failed:{exc}"
            _append_merge_failure_audit(db, action.task_id, reason)
            _fail_merge_stage(db, action.task_id, reason)
            return None
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
        if (
            _is_root_task(db, action.task_id)
            and artifacts.integration_workspace_id is not None
            and source_id == artifacts.integration_workspace_id
        ):
            if worktree_source is None:
                raise RuntimeError("source worktree metadata is missing")
            return _WorkspacePaths(
                worktree_source.worktree_path,
                str(_repo_path_for_task(db, action.task_id)),
                worktree_source.branch_name,
                worktree_source.id,
                None,
                target_is_local=True,
            )
        worktree_target = storage.get_by_branch(project_id, action.target_branch)
        if worktree_target is None:
            _repair_parent_integration_workspace(
                db,
                action.task_id,
                backend="worktree",
                project_id=project_id,
                services=services,
            )
            worktree_target = storage.get_by_branch(project_id, action.target_branch)
        if worktree_source is None:
            raise RuntimeError("source or target worktree metadata is missing")
        if worktree_target is None:
            local_target = _local_target_path_if_checked_out(
                db,
                action.task_id,
                action.target_branch,
            )
            if local_target is not None:
                return _WorkspacePaths(
                    worktree_source.worktree_path,
                    str(local_target),
                    worktree_source.branch_name,
                    worktree_source.id,
                    None,
                    target_is_local=True,
                )
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
    if (
        _is_root_task(db, action.task_id)
        and artifacts.integration_clone_id is not None
        and source_id == artifacts.integration_clone_id
    ):
        if clone_source is None:
            raise RuntimeError("source clone metadata is missing")
        return _WorkspacePaths(
            clone_source.clone_path,
            str(_repo_path_for_task(db, action.task_id)),
            clone_source.branch_name,
            clone_source.id,
            None,
            target_is_local=True,
        )
    clone_target = clone_storage.get_by_branch(project_id, action.target_branch)
    if clone_target is None:
        _repair_parent_integration_workspace(
            db,
            action.task_id,
            backend="clone",
            project_id=project_id,
            services=services,
        )
        clone_target = clone_storage.get_by_branch(project_id, action.target_branch)
    if clone_source is None:
        raise RuntimeError("source or target clone metadata is missing")
    if clone_target is None:
        local_target = _local_target_path_if_checked_out(
            db,
            action.task_id,
            action.target_branch,
        )
        if local_target is not None:
            return _WorkspacePaths(
                clone_source.clone_path,
                str(local_target),
                clone_source.branch_name,
                clone_source.id,
                None,
                target_is_local=True,
            )
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


def _is_root_task(db: DatabaseProtocol, task_id: str) -> bool:
    row = db.fetchone("SELECT parent_task_id FROM tasks WHERE id = ?", (task_id,))
    if row is None:
        raise RuntimeError(f"task not found: {task_id}")
    return row["parent_task_id"] is None


def _repair_parent_integration_workspace(
    db: DatabaseProtocol,
    task_id: str,
    *,
    backend: WorkspaceBackend,
    project_id: str,
    services: object | None,
) -> None:
    task_manager = LocalTaskManager(db)
    task = task_manager.get_task(task_id)
    ensure_task_parent_integration_workspace(
        task_manager=task_manager,
        task=task,
        backend=backend,
        project_id=project_id,
        services=services,
    )


def _require_integration_target(role: str) -> None:
    if role != "integration":
        raise RuntimeError("target workspace is not an integration workspace")


def _ensure_branch(path: str, expected: str, label: str) -> None:
    current = _git_stdout(path, ["rev-parse", "--abbrev-ref", "HEAD"])
    if current != expected:
        raise RuntimeError(f"{label} workspace branch mismatch: {current} != {expected}")


def _ensure_target_merge_safe(path: str, source_commit: str, label: str) -> None:
    status = _git_ok(path, ["status", "--porcelain"]).stdout
    dirty_paths = _non_gobby_dirty_paths(status)
    if not dirty_paths:
        return
    incoming_paths = set(
        _git_stdout(path, ["diff", "--name-only", "HEAD", source_commit]).splitlines()
    )
    overlapping = sorted(dirty_paths & incoming_paths)
    if overlapping:
        joined = ", ".join(overlapping)
        raise RuntimeError(f"{label} dirty paths overlap merge: {joined}")


def _status_path_is_gobby_only(pathspec: str) -> bool:
    paths = [part.strip() for part in pathspec.split(" -> ")]
    return all(path == ".gobby" or path.startswith(".gobby/") for path in paths)


def _non_gobby_dirty_paths(status_output: str) -> set[str]:
    paths: set[str] = set()
    for line in _non_gobby_status_lines(status_output):
        pathspec = _porcelain_pathspec(line)
        paths.update(part.strip() for part in pathspec.split(" -> ") if part.strip())
    return paths


def _non_gobby_status_lines(status_output: str) -> list[str]:
    dirty: list[str] = []
    for line in status_output.splitlines():
        if not line:
            continue
        pathspec = _porcelain_pathspec(line)
        if not _status_path_is_gobby_only(pathspec):
            dirty.append(line)
    return dirty


def _porcelain_pathspec(line: str) -> str:
    if len(line) >= 3 and line[2] == " ":
        return line[3:]
    if len(line) >= 2 and line[1] == " ":
        return line[2:]
    return line[3:] if len(line) > 3 else line


def _is_ancestor(target_path: str, commit_sha: str) -> bool:
    result = _git(Path(target_path), ["merge-base", "--is-ancestor", commit_sha, "HEAD"])
    return result.returncode == 0


def _conflicted_files(path: str) -> list[str]:
    result = _git(Path(path), ["diff", "--name-only", "--diff-filter=U"])
    return [line for line in result.stdout.splitlines() if line.strip()]


def _resolve_worktree_local_conflicts(path: str, conflicted: list[str]) -> list[str]:
    remaining: list[str] = []
    for file_path in conflicted:
        if file_path in WORKTREE_LOCAL_METADATA_CONFLICTS and _same_project_config_except_local(
            path,
            file_path,
        ):
            _git_ok(path, ["checkout", "--ours", "--", file_path])
            _git_ok(path, ["add", "--", file_path])
            continue
        if file_path == DOCS_GUIDES_README and _resolve_docs_guides_readme_conflict(
            path,
            file_path,
        ):
            continue
        remaining.append(file_path)
    return remaining


def _resolve_docs_guides_readme_conflict(path: str, file_path: str) -> bool:
    staged = _staged_file_versions(path, file_path)
    if staged is None:
        return False
    base, ours, theirs = staged
    non_row_changes_represented = _non_row_changes_are_represented(base, ours, theirs)
    if _without_guide_rows(base) != _without_guide_rows(theirs) and not (
        non_row_changes_represented
    ):
        return False

    base_rows = _guide_rows_by_key(base)
    ours_rows = _guide_rows_by_key(ours)
    theirs_rows = _guide_rows_by_key(theirs)
    changed_theirs = {
        key: row
        for key, row in theirs_rows.items()
        if key not in base_rows or row.line != base_rows[key].line
    }
    if not changed_theirs or any(key not in ours_rows for key in changed_theirs):
        if non_row_changes_represented:
            (Path(path) / file_path).write_text(ours)
            _git_ok(path, ["add", "--", file_path])
            return True
        return False

    merged_lines = ours.splitlines()
    for key, row in changed_theirs.items():
        ours_row = ours_rows[key]
        merged_lines[ours_row.index] = f"| {ours_row.link} | {row.description} |"
    merged = "\n".join(merged_lines)
    if ours.endswith("\n"):
        merged += "\n"
    (Path(path) / file_path).write_text(merged)
    _git_ok(path, ["add", "--", file_path])
    return True


def _staged_file_versions(path: str, file_path: str) -> tuple[str, str, str] | None:
    versions = []
    for stage in ("1", "2", "3"):
        result = _git(Path(path), ["show", f":{stage}:{file_path}"])
        if result.returncode != 0:
            return None
        versions.append(result.stdout)
    return versions[0], versions[1], versions[2]


def _guide_rows_by_key(text: str) -> dict[str, _GuideRow]:
    rows: dict[str, _GuideRow] = {}
    for index, line in enumerate(text.splitlines()):
        match = GUIDE_ROW_RE.match(line)
        if match is None:
            continue
        rows[_normalize_guide_target(match.group("target"))] = _GuideRow(
            index=index,
            link=match.group("link"),
            description=match.group("description"),
            line=line,
        )
    return rows


def _without_guide_rows(text: str) -> list[str]:
    return [line for line in text.splitlines() if GUIDE_ROW_RE.match(line) is None]


def _non_row_changes_are_represented(base: str, ours: str, theirs: str) -> bool:
    base_lines = Counter(_without_guide_rows(base))
    ours_lines = Counter(_without_guide_rows(ours))
    theirs_lines = Counter(_without_guide_rows(theirs))
    added_lines = theirs_lines - base_lines
    removed_lines = base_lines - theirs_lines
    return all(ours_lines[line] >= count for line, count in added_lines.items()) and all(
        ours_lines[line] <= theirs_lines[line] for line in removed_lines
    )


def _normalize_guide_target(target: str) -> str:
    if target.startswith("./"):
        return target[2:]
    return target


def _same_project_config_except_local(path: str, file_path: str) -> bool:
    staged = _staged_file_versions(path, file_path)
    if staged is None:
        return False
    _, ours, theirs = staged
    try:
        ours_json = json.loads(ours)
        theirs_json = json.loads(theirs)
    except json.JSONDecodeError:
        return False
    if not isinstance(ours_json, dict) or not isinstance(theirs_json, dict):
        return False
    return _without_worktree_local_project_keys(ours_json) == _without_worktree_local_project_keys(
        theirs_json,
    )


def _without_worktree_local_project_keys(value: dict[str, object]) -> dict[str, object]:
    return {key: item for key, item in value.items() if key not in WORKTREE_LOCAL_PROJECT_KEYS}


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
    try:
        cleanup_successful_merge_artifacts(db, task_id)
    except Exception:
        logger.warning(
            "successful_workspace_merge_cleanup_failed",
            extra={"task_id": task_id, "commit_sha": commit_sha},
            exc_info=True,
        )


def _fail_merge_stage(db: DatabaseProtocol, task_id: str, reason: str) -> None:
    StageStatesManager(db, TaskLifecycleEventManager(db)).fail_stage(
        task_id,
        "merge",
        reason=reason,
        needs_human=True,
        by_session_id="dispatcher",
    )


def _append_merge_failure_audit(db: DatabaseProtocol, task_id: str, reason: str) -> None:
    task_manager = LocalTaskManager(db)
    task = task_manager.get_task(task_id)
    description = task.description or ""
    marker = f"\n\n### Workspace merge failed\n\n{reason}"
    if marker in description:
        return
    task_manager.update_task(task_id, description=f"{description}{marker}")


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


def _local_target_path_if_checked_out(
    db: DatabaseProtocol,
    task_id: str,
    target_branch: str,
) -> Path | None:
    repo_path = _repo_path_for_task(db, task_id)
    current = _git_stdout(repo_path, ["rev-parse", "--abbrev-ref", "HEAD"])
    if current == target_branch:
        return repo_path
    return None


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
