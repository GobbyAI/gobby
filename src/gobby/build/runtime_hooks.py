"""Runtime hook types for build lifecycle helpers."""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Protocol

from gobby.build.dispatch_tick import DispatcherTickSummary
from gobby.build.options import BuildOptions
from gobby.storage.hub.protocol import HubDatabase


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
    build_dispatcher_tick: BuildDispatcherTickHook
    attach_build_run_root: AttachBuildRunRootHook
