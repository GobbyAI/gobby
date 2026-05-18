"""Integration workspace setup for build automation."""

from __future__ import annotations

import re
import subprocess  # nosec B404 # git subprocesses use fixed argument vectors.
from pathlib import Path
from typing import Literal, cast

from gobby.clones.git import CloneGitManager
from gobby.storage.clones import Clone, LocalCloneManager
from gobby.storage.database import DatabaseProtocol
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.tasks._artifacts import TaskArtifacts
from gobby.storage.worktrees import LocalWorktreeManager, Worktree
from gobby.worktrees.git import WorktreeGitManager, WorktreeInfo

WorkspaceBackend = Literal["worktree", "clone"]


class BuildWorkspaceError(ValueError):
    """Raised when build integration workspace state is unsafe to reuse."""


def ensure_epic_integration_workspaces(
    *,
    task_manager: LocalTaskManager,
    root_task: Task,
    backend: WorkspaceBackend,
    target_branch: str,
    project_id: str,
    services: object | None,
) -> None:
    """Create/reuse integration workspaces for open epics in a build subtree."""
    repo_path = _project_repo_path(task_manager.db, project_id)
    workspace_services = _WorkspaceServices.resolve(
        db=task_manager.db,
        project_id=project_id,
        repo_path=repo_path,
        services=services,
    )
    tasks = _subtree_tasks(task_manager.db, root_task.id)
    parent_by_id = {task.id: task.parent_task_id for task in tasks}
    integration_by_epic: dict[str, str] = {}

    for task in tasks:
        if task.task_type != "epic" or task.closed_at is not None:
            continue
        artifacts = task_manager.artifacts.get_artifacts(task.id)
        parent_integration = _nearest_ancestor_integration(
            task.parent_task_id,
            parent_by_id=parent_by_id,
            integration_by_epic=integration_by_epic,
        )
        base_branch = parent_integration or target_branch
        integration_branch = artifacts.integration_branch or _integration_branch(task)
        integration = workspace_services.ensure_integration(
            task=task,
            backend=backend,
            branch_name=integration_branch,
            base_branch=base_branch,
            artifacts=artifacts,
        )
        artifact_fields: dict[str, str | int | None] = {
            "integration_branch": integration_branch,
            "target_branch": base_branch,
        }
        if backend == "worktree":
            artifact_fields["integration_workspace_id"] = integration.id
            artifact_fields["integration_clone_id"] = None
        else:
            artifact_fields["integration_clone_id"] = integration.id
            artifact_fields["integration_workspace_id"] = None
        task_manager.artifacts.set_artifacts_atomic(task.id, **artifact_fields)
        integration_by_epic[task.id] = integration_branch

    _cascade_nearest_integration_branch(
        task_manager,
        tasks=tasks,
        root_task_id=root_task.id,
        parent_by_id=parent_by_id,
        integration_by_epic=integration_by_epic,
    )


def ensure_task_parent_integration_workspace(
    *,
    task_manager: LocalTaskManager,
    task: Task,
    backend: WorkspaceBackend,
    project_id: str,
    services: object | None,
    base_branch_override: str | None = None,
) -> Worktree | Clone | None:
    """Ensure the nearest parent epic integration workspace for a child task."""
    if not task.parent_task_id:
        return None

    task_artifacts = task_manager.artifacts.get_artifacts(task.id)
    branch_name = task_artifacts.target_branch
    if not branch_name:
        return None

    match = _nearest_parent_epic_for_integration_branch(
        task_manager,
        task,
        branch_name,
    )
    if match is None:
        return None

    epic, artifacts = match
    base_branch = (
        artifacts.target_branch
        or base_branch_override
        or _nearest_ancestor_integration_branch(task_manager, epic.parent_task_id)
    )
    if base_branch is None:
        raise BuildWorkspaceError("target_branch is required for parent integration workspace")

    repo_path = _project_repo_path(task_manager.db, project_id)
    workspace_services = _WorkspaceServices.resolve(
        db=task_manager.db,
        project_id=project_id,
        repo_path=repo_path,
        services=services,
    )
    integration = workspace_services.ensure_integration(
        task=epic,
        backend=backend,
        branch_name=branch_name,
        base_branch=base_branch,
        artifacts=artifacts,
    )
    artifact_fields: dict[str, str | int | None] = {
        "integration_branch": branch_name,
        "target_branch": base_branch,
    }
    if backend == "worktree":
        artifact_fields["integration_workspace_id"] = integration.id
        artifact_fields["integration_clone_id"] = None
    else:
        artifact_fields["integration_clone_id"] = integration.id
        artifact_fields["integration_workspace_id"] = None
    task_manager.artifacts.set_artifacts_atomic(epic.id, **artifact_fields)
    return integration


class _WorkspaceServices:
    def __init__(
        self,
        *,
        project_id: str,
        repo_path: Path,
        git_manager: WorktreeGitManager,
        worktree_storage: LocalWorktreeManager,
        clone_manager: CloneGitManager,
        clone_storage: LocalCloneManager,
    ) -> None:
        self.project_id = project_id
        self.repo_path = repo_path
        self.git_manager = git_manager
        self.worktree_storage = worktree_storage
        self.clone_manager = clone_manager
        self.clone_storage = clone_storage

    @classmethod
    def resolve(
        cls,
        *,
        db: DatabaseProtocol,
        project_id: str,
        repo_path: Path,
        services: object | None,
    ) -> _WorkspaceServices:
        git_manager = _service_git_manager(services, project_id) or WorktreeGitManager(repo_path)
        worktree_storage = cast(
            LocalWorktreeManager,
            getattr(services, "worktree_storage", None) or LocalWorktreeManager(db),
        )
        clone_storage = cast(
            LocalCloneManager,
            getattr(services, "clone_storage", None) or LocalCloneManager(db),
        )
        clone_manager = cast(
            CloneGitManager,
            getattr(services, "clone_manager", None) or CloneGitManager(repo_path),
        )
        return cls(
            project_id=project_id,
            repo_path=repo_path,
            git_manager=git_manager,
            worktree_storage=worktree_storage,
            clone_manager=clone_manager,
            clone_storage=clone_storage,
        )

    def ensure_integration(
        self,
        *,
        task: Task,
        backend: WorkspaceBackend,
        branch_name: str,
        base_branch: str,
        artifacts: TaskArtifacts,
    ) -> Worktree | Clone:
        if backend == "worktree":
            return self._ensure_worktree(task, branch_name, base_branch, artifacts)
        return self._ensure_clone(task, branch_name, base_branch, artifacts)

    def _ensure_worktree(
        self,
        task: Task,
        branch_name: str,
        base_branch: str,
        artifacts: TaskArtifacts,
    ) -> Worktree:
        if artifacts.integration_workspace_id:
            existing = self.worktree_storage.get(artifacts.integration_workspace_id)
            if existing is None:
                raise BuildWorkspaceError("integration worktree metadata is missing; clean/restart")
            self._validate_record(existing, branch_name=branch_name, backend="worktree")
            _ensure_clean_git_dir(existing.worktree_path)
            return existing

        existing = self.worktree_storage.get_by_branch(self.project_id, branch_name)
        if existing is not None:
            self._validate_record(existing, branch_name=branch_name, backend="worktree")
            _ensure_clean_git_dir(existing.worktree_path)
            return existing

        unmanaged = self._find_unmanaged_worktree(branch_name)
        if unmanaged is not None:
            stored = self.worktree_storage.get_by_path(unmanaged.path)
            if stored is not None:
                self._validate_record(stored, branch_name=branch_name, backend="worktree")
                _ensure_clean_git_dir(stored.worktree_path)
                return stored
            _ensure_clean_git_dir(unmanaged.path)
            return self.worktree_storage.create(
                project_id=self.project_id,
                branch_name=branch_name,
                worktree_path=unmanaged.path,
                base_branch=base_branch,
                task_id=task.id,
                workspace_role="integration",
            )

        branch_exists = _branch_exists(self.repo_path, branch_name)
        path = _workspace_path("worktrees", self.repo_path.name, branch_name)
        result = self.git_manager.create_worktree(
            worktree_path=path,
            branch_name=branch_name,
            base_branch=base_branch,
            create_branch=not branch_exists,
            use_local=True,
        )
        if not result.success:
            raise BuildWorkspaceError(result.error or result.message)
        return self.worktree_storage.create(
            project_id=self.project_id,
            branch_name=branch_name,
            worktree_path=str(path),
            base_branch=base_branch,
            task_id=task.id,
            workspace_role="integration",
        )

    def _ensure_clone(
        self,
        task: Task,
        branch_name: str,
        base_branch: str,
        artifacts: TaskArtifacts,
    ) -> Clone:
        if artifacts.integration_clone_id:
            existing = self.clone_storage.get(artifacts.integration_clone_id)
            if existing is None:
                raise BuildWorkspaceError("integration clone metadata is missing; clean/restart")
            self._validate_record(existing, branch_name=branch_name, backend="clone")
            _ensure_clean_git_dir(existing.clone_path)
            return existing

        existing = self.clone_storage.get_by_branch(self.project_id, branch_name)
        if existing is not None:
            self._validate_record(existing, branch_name=branch_name, backend="clone")
            _ensure_clean_git_dir(existing.clone_path)
            return existing

        _ensure_source_branch(self.repo_path, branch_name=branch_name, base_branch=base_branch)
        path = _workspace_path("clones", self.repo_path.name, branch_name)
        result = self.clone_manager.create_clone(
            clone_path=path,
            branch_name=branch_name,
            base_branch=branch_name,
            shallow=False,
            use_local=True,
        )
        if not result.success:
            raise BuildWorkspaceError(result.error or result.message)
        return self.clone_storage.create(
            project_id=self.project_id,
            branch_name=branch_name,
            clone_path=str(path),
            base_branch=base_branch,
            task_id=task.id,
            workspace_role="integration",
        )

    def _find_unmanaged_worktree(self, branch_name: str) -> WorktreeInfo | None:
        for worktree in self.git_manager.list_worktrees():
            if worktree.branch == branch_name and Path(worktree.path).is_dir():
                return worktree
        return None

    @staticmethod
    def _validate_record(
        record: Worktree | Clone,
        *,
        branch_name: str,
        backend: WorkspaceBackend,
    ) -> None:
        role = getattr(record, "workspace_role", "task")
        path = getattr(record, "worktree_path", None) or getattr(record, "clone_path", None)
        if role != "integration":
            raise BuildWorkspaceError(
                f"{backend} branch {branch_name} is already used by a task workspace"
            )
        if record.branch_name != branch_name:
            raise BuildWorkspaceError(
                f"integration {backend} branch mismatch: {record.branch_name} != {branch_name}"
            )
        if path is None or not Path(str(path)).is_dir():
            raise BuildWorkspaceError(f"integration {backend} path is missing; clean/restart")


def _project_repo_path(db: DatabaseProtocol, project_id: str) -> Path:
    project = LocalProjectManager(db).get(project_id)
    if project is None or not project.repo_path:
        raise BuildWorkspaceError("project repo_path is required for integration workspaces")
    repo_path = Path(project.repo_path)
    if not (repo_path / ".git").exists():
        raise BuildWorkspaceError(f"project repo_path is not a git repository: {repo_path}")
    return repo_path


def _service_git_manager(services: object | None, project_id: str) -> WorktreeGitManager | None:
    getter = getattr(services, "get_git_manager", None)
    if callable(getter):
        try:
            manager = getter(project_id)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to resolve build workspace git manager for project {project_id}"
            ) from exc
        if manager is not None:
            return cast(WorktreeGitManager, manager)
    manager = getattr(services, "git_manager", None)
    return cast(WorktreeGitManager | None, manager)


def _subtree_tasks(db: DatabaseProtocol, root_task_id: str) -> list[Task]:
    rows = db.fetchall(
        """
        WITH RECURSIVE subtree(id, depth) AS (
            SELECT id, 0 FROM tasks WHERE id = ?
            UNION ALL
            SELECT child.id, parent.depth + 1
              FROM tasks child
              JOIN subtree parent ON child.parent_task_id = parent.id
        )
        SELECT tasks.*
          FROM tasks
          JOIN subtree ON subtree.id = tasks.id
         ORDER BY subtree.depth, tasks.seq_num, tasks.created_at
        """,
        (root_task_id,),
    )
    return [Task.from_row(row) for row in rows]


def _cascade_nearest_integration_branch(
    task_manager: LocalTaskManager,
    *,
    tasks: list[Task],
    root_task_id: str,
    parent_by_id: dict[str, str | None],
    integration_by_epic: dict[str, str],
) -> None:
    for task in tasks:
        if task.id == root_task_id or task.closed_at is not None:
            continue
        branch = _nearest_ancestor_integration(
            parent_by_id.get(task.id),
            parent_by_id=parent_by_id,
            integration_by_epic=integration_by_epic,
        )
        if branch:
            task_manager.artifacts.set_artifact(task.id, "target_branch", branch)


def _nearest_ancestor_integration(
    task_id: str | None,
    *,
    parent_by_id: dict[str, str | None],
    integration_by_epic: dict[str, str],
) -> str | None:
    current = task_id
    while current:
        branch = integration_by_epic.get(current)
        if branch:
            return branch
        current = parent_by_id.get(current)
    return None


def _nearest_parent_epic_for_integration_branch(
    task_manager: LocalTaskManager,
    task: Task,
    branch_name: str,
) -> tuple[Task, TaskArtifacts] | None:
    current_id = task.parent_task_id
    while current_id:
        current = _task_by_id(task_manager.db, current_id)
        if current is None:
            return None
        if current.task_type == "epic":
            artifacts = task_manager.artifacts.get_artifacts(current.id)
            if artifacts.integration_branch == branch_name:
                return current, artifacts
            if artifacts.integration_branch is None and _integration_branch(current) == branch_name:
                return current, artifacts
        current_id = current.parent_task_id
    return None


def _nearest_ancestor_integration_branch(
    task_manager: LocalTaskManager,
    task_id: str | None,
) -> str | None:
    current_id = task_id
    while current_id:
        task = _task_by_id(task_manager.db, current_id)
        if task is None:
            return None
        artifacts = task_manager.artifacts.get_artifacts(task.id)
        if artifacts.integration_branch:
            return artifacts.integration_branch
        current_id = task.parent_task_id
    return None


def _task_by_id(db: DatabaseProtocol, task_id: str) -> Task | None:
    row = db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    return Task.from_row(row) if row is not None else None


def _integration_branch(task: Task) -> str:
    ref = str(task.seq_num or task.id[:8])
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", task.title.lower()).strip("-")[:36]
    return f"gobby/integration/{ref}-{slug or 'epic'}"


def _workspace_path(kind: str, project_name: str, branch_name: str) -> Path:
    safe_branch = branch_name.replace("/", "-").replace("\\", "-")
    return Path.home() / ".gobby" / kind / project_name / safe_branch


def _branch_exists(repo_path: Path, branch_name: str) -> bool:
    result = _git(repo_path, ["rev-parse", "--verify", branch_name], timeout=10)
    return result.returncode == 0


def _ensure_source_branch(repo_path: Path, *, branch_name: str, base_branch: str) -> None:
    if _branch_exists(repo_path, branch_name):
        return
    result = _git(repo_path, ["branch", branch_name, base_branch], timeout=30)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise BuildWorkspaceError(f"failed to create integration branch {branch_name}: {detail}")


def _ensure_clean_git_dir(path: str) -> None:
    result = _git(Path(path), ["status", "--porcelain"], timeout=10)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise BuildWorkspaceError(f"failed to inspect integration workspace {path}: {detail}")
    if result.stdout.strip():
        raise BuildWorkspaceError(f"integration workspace is dirty; clean/restart: {path}")


def _git(repo_path: Path, args: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 # git args are fixed by callers.
        ["git", *args],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
