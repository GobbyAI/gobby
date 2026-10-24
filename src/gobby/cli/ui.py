"""
CLI commands for Gobby web UI management and development.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404 # subprocess needed for npm commands
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click
import psutil

from gobby.config.bootstrap import load_bootstrap
from gobby.config.logging import UI_LOG_FILENAME, resolved_log_path
from gobby.ui_exposure import (
    UiExposeError,
    disable_tailscale_ui,
    enable_tailscale_ui,
    get_ui_exposure_status,
)

from .ui_mode import UIModeResolution
from .utils import (
    _is_process_alive,
    find_web_dir,
    get_gobby_home,
    spawn_ui_server,
    stop_ui_server,
)
from .utils_ui import _process_start_matches, _read_ui_pid_record

if TYPE_CHECKING:
    from gobby.config.app import DaemonConfig


def _resolve_source_web_dir(ctx: click.Context | None) -> Path | None:
    """Locate the web/ source tree (with package.json) for npm dev/build."""
    from gobby.cli.runtime import get_cli_runtime

    config = get_cli_runtime(ctx).operational_config if ctx is not None else None
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
        pid, started_at = _read_ui_pid_record(pid_file)
        if _is_process_alive(pid) and _process_start_matches(psutil.Process(pid), started_at):
            return pid
    except (OSError, ValueError, psutil.Error):
        pass
    return None


_OPTIONAL_FLAGS = ("optional", "devOptional", "peerOptional")
_LOCK_DIFFERENCE_SAMPLE = 3


def _lock_differences(
    locked: dict[str, dict[str, object]], installed: dict[str, dict[str, object]]
) -> list[str]:
    """Paths where the installed tree disagrees with the lockfile. Optional entries that npm
    skips on this platform are absent by design and do not count."""
    differences = [
        f"{path} installed {entry.get('version')}, locked {locked[path].get('version')}"
        for path, entry in installed.items()
        if path and path in locked and entry.get("version") != locked[path].get("version")
    ]
    differences += [
        f"{path} installed but not locked" for path in installed if path and path not in locked
    ]
    differences += [
        f"{path} locked but not installed"
        for path, entry in locked.items()
        if path and path not in installed and not any(entry.get(flag) for flag in _OPTIONAL_FLAGS)
    ]
    return differences


def _lock_mismatch(web_dir: Path) -> str | None:
    """Name how node_modules differs from package-lock.json, or None when they agree."""
    if not (web_dir / "node_modules").is_dir():
        return "node_modules is missing"
    lock_file = web_dir / "package-lock.json"
    if not lock_file.is_file():
        return None
    try:
        locked = json.loads(lock_file.read_text())["packages"]
        hidden = web_dir / "node_modules" / ".package-lock.json"
        installed = json.loads(hidden.read_text())["packages"]
        differences = _lock_differences(locked, installed)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return "could not read package-lock.json and node_modules/.package-lock.json to compare"
    if not differences:
        return None
    sample = "; ".join(differences[:_LOCK_DIFFERENCE_SAMPLE])
    return f"node_modules is out of sync with package-lock.json ({len(differences)}: {sample})"


def _ensure_npm_deps_installed(web_dir: Path) -> bool:
    """Install npm dependencies unless node_modules matches package-lock.json. True on success."""
    reason = _lock_mismatch(web_dir)
    if reason is None:
        return True
    click.echo(f"Installing dependencies: {reason}")
    command = ["npm", "ci"] if (web_dir / "package-lock.json").is_file() else ["npm", "install"]
    try:
        result = subprocess.run(  # nosec B603 B607
            command,
            cwd=web_dir,
            capture_output=False,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        click.echo(f"{' '.join(command)} timed out after 120 seconds", err=True)
        return False
    except FileNotFoundError:
        click.echo("npm not found. Please install Node.js and npm.", err=True)
        return False
    except OSError as e:
        click.echo(f"Failed to run {' '.join(command)}: {e}", err=True)
        return False
    return result.returncode == 0


@click.group()
def ui() -> None:
    """Web UI management and development commands."""
    pass


@ui.command("sign-in-link")
def sign_in_link() -> None:
    """Print a one-time loopback browser sign-in URL (valid for 120 seconds)."""
    from gobby.utils.daemon_client import DaemonClient

    response = DaemonClient().call_http_api("/api/auth/sign-in-link")
    if response.status_code != 200:
        raise click.ClickException(
            "Sign-in link refused; use an operator or live managed credential"
        )
    click.echo(response.json()["url"])


@ui.command("expose")
def ui_expose() -> None:
    """Expose the web UI to this machine's Tailscale network."""
    daemon_port = load_bootstrap().daemon_port
    try:
        result = enable_tailscale_ui(daemon_port)
    except UiExposeError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Web UI exposed at {result.url}")


@ui.command("unexpose")
@click.option(
    "--forget",
    is_flag=True,
    help="Forget Gobby's exposure intent without asserting Serve state was removed.",
)
def ui_unexpose(forget: bool) -> None:
    """Remove Gobby's Tailscale root handler."""
    daemon_port = load_bootstrap().daemon_port
    try:
        disable_tailscale_ui(daemon_port, forget=forget)
    except UiExposeError as exc:
        raise click.ClickException(str(exc)) from exc
    if forget:
        click.echo("UI exposure intent forgotten; Tailscale Serve state was not changed.")
    else:
        click.echo("Web UI exposure disabled.")


@ui.command("start")
@click.pass_context
def ui_start(ctx: click.Context) -> None:
    """Start the web UI server."""
    from gobby.cli.runtime import get_cli_runtime

    config = get_cli_runtime(ctx).operational_config

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

        ui_log = resolved_log_path(config.logging, UI_LOG_FILENAME)
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
    from gobby.cli.runtime import get_cli_runtime

    config = get_cli_runtime(ctx).operational_config

    if not config.ui.enabled:
        click.echo("Web UI: Disabled")
        _echo_exposure_status(config.daemon_port)
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
    _echo_exposure_status(config.daemon_port)


def _echo_exposure_status(daemon_port: int) -> None:
    exposure = get_ui_exposure_status(daemon_port)
    if exposure.mode is None:
        click.echo("Exposure: off")
    elif exposure.degraded_reason is not None:
        click.echo(f"Exposure: tailscale (degraded: {exposure.degraded_reason})")
    else:
        click.echo(f"Exposure: tailscale ({exposure.url})")


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
