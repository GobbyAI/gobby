"""Management commands for workflows."""

import logging
import os
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import click
import yaml

from gobby.cli.workflows import common
from gobby.paths import get_global_workflows_dir
from gobby.storage.hub.runtime import runtime_hub_database
from gobby.utils.local_token import daemon_auth_headers
from gobby.utils.project_context import get_project_context
from gobby.workflows.imports import sync_imported_workflow_file

logger = logging.getLogger(__name__)


VALID_WORKFLOW_TYPES = ("rule", "workflow", "pipeline", "agent", "variable")
GOBBY_OWNED_WORKFLOW_SOURCES = ("installed", "template")


@click.command("reinstall")
@click.option(
    "--type",
    "-t",
    "workflow_type",
    default=None,
    type=click.Choice(VALID_WORKFLOW_TYPES),
    help="Only reinstall a specific type",
)
@click.option("--force", "-f", is_flag=True, help="Skip confirmation prompt")
def reinstall_workflows(workflow_type: str | None, force: bool) -> None:
    """Delete bundled workflow definitions and reinstall from bundled templates."""
    from gobby.storage.hub.runtime import open_runtime_hub_database

    type_label = workflow_type or "all"
    if not force:
        click.confirm(
            f"This will delete and reinstall only bundled {type_label} workflow definitions. "
            "User and project definitions will be preserved. Continue?",
            abort=True,
        )

    db = open_runtime_hub_database(apply_migrations=False)

    # 1. Hard-delete Gobby-owned rows; preserve user/project-authored definitions.
    with db.transaction() as conn:
        if workflow_type:
            cursor = conn.execute(
                "DELETE FROM workflow_definitions WHERE workflow_type = %s AND source IN (%s, %s)",
                (workflow_type, *GOBBY_OWNED_WORKFLOW_SOURCES),
            )
        else:
            cursor = conn.execute(
                "DELETE FROM workflow_definitions WHERE source IN (%s, %s)",
                GOBBY_OWNED_WORKFLOW_SOURCES,
            )
        deleted = cursor.rowcount
    click.echo(f"Deleted {deleted} existing definitions")

    # 2. Re-sync from bundled YAML (creates installed rows directly)
    sync_results = _run_sync(db, workflow_type)
    total_synced = sum(r.get("synced", 0) + r.get("updated", 0) for r in sync_results.values())
    click.echo(f"Synced {total_synced} definitions from bundled YAML")

    # 5. Notify daemon to reload
    _notify_daemon_reload()

    # 6. Print summary
    rows = db.fetchall(
        "SELECT COUNT(*) as cnt, source, enabled, workflow_type "
        "FROM workflow_definitions WHERE deleted_at IS NULL "
        "GROUP BY source, enabled, workflow_type ORDER BY source, workflow_type",
    )
    click.echo("\nCurrent state:")
    click.echo(f"  {'source':<12} {'enabled':<8} {'type':<12} {'count':<6}")
    click.echo(f"  {'─' * 12} {'─' * 8} {'─' * 12} {'─' * 6}")
    for row in rows:
        click.echo(
            f"  {row['source']:<12} {row['enabled']:<8} {row['workflow_type']:<12} {row['cnt']:<6}"
        )


def _run_sync(db: Any, workflow_type: str | None) -> dict[str, Any]:
    """Run the appropriate sync functions for the given workflow type."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.workflows.sync_pipelines import sync_bundled_pipelines
    from gobby.workflows.sync_rules import sync_bundled_rules
    from gobby.workflows.sync_variables import sync_bundled_variables

    sync_map: dict[str, Any] = {
        "rule": ("rules", sync_bundled_rules),
        "workflow": ("pipelines", sync_bundled_pipelines),
        "pipeline": ("pipelines", sync_bundled_pipelines),
        "agent": ("agents", sync_bundled_agents),
        "variable": ("variables", sync_bundled_variables),
    }

    results: dict[str, Any] = {}
    if workflow_type:
        label, fn = sync_map[workflow_type]
        results[label] = fn(db)
    else:
        seen: set[str] = set()
        for label, fn in sync_map.values():
            if label not in seen:
                seen.add(label)
                results[label] = fn(db)
    return results


def _notify_daemon_reload(
    *,
    project_path: Path | None = None,
    project_id: str | None = None,
) -> None:
    """Tell the running daemon to reload workflow definitions."""
    try:
        import httpx

        from gobby.cli.utils_config import get_daemon_url

        response = httpx.post(
            f"{get_daemon_url()}/api/admin/workflows/reload",
            headers=daemon_auth_headers(),
            params={
                key: value
                for key, value in {
                    "project_path": str(project_path) if project_path else None,
                    "project_id": project_id,
                }.items()
                if value is not None
            },
            timeout=2.0,
        )
        if response.status_code == 200:
            click.echo("Triggered daemon workflow reload")
        else:
            click.echo(f"Daemon reload returned status {response.status_code}", err=True)
    except Exception as e:
        logger.debug("Could not notify daemon: %s", e, exc_info=True)
        click.echo("Daemon not reachable; reload will happen on next restart")


@click.command("import")
@click.argument("source")
@click.option("--name", "-n", help="Override workflow name")
@click.option("--global", "-g", "is_global", is_flag=True, help="Install to global directory")
@click.pass_context
def import_workflow(ctx: click.Context, source: str, name: str | None, is_global: bool) -> None:
    """Import a workflow from a file or URL."""

    # Determine if URL or file
    parsed = urlparse(source)
    is_url = parsed.scheme in ("http", "https")

    if is_url:
        click.echo("URL import not yet implemented. Download the file and import locally.")
        raise SystemExit(1)

    # File import
    source_path = Path(source)
    if not source_path.exists():
        click.echo(f"File not found: {source}", err=True)
        raise SystemExit(1)

    if source_path.suffix.lower() not in {".yaml", ".yml"}:
        click.echo("Workflow file must have .yaml or .yml extension.", err=True)
        raise SystemExit(1)

    # Validate it's a valid workflow
    try:
        with open(source_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not data or "name" not in data:
            click.echo("Invalid workflow: missing 'name' field.", err=True)
            raise SystemExit(1)

    except yaml.YAMLError as e:
        click.echo(f"Invalid YAML: {e}", err=True)
        raise SystemExit(1) from None

    # Determine destination
    workflow_name = name or data.get("name", source_path.stem)

    # Sanitize workflow name to prevent path traversal
    safe_name = Path(workflow_name).name
    if safe_name != workflow_name:
        click.echo(
            f"Invalid workflow name: '{workflow_name}' (contains path separators).", err=True
        )
        raise SystemExit(1)

    filename = f"{workflow_name}.yaml"

    project_path: Path | None = None
    project_id: str | None = None
    if is_global:
        dest_dir = get_global_workflows_dir()
    else:
        project_path = common.get_project_path()
        if not project_path:
            click.echo("Not in a gobby project. Use --global to install globally.", err=True)
            raise SystemExit(1)
        project_context = get_project_context(project_path)
        project_id = str(project_context["id"]) if project_context else None
        if project_id is None:
            click.echo("Project configuration is missing its id.", err=True)
            raise SystemExit(1)
        dest_dir = project_path / ".gobby" / "workflows"

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / filename

    if dest_path.exists():
        click.confirm(f"Workflow '{workflow_name}' already exists. Overwrite?", abort=True)

    previous_contents = dest_path.read_bytes() if dest_path.exists() else None
    if workflow_name == data["name"]:
        shutil.copy(source_path, dest_path)
    else:
        data["name"] = workflow_name
        dest_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    try:
        with runtime_hub_database(apply_migrations=False) as db:
            sync_imported_workflow_file(db, dest_path, project_id)
    except Exception as exc:
        if previous_contents is None:
            dest_path.unlink(missing_ok=True)
        else:
            dest_path.write_bytes(previous_contents)
        raise click.ClickException(f"Failed to import workflow: {exc}") from None

    click.echo(f"✓ Imported workflow '{workflow_name}' to {dest_path}")
    _notify_daemon_reload(project_path=project_path, project_id=project_id)


@click.command("reload")
@click.pass_context
def reload_workflows(ctx: click.Context) -> None:
    """Reload workflow definitions from disk."""
    import httpx
    import psutil

    from gobby.cli.utils_config import get_daemon_url

    # Try to tell daemon to reload
    try:
        daemon_url = get_daemon_url()

        # Check if running
        is_running = False
        try:
            for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                try:
                    cmdline = proc.cmdline()
                    if not cmdline:
                        continue
                    # Check if the process is a gobby daemon
                    cmd_base = os.path.basename(cmdline[0])
                    has_gobby = (
                        "gobby" in cmd_base
                        or (len(cmdline) >= 3 and cmdline[1] == "-m" and cmdline[2] == "gobby")
                        or (cmd_base == "uv" and "run" in cmdline[1:] and "gobby" in cmdline[1:])
                    )
                    has_start = "start" in cmdline[1:]
                    if has_gobby and has_start:
                        is_running = True
                        break
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except psutil.Error:
            is_running = False

        if is_running:
            try:
                response = httpx.post(
                    f"{daemon_url}/api/admin/workflows/reload",
                    headers=daemon_auth_headers(),
                    timeout=2.0,
                )
                if response.status_code == 200:
                    data = response.json()
                    if data.get("status") == "success":
                        click.echo("✓ Triggered daemon workflow reload")
                        return
                    click.echo(f"Daemon reload failed: {data.get('message')}", err=True)
                else:
                    click.echo(f"Daemon returned status {response.status_code}", err=True)
            except httpx.ConnectError:
                click.echo("Could not reach daemon; reload may not have occurred.", err=True)
            except httpx.RequestError as e:
                click.echo(f"Failed to communicate with daemon: {e}", err=True)
            raise SystemExit(1)
    except Exception as e:
        logger.debug("Error checking daemon status: %s", e, exc_info=True)
        raise click.ClickException(f"Failed to check daemon status: {e}") from None

    # Fallback: Clear local cache (useful if running in same process or just validating)
    # This also helps if the user just wants to verify the command runs
    loader = common.get_workflow_loader()
    loader.clear_cache()
    click.echo("✓ Cleared local workflow cache")
