"""State helpers for build lifecycle orchestration."""

from __future__ import annotations

from typing import Literal

from gobby.build.options import BuildOptions
from gobby.build.stage_manifest import InputKind, resolve_stage_manifest_specs
from gobby.storage.tasks import (
    LocalTaskManager,
    ManifestAlreadyInitializedError,
    StageManifestSpec,
    Task,
)
from gobby.storage.tasks._lifecycle_events import BUILD_EVENT_REASON

BuildState = Literal["never_started", "running", "paused"]


def derive_build_state(*, allow_automation: bool, has_build_event: bool) -> BuildState:
    """Resolve a task's definitive build state for the web payload.

    - ``running``: automation is currently enabled (``allow_automation``).
    - ``paused``: automation is off but a build was started at some point
      (``build_stop_target`` clears ``allow_automation`` without recording a
      new lifecycle event or bumping ``dispatch_failure_count``, so the durable
      ``gobby build`` event is the only honest signal here).
    - ``never_started``: automation is off and no build was ever started.
    """
    if allow_automation:
        return "running"
    if has_build_event:
        return "paused"
    return "never_started"


def initialize_stage_manifest(
    task_manager: LocalTaskManager,
    task: Task,
    opts: BuildOptions,
    skip_stages: list[str],
    input_kind: InputKind,
) -> list[StageManifestSpec]:
    specs = resolve_stage_manifest_specs(task_manager, task, input_kind, opts, skip_stages)
    try:
        task_manager.stage_states.initialize_manifest(task.id, specs, by_session_id=None)
    except ManifestAlreadyInitializedError as exc:
        raise ValueError(
            "Task already has a different lifecycle manifest. "
            f"Use `gobby build restart {task.id}` or `gobby build clean {task.id}` "
            "before changing the build stage shape."
        ) from exc
    return specs


def current_stage_name(
    task_manager: LocalTaskManager,
    task_id: str,
    specs: list[StageManifestSpec],
) -> str:
    current = task_manager.stage_states.current_stage(task_id)
    if current is not None:
        return current.stage_name
    if not specs:
        raise ValueError(f"stage manifest is empty for task {task_id}")
    return min(specs, key=lambda spec: spec.position).stage_name


def record_build_event(
    task_manager: LocalTaskManager,
    task_id: str,
    to_state: str,
) -> None:
    task_manager.lifecycle_events.record_lifecycle_event(
        task_id,
        from_state=None,
        to_state=to_state,
        reason=BUILD_EVENT_REASON,
        by_actor="build",
    )
