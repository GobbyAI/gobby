"""
CLI commands for managing cron jobs.
"""

import json
from datetime import datetime
from typing import Any, Literal, cast

import click

from gobby.cli.utils import resolve_project_ref
from gobby.storage.cron import CronJobStorage
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.hub.runtime import open_runtime_hub_database


def get_cron_storage() -> tuple[HubDatabase, CronJobStorage]:
    """Get initialized cron storage."""
    db = open_runtime_hub_database(apply_migrations=False)
    return db, CronJobStorage(db)


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
    jobs = storage.list_jobs(project_id=project_id, enabled=enabled)

    if json_format:
        click.echo(json.dumps([j.to_dict() for j in jobs], indent=2, default=str))
        return

    if not jobs:
        click.echo("No cron jobs found.")
        return

    click.echo(f"Found {len(jobs)} cron job(s):\n")
    for job in jobs:
        status_icon = "●" if job.enabled else "○"
        schedule = job.cron_expr or f"every {job.interval_seconds}s" or job.run_at or "?"
        last = job.last_status or "never"
        click.echo(f"  {status_icon} {job.id}  {job.name:<30} {schedule:<20} last: {last}")


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
@click.option("--timezone", "tz", default="UTC", help="Timezone (default: UTC)")
@click.option("--description", "-d", help="Job description")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def add_job(
    name: str,
    schedule: str,
    action_type: str,
    action_config: str,
    project_ref: str | None,
    tz: str,
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

    # Parse schedule: detect interval vs cron expression
    schedule_type: Literal["cron", "interval", "once"] = "cron"
    cron_expr = None
    interval_seconds = None

    schedule_normalized = schedule.strip().lower()
    multipliers = {"s": 1, "m": 60, "h": 3600}
    suffix = schedule_normalized[-1:] if schedule_normalized else ""
    if suffix in multipliers and schedule_normalized[:-1].isdigit():
        schedule_type = "interval"
        interval_seconds = int(schedule_normalized[:-1]) * multipliers[suffix]
    else:
        schedule_type = "cron"
        cron_expr = schedule

    _, storage = get_cron_storage()
    job = storage.create_job(
        project_id=project_id,
        name=name,
        schedule_type=cast(Literal["cron", "interval", "once"], schedule_type),
        action_type=cast(Literal["agent_spawn", "pipeline", "shell", "handler"], action_type),
        action_config=config,
        cron_expr=cron_expr,
        interval_seconds=interval_seconds,
        timezone=tz,
        description=description,
    )

    if json_format:
        click.echo(json.dumps(job.to_dict(), indent=2, default=str))
        return

    click.echo(f"Created cron job: {job.id}")
    click.echo(f"  Name: {job.name}")
    click.echo(f"  Schedule: {cron_expr or f'every {interval_seconds}s'}")
    click.echo(f"  Action: {action_type}")


@cron.command("run")
@click.argument("job_id")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def run_job(job_id: str, json_format: bool) -> None:
    """Trigger immediate execution of a cron job."""
    _, storage = get_cron_storage()
    job = storage.get_job(job_id)
    if not job:
        click.echo(f"Job not found: {job_id}", err=True)
        raise SystemExit(1)

    run = storage.create_run(job.id)

    if json_format:
        click.echo(json.dumps(run.to_dict(), indent=2, default=str))
        return

    click.echo(f"Triggered run {run.id} for job {job.name}")


@cron.command("toggle")
@click.argument("job_id")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def toggle_job(job_id: str, json_format: bool) -> None:
    """Toggle a cron job enabled/disabled."""
    _, storage = get_cron_storage()
    job = storage.toggle_job(job_id)
    if not job:
        click.echo(f"Job not found: {job_id}", err=True)
        raise SystemExit(1)

    if json_format:
        click.echo(json.dumps(job.to_dict(), indent=2, default=str))
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
    job = storage.get_job(job_id)
    if not job:
        click.echo(f"Job not found: {job_id}", err=True)
        raise SystemExit(1)

    runs = storage.list_runs(job_id, limit=limit)

    if json_format:
        click.echo(json.dumps([r.to_dict() for r in runs], indent=2, default=str))
        return

    if not runs:
        click.echo(f"No runs found for job {job.name}.")
        return

    click.echo(f"Runs for {job.name} ({len(runs)}):\n")
    for run in runs:
        status_icon = {"completed": "✓", "failed": "✗", "running": "→", "pending": "○"}.get(
            run.status, "?"
        )
        duration = ""
        if run.started_at and run.completed_at:
            start = datetime.fromisoformat(run.started_at)
            end = datetime.fromisoformat(run.completed_at)
            secs = (end - start).total_seconds()
            duration = f" ({secs:.1f}s)"
        click.echo(f"  {status_icon} {run.id}  {run.status:<12} {run.triggered_at}{duration}")


@cron.command("remove")
@click.argument("job_id")
@click.confirmation_option(prompt="Are you sure you want to remove this cron job?")
def remove_job(job_id: str) -> None:
    """Remove a cron job."""
    _, storage = get_cron_storage()
    success = storage.delete_job(job_id)
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
        if schedule.endswith("s") and schedule[:-1].isdigit():
            kwargs["schedule_type"] = "interval"
            kwargs["interval_seconds"] = int(schedule[:-1])
            kwargs["cron_expr"] = None
        else:
            kwargs["schedule_type"] = "cron"
            kwargs["cron_expr"] = schedule
            kwargs["interval_seconds"] = None

    if not kwargs:
        click.echo("No changes specified.", err=True)
        raise SystemExit(1)

    updated = storage.update_job(job_id, **kwargs)
    if not updated:
        click.echo(f"Failed to update job: {job_id}", err=True)
        raise SystemExit(1)

    if json_format:
        click.echo(json.dumps(updated.to_dict(), indent=2, default=str))
        return

    click.echo(f"Updated cron job: {updated.id} ({updated.name})")
