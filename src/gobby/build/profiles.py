"""Build profile resolution."""

from __future__ import annotations

from gobby.build.options import BuildOptions
from gobby.storage.build_profiles import (
    BuildProfileError,
    BuildProfileLoader,
    BuildProfileManager,
)
from gobby.storage.hub.protocol import HubDatabase


def resolve_build_profile_options(
    opts: BuildOptions,
    *,
    db: HubDatabase,
    project_id: str,
) -> BuildOptions:
    """Overlay DB-backed profile defaults with explicit build request fields."""

    manager = BuildProfileManager(db)
    if manager.get("default", source="installed", project_id=None, include_deleted=True) is None:
        BuildProfileLoader().sync(db)
    profile = manager.resolve(opts.profile or "default", project_id=project_id)
    has_request_skip_stages = opts.skip_stages_explicit or bool(opts.skip_stages)
    # Request-supplied peer fields win over profile defaults; fields without an
    # explicit marker remain direct build options rather than profile overlays.
    return BuildOptions(
        profile=opts.profile,
        profile_explicit=opts.profile_explicit,
        quick=opts.quick,
        skip_stages=list(opts.skip_stages)
        if has_request_skip_stages
        else list(profile.skip_stages),
        skip_stages_explicit=has_request_skip_stages,
        isolation=profile.isolation if not opts.isolation_explicit else opts.isolation,
        isolation_explicit=opts.isolation_explicit,
        unattended=profile.unattended if not opts.unattended_explicit else opts.unattended,
        unattended_explicit=opts.unattended_explicit,
        delivery_mode=profile.delivery_mode,
        delivery_target_repo=profile.delivery_target_repo,
        no_merge=opts.no_merge,
        pr=opts.pr,
        stage_caps=list(opts.stage_caps),
        target_branch=opts.target_branch,
        assigned_agent=opts.assigned_agent,
        clones_dir=opts.clones_dir,
        reset_expansion_output=opts.reset_expansion_output,
        max_active_agents=opts.max_active_agents,
        max_retries=opts.max_retries,
        planning_seed_state=opts.planning_seed_state,
        completed_plan_review_rounds=opts.completed_plan_review_rounds,
        dry_run=opts.dry_run,
        coordinator_session_ref=opts.coordinator_session_ref,
    )


__all__ = ["BuildProfileError", "resolve_build_profile_options"]
