"""Build option dataclasses shared by build entry points."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from gobby.build.workspaces import WorkspaceBackend
from gobby.config.build import DeliveryMode, Isolation, StageCapOverride


@dataclass
class BuildOptions:
    """Resolved options for a build request."""

    profile: str = "default"
    profile_explicit: bool = False
    quick: bool = False
    skip_stages: list[str] = field(default_factory=list)
    skip_stages_explicit: bool = False
    isolation: Isolation = "worktree"
    isolation_explicit: bool = True
    unattended: bool = False
    unattended_explicit: bool = False
    delivery_mode: DeliveryMode = "auto"
    delivery_target_repo: str | None = None
    no_merge: bool = False
    pr: str | None = None
    stage_caps: list[StageCapOverride] = field(default_factory=list)
    target_branch: str | None = None
    assigned_agent: str | None = None
    clones_dir: Path | None = None
    reset_expansion_output: bool = False
    max_active_agents: int | None = None
    max_retries: int | None = None
    planning_seed_state: Literal["drafted", "needs_review", "approved"] = "drafted"
    completed_plan_review_rounds: int = 0
    dry_run: bool = False
    coordinator_session_ref: str | None = None

    @property
    def workspace_backend(self) -> WorkspaceBackend:
        return "clone" if self.isolation == "clone" else "worktree"

    @property
    def workspace_backend_explicit(self) -> bool:
        return self.isolation_explicit


@dataclass(frozen=True, slots=True)
class BuildIsolationResolution:
    """Resolved isolation value and whether the caller supplied an isolation knob."""

    isolation: Isolation
    explicit: bool


def resolve_build_isolation(
    *,
    isolation: Isolation | None,
    workspace_backend: WorkspaceBackend | None,
    clone: bool,
) -> BuildIsolationResolution:
    """Resolve legacy and current build isolation fields with one conflict policy."""

    if clone and isolation in {"none", "worktree"}:
        raise ValueError(f"clone=true conflicts with isolation={isolation}")
    if clone and workspace_backend == "worktree":
        raise ValueError("clone=true conflicts with workspace_backend=worktree")
    if isolation is not None and workspace_backend is not None and isolation != workspace_backend:
        raise ValueError("isolation conflicts with workspace_backend")

    resolved = isolation or workspace_backend or ("clone" if clone else "worktree")
    if clone and resolved != "clone":
        raise ValueError("clone=true requires isolation=clone or workspace_backend=clone")
    return BuildIsolationResolution(
        isolation=resolved,
        explicit=isolation is not None or workspace_backend is not None or clone,
    )


def retry_attempt_cap(opts: BuildOptions) -> int | None:
    """Return total allowed attempts/rounds for a max-retries request."""
    if opts.max_retries is None:
        return None
    return opts.max_retries + 1
