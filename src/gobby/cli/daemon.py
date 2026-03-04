"""
Daemon management commands.
"""

import asyncio
import logging
import os
import subprocess  # nosec B404 - subprocess needed for daemon management
import sys
import time
from pathlib import Path
from typing import Any

import click
import httpx
import psutil

from gobby.utils.status import fetch_rich_status, format_status_message

from .utils import (
    find_web_dir,
    format_uptime,
    get_gobby_home,
    init_local_storage,
    is_port_available,
    kill_all_gobby_daemons,
    setup_logging,
    spawn_ui_server,
    stop_watchdog,
    wait_for_port_available,
)
from .utils import (
    stop_daemon as stop_daemon_util,
)

logger = logging.getLogger(__name__)


def _neo4j_start(gobby_home: Path) -> None:
    """Start Neo4j Docker containers if installed.

    Resolves Neo4j auth from config and passes it to the Docker
    subprocess environment.
    """
    compose_file = gobby_home / "services" / "neo4j" / "docker-compose.yml"
    if not compose_file.exists():
        return

    # Build subprocess env with config resolved from the store
    env = dict(os.environ)
    try:
        from gobby.config.app import load_config

        config = load_config()

        # Inject neo4j auth from config (format: "user:password")
        if config.memory.neo4j_auth:
            parts = config.memory.neo4j_auth.split(":", 1)
            if len(parts) == 2:
                env["GOBBY_NEO4J_PASSWORD"] = parts[1]
    except Exception as e:
        logger.warning(f"Could not resolve config for Neo4j: {e}")

    try:
        result = subprocess.run(  # nosec B603 B607 - hardcoded docker command
            ["docker", "compose", "-f", str(compose_file), "up", "-d"],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        if result.returncode != 0:
            logger.warning(f"Failed to start Neo4j containers: {result.stderr or result.stdout}")
    except subprocess.TimeoutExpired:
        logger.warning("Timed out starting Neo4j containers")
    except Exception as e:
        logger.warning(f"Failed to start Neo4j containers: {e}")


def _neo4j_stop(gobby_home: Path) -> None:
    """Stop Neo4j Docker containers if installed."""
    compose_file = gobby_home / "services" / "neo4j" / "docker-compose.yml"
    if not compose_file.exists():
        return
    try:
        result = subprocess.run(  # nosec B603 B607 - hardcoded docker command
            ["docker", "compose", "-f", str(compose_file), "down"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            logger.warning(f"Failed to stop Neo4j containers: {result.stderr or result.stdout}")
    except subprocess.TimeoutExpired:
        logger.warning("Timed out stopping Neo4j containers")
    except Exception as e:
        logger.warning(f"Failed to stop Neo4j containers: {e}")


def spawn_watchdog(daemon_port: int, verbose: bool, log_file: Path) -> int | None:
    """
    Spawn the watchdog process.

    Args:
        daemon_port: Port the daemon is listening on
        verbose: Enable verbose logging
        log_file: Path to watchdog log file

    Returns:
        Watchdog process PID, or None on failure
    """
    cmd = [sys.executable, "-m", "gobby.watchdog", "--port", str(daemon_port)]
    if verbose:
        cmd.append("--verbose")

    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_f = open(log_file, "a")

        process = subprocess.Popen(  # nosec B603
            cmd,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=os.environ.copy(),
        )

        log_f.close()
        return process.pid

    except Exception as e:
        logger.error(f"Failed to spawn watchdog: {e}")
        return None


@click.command()
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Enable verbose debug output",
)
@click.option(
    "--no-watchdog",
    is_flag=True,
    help="Disable watchdog process (watchdog is enabled by default)",
)
@click.option(
    "--no-ui",
    is_flag=True,
    help="Disable auto-starting the web UI",
)
@click.option(
    "--neo4j",
    "neo4j_flag",
    is_flag=True,
    help="Also start Neo4j Docker containers",
)
@click.pass_context
def start(
    ctx: click.Context, verbose: bool, no_watchdog: bool, no_ui: bool, neo4j_flag: bool
) -> None:
    """Start the Gobby daemon."""
    # Get config object
    config = ctx.obj["config"]

    # Get paths from config (respects GOBBY_HOME env var)
    gobby_dir = get_gobby_home()
    pid_file = gobby_dir / "gobby.pid"
    log_file = Path(config.logging.client).expanduser()
    error_log_file = Path(config.logging.client_error).expanduser()

    gobby_dir.mkdir(parents=True, exist_ok=True)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    error_log_file.parent.mkdir(parents=True, exist_ok=True)

    # Initialize local storage before starting daemon
    click.echo("Initializing local storage...")
    init_local_storage()

    # Start Neo4j containers if requested or if installed
    if neo4j_flag:
        click.echo("Starting Neo4j containers...")
        _neo4j_start(gobby_dir)
    else:
        compose_file = gobby_dir / "services" / "neo4j" / "docker-compose.yml"
        if compose_file.exists():
            click.echo("Neo4j installed, auto-starting containers...")
            _neo4j_start(gobby_dir)

    # Check if already running
    if pid_file.exists():
        try:
            with open(pid_file) as f:
                pid = int(f.read().strip())

            # Check if process is actually running
            try:
                os.kill(pid, 0)
                click.echo(f"Gobby daemon is already running (PID: {pid})", err=True)
                sys.exit(1)
            except ProcessLookupError:
                # Stale PID file
                click.echo(f"Removing stale PID file (PID: {pid})")
                pid_file.unlink()
        except Exception:
            pid_file.unlink()

    # Kill any existing watchdog before spawning a new one
    stop_watchdog(quiet=True)

    # Kill any existing gobby daemon processes
    click.echo("Checking for existing gobby processes...")
    killed_count = kill_all_gobby_daemons()
    if killed_count > 0:
        click.echo(f"Stopped {killed_count} existing process(es)")
        time.sleep(2.0)  # Wait for ports to be released
    else:
        click.echo("No existing processes found")

    # Check ports
    http_port = config.daemon_port
    ws_port = config.websocket.port

    if not is_port_available(http_port):
        click.echo(f"Waiting for HTTP port {http_port} to become available...", err=True)
        if not wait_for_port_available(http_port, timeout=5.0):
            click.echo(f"Error: Port {http_port} is still in use", err=True)
            sys.exit(1)

    if not is_port_available(ws_port):
        click.echo(f"Waiting for WebSocket port {ws_port} to become available...", err=True)
        if not wait_for_port_available(ws_port, timeout=5.0):
            click.echo(f"Error: Port {ws_port} is still in use", err=True)
            sys.exit(1)

    click.echo(f"Ports available (HTTP: {http_port}, WebSocket: {ws_port})")

    # Start the runner as a subprocess
    click.echo("Starting Gobby daemon...")

    # Build command
    cmd = [sys.executable, "-m", "gobby.runner"]
    if verbose:
        cmd.append("--verbose")

    # Open log files
    log_f = open(log_file, "a")
    error_log_f = open(error_log_file, "a")

    try:
        # Start detached subprocess
        process = subprocess.Popen(  # nosec B603 - cmd built from sys.executable and module path
            cmd,
            stdout=log_f,
            stderr=error_log_f,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # Detach from terminal
            env=os.environ.copy(),  # Inherit parent's environment (including PATH)
        )

        # Write PID file
        with open(pid_file, "w") as f:
            f.write(str(process.pid))

        # Give it a moment to start
        time.sleep(1.0)

        # Check if still running
        if process.poll() is not None:
            click.echo("Process exited immediately", err=True)
            click.echo(f"  Check logs: {error_log_file}", err=True)
            sys.exit(1)

        # Give server time to fully start
        time.sleep(2.0)

        # Display formatted status
        # Try to verify daemon is responding
        daemon_healthy = False
        start_time = time.time()
        max_wait = 120.0

        while (time.time() - start_time) < max_wait:
            try:
                response = httpx.get(f"http://localhost:{http_port}/api/admin/health", timeout=1.0)
                if response.status_code == 200:
                    daemon_healthy = True
                    break
            except (httpx.ConnectError, httpx.TimeoutException):
                time.sleep(0.5)
                continue

        # Spawn watchdog if daemon is healthy and watchdog is enabled
        watchdog_pid = None
        if daemon_healthy and not no_watchdog and config.watchdog.enabled:
            watchdog_log = Path(config.logging.watchdog).expanduser()
            watchdog_pid = spawn_watchdog(http_port, verbose, watchdog_log)
            if watchdog_pid:
                watchdog_pid_file = gobby_dir / "watchdog.pid"
                with open(watchdog_pid_file, "w") as f:
                    f.write(str(watchdog_pid))

        # Spawn UI server if enabled
        ui_pid = None
        ui_url = None
        if daemon_healthy and not no_ui and config.ui.enabled:
            if config.ui.mode == "dev":
                web_dir = find_web_dir(config)
                if web_dir:
                    ui_log = Path(config.logging.client).expanduser().parent / "ui.log"
                    ui_pid = spawn_ui_server(config.ui.host, config.ui.port, web_dir, ui_log)
                    if ui_pid:
                        ui_url = f"http://{config.ui.host}:{config.ui.port}"
                        ui_pid_file = gobby_dir / "ui.pid"
                        with open(ui_pid_file, "w") as f:
                            f.write(str(ui_pid))
                else:
                    click.echo("Warning: Web UI enabled but web/ directory not found")
            elif config.ui.mode == "production":
                ui_url = f"http://localhost:{http_port}/"

        # Format and display status
        status_kwargs = {
            "running": daemon_healthy,
            "pid": process.pid,
            "pid_file": str(pid_file),
            "log_files": str(log_file.parent),
            "http_port": http_port,
            "websocket_port": ws_port,
            "watchdog_pid": watchdog_pid,
            "ui_enabled": config.ui.enabled and not no_ui,
            "ui_mode": config.ui.mode if config.ui.enabled and not no_ui else None,
            "ui_url": ui_url,
            "ui_pid": ui_pid,
        }

        # Fetch rich status if daemon is healthy
        # Brief delay to allow stats to be computed
        if daemon_healthy:
            time.sleep(1.0)
            rich_status = asyncio.run(fetch_rich_status(http_port, timeout=2.0))
            status_kwargs.update(rich_status)

        message = format_status_message(**status_kwargs)
        click.echo("")
        click.echo(message)
        click.echo("")

        if not daemon_healthy:
            click.echo("Warning: Daemon started but health check failed")
            click.echo(f"  Check logs: {error_log_file}")

    except Exception as e:
        click.echo(f"Error starting daemon: {e}", err=True)
        sys.exit(1)
    finally:
        log_f.close()
        error_log_f.close()


@click.command()
@click.option(
    "--neo4j",
    "neo4j_flag",
    is_flag=True,
    help="Also stop Neo4j Docker containers",
)
@click.pass_context
def stop(ctx: click.Context, neo4j_flag: bool) -> None:
    """Stop the Gobby daemon."""
    success = stop_daemon_util(quiet=False)

    # Stop Neo4j containers if requested
    if neo4j_flag:
        click.echo("Stopping Neo4j containers...")
        _neo4j_stop(get_gobby_home())

    sys.exit(0 if success else 1)


@click.command()
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Enable verbose debug output",
)
@click.option(
    "--no-watchdog",
    is_flag=True,
    help="Disable watchdog process (watchdog is enabled by default)",
)
@click.option(
    "--no-ui",
    is_flag=True,
    help="Disable auto-starting the web UI",
)
@click.option(
    "--neo4j",
    "neo4j_flag",
    is_flag=True,
    help="Also restart Neo4j Docker containers",
)
@click.pass_context
def restart(
    ctx: click.Context, verbose: bool, no_watchdog: bool, no_ui: bool, neo4j_flag: bool
) -> None:
    """Restart the Gobby daemon (stop then start)."""
    setup_logging(verbose)

    click.echo("Restarting Gobby daemon...")

    # Stop Neo4j containers if requested (before daemon stop)
    if neo4j_flag:
        click.echo("Stopping Neo4j containers...")
        _neo4j_stop(get_gobby_home())

    # Stop daemon using helper function (doesn't call sys.exit)
    if not stop_daemon_util(quiet=False):
        click.echo("Failed to stop daemon, aborting restart", err=True)
        sys.exit(1)

    # Wait for cleanup and port release (TIME_WAIT state)
    time.sleep(3)

    # Call start command (with neo4j flag forwarded)
    ctx.invoke(start, verbose=verbose, no_watchdog=no_watchdog, no_ui=no_ui, neo4j_flag=neo4j_flag)


@click.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show Gobby daemon status and information."""
    config = ctx.obj["config"]
    pid_file = get_gobby_home() / "gobby.pid"
    log_dir = Path(config.logging.client).expanduser().parent

    # Read PID from file
    if not pid_file.exists():
        message = format_status_message(running=False)
        click.echo(message)
        sys.exit(0)

    try:
        with open(pid_file) as f:
            pid = int(f.read().strip())
    except Exception:
        message = format_status_message(running=False)
        click.echo(message)
        sys.exit(0)

    # Check if process is actually running
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        message = format_status_message(running=False)
        click.echo(message)
        click.echo(f"Note: Stale PID file found (PID {pid})")
        sys.exit(0)

    # Get process info for uptime (fallback)
    try:
        process = psutil.Process(pid)
        uptime_seconds = time.time() - process.create_time()
        uptime_str = format_uptime(uptime_seconds)
    except Exception:
        uptime_str = None

    http_port = config.daemon_port
    websocket_port = config.websocket.port

    # Check watchdog status
    watchdog_pid = None
    watchdog_pid_file = get_gobby_home() / "watchdog.pid"
    if watchdog_pid_file.exists():
        try:
            with open(watchdog_pid_file) as f:
                wd_pid = int(f.read().strip())
            # Check if watchdog is actually running
            os.kill(wd_pid, 0)
            watchdog_pid = wd_pid
        except (ProcessLookupError, ValueError, OSError):
            pass  # Watchdog not running or stale PID file

    # Check UI server status
    ui_enabled = config.ui.enabled
    ui_mode = config.ui.mode if ui_enabled else None
    ui_url = None
    ui_pid = None

    if ui_enabled:
        if ui_mode == "dev":
            ui_pid_file = get_gobby_home() / "ui.pid"
            if ui_pid_file.exists():
                try:
                    with open(ui_pid_file) as f:
                        _ui_pid = int(f.read().strip())
                    os.kill(_ui_pid, 0)
                    ui_pid = _ui_pid
                    ui_url = f"http://{config.ui.host}:{config.ui.port}"
                except (ProcessLookupError, ValueError, OSError):
                    pass  # UI server not running or stale PID file
        elif ui_mode == "production":
            ui_url = f"http://localhost:{http_port}/"

    # Build status kwargs
    status_kwargs: dict[str, Any] = {
        "running": True,
        "pid": pid,
        "pid_file": str(pid_file),
        "log_files": str(log_dir),
        "uptime": uptime_str,
        "http_port": http_port,
        "websocket_port": websocket_port,
        "watchdog_pid": watchdog_pid,
        "ui_enabled": ui_enabled,
        "ui_mode": ui_mode,
        "ui_url": ui_url,
        "ui_pid": ui_pid,
    }

    # Fetch rich status from daemon API (includes Neo4j status)
    rich_status = asyncio.run(fetch_rich_status(http_port, timeout=2.0))
    status_kwargs.update(rich_status)

    # Format and display status
    message = format_status_message(**status_kwargs)
    click.echo(message)
    sys.exit(0)


def get_merge_status() -> dict[str, Any]:
    """
    Get the current merge status for status output.

    Returns:
        Dict with merge status info:
        - active: bool - Whether there's an active merge
        - resolution_id: str | None - ID of active resolution
        - source_branch: str | None - Source branch being merged
        - target_branch: str | None - Target branch
        - pending_conflicts: int - Number of unresolved conflicts
    """
    try:
        from gobby.storage.database import LocalDatabase
        from gobby.storage.merge_resolutions import MergeResolutionManager

        db = LocalDatabase()
        manager = MergeResolutionManager(db)

        resolution = manager.get_active_resolution()
        if not resolution:
            return {"active": False}

        conflicts = manager.list_conflicts(resolution_id=resolution.id)
        pending_count = sum(1 for c in conflicts if c.status == "pending")

        return {
            "active": True,
            "resolution_id": resolution.id,
            "source_branch": resolution.source_branch,
            "target_branch": resolution.target_branch,
            "pending_conflicts": pending_count,
            "total_conflicts": len(conflicts),
        }
    except Exception as e:
        logger.debug(f"Error getting merge status: {e}")
        return {"active": False, "error": str(e)}
