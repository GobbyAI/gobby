"""CLI surface for build lifecycle automation."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict

import click

from gobby.build import (
    BuildControlResult,
    BuildOptions,
    BuildResult,
    DispatcherTickSummary,
    build,
    build_clean_target,
    build_restart_target,
    build_resume,
    build_resume_target,
    build_stop,
    build_stop_target,
)
from gobby.config.build import StageCapOverride
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations

from .utils import resolve_project_ref

logger = logging.getLogger(__name__)


def resolve_project_id() -> str:
    """Resolve the current project id for build requests."""
    project_id = resolve_project_ref(None, exit_on_not_found=False)
    if project_id is None:
        raise click.ClickException("No project context found")
    return project_id


def invoke_build_skill() -> None:
    """Invoke the interactive build skill path."""
    click.echo("No build input provided. Invoke the build skill from your active Gobby session.")


def _parse_skip_stages(raw_values: tuple[str, ...]) -> list[str]:
    stages: list[str] = []
    for raw in raw_values:
        stages.extend(stage.strip() for stage in raw.split(",") if stage.strip())
    return stages


def _parse_stage_cap(raw_values: tuple[str, ...]) -> list[StageCapOverride]:
    by_stage: dict[str, dict[str, int | None]] = {}
    for raw in raw_values:
        stage_name, separator, cap_text = raw.partition(":")
        stage_name = stage_name.strip()
        if not stage_name:
            raise click.ClickException("--stage requires a stage name")
        stage_caps = by_stage.setdefault(stage_name, {})
        if not separator:
            continue
        for cap_item in (item.strip() for item in cap_text.split(",") if item.strip()):
            cap_name, cap_separator, value_text = cap_item.partition("=")
            if not cap_separator:
                raise click.ClickException("--stage setting must use <name>=<value>")
            cap_name = cap_name.strip()
            if cap_name not in {"max_work_attempts", "max_review_rounds"}:
                raise click.ClickException(
                    "--stage setting must be max_work_attempts or max_review_rounds"
                )
            try:
                value = int(value_text)
            except ValueError as exc:
                raise click.ClickException("--stage setting value must be an integer") from exc
            stage_caps[cap_name] = value
    return [
        StageCapOverride(
            stage_name=stage_name,
            max_work_attempts=values.get("max_work_attempts"),
            max_review_rounds=values.get("max_review_rounds"),
        )
        for stage_name, values in by_stage.items()
    ]


def _stage_cap_options(stage_caps: list[StageCapOverride]) -> list[str]:
    options: list[str] = []
    for cap in stage_caps:
        settings = []
        if cap.max_work_attempts is not None:
            settings.append(f"max_work_attempts={cap.max_work_attempts}")
        if cap.max_review_rounds is not None:
            settings.append(f"max_review_rounds={cap.max_review_rounds}")
        suffix = f":{','.join(settings)}" if settings else ""
        options.append(f"{cap.stage_name}{suffix}")
    return options


def _echo_build_result(result: BuildResult) -> None:
    click.echo(f"Task: {result.task_id}")
    click.echo(f"Lifecycle: {result.initial_lifecycle}")
    if result.applied_stages_skipped:
        click.echo(f"Skipped stages: {', '.join(result.applied_stages_skipped)}")
    tick = result.dispatcher_tick
    line = (
        f"Dispatcher tick: scanned={tick.scanned} executed={tick.executed} skipped={tick.skipped}"
    )
    if tick.cap_reached:
        line = f"{line} cap_reached"
    elif tick.reason:
        line = f"{line} reason={tick.reason}"
    click.echo(line)
    if tick.reason == "dispatcher_cron_disabled":
        click.echo("Dispatcher cron is disabled. Run `gobby build resume` to re-enable it.")


def _build_payload(opts: BuildOptions, input_ref: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "input_ref": input_ref,
        "quick": opts.quick,
        "skip_stages": opts.skip_stages,
        "no_merge": opts.no_merge,
        "pr": opts.pr,
        "stage": _stage_cap_options(opts.stage_caps),
        "target_branch": opts.target_branch,
        "agent": opts.assigned_agent,
        "reset_expansion_output": opts.reset_expansion_output,
        "max_active_agents": opts.max_active_agents,
    }
    if opts.workspace_backend_explicit:
        payload["workspace_backend"] = opts.workspace_backend
    return payload


def _result_from_payload(payload: dict[str, object]) -> BuildResult:
    tick_payload = payload.get("dispatcher_tick")
    dispatcher_tick = (
        _dispatcher_tick_from_payload(tick_payload)
        if isinstance(tick_payload, dict)
        else DispatcherTickSummary(ticks=_payload_int(payload.get("tick_dispatched")))
    )
    manifest = payload.get("manifest")
    return BuildResult(
        task_id=str(payload["task_id"]),
        created=bool(payload["created"]),
        initial_lifecycle=str(payload["initial_lifecycle"]),
        applied_stages_skipped=_payload_string_list(payload.get("applied_stages_skipped")),
        tick_dispatched=_payload_int(payload.get("tick_dispatched"), dispatcher_tick.ticks),
        dispatcher_tick=dispatcher_tick,
        manifest=manifest if isinstance(manifest, list) else None,
    )


def _payload_int(value: object, default: int = 0) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _dispatcher_tick_from_payload(payload: dict[object, object]) -> DispatcherTickSummary:
    reason = payload.get("reason")
    cap_reached = payload.get("cap_reached")
    return DispatcherTickSummary(
        ticks=_payload_int(payload.get("ticks")),
        scanned=_payload_int(payload.get("scanned")),
        executed=_payload_int(payload.get("executed")),
        skipped=_payload_int(payload.get("skipped")),
        cap_reached=cap_reached if isinstance(cap_reached, bool) else False,
        reason=reason if isinstance(reason, str) else None,
    )


def _payload_string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _try_daemon_build(input_ref: str, opts: BuildOptions) -> BuildResult | None:
    try:
        from gobby.config.app import load_config
        from gobby.utils.daemon_client import DaemonClient

        config = load_config()
        client = DaemonClient(port=config.daemon_port, timeout=5.0)
        is_healthy, _ = client.check_health()
        if not is_healthy:
            return None
        response = client.call_http_api(
            "/api/build",
            method="POST",
            json_data=_build_payload(opts, input_ref),
            timeout=300.0,
        )
        if response.status_code == 200:
            return _result_from_payload(response.json())
        if response.status_code == 400:
            detail = response.json().get("detail", response.text)
            raise click.ClickException(str(detail))
        return None
    except click.ClickException:
        raise
    except Exception:
        logger.debug("Daemon build request failed; falling back to local build", exc_info=True)
        return None


def _echo_build_control_result(result: BuildControlResult) -> None:
    state = "enabled" if result.enabled else "disabled"
    click.echo(f"Dispatcher cron: {state}")
    click.echo(f"Project: {result.project_id}")
    click.echo(f"Event: {result.lifecycle_event.reason}")


def _echo_target_control_result(payload: dict[str, object]) -> None:
    action = str(payload["action"])
    click.echo(f"Build {action}: task-scoped")
    click.echo(f"Root task: {payload['root_task_id']}")
    affected = payload.get("affected_tasks")
    if isinstance(affected, list):
        click.echo(f"Affected tasks: {len(affected)}")
    agents = payload.get("agents")
    if isinstance(agents, list):
        click.echo(f"Agents: {len(agents)}")
    stages_reset = payload.get("stages_reset")
    if isinstance(stages_reset, int):
        click.echo(f"Stages reset: {stages_reset}")
    escalations_cleared = payload.get("escalations_cleared")
    if isinstance(escalations_cleared, int) and escalations_cleared:
        click.echo(f"Escalations cleared: {escalations_cleared}")
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, list):
        deleted = sum(1 for item in artifacts if isinstance(item, dict) and item.get("deleted"))
        click.echo(f"Artifacts: {len(artifacts)}" + (f" deleted={deleted}" if deleted else ""))
    blockers = payload.get("blocked_reasons")
    if isinstance(blockers, list) and blockers:
        click.echo("Blocked:")
        for reason in blockers:
            click.echo(f"  {reason}")
    tick = payload.get("dispatcher_tick")
    if isinstance(tick, dict):
        click.echo(
            "Dispatcher tick: "
            f"scanned={tick.get('scanned', 0)} "
            f"executed={tick.get('executed', 0)} "
            f"skipped={tick.get('skipped', 0)}"
        )
    if payload.get("dry_run"):
        click.echo("Dry run: no changes made")


def _open_database() -> LocalDatabase:
    """Open the hub database and apply pending migrations before build storage use."""
    db = LocalDatabase()
    try:
        run_migrations(db)
    except Exception:
        db.close()
        raise
    return db


@click.command("build")
@click.argument("input_ref", required=False, metavar="[INPUT|ACTION]")
@click.argument("target_ref", required=False, metavar="[REF]")
@click.option("--quick", is_flag=True, default=False, help="Run one lifecycle step.")
@click.option(
    "--skip-stage", multiple=True, help="Stage to skip. May be repeated or comma-separated."
)
@click.option(
    "--stage",
    "stage_cap",
    multiple=True,
    help="Stage selector/cap override, e.g. development:max_review_rounds=4.",
)
@click.option("--clone", "use_clone", is_flag=True, default=False, help="Use clone workspaces.")
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
    type=int,
    help="Maximum active automation agents allowed during the immediate dispatcher tick.",
)
@click.option("--dry-run", is_flag=True, default=False, help="Preview clean/restart effects.")
@click.option("--force", is_flag=True, default=False, help="Force destructive cleanup.")
@click.option("--yes", is_flag=True, default=False, help="Confirm destructive clean/restart.")
def build_command(
    input_ref: str | None,
    target_ref: str | None,
    quick: bool,
    skip_stage: tuple[str, ...],
    stage_cap: tuple[str, ...],
    use_clone: bool,
    no_merge: bool,
    pr: str | None,
    target_branch: str | None,
    assigned_agent: str | None,
    reset_expansion_output: bool,
    max_active_agents: int | None,
    dry_run: bool,
    force: bool,
    yes: bool,
) -> None:
    """Start lifecycle automation from a plan file or task reference."""
    if input_ref == "stop":
        _run_build_stop(target_ref)
        return
    if input_ref == "resume":
        _run_build_resume(target_ref)
        return
    if input_ref == "clean":
        _run_build_clean(target_ref, dry_run=dry_run, force=force, yes=yes)
        return
    if input_ref == "restart":
        _run_build_restart(target_ref, dry_run=dry_run, force=force, yes=yes)
        return
    if input_ref is None:
        invoke_build_skill()
        return
    if target_ref is not None:
        raise click.ClickException(f"Unexpected build argument: {target_ref}")

    opts = BuildOptions(
        quick=quick,
        skip_stages=_parse_skip_stages(skip_stage),
        isolation="clone" if use_clone else "worktree",
        isolation_explicit=use_clone,
        no_merge=no_merge,
        pr=pr,
        stage_caps=_parse_stage_cap(stage_cap),
        target_branch=target_branch,
        assigned_agent=assigned_agent,
        reset_expansion_output=reset_expansion_output,
        max_active_agents=max_active_agents,
    )
    project_id = resolve_project_id()
    result = _try_daemon_build(input_ref, opts)
    if result is None:
        db = _open_database()
        try:
            result = asyncio.run(build(input_ref, opts, db=db, project_id=project_id))
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        finally:
            db.close()

    _echo_build_result(result)


@click.command("stop")
@click.argument("input_ref", required=False, metavar="[REF]")
def build_stop_command(input_ref: str | None) -> None:
    """Stop future dispatcher build ticks."""
    _run_build_stop(input_ref)


@click.command("resume")
@click.argument("input_ref", required=False, metavar="[REF]")
def build_resume_command(input_ref: str | None) -> None:
    """Resume dispatcher build ticks."""
    _run_build_resume(input_ref)


def _run_build_stop(input_ref: str | None = None) -> None:
    project_id = resolve_project_id()
    db = _open_database()
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
    finally:
        db.close()

    if payload is not None:
        _echo_target_control_result(payload)
    elif result is not None:
        _echo_build_control_result(result)


def _run_build_resume(input_ref: str | None = None) -> None:
    project_id = resolve_project_id()
    db = _open_database()
    try:
        if input_ref is None:
            result = build_resume(db=db, project_id=project_id)
            payload: dict[str, object] | None = None
        else:
            target_result = asyncio.run(
                build_resume_target(input_ref, db=db, project_id=project_id)
            )
            result = None
            payload = asdict(target_result)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        db.close()

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
    yes: bool,
) -> None:
    if input_ref is None:
        raise click.ClickException("gobby build clean requires a task ref")
    if not _confirm_destructive("clean", input_ref, yes, dry_run):
        return
    project_id = resolve_project_id()
    db = _open_database()
    try:
        result = asyncio.run(
            build_clean_target(
                input_ref,
                db=db,
                project_id=project_id,
                dry_run=dry_run,
                force=force,
                yes=True,
            )
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        db.close()
    _echo_target_control_result(asdict(result))


def _run_build_restart(
    input_ref: str | None,
    *,
    dry_run: bool,
    force: bool,
    yes: bool,
) -> None:
    if input_ref is None:
        raise click.ClickException("gobby build restart requires a task ref")
    if not _confirm_destructive("restart", input_ref, yes, dry_run):
        return
    project_id = resolve_project_id()
    db = _open_database()
    try:
        result = asyncio.run(
            build_restart_target(
                input_ref,
                db=db,
                project_id=project_id,
                dry_run=dry_run,
                force=force,
                yes=True,
            )
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        db.close()
    _echo_target_control_result(asdict(result))
