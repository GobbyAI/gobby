"""
CLI commands for GitHub integration.

Provides commands for syncing gobby tasks with GitHub issues and PRs.
"""

import asyncio
import logging
from typing import cast

import click

from gobby.cli.runtime import require_cli_database
from gobby.mcp_proxy.manager import MCPClientManager
from gobby.storage.external_issue_sync import ExternalIssueSyncStatusStore
from gobby.storage.github_triage import GitHubTriageConfig, GitHubTriageStore
from gobby.storage.mcp import LocalMCPManager
from gobby.storage.projects import LocalProjectManager, Project
from gobby.storage.tasks import LocalTaskManager
from gobby.sync.github import GitHubSyncService
from gobby.sync.github_issue_sync import (
    GitHubIssueSyncService,
    GitHubRepositoryReadinessError,
)
from gobby.utils.json_helpers import json_dumps
from gobby.utils.project_context import get_project_context

logger = logging.getLogger(__name__)


def get_github_deps(
    project_ref: str | None = None,
    *,
    require_project: bool = True,
) -> tuple[LocalTaskManager, MCPClientManager, LocalProjectManager, str]:
    """Get dependencies for GitHub commands."""
    from pathlib import Path

    db = require_cli_database()
    task_manager = LocalTaskManager(db)
    project_manager = LocalProjectManager(db)
    if project_ref:
        project = project_manager.resolve_ref(project_ref)
        if not project or project.deleted_at:
            raise click.ClickException(f"Project not found: {project_ref}")
        project_id = project.id
    elif require_project:
        ctx = get_project_context(cwd=Path.cwd())
        if not ctx or not ctx.get("id"):
            raise click.ClickException("Not in a gobby project directory. Run 'gobby init' first.")
        project_id = str(ctx["id"])
    else:
        project_id = ""
    mcp_manager = MCPClientManager(
        mcp_db_manager=LocalMCPManager(db),
        project_id=project_id,
    )
    return task_manager, mcp_manager, project_manager, project_id


def get_sync_service(repo: str | None = None) -> GitHubSyncService:
    """Create GitHubSyncService for CLI commands."""
    task_manager, mcp_manager, _, project_id = get_github_deps()
    return GitHubSyncService(
        mcp_manager=mcp_manager,
        task_manager=task_manager,
        project_id=project_id,
        github_repo=repo,
    )


async def _check_github_access(
    readiness: GitHubIssueSyncService,
    project: Project,
    config: GitHubTriageConfig,
    mcp_manager: MCPClientManager,
) -> tuple[str, ...]:
    """Check access and close CLI-owned MCP transports in the same async task."""
    try:
        return await readiness.check_access(project, config)
    finally:
        await mcp_manager.disconnect_all()


@click.group()
def github() -> None:
    """GitHub integration commands."""
    pass


@github.command("setup")
@click.option("--project", "project_ref", help="Gobby project name or UUID")
@click.option("--repo", "repositories", multiple=True, help="GitHub owner/repo (repeatable)")
@click.option("--sync/--no-sync", "sync_enabled", default=None)
@click.option("--triage/--no-triage", "triage_enabled", default=None)
@click.option("--webhook/--no-webhook", "webhook_enabled", default=None)
@click.option("--webhook-secret-ref", help="Secret-store reference for webhook HMAC")
@click.option("--interval", type=int, help="Missed-webhook recovery interval in seconds")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def github_setup(
    project_ref: str | None,
    repositories: tuple[str, ...],
    sync_enabled: bool | None,
    triage_enabled: bool | None,
    webhook_enabled: bool | None,
    webhook_secret_ref: str | None,
    interval: int | None,
    json_format: bool,
) -> None:
    """Configure GitHub issue sync and automated triage independently."""
    try:
        task_manager, mcp_manager, project_manager, project_id = get_github_deps(project_ref)
        project = project_manager.get(project_id)
        if project is None:
            raise click.ClickException(f"Project not found: {project_id}")
        store = GitHubTriageStore(task_manager.db)
        current = store.get_config(project_id)
        if interval is not None and interval <= 0:
            raise click.ClickException("--interval must be greater than zero")
        if sync_enabled is None and triage_enabled is None:
            sync_enabled = current.sync_enabled or not current.triage_enabled
        candidate = GitHubTriageConfig(
            project_id=project_id,
            sync_enabled=current.sync_enabled if sync_enabled is None else sync_enabled,
            triage_enabled=(current.triage_enabled if triage_enabled is None else triage_enabled),
            webhook_enabled=(
                current.webhook_enabled if webhook_enabled is None else webhook_enabled
            ),
            repositories=repositories or current.repositories,
            reconcile_interval_seconds=interval or current.reconcile_interval_seconds,
            webhook_secret_ref=webhook_secret_ref or current.webhook_secret_ref,
        )
        readiness = GitHubIssueSyncService(
            db=task_manager.db,
            mcp_manager=mcp_manager,
            task_manager=task_manager,
            project_manager=project_manager,
        )
        resolved = (
            asyncio.run(_check_github_access(readiness, project, candidate, mcp_manager))
            if candidate.sync_enabled or candidate.triage_enabled
            else readiness.repositories_for(project, candidate)
        )
        saved = store.upsert_config(candidate)
        payload = {**saved.to_dict(), "repositories": list(resolved)}
        if json_format:
            click.echo(json_dumps(payload, indent=2, default=str))
        else:
            click.echo(f"✓ GitHub setup complete for {project.name}")
            click.echo(f"  Repositories: {', '.join(resolved)}")
            click.echo(f"  Sync: {'enabled' if saved.sync_enabled else 'disabled'}")
            click.echo(f"  Triage: {'enabled' if saved.triage_enabled else 'disabled'}")
    except GitHubRepositoryReadinessError as exc:
        raise click.ClickException(str(exc)) from exc
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(str(exc)) from None


@github.command("status")
@click.option("--project", "project_ref", help="Gobby project name or UUID")
@click.option("--all", "all_projects", is_flag=True, help="Show every registered project")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def github_status(project_ref: str | None, all_projects: bool, json_format: bool) -> None:
    """Show GitHub integration status."""
    try:
        if project_ref and all_projects:
            raise click.ClickException("Use either --project or --all, not both.")
        task_manager, _, project_manager, default_project_id = get_github_deps(
            project_ref,
            require_project=not all_projects,
        )
        selected = project_manager.get(default_project_id) if default_project_id else None
        projects = project_manager.list() if all_projects else ([selected] if selected else [])
        config_store = GitHubTriageStore(task_manager.db)
        status_store = ExternalIssueSyncStatusStore(task_manager.db)
        payloads: list[dict[str, object]] = []
        for project in projects:
            if project is None or project.deleted_at:
                continue
            mcp_manager = MCPClientManager(
                mcp_db_manager=LocalMCPManager(task_manager.db),
                project_id=project.id,
            )
            config = config_store.get_config(project.id)
            status = status_store.get(project.id, "github")
            linked, pending = status_store.counts(project.id, "github")
            readiness = GitHubIssueSyncService(
                db=task_manager.db,
                mcp_manager=mcp_manager,
                task_manager=task_manager,
                project_manager=project_manager,
            )
            ready = False
            readiness_error = None
            try:
                repositories = readiness.repositories_for(project, config)
            except ValueError:
                repositories = ()
            try:
                repositories = asyncio.run(
                    _check_github_access(readiness, project, config, mcp_manager)
                )
                ready = True
            except GitHubRepositoryReadinessError as exc:
                readiness_error = str(exc)
            payloads.append(
                {
                    "project_id": project.id,
                    "project_name": project.name,
                    **config.to_dict(),
                    "repositories": list(repositories or config.repositories),
                    "ready": ready,
                    "readiness_error": readiness_error,
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
            click.echo(json_dumps(payloads if all_projects else payloads[0], indent=2, default=str))
        else:
            for index, payload in enumerate(payloads):
                if index:
                    click.echo()
                click.echo(f"GitHub: {payload['project_name']} ({payload['project_id']})")
                click.echo(f"Sync: {'✓' if payload['sync_enabled'] else '✗'}")
                click.echo(f"Triage: {'✓' if payload['triage_enabled'] else '✗'}")
                click.echo(f"Ready: {'✓' if payload['ready'] else '✗'}")
                click.echo(f"State: {payload['state']}")
                display_repositories = cast(list[str], payload["repositories"])
                click.echo(f"Repositories: {', '.join(display_repositories) or '-'}")
                click.echo(
                    f"Linked: {payload['linked_count']}  Pending: {payload['pending_count']}"
                )
                if payload["readiness_error"] or payload["last_error"]:
                    click.echo(f"Error: {payload['readiness_error'] or payload['last_error']}")
                if payload["retry_at"]:
                    click.echo(f"Retry at: {payload['retry_at']}")

    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from None


@github.command("link")
@click.argument("repo")
def github_link(repo: str) -> None:
    """Link a GitHub repo to this project.

    REPO should be in 'owner/repo' format (e.g., 'anthropics/claude-code').
    """
    try:
        _, _, project_manager, project_id = get_github_deps()

        # Validate repo format
        if "/" not in repo or repo.count("/") != 1:
            raise click.ClickException(f"Invalid repo format: '{repo}'. Expected 'owner/repo'")

        project_manager.update(project_id, github_repo=repo)
        click.echo(f"✓ Linked project to GitHub repo: {repo}")

    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from None


@github.command("unlink")
def github_unlink() -> None:
    """Remove GitHub repo link from this project."""
    try:
        _, _, project_manager, project_id = get_github_deps()

        project_manager.update(project_id, github_repo=None)
        click.echo("✓ Unlinked GitHub repo from project")

    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from None


@github.command("import")
@click.argument("repo", required=False)
@click.option("--labels", "-l", help="Comma-separated labels to filter issues")
@click.option(
    "--state",
    "-s",
    type=click.Choice(["open", "closed", "all"]),
    default="open",
    help="Issue state filter",
)
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def github_import(repo: str | None, labels: str | None, state: str, json_format: bool) -> None:
    """Import GitHub issues as gobby tasks.

    If REPO is not specified, uses the linked repo.
    """
    try:
        task_manager, mcp_manager, project_manager, project_id = get_github_deps()

        # Get repo from argument or project config
        if not repo:
            project = project_manager.get(project_id)
            repo = project.github_repo if project else None
            if not repo:
                raise click.ClickException(
                    "No repo specified and project not linked to a GitHub repo. "
                    "Use 'gobby github link <owner/repo>' first or specify the repo."
                )

        service = GitHubSyncService(
            mcp_manager=mcp_manager,
            task_manager=task_manager,
            project_id=project_id,
            github_repo=repo,
        )

        # Run async import
        label_list = labels.split(",") if labels else None
        tasks = asyncio.run(service.import_github_issues(repo=repo, labels=label_list, state=state))

        if json_format:
            click.echo(json_dumps({"tasks": tasks, "count": len(tasks)}, indent=2))
        else:
            click.echo(f"✓ Imported {len(tasks)} issues from {repo}")
            for task in tasks:
                click.echo(f"  - {task.get('id', 'unknown')}: {task.get('title', 'Untitled')}")

    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from None


@github.command("sync")
@click.argument("task_id")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def github_sync(task_id: str, json_format: bool) -> None:
    """Sync a task to its linked GitHub issue.

    Updates the GitHub issue title and body to match the task.
    """
    try:
        service = get_sync_service()
        result = asyncio.run(service.sync_task_to_github(task_id))

        if json_format:
            click.echo(json_dumps(result, indent=2))
        else:
            click.echo(f"✓ Synced task {task_id} to GitHub")

    except click.ClickException:
        raise
    except ValueError as e:
        raise click.ClickException(str(e)) from None
    except Exception as e:
        raise click.ClickException(str(e)) from None


@github.command("pr")
@click.argument("task_id")
@click.option("--head", "-H", "head_branch", required=True, help="Branch with changes")
@click.option("--base", "-b", "base_branch", default="main", help="Branch to merge into")
@click.option("--draft", "-d", is_flag=True, help="Create as draft PR")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def github_pr(
    task_id: str,
    head_branch: str,
    base_branch: str,
    draft: bool,
    json_format: bool,
) -> None:
    """Create a GitHub PR for a task."""
    try:
        service = get_sync_service()
        result = asyncio.run(
            service.create_pr_for_task(
                task_id=task_id,
                head_branch=head_branch,
                base_branch=base_branch,
                draft=draft,
            )
        )

        if json_format:
            click.echo(json_dumps(result, indent=2))
        else:
            pr_number = result.get("number", "unknown")
            pr_url = result.get("html_url") or result.get("url", "")
            click.echo(f"✓ Created PR #{pr_number} for task {task_id}")
            if pr_url:
                click.echo(f"  {pr_url}")

    except click.ClickException:
        raise
    except ValueError as e:
        raise click.ClickException(str(e)) from None
    except Exception as e:
        raise click.ClickException(str(e)) from None
