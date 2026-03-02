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

logger = logging.getLogger(__name__)


VALID_WORKFLOW_TYPES = ("rule", "workflow", "pipeline", "agent", "variable")


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
    """Delete all workflow definitions and reinstall from bundled templates."""
    from gobby.storage.database import LocalDatabase
    from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

    type_label = workflow_type or "all"
    if not force:
        click.confirm(
            f"This will delete and reinstall {type_label} workflow definitions. Continue?",
            abort=True,
        )

    db = LocalDatabase()
    manager = LocalWorkflowDefinitionManager(db)

    # 1. Hard-delete existing rows
    with db.transaction() as conn:
        if workflow_type:
            cursor = conn.execute(
                "DELETE FROM workflow_definitions WHERE workflow_type = ?",
                (workflow_type,),
            )
        else:
            cursor = conn.execute("DELETE FROM workflow_definitions")
        deleted = cursor.rowcount
    click.echo(f"Deleted {deleted} existing definitions")

    # 2. Re-sync templates from bundled YAML
    sync_results = _run_sync(db, workflow_type)
    total_synced = sum(r.get("synced", 0) + r.get("updated", 0) for r in sync_results.values())
    click.echo(f"Synced {total_synced} templates from bundled YAML")

    # 3. Install templates (create source='installed' copies)
    installed = manager.install_all_templates(workflow_type=workflow_type)
    click.echo(f"Created {len(installed)} installed copies")

    # 4. Enable all installed copies
    with db.transaction() as conn:
        if workflow_type:
            conn.execute(
                "UPDATE workflow_definitions SET enabled = 1, updated_at = datetime('now') "
                "WHERE source = 'installed' AND workflow_type = ? AND deleted_at IS NULL",
                (workflow_type,),
            )
        else:
            conn.execute(
                "UPDATE workflow_definitions SET enabled = 1, updated_at = datetime('now') "
                "WHERE source = 'installed' AND deleted_at IS NULL",
            )

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
    from gobby.workflows.sync import (
        sync_bundled_rules,
        sync_bundled_variables,
        sync_bundled_workflows,
    )

    sync_map: dict[str, Any] = {
        "rule": ("rules", sync_bundled_rules),
        "workflow": ("workflows", sync_bundled_workflows),
        "pipeline": ("workflows", sync_bundled_workflows),  # pipelines live in workflows/
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


def _notify_daemon_reload() -> None:
    """Tell the running daemon to reload workflow definitions."""
    try:
        import httpx

        from gobby.config.app import load_config

        config = load_config()
        response = httpx.post(
            f"http://localhost:{config.daemon_port}/api/admin/workflows/reload",
            timeout=2.0,
        )
        if response.status_code == 200:
            click.echo("Triggered daemon workflow reload")
        else:
            click.echo(f"Daemon reload returned status {response.status_code}", err=True)
    except Exception as e:
        logger.debug("Could not notify daemon: %s", e)
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

    if is_global:
        dest_dir = Path.home() / ".gobby" / "workflows"
    else:
        project_path = common.get_project_path()
        if not project_path:
            click.echo("Not in a gobby project. Use --global to install globally.", err=True)
            raise SystemExit(1)
        dest_dir = project_path / ".gobby" / "workflows"

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / filename

    if dest_path.exists():
        click.confirm(f"Workflow '{workflow_name}' already exists. Overwrite?", abort=True)

    shutil.copy(source_path, dest_path)
    click.echo(f"✓ Imported workflow '{workflow_name}' to {dest_path}")


@click.command("reload")
@click.pass_context
def reload_workflows(ctx: click.Context) -> None:
    """Reload workflow definitions from disk."""
    import httpx
    import psutil

    from gobby.config.app import load_config

    # Try to tell daemon to reload
    try:
        config = load_config()
        port = config.daemon_port

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
                    f"http://localhost:{port}/api/admin/workflows/reload", timeout=2.0
                )
                if response.status_code == 200:
                    data = response.json()
                    if data.get("status") == "success":
                        click.echo("✓ Triggered daemon workflow reload")
                        return
                    else:
                        click.echo(f"Daemon reload failed: {data.get('message')}", err=True)
                else:
                    click.echo(f"Daemon returned status {response.status_code}", err=True)
            except httpx.ConnectError:
                click.echo("Could not reach daemon; reload may not have occurred.", err=True)
            except httpx.RequestError as e:
                click.echo(f"Failed to communicate with daemon: {e}", err=True)
    except Exception as e:
        logger.debug("Error checking daemon status: %s", e, exc_info=True)

    # Fallback: Clear local cache (useful if running in same process or just validating)
    # This also helps if the user just wants to verify the command runs
    loader = common.get_workflow_loader()
    loader.clear_cache()
    click.echo("✓ Cleared local workflow cache")
