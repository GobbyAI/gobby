"""Daemon-backed spawn boundary for dispatcher actions."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import psycopg

from gobby.agents.completion_subscribers import subscribe_agent_completion
from gobby.build.coordinator import summary_allows_cross_project_coordinator
from gobby.build.workspaces import BuildWorkspaceError, ensure_epic_integration_workspaces
from gobby.dispatch.actions import SpawnAgentAction
from gobby.storage.build_history import BuildHistoryStorage
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._artifacts import (
    TaskArtifactConstraintError,
    TaskArtifactManager,
    TaskArtifacts,
)
from gobby.storage.tasks._artifacts import set_artifacts_atomic as _set_artifacts_atomic

if TYPE_CHECKING:
    from gobby.agents.runner import AgentRunner
    from gobby.storage.sessions import SessionManager
    from gobby.storage.tasks import LocalTaskManager, Task

logger = logging.getLogger(__name__)

MAX_DISPATCH_SPAWN_ATTEMPTS = 3
_EXPLICIT_AGENT_ISOLATIONS = {"none", "worktree", "clone"}
_PRE_DEVELOPMENT_ISOLATION_STAGES = {
    "ideation",
    "research",
    "architecture",
    "prd",
    "planning",
    "expansion",
}
_DEVELOPMENT_FORWARD_ISOLATION_STAGES = {"development", "holistic_qa", "pr", "merge"}
# Taskless plan review coordinates the caller's current plan workflow and has no
# build task isolation to inherit.
_TASKLESS_MAIN_CONTEXT_AGENT_SLUGS = frozenset({"plan-adversary-taskless"})

SpawnIsolation = Literal["none", "worktree", "clone"]


class DispatchSpawnUnavailable(RuntimeError):
    """Raised when dispatcher lacks the daemon services needed to spawn."""


class DispatchSpawnFailed(RuntimeError):
    """Raised when the daemon spawn path returns an unsuccessful result."""

    def __init__(
        self,
        message: str,
        *,
        stage_failure_cited_subtasks: Sequence[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.stage_failure_cited_subtasks = tuple(stage_failure_cited_subtasks or ())


async def spawn_agent(
    action: SpawnAgentAction,
    *,
    db: HubDatabase | None = None,
    context: object | None = None,
    services: object | None = None,
) -> str:
    """Spawn an agent through daemon services and return its real agent run id."""
    if db is None:
        raise DispatchSpawnUnavailable("database_missing")
    from gobby.agents.readiness import spawn_readiness_blocker

    readiness_reason = spawn_readiness_blocker(services)
    if readiness_reason is not None:
        logger.info("Dispatcher spawn skipped", extra={"readiness_reason": readiness_reason})
        raise DispatchSpawnUnavailable(readiness_reason)
    required = {
        "database": getattr(services, "database", None),
        "task_manager": getattr(services, "task_manager", None),
        "session_manager": getattr(services, "session_manager", None),
        "agent_runner": getattr(services, "agent_runner", None),
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise DispatchSpawnUnavailable(f"services_missing:{','.join(missing)}")

    task_manager = cast("LocalTaskManager", required["task_manager"])
    session_manager = cast("SessionManager", required["session_manager"])
    runner = cast("AgentRunner", required["agent_runner"])
    try:
        task = task_manager.get_task(action.task_id)
    except ValueError as err:
        raise DispatchSpawnFailed(f"task_not_found:{action.task_id}") from err
    if task is None:
        raise DispatchSpawnFailed(f"task_not_found:{action.task_id}")

    project_id = str(getattr(task, "project_id", "") or _field(context, "project_id", ""))
    if not project_id:
        raise DispatchSpawnFailed("project_id_missing")

    from gobby.agents.launcher_session import get_or_create_launcher_session
    from gobby.storage.projects import LocalProjectManager
    from gobby.workflows.agent_resolver import AgentResolutionError, resolve_agent

    parent_session_id = get_or_create_launcher_session(
        session_manager,
        project_id,
        "dispatcher_launcher",
        "Dispatcher Launcher",
    )
    try:
        agent_body = resolve_agent(action.agent_slug, db, project_id=project_id)
    except AgentResolutionError as exc:
        raise DispatchSpawnFailed(f"agent_definition_missing:{action.agent_slug}") from exc

    prompt = action.prompt
    if agent_body is not None:
        preamble = agent_body.build_prompt_preamble()
        if preamble:
            prompt = f"{preamble}\n\n---\n\n{prompt}"

    initial_variables = dict(action.initial_variables or {})
    initial_variables["_agent_type"] = action.agent_slug
    if action.additional_skills:
        initial_variables["additional_skills"] = list(action.additional_skills)
    if agent_body is not None:
        if agent_body.workflows.rules:
            initial_variables["_agent_rules"] = agent_body.workflows.rules
        if agent_body.workflows.variables:
            initial_variables.update(agent_body.workflows.variables)
        if agent_body.steps:
            from gobby.mcp_proxy.tools.spawn_agent._factory import _register_agent_step_workflow

            initial_variables["_step_workflow_name"] = _register_agent_step_workflow(
                agent_body,
                db,
            )

    workflow = (
        agent_body.workflows.pipeline if agent_body and agent_body.workflows.pipeline else None
    )
    effective_isolation = _effective_spawn_isolation(
        task=task,
        action=action,
        agent_body=agent_body,
    )
    artifacts = _prepare_spawn_artifacts(
        db=db,
        action=action,
        task=task,
        task_manager=task_manager,
        project_id=project_id,
        services=services,
        isolation=effective_isolation,
    )
    artifacts = _sanitize_reusable_spawn_artifacts(
        db=db,
        task=task,
        artifacts=artifacts,
        services=services,
        isolation=effective_isolation,
    )
    artifacts = _repair_leaf_target_branch(
        db=db,
        task=task,
        task_manager=task_manager,
        artifacts=artifacts,
        isolation=effective_isolation,
    )
    project = LocalProjectManager(db).get(project_id)
    project_path = project.repo_path if project is not None else None
    worktree_id, clone_id = _spawn_workspace_ids(
        task=task,
        action=action,
        artifacts=artifacts,
        isolation=effective_isolation,
    )

    from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

    result = await spawn_agent_impl(
        prompt=prompt,
        runner=runner,
        agent_body=agent_body,
        agent_lookup_name=action.agent_slug,
        task_id=action.task_id,
        task_manager=task_manager,
        isolation=effective_isolation,
        branch_name=None,
        base_branch=artifacts.target_branch,
        clone_id=clone_id,
        worktree_id=worktree_id,
        worktree_storage=getattr(services, "worktree_storage", None),
        git_manager=_project_git_manager(services, project_id),
        clone_storage=getattr(services, "clone_storage", None),
        clone_manager=_project_clone_manager(services, project_id),
        workflow=workflow,
        provider=None,
        model=action.model_override,
        reasoning_effort=action.reasoning_effort,
        parent_session_id=parent_session_id,
        project_path=project_path,
        initial_variables=initial_variables,
        session_manager=session_manager,
        db=db,
        daemon_config=getattr(services, "config", None),
        code_index=getattr(services, "code_indexer", None),
    )
    if not result.get("success"):
        raise DispatchSpawnFailed(str(result.get("error") or "spawn_failed"))
    run_id = result.get("run_id")
    if not run_id:
        raise DispatchSpawnFailed("missing run_id")

    _persist_spawn_artifacts(db, action.task_id, result)
    try:
        _subscribe_build_coordinator_completion(
            db=db,
            project_id=project_id,
            task_id=action.task_id,
            run_id=str(run_id),
            services=services,
        )
    except Exception:
        logger.warning(
            "Failed to subscribe build coordinator to dispatcher-spawned agent completion",
            extra={"task_id": action.task_id, "run_id": str(run_id), "project_id": project_id},
            exc_info=True,
        )
    return str(run_id)


def _subscribe_build_coordinator_completion(
    *,
    db: HubDatabase,
    project_id: str,
    task_id: str,
    run_id: str,
    services: object | None,
) -> None:
    """Subscribe the active build coordinator, if any, to agent completion."""
    run = BuildHistoryStorage(db).latest_coordinated_run_for_task(project_id, task_id)
    if run is None or not run.summary:
        return
    coordinator_session_id = run.summary.get("coordinator_session_id")
    if not isinstance(coordinator_session_id, str) or not coordinator_session_id:
        return
    session_manager = getattr(services, "session_manager", None)
    if not _coordinator_session_matches_project(
        session_manager,
        coordinator_session_id,
        project_id,
        run.summary,
    ):
        return
    subscribe_agent_completion(
        completion_registry=getattr(services, "completion_registry", None),
        run_id=run_id,
        subscriber_session_id=coordinator_session_id,
        session_manager=session_manager,
        db=db,
    )


def _coordinator_session_matches_project(
    session_manager: SessionManager | None,
    coordinator_session_id: str,
    project_id: str,
    run_summary: dict[str, object],
) -> bool:
    """Return whether a coordinator session exists and is authorized for this build."""
    if session_manager is None:
        logger.debug(
            "Skipping build coordinator completion subscription; no session_manager",
            extra={"coordinator_session_id": coordinator_session_id, "project_id": project_id},
        )
        return False
    session = session_manager.get(coordinator_session_id)
    if session is None:
        logger.debug(
            "Skipping build coordinator completion subscription; coordinator session missing",
            extra={"coordinator_session_id": coordinator_session_id, "project_id": project_id},
        )
        return False
    coordinator_project_id = getattr(session, "project_id", None)
    if coordinator_project_id != project_id and not summary_allows_cross_project_coordinator(
        run_summary,
        coordinator_project_id=coordinator_project_id,
        build_project_id=project_id,
    ):
        logger.warning(
            "Skipping build coordinator completion subscription for cross-project session",
            extra={
                "coordinator_session_id": coordinator_session_id,
                "coordinator_project_id": coordinator_project_id,
                "project_id": project_id,
            },
        )
        return False
    return True


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
    if not _uses_epic_integration_workspace(task, action):
        return artifacts

    if isolation not in {"worktree", "clone"}:
        return artifacts
    if not artifacts.target_branch:
        artifacts = _repair_missing_epic_target_branch(
            db=db,
            task=task,
            task_manager=task_manager,
            project_id=project_id,
            backend=cast(Literal["worktree", "clone"], isolation),
            artifacts=artifacts,
        )
    if not artifacts.target_branch:
        raise DispatchSpawnFailed("target_branch_missing")

    try:
        ensure_epic_integration_workspaces(
            task_manager=task_manager,
            root_task=task,
            backend=cast(Literal["worktree", "clone"], isolation),
            target_branch=artifacts.target_branch,
            project_id=project_id,
            services=services,
            merge_closed_descendant_commits=True,
        )
    except BuildWorkspaceError as exc:
        raise DispatchSpawnFailed(
            str(exc),
            stage_failure_cited_subtasks=_holistic_workspace_failure_cited_subtasks(
                action=action,
                task=task,
                task_manager=task_manager,
            ),
        ) from exc
    return TaskArtifactManager(db).get_artifacts(action.task_id)


def _holistic_workspace_failure_cited_subtasks(
    *,
    action: SpawnAgentAction,
    task: Task,
    task_manager: LocalTaskManager,
) -> tuple[str, ...]:
    if action.agent_slug != "holistic-reviewer":
        return ()
    initial_variables = action.initial_variables or {}
    if initial_variables.get("stage_name") != "holistic_qa":
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
    artifacts: TaskArtifacts,
    isolation: SpawnIsolation | None,
) -> TaskArtifacts:
    if isolation not in {"worktree", "clone"} or not task.parent_task_id:
        return artifacts
    if task.task_type == "epic" or artifacts.worktree_id or artifacts.clone_id:
        return artifacts

    target_branch = _nearest_parent_integration_or_target(task_manager, task)
    if not target_branch or artifacts.target_branch == target_branch:
        return artifacts

    TaskArtifactManager(db).set_artifacts_atomic(task.id, target_branch=target_branch)
    return TaskArtifactManager(db).get_artifacts(task.id)


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


def _repair_missing_epic_target_branch(
    *,
    db: HubDatabase,
    task: Task,
    task_manager: LocalTaskManager,
    project_id: str,
    backend: Literal["worktree", "clone"],
    artifacts: TaskArtifacts,
) -> TaskArtifacts:
    if backend == "worktree":
        repaired = _promote_existing_worktree_artifact(db, task, artifacts)
    else:
        repaired = _promote_existing_clone_artifact(db, task, artifacts)
    if repaired is not None:
        return repaired

    target_branch = _nearest_parent_integration_or_target(task_manager, task)
    if target_branch is None:
        target_branch = _current_project_branch(db, project_id)
    if target_branch is None:
        return artifacts

    TaskArtifactManager(db).set_artifacts_atomic(task.id, target_branch=target_branch)
    return TaskArtifactManager(db).get_artifacts(task.id)


def _promote_existing_worktree_artifact(
    db: HubDatabase,
    task: Task,
    artifacts: TaskArtifacts,
) -> TaskArtifacts | None:
    if not artifacts.worktree_id:
        return None

    from pathlib import Path

    from gobby.storage.worktrees import LocalWorktreeManager

    worktrees = LocalWorktreeManager(db)
    worktree = worktrees.get(artifacts.worktree_id)
    if worktree is None or worktree.task_id != task.id:
        return None
    if not worktree.base_branch or not Path(worktree.worktree_path).is_dir():
        return None

    worktrees.update(worktree.id, workspace_role="integration")
    TaskArtifactManager(db).set_artifacts_atomic(
        task.id,
        target_branch=worktree.base_branch,
        integration_branch=worktree.branch_name,
        integration_workspace_id=worktree.id,
        integration_clone_id=None,
        worktree_path=None,
        worktree_id=None,
        base_commit_sha=None,
    )
    return TaskArtifactManager(db).get_artifacts(task.id)


def _promote_existing_clone_artifact(
    db: HubDatabase,
    task: Task,
    artifacts: TaskArtifacts,
) -> TaskArtifacts | None:
    if not artifacts.clone_id:
        return None

    from pathlib import Path

    from gobby.storage.clones import LocalCloneManager

    clones = LocalCloneManager(db)
    clone = clones.get(artifacts.clone_id)
    if clone is None or clone.task_id != task.id:
        return None
    if not clone.base_branch or not Path(clone.clone_path).is_dir():
        return None

    clones.update(clone.id, workspace_role="integration")
    TaskArtifactManager(db).set_artifacts_atomic(
        task.id,
        target_branch=clone.base_branch,
        integration_branch=clone.branch_name,
        integration_workspace_id=None,
        integration_clone_id=clone.id,
        clone_path=None,
        clone_id=None,
        base_commit_sha=None,
    )
    return TaskArtifactManager(db).get_artifacts(task.id)


def _nearest_parent_integration_or_target(
    task_manager: LocalTaskManager,
    task: Task,
) -> str | None:
    current_id = task.parent_task_id
    while current_id:
        try:
            parent = task_manager.get_task(current_id)
        except ValueError:
            return None
        if parent is None:
            return None
        parent_artifacts = TaskArtifactManager(task_manager.db).get_artifacts(parent.id)
        if parent_artifacts.integration_branch:
            return parent_artifacts.integration_branch
        if parent_artifacts.target_branch:
            return parent_artifacts.target_branch
        current_id = parent.parent_task_id
    return None


def _current_project_branch(db: HubDatabase, project_id: str) -> str | None:
    from gobby.storage.projects import LocalProjectManager
    from gobby.worktrees.git import WorktreeGitManager

    project = LocalProjectManager(db).get(project_id)
    if project is None or project.repo_path is None:
        return None
    repo_path = Path(project.repo_path)
    if not (repo_path / ".git").exists():
        return None
    status = WorktreeGitManager(repo_path).get_worktree_status(repo_path)
    if status is None or not status.branch or status.branch == "HEAD":
        return None
    return status.branch


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
        if task_isolation is not None:
            return task_isolation
        return "none"
    if stage_name in _DEVELOPMENT_FORWARD_ISOLATION_STAGES:
        return task_isolation

    agent_isolation = getattr(agent_body, "isolation", None)
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
    if action.agent_slug != "holistic-reviewer":
        return False
    if getattr(task, "task_type", None) != "epic":
        return False
    stage_name = (action.initial_variables or {}).get("stage_name")
    return stage_name in {None, "holistic_qa"}


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
    artifacts = TaskArtifactManager(db).get_artifacts(task_id)
    fields: dict[str, str | int | None] = {}
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
        try:
            _set_artifacts_atomic(db, task_id, **fields)
        except (TaskArtifactConstraintError, ValueError, psycopg.Error):
            logger.error(
                "Failed to persist dispatcher spawn artifacts",
                extra={"task_id": task_id, "fields": fields},
                exc_info=True,
            )


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
