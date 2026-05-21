"""
CLI commands for GitHub integration.

Provides commands for syncing gobby tasks with GitHub issues and PRs.
"""

import asyncio
import json
import logging

import click

from gobby.integrations.github import GitHubIntegration
from gobby.mcp_proxy.manager import MCPClientManager
from gobby.storage.hub.runtime import open_runtime_hub_database
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager
from gobby.sync.github import GitHubSyncService
from gobby.utils.project_context import get_project_context

logger = logging.getLogger(__name__)


def get_github_deps() -> tuple[LocalTaskManager, MCPClientManager, LocalProjectManager, str]:
    """Get dependencies for GitHub commands."""
    from pathlib import Path

    db = open_runtime_hub_database(apply_migrations=False)
    task_manager = LocalTaskManager(db)
    project_manager = LocalProjectManager(db)
    mcp_manager = MCPClientManager()

    ctx = get_project_context(cwd=Path.cwd())
    if not ctx or not ctx.get("id"):
        raise click.ClickException("Not in a gobby project directory. Run 'gobby init' first.")

    project_id: str = ctx["id"]
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


@click.group()
def github() -> None:
    """GitHub integration commands."""
    pass


@github.command("status")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def github_status(json_format: bool) -> None:
    """Show GitHub integration status."""
    try:
        task_manager, mcp_manager, project_manager, project_id = get_github_deps()

        # Get project info
        project = project_manager.get(project_id)
        github_repo = project.github_repo if project else None

        # Check GitHub MCP availability
        github = GitHubIntegration(mcp_manager)
        available = github.is_available()
        unavailable_reason = github.get_unavailable_reason() if not available else None

        # Count linked tasks
        row = task_manager.db.fetchone(
            "SELECT COUNT(*) as count FROM tasks WHERE project_id = ? AND github_issue_number IS NOT NULL",
            (project_id,),
        )
        linked_count = row["count"] if row else 0

        if json_format:
            click.echo(
                json.dumps(
                    {
                        "project_id": project_id,
                        "github_repo": github_repo,
                        "github_available": available,
                        "unavailable_reason": unavailable_reason,
                        "linked_tasks_count": linked_count,
                    },
                    indent=2,
                )
            )
        else:
            click.echo("GitHub Integration Status")
            click.echo("=" * 40)
            click.echo(f"Project ID: {project_id}")
            click.echo(f"Linked repo: {github_repo or '(not linked)'}")
            click.echo(f"GitHub MCP available: {'✓' if available else '✗'}")
            if not available:
                click.echo(f"  Reason: {unavailable_reason}")
            click.echo(f"Linked tasks: {linked_count}")

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
            click.echo(json.dumps({"tasks": tasks, "count": len(tasks)}, indent=2))
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
            click.echo(json.dumps(result, indent=2))
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
            click.echo(json.dumps(result, indent=2))
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
