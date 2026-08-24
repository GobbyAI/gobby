"""Integration workspace setup for build automation."""

from __future__ import annotations

import re
from pathlib import Path

from gobby.build.workspace_common import BuildWorkspaceError, WorkspaceBackend
from gobby.build.workspace_git import _merge_required_commits, _refresh_clean_git_dir
from gobby.build.workspace_services import _WorkspaceServices
from gobby.storage.clones import Clone
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.worktrees import Worktree

__all__ = [
    "BuildWorkspaceError",
    "WorkspaceBackend",
    "_integration_branch",
    "_refresh_clean_git_dir",
    "ensure_epic_integration_workspaces",
    "ensure_task_parent_integration_workspace",
]


def ensure_epic_integration_workspaces(
    *,
    task_manager: LocalTaskManager,
    root_task: Task,
    backend: WorkspaceBackend,
    target_branch: str,
    project_id: str,
    services: object | None,
    merge_closed_descendant_commits: bool = False,
    repair_only: bool = False,
) -> None:
    """Create or repair integration workspaces for open epics in a build subtree."""
    root_artifacts = task_manager.artifacts.get_artifacts(root_task.id)
    if repair_only and not root_artifacts.integration_branch:
        raise BuildWorkspaceError(
            f"epic integration workspace has not been provisioned for {_task_ref(root_task)}"
        )

    repo_path = _project_repo_path(task_manager.db, project_id)
    workspace_services = _WorkspaceServices.resolve(
        db=task_manager.db,
        task_manager=task_manager,
        project_id=project_id,
        repo_path=repo_path,
        services=services,
    )
    tasks = _subtree_tasks(task_manager.db, root_task.id)
    parent_by_id = {task.id: task.parent_task_id for task in tasks}
    integration_by_epic: dict[str, str] = {}

    for task in tasks:
        if task.task_type != "epic":
            continue
        artifacts = task_manager.artifacts.get_artifacts(task.id)
        if repair_only and not artifacts.integration_branch:
            continue
        if task.closed_at is not None and not (
            merge_closed_descendant_commits and artifacts.integration_branch
        ):
            # An epic closes the moment its last child closes (#20600), so by
            # repair time the epic owning the work is normally already closed,
            # and its last child's commit is the one most likely to be missing.
            # Merging into a workspace it already has is what this repair is
            # for; provisioning a new one for a closed epic is not.
            continue
        parent_integration = _nearest_ancestor_integration(
            task.parent_task_id,
            parent_by_id=parent_by_id,
            integration_by_epic=integration_by_epic,
        )
        base_branch = parent_integration or target_branch
        integration_branch = (
            artifacts.integration_branch
            or workspace_services.existing_task_workspace_branch(
                task=task,
                backend=backend,
                artifacts=artifacts,
            )
            or _integration_branch(task)
        )
        integration = workspace_services.ensure_integration(
            task=task,
            backend=backend,
            branch_name=integration_branch,
            base_branch=base_branch,
            artifacts=artifacts,
        )
        task_manager.artifacts.set_artifacts_atomic(
            task.id,
            **_integration_artifact_fields(
                backend=backend,
                integration_id=integration.id,
                integration_branch=integration_branch,
                base_branch=base_branch,
            ),
        )
        if merge_closed_descendant_commits:
            _merge_closed_descendant_commits(
                tasks=tasks,
                parent_by_id=parent_by_id,
                epic_id=task.id,
                workspace=integration,
                source_repo_path=repo_path,
            )
        integration_by_epic[task.id] = integration_branch

    if not repair_only:
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
) -> Worktree | Clone | None:
    """Ensure the open epic ancestry required by an isolated child task."""
    ancestor_epics = _open_ancestor_epics(task_manager, task)
    if not ancestor_epics:
        return None

    root_epic = ancestor_epics[0]
    root_artifacts = task_manager.artifacts.get_artifacts(root_epic.id)
    base_branch = root_artifacts.target_branch
    if not base_branch:
        raise BuildWorkspaceError(
            f"target_branch is required for root epic integration workspace {_task_ref(root_epic)}"
        )

    repo_path = _project_repo_path(task_manager.db, project_id)
    workspace_services = _WorkspaceServices.resolve(
        db=task_manager.db,
        task_manager=task_manager,
        project_id=project_id,
        repo_path=repo_path,
        services=services,
    )
    nearest_integration: Worktree | Clone | None = None
    nearest_branch: str | None = None

    for epic in ancestor_epics:
        artifacts = task_manager.artifacts.get_artifacts(epic.id)
        integration_branch = (
            artifacts.integration_branch
            or workspace_services.existing_task_workspace_branch(
                task=epic,
                backend=backend,
                artifacts=artifacts,
            )
            or _integration_branch(epic)
        )
        integration = workspace_services.ensure_integration(
            task=epic,
            backend=backend,
            branch_name=integration_branch,
            base_branch=base_branch,
            artifacts=artifacts,
        )
        task_manager.artifacts.set_artifacts_atomic(
            epic.id,
            **_integration_artifact_fields(
                backend=backend,
                integration_id=integration.id,
                integration_branch=integration_branch,
                base_branch=base_branch,
            ),
        )
        nearest_integration = integration
        nearest_branch = integration_branch
        base_branch = integration_branch

    if nearest_branch is not None:
        task_manager.artifacts.set_artifact(task.id, "target_branch", nearest_branch)
    return nearest_integration


def _integration_artifact_fields(
    *,
    backend: WorkspaceBackend,
    integration_id: str,
    integration_branch: str,
    base_branch: str,
) -> dict[str, str | int | None]:
    fields: dict[str, str | int | None] = {
        "integration_branch": integration_branch,
        "target_branch": base_branch,
        "worktree_id": None,
        "worktree_path": None,
        "clone_id": None,
        "clone_path": None,
        "base_commit_sha": None,
    }
    if backend == "worktree":
        fields["integration_workspace_id"] = integration_id
        fields["integration_clone_id"] = None
    else:
        fields["integration_clone_id"] = integration_id
        fields["integration_workspace_id"] = None
    return fields


def _project_repo_path(db: HubDatabase, project_id: str) -> Path:
    project = LocalProjectManager(db).get(project_id)
    if project is None or not project.repo_path:
        raise BuildWorkspaceError("project repo_path is required for integration workspaces")
    repo_path = Path(project.repo_path)
    if not (repo_path / ".git").exists():
        raise BuildWorkspaceError(f"project repo_path is not a git repository: {repo_path}")
    return repo_path


def _subtree_tasks(db: HubDatabase, root_task_id: str) -> list[Task]:
    rows = db.fetchall(
        """
        WITH RECURSIVE subtree(id, depth, path) AS (
            SELECT id, 0, ARRAY[id] FROM tasks WHERE id = %s
            UNION ALL
            SELECT child.id, parent.depth + 1, parent.path || child.id
              FROM tasks child
              JOIN subtree parent ON child.parent_task_id = parent.id
             WHERE parent.depth < 100
               AND NOT child.id = ANY(parent.path)
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


def _open_ancestor_epics(task_manager: LocalTaskManager, task: Task) -> list[Task]:
    epics: list[Task] = []
    current_id = task.parent_task_id
    while current_id:
        current = _task_by_id(task_manager.db, current_id)
        if current is None:
            break
        if current.task_type == "epic" and current.closed_at is None:
            epics.append(current)
        current_id = current.parent_task_id
    epics.reverse()
    return epics


def _task_by_id(db: HubDatabase, task_id: str) -> Task | None:
    row = db.fetchone("SELECT * FROM tasks WHERE id = %s", (task_id,))
    return Task.from_row(row) if row is not None else None


def _merge_closed_descendant_commits(
    *,
    tasks: list[Task],
    parent_by_id: dict[str, str | None],
    epic_id: str,
    workspace: Worktree | Clone,
    source_repo_path: Path,
) -> None:
    commits = _closed_descendant_commits(
        tasks=tasks,
        parent_by_id=parent_by_id,
        epic_id=epic_id,
    )
    if not commits:
        return
    _merge_required_commits(
        _workspace_record_path(workspace),
        commits=commits,
        source_repo_path=source_repo_path,
    )


def _closed_descendant_commits(
    *,
    tasks: list[Task],
    parent_by_id: dict[str, str | None],
    epic_id: str,
) -> list[tuple[str, str]]:
    commits: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for task in tasks:
        if task.id == epic_id or task.closed_at is None:
            continue
        if not _is_descendant(task.id, epic_id, parent_by_id):
            continue
        if not _should_merge_closed_descendant_commits(task):
            continue
        for commit_sha in _linked_commits(task):
            item = (_task_ref(task), commit_sha)
            if item in seen:
                continue
            seen.add(item)
            commits.append(item)
    return commits


def _should_merge_closed_descendant_commits(task: Task) -> bool:
    if task.allow_automation:
        return True
    labels = set(task.labels or ())
    if task.category == "planning" or any(label.startswith("interactive:") for label in labels):
        return False
    return True


def _linked_commits(task: Task) -> tuple[str, ...]:
    commits: list[str] = []
    seen: set[str] = set()
    task_commits = (task.closed_commit_sha,) if task.closed_commit_sha else (task.commits or ())
    for raw in task_commits:
        if not raw:
            continue
        commit_sha = str(raw)
        if commit_sha in seen:
            continue
        seen.add(commit_sha)
        commits.append(commit_sha)
    return tuple(commits)


def _is_descendant(
    task_id: str,
    ancestor_id: str,
    parent_by_id: dict[str, str | None],
) -> bool:
    current = parent_by_id.get(task_id)
    while current:
        if current == ancestor_id:
            return True
        current = parent_by_id.get(current)
    return False


def _workspace_record_path(workspace: Worktree | Clone) -> Path:
    raw_path = getattr(workspace, "worktree_path", None) or getattr(workspace, "clone_path", None)
    if not raw_path:
        raise BuildWorkspaceError("integration workspace path is missing; clean/restart")
    return Path(str(raw_path))


def _task_ref(task: Task) -> str:
    if task.seq_num:
        return f"#{task.seq_num}"
    return task.id[:8]


def _integration_branch(task: Task) -> str:
    ref = str(task.seq_num or task.id[:8])
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", task.title.lower()).strip("-")[:36]
    return f"gobby/integration/{ref}-{slug or 'epic'}"
