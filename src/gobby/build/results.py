"""Build service result types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from gobby.build.dispatch_tick import DispatcherTickSummary
from gobby.utils.datetime import normalize_datetime_model


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


__all__ = [
    "BuildControlResult",
    "BuildLifecycleEvent",
    "BuildResult",
]
