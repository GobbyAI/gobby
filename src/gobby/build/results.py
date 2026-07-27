"""Build service result types."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal

from gobby.build.control_artifacts import BuildArtifactSummary
from gobby.build.dispatch_tick import DispatcherTickSummary
from gobby.storage.build_history import best_effort_record_event, best_effort_record_run
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import normalize_datetime_model

BuildTargetAction = Literal["stop", "resume", "clean", "restart"]


@dataclass
class BuildResult:
    task_id: str
    created: bool
    initial_lifecycle: str
    applied_stages_skipped: list[str]
    tick_dispatched: int
    dispatcher_tick: DispatcherTickSummary = field(default_factory=DispatcherTickSummary)
    manifest: list[dict[str, str | int | None]] | None = None
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False


@normalize_datetime_model(required=("created_at",))
@dataclass(frozen=True)
class BuildLifecycleEvent:
    """Project-level build lifecycle audit event."""

    id: int
    project_id: str
    event: str
    reason: str
    by_actor: str
    created_at: datetime


@dataclass(frozen=True)
class BuildControlResult:
    """Result returned by build stop/resume entry points."""

    project_id: str
    enabled: bool
    lifecycle_event: BuildLifecycleEvent


@dataclass(frozen=True)
class BuildTaskSummary:
    """Task touched by a task-scoped build control."""

    task_id: str
    ref: str
    title: str
    task_type: str


@dataclass(frozen=True)
class BuildAgentSummary:
    """Active agent affected by a task-scoped build control."""

    run_id: str
    task_id: str | None
    status: str
    child_session_id: str | None
    worktree_id: str | None
    clone_id: str | None


@dataclass
class BuildTargetControlResult:
    """Result returned by task-scoped build lifecycle controls."""

    action: BuildTargetAction
    project_id: str
    root_task_id: str
    affected_tasks: list[BuildTaskSummary]
    agents: list[BuildAgentSummary] = field(default_factory=list)
    artifacts: list[BuildArtifactSummary] = field(default_factory=list)
    dry_run: bool = False
    force: bool = False
    automation_updated: int = 0
    mutexes_cleared: int = 0
    claims_released: int = 0
    parked_runs_released: int = 0
    stages_reset: int = 0
    branches_deleted: int = 0
    escalations_cleared: int = 0
    dispatch_failures_reset: int = 0
    dispatcher_tick: DispatcherTickSummary | None = None
    manifest: list[dict[str, Any]] = field(default_factory=list)
    blocked_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return asdict(self)


def _record_target_history(
    db: HubDatabase,
    result: BuildTargetControlResult,
    *,
    input_ref: str,
) -> None:
    summary = result.to_dict()
    run = best_effort_record_run(
        db,
        project_id=result.project_id,
        root_task_id=result.root_task_id,
        input_ref=input_ref,
        action=result.action,
        status="completed",
        actor="build",
        summary=summary,
    )
    best_effort_record_event(
        db,
        run_id=run.id if run is not None else None,
        project_id=result.project_id,
        root_task_id=result.root_task_id,
        event_type="task_build_control",
        action=result.action,
        message=f"gobby build {result.action}",
        payload=summary,
    )


__all__ = [
    "BuildAgentSummary",
    "BuildControlResult",
    "BuildLifecycleEvent",
    "BuildResult",
    "BuildTargetAction",
    "BuildTargetControlResult",
    "BuildTaskSummary",
]
