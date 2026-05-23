"""Build service result types."""

from __future__ import annotations

from dataclasses import dataclass, field

from gobby.build.dispatch_tick import DispatcherTickSummary


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

    @property
    def stage_manifest(self) -> list[dict[str, str | int | None]] | None:
        """Intentionally alias ``manifest`` for compatibility."""
        return self.manifest


@dataclass(frozen=True)
class BuildLifecycleEvent:
    """Project-level build lifecycle audit event."""

    id: int
    project_id: str
    event: str
    reason: str
    by_actor: str
    created_at: str


@dataclass(frozen=True)
class BuildControlResult:
    """Result returned by build stop/resume entry points."""

    project_id: str
    enabled: bool
    cron_job_id: str
    lifecycle_event: BuildLifecycleEvent


__all__ = [
    "BuildControlResult",
    "BuildLifecycleEvent",
    "BuildResult",
]
