"""
Daemon management commands.
"""

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import click
import httpx
import psutil
from gobby.utils.status import format_status_message

from .utils import (
    format_uptime,
    init_local_storage,
    is_port_available,
    kill_all_gobby_daemons,
    setup_logging,
    wait_for_port_available,
)
from .utils import (
    stop_daemon as stop_daemon_util,
)

logger = logging.getLogger(__name__)


@click.command()
@click.option(
    "--verbose",
    is_flag=True,
    help="Enable verbose debug output",
)
@click.pass_context
def start(ctx: click.Context, verbose: bool) -> None:
    """Start the Gobby daemon."""
    # Get config object
    config = ctx.obj["config"]

    # Get paths from config
    gobby_dir = Path.home() / ".gobby"
    pid_file = gobby_dir / "gobby.pid"
    log_file = Path(config.logging.client).expanduser()
    error_log_file = Path(config.logging.client_error).expanduser()

    gobby_dir.mkdir(parents=True, exist_ok=True)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    error_log_file.parent.mkdir(parents=True, exist_ok=True)

    # Initialize local storage before starting daemon
    click.echo("Initializing local storage...")
    init_local_storage()

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

    # Kill any existing gobby processes
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
        process = subprocess.Popen(
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
        max_wait = 15.0

        while (time.time() - start_time) < max_wait:
            try:
                response = httpx.get(f"http://localhost:{http_port}/admin/status", timeout=1.0)
                if response.status_code == 200:
                    daemon_healthy = True
                    break
            except (httpx.ConnectError, httpx.TimeoutException):
                time.sleep(0.5)
                continue

        # Format and display status
        message = format_status_message(
            running=daemon_healthy,
            pid=process.pid,
            pid_file=str(pid_file),
            log_files=str(log_file.parent),
            http_port=http_port,
            websocket_port=ws_port,
        )
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
@click.pass_context
def stop(ctx: click.Context) -> None:
    """Stop the Gobby daemon."""
    success = stop_daemon_util(quiet=False)
    sys.exit(0 if success else 1)


@click.command()
@click.option(
    "--verbose",
    is_flag=True,
    help="Enable verbose debug output",
)
@click.pass_context
def restart(ctx: click.Context, verbose: bool) -> None:
    """Restart the Gobby daemon (stop then start)."""
    setup_logging(verbose)

    click.echo("Restarting Gobby daemon...")

    # Stop daemon using helper function (doesn't call sys.exit)
    if not stop_daemon_util(quiet=False):
        click.echo("Failed to stop daemon, aborting restart", err=True)
        sys.exit(1)

    # Wait a moment for cleanup
    time.sleep(1)

    # Call start command
    ctx.invoke(start, verbose=verbose)


@click.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show Gobby daemon status and information."""
    config = ctx.obj["config"]
    pid_file = Path.home() / ".gobby" / "gobby.pid"
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

    # Get process info
    try:
        process = psutil.Process(pid)
        uptime_seconds = time.time() - process.create_time()
        uptime_str = format_uptime(uptime_seconds)
    except Exception:
        uptime_str = None

    http_port = config.daemon_port
    websocket_port = config.websocket.port

    # Format and display status
    message = format_status_message(
        running=True,
        pid=pid,
        pid_file=str(pid_file),
        log_files=str(log_dir),
        uptime=uptime_str,
        http_port=http_port,
        websocket_port=websocket_port,
    )
    click.echo(message)
    sys.exit(0)
