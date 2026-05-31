"""Artifact cleanup helpers for task-scoped build controls."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
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
    cleanup_reason: str | None = None
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
            artifact.cleanup_reason = "active_agent_deferred"
            continue
        if artifact.family == "clone" and artifact.artifact_id in active_clone_ids:
            artifact.deferred = True
            artifact.cleanup_reason = "active_agent_deferred"
            continue
        artifacts_to_delete.append(artifact)

    return artifacts_to_delete


def classify_dirty_descendant_worktree_artifacts(
    db: HubDatabase,
    artifacts: list[BuildArtifactSummary],
    *,
    root: Task,
    tasks: list[Task],
    project_path: Path,
) -> list[BuildArtifactSummary]:
    worktree_git = WorktreeGitManager(project_path)
    task_manager = LocalTaskManager(db)
    tasks_by_id = {task.id: task for task in tasks}
    target_ref = _root_cleanup_target(root, task_manager.artifacts.get_artifacts(root.id))
    artifacts_to_delete: list[BuildArtifactSummary] = []

    for artifact in artifacts:
        task_id = artifact.task_id
        if artifact.deferred:
            continue
        if artifact.family != "worktree" or task_id in {None, root.id}:
            artifacts_to_delete.append(artifact)
            continue
        if not Path(artifact.path).exists():
            artifacts_to_delete.append(artifact)
            continue
        porcelain = _git_text_or_none(
            worktree_git,
            ["status", "--porcelain", "--untracked-files=all"],
            cwd=artifact.path,
        )
        if porcelain is None:
            artifact.deferred = True
            artifact.cleanup_reason = "worktree_status_unknown_deferred"
            continue
        if porcelain:
            if task_id is None:
                artifacts_to_delete.append(artifact)
                continue
            task = tasks_by_id.get(task_id)
            if task is None or task.closed_at is None or task.is_escalated:
                artifact.deferred = True
                artifact.cleanup_reason = "dirty_open_task_deferred"
                continue
            head = _git_text(worktree_git, ["rev-parse", "HEAD"], cwd=artifact.path)
            if not head or task.closed_commit_sha != head:
                artifact.deferred = True
                artifact.cleanup_reason = "dirty_closed_commit_mismatch_deferred"
                continue
            if target_ref and head and _is_ancestor(worktree_git, head, target_ref):
                evidence = _dirty_worktree_evidence(
                    worktree_git,
                    artifact=artifact,
                    head=head,
                    target_ref=target_ref,
                )
                _append_system_task_comment_once(db, task.id, evidence)
                artifact.cleanup_reason = "dirty_closed_integrated_cleaned"
                artifacts_to_delete.append(artifact)
                continue
            artifact.deferred = True
            artifact.cleanup_reason = "dirty_closed_unintegrated_deferred"
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
            if artifact.deferred:
                continue
            path = Path(artifact.path)
            if artifact.family == "worktree":
                worktree_id = artifact.artifact_id
                if worktree_id is None:
                    worktree = worktrees.get_by_path(str(path))
                    worktree_id = worktree.id if worktree is not None else None
                stored_worktree = worktrees.get(worktree_id) if worktree_id is not None else None
                if stored_worktree is not None:
                    stored_path = Path(stored_worktree.worktree_path)
                    if stored_path.exists():
                        path = stored_path
                if path.exists():
                    porcelain = _git_text_or_none(
                        worktree_git,
                        ["status", "--porcelain", "--untracked-files=all"],
                        cwd=path,
                    )
                    if porcelain is None:
                        artifact.deferred = True
                        artifact.cleanup_reason = "worktree_status_unknown_deferred"
                        continue
                    if porcelain and artifact.cleanup_reason != "dirty_closed_integrated_cleaned":
                        task = (
                            task_manager.get_task(artifact.task_id, project_id=project_id)
                            if artifact.task_id is not None
                            else None
                        )
                        artifact.deferred = True
                        if task is None or task.closed_at is None or task.is_escalated:
                            artifact.cleanup_reason = "dirty_open_task_deferred"
                        else:
                            artifact.cleanup_reason = "dirty_closed_unclassified_deferred"
                        continue
                    worktree_result = worktree_git.delete_worktree(path, force=force)
                    if not worktree_result.success and path.exists():
                        artifact.error = worktree_result.error or worktree_result.message
                        continue
                    if not worktree_result.success:
                        prune = getattr(worktree_git, "prune_worktrees", None)
                        if callable(prune):
                            prune()
                if worktree_id:
                    worktrees.delete(worktree_id)
                    task_manager.artifacts.clear_worktree_references(worktree_id)
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


def _root_cleanup_target(root: Task, artifacts: object) -> str | None:
    for value in (
        getattr(artifacts, "integration_branch", None),
        getattr(artifacts, "target_branch", None),
        root.closed_commit_sha,
    ):
        if value:
            return str(value)
    return None


def _is_ancestor(worktree_git: WorktreeGitManager, head: str, target_ref: str) -> bool:
    result = worktree_git.run_git_command(
        ["merge-base", "--is-ancestor", head, target_ref],
        timeout=10,
    )
    return result.returncode == 0


def _dirty_worktree_evidence(
    worktree_git: WorktreeGitManager,
    *,
    artifact: BuildArtifactSummary,
    head: str,
    target_ref: str,
) -> str:
    path = artifact.path
    branch = _git_text(worktree_git, ["branch", "--show-current"], cwd=path) or "(detached)"
    porcelain = _git_text(
        worktree_git,
        ["status", "--porcelain", "--untracked-files=all"],
        cwd=path,
    )
    cached_stat = _git_text(worktree_git, ["diff", "--cached", "--stat"], cwd=path)
    unstaged_stat = _git_text(worktree_git, ["diff", "--stat"], cwd=path)
    return "\n".join(
        [
            "## Closed Dirty Worktree Cleanup",
            "",
            "Closed task worktree was removed because its HEAD is already integrated.",
            "",
            f"Path: {path}",
            f"Branch: {branch}",
            f"HEAD: {head}",
            f"Target: {target_ref}",
            "",
            "Porcelain status:",
            _bounded_block(porcelain),
            "",
            "Cached diff stat:",
            _bounded_block(cached_stat),
            "",
            "Unstaged diff stat:",
            _bounded_block(unstaged_stat),
            "",
            "Untracked files:",
            _bounded_block("\n".join(_untracked_names(porcelain))),
        ]
    )


def _append_system_task_comment_once(db: HubDatabase, task_id: str, body: str) -> None:
    existing = db.fetchone(
        """
        SELECT id
          FROM task_comments
         WHERE task_id = %s
           AND author_type = 'system'
           AND body = %s
         LIMIT 1
        """,
        (task_id, body),
    )
    if existing is not None:
        return
    now = datetime.now(UTC).isoformat()
    db.execute(
        """
        INSERT INTO task_comments (
            id, task_id, parent_comment_id, author, author_type, body, created_at, updated_at
        )
        VALUES (%s, %s, NULL, 'build-cleanup', 'system', %s, %s, %s)
        """,
        (str(uuid.uuid4()), task_id, body, now, now),
    )


def _git_text(
    worktree_git: WorktreeGitManager,
    args: list[str],
    *,
    cwd: str | Path | None = None,
    timeout: int = 10,
) -> str:
    result = worktree_git.run_git_command(args, cwd=cwd, timeout=timeout)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _git_text_or_none(
    worktree_git: WorktreeGitManager,
    args: list[str],
    *,
    cwd: str | Path | None = None,
    timeout: int = 10,
) -> str | None:
    result = worktree_git.run_git_command(args, cwd=cwd, timeout=timeout)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _bounded_block(value: str, *, max_lines: int = 40, max_chars: int = 2000) -> str:
    if not value:
        return "(none)"
    lines = value.splitlines()
    truncated = len(lines) > max_lines or len(value) > max_chars
    bounded = "\n".join(lines[:max_lines])[:max_chars]
    if truncated:
        return f"{bounded}\n... truncated ..."
    return bounded


def _untracked_names(porcelain: str, *, limit: int = 20) -> list[str]:
    names = [
        line[3:] for line in porcelain.splitlines() if line.startswith("?? ") and len(line) > 3
    ]
    if len(names) > limit:
        return [*names[:limit], "... truncated ..."]
    return names


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
    "classify_dirty_descendant_worktree_artifacts",
    "collect_clean_artifacts",
    "defer_active_agent_artifacts",
    "delete_artifacts",
    "get_project_path",
]
