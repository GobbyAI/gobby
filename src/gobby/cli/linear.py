"""
CLI commands for Linear integration.

Provides commands for syncing gobby tasks with Linear issues.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any

import click

from gobby.cli.runtime import require_cli_database, resolve_cli_project
from gobby.cli.tasks._utils import resolve_task_id
from gobby.mcp_proxy.manager import MCPClientManager
from gobby.storage.external_issue_sync import ExternalIssueSyncStatusStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.mcp import LocalMCPManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager
from gobby.sync.linear import LinearSyncService
from gobby.utils.json_helpers import json_dumps
from gobby.utils.project_init import update_project_json_fields

logger = logging.getLogger(__name__)


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def get_linear_deps(
    project_ref: str | None = None,
    *,
    require_project: bool = True,
) -> tuple[LocalTaskManager, MCPClientManager, LocalProjectManager, str]:
    """Get dependencies for Linear commands."""
    db = require_cli_database()
    task_manager = LocalTaskManager(db)
    project_manager = LocalProjectManager(db)
    project_id = resolve_cli_project(
        project_manager,
        project_ref,
        require_project=require_project,
    )
    mcp_manager = _create_linear_mcp_manager(db, project_id)
    return task_manager, mcp_manager, project_manager, project_id


def _create_linear_mcp_manager(db: HubDatabase, project_id: str) -> MCPClientManager:
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
    *,
    enabled: bool | None = None,
) -> None:
    fields: dict[str, Any] = {
        "linear_team_id": team_id,
        "linear_project_id": linear_project_id,
    }
    if enabled is not None:
        fields["linear_sync_enabled"] = enabled
    updated = project_manager.update(project_id, **fields)
    if updated and updated.repo_path:
        json_fields = dict(fields)
        update_project_json_fields(Path(updated.repo_path), **json_fields)


async def _run_linear_setup(
    task_manager: LocalTaskManager,
    mcp_manager: MCPClientManager,
    project_manager: LocalProjectManager,
    project_id: str,
    bootstrap: bool,
    team_id: str | None,
    linear_project_id: str | None,
    project_name: str | None,
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
        enabled=True,
    )

    return {
        "project_id": project_id,
        "linear_team_id": selected_team_id,
        "linear_project_id": resolved_linear_project_id,
        "linear_project_name": linear_project.get("name") or resolved_project_name,
        "created_linear_project": created_project,
        "linear_sync_enabled": True,
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
            click.echo(json_dumps({"teams": teams, "count": len(teams)}, indent=2))
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
@click.option("--project", "project_ref", help="Gobby project name or UUID")
@click.option("--all", "all_projects", is_flag=True, help="Show every registered project")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def linear_status(project_ref: str | None, all_projects: bool, json_format: bool) -> None:
    """Show Linear integration status."""
    try:
        if project_ref and all_projects:
            raise click.ClickException("Use either --project or --all, not both.")
        task_manager, _, project_manager, default_project_id = get_linear_deps(
            project_ref,
            require_project=not all_projects,
        )
        selected = project_manager.get(default_project_id) if default_project_id else None
        projects = project_manager.list() if all_projects else ([selected] if selected else [])
        status_store = ExternalIssueSyncStatusStore(task_manager.db)
        payloads: list[dict[str, object]] = []
        for project in projects:
            if project is None or project.deleted_at:
                continue
            mcp_manager = _create_linear_mcp_manager(task_manager.db, project.id)
            service = LinearSyncService(
                mcp_manager=mcp_manager,
                task_manager=task_manager,
                project_id=project.id,
                linear_team_id=project.linear_team_id,
                linear_project_id=project.linear_project_id,
                project_manager=project_manager,
            )
            status = status_store.get(project.id, "linear")
            linked, pending = status_store.counts(project.id, "linear")
            available = service.is_available()
            payloads.append(
                {
                    "project_id": project.id,
                    "project_name": project.name,
                    "enabled": project.linear_sync_enabled,
                    "linear_team_id": project.linear_team_id,
                    "linear_project_id": project.linear_project_id,
                    "ready": bool(
                        project.linear_team_id and project.linear_project_id and available
                    ),
                    "unavailable_reason": (
                        service.get_unavailable_reason() if not available else None
                    ),
                    "state": status.state if status else "pending",
                    "linked_count": linked,
                    "pending_count": pending,
                    "last_attempt_at": status.last_attempt_at if status else None,
                    "last_success_at": status.last_success_at if status else None,
                    "retry_at": status.retry_at if status else None,
                    "last_statistics": status.last_statistics if status else {},
                    "consecutive_failures": status.consecutive_failures if status else 0,
                    "last_error": status.last_error if status else None,
                }
            )

        if json_format:
            output = payloads if all_projects or not payloads else payloads[0]
            click.echo(json_dumps(output, indent=2, default=str))
        else:
            for index, payload in enumerate(payloads):
                if index:
                    click.echo()
                click.echo(f"Linear: {payload['project_name']} ({payload['project_id']})")
                click.echo(f"Enabled: {'✓' if payload['enabled'] else '✗'}")
                click.echo(f"Ready: {'✓' if payload['ready'] else '✗'}")
                click.echo(f"State: {payload['state']}")
                click.echo(f"Team: {payload['linear_team_id'] or '-'}")
                click.echo(f"Linear project: {payload['linear_project_id'] or '-'}")
                click.echo(
                    f"Linked: {payload['linked_count']}  Pending: {payload['pending_count']}"
                )
                if payload["last_error"]:
                    click.echo(f"Error: {payload['last_error']}")
                if payload["retry_at"]:
                    click.echo(f"Retry at: {payload['retry_at']}")

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
@click.option("--project", "project_ref", help="Gobby project name or UUID")
@click.option("--bootstrap", is_flag=True, help="Create or reuse a Linear project by name")
@click.option("--team-id", help="Linear team ID")
@click.option("--project-id", "linear_project_id", help="Existing Linear project ID")
@click.option("--project-name", help="Linear project name to create or reuse")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def linear_setup(
    project_ref: str | None,
    bootstrap: bool,
    team_id: str | None,
    linear_project_id: str | None,
    project_name: str | None,
    json_format: bool,
) -> None:
    """Configure and enable project-scoped Linear synchronization."""
    try:
        task_manager, mcp_manager, project_manager, project_id = get_linear_deps(project_ref)
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
            )
        )

        if json_format:
            click.echo(json_dumps(result, indent=2, default=str))
            return

        click.echo("✓ Linear setup complete")
        click.echo(f"  Team: {result['linear_team_id']}")
        click.echo(f"  Project: {result['linear_project_name']} ({result['linear_project_id']})")
        click.echo("  Daemon sync: enabled")

    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from None


@linear.command("import")
@click.argument("team_id", required=False)
@click.option("--state", help="Issue state filter (e.g., 'Todo', 'In Progress')")
@click.option("--labels", help="Comma-separated labels to filter issues")
@click.option(
    "--allow-team-wide",
    is_flag=True,
    help="Import every matching issue in the team when no Linear project is bound.",
)
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def linear_import(
    team_id: str | None,
    state: str | None,
    labels: str | None,
    allow_team_wide: bool,
    json_format: bool,
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
            service.import_linear_issues(
                team_id=team_id,
                state=state,
                labels=label_list,
                allow_team_wide=allow_team_wide,
            )
        )

        if json_format:
            click.echo(json_dumps({"tasks": tasks, "count": len(tasks)}, indent=2))
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
            raise click.ClickException(f"Task not found: {task_id}")

        service = get_sync_service()
        result = asyncio.run(service.sync_task_to_linear(resolved.id))

        if json_format:
            click.echo(json_dumps(result, indent=2))
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
@click.option(
    "--forward",
    is_flag=True,
    help="Push active non-closed Gobby tasks to Linear without pulling first",
)
def linear_sync_all(team_id: str | None, json_format: bool, forward: bool) -> None:
    """Bidirectional sync between gobby and Linear.

    Pulls updates from Linear first, then pushes dirty gobby tasks back.
    Use --forward for initial setup from Gobby into Linear without pulling or
    syncing closed local task history. If TEAM_ID is not specified, uses the
    linked team.
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
        if forward:
            result = asyncio.run(service.sync_active_forward(team_id=team_id))
            push = result["push"]

            if json_format:
                click.echo(json_dumps(result, indent=2))
            else:
                click.echo("✓ Forward active Linear sync complete")
                click.echo(f"  Created missing issues: {result['created_count']}")
                click.echo(
                    f"  Push: {push['pushed']} pushed, "
                    f"{push['skipped']} skipped, "
                    f"{push['errors']} errors"
                )
            return

        result = asyncio.run(service.sync_all(team_id=team_id))
        pull = result["pull"]
        push = result["push"]

        if json_format:
            click.echo(json_dumps(result, indent=2))
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
            raise click.ClickException(f"Task not found: {task_id}")

        service = get_sync_service(team_id)
        result = asyncio.run(service.create_issue_for_task(task_id=resolved.id, team_id=team_id))

        if json_format:
            click.echo(json_dumps(result, indent=2))
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
