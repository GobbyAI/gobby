"""Runtime hook types for build lifecycle helpers."""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Protocol

from gobby.build.dispatch_tick import DispatcherTickSummary
from gobby.build.options import BuildOptions
from gobby.build.workspace_common import WorkspaceBackend
from gobby.storage.clones import Clone
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.worktrees import Worktree


class DispatcherTickHook(Protocol):
    def __call__(
        self,
        db: HubDatabase | None = None,
        project_id: str | None = None,
        *,
        dispatcher_enabled: bool | None = None,
        services: object | None = None,
        max_ticks: int | None = None,
        max_actions: int | None = None,
        max_active_agents: int | None = None,
    ) -> Awaitable[DispatcherTickSummary]: ...


class EpicIntegrationWorkspacesHook(Protocol):
    def __call__(
        self,
        *,
        task_manager: LocalTaskManager,
        root_task: Task,
        backend: WorkspaceBackend,
        target_branch: str,
        project_id: str,
        services: object | None,
        merge_closed_descendant_commits: bool = False,
    ) -> None: ...


class TaskParentIntegrationWorkspaceHook(Protocol):
    def __call__(
        self,
        *,
        task_manager: LocalTaskManager,
        task: Task,
        backend: WorkspaceBackend,
        project_id: str,
        services: object | None,
        base_branch_override: str | None = None,
    ) -> Worktree | Clone | None: ...


class BuildDispatcherTickHook(Protocol):
    def __call__(
        self,
        db: HubDatabase,
        project_id: str,
        opts: BuildOptions,
        *,
        dispatcher_enabled: bool,
        services: object | None,
        runtime: RuntimeHooks,
    ) -> Awaitable[DispatcherTickSummary]: ...


class AttachBuildRunRootHook(Protocol):
    def __call__(
        self,
        db: HubDatabase,
        build_run_id: str | None,
        root_task_id: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class RuntimeHooks:
    dispatcher_tick: DispatcherTickHook
    ensure_epic_integration_workspaces: EpicIntegrationWorkspacesHook
    ensure_task_parent_integration_workspace: TaskParentIntegrationWorkspaceHook
    build_dispatcher_tick: BuildDispatcherTickHook
    attach_build_run_root: AttachBuildRunRootHook
