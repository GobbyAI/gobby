"""
CLI commands for Gobby web UI management and development.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404 # subprocess needed for npm commands
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click

from .ui_mode import UIModeResolution
from .utils import find_web_dir, get_gobby_home, spawn_ui_server, stop_ui_server

if TYPE_CHECKING:
    from gobby.config.app import DaemonConfig


def _resolve_source_web_dir(ctx: click.Context | None) -> Path | None:
    """Locate the web/ source tree (with package.json) for npm dev/build."""
    config = ctx.obj.get("config") if ctx is not None and ctx.obj else None
    return find_web_dir(config, require_source=True)


def _resolve_ui_mode_for_command(config: DaemonConfig) -> UIModeResolution:
    mode = getattr(getattr(config, "ui", None), "mode", "auto")
    if mode == "production":
        return UIModeResolution(configured="production", effective="production")

    source_web_dir = find_web_dir(config, require_source=True)
    if mode == "dev":
        return UIModeResolution(configured="dev", effective="dev", source_web_dir=source_web_dir)
    if source_web_dir is not None:
        return UIModeResolution(
            configured="auto",
            effective="dev",
            source_web_dir=source_web_dir,
        )
    return UIModeResolution(configured="auto", effective="production")


def _get_ui_pid() -> int | None:
    """Read UI server PID if running."""
    pid_file = get_gobby_home() / "ui.pid"
    if not pid_file.exists():
        return None
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (ProcessLookupError, ValueError, OSError):
        return None


def _ensure_npm_deps_installed(web_dir: Path) -> bool:
    """Install npm dependencies if node_modules is missing. Returns True on success."""
    if (web_dir / "node_modules").exists():
        return True
    click.echo("Installing dependencies...")
    try:
        result = subprocess.run(  # nosec B603 B607
            ["npm", "install"],
            cwd=web_dir,
            capture_output=False,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        click.echo("npm install timed out after 120 seconds", err=True)
        return False
    except FileNotFoundError:
        click.echo("npm not found. Please install Node.js and npm.", err=True)
        return False
    except OSError as e:
        click.echo(f"Failed to run npm install: {e}", err=True)
        return False
    return result.returncode == 0


@click.group()
def ui() -> None:
    """Web UI management and development commands."""
    pass


@ui.command("start")
@click.pass_context
def ui_start(ctx: click.Context) -> None:
    """Start the web UI server."""
    config = ctx.obj["config"]

    if not config.ui.enabled:
        click.echo("Web UI is not enabled. Set ui.enabled: true in config.", err=True)
        sys.exit(1)

    ui_resolution = _resolve_ui_mode_for_command(config)

    # Check if already running (dev mode)
    if ui_resolution.effective == "dev":
        existing_pid = _get_ui_pid()
        if existing_pid:
            click.echo(f"UI server is already running (PID: {existing_pid})", err=True)
            sys.exit(1)

        web_dir = ui_resolution.source_web_dir
        if not web_dir:
            click.echo("Error: Web UI directory not found", err=True)
            sys.exit(1)

        ui_log = Path(config.telemetry.log_file).expanduser().parent / "ui.log"
        pid = spawn_ui_server(config.ui.host, config.ui.port, web_dir, ui_log)
        if pid:
            click.echo(
                f"UI dev server started (PID: {pid}) at http://{config.ui.host}:{config.ui.port}"
            )
        else:
            click.echo("Failed to start UI server", err=True)
            sys.exit(1)
    else:
        click.echo("Production mode UI is served by the daemon automatically.")
        click.echo("Ensure the daemon is running with 'gobby start'.")


@ui.command("stop")
def ui_stop() -> None:
    """Stop the web UI server."""
    pid = _get_ui_pid()
    if not pid:
        click.echo("UI server is not running")
        return

    success = stop_ui_server(quiet=False)
    if success:
        click.echo("UI server stopped")
    else:
        click.echo("Failed to stop UI server", err=True)
        sys.exit(1)


@ui.command("restart")
@click.pass_context
def ui_restart(ctx: click.Context) -> None:
    """Restart the web UI server."""
    stop_ui_server(quiet=True)
    ctx.invoke(ui_start)


@ui.command("status")
@click.pass_context
def ui_status(ctx: click.Context) -> None:
    """Show web UI server status."""
    config = ctx.obj["config"]

    if not config.ui.enabled:
        click.echo("Web UI: Disabled")
        return

    ui_resolution = _resolve_ui_mode_for_command(config)
    click.echo(f"Web UI: Enabled (mode: {ui_resolution.display})")

    if ui_resolution.effective == "dev":
        pid = _get_ui_pid()
        if pid:
            click.echo(f"  Status: Running (PID: {pid})")
            click.echo(f"  URL: http://{config.ui.host}:{config.ui.port}")
        else:
            click.echo("  Status: Stopped")
    elif ui_resolution.effective == "production":
        click.echo(f"  URL: http://localhost:{config.daemon_port}/")
        click.echo("  Status: Served by daemon (check 'gobby status')")


@ui.command()
@click.option("--port", "-p", default=60889, help="Dev server port")
@click.option("--host", "-h", default="localhost", help="Dev server host")
@click.pass_context
def dev(ctx: click.Context, port: int, host: str) -> None:
    """Start the web UI development server with hot-reload (foreground)."""
    web_dir = _resolve_source_web_dir(ctx)
    if web_dir is None:
        click.echo(
            "Error: web/ source tree with package.json not found. "
            "Run from a repo checkout or set ui.web_dir in config.",
            err=True,
        )
        sys.exit(1)

    if not _ensure_npm_deps_installed(web_dir):
        click.echo("Failed to install dependencies", err=True)
        sys.exit(1)

    click.echo(f"Starting dev server at http://{host}:{port}")
    click.echo("Press Ctrl+C to stop")
    click.echo()

    try:
        subprocess.run(  # nosec B603 B607
            ["npm", "run", "dev", "--", "--host", host, "--port", str(port)],
            cwd=web_dir,
            check=True,
        )
    except KeyboardInterrupt:
        click.echo("\nDev server stopped")
    except subprocess.CalledProcessError as e:
        click.echo(f"Dev server failed with code {e.returncode}", err=True)
        sys.exit(e.returncode)


@ui.command()
@click.pass_context
def build(ctx: click.Context) -> None:
    """Build the web UI for production."""
    web_dir = _resolve_source_web_dir(ctx)
    if web_dir is None:
        click.echo(
            "Error: web/ source tree with package.json not found. "
            "Run from a repo checkout or set ui.web_dir in config.",
            err=True,
        )
        sys.exit(1)

    if not _ensure_npm_deps_installed(web_dir):
        click.echo("Failed to install dependencies", err=True)
        sys.exit(1)

    click.echo("Building web UI...")
    result = subprocess.run(  # nosec B603 B607
        ["npm", "run", "build"],
        cwd=web_dir,
        capture_output=False,
    )

    if result.returncode == 0:
        dist_dir = web_dir / "dist"
        click.echo(f"Build complete: {dist_dir}")
    else:
        click.echo("Build failed", err=True)
        sys.exit(result.returncode)


@ui.command()
@click.pass_context
def install_deps(ctx: click.Context) -> None:
    """Install web UI dependencies."""
    web_dir = _resolve_source_web_dir(ctx)
    if web_dir is None:
        click.echo(
            "Error: web/ source tree with package.json not found. "
            "Run from a repo checkout or set ui.web_dir in config.",
            err=True,
        )
        sys.exit(1)

    if _ensure_npm_deps_installed(web_dir):
        click.echo("Dependencies installed")
    else:
        click.echo("Failed to install dependencies", err=True)
        sys.exit(1)
