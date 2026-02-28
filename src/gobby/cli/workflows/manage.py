"""Management commands for workflows."""

import logging
import os
import shutil
from pathlib import Path
from urllib.parse import urlparse

import click
import yaml

from gobby.cli.workflows import common

logger = logging.getLogger(__name__)


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
