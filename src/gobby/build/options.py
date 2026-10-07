"""Build option dataclasses shared by build entry points."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from gobby.build.workspaces import WorkspaceBackend
from gobby.config.build import CheckoutMode, StageCapOverride


@dataclass
class BuildOptions:
    """Resolved options for a build request."""

    profile: str = "default"
    quick: bool = False
    skip_stages: list[str] = field(default_factory=list)
    skip_stages_explicit: bool = False
    checkout_mode: CheckoutMode = "worktree"
    checkout_mode_explicit: bool = False
    unattended: bool = False
    unattended_explicit: bool = False
    no_merge: bool = False
    pr: str | None = None
    stage_caps: list[StageCapOverride] = field(default_factory=list)
    target_branch: str | None = None
    assigned_agent: str | None = None
    clones_dir: Path | None = None
    cwd: Path | None = None
    reset_expansion_output: bool = False
    max_active_agents: int | None = None
    max_retries: int | None = None
    planning_seed_state: Literal["drafted", "needs_review", "approved"] = "drafted"
    completed_plan_review_rounds: int = 0
    plan_enhancement_rounds: int = 0
    plan_enhancement_rounds_explicit: bool = False
    dry_run: bool = False
    coordinator_session_ref: str | None = None
    project_explicit: bool = False

    @property
    def workspace_backend(self) -> WorkspaceBackend:
        return "clone" if self.checkout_mode == "clone" else "worktree"

    @property
    def workspace_backend_explicit(self) -> bool:
        return self.checkout_mode_explicit


@dataclass(frozen=True, slots=True)
class BuildCheckoutModeResolution:
    """Resolved checkout mode and whether the caller supplied it explicitly."""

    checkout_mode: CheckoutMode
    explicit: bool


def resolve_build_checkout_mode(
    *,
    checkout_mode: CheckoutMode | None,
    workspace_backend: WorkspaceBackend | None,
    clone: bool,
) -> BuildCheckoutModeResolution:
    """Resolve build checkout fields with one conflict policy."""

    if clone and checkout_mode in {"none", "worktree"}:
        raise ValueError(f"clone=true conflicts with checkout_mode={checkout_mode}")
    if clone and workspace_backend == "worktree":
        raise ValueError("clone=true conflicts with workspace_backend=worktree")
    if (
        checkout_mode is not None
        and workspace_backend is not None
        and checkout_mode != workspace_backend
    ):
        raise ValueError("checkout_mode conflicts with workspace_backend")

    resolved = checkout_mode or workspace_backend or ("clone" if clone else "worktree")
    if clone and resolved != "clone":
        raise ValueError("clone=true requires checkout_mode=clone or workspace_backend=clone")
    return BuildCheckoutModeResolution(
        checkout_mode=resolved,
        explicit=checkout_mode is not None or workspace_backend is not None or clone,
    )


def retry_attempt_cap(opts: BuildOptions) -> int | None:
    """Return total allowed attempts/rounds for a max-retries request."""
    if opts.max_retries is None:
        return None
    return opts.max_retries + 1
