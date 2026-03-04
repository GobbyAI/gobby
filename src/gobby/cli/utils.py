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

from gobby.config.app import DaemonConfig, load_config
from gobby.storage.database import LocalDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import LocalSessionManager
from gobby.utils.project_context import get_project_context

logger = logging.getLogger(__name__)


def load_full_config_from_db() -> DaemonConfig:
    """Load full DaemonConfig from DB config_store + Pydantic defaults.

    Opens the database directly (using bootstrap.yaml for db_path),
    creates a ConfigStore, and calls load_config with it. Use this
    when CLI commands need the full config without a running daemon.

    Returns:
        Fully resolved DaemonConfig
    """
    from gobby.storage.config_store import ConfigStore
    from gobby.storage.secrets import SecretStore

    # Phase 1: bootstrap gives us the database path
    bootstrap_config = load_config()
    db_path = Path(bootstrap_config.database_path).expanduser()

    if not db_path.exists():
        # No DB yet — return bootstrap-only config
        return bootstrap_config

    db = LocalDatabase(db_path)
    try:
        config_store = ConfigStore(db)
        secret_store = SecretStore(db)
        return load_config(
            config_store=config_store,
            secret_resolver=secret_store.get,
        )
    finally:
        db.close()


def get_gobby_home() -> Path:
    """Get gobby home directory, respecting GOBBY_HOME env var.

    Returns:
        Path to gobby home (~/.gobby by default, or GOBBY_HOME if set)
    """
    gobby_home = os.environ.get("GOBBY_HOME")
    if gobby_home:
        return Path(gobby_home)
    return Path.home() / ".gobby"


def get_resources_dir(project_path: str | None = None) -> Path:
    """Get the resources directory for storing media files.

    If a project path is provided, returns the project-local resources directory
    (.gobby/resources/ within the project). Otherwise, returns the global
    resources directory (~/.gobby/resources/).

    The directory is created if it doesn't exist.

    Args:
        project_path: Optional project root path for project-local resources

    Returns:
        Path to the resources directory
    """
    if project_path:
        resources_dir = Path(project_path) / ".gobby" / "resources"
    else:
        resources_dir = get_gobby_home() / "resources"

    # Ensure directory exists
    resources_dir.mkdir(parents=True, exist_ok=True)
    return resources_dir


def resolve_project_ref(project_ref: str | None, exit_on_not_found: bool = True) -> str | None:
    """Resolve a project reference (name or UUID) to project ID.

    Accepts:
    - Project name (e.g., "gobby")
    - Project UUID
    - None (returns current project from context)

    Args:
        project_ref: Project name, UUID, or None
        exit_on_not_found: If True (default), exit the CLI when an explicit
            project_ref is provided but not found

    Returns:
        Project ID string, or None if not found/no context
    """
    if not project_ref:
        # Use current project context
        ctx = get_project_context()
        return ctx.get("id") if ctx else None

    db = LocalDatabase()
    try:
        manager = LocalProjectManager(db)

        # Try as direct UUID first
        project = manager.get(project_ref)
        if project:
            return project.id

        # Try as project name
        project = manager.get_by_name(project_ref)
        if project:
            return project.id
    finally:
        db.close()

    if exit_on_not_found:
        click.echo(f"Project not found: {project_ref}", err=True)
        raise SystemExit(1)
    return None


def get_active_session_id(db: LocalDatabase | None = None) -> str | None:
    """Get the most recent active session ID."""
    close_db = False
    if db is None:
        db = LocalDatabase()
        close_db = True

    try:
        # SELECT id FROM sessions WHERE status = 'active' ORDER BY updated_at DESC LIMIT 1
        # Using format compatible with the rest of the codebase (raw SQL) to avoid circular imports
        # if using session manager directly which might pull in other things.
        # But we import LocalSessionManager at top, so let's use it if possible or raw SQL for speed.
        row = db.fetchone(
            "SELECT id FROM sessions WHERE status = 'active' ORDER BY updated_at DESC LIMIT 1"
        )
        return row["id"] if row else None
    finally:
        if close_db:
            db.close()


def resolve_session_id(session_ref: str | None, project_id: str | None = None) -> str:
    """
    Resolve session reference to UUID.

    Centralized logic used by all CLI commands.

    Args:
        session_ref: User input string (UUID, #N, N, prefix) or None
        project_id: Project ID for project-scoped #N lookup.
            If not provided, auto-detected from current project context.

    Returns:
        Resolved UUID string

    Raises:
        click.ClickException: If session not found or ambiguous
    """
    db = LocalDatabase()
    try:
        # If no reference provided, try to find active session
        if not session_ref:
            active_id = get_active_session_id(db)
            if not active_id:
                raise click.ClickException("No active session found. Specify --session.")
            return active_id

        # Get project_id from context if not provided
        if not project_id:
            ctx = get_project_context()
            project_id = ctx.get("id") if ctx else None

        # Use SessionManager for resolution logic
        manager = LocalSessionManager(db)
        try:
            return manager.resolve_session_reference(session_ref, project_id)
        except ValueError as e:
            raise click.ClickException(str(e)) from None
    finally:
        db.close()


def list_project_names() -> list[str]:
    """List all project names for shell completion."""
    db = LocalDatabase()
    try:
        manager = LocalProjectManager(db)
        return [p.name for p in manager.list()]
    finally:
        db.close()


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
    Find and kill all gobby DAEMON and WATCHDOG processes (not CLI commands).

    Only kills processes that are actually running daemon servers or watchdogs,
    not CLI invocations or other tools.

    Detection methods:
    1. Matches gobby.runner (the main daemon process)
    2. Matches gobby.watchdog (the watchdog process)
    3. Matches processes listening on daemon ports (60887/60888)

    Returns:
        Number of processes killed
    """
    if os.environ.get("GOBBY_TEST_PROTECT", "").lower() in ("1", "true", "yes"):
        logger.warning("kill_all_gobby_daemons called during test — skipping")
        return 0

    # Load config to get the configured ports
    try:
        config = load_config()
        http_port = config.daemon_port
        ws_port = config.websocket.port
    except Exception:
        # Fallback to defaults if config can't be loaded
        http_port = 60887
        ws_port = 60888

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

            # Match gobby.runner and gobby.watchdog processes
            # Started via: python -m gobby.runner / python -m gobby.watchdog
            is_gobby_daemon = (
                "python" in cmdline_str.lower()
                and (
                    # Match gobby.runner (the main daemon process)
                    "gobby.runner" in cmdline_str
                    # Match gobby.watchdog (the watchdog process)
                    or "gobby.watchdog" in cmdline_str
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
                    connections = proc.net_connections()
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
                from gobby.runner_maintenance import write_shutdown_source

                try:
                    try:
                        write_shutdown_source("cli_kill_all")
                    except Exception as e:
                        logger.warning("Failed to write shutdown source: %s", e)
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


def init_local_storage() -> "LocalDatabase":
    """Initialize hub SQLite storage and run migrations.

    Returns:
        The initialized database instance.
    """
    from gobby.storage.database import LocalDatabase
    from gobby.storage.migrations import run_migrations

    config = load_config()
    hub_db_path = Path(config.database_path).expanduser()

    # Ensure hub db directory exists
    hub_db_path.parent.mkdir(parents=True, exist_ok=True)

    hub_db = LocalDatabase(hub_db_path)
    run_migrations(hub_db)
    logger.debug(f"Database: {hub_db_path}")
    return hub_db


def get_install_dir() -> Path:
    """Get the gobby install directory.

    Checks for source directory (development mode) first,
    falls back to package directory.

    Returns:
        Path to the install directory
    """
    # Import from centralized paths module to avoid duplication
    from gobby.paths import get_install_dir as _get_install_dir

    return _get_install_dir()


def _is_process_alive(pid: int) -> bool:
    """Check if a process is truly alive (not zombie, not dead).

    Uses psutil to check process status, which handles zombies correctly.
    os.kill(pid, 0) succeeds on zombie processes, but they're effectively dead.

    Args:
        pid: Process ID to check

    Returns:
        True only if process exists and is not a zombie
    """
    try:
        proc = psutil.Process(pid)
        return bool(proc.status() != psutil.STATUS_ZOMBIE)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def stop_watchdog(quiet: bool = False) -> bool:
    """Stop the watchdog process. Returns True on success, False on failure.

    Args:
        quiet: If True, suppress output messages

    Returns:
        True if watchdog was stopped successfully or wasn't running, False on error
    """
    pid_file = get_gobby_home() / "watchdog.pid"

    if not pid_file.exists():
        if not quiet:
            logger.debug("Watchdog not running (no PID file)")
        return True

    try:
        with open(pid_file) as f:
            pid = int(f.read().strip())
    except Exception as e:
        if not quiet:
            logger.debug(f"Error reading watchdog PID file: {e}")
        pid_file.unlink(missing_ok=True)
        return True

    # Check if process is actually running
    if not _is_process_alive(pid):
        if not quiet:
            logger.debug(f"Watchdog not running (stale PID file with PID {pid})")
        pid_file.unlink(missing_ok=True)
        return True

    try:
        # Send SIGTERM for graceful shutdown
        os.kill(pid, signal.SIGTERM)
        if not quiet:
            click.echo(f"Stopping watchdog (PID {pid})")

        # Wait for graceful shutdown (watchdog should stop quickly)
        max_wait = 5
        for _ in range(max_wait * 10):
            time.sleep(0.1)
            if not _is_process_alive(pid):
                if not quiet:
                    logger.debug("Watchdog stopped")
                pid_file.unlink(missing_ok=True)
                return True

        # Force kill if needed
        try:
            os.kill(pid, signal.SIGKILL)
            time.sleep(0.5)
        except ProcessLookupError:
            pass

        pid_file.unlink(missing_ok=True)
        return True

    except ProcessLookupError:
        pid_file.unlink(missing_ok=True)
        return True
    except Exception as e:
        if not quiet:
            logger.debug(f"Error stopping watchdog: {e}")
        return False


def find_web_dir(config: DaemonConfig | None = None) -> Path | None:
    """Find the web UI directory.

    Search order:
    1. config.ui.web_dir if set
    2. cwd / web/ (has package.json)
    3. Relative to gobby package (src/gobby/ui/web/)

    Args:
        config: DaemonConfig instance (optional)

    Returns:
        Path to web/ directory, or None if not found
    """
    # 1. Explicit config path
    if config and hasattr(config, "ui") and config.ui.web_dir:
        p = Path(config.ui.web_dir).expanduser()
        if p.exists() and (p / "package.json").exists():
            return p

    # 2. cwd / web/
    cwd_web = Path.cwd() / "web"
    if cwd_web.exists() and (cwd_web / "package.json").exists():
        return cwd_web

    # 3. Relative to gobby package
    try:
        import gobby

        pkg_web = Path(gobby.__file__).parent / "ui" / "web"
        if pkg_web.exists() and (pkg_web / "package.json").exists():
            return pkg_web
    except ImportError:
        logger.debug("gobby package not importable, skipping package web dir")
    except OSError as e:
        logger.debug("Could not locate package web directory: %s", e)

    return None


def _kill_port_holder(port: int) -> None:
    """Kill any process listening on the given port.

    Handles orphaned Vite/node processes that weren't cleaned up by a previous
    daemon stop (e.g. manually started with ``npm run dev``).
    """
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            for conn in proc.net_connections():
                if (
                    hasattr(conn, "laddr")
                    and conn.laddr
                    and conn.laddr.port == port
                    and conn.status == psutil.CONN_LISTEN
                ):
                    logger.info(
                        f"Killing orphan process on port {port}: PID {proc.pid} ({proc.name()})"
                    )
                    parent = psutil.Process(proc.pid)
                    children = parent.children(recursive=True)
                    for child in children:
                        child.terminate()
                    parent.terminate()
                    _, alive = psutil.wait_procs([parent] + children, timeout=3)
                    for p in alive:
                        p.kill()
                    return
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue


def spawn_ui_server(
    host: str,
    port: int,
    web_dir: Path,
    log_file: Path,
    daemon_port: int = 60887,
    ws_port: int = 60888,
) -> int | None:
    """Spawn the UI dev server as a detached subprocess.

    Args:
        host: Host to bind dev server to
        port: Port for dev server
        web_dir: Path to web/ directory
        log_file: Path to UI log file
        daemon_port: Gobby HTTP daemon port (passed to Vite as GOBBY_DAEMON_PORT)
        ws_port: Gobby WebSocket server port (passed to Vite as GOBBY_WS_PORT)

    Returns:
        Process PID, or None on failure
    """
    import subprocess  # nosec B404

    # Clean up any stale PID from a previous run
    stop_ui_server(quiet=True)

    # Kill any orphan process occupying our port (e.g. manually started Vite)
    if not is_port_available(port, host="0.0.0.0"):  # nosec B104
        _kill_port_holder(port)
        if not wait_for_port_available(port, host="0.0.0.0", timeout=5.0):  # nosec B104
            logger.error(f"Port {port} still in use after cleanup — aborting UI server spawn")
            return None

    # Install deps if needed
    node_modules = web_dir / "node_modules"
    if not node_modules.exists():
        logger.debug("Installing web UI dependencies...")
        try:
            result = subprocess.run(  # nosec B603 B607
                ["npm", "install"],
                cwd=web_dir,
                capture_output=True,
                timeout=120,  # 2 minute timeout for npm install
            )
        except subprocess.TimeoutExpired:
            logger.error("npm install timed out after 120s")
            return None
        except FileNotFoundError:
            logger.error("npm not found — install Node.js/npm and ensure it is on PATH")
            return None

        if result.returncode != 0:
            logger.error(f"Failed to install UI dependencies: {result.stderr.decode()}")
            return None

    cmd = ["npm", "run", "dev", "--", "--host", host, "--port", str(port)]

    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)

        log_f = open(log_file, "a")  # noqa: SIM115
        try:
            env = os.environ.copy()
            env["GOBBY_DAEMON_PORT"] = str(daemon_port)
            env["GOBBY_WS_PORT"] = str(ws_port)
            env["GOBBY_UI_HOST"] = host
            process = subprocess.Popen(  # nosec B603 B607
                cmd,
                cwd=web_dir,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )
        except Exception:
            log_f.close()
            raise

        # Popen duplicates the fd internally; parent can close its handle
        log_f.close()

        # Give the process a moment to start, then verify it's still alive
        time.sleep(1.0)

        if process.poll() is not None:
            logger.error(
                f"UI server process exited immediately with code {process.returncode}. "
                f"Check logs: {log_file}"
            )
            return None

        # Write PID file only after confirming process is running
        pid_file = get_gobby_home() / "ui.pid"
        with open(pid_file, "w") as f:
            f.write(str(process.pid))

        return process.pid

    except Exception as e:
        logger.error(f"Failed to spawn UI server: {e}")
        return None


def stop_ui_server(quiet: bool = False) -> bool:
    """Stop the UI dev server. Returns True on success, False on failure.

    Kills the npm process and its node child processes.

    Args:
        quiet: If True, suppress output messages

    Returns:
        True if UI server was stopped successfully or wasn't running, False on error
    """
    pid_file = get_gobby_home() / "ui.pid"

    if not pid_file.exists():
        if not quiet:
            logger.debug("UI server not running (no PID file)")
        return True

    try:
        with open(pid_file) as f:
            pid = int(f.read().strip())
    except Exception as e:
        if not quiet:
            logger.debug(f"Error reading UI PID file: {e}")
        pid_file.unlink(missing_ok=True)
        return True

    # Check if process is actually running
    if not _is_process_alive(pid):
        if not quiet:
            logger.debug(f"UI server not running (stale PID file with PID {pid})")
        pid_file.unlink(missing_ok=True)
        return True

    try:
        # Kill process tree (npm spawns node child)
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)

        # Send SIGTERM to parent first
        os.kill(pid, signal.SIGTERM)
        if not quiet:
            click.echo(f"Stopping UI server (PID {pid})")

        # Wait for parent to stop
        max_wait = 5
        for _ in range(max_wait * 10):
            time.sleep(0.1)
            if not _is_process_alive(pid):
                break

        # Kill any remaining children
        for child in children:
            try:
                if child.is_running():
                    child.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        # Force kill parent if still alive
        if _is_process_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
                time.sleep(0.5)
            except ProcessLookupError:
                pass

        pid_file.unlink(missing_ok=True)
        return True

    except (ProcessLookupError, psutil.NoSuchProcess):
        pid_file.unlink(missing_ok=True)
        return True
    except Exception as e:
        if not quiet:
            logger.debug(f"Error stopping UI server: {e}")
        return False


def stop_daemon(quiet: bool = False) -> bool:
    """Stop the daemon process. Returns True on success, False on failure.

    Stops the watchdog first (if running) to prevent it from restarting the daemon.

    Args:
        quiet: If True, suppress output messages

    Returns:
        True if daemon was stopped successfully or wasn't running, False on error
    """
    # Stop UI server first
    stop_ui_server(quiet=True)

    # Stop watchdog to prevent it from restarting the daemon
    stop_watchdog(quiet=True)

    pid_file = get_gobby_home() / "gobby.pid"

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
        pid_file.unlink(missing_ok=True)
        return False

    # Check if process is actually running (handles zombies correctly)
    if not _is_process_alive(pid):
        if not quiet:
            click.echo(f"Stale PID file (PID {pid} not running), scanning for orphaned daemons...")
        pid_file.unlink(missing_ok=True)
        # Don't return early — scan for daemons on the port in case a different
        # PID is running the daemon (e.g. watchdog restarted it, crash + restart)
        killed = kill_all_gobby_daemons()
        if killed > 0 and not quiet:
            click.echo(f"Cleaned up {killed} orphaned daemon process(es)")
        return True

    # Verify the PID is actually a gobby daemon before sending signals
    # (PID reuse could mean it's a completely different process now)
    try:
        proc = psutil.Process(pid)
        cmdline_str = " ".join(proc.cmdline())
        if "gobby" not in cmdline_str.lower():
            if not quiet:
                click.echo(
                    f"PID {pid} is not a gobby process (cmdline: {cmdline_str[:80]}), "
                    f"removing stale PID file and scanning for orphaned daemons..."
                )
            pid_file.unlink(missing_ok=True)
            killed = kill_all_gobby_daemons()
            if killed > 0 and not quiet:
                click.echo(f"Cleaned up {killed} orphaned daemon process(es)")
            return True
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass  # Process died or can't read cmdline — proceed with kill attempt

    try:
        # Send SIGTERM signal for graceful shutdown
        try:
            from gobby.runner_maintenance import write_shutdown_source

            write_shutdown_source("cli_stop")
        except Exception:
            pass  # Best-effort — must not prevent os.kill below
        os.kill(pid, signal.SIGTERM)
        if not quiet:
            click.echo(f"Sent shutdown signal to Gobby daemon (PID {pid})")

        # Wait for graceful shutdown
        # Match daemon's uvicorn timeout_graceful_shutdown (15s) + buffer
        max_wait = 20
        for _ in range(max_wait * 10):
            time.sleep(0.1)
            if not _is_process_alive(pid):
                if not quiet:
                    click.echo("Gobby daemon stopped successfully")
                pid_file.unlink(missing_ok=True)
                return True

        # Process didn't stop gracefully - try force kill
        if not quiet:
            click.echo(f"Process didn't stop gracefully after {max_wait}s, force killing...")

        try:
            os.kill(pid, signal.SIGKILL)
            time.sleep(0.5)
        except ProcessLookupError:
            pass  # Already dead

        # Final check
        if not _is_process_alive(pid):
            if not quiet:
                click.echo("Gobby daemon force killed successfully")
            pid_file.unlink(missing_ok=True)
            return True

        if not quiet:
            click.echo("Warning: Failed to stop process", err=True)
        return False

    except PermissionError:
        if not quiet:
            click.echo(f"Error: Permission denied to stop process (PID {pid})", err=True)
        return False

    except ProcessLookupError:
        # Process died between our check and sending signal - that's fine
        if not quiet:
            click.echo("Gobby daemon stopped")
        pid_file.unlink(missing_ok=True)
        return True

    except Exception as e:
        if not quiet:
            click.echo(f"Error stopping daemon: {e}", err=True)
        return False
