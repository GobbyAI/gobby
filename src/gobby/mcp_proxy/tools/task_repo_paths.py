"""Repo path validation for task Git helper tools."""

from __future__ import annotations

import errno
import os
import stat
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from gobby.storage.clones import CloneStatus, LocalCloneManager
from gobby.storage.tasks import TaskNotFoundError
from gobby.storage.worktrees import LocalWorktreeManager, WorktreeStatus
from gobby.utils.git import run_git_command
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
    """Return a symlink-safe cwd validated for immediate task-scoped Git operations."""
    if project_path:
        candidate = _resolve_existing_dir(project_path, label="project_path")
        roots = list(_task_allowed_roots(task_manager, project_manager, task))
        if _is_under_any_root(candidate, roots):
            return str(candidate)
        raise RepoPathValidationError(
            "project_path is outside the task project repo and registered "
            "task/ancestor worktree or clone paths"
        )

    return _project_repo_path(project_manager, task.project_id)


@dataclass(frozen=True, slots=True)
class CloseWorktreeRoot:
    """Whether a task's registered isolation worktree is the close-gate root."""

    worktree_path: str | None
    repo_path: str | None
    skip_reason: str | None

    @property
    def applies(self) -> bool:
        return self.repo_path is not None


def resolve_close_worktree_root(
    *,
    task_manager: LocalTaskManager,
    task: Task,
    commit_shas: Sequence[str],
) -> CloseWorktreeRoot:
    """Pick the task's registered worktree as close root when its linked commits live there.

    Named acceptance tests may exist only on the worktree branch, so gates that
    resolve them against the main checkout fail for the wrong reason (#21098).
    The worktree qualifies when it is registered on the task, still exists, and
    every linked commit is reachable from its HEAD; otherwise ``skip_reason``
    says why so the close diagnostic can name it.
    """
    worktree_path = task_manager.artifacts.get_artifacts(task.id).worktree_path
    if not isinstance(worktree_path, str) or not worktree_path:
        return CloseWorktreeRoot(None, None, "the task has no registered isolation worktree")
    try:
        resolved = _resolve_existing_dir(worktree_path, label="registered worktree")
    except RepoPathValidationError as exc:
        return CloseWorktreeRoot(worktree_path, None, f"{exc} (not used)")
    if not commit_shas:
        return CloseWorktreeRoot(
            worktree_path,
            None,
            f"registered worktree {worktree_path} was not used: "
            "the close names no linked commit to locate there",
        )
    unreachable = [
        sha
        for sha in commit_shas
        if run_git_command(["git", "merge-base", "--is-ancestor", sha, "HEAD"], cwd=resolved)
        is None
    ]
    if unreachable:
        return CloseWorktreeRoot(
            worktree_path,
            None,
            f"registered worktree {worktree_path} was not used: linked commit "
            f"{', '.join(unreachable)} is not reachable from its HEAD",
        )
    return CloseWorktreeRoot(worktree_path, str(resolved), None)


def resolve_project_repo_path(
    *,
    project_manager: LocalProjectManager | None,
    project_path: str | None,
    project_id: str | None = None,
) -> str | None:
    """Return a symlink-safe cwd validated for immediate project-scoped Git operations."""
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
    default_repo = _project_repo_path_value(project_manager, task.project_id)
    if default_repo:
        yield default_repo

    tasks = list(_task_and_ancestors(task_manager, task))
    for ancestor in tasks:
        yield from _artifact_roots(task_manager, ancestor.id)
    yield from _registered_isolation_roots(task_manager)


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
    artifacts = task_manager.artifacts.get_artifacts(task_id)
    for value in (artifacts.worktree_path, artifacts.clone_path):
        if isinstance(value, str) and value:
            yield value


def _registered_isolation_roots(
    task_manager: LocalTaskManager,
) -> Iterable[str]:
    db = task_manager.db
    for worktree in LocalWorktreeManager(db).list_worktrees(
        status=WorktreeStatus.ACTIVE.value,
        limit=1000,
    ):
        yield worktree.worktree_path
    for clone in LocalCloneManager(db).list_clones(
        status=CloneStatus.ACTIVE.value,
        limit=1000,
    ):
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
    repo_path = _project_repo_path_value(project_manager, project_id)
    if repo_path is None:
        return None
    return str(_resolve_existing_dir(repo_path, label="project record repo_path"))


def _project_repo_path_value(
    project_manager: LocalProjectManager | None,
    project_id: str | None,
) -> str | None:
    if project_manager is None or not project_id:
        return None
    project = project_manager.get(project_id)
    repo_path = getattr(project, "repo_path", None) if project else None
    if not isinstance(repo_path, str) or not repo_path:
        return None
    return repo_path


def _current_project_id() -> str | None:
    context = get_project_context()
    project_id = context.get("id") if context else None
    return project_id if isinstance(project_id, str) else None


def _current_project_path() -> str | None:
    context = get_project_context()
    project_path = context.get("project_path") if context else None
    return project_path if isinstance(project_path, str) and project_path else None


def _resolve_path(path: str) -> Path:
    return _normalize_platform_path_alias(Path(path).expanduser().absolute())


def _normalize_platform_path_alias(path: Path) -> Path:
    """Normalize OS-level temp path aliases before symlink-safe validation."""
    if sys.platform != "darwin" or len(path.parts) < 2 or path.parts[1] != "var":
        return path

    var_path = Path("/var")
    if not var_path.is_symlink():
        return path
    try:
        real_var = var_path.resolve(strict=True)
    except OSError:
        return path
    if real_var != Path("/private/var"):
        return path
    return real_var.joinpath(*path.parts[2:])


def _resolve_existing_dir(path: str, *, label: str) -> Path:
    candidate = _resolve_path(path)
    _stat_existing_dir(candidate, label=label)
    return candidate


def _is_under_any_root(candidate: Path, roots: Iterable[str]) -> bool:
    try:
        candidate_stat = _stat_existing_dir(candidate, label="project_path")
    except RepoPathValidationError:
        return False

    for root in roots:
        try:
            resolved_root = _resolve_existing_dir(root, label="registered repository")
            root_stat = _stat_existing_dir(resolved_root, label="registered repository")
        except RepoPathValidationError:
            continue
        if _is_same_or_descendant(candidate, candidate_stat, root_stat):
            return True
    return False


def _is_same_or_descendant(
    candidate: Path,
    candidate_stat: os.stat_result,
    root_stat: os.stat_result,
) -> bool:
    current = candidate
    current_stat = candidate_stat
    while True:
        if _same_inode(current_stat, root_stat):
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent
        try:
            current_stat = _stat_existing_dir(current, label="project_path parent")
        except RepoPathValidationError:
            return False


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _stat_existing_dir(path: Path, *, label: str) -> os.stat_result:
    fd = _open_dir_no_symlinks(path, label=label)
    try:
        return os.fstat(fd)
    finally:
        os.close(fd)


def _open_dir_no_symlinks(path: Path, *, label: str) -> int:
    if not path.is_absolute():
        raise RepoPathValidationError(f"{label} must be an absolute path: {path}")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    parts = path.parts
    fd = os.open(parts[0], flags)
    current = Path(parts[0])
    try:
        for part in parts[1:]:
            current = current / part
            # The stat/open pair is intentionally dir-fd relative: stat with
            # follow_symlinks=False rejects symlink components, then O_NOFOLLOW
            # keeps a swapped component from becoming the opened directory.
            try:
                component_stat = os.stat(part, dir_fd=fd, follow_symlinks=False)
            except OSError as exc:
                _raise_path_error(exc, label=label, path=path)
            if stat.S_ISLNK(component_stat.st_mode):
                raise RepoPathValidationError(f"{label} contains symlink component: {current}")
            if not stat.S_ISDIR(component_stat.st_mode):
                raise RepoPathValidationError(f"{label} is not a directory: {path}")
            try:
                next_fd = os.open(part, flags | nofollow, dir_fd=fd)
            except OSError as exc:
                _raise_path_error(exc, label=label, path=path)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def _raise_path_error(exc: OSError, *, label: str, path: Path) -> NoReturn:
    if exc.errno == errno.ENOENT:
        raise RepoPathValidationError(f"{label} does not exist: {path}") from exc
    if exc.errno == errno.ENOTDIR:
        raise RepoPathValidationError(f"{label} is not a directory: {path}") from exc
    if exc.errno == errno.ELOOP:
        raise RepoPathValidationError(f"{label} contains symlink component: {path}") from exc
    raise RepoPathValidationError(f"{label} cannot be opened safely: {path}: {exc}") from exc
