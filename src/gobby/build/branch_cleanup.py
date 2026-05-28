"""Scoped build branch cleanup helpers."""

from __future__ import annotations

import re
import subprocess  # nosec B404 # git subprocesses use fixed argument vectors.
from pathlib import Path

from gobby.storage.clones import LocalCloneManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.git import git_subprocess_env


def delete_orphan_build_branches(
    db: HubDatabase,
    project_id: str,
    tasks: list[Task],
) -> tuple[int, list[str]]:
    """Delete local task/integration branches owned by a cleaned build scope."""
    repo_path = project_path(db, project_id)
    candidates = build_branch_candidates(db, project_id, tasks, repo_path=repo_path)
    if not candidates:
        return 0, []

    existing = local_branches(repo_path)
    current = current_branch(repo_path)
    deleted = 0
    errors: list[str] = []

    for branch in sorted(candidates & existing):
        if branch == current:
            errors.append(f"refusing to delete current branch {branch}")
            continue
        result = git(repo_path, ["branch", "-D", branch], timeout=30)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            if is_missing_branch_delete(branch, detail):
                continue
            errors.append(f"failed to delete build branch {branch}: {detail}")
            continue
        deleted += 1

    return deleted, errors


def build_branch_candidates(
    db: HubDatabase,
    project_id: str,
    tasks: list[Task],
    *,
    repo_path: Path,
) -> set[str]:
    """Return branch names that belong to the provided build task scope."""
    worktrees = LocalWorktreeManager(db)
    clones = LocalCloneManager(db)
    task_manager = LocalTaskManager(db)
    branches = local_branches(repo_path)
    candidates: set[str] = set()

    for task in tasks:
        artifacts = task_manager.artifacts.get_artifacts(task.id)
        if artifacts.integration_branch:
            candidates.add(artifacts.integration_branch)
        if task.task_type == "epic":
            candidates.add(integration_branch_name(task))
        if task.seq_num:
            candidates.add(default_task_branch_name(task))
            prefix = f"task-{task.seq_num}-"
            candidates.update(branch for branch in branches if branch.startswith(prefix))

        for worktree_id in (artifacts.worktree_id, artifacts.integration_workspace_id):
            if not worktree_id:
                continue
            worktree = worktrees.get(worktree_id)
            if worktree is not None:
                candidates.add(worktree.branch_name)
        if artifacts.clone_id:
            clone = clones.get(artifacts.clone_id)
            if clone is not None:
                candidates.add(clone.branch_name)
        if artifacts.integration_clone_id:
            clone = clones.get(artifacts.integration_clone_id)
            if clone is not None:
                candidates.add(clone.branch_name)

        worktree = worktrees.get_by_task(task.id)
        if worktree is not None:
            candidates.add(worktree.branch_name)
        clone = clones.get_by_task(task.id)
        if clone is not None:
            candidates.add(clone.branch_name)

    return {branch for branch in candidates if branch}


def default_task_branch_name(task: Task) -> str:
    """Return the default task branch name used by agent worktree isolation."""
    slug = task.title.lower().replace(" ", "-")
    slug = "".join(c for c in slug if c.isalnum() or c == "-")
    branch_slug = slug[:40] or "untitled"
    return f"task-{task.seq_num}-{branch_slug}"


def integration_branch_name(task: Task) -> str:
    """Return the default integration branch name used for build epics."""
    ref = str(task.seq_num or task.id[:8])
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", task.title.lower()).strip("-")[:36]
    return f"gobby/integration/{ref}-{slug or 'epic'}"


def local_branches(repo_path: Path) -> set[str]:
    result = git(repo_path, ["branch", "--format=%(refname:short)"], timeout=30)
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def current_branch(repo_path: Path) -> str | None:
    result = git(repo_path, ["branch", "--show-current"], timeout=10)
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return branch or None


def is_missing_branch_delete(branch: str, detail: str) -> bool:
    """Return whether git failed because the branch was already absent."""
    normalized = detail.lower()
    quoted = branch.lower()
    return (
        "not found" in normalized
        and ("branch" in normalized or "ref" in normalized)
        and (quoted in normalized or f"'{quoted}'" in normalized)
    )


def project_path(db: HubDatabase, project_id: str) -> Path:
    project = LocalProjectManager(db).get(project_id)
    if project is not None and project.repo_path:
        return Path(project.repo_path)
    return Path.cwd()


def git(repo_path: Path, args: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    env = git_subprocess_env()
    subprocess_kwargs = {"env": env} if env is not None else {}
    return subprocess.run(  # nosec B603 # git args are fixed by callers.
        ["git", *args],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        **subprocess_kwargs,
    )
