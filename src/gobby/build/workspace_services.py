"""Storage and adoption services for build integration workspaces."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import cast

from gobby.build.workspace_common import BuildWorkspaceError, WorkspaceBackend
from gobby.build.workspace_git import (
    _branch_exists,
    _clone_base_ref,
    _ensure_source_branch,
    _is_git_workspace_dir,
    _refresh_clean_git_dir,
    _workspace_path,
)
from gobby.build.workspace_recovery import (
    _is_promotable_workspace,
    _is_recoverable_workspace,
    recover_stale_integration_artifact,
)
from gobby.clones.git import CloneGitManager
from gobby.storage.clones import Clone, LocalCloneManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.tasks._artifacts import TaskArtifacts
from gobby.storage.worktrees import LocalWorktreeManager, Worktree
from gobby.worktrees.git import WorktreeGitManager, WorktreeInfo


class _WorkspaceServices:
    def __init__(
        self,
        *,
        db: HubDatabase,
        task_manager: LocalTaskManager,
        project_id: str,
        repo_path: Path,
        git_manager: WorktreeGitManager,
        worktree_storage: LocalWorktreeManager,
        clone_manager: CloneGitManager,
        clone_storage: LocalCloneManager,
    ) -> None:
        self.db = db
        self.task_manager = task_manager
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
        db: HubDatabase,
        task_manager: LocalTaskManager,
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
            db=db,
            task_manager=task_manager,
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

    def existing_task_workspace_branch(
        self,
        *,
        task: Task,
        backend: WorkspaceBackend,
        artifacts: TaskArtifacts,
    ) -> str | None:
        if backend == "worktree" and artifacts.worktree_id:
            worktree = self.worktree_storage.get(artifacts.worktree_id)
            if worktree is not None and _is_recoverable_workspace(worktree, task.id, "worktree"):
                return worktree.branch_name
        if backend == "clone" and artifacts.clone_id:
            clone = self.clone_storage.get(artifacts.clone_id)
            if clone is not None and _is_recoverable_workspace(clone, task.id, "clone"):
                return clone.branch_name
        return None

    def _ensure_worktree(
        self,
        task: Task,
        branch_name: str,
        base_branch: str,
        artifacts: TaskArtifacts,
    ) -> Worktree:
        path = _workspace_path("worktrees", self.repo_path.name, branch_name)
        if artifacts.integration_workspace_id:
            existing = self.worktree_storage.get(artifacts.integration_workspace_id)
            recovered = recover_stale_integration_artifact(
                db=self.db,
                task_manager=self.task_manager,
                task_id=task.id,
                backend="worktree",
                workspace_id=artifacts.integration_workspace_id,
                record=existing,
            )
            if recovered:
                if existing is not None:
                    _remove_invalid_workspace_dir(existing.worktree_path, expected_path=path)
                    self.worktree_storage.delete(existing.id)
            else:
                self._validate_record(existing, branch_name=branch_name, backend="worktree")
                assert existing is not None
                _refresh_clean_git_dir(existing.worktree_path, branch_name, base_branch)
                return existing

        existing = self.worktree_storage.get_by_branch(self.project_id, branch_name)
        if existing is not None:
            if _is_stale_integration_record(existing, task.id):
                recovered = recover_stale_integration_artifact(
                    db=self.db,
                    task_manager=self.task_manager,
                    task_id=task.id,
                    backend="worktree",
                    workspace_id=existing.id,
                    record=existing,
                )
                if recovered:
                    _remove_invalid_workspace_dir(existing.worktree_path, expected_path=path)
                    self.worktree_storage.delete(existing.id)
                    existing = None
            if existing is not None and _is_promotable_workspace(existing, task.id, "worktree"):
                promoted = self.worktree_storage.update(existing.id, workspace_role="integration")
                if promoted is None:
                    raise BuildWorkspaceError("failed to promote task worktree to integration")
                _refresh_clean_git_dir(promoted.worktree_path, branch_name, base_branch)
                return promoted
            if existing is not None:
                self._validate_record(existing, branch_name=branch_name, backend="worktree")
                _refresh_clean_git_dir(existing.worktree_path, branch_name, base_branch)
                return existing

        unmanaged = self._find_unmanaged_worktree(branch_name)
        if unmanaged is not None:
            stored = self.worktree_storage.get_by_path(unmanaged.path)
            if stored is not None:
                self._validate_record(stored, branch_name=branch_name, backend="worktree")
                _refresh_clean_git_dir(stored.worktree_path, branch_name, base_branch)
                return stored
            _refresh_clean_git_dir(unmanaged.path, branch_name, base_branch)
            return self.worktree_storage.create(
                project_id=self.project_id,
                branch_name=branch_name,
                worktree_path=unmanaged.path,
                base_branch=base_branch,
                task_id=task.id,
                workspace_role="integration",
            )

        branch_exists = _branch_exists(self.repo_path, branch_name)
        result = self.git_manager.create_worktree(
            worktree_path=path,
            branch_name=branch_name,
            base_branch=base_branch,
            create_branch=not branch_exists,
            use_local=True,
        )
        if not result.success:
            raise BuildWorkspaceError(result.error or result.message)
        _refresh_clean_git_dir(path, branch_name, base_branch)
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
        path = _workspace_path("clones", self.repo_path.name, branch_name)
        if artifacts.integration_clone_id:
            existing = self.clone_storage.get(artifacts.integration_clone_id)
            recovered = recover_stale_integration_artifact(
                db=self.db,
                task_manager=self.task_manager,
                task_id=task.id,
                backend="clone",
                workspace_id=artifacts.integration_clone_id,
                record=existing,
            )
            if recovered:
                if existing is not None:
                    _remove_invalid_workspace_dir(existing.clone_path, expected_path=path)
                    self.clone_storage.delete(existing.id)
            else:
                self._validate_record(existing, branch_name=branch_name, backend="clone")
                assert existing is not None
                _refresh_clean_git_dir(
                    existing.clone_path,
                    branch_name,
                    _clone_base_ref(existing.clone_path, base_branch),
                )
                return existing

        existing = self.clone_storage.get_by_branch(self.project_id, branch_name)
        if existing is not None:
            if _is_stale_integration_record(existing, task.id):
                recovered = recover_stale_integration_artifact(
                    db=self.db,
                    task_manager=self.task_manager,
                    task_id=task.id,
                    backend="clone",
                    workspace_id=existing.id,
                    record=existing,
                )
                if recovered:
                    _remove_invalid_workspace_dir(existing.clone_path, expected_path=path)
                    self.clone_storage.delete(existing.id)
                    existing = None
            if existing is not None and _is_promotable_workspace(existing, task.id, "clone"):
                promoted = self.clone_storage.update(existing.id, workspace_role="integration")
                if promoted is None:
                    raise BuildWorkspaceError("failed to promote task clone to integration")
                _refresh_clean_git_dir(
                    promoted.clone_path,
                    branch_name,
                    _clone_base_ref(promoted.clone_path, base_branch),
                )
                return promoted
            if existing is not None:
                self._validate_record(existing, branch_name=branch_name, backend="clone")
                _refresh_clean_git_dir(
                    existing.clone_path,
                    branch_name,
                    _clone_base_ref(existing.clone_path, base_branch),
                )
                return existing

        _ensure_source_branch(self.repo_path, branch_name=branch_name, base_branch=base_branch)
        result = self.clone_manager.create_clone(
            clone_path=path,
            branch_name=branch_name,
            base_branch=branch_name,
            shallow=False,
            use_local=True,
        )
        if not result.success:
            raise BuildWorkspaceError(result.error or result.message)
        _refresh_clean_git_dir(path, branch_name, _clone_base_ref(path, base_branch))
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
        record: Worktree | Clone | None,
        *,
        branch_name: str,
        backend: WorkspaceBackend,
    ) -> None:
        if record is None:
            raise BuildWorkspaceError(f"integration {backend} metadata is missing; clean/restart")
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
    return manager


def _is_stale_integration_record(record: Worktree | Clone, task_id: str) -> bool:
    if record.task_id != task_id or getattr(record, "workspace_role", "task") != "integration":
        return False
    raw_path = getattr(record, "worktree_path", None) or getattr(record, "clone_path", None)
    return (
        raw_path is not None
        and Path(str(raw_path)).is_dir()
        and not _is_git_workspace_dir(raw_path)
    )


def _remove_invalid_workspace_dir(raw_path: str | None, *, expected_path: Path) -> None:
    if raw_path is None:
        return
    path = Path(raw_path)
    if path != expected_path or not path.is_dir() or path.is_symlink():
        return
    if _is_git_workspace_dir(path):
        return
    shutil.rmtree(path)
