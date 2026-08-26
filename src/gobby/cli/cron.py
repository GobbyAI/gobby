"""
CLI commands for managing cron jobs.
"""

import json
from datetime import datetime
from typing import Any, Literal, NamedTuple, cast
from uuid import UUID

import click
import httpx
from croniter import croniter

from gobby.cli._build_daemon import _daemon_error_detail, _daemon_error_message
from gobby.cli.runtime import require_cli_database
from gobby.cli.utils import resolve_project_ref
from gobby.cli.utils_config import get_daemon_client
from gobby.storage.cron import CronJobStorage
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.daemon_client import DaemonClient
from gobby.utils.datetime import datetime_to_local_iso
from gobby.utils.json_helpers import json_dumps


def get_cron_storage() -> tuple[HubDatabase, CronJobStorage]:
    """Get initialized cron storage."""
    db = require_cli_database()
    return db, CronJobStorage(db)


def _resolve_job_id(storage: CronJobStorage, job_ref: str) -> str:
    """Resolve a job reference (uuid or name) to the job's uuid.

    The cron_jobs id column is a uuid; passing a name straight through
    crashes Postgres with an invalid-uuid cast, so non-uuid refs resolve
    by name first.
    """
    try:
        UUID(job_ref)
        return job_ref
    except ValueError:
        pass
    job = storage.get_job_by_name(job_ref)
    if not job:
        click.echo(f"Job not found: {job_ref}", err=True)
        raise SystemExit(1)
    return str(job.id)


def _get_daemon_client(_ctx: click.Context) -> DaemonClient:
    return get_daemon_client()


class ParsedSchedule(NamedTuple):
    schedule_type: Literal["cron", "interval"]
    cron_expr: str | None
    interval_seconds: int | None


def _parse_schedule(schedule: str) -> ParsedSchedule:
    schedule_normalized = schedule.strip().lower()
    multipliers = {"s": 1, "m": 60, "h": 3600}
    suffix = schedule_normalized[-1:] if schedule_normalized else ""
    if suffix in multipliers and schedule_normalized[:-1].isdigit():
        return ParsedSchedule("interval", None, int(schedule_normalized[:-1]) * multipliers[suffix])

    try:
        croniter(schedule, datetime.now())
    except (ValueError, KeyError) as e:
        raise click.ClickException(f"Invalid cron schedule: {schedule}") from e
    return ParsedSchedule("cron", schedule, None)


@click.group()
def cron() -> None:
    """Manage cron jobs."""
    pass


@cron.command("list")
@click.option("--project", "-p", "project_ref", help="Filter by project (name or UUID)")
@click.option("--enabled/--disabled", default=None, help="Filter by enabled state")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def list_jobs(
    project_ref: str | None,
    enabled: bool | None,
    json_format: bool,
) -> None:
    """List cron jobs."""
    project_id = resolve_project_ref(project_ref) if project_ref else None
    _, storage = get_cron_storage()
    jobs = storage.list_jobs(
        project_id=project_id,
        enabled=enabled,
        exclude_removed_automation=True,
    )

    if json_format:
        click.echo(json_dumps([j.to_dict() for j in jobs], indent=2, default=str))
        return

    if not jobs:
        click.echo("No cron jobs found.")
        return

    click.echo(f"Found {len(jobs)} cron job(s):\n")
    for job in jobs:
        status_icon = "●" if job.enabled else "○"
        if job.schedule_type == "cron":
            # A bare expression is ambiguous without the zone it is read in.
            schedule = f"{job.cron_expr} {job.timezone}" if job.cron_expr else "?"
        elif job.schedule_type == "interval":
            schedule = f"every {job.interval_seconds}s" if job.interval_seconds else "?"
        else:
            schedule = datetime_to_local_iso(job.run_at) or "?"
        last = job.last_status or "never"
        click.echo(f"  {status_icon} {job.id}  {job.name:<30} {schedule:<32} last: {last}")


@cron.command("add")
@click.option("--name", "-n", required=True, help="Job name")
@click.option(
    "--schedule",
    "-s",
    required=True,
    help="Cron expression (e.g., '0 7 * * *') or interval (e.g., '300s')",
)
@click.option(
    "--action-type",
    "-t",
    required=True,
    type=click.Choice(["shell", "agent_spawn", "pipeline", "handler"]),
    help="Action type",
)
@click.option("--action-config", "-c", required=True, help="Action config as JSON string")
@click.option("--project", "-p", "project_ref", help="Project (name or UUID)")
@click.option(
    "--timezone",
    "tz",
    default=None,
    help="Schedule timezone (default: the host's local zone)",
)
@click.option("--description", "-d", help="Job description")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def add_job(
    name: str,
    schedule: str,
    action_type: str,
    action_config: str,
    project_ref: str | None,
    tz: str | None,
    description: str | None,
    json_format: bool,
) -> None:
    """Add a new cron job."""
    project_id = resolve_project_ref(project_ref, exit_on_not_found=False) or ""

    try:
        config = json.loads(action_config)
    except json.JSONDecodeError as e:
        click.echo(f"Invalid JSON for --action-config: {e}", err=True)
        raise SystemExit(1) from None

    parsed_schedule = _parse_schedule(schedule)

    _, storage = get_cron_storage()
    job = storage.create_job(
        project_id=project_id,
        name=name,
        schedule_type=cast(Literal["cron", "interval", "once"], parsed_schedule.schedule_type),
        action_type=cast(Literal["agent_spawn", "pipeline", "shell", "handler"], action_type),
        action_config=config,
        cron_expr=parsed_schedule.cron_expr,
        interval_seconds=parsed_schedule.interval_seconds,
        timezone=tz,
        description=description,
    )

    if json_format:
        click.echo(json_dumps(job.to_dict(), indent=2, default=str))
        return

    click.echo(f"Created cron job: {job.id}")
    click.echo(f"  Name: {job.name}")
    schedule_label = (
        f"{parsed_schedule.cron_expr} {job.timezone}"
        if parsed_schedule.cron_expr
        else f"every {parsed_schedule.interval_seconds}s"
    )
    click.echo(f"  Schedule: {schedule_label}")
    click.echo(f"  Action: {action_type}")


@cron.command("run")
@click.argument("job_id")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def run_job(ctx: click.Context, job_id: str, json_format: bool) -> None:
    """Trigger immediate execution of a cron job."""
    _, storage = get_cron_storage()
    job_id = _resolve_job_id(storage, job_id)
    client = _get_daemon_client(ctx)
    try:
        response = client.call_http_api(
            f"/api/cron/jobs/{job_id}/run",
            method="POST",
        )
    except httpx.HTTPError as exc:
        raise click.ClickException(f"Daemon unavailable: {exc}") from exc

    if response.status_code != 200:
        raise click.ClickException(_daemon_error_message(_daemon_error_detail(response)))

    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("run"), dict):
        raise click.ClickException("Daemon returned an invalid cron run response")
    run = cast(dict[str, Any], payload["run"])

    if json_format:
        click.echo(json_dumps(run, indent=2, default=str))
        return

    click.echo(f"Triggered run {run.get('id', '<unknown>')} for job {job_id}")


@cron.command("toggle")
@click.argument("job_id")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def toggle_job(job_id: str, json_format: bool) -> None:
    """Toggle a cron job enabled/disabled."""
    _, storage = get_cron_storage()
    job = storage.toggle_job(_resolve_job_id(storage, job_id))
    if not job:
        click.echo(f"Job not found: {job_id}", err=True)
        raise SystemExit(1)

    if json_format:
        click.echo(json_dumps(job.to_dict(), indent=2, default=str))
        return

    state = "enabled" if job.enabled else "disabled"
    click.echo(f"Job {job.id} ({job.name}) is now {state}")


@cron.command("runs")
@click.argument("job_id")
@click.option("--limit", "-n", default=20, help="Max runs to show")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def list_runs(job_id: str, limit: int, json_format: bool) -> None:
    """Show run history for a cron job."""
    _, storage = get_cron_storage()
    job_id = _resolve_job_id(storage, job_id)
    job = storage.get_job(job_id)
    if not job:
        click.echo(f"Job not found: {job_id}", err=True)
        raise SystemExit(1)

    runs = storage.list_runs(job_id, limit=limit)

    if json_format:
        click.echo(json_dumps([r.to_dict() for r in runs], indent=2, default=str))
        return

    if not runs:
        click.echo(f"No runs found for job {job.name}.")
        return

    click.echo(f"Runs for {job.name} ({len(runs)}):\n")
    for run in runs:
        status_icon = {
            "completed": "✓",
            "failed": "✗",
            "running": "→",
            "pending": "○",
            "interrupted": "↯",
        }.get(run.status, "?")
        duration = ""
        if run.started_at and run.completed_at:
            secs = (run.completed_at - run.started_at).total_seconds()
            duration = f" ({secs:.1f}s)"
        triggered_at = datetime_to_local_iso(run.triggered_at) or "?"
        click.echo(f"  {status_icon} {run.id}  {run.status:<12} {triggered_at}{duration}")


@cron.command("remove")
@click.argument("job_id")
@click.confirmation_option(prompt="Are you sure you want to remove this cron job?")
def remove_job(job_id: str) -> None:
    """Remove a cron job."""
    _, storage = get_cron_storage()
    success = storage.delete_job(_resolve_job_id(storage, job_id))
    if success:
        click.echo(f"Removed cron job: {job_id}")
    else:
        click.echo(f"Job not found: {job_id}", err=True)
        raise SystemExit(1)


@cron.command("edit")
@click.argument("job_id")
@click.option("--name", "-n", help="New name")
@click.option("--schedule", "-s", help="New schedule")
@click.option("--enabled/--disabled", default=None, help="Set enabled state")
@click.option("--action-config", "-c", help="New action config (JSON)")
@click.option("--description", "-d", help="New description")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def edit_job(
    job_id: str,
    name: str | None,
    schedule: str | None,
    enabled: bool | None,
    action_config: str | None,
    description: str | None,
    json_format: bool,
) -> None:
    """Edit a cron job's configuration."""
    _, storage = get_cron_storage()
    job_id = _resolve_job_id(storage, job_id)
    job = storage.get_job(job_id)
    if not job:
        click.echo(f"Job not found: {job_id}", err=True)
        raise SystemExit(1)

    kwargs: dict[str, Any] = {}
    if name is not None:
        kwargs["name"] = name
    if description is not None:
        kwargs["description"] = description
    if enabled is not None:
        kwargs["enabled"] = enabled
    if action_config is not None:
        try:
            kwargs["action_config"] = json.loads(action_config)
        except json.JSONDecodeError as e:
            click.echo(f"Invalid JSON for --action-config: {e}", err=True)
            raise SystemExit(1) from None

    if schedule is not None:
        parsed_schedule = _parse_schedule(schedule)
        kwargs["schedule_type"] = parsed_schedule.schedule_type
        kwargs["cron_expr"] = parsed_schedule.cron_expr
        kwargs["interval_seconds"] = parsed_schedule.interval_seconds

    if not kwargs:
        click.echo("No changes specified.", err=True)
        raise SystemExit(1)

    updated = storage.update_job(job_id, **kwargs)
    if not updated:
        click.echo(f"Failed to update job: {job_id}", err=True)
        raise SystemExit(1)

    if json_format:
        click.echo(json_dumps(updated.to_dict(), indent=2, default=str))
        return

    click.echo(f"Updated cron job: {updated.id} ({updated.name})")
