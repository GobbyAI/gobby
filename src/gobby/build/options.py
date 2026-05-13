"""Build option dataclasses shared by build entry points."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

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

    @property
    def workspace_backend(self) -> WorkspaceBackend:
        return "clone" if self.isolation == "clone" else "worktree"

    @property
    def workspace_backend_explicit(self) -> bool:
        return self.isolation_explicit


def retry_attempt_cap(opts: BuildOptions) -> int | None:
    """Return total allowed attempts/rounds for a max-retries request."""
    if opts.max_retries is None:
        return None
    return opts.max_retries + 1
