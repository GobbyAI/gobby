"""Workspace and task-artifact handling for dispatcher spawns."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from gobby.build.workspaces import (
    BuildWorkspaceError,
    ensure_epic_integration_workspaces,
    ensure_task_parent_integration_workspace,
)
from gobby.dispatch.actions import SpawnAgentAction
from gobby.dispatch.spawn_errors import DispatchSpawnFailed
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._artifacts import (
    TaskArtifactManager,
    TaskArtifacts,
)
from gobby.storage.tasks._artifacts import set_artifacts_atomic as _set_artifacts_atomic
from gobby.tasks.state_semantics import current_stage, is_task_merge_ready

if TYPE_CHECKING:
    from gobby.storage.tasks import LocalTaskManager, Task

logger = logging.getLogger(__name__)

_EXPLICIT_AGENT_ISOLATIONS = {"none", "worktree", "clone"}
_PRE_DEVELOPMENT_ISOLATION_STAGES = {
    "ideation",
    "research",
    "architecture",
    "prd",
    "planning",
    "expansion",
}
_DEVELOPMENT_FORWARD_ISOLATION_STAGES = {"development", "epic_qa", "pr", "merge"}
# Taskless plan review and enhancement coordinate the caller's current plan
# workflow and have no build task isolation to inherit.
_TASKLESS_MAIN_CONTEXT_AGENT_SLUGS = frozenset(
    {"plan-adversary-taskless", "plan-enhancer-taskless"}
)

SpawnIsolation = Literal["none", "worktree", "clone"]

__all__ = [
    "SpawnIsolation",
    "_artifact_ref_resolves",
    "_artifact_ref_sha",
    "_effective_spawn_isolation",
    "_field",
    "_guard_merge_ready_leaf_branch",
    "_persist_spawn_artifacts",
    "_prepare_spawn_artifacts",
    "_project_clone_manager",
    "_project_git_manager",
    "_repair_leaf_target_branch",
    "_sanitize_reusable_spawn_artifacts",
    "_spawn_workspace_ids",
    "ensure_epic_integration_workspaces",
]


def _prepare_spawn_artifacts(
    *,
    db: HubDatabase,
    action: SpawnAgentAction,
    task: Task,
    task_manager: LocalTaskManager,
    project_id: str,
    services: object | None,
    isolation: SpawnIsolation | None,
) -> TaskArtifacts:
    artifacts = TaskArtifactManager(db).get_artifacts(action.task_id)
    if isolation not in {"worktree", "clone"}:
        return artifacts

    backend = cast(Literal["worktree", "clone"], isolation)
    if task.task_type != "epic":
        try:
            ensure_task_parent_integration_workspace(
                task_manager=task_manager,
                task=task,
                backend=backend,
                project_id=project_id,
                services=services,
            )
        except BuildWorkspaceError as exc:
            raise DispatchSpawnFailed(str(exc)) from exc
        return TaskArtifactManager(db).get_artifacts(action.task_id)

    if not _uses_epic_integration_workspace(task, action):
        return artifacts

    try:
        if not artifacts.target_branch:
            raise BuildWorkspaceError(
                f"target_branch is required for epic integration workspace #{task.seq_num}"
            )
        ensure_epic_integration_workspaces(
            task_manager=task_manager,
            root_task=task,
            backend=backend,
            target_branch=artifacts.target_branch,
            project_id=project_id,
            services=services,
            merge_closed_descendant_commits=True,
            repair_only=True,
        )
    except BuildWorkspaceError as exc:
        raise DispatchSpawnFailed(
            str(exc),
            stage_failure_cited_subtasks=_epic_workspace_failure_cited_subtasks(
                action=action,
                task=task,
                task_manager=task_manager,
            ),
        ) from exc
    return TaskArtifactManager(db).get_artifacts(action.task_id)


def _epic_workspace_failure_cited_subtasks(
    *,
    action: SpawnAgentAction,
    task: Task,
    task_manager: LocalTaskManager,
) -> tuple[str, ...]:
    if action.agent_slug != "epic-reviewer":
        return ()
    initial_variables = action.initial_variables or {}
    if initial_variables.get("stage_name") != "epic_qa":
        return ()
    if task.task_type != "epic":
        return ()
    children = task_manager.list_tasks(
        project_id=task.project_id,
        parent_task_id=task.id,
        closed=True,
        limit=1000,
    )
    return tuple(child.id for child in children if child.allow_automation)


def _sanitize_reusable_spawn_artifacts(
    *,
    db: HubDatabase,
    task: Task,
    artifacts: TaskArtifacts,
    services: object | None,
    isolation: SpawnIsolation | None,
) -> TaskArtifacts:
    """Clear stale task workspace pointers before passing explicit reuse IDs to spawn."""
    fields: dict[str, str | int | None] = {}
    if isolation == "worktree" and artifacts.worktree_id:
        if _worktree_artifact_is_stale(
            db=db,
            task=task,
            worktree_id=artifacts.worktree_id,
            services=services,
        ):
            fields.update(
                {
                    "worktree_id": None,
                    "worktree_path": None,
                    "base_commit_sha": None,
                }
            )
    elif isolation == "clone" and artifacts.clone_id:
        if _clone_artifact_is_stale(
            db=db,
            task=task,
            clone_id=artifacts.clone_id,
            services=services,
        ):
            fields.update(
                {
                    "clone_id": None,
                    "clone_path": None,
                    "base_commit_sha": None,
                }
            )

    if not fields:
        return artifacts
    TaskArtifactManager(db).set_artifacts_atomic(task.id, **fields)
    return TaskArtifactManager(db).get_artifacts(task.id)


def _repair_leaf_target_branch(
    *,
    db: HubDatabase,
    task: Task,
    task_manager: LocalTaskManager,
    project_id: str,
    services: object | None,
    artifacts: TaskArtifacts,
    isolation: SpawnIsolation | None,
) -> TaskArtifacts:
    if isolation not in {"worktree", "clone"} or not task.parent_task_id:
        return artifacts
    if task.task_type == "epic" or artifacts.worktree_id or artifacts.clone_id:
        return artifacts

    target_branch = _nearest_parent_integration_or_target(
        task_manager,
        task,
        project_id=project_id,
        services=services,
    )
    if not target_branch or artifacts.target_branch == target_branch:
        return artifacts

    TaskArtifactManager(db).set_artifacts_atomic(task.id, target_branch=target_branch)
    return TaskArtifactManager(db).get_artifacts(task.id)


def _guard_merge_ready_leaf_branch(
    *,
    db: HubDatabase,
    action: SpawnAgentAction,
    task: Task,
    project_id: str,
    services: object | None,
    artifacts: TaskArtifacts,
    isolation: SpawnIsolation | None,
) -> None:
    if isolation not in {"worktree", "clone"}:
        return
    if _uses_epic_integration_workspace(task, action):
        return
    if not is_task_merge_ready(task):
        return
    stage = current_stage(task)
    stage_name = _field(stage, "name", None) or _field(stage, "stage_name", None)
    if stage_name not in _DEVELOPMENT_FORWARD_ISOLATION_STAGES:
        return
    if isolation == "worktree" and artifacts.worktree_id:
        return
    if isolation == "clone" and artifacts.clone_id:
        return
    if not artifacts.target_branch:
        return

    branch_name = _generated_task_branch_name(
        task=task,
        project_id=project_id,
        base_branch=artifacts.target_branch,
    )
    target_inspected, target_sha = _artifact_ref_sha(
        db=db,
        project_id=project_id,
        services=services,
        ref_name=artifacts.target_branch,
    )
    if not target_inspected or not target_sha:
        return
    branch_inspected, branch_sha = _artifact_ref_sha(
        db=db,
        project_id=project_id,
        services=services,
        ref_name=branch_name,
    )
    if not branch_inspected:
        return
    if branch_sha is None:
        raise DispatchSpawnFailed(f"merge_ready_task_branch_missing:{branch_name}")
    if branch_sha == target_sha:
        raise DispatchSpawnFailed(
            f"merge_ready_task_branch_matches_target:{branch_name}:{artifacts.target_branch}"
        )


def _generated_task_branch_name(*, task: Task, project_id: str, base_branch: str) -> str:
    from gobby.agents.isolation import SpawnConfig, generate_branch_name

    seq_num = getattr(task, "seq_num", None)
    title = getattr(task, "title", None)
    return generate_branch_name(
        SpawnConfig(
            prompt="",
            task_id=getattr(task, "id", None),
            task_title=title if isinstance(title, str) else None,
            task_seq_num=seq_num if isinstance(seq_num, int) else None,
            branch_name=None,
            branch_prefix=None,
            base_branch=base_branch,
            project_id=project_id,
            project_path="",
            provider="",
            parent_session_id="",
        )
    )


def _worktree_artifact_is_stale(
    *,
    db: HubDatabase,
    task: Task,
    worktree_id: str,
    services: object | None,
) -> bool:
    from gobby.storage.worktrees import LocalWorktreeManager

    storage = cast(
        LocalWorktreeManager,
        getattr(services, "worktree_storage", None) or LocalWorktreeManager(db),
    )
    worktree = storage.get(worktree_id)
    if worktree is None:
        return True
    if worktree.task_id != task.id:
        return True
    if not Path(worktree.worktree_path).is_dir():
        storage.delete(worktree.id)
        return True
    return False


def _clone_artifact_is_stale(
    *,
    db: HubDatabase,
    task: Task,
    clone_id: str,
    services: object | None,
) -> bool:
    from gobby.storage.clones import LocalCloneManager

    storage = cast(
        LocalCloneManager,
        getattr(services, "clone_storage", None) or LocalCloneManager(db),
    )
    clone = storage.get(clone_id)
    if clone is None:
        return True
    if clone.task_id != task.id:
        return True
    if not Path(clone.clone_path).is_dir():
        storage.delete(clone.id)
        return True
    return False


def _nearest_parent_integration_or_target(
    task_manager: LocalTaskManager,
    task: Task,
    *,
    project_id: str | None = None,
    services: object | None = None,
) -> str | None:
    target_fallback: str | None = None
    current_id = task.parent_task_id
    while current_id:
        try:
            parent = task_manager.get_task(current_id)
        except ValueError:
            return None
        if parent is None:
            return None
        parent_artifacts = TaskArtifactManager(task_manager.db).get_artifacts(parent.id)
        if parent_artifacts.integration_branch and _artifact_ref_resolves(
            db=task_manager.db,
            project_id=project_id,
            services=services,
            ref_name=parent_artifacts.integration_branch,
        ):
            return parent_artifacts.integration_branch
        if (
            target_fallback is None
            and parent_artifacts.target_branch
            and _artifact_ref_resolves(
                db=task_manager.db,
                project_id=project_id,
                services=services,
                ref_name=parent_artifacts.target_branch,
            )
        ):
            target_fallback = parent_artifacts.target_branch
        current_id = parent.parent_task_id
    return target_fallback


def _artifact_ref_resolves(
    *,
    db: HubDatabase,
    project_id: str | None,
    services: object | None,
    ref_name: str,
) -> bool:
    """Return false only when the project git repo proves the artifact ref is stale."""
    if not ref_name:
        return False

    git_manager = _service_git_manager(services, project_id) if project_id else None
    if git_manager is None:
        from gobby.worktrees.git import WorktreeGitManager

        if project_id is None:
            return True
        repo_path = _checkout_root(db, project_id)
        if not (repo_path / ".git").exists():
            return True
        git_manager = WorktreeGitManager(repo_path)

    runner = getattr(git_manager, "_run_git", None)
    if runner is None:
        return True

    for candidate in (ref_name, f"origin/{ref_name}"):
        try:
            result = runner(["rev-parse", "--verify", candidate], timeout=10)
        except Exception:
            return True
        if getattr(result, "returncode", 1) == 0:
            return True
    return False


def _artifact_ref_sha(
    *,
    db: HubDatabase,
    project_id: str | None,
    services: object | None,
    ref_name: str,
) -> tuple[bool, str | None]:
    """Return (inspected, sha), where sha None means the ref was missing."""
    if not ref_name:
        return True, None

    git_manager = _service_git_manager(services, project_id) if project_id else None
    if git_manager is None:
        from gobby.worktrees.git import WorktreeGitManager

        if project_id is None:
            return False, None
        repo_path = _checkout_root(db, project_id)
        if not (repo_path / ".git").exists():
            return False, None
        git_manager = WorktreeGitManager(repo_path)

    runner = getattr(git_manager, "_run_git", None)
    if runner is None:
        return False, None

    for candidate in (ref_name, f"origin/{ref_name}"):
        try:
            result = runner(["rev-parse", "--verify", f"{candidate}^{{commit}}"], timeout=10)
        except Exception:
            return False, None
        if getattr(result, "returncode", 1) == 0:
            stdout = getattr(result, "stdout", "")
            if isinstance(stdout, str):
                sha = stdout.strip()
                if sha:
                    return True, sha
    return True, None


def _spawn_workspace_ids(
    *,
    task: object,
    action: SpawnAgentAction,
    artifacts: TaskArtifacts,
    isolation: SpawnIsolation | None,
) -> tuple[str | None, str | None]:
    if _uses_epic_integration_workspace(task, action):
        if isolation == "worktree" and artifacts.integration_workspace_id:
            return artifacts.integration_workspace_id, None
        if isolation == "clone" and artifacts.integration_clone_id:
            return None, artifacts.integration_clone_id
    if isolation == "worktree":
        return artifacts.worktree_id, None
    if isolation == "clone":
        return None, artifacts.clone_id
    return None, None


def _effective_spawn_isolation(
    *,
    task: object,
    action: SpawnAgentAction,
    agent_body: object | None,
) -> SpawnIsolation | None:
    if action.agent_slug in _TASKLESS_MAIN_CONTEXT_AGENT_SLUGS:
        return "none"
    stage_name = _spawn_stage_name(action)
    task_isolation = _task_spawn_isolation(task)
    if stage_name in _PRE_DEVELOPMENT_ISOLATION_STAGES:
        return "none"

    agent_isolation = getattr(agent_body, "isolation", None)
    if stage_name in _DEVELOPMENT_FORWARD_ISOLATION_STAGES:
        if task_isolation is not None:
            return task_isolation
        if agent_isolation in _EXPLICIT_AGENT_ISOLATIONS:
            return cast(SpawnIsolation, agent_isolation)
        return None

    if agent_isolation in _EXPLICIT_AGENT_ISOLATIONS:
        return cast(SpawnIsolation, agent_isolation)
    return task_isolation


def _task_spawn_isolation(task: object) -> SpawnIsolation | None:
    task_isolation = getattr(task, "isolation", None)
    if task_isolation in _EXPLICIT_AGENT_ISOLATIONS:
        return cast(SpawnIsolation, task_isolation)
    return None


def _spawn_stage_name(action: SpawnAgentAction) -> str | None:
    stage_name = (action.initial_variables or {}).get("stage_name")
    return str(stage_name) if isinstance(stage_name, str) and stage_name else None


def _uses_epic_integration_workspace(task: object, action: SpawnAgentAction) -> bool:
    if action.agent_slug != "epic-reviewer":
        return False
    if getattr(task, "task_type", None) != "epic":
        return False
    stage_name = (action.initial_variables or {}).get("stage_name")
    return stage_name in {None, "epic_qa"}


def _checkout_root(db: HubDatabase, project_id: str) -> Path:
    from gobby.storage.project_checkouts import require_root
    from gobby.storage.workspace_machine_scope import require_local_machine_id

    machine_id = require_local_machine_id(
        None, resource_kind="project_checkout", resource_id=project_id
    )
    return Path(require_root(db, project_id, machine_id))


def _service_git_manager(services: object | None, project_id: str) -> object | None:
    getter = getattr(services, "get_git_manager", None)
    if callable(getter):
        try:
            return cast(object | None, getter(project_id))
        except (LookupError, OSError, RuntimeError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Failed to resolve dispatch git manager for project {project_id}"
            ) from exc
    return None


def _project_git_manager(services: object | None, project_id: str) -> object | None:
    """Return the project-scoped git manager, falling back to the container default."""
    return _service_git_manager(services, project_id) or getattr(services, "git_manager", None)


def _service_clone_manager(services: object | None, project_id: str) -> object | None:
    git_manager = _service_git_manager(services, project_id)
    repo_path = getattr(git_manager, "repo_path", None)
    if repo_path is None:
        return None
    try:
        from gobby.clones.git import CloneGitManager

        return cast(object, CloneGitManager(repo_path))
    except (TypeError, ValueError, OSError, RuntimeError):
        return None


def _project_clone_manager(services: object | None, project_id: str) -> object | None:
    """Return the project-scoped clone manager, falling back to the container default."""
    return _service_clone_manager(services, project_id) or getattr(services, "clone_manager", None)


def _persist_spawn_artifacts(
    db: HubDatabase,
    task_id: str,
    result: Mapping[str, object],
) -> None:
    fields: dict[str, str | int | None] = {}
    try:
        artifacts = TaskArtifactManager(db).get_artifacts(task_id)
        worktree_id = result.get("worktree_id")
        worktree_path = result.get("worktree_path")
        clone_id = result.get("clone_id")
        base_commit_sha = result.get("base_commit_sha")
        if (
            isinstance(worktree_id, str)
            and isinstance(worktree_path, str)
            and worktree_id != artifacts.integration_workspace_id
        ):
            fields["worktree_id"] = worktree_id
            fields["worktree_path"] = worktree_path
        clone_path = result.get("clone_path")
        if (
            isinstance(clone_id, str)
            and isinstance(clone_path, str)
            and clone_id != artifacts.integration_clone_id
        ):
            fields["clone_id"] = clone_id
            fields["clone_path"] = clone_path
        if fields and isinstance(base_commit_sha, str) and base_commit_sha:
            fields["base_commit_sha"] = base_commit_sha
        elif (
            not fields
            and isinstance(base_commit_sha, str)
            and base_commit_sha
            and (artifacts.worktree_path or artifacts.clone_path)
        ):
            fields["base_commit_sha"] = base_commit_sha
        if fields:
            _set_artifacts_atomic(db, task_id, **fields)
    except Exception as exc:
        logger.exception(
            "Failed to persist dispatcher spawn artifacts",
            extra={"task_id": task_id, "fields": fields},
        )
        raise DispatchSpawnFailed("artifact_persistence_failed") from exc


def _field(
    obj: object | None,
    name: str,
    default: object | None = None,
) -> object | None:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return cast(object | None, obj.get(name, default))
    return cast(object | None, getattr(obj, name, default))
