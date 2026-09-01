"""CLI surface for build lifecycle automation."""

from __future__ import annotations

import asyncio
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, cast

import click

from gobby.build import (
    BuildOptions,
    build,
    build_clean_target,
    build_restart_target,
    build_resume,
    build_resume_target,
    build_stop,
    build_stop_target,
)
from gobby.build.dispatch_tick import kick_dispatcher_tick as _kick_dispatcher_tick
from gobby.build.profiles import BuildProfileError
from gobby.config.build import DeliveryMode, Isolation
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.uuid_validation import is_full_uuid

from . import _build_daemon as _build_daemon_helpers
from . import _build_options as _build_options_helpers
from . import _build_output as _build_output_helpers
from .utils import resolve_project_ref, resolve_session_id

BuildPlanningSeedState = Literal["drafted", "needs_review", "approved"]
CURRENT_COORDINATOR = "__current_cli_session__"

# Compatibility aliases for tests and callers that import private build CLI helpers.
DAEMON_BUILD_REQUEST_TIMEOUT_SECONDS = _build_daemon_helpers.DAEMON_BUILD_REQUEST_TIMEOUT_SECONDS
BuildProfileClickException = _build_daemon_helpers.BuildProfileClickException
_PROFILE_ERROR_RE = _build_daemon_helpers._PROFILE_ERROR_RE
_build_payload = _build_daemon_helpers._build_payload
_control_payload_from_daemon = _build_daemon_helpers._control_payload_from_daemon
_daemon_error_detail = _build_daemon_helpers._daemon_error_detail
_daemon_error_message = _build_daemon_helpers._daemon_error_message
_dispatcher_tick_from_payload = _build_daemon_helpers._dispatcher_tick_from_payload
_is_profile_error = _build_daemon_helpers._is_profile_error
_payload_int = _build_daemon_helpers._payload_int
_payload_string_list = _build_daemon_helpers._payload_string_list
_restart_options_payload = _build_daemon_helpers._restart_options_payload
_result_from_payload = _build_daemon_helpers._result_from_payload
_try_daemon_build = _build_daemon_helpers._try_daemon_build
_try_daemon_build_control = _build_daemon_helpers._try_daemon_build_control
_parse_skip_stages = _build_options_helpers._parse_skip_stages
_parse_stage_cap = _build_options_helpers._parse_stage_cap
_restart_options_were_supplied = _build_options_helpers._restart_options_were_supplied
_stage_cap_options = _build_options_helpers._stage_cap_options
_echo_build_control_result = _build_output_helpers._echo_build_control_result
_echo_build_result = _build_output_helpers._echo_build_result
_echo_target_control_result = _build_output_helpers._echo_target_control_result
_lifecycle_display = _build_output_helpers._lifecycle_display


@dataclass(frozen=True)
class _BuildProjectContext:
    project_id: str
    cwd: Path
    explicit: bool
    caller_project_id: str | None = None


def resolve_project_id(project_ref: str | None = None) -> str:
    """Resolve the current or explicit project id for build requests."""
    project_id = resolve_project_ref(project_ref, exit_on_not_found=project_ref is not None)
    if project_id is None:
        raise click.ClickException("No project context found")
    return project_id


def _resolve_build_project_context(
    project_ref: str | None, caller_cwd: Path
) -> _BuildProjectContext:
    explicit = bool(project_ref)
    caller_project_id = resolve_project_ref(None, exit_on_not_found=False) if explicit else None
    project_id = resolve_project_id(project_ref)
    if not explicit:
        return _BuildProjectContext(
            project_id=project_id,
            cwd=caller_cwd,
            explicit=False,
            caller_project_id=project_id,
        )
    return _BuildProjectContext(
        project_id=project_id,
        cwd=_project_repo_path(project_id),
        explicit=True,
        caller_project_id=caller_project_id,
    )


def _project_repo_path(project_id: str) -> Path:
    from gobby.cli.runtime import require_cli_database
    from gobby.storage.project_checkouts import (
        CheckoutNotFoundError,
        MissingMachineContextError,
        require_root,
    )
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.workspace_machine_scope import require_local_machine_id

    db = require_cli_database()
    project = LocalProjectManager(db).get(project_id)
    if project is None:
        raise click.ClickException(f"Project not found: {project_id}")
    try:
        machine_id = require_local_machine_id(
            None, resource_kind="project_checkout", resource_id=project_id
        )
        return Path(require_root(db, project_id, machine_id))
    except (CheckoutNotFoundError, MissingMachineContextError) as exc:
        raise click.ClickException(str(exc)) from exc


def invoke_build_skill() -> None:
    """Invoke the interactive build skill path."""
    click.echo("No build input provided. Invoke the build skill from your active Gobby session.")


def _require_database() -> HubDatabase:
    """Borrow the active hub database before build storage use."""
    from gobby.cli.runtime import require_cli_database

    return require_cli_database()


def _make_build_options(
    *,
    profile: str | None,
    quick: bool,
    skip_stage: tuple[str, ...],
    stage_cap: tuple[str, ...],
    isolation: Isolation | None,
    use_clone: bool,
    delivery_mode: DeliveryMode | None,
    delivery_target_repo: str | None,
    no_merge: bool,
    pr: str | None,
    target_branch: str | None,
    assigned_agent: str | None,
    reset_expansion_output: bool,
    max_active_agents: int | None,
    max_retries: int | None,
    planning_seed_state: str,
    completed_plan_review_rounds: int,
    plan_enhancement_rounds: int | None,
    dry_run: bool,
    coordinator: str | None,
    cwd: Path,
    project_explicit: bool = False,
    caller_project_id: str | None = None,
) -> BuildOptions:
    resolved_isolation: Isolation = isolation or ("clone" if use_clone else "worktree")
    return BuildOptions(
        profile=profile or "default",
        quick=quick,
        skip_stages=_parse_skip_stages(skip_stage),
        skip_stages_explicit=bool(skip_stage),
        isolation=resolved_isolation,
        isolation_explicit=isolation is not None or use_clone,
        delivery_mode=delivery_mode or "auto",
        delivery_mode_explicit=delivery_mode is not None,
        delivery_target_repo=delivery_target_repo,
        delivery_target_repo_explicit=delivery_target_repo is not None,
        no_merge=no_merge,
        pr=pr,
        stage_caps=_parse_stage_cap(stage_cap),
        target_branch=target_branch,
        assigned_agent=assigned_agent,
        cwd=cwd,
        reset_expansion_output=reset_expansion_output,
        max_active_agents=max_active_agents,
        max_retries=max_retries,
        planning_seed_state=cast(BuildPlanningSeedState, planning_seed_state),
        completed_plan_review_rounds=completed_plan_review_rounds,
        plan_enhancement_rounds=plan_enhancement_rounds
        if plan_enhancement_rounds is not None
        else 0,
        plan_enhancement_rounds_explicit=plan_enhancement_rounds is not None,
        dry_run=dry_run,
        coordinator_session_ref=_coordinator_session_ref(
            coordinator,
            project_explicit=project_explicit,
            caller_project_id=caller_project_id,
        ),
        project_explicit=project_explicit,
    )


@click.command("build")
@click.argument("input_ref", required=False, metavar="[INPUT|ACTION]")
@click.argument("target_ref", required=False, metavar="[REF]")
@click.option("--profile", help="Build profile to apply.")
@click.option("--quick", is_flag=True, default=False, help="Run one lifecycle step.")
@click.option(
    "--skip-stage", multiple=True, help="Stage to skip. May be repeated or comma-separated."
)
@click.option(
    "--stage",
    "stage_cap",
    multiple=True,
    help="Stage cap/settings override, e.g. development:max_review_rounds=4.",
)
@click.option(
    "--isolation",
    type=click.Choice(["none", "worktree", "clone"]),
    help="Build workspace isolation mode.",
)
@click.option("--clone", "use_clone", is_flag=True, default=False, help="Use clone workspaces.")
@click.option("--delivery-mode", type=click.Choice(["auto", "pull_request"]))
@click.option("--delivery-target-repo", help="Delivery target repository override.")
@click.option("--no-merge", is_flag=True, default=False, help="Leave isolated work unmerged.")
@click.option("--pr", "pr", help="Existing PR number or URL for PR-gated builds.")
@click.option("--target-branch", help="Target branch for the build.")
@click.option("--agent", "assigned_agent", help="Agent to assign to build work.")
@click.option(
    "--reset-expansion-output",
    is_flag=True,
    default=False,
    help="Delete existing generated expansion output before rebuilding a task ref.",
)
@click.option(
    "--max-active-agents",
    type=click.IntRange(min=1),
    help="Maximum active automation agents allowed during the immediate dispatcher tick.",
)
@click.option(
    "--max-retries",
    type=click.IntRange(min=0),
    help="Maximum retries per build stage; 0 means one attempt.",
)
@click.option(
    "--planning-seed-state",
    type=click.Choice(["drafted", "needs_review", "approved"]),
    default="drafted",
    show_default=True,
    help="Initial plan-file lifecycle state.",
)
@click.option(
    "--completed-plan-review-rounds",
    type=click.IntRange(min=0),
    default=0,
    show_default=True,
    help="Review rounds already completed before plan-file build handoff.",
)
@click.option(
    "--plan-enhancement-rounds",
    type=click.IntRange(min=0),
    default=None,
    help=(
        "Target plan-enhancement rounds before the adversary gate. "
        "Overrides the build profile default when supplied."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview build, clean, or restart without persisting changes.",
)
@click.option(
    "--coordinator",
    is_flag=False,
    flag_value=CURRENT_COORDINATOR,
    help="Session to wake for build-spawned agent completions.",
)
@click.option("--project", "project_ref", help="Project name or UUID to build.")
@click.option("--force", is_flag=True, default=False, help="Force destructive cleanup.")
@click.option(
    "--delete-dirty-worktrees",
    is_flag=True,
    default=False,
    help="Allow clean to delete dirty descendant worktrees.",
)
@click.option("--yes", is_flag=True, default=False, help="Confirm destructive clean/restart.")
@click.option(
    "--no-resume",
    is_flag=True,
    default=False,
    help="For restart, reset state but leave automation paused.",
)
def build_command(
    input_ref: str | None,
    target_ref: str | None,
    profile: str | None,
    quick: bool,
    skip_stage: tuple[str, ...],
    stage_cap: tuple[str, ...],
    isolation: Isolation | None,
    use_clone: bool,
    delivery_mode: DeliveryMode | None,
    delivery_target_repo: str | None,
    no_merge: bool,
    pr: str | None,
    target_branch: str | None,
    assigned_agent: str | None,
    reset_expansion_output: bool,
    max_active_agents: int | None,
    max_retries: int | None,
    planning_seed_state: str,
    completed_plan_review_rounds: int,
    plan_enhancement_rounds: int | None,
    dry_run: bool,
    coordinator: str | None,
    project_ref: str | None,
    force: bool,
    delete_dirty_worktrees: bool,
    yes: bool,
    no_resume: bool,
) -> None:
    """Start lifecycle automation from a plan file or task reference."""
    if input_ref == "stop":
        _run_build_stop(target_ref, project_ref=project_ref)
        return
    if input_ref == "resume":
        _run_build_resume(target_ref, project_ref=project_ref)
        return
    if input_ref == "clean":
        _run_build_clean(
            target_ref,
            dry_run=dry_run,
            force=force,
            delete_dirty_worktrees=delete_dirty_worktrees,
            yes=yes,
            project_ref=project_ref,
        )
        return
    if input_ref is None:
        invoke_build_skill()
        return
    if input_ref != "restart" and target_ref is not None:
        raise click.ClickException(f"Unexpected build argument: {target_ref}")
    if use_clone and isolation in {"none", "worktree"}:
        raise click.ClickException(f"--clone conflicts with --isolation {isolation}")
    project_context = _resolve_build_project_context(project_ref, Path.cwd())
    opts = _make_build_options(
        profile=profile,
        quick=quick,
        skip_stage=skip_stage,
        stage_cap=stage_cap,
        isolation=isolation,
        use_clone=use_clone,
        delivery_mode=delivery_mode,
        delivery_target_repo=delivery_target_repo,
        no_merge=no_merge,
        pr=pr,
        target_branch=target_branch,
        assigned_agent=assigned_agent,
        reset_expansion_output=reset_expansion_output,
        max_active_agents=max_active_agents,
        max_retries=max_retries,
        planning_seed_state=planning_seed_state,
        completed_plan_review_rounds=completed_plan_review_rounds,
        plan_enhancement_rounds=plan_enhancement_rounds,
        dry_run=dry_run,
        coordinator=coordinator,
        cwd=project_context.cwd,
        project_explicit=project_context.explicit,
        caller_project_id=project_context.caller_project_id,
    )
    if input_ref == "restart":
        _run_build_restart(
            target_ref,
            dry_run=dry_run,
            force=force,
            yes=yes,
            no_resume=no_resume,
            opts=opts if _restart_options_were_supplied(opts) else None,
            project_ref=project_ref,
        )
        return
    project_id = project_context.project_id
    cwd = str(project_context.cwd)
    result = _try_daemon_build(input_ref, opts, project_id=project_id, cwd=cwd)
    if result is None:
        db = _require_database()
        try:
            result = asyncio.run(build(input_ref, opts, db=db, project_id=project_id))
        except BuildProfileError as exc:
            raise BuildProfileClickException(str(exc)) from exc
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc

    _echo_build_result(result)


def _coordinator_session_ref(
    coordinator: str | None,
    *,
    project_explicit: bool = False,
    caller_project_id: str | None = None,
) -> str | None:
    if coordinator is None:
        return None
    ref = coordinator.strip()
    is_current = coordinator == CURRENT_COORDINATOR or ref == "current"
    if not is_current and ref:
        if project_explicit and not is_full_uuid(ref):
            raise click.ClickException(
                "--coordinator with --project must be `current` or a full session UUID"
            )
        return ref
    current_session = (os.environ.get("GOBBY_SESSION_ID") or "").strip()
    if current_session and not project_explicit:
        return current_session
    if current_session:
        try:
            return resolve_session_id(current_session, project_id=caller_project_id)
        except click.ClickException as exc:
            raise click.ClickException(f"Could not resolve current coordinator: {exc}") from exc
    codex_thread_id = (os.environ.get("CODEX_THREAD_ID") or "").strip()
    if codex_thread_id:
        db = _require_database()
        session = SessionManager(db).find_active_by_external_id(codex_thread_id, "codex")
        if session and (
            caller_project_id is None or getattr(session, "project_id", None) == caller_project_id
        ):
            return session.id
    raise click.ClickException(
        "--coordinator needs an active Gobby session; pass --coordinator SESSION explicitly"
    )


def _run_build_stop(input_ref: str | None = None, *, project_ref: str | None = None) -> None:
    project_context = _resolve_build_project_context(project_ref, Path.cwd())
    project_id = project_context.project_id
    cwd = str(project_context.cwd)
    if input_ref is not None:
        daemon_payload = _try_daemon_build_control(
            "stop", input_ref=input_ref, project_id=project_id, cwd=cwd
        )
        if daemon_payload is not None:
            _echo_target_control_result(daemon_payload)
            return
    db = _require_database()
    try:
        if input_ref is None:
            result = build_stop(db=db, project_id=project_id)
            payload: dict[str, object] | None = None
        else:
            target_result = asyncio.run(build_stop_target(input_ref, db=db, project_id=project_id))
            result = None
            payload = asdict(target_result)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    if payload is not None:
        _echo_target_control_result(payload)
    elif result is not None:
        _echo_build_control_result(result)


def _run_build_resume(input_ref: str | None = None, *, project_ref: str | None = None) -> None:
    project_context = _resolve_build_project_context(project_ref, Path.cwd())
    project_id = project_context.project_id
    cwd = str(project_context.cwd)
    if input_ref is not None:
        daemon_payload = _try_daemon_build_control(
            "resume", input_ref=input_ref, project_id=project_id, cwd=cwd
        )
        if daemon_payload is not None:
            _echo_target_control_result(daemon_payload)
            return
    db = _require_database()
    try:
        if input_ref is None:
            result = build_resume(db=db, project_id=project_id)
            asyncio.run(_kick_dispatcher_tick(db, project_id))
            payload: dict[str, object] | None = None
        else:
            target_result = asyncio.run(
                build_resume_target(input_ref, db=db, project_id=project_id)
            )
            result = None
            payload = asdict(target_result)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    if payload is not None:
        _echo_target_control_result(payload)
    elif result is not None:
        _echo_build_control_result(result)


def _confirm_destructive(action: str, input_ref: str, yes: bool, dry_run: bool) -> bool:
    if yes or dry_run:
        return True
    return click.confirm(f"Delete build artifacts for {input_ref} before build {action}?")


def _run_build_clean(
    input_ref: str | None,
    *,
    dry_run: bool,
    force: bool,
    delete_dirty_worktrees: bool = False,
    yes: bool,
    project_ref: str | None = None,
) -> None:
    if input_ref is None:
        raise click.ClickException("gobby build clean requires a task ref")
    if not _confirm_destructive("clean", input_ref, yes, dry_run):
        return
    project_context = _resolve_build_project_context(project_ref, Path.cwd())
    project_id = project_context.project_id
    cwd = str(project_context.cwd)
    daemon_payload = _try_daemon_build_control(
        "clean",
        input_ref=input_ref,
        project_id=project_id,
        cwd=cwd,
        dry_run=dry_run,
        force=force,
        delete_dirty_worktrees=delete_dirty_worktrees,
        yes=True,
    )
    if daemon_payload is not None:
        _echo_target_control_result(daemon_payload)
        return
    db = _require_database()
    try:
        result = asyncio.run(
            build_clean_target(
                input_ref,
                db=db,
                project_id=project_id,
                dry_run=dry_run,
                force=force,
                delete_dirty_worktrees=delete_dirty_worktrees,
                yes=True,
            )
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    _echo_target_control_result(asdict(result))


def _run_build_restart(
    input_ref: str | None,
    *,
    dry_run: bool,
    force: bool,
    yes: bool,
    no_resume: bool = False,
    opts: BuildOptions | None = None,
    project_ref: str | None = None,
) -> None:
    if input_ref is None:
        raise click.ClickException("gobby build restart requires a task ref")
    if not _confirm_destructive("restart", input_ref, yes, dry_run):
        return
    project_context = _resolve_build_project_context(project_ref, Path.cwd())
    project_id = project_context.project_id
    cwd = str(project_context.cwd)
    daemon_payload = _try_daemon_build_control(
        "restart",
        input_ref=input_ref,
        project_id=project_id,
        cwd=cwd,
        dry_run=dry_run,
        force=force,
        yes=True,
        no_resume=no_resume,
        opts=opts,
    )
    if daemon_payload is not None:
        _echo_target_control_result(daemon_payload)
        return
    db = _require_database()
    try:
        result = asyncio.run(
            build_restart_target(
                input_ref,
                db=db,
                project_id=project_id,
                dry_run=dry_run,
                force=force,
                yes=True,
                no_resume=no_resume,
                opts=opts,
            )
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    _echo_target_control_result(asdict(result))
