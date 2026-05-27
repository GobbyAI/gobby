"""Artifact cleanup helpers for task-scoped build controls."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from gobby.build.branch_cleanup import default_task_branch_name
from gobby.clones.git import CloneGitManager
from gobby.storage.agents import AgentRun
from gobby.storage.clones import LocalCloneManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.worktrees.git import WorktreeGitManager

ArtifactFamily = Literal["worktree", "clone"]


@dataclass
class BuildArtifactSummary:
    """Build artifact considered or removed by a clean operation."""

    family: ArtifactFamily
    task_id: str | None
    path: str
    artifact_id: str | None = None
    source: str = "tracked"
    orphan: bool = False
    exists: bool = False
    deleted: bool = False
    deferred: bool = False
    error: str | None = None


def defer_active_agent_artifacts(
    artifacts: list[BuildArtifactSummary],
    agents: list[AgentRun],
) -> list[BuildArtifactSummary]:
    active_worktree_ids = {run.worktree_id for run in agents if run.worktree_id}
    active_clone_ids = {run.clone_id for run in agents if run.clone_id}
    artifacts_to_delete: list[BuildArtifactSummary] = []

    for artifact in artifacts:
        if artifact.family == "worktree" and artifact.artifact_id in active_worktree_ids:
            artifact.deferred = True
            continue
        if artifact.family == "clone" and artifact.artifact_id in active_clone_ids:
            artifact.deferred = True
            continue
        artifacts_to_delete.append(artifact)

    return artifacts_to_delete


def defer_dirty_descendant_worktree_artifacts(
    artifacts: list[BuildArtifactSummary],
    *,
    root_task_id: str,
    project_path: Path,
) -> list[BuildArtifactSummary]:
    worktree_git = WorktreeGitManager(project_path)
    artifacts_to_delete: list[BuildArtifactSummary] = []

    for artifact in artifacts:
        if (
            artifact.family != "worktree"
            or artifact.task_id in {None, root_task_id}
            or artifact.deferred
        ):
            artifacts_to_delete.append(artifact)
            continue
        if not Path(artifact.path).exists():
            artifacts_to_delete.append(artifact)
            continue
        status = worktree_git.get_worktree_status(artifact.path)
        if status is not None and (
            status.has_uncommitted_changes
            or status.has_staged_changes
            or status.has_untracked_files
        ):
            artifact.deferred = True
            continue
        artifacts_to_delete.append(artifact)

    return artifacts_to_delete


def collect_clean_artifacts(
    db: HubDatabase,
    project_id: str,
    tasks: list[Task],
) -> list[BuildArtifactSummary]:
    worktrees = LocalWorktreeManager(db)
    clones = LocalCloneManager(db)
    project_path = get_project_path(db, project_id)
    project_name = project_path.name
    summaries: list[BuildArtifactSummary] = []
    seen: set[tuple[str, str]] = set()

    for task in tasks:
        artifacts = LocalTaskManager(db).artifacts.get_artifacts(task.id)
        _append_artifact(
            summaries,
            seen,
            family="worktree",
            task_id=task.id,
            path=artifacts.worktree_path,
            artifact_id=artifacts.worktree_id,
            source="task_artifacts",
        )
        _append_artifact(
            summaries,
            seen,
            family="clone",
            task_id=task.id,
            path=artifacts.clone_path,
            artifact_id=artifacts.clone_id,
            source="task_artifacts",
        )
        if artifacts.integration_workspace_id:
            integration_worktree = worktrees.get(artifacts.integration_workspace_id)
            _append_artifact(
                summaries,
                seen,
                family="worktree",
                task_id=task.id,
                path=integration_worktree.worktree_path
                if integration_worktree is not None
                else _expected_integration_path(
                    family="worktree",
                    project_name=project_name,
                    branch_name=artifacts.integration_branch,
                    artifact_id=artifacts.integration_workspace_id,
                ),
                artifact_id=artifacts.integration_workspace_id,
                source="task_artifacts_integration",
            )
        if artifacts.integration_clone_id:
            integration_clone = clones.get(artifacts.integration_clone_id)
            _append_artifact(
                summaries,
                seen,
                family="clone",
                task_id=task.id,
                path=integration_clone.clone_path
                if integration_clone is not None
                else _expected_integration_path(
                    family="clone",
                    project_name=project_name,
                    branch_name=artifacts.integration_branch,
                    artifact_id=artifacts.integration_clone_id,
                ),
                artifact_id=artifacts.integration_clone_id,
                source="task_artifacts_integration",
            )

        worktree = worktrees.get_by_task(task.id)
        if worktree is not None:
            _append_artifact(
                summaries,
                seen,
                family="worktree",
                task_id=task.id,
                path=worktree.worktree_path,
                artifact_id=worktree.id,
                source="worktrees_integration"
                if worktree.workspace_role == "integration"
                else "worktrees",
            )
        clone = clones.get_by_task(task.id)
        if clone is not None:
            _append_artifact(
                summaries,
                seen,
                family="clone",
                task_id=task.id,
                path=clone.clone_path,
                artifact_id=clone.id,
                source="clones_integration" if clone.workspace_role == "integration" else "clones",
            )

    summaries.extend(_detect_orphan_artifacts(db, project_id, tasks, seen))
    return summaries


def delete_artifacts(
    db: HubDatabase,
    project_id: str,
    artifacts: list[BuildArtifactSummary],
    *,
    force: bool,
) -> None:
    project_path = get_project_path(db, project_id)
    worktree_git = WorktreeGitManager(project_path)
    clone_git = CloneGitManager(project_path)
    worktrees = LocalWorktreeManager(db)
    clones = LocalCloneManager(db)
    task_manager = LocalTaskManager(db)

    for artifact in artifacts:
        try:
            path = Path(artifact.path)
            if artifact.family == "worktree":
                if path.exists():
                    worktree_result = worktree_git.delete_worktree(path, force=force)
                    if not worktree_result.success and path.exists():
                        artifact.error = worktree_result.error or worktree_result.message
                        continue
                    if not worktree_result.success:
                        prune = getattr(worktree_git, "prune_worktrees", None)
                        if callable(prune):
                            prune()
                if artifact.artifact_id:
                    worktrees.delete(artifact.artifact_id)
            else:
                if path.exists():
                    clone_result = clone_git.delete_clone(path, force=force)
                    if not clone_result.success:
                        artifact.error = clone_result.error or clone_result.message
                        continue
                if artifact.artifact_id:
                    clones.delete(artifact.artifact_id)

            if artifact.task_id is not None and artifact.source.endswith("_integration"):
                task_manager.artifacts.set_artifacts_atomic(
                    artifact.task_id,
                    integration_branch=None,
                    integration_workspace_id=None,
                    integration_clone_id=None,
                )
            elif artifact.task_id is not None and not artifact.orphan:
                task_manager.artifacts.clear_isolation_pair(artifact.task_id, artifact.family)
            artifact.exists = False
            artifact.deleted = True
        except Exception as exc:
            artifact.error = str(exc)


def get_project_path(db: HubDatabase, project_id: str) -> Path:
    project = LocalProjectManager(db).get(project_id)
    if project is not None and project.repo_path:
        return Path(project.repo_path)
    return Path.cwd()


def _append_artifact(
    summaries: list[BuildArtifactSummary],
    seen: set[tuple[str, str]],
    *,
    family: ArtifactFamily,
    task_id: str | None,
    path: str | None,
    artifact_id: str | None,
    source: str,
) -> None:
    if not path:
        return
    expanded_path = Path(path).expanduser()
    key = (family, str(expanded_path))
    if key in seen:
        return
    seen.add(key)
    summaries.append(
        BuildArtifactSummary(
            family=family,
            task_id=task_id,
            path=str(expanded_path),
            artifact_id=artifact_id,
            source=source,
            exists=expanded_path.exists(),
        )
    )


def _expected_integration_path(
    *,
    family: ArtifactFamily,
    project_name: str,
    branch_name: str | None,
    artifact_id: str,
) -> str:
    if branch_name:
        safe_branch = branch_name.replace("/", "-").replace("\\", "-")
        root = "worktrees" if family == "worktree" else "clones"
        return str(Path.home() / ".gobby" / root / project_name / safe_branch)
    return f"<missing-{family}-{artifact_id}>"


def _detect_orphan_artifacts(
    db: HubDatabase,
    project_id: str,
    tasks: list[Task],
    seen: set[tuple[str, str]],
) -> list[BuildArtifactSummary]:
    project_path = get_project_path(db, project_id)
    project_name = project_path.name
    roots: dict[ArtifactFamily, Path] = {
        "worktree": Path.home() / ".gobby" / "worktrees" / project_name,
        "clone": Path.home() / ".gobby" / "clones" / project_name,
    }
    orphan_summaries: list[BuildArtifactSummary] = []

    for task in tasks:
        if not task.seq_num:
            continue
        prefix = f"task-{task.seq_num}-"
        expected = default_task_branch_name(task)
        for family, root in roots.items():
            if not root.exists() or not root.is_dir():
                continue
            for candidate in root.iterdir():
                if candidate.name != expected and not candidate.name.startswith(prefix):
                    continue
                key = (family, str(candidate))
                if key in seen:
                    continue
                seen.add(key)
                orphan_summaries.append(
                    BuildArtifactSummary(
                        family=family,
                        task_id=task.id,
                        path=str(candidate),
                        source="orphan",
                        orphan=True,
                        exists=candidate.exists(),
                    )
                )
    return orphan_summaries


__all__ = [
    "BuildArtifactSummary",
    "collect_clean_artifacts",
    "defer_active_agent_artifacts",
    "defer_dirty_descendant_worktree_artifacts",
    "delete_artifacts",
    "get_project_path",
]
