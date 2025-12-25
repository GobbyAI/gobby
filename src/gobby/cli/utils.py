"""
Shared utilities for CLI commands.
"""

import logging
import os
import signal
import time
from pathlib import Path

import click
import psutil
from gobby.config.app import load_config

logger = logging.getLogger(__name__)


def setup_logging(verbose: bool = False) -> None:
    """
    Configure logging for CLI.

    Args:
        verbose: If True, enable DEBUG level logging
    """
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Silence noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def format_uptime(seconds: float) -> str:
    """
    Format uptime in human-readable format.

    Args:
        seconds: Uptime in seconds

    Returns:
        Formatted string like "1h 23m 45s"
    """
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)

    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if secs > 0 or not parts:
        parts.append(f"{secs}s")

    return " ".join(parts)


def is_port_available(port: int, host: str = "localhost") -> bool:
    """
    Check if a port is available for binding.

    Args:
        port: Port number to check
        host: Host address to bind to

    Returns:
        True if port is available, False otherwise
    """
    import socket

    # Try to bind to the port
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        sock.bind((host, port))
        sock.close()
        return True
    except OSError:
        sock.close()
        return False


def wait_for_port_available(port: int, host: str = "localhost", timeout: float = 5.0) -> bool:
    """
    Wait for a port to become available.

    Args:
        port: Port number to check
        host: Host address to bind to
        timeout: Maximum time to wait in seconds

    Returns:
        True if port became available, False if timeout
    """
    start_time = time.time()

    while (time.time() - start_time) < timeout:
        if is_port_available(port, host):
            return True
        time.sleep(0.1)

    return False


def kill_all_gobby_daemons() -> int:
    """
    Find and kill all gobby DAEMON processes (not CLI commands).

    Only kills processes that are actually running daemon servers,
    not CLI invocations or other tools.

    Detection methods:
    1. Matches gobby.runner (the main daemon process)
    2. Matches processes listening on daemon ports (8765/8766)

    Returns:
        Number of processes killed
    """
    # Load config to get the configured ports
    try:
        config = load_config(create_default=False)
        http_port = config.daemon_port
        ws_port = config.websocket.port
    except Exception:
        # Fallback to defaults if config can't be loaded
        http_port = 8765
        ws_port = 8766

    killed_count = 0
    current_pid = os.getpid()
    parent_pid = os.getppid()

    # Get our parent process tree to avoid killing it
    parent_pids = {current_pid, parent_pid}
    try:
        parent_proc = psutil.Process(parent_pid)
        while parent_proc.parent() is not None:
            parent_proc = parent_proc.parent()
            parent_pids.add(parent_proc.pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    # Find all gobby daemon processes
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            # Skip our own process and parent tree
            if proc.pid in parent_pids:
                continue

            # Check if this is a gobby daemon process
            cmdline = proc.cmdline()
            cmdline_str = " ".join(cmdline)

            # Match gobby.runner which is the actual daemon process
            # Started via: python -m gobby.runner
            is_gobby_daemon = (
                "python" in cmdline_str.lower()
                and (
                    # Match gobby.runner (new package)
                    "gobby.runner" in cmdline_str
                    # Also match legacy gobby_client.runner if it exists
                    or "gobby_client.runner" in cmdline_str
                )
                # Exclude CLI invocations
                and "gobby.cli" not in cmdline_str
                and "gobby_client.cli" not in cmdline_str
            )

            # Also check for processes that might be old daemon instances
            # by checking if they're listening on our ports
            if not is_gobby_daemon:
                try:
                    # Check if process has connections on daemon ports
                    connections = proc.connections()
                    for conn in connections:
                        if hasattr(conn, "laddr") and conn.laddr:
                            if conn.laddr.port in [http_port, ws_port]:
                                # Only consider it a daemon if it's a Python process
                                # to avoid killing unrelated services
                                if "python" in cmdline_str.lower():
                                    is_gobby_daemon = True
                                    break
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    pass

            if is_gobby_daemon:
                click.echo(f"Found gobby daemon (PID {proc.pid}): {cmdline_str[:100]}")

                # Try graceful shutdown first (SIGTERM)
                try:
                    proc.send_signal(signal.SIGTERM)
                    # Wait up to 5 seconds for graceful shutdown
                    proc.wait(timeout=5)
                    click.echo(f"Gracefully stopped PID {proc.pid}")
                    killed_count += 1
                except psutil.TimeoutExpired:
                    # Force kill if graceful shutdown fails
                    click.echo(f"Process {proc.pid} didn't stop gracefully, force killing...")
                    proc.kill()
                    proc.wait(timeout=2)
                    click.echo(f"Force killed PID {proc.pid}")
                    killed_count += 1

        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            # Process already gone or we can't access it
            pass
        except Exception as e:
            click.echo(f"Warning: Error checking process {proc.pid}: {e}", err=True)

    return killed_count


def init_local_storage() -> None:
    """Initialize local SQLite storage and run migrations."""
    from gobby.storage.database import LocalDatabase
    from gobby.storage.migrations import run_migrations

    # Create database and run migrations
    db = LocalDatabase()
    run_migrations(db)


def get_install_dir() -> Path:
    """Get the gobby install directory.

    Checks for source directory (development mode) first,
    falls back to package directory.

    Returns:
        Path to the install directory
    """
    import gobby

    package_install_dir = Path(gobby.__file__).parent / "install"

    # Try to find source directory (project root)
    current = Path(gobby.__file__).resolve()
    source_install_dir = None

    for parent in current.parents:
        potential_source = parent / "src" / "gobby" / "install"
        if potential_source.exists():
            source_install_dir = potential_source
            break

    if source_install_dir and source_install_dir.exists():
        return source_install_dir
    return package_install_dir


def stop_daemon(quiet: bool = False) -> bool:
    """Stop the daemon process. Returns True on success, False on failure.

    Args:
        quiet: If True, suppress output messages

    Returns:
        True if daemon was stopped successfully or wasn't running, False on error
    """
    pid_file = Path.home() / ".gobby" / "gobby.pid"

    # Read PID from file
    if not pid_file.exists():
        if not quiet:
            click.echo("Gobby daemon is not running (no PID file found)")
        return True

    try:
        with open(pid_file) as f:
            pid = int(f.read().strip())
    except Exception as e:
        if not quiet:
            click.echo(f"Error reading PID file: {e}", err=True)
        pid_file.unlink()
        return False

    # Check if process is actually running
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        if not quiet:
            click.echo(f"Gobby daemon is not running (stale PID file with PID {pid})")
        pid_file.unlink()
        return True

    try:
        # Send SIGTERM signal for graceful shutdown
        os.kill(pid, signal.SIGTERM)
        if not quiet:
            click.echo(f"Sent shutdown signal to Gobby daemon (PID {pid})")

        # Wait for shutdown
        max_wait = 5
        for _ in range(max_wait * 10):
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                if not quiet:
                    click.echo("Gobby daemon stopped successfully")
                pid_file.unlink()
                return True

        if not quiet:
            click.echo(f"Warning: Process still running after {max_wait}s", err=True)
        return False

    except PermissionError:
        if not quiet:
            click.echo(f"Error: Permission denied to stop process (PID {pid})", err=True)
        return False

    except Exception as e:
        if not quiet:
            click.echo(f"Error stopping daemon: {e}", err=True)
        return False
