"""
CLI commands for Linear integration.

Provides commands for syncing gobby tasks with Linear issues.
"""

import asyncio
import json
import logging
from pathlib import Path

import click

from gobby.cli.tasks._utils import resolve_task_id
from gobby.integrations.linear import LinearIntegration
from gobby.mcp_proxy.manager import MCPClientManager
from gobby.storage.database import LocalDatabase
from gobby.storage.mcp import LocalMCPManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager
from gobby.sync.linear import LinearSyncService
from gobby.utils.project_context import get_project_context
from gobby.utils.project_init import update_project_json_fields

logger = logging.getLogger(__name__)


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def get_linear_deps() -> tuple[LocalTaskManager, MCPClientManager, LocalProjectManager, str]:
    """Get dependencies for Linear commands."""
    db = LocalDatabase()
    task_manager = LocalTaskManager(db)
    project_manager = LocalProjectManager(db)

    ctx = get_project_context(cwd=Path.cwd())
    if not ctx or not ctx.get("id"):
        raise click.ClickException("Not in a gobby project directory. Run 'gobby init' first.")

    project_id: str = ctx["id"]
    mcp_manager = _create_linear_mcp_manager(db, project_id)
    return task_manager, mcp_manager, project_manager, project_id


def _create_linear_mcp_manager(db: LocalDatabase, project_id: str) -> MCPClientManager:
    """Create an MCP manager with the same database-backed servers as the daemon."""
    return MCPClientManager(
        mcp_db_manager=LocalMCPManager(db),
        project_id=project_id,
    )


def get_sync_service(team_id: str | None = None) -> LinearSyncService:
    """Create LinearSyncService for CLI commands."""
    task_manager, mcp_manager, project_manager, project_id = get_linear_deps()
    project = project_manager.get(project_id)
    return LinearSyncService(
        mcp_manager=mcp_manager,
        task_manager=task_manager,
        project_id=project_id,
        linear_team_id=team_id or (_optional_str(project.linear_team_id) if project else None),
        linear_project_id=_optional_str(project.linear_project_id) if project else None,
        project_manager=project_manager,
    )


def _project_linear_name(project_name: str, repo_path: str | None) -> str:
    return Path(repo_path).name if repo_path else project_name


def _team_identifier(team: dict[str, object]) -> str:
    for key in ("id", "key", "identifier"):
        value = _optional_str(team.get(key))
        if value:
            return value
    return ""


def _team_key(team: dict[str, object]) -> str:
    return _optional_str(team.get("key")) or _optional_str(team.get("identifier")) or ""


def _select_team(teams: list[dict[str, object]], team_id: str | None) -> dict[str, object]:
    if not teams:
        raise click.ClickException("No Linear teams found for the configured Linear auth.")

    if team_id:
        for team in teams:
            if team_id in {_optional_str(team.get("id")), _team_key(team)}:
                return team
        raise click.ClickException(f"Linear team not found: {team_id}")

    if len(teams) == 1:
        return teams[0]

    raise click.ClickException(
        f"Found {len(teams)} Linear teams. Re-run with --team-id to select one."
    )


def _persist_linear_binding(
    project_manager: LocalProjectManager,
    project_id: str,
    team_id: str | None,
    linear_project_id: str | None,
) -> None:
    updated = project_manager.update(
        project_id,
        linear_team_id=team_id,
        linear_project_id=linear_project_id,
    )
    if updated and updated.repo_path:
        update_project_json_fields(
            Path(updated.repo_path),
            linear_team_id=team_id,
            linear_project_id=linear_project_id,
        )


def _linear_sync_job_name(project_id: str) -> str:
    return f"gobby:linear-sync:{project_id}"


def _linear_sync_handler_name(project_id: str) -> str:
    return f"linear_sync:{project_id}"


def _enable_linear_auto_sync(
    task_manager: LocalTaskManager,
    project_id: str,
    interval: int,
) -> str:
    from gobby.storage.cron import CronJobStorage

    cron_storage = CronJobStorage(task_manager.db)
    job_name = _linear_sync_job_name(project_id)
    handler_name = _linear_sync_handler_name(project_id)
    existing = cron_storage.get_job_by_name(job_name)

    if existing:
        cron_storage.update_job(
            existing.id,
            interval_seconds=interval,
            action_config={"handler": handler_name},
            enabled=1,
        )
        return existing.id

    job = cron_storage.create_job(
        project_id=project_id,
        name=job_name,
        description="Periodic bidirectional sync with Linear",
        schedule_type="interval",
        interval_seconds=interval,
        action_type="handler",
        action_config={"handler": handler_name},
        enabled=True,
    )
    return job.id


async def _run_linear_setup(
    task_manager: LocalTaskManager,
    mcp_manager: MCPClientManager,
    project_manager: LocalProjectManager,
    project_id: str,
    bootstrap: bool,
    team_id: str | None,
    linear_project_id: str | None,
    project_name: str | None,
    import_issues: bool,
    create_missing: bool,
) -> dict[str, object]:
    project = project_manager.get(project_id)
    if not project:
        raise click.ClickException(f"Project not found: {project_id}")

    service = LinearSyncService(
        mcp_manager=mcp_manager,
        task_manager=task_manager,
        project_id=project_id,
        linear_team_id=team_id,
        linear_project_id=linear_project_id,
        project_manager=project_manager,
    )

    teams = await service.list_teams()
    team = _select_team(teams, team_id)
    selected_team_id = _team_identifier(team)
    if not selected_team_id:
        raise click.ClickException("Selected Linear team did not include an id.")

    resolved_project_name = project_name or _project_linear_name(project.name, project.repo_path)
    if not bootstrap and not linear_project_id:
        raise click.ClickException("Pass --bootstrap to create/reuse a Linear project.")

    linear_project, created_project = await service.ensure_linear_project(
        selected_team_id,
        resolved_project_name,
        project_id=linear_project_id,
    )
    resolved_linear_project_id = _optional_str(linear_project.get("id"))
    if not resolved_linear_project_id:
        raise click.ClickException("Linear project setup did not return a project id.")

    service.linear_team_id = selected_team_id
    service.linear_project_id = resolved_linear_project_id
    _persist_linear_binding(
        project_manager,
        project_id,
        selected_team_id,
        resolved_linear_project_id,
    )

    imported = await service.import_linear_issues(team_id=selected_team_id) if import_issues else []
    created_issues = (
        await service.create_missing_issues(team_id=selected_team_id) if create_missing else []
    )
    sync_result = await service.sync_all(team_id=selected_team_id)

    return {
        "project_id": project_id,
        "linear_team_id": selected_team_id,
        "linear_project_id": resolved_linear_project_id,
        "linear_project_name": linear_project.get("name") or resolved_project_name,
        "created_linear_project": created_project,
        "imported_count": len(imported),
        "created_missing_count": len(created_issues),
        "sync": sync_result,
    }


@click.group()
def linear() -> None:
    """Linear integration commands."""
    pass


@linear.command("teams")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def linear_teams(json_format: bool) -> None:
    """List Linear teams available to the configured Linear auth."""
    try:
        task_manager, mcp_manager, project_manager, project_id = get_linear_deps()
        project = project_manager.get(project_id)
        service = LinearSyncService(
            mcp_manager=mcp_manager,
            task_manager=task_manager,
            project_id=project_id,
            linear_team_id=_optional_str(project.linear_team_id) if project else None,
            linear_project_id=_optional_str(project.linear_project_id) if project else None,
            project_manager=project_manager,
        )
        teams = asyncio.run(service.list_teams())

        if json_format:
            click.echo(json.dumps({"teams": teams, "count": len(teams)}, indent=2))
            return

        if not teams:
            click.echo("No Linear teams found.")
            return

        click.echo(f"Found {len(teams)} Linear team(s):")
        for team in teams:
            name = _optional_str(team.get("name")) or "(unnamed)"
            key = _team_key(team) or "-"
            team_id = _optional_str(team.get("id")) or "-"
            click.echo(f"  {name:<30} {key:<10} {team_id}")

    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from None


@linear.command("status")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def linear_status(json_format: bool) -> None:
    """Show Linear integration status."""
    try:
        task_manager, mcp_manager, project_manager, project_id = get_linear_deps()

        # Get project info
        project = project_manager.get(project_id)
        linear_team_id = _optional_str(project.linear_team_id) if project else None
        linear_project_id = _optional_str(project.linear_project_id) if project else None

        # Check Linear MCP availability
        linear = LinearIntegration(mcp_manager)
        available = linear.is_available()
        unavailable_reason = linear.get_unavailable_reason() if not available else None

        # Count linked tasks
        row = task_manager.db.fetchone(
            "SELECT COUNT(*) as count FROM tasks WHERE project_id = ? AND linear_issue_id IS NOT NULL",
            (project_id,),
        )
        linked_count = row["count"] if row else 0

        if json_format:
            click.echo(
                json.dumps(
                    {
                        "project_id": project_id,
                        "linear_team_id": linear_team_id,
                        "linear_project_id": linear_project_id,
                        "linear_available": available,
                        "unavailable_reason": unavailable_reason,
                        "linked_tasks_count": linked_count,
                    },
                    indent=2,
                )
            )
        else:
            click.echo("Linear Integration Status")
            click.echo("=" * 40)
            click.echo(f"Project ID: {project_id}")
            click.echo(f"Linked team: {linear_team_id or '(not linked)'}")
            click.echo(f"Linked project: {linear_project_id or '(not linked)'}")
            click.echo(f"Linear MCP available: {'✓' if available else '✗'}")
            if not available:
                click.echo(f"  Reason: {unavailable_reason}")
            click.echo(f"Linked tasks: {linked_count}")

    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from None


@linear.command("link")
@click.argument("team_id")
def linear_link(team_id: str) -> None:
    """Link a Linear team to this project.

    TEAM_ID is the Linear team identifier (e.g., 'ENG-123' or UUID).
    """
    try:
        _, _, project_manager, project_id = get_linear_deps()

        _persist_linear_binding(project_manager, project_id, team_id, None)
        click.echo(f"✓ Linked project to Linear team: {team_id}")

    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from None


@linear.command("unlink")
def linear_unlink() -> None:
    """Remove Linear team link from this project."""
    try:
        _, _, project_manager, project_id = get_linear_deps()

        _persist_linear_binding(project_manager, project_id, None, None)
        click.echo("✓ Unlinked Linear team and project from project")

    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from None


@linear.command("setup")
@click.option("--bootstrap", is_flag=True, help="Create or reuse a Linear project by name")
@click.option("--team-id", help="Linear team ID")
@click.option("--project-id", "linear_project_id", help="Existing Linear project ID")
@click.option("--project-name", help="Linear project name to create or reuse")
@click.option("--import", "import_issues", is_flag=True, help="Import Linear project issues")
@click.option("--create-missing", is_flag=True, help="Create Linear issues for unlinked tasks")
@click.option("--auto-sync", is_flag=True, help="Enable periodic Linear sync")
@click.option("--interval", default=300, show_default=True, help="Auto-sync interval in seconds")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def linear_setup(
    bootstrap: bool,
    team_id: str | None,
    linear_project_id: str | None,
    project_name: str | None,
    import_issues: bool,
    create_missing: bool,
    auto_sync: bool,
    interval: int,
    json_format: bool,
) -> None:
    """Set up this Gobby project for project-scoped Linear sync."""
    try:
        task_manager, mcp_manager, project_manager, project_id = get_linear_deps()
        result = asyncio.run(
            _run_linear_setup(
                task_manager=task_manager,
                mcp_manager=mcp_manager,
                project_manager=project_manager,
                project_id=project_id,
                bootstrap=bootstrap,
                team_id=team_id,
                linear_project_id=linear_project_id,
                project_name=project_name,
                import_issues=import_issues,
                create_missing=create_missing,
            )
        )

        auto_sync_job_id = None
        if auto_sync:
            auto_sync_job_id = _enable_linear_auto_sync(task_manager, project_id, interval)
            result["auto_sync_job_id"] = auto_sync_job_id
            result["auto_sync_interval"] = interval

        if json_format:
            click.echo(json.dumps(result, indent=2, default=str))
            return

        click.echo("✓ Linear setup complete")
        click.echo(f"  Team: {result['linear_team_id']}")
        click.echo(f"  Project: {result['linear_project_name']} ({result['linear_project_id']})")
        click.echo(f"  Imported issues: {result['imported_count']}")
        click.echo(f"  Created missing issues: {result['created_missing_count']}")
        if auto_sync_job_id:
            click.echo(f"  Auto-sync: enabled every {interval}s ({auto_sync_job_id})")

    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from None


@linear.command("import")
@click.argument("team_id", required=False)
@click.option("--state", help="Issue state filter (e.g., 'Todo', 'In Progress')")
@click.option("--labels", help="Comma-separated labels to filter issues")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def linear_import(
    team_id: str | None, state: str | None, labels: str | None, json_format: bool
) -> None:
    """Import Linear issues as gobby tasks.

    If TEAM_ID is not specified, uses the linked team.
    """
    try:
        task_manager, mcp_manager, project_manager, project_id = get_linear_deps()

        # Get team from argument or project config
        project = project_manager.get(project_id)
        if not team_id:
            team_id = _optional_str(project.linear_team_id) if project else None
            if not team_id:
                raise click.ClickException(
                    "No team specified and project not linked to a Linear team. "
                    "Use 'gobby linear setup --bootstrap' first or specify the team."
                )

        service = LinearSyncService(
            mcp_manager=mcp_manager,
            task_manager=task_manager,
            project_id=project_id,
            linear_team_id=team_id,
            linear_project_id=_optional_str(project.linear_project_id) if project else None,
            project_manager=project_manager,
        )

        # Run async import
        label_list = labels.split(",") if labels else None
        tasks = asyncio.run(
            service.import_linear_issues(team_id=team_id, state=state, labels=label_list)
        )

        if json_format:
            click.echo(json.dumps({"tasks": tasks, "count": len(tasks)}, indent=2))
        else:
            click.echo(f"✓ Imported {len(tasks)} issues from Linear team {team_id}")
            for task in tasks:
                click.echo(f"  - {task.get('id', 'unknown')}: {task.get('title', 'Untitled')}")

    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from None


@linear.command("sync")
@click.argument("task_id")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def linear_sync(task_id: str, json_format: bool) -> None:
    """Sync a task to its linked Linear issue.

    Updates the Linear issue title, description, status, and priority to match the task.
    """
    try:
        task_manager, _, _, _ = get_linear_deps()
        resolved = resolve_task_id(task_manager, task_id)
        if not resolved:
            return

        service = get_sync_service()
        result = asyncio.run(service.sync_task_to_linear(resolved.id))

        if json_format:
            click.echo(json.dumps(result, indent=2))
        else:
            click.echo(f"✓ Synced task {task_id} to Linear")

    except click.ClickException:
        raise
    except ValueError as e:
        raise click.ClickException(str(e)) from None
    except Exception as e:
        raise click.ClickException(str(e)) from None


@linear.command("sync-all")
@click.argument("team_id", required=False)
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def linear_sync_all(team_id: str | None, json_format: bool) -> None:
    """Bidirectional sync between gobby and Linear.

    Pulls updates from Linear first, then pushes dirty gobby tasks back.
    If TEAM_ID is not specified, uses the linked team.
    """
    try:
        _, _, project_manager, project_id = get_linear_deps()

        if not team_id:
            project = project_manager.get(project_id)
            team_id = _optional_str(project.linear_team_id) if project else None
            if not team_id:
                raise click.ClickException(
                    "No team specified and project not linked to a Linear team. "
                    "Use 'gobby linear setup --bootstrap' first or specify the team."
                )

        service = get_sync_service(team_id)
        result = asyncio.run(service.sync_all(team_id=team_id))

        pull = result["pull"]
        push = result["push"]

        if json_format:
            click.echo(json.dumps(result, indent=2))
        else:
            click.echo("✓ Linear sync complete")
            click.echo(
                f"  Pull: {pull['updated']} updated, "
                f"{pull['skipped']} skipped, "
                f"{pull['errors']} errors"
            )
            click.echo(
                f"  Push: {push['pushed']} pushed, "
                f"{push['skipped']} skipped, "
                f"{push['errors']} errors"
            )

    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from None


@linear.command("auto-sync")
@click.option("--interval", default=300, show_default=True, help="Sync interval in seconds")
@click.option("--disable", is_flag=True, help="Disable the existing auto-sync job")
def linear_auto_sync(interval: int, disable: bool) -> None:
    """Create or manage a cron job for periodic Linear sync.

    Creates an interval-based project cron job that triggers bidirectional sync
    on the given interval. Use --disable to turn it off.
    """
    try:
        task_manager, _, project_manager, project_id = get_linear_deps()
        project = project_manager.get(project_id)
        if not project or not _optional_str(project.linear_team_id):
            raise click.ClickException(
                "Project is not linked to Linear. Run 'gobby linear setup --bootstrap' first."
            )

        from gobby.storage.cron import CronJobStorage

        cron_storage = CronJobStorage(task_manager.db)

        job_name = _linear_sync_job_name(project_id)
        existing = cron_storage.get_job_by_name(job_name)

        if disable:
            if not existing:
                raise click.ClickException("No auto-sync job found to disable.")
            cron_storage.update_job(existing.id, enabled=0)
            click.echo("✓ Disabled Linear auto-sync job")
            return

        if existing:
            _enable_linear_auto_sync(task_manager, project_id, interval)
            click.echo(f"✓ Updated Linear auto-sync job: interval={interval}s (id={existing.id})")
        else:
            job_id = _enable_linear_auto_sync(task_manager, project_id, interval)
            click.echo(f"✓ Created Linear auto-sync job: interval={interval}s (id={job_id})")

    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from None


@linear.command("create")
@click.argument("task_id")
@click.option("--team", "team_id", help="Linear team ID")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def linear_create(task_id: str, team_id: str | None, json_format: bool) -> None:
    """Create a Linear issue from a gobby task."""
    try:
        task_manager, _, _, _ = get_linear_deps()
        resolved = resolve_task_id(task_manager, task_id)
        if not resolved:
            return

        service = get_sync_service(team_id)
        result = asyncio.run(service.create_issue_for_task(task_id=resolved.id, team_id=team_id))

        if json_format:
            click.echo(json.dumps(result, indent=2))
        else:
            gobby_ref = result.get("gobby_ref") or task_id
            linear_key = (
                result.get("linear_identifier") or result.get("identifier") or result.get("id")
            )
            project_name = result.get("linear_project_name") or result.get("linear_project_id")
            target = f"Linear project {project_name}" if project_name else "Linear"
            suffix = f" (Linear {linear_key})" if linear_key else ""
            click.echo(f"✓ Registered {gobby_ref} in {target}{suffix}")

    except click.ClickException:
        raise
    except ValueError as e:
        raise click.ClickException(str(e)) from None
    except Exception as e:
        raise click.ClickException(str(e)) from None
