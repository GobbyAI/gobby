"""
Project management CLI commands.
"""

import json
from pathlib import Path

import click

from gobby.storage.database import LocalDatabase
from gobby.storage.projects import SYSTEM_PROJECT_NAMES, LocalProjectManager, Project


def get_project_manager() -> LocalProjectManager:
    """Get initialized project manager."""
    db = LocalDatabase()
    return LocalProjectManager(db)


def resolve_project(manager: LocalProjectManager, ref: str) -> Project:
    """Resolve a project reference or exit with error."""
    project = manager.resolve_ref(ref)
    if not project:
        click.echo(f"Project not found: {ref}", err=True)
        raise SystemExit(1)
    return project


@click.group()
def projects() -> None:
    """Manage Gobby projects."""
    pass


@projects.command("list")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.option("--all", "show_all", is_flag=True, help="Include system projects (prefixed with _)")
def list_projects(json_format: bool, show_all: bool) -> None:
    """List all known projects."""
    manager = get_project_manager()
    projects_list = manager.list()

    if not show_all:
        projects_list = [p for p in projects_list if not p.name.startswith("_")]

    if json_format:
        click.echo(json.dumps([p.to_dict() for p in projects_list], indent=2, default=str))
        return

    if not projects_list:
        click.echo("No projects found.")
        click.echo("Use 'gobby init' in a project directory to register it.")
        return

    click.echo(f"Found {len(projects_list)} project(s):\n")
    for project in projects_list:
        path_info = f"  {project.repo_path}" if project.repo_path else ""
        click.echo(f"  {project.name:<20} {project.id[:12]}{path_info}")


@projects.command("show")
@click.argument("project_ref")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def show_project(project_ref: str, json_format: bool) -> None:
    """Show details for a project.

    PROJECT_REF can be a project name or UUID.
    """
    manager = get_project_manager()
    project = resolve_project(manager, project_ref)

    if json_format:
        click.echo(json.dumps(project.to_dict(), indent=2, default=str))
        return

    click.echo(f"Project: {project.name}")
    click.echo(f"  ID: {project.id}")
    if project.repo_path:
        click.echo(f"  Path: {project.repo_path}")
    if project.github_url:
        click.echo(f"  GitHub: {project.github_url}")
    if project.github_repo:
        click.echo(f"  Repo: {project.github_repo}")
    if project.linear_team_id:
        click.echo(f"  Linear Team: {project.linear_team_id}")
    if project.linear_project_id:
        click.echo(f"  Linear Project: {project.linear_project_id}")
    click.echo(f"  Created: {project.created_at}")
    click.echo(f"  Updated: {project.updated_at}")


@projects.command("rename")
@click.argument("project_ref")
@click.argument("new_name")
def rename_project(project_ref: str, new_name: str) -> None:
    """Rename a project.

    PROJECT_REF can be a project name or UUID.
    """
    manager = get_project_manager()
    project = resolve_project(manager, project_ref)

    if manager.is_protected(project):
        click.echo(f"Cannot rename protected project: {project.name}", err=True)
        raise SystemExit(1)

    if new_name in SYSTEM_PROJECT_NAMES:
        click.echo(f"Cannot use reserved name: {new_name}", err=True)
        raise SystemExit(1)

    existing = manager.get_by_name(new_name)
    if existing:
        click.echo(f"A project named '{new_name}' already exists.", err=True)
        raise SystemExit(1)

    old_name = project.name
    manager.update(project.id, name=new_name)

    # Update .gobby/project.json if repo_path is accessible
    if project.repo_path:
        project_json = Path(project.repo_path) / ".gobby" / "project.json"
        if project_json.exists():
            try:
                with open(project_json, encoding="utf-8") as f:
                    data = json.load(f)
                data["name"] = new_name
                with open(project_json, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
            except (json.JSONDecodeError, OSError) as e:
                click.echo(f"Warning: Could not update project.json: {e}", err=True)

    click.echo(f"Renamed '{old_name}' -> '{new_name}'")
    click.echo(f"Note: Existing commits with [{old_name}-#N] won't auto-link to the new name.")


@projects.command("delete")
@click.argument("project_ref")
@click.option("--confirm", required=True, help="Type the project name to confirm deletion")
def delete_project(project_ref: str, confirm: str) -> None:
    """Soft-delete a project.

    The project is marked as deleted but data is preserved. Use --confirm=<name> to confirm.
    """
    manager = get_project_manager()
    project = resolve_project(manager, project_ref)

    if manager.is_protected(project):
        click.echo(f"Cannot delete protected project: {project.name}", err=True)
        raise SystemExit(1)

    if confirm != project.name:
        click.echo(f"Confirmation mismatch: expected '{project.name}', got '{confirm}'", err=True)
        raise SystemExit(1)

    if manager.soft_delete(project.id):
        click.echo(f"Deleted project: {project.name}")
    else:
        click.echo(f"Failed to delete project: {project.name}", err=True)
        raise SystemExit(1)


@projects.command("update")
@click.argument("project_ref")
@click.option("--repo-path", help="Local repository path")
@click.option("--github-url", help="GitHub repository URL")
@click.option("--github-repo", help="GitHub repo in owner/repo format")
@click.option("--linear-team-id", help="Linear team ID")
@click.option("--linear-project-id", help="Linear project ID")
def update_project(
    project_ref: str,
    repo_path: str | None,
    github_url: str | None,
    github_repo: str | None,
    linear_team_id: str | None,
    linear_project_id: str | None,
) -> None:
    """Update project fields.

    PROJECT_REF can be a project name or UUID.
    """
    manager = get_project_manager()
    project = resolve_project(manager, project_ref)

    fields: dict[str, str] = {}
    if repo_path is not None:
        fields["repo_path"] = repo_path
    if github_url is not None:
        fields["github_url"] = github_url
    if github_repo is not None:
        fields["github_repo"] = github_repo
    if linear_team_id is not None:
        fields["linear_team_id"] = linear_team_id
    if linear_project_id is not None:
        fields["linear_project_id"] = linear_project_id

    if not fields:
        click.echo(
            "No fields to update. Use --repo-path, --github-url, --github-repo, "
            "--linear-team-id, or --linear-project-id."
        )
        return

    updated = manager.update(project.id, **fields)
    if updated:
        click.echo(f"Updated project: {updated.name}")
        for key, value in fields.items():
            click.echo(f"  {key}: {value}")
    else:
        click.echo(f"Failed to update project: {project.name}", err=True)
        raise SystemExit(1)


@projects.command("repair")
@click.option("--fix", is_flag=True, help="Apply fixes (default is dry-run)")
def repair_project(fix: bool) -> None:
    """Repair project configuration from the current directory.

    Checks for mismatches between .gobby/project.json and the database.
    Without --fix, prints issues found. With --fix, applies corrections.
    """
    cwd = Path.cwd().resolve()
    project_json_path = cwd / ".gobby" / "project.json"

    if not project_json_path.exists():
        click.echo("No .gobby/project.json found in current directory.", err=True)
        click.echo("Run 'gobby init' to initialize a project here.")
        raise SystemExit(1)

    try:
        with open(project_json_path, encoding="utf-8") as f:
            local_data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        click.echo(f"Failed to read project.json: {e}", err=True)
        raise SystemExit(1) from e

    project_id = local_data.get("id")
    local_name = local_data.get("name")

    if not project_id:
        click.echo("project.json missing 'id' field.", err=True)
        raise SystemExit(1)

    manager = get_project_manager()
    db_project = manager.get(project_id)

    issues: list[tuple[str, str, str]] = []  # (description, current, expected)

    if not db_project:
        click.echo(f"Project {project_id} not found in database.", err=True)
        click.echo("The project may have been deleted. Run 'gobby init' to re-register.")
        raise SystemExit(1)

    # Check name mismatch
    if local_name and db_project.name != local_name:
        issues.append(("Name mismatch", f"DB: {db_project.name}", f"Local: {local_name}"))

    # Check repo_path
    if db_project.repo_path != str(cwd):
        issues.append(
            (
                "repo_path mismatch",
                f"DB: {db_project.repo_path}",
                f"Actual: {cwd}",
            )
        )

    if not issues:
        click.echo("No issues found. Project configuration is consistent.")
        return

    click.echo(f"Found {len(issues)} issue(s):\n")
    for desc, current, expected in issues:
        click.echo(f"  {desc}")
        click.echo(f"    {current}")
        click.echo(f"    {expected}")

    if not fix:
        click.echo("\nRun with --fix to apply corrections.")
        return

    # Apply fixes
    fixes_applied = 0

    # Fix name mismatch: DB wins (rename local)
    name_mismatch = local_name and db_project.name != local_name
    if name_mismatch:
        local_data["name"] = db_project.name
        fixes_applied += 1

    # Fix repo_path: update DB to match cwd
    if db_project.repo_path != str(cwd):
        manager.update(db_project.id, repo_path=str(cwd))
        fixes_applied += 1

    # Write back project.json if name was fixed
    if name_mismatch:
        with open(project_json_path, "w", encoding="utf-8") as f:
            json.dump(local_data, f, indent=2)
            f.write("\n")

    click.echo(f"\nApplied {fixes_applied} fix(es).")
