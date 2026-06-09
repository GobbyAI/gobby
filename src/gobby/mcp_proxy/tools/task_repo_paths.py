"""Repo path validation for task Git helper tools."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from gobby.storage.clones import LocalCloneManager
from gobby.storage.tasks import TaskNotFoundError
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.project_context import get_project_context

if TYPE_CHECKING:
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.tasks import LocalTaskManager, Task


class RepoPathValidationError(ValueError):
    """Raised when an explicit Git repo path is not registered for the task scope."""


def resolve_task_repo_path(
    *,
    task_manager: LocalTaskManager,
    project_manager: LocalProjectManager | None,
    task: Task,
    project_path: str | None,
) -> str | None:
    """Return a safe cwd for task-scoped Git operations."""
    default_repo = _project_repo_path(project_manager, task.project_id)
    if not project_path:
        return default_repo

    candidate = _resolve_existing_dir(project_path, label="project_path")
    roots = list(_task_allowed_roots(task_manager, project_manager, task))
    if _is_under_any_root(candidate, roots):
        return str(candidate)
    raise RepoPathValidationError(
        "project_path is outside the task project repo and registered "
        "task/ancestor worktree or clone paths"
    )


def resolve_project_repo_path(
    *,
    project_manager: LocalProjectManager | None,
    project_path: str | None,
    project_id: str | None = None,
) -> str | None:
    """Return a safe cwd for project-scoped Git operations."""
    default_repo = _project_repo_path(project_manager, project_id or _current_project_id())
    if not project_path:
        return default_repo

    candidate = _resolve_existing_dir(project_path, label="project_path")
    roots = list(_registered_project_roots(project_manager))
    context_path = _current_project_path()
    if context_path:
        roots.append(context_path)
    if _is_under_any_root(candidate, roots):
        return str(candidate)
    raise RepoPathValidationError("project_path is not a registered project repository")


def _task_allowed_roots(
    task_manager: LocalTaskManager,
    project_manager: LocalProjectManager | None,
    task: Task,
) -> Iterable[str]:
    default_repo = _project_repo_path(project_manager, task.project_id)
    if default_repo:
        yield default_repo

    tasks = list(_task_and_ancestors(task_manager, task))
    task_ids = {ancestor.id for ancestor in tasks}
    for ancestor in tasks:
        yield from _artifact_roots(task_manager, ancestor.id)
    yield from _registered_isolation_roots(task_manager, task.project_id, task_ids)


def _task_and_ancestors(task_manager: LocalTaskManager, task: Task) -> Iterable[Task]:
    current: Task | None = task
    seen: set[str] = set()
    while current and current.id not in seen:
        yield current
        seen.add(current.id)
        if not current.parent_task_id:
            return
        try:
            current = task_manager.get_task(current.parent_task_id)
        except (TaskNotFoundError, ValueError):
            return


def _artifact_roots(task_manager: LocalTaskManager, task_id: str) -> Iterable[str]:
    try:
        artifacts = task_manager.artifacts.get_artifacts(task_id)
    except (AttributeError, TypeError, ValueError):
        return
    for value in (artifacts.worktree_path, artifacts.clone_path):
        if isinstance(value, str) and value:
            yield value


def _registered_isolation_roots(
    task_manager: LocalTaskManager,
    project_id: str,
    task_ids: set[str],
) -> Iterable[str]:
    db = task_manager.db
    for worktree in LocalWorktreeManager(db).list_worktrees(project_id=project_id, limit=1000):
        if worktree.task_id in task_ids:
            yield worktree.worktree_path
    for clone in LocalCloneManager(db).list_clones(project_id=project_id, limit=1000):
        if clone.task_id in task_ids:
            yield clone.clone_path


def _registered_project_roots(project_manager: LocalProjectManager | None) -> Iterable[str]:
    if project_manager is None:
        return
    for project in project_manager.list():
        repo_path = project.repo_path
        if isinstance(repo_path, str) and repo_path:
            yield repo_path


def _project_repo_path(
    project_manager: LocalProjectManager | None,
    project_id: str | None,
) -> str | None:
    if project_manager is None or not project_id:
        return None
    project = project_manager.get(project_id)
    repo_path = getattr(project, "repo_path", None) if project else None
    if not isinstance(repo_path, str) or not repo_path:
        return None
    return str(_resolve_existing_dir(repo_path, label="task project repository"))


def _current_project_id() -> str | None:
    context = get_project_context()
    project_id = context.get("id") if context else None
    return project_id if isinstance(project_id, str) else None


def _current_project_path() -> str | None:
    context = get_project_context()
    project_path = context.get("project_path") if context else None
    return project_path if isinstance(project_path, str) and project_path else None


def _resolve_path(path: str) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _resolve_existing_dir(path: str, *, label: str) -> Path:
    candidate = _resolve_path(path)
    if not candidate.exists():
        raise RepoPathValidationError(f"{label} does not exist: {candidate}")
    if not candidate.is_dir():
        raise RepoPathValidationError(f"{label} is not a directory: {candidate}")
    return candidate


def _is_under_any_root(candidate: Path, roots: Iterable[str]) -> bool:
    for root in roots:
        resolved_root = _resolve_path(root)
        try:
            candidate.relative_to(resolved_root)
        except ValueError:
            continue
        return True
    return False
