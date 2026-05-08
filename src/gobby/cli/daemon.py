"""
Daemon management commands.
"""

import asyncio
import contextlib
import logging
import os
import subprocess  # nosec B404 # subprocess needed for daemon management
import sys
import time
from pathlib import Path
from typing import Any

import click
import httpx
import psutil

from gobby.agents.spawners.auth_env import has_auth_env
from gobby.utils.status import fetch_rich_status, format_startup_summary, format_status_message

from .installers.service import (
    get_service_status,
    service_start,
    service_stop,
)
from .utils import (
    _is_process_alive,
    find_web_dir,
    format_uptime,
    get_gobby_home,
    init_local_storage,
    is_port_available,
    kill_all_gobby_daemons,
    setup_logging,
    spawn_ui_server,
    wait_for_port_available,
)
from .utils import (
    stop_daemon as stop_daemon_util,
)

logger = logging.getLogger(__name__)


def _services_start(gobby_home: Path) -> None:
    """Start Docker services (Qdrant, Neo4j) via unified compose file.

    Uses Docker Compose profiles to start only installed services.
    Falls back to legacy per-service compose files during migration.
    """
    import shutil

    if not shutil.which("docker"):
        return

    services_dir = gobby_home / "services"
    compose_file = services_dir / "docker-compose.yml"

    # Fall back to legacy Neo4j-only compose if unified file doesn't exist yet
    if not compose_file.exists():
        legacy_compose = services_dir / "neo4j" / "docker-compose.yml"
        if legacy_compose.exists():
            compose_file = legacy_compose
        else:
            return

    # Build subprocess env with config resolved from bootstrap + DB config
    env = dict(os.environ)
    profiles: list[str] = []
    try:
        from gobby.config.app import load_config
        from gobby.config.bootstrap import load_bootstrap

        bootstrap = load_bootstrap()
        config = load_config()

        # Neo4j auth — read password directly from bootstrap
        env["GOBBY_NEO4J_PASSWORD"] = bootstrap.neo4j_password

        # Determine which profiles to start
        if config.databases.neo4j.url:
            profiles.append("neo4j")
        if config.databases.qdrant.url:
            profiles.append("qdrant")
    except Exception as e:
        logger.warning(f"Could not resolve config for services: {e}")
        # Default: try starting all profiles
        profiles = ["all"]

    if not profiles:
        logger.debug("No external services configured — skipping Docker startup")
        return

    cmd = ["docker", "compose", "-f", str(compose_file)]
    for profile in profiles:
        cmd.extend(["--profile", profile])
    cmd.extend(["up", "-d"])

    try:
        result = subprocess.run(  # nosec B603 B607 # hardcoded docker command
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            cwd=str(services_dir),
        )
        if result.returncode != 0:
            logger.warning(f"Failed to start services: {result.stderr or result.stdout}")
    except subprocess.TimeoutExpired:
        logger.warning("Timed out starting Docker services")
    except Exception as e:
        logger.warning(f"Failed to start Docker services: {e}")


def _services_stop(gobby_home: Path) -> None:
    """Stop all Docker services via unified compose file."""
    import shutil

    if not shutil.which("docker"):
        return

    services_dir = gobby_home / "services"
    compose_file = services_dir / "docker-compose.yml"

    # Fall back to legacy Neo4j-only compose
    if not compose_file.exists():
        legacy_compose = services_dir / "neo4j" / "docker-compose.yml"
        if legacy_compose.exists():
            compose_file = legacy_compose
        else:
            return

    try:
        result = subprocess.run(  # nosec B603 B607 # hardcoded docker command
            [
                "docker",
                "compose",
                "-f",
                str(compose_file),
                "down",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(services_dir),
        )
        if result.returncode != 0:
            logger.warning(f"Failed to stop services: {result.stderr or result.stdout}")
    except subprocess.TimeoutExpired:
        logger.warning("Timed out stopping Docker services")
    except Exception as e:
        logger.warning(f"Failed to stop Docker services: {e}")


def _step(msg: str, *, error: bool = False, scheduled: bool = False) -> None:
    """Print a startup/shutdown step with consistent formatting."""
    if error:
        click.echo(f"  ! {msg}", err=True)
    elif scheduled:
        click.echo(f"  ~ {msg}")
    else:
        click.echo(f"  + {msg}")


def _show_error_log_tail(error_log_file: Path, n: int = 15) -> None:
    """Show the last N lines of the error log."""
    try:
        if error_log_file.exists():
            lines = error_log_file.read_text().splitlines()
            tail = lines[-n:] if len(lines) > n else lines
            if tail:
                click.echo("")
                click.echo("  Recent error log:", err=True)
                for line in tail:
                    click.echo(f"    {line}", err=True)
    except Exception:
        click.echo(f"  Check logs: {error_log_file}", err=True)


def _poll_startup_progress(http_port: int, max_wait: float = 60.0) -> bool:
    """Poll the daemon's startup progress endpoint and display steps."""
    displayed_steps: set[str] = set()
    displayed_errors: set[str] = set()
    poll_start = time.time()
    shown_header = False

    while (time.time() - poll_start) < max_wait:
        try:
            resp = httpx.get(
                f"http://localhost:{http_port}/api/admin/startup-progress",
                timeout=1.0,
            )
            if resp.status_code != 200:
                return False
            progress = resp.json()

            # Show completed steps
            for step in progress.get("steps_completed", []):
                if step not in displayed_steps:
                    if not shown_header:
                        click.echo("")
                        click.echo("Subsystem initialization:")
                        shown_header = True
                    _step(step)
                    displayed_steps.add(step)

            # Show errors
            for err in progress.get("errors", []):
                key = f"{err['subsystem']}:{err['error']}"
                if key not in displayed_errors:
                    if not shown_header:
                        click.echo("")
                        click.echo("Subsystem initialization:")
                        shown_header = True
                    _step(f"{err['subsystem']}: {err['error']}", error=True)
                    displayed_errors.add(key)

            # Done — show scheduled tasks
            if progress.get("done"):
                scheduled = progress.get("steps_scheduled", [])
                if scheduled:
                    click.echo("")
                    click.echo("Background tasks:")
                    for task in scheduled:
                        _step(task, scheduled=True)
                return True

        except (httpx.ConnectError, httpx.TimeoutException):
            pass
        except Exception:
            return False
        time.sleep(0.5)
    return False


def _wait_for_daemon_health(
    http_port: int,
    *,
    timeout: float = 120.0,
    interval: float = 0.5,
) -> float | None:
    """Wait for the daemon health endpoint to respond successfully."""
    start = time.monotonic()
    deadline = start + timeout

    while time.monotonic() < deadline:
        if _is_daemon_healthy(http_port):
            return time.monotonic() - start
        time.sleep(interval)

    return None


def _is_daemon_healthy(http_port: int) -> bool:
    """Check whether the daemon health endpoint is currently healthy."""
    try:
        response = httpx.get(f"http://localhost:{http_port}/api/admin/health", timeout=1.0)
        return response.status_code == 200
    except (httpx.ConnectError, httpx.TimeoutException):
        return False


def _wait_for_daemon_unhealthy(
    http_port: int,
    *,
    timeout: float = 30.0,
    interval: float = 0.25,
) -> float | None:
    """Wait for the daemon health endpoint to stop responding successfully."""
    start = time.monotonic()
    deadline = start + timeout

    while time.monotonic() < deadline:
        if not _is_daemon_healthy(http_port):
            return time.monotonic() - start
        time.sleep(interval)

    return None


def _read_pid_file() -> int | None:
    """Read the daemon PID file if present and parseable."""
    pid_file = get_gobby_home() / "gobby.pid"
    if not pid_file.exists():
        return None

    try:
        with open(pid_file) as f:
            return int(f.read().strip())
    except Exception:
        return None


def _get_running_daemon_pid(service_status: dict[str, Any] | None = None) -> int | None:
    """Resolve the current daemon PID from service state or the pid file."""
    status = service_status or get_service_status()

    service_pid = status.get("pid")
    if isinstance(service_pid, int) and service_pid > 0:
        return service_pid

    pid = _read_pid_file()
    if pid is not None and _is_process_alive(pid):
        return pid

    return None


def _wait_for_process_exit(
    pid: int,
    *,
    timeout: float = 30.0,
    interval: float = 0.25,
) -> float | None:
    """Wait for a specific process to exit."""
    start = time.monotonic()
    deadline = start + timeout

    while time.monotonic() < deadline:
        if not _is_process_alive(pid):
            return time.monotonic() - start
        time.sleep(interval)

    return None


def _wait_for_service_stop(
    previous_pid: int | None,
    *,
    timeout: float = 30.0,
    interval: float = 0.25,
) -> float | None:
    """Wait for a service-managed daemon stop to complete."""
    if previous_pid is not None:
        return _wait_for_process_exit(previous_pid, timeout=timeout, interval=interval)

    start = time.monotonic()
    deadline = start + timeout

    while time.monotonic() < deadline:
        if not get_service_status().get("running"):
            return time.monotonic() - start
        time.sleep(interval)

    return None


@click.command()
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Enable verbose debug output",
)
@click.option(
    "--no-ui",
    is_flag=True,
    help="Disable auto-starting the web UI",
)
@click.option(
    "--docker",
    "docker_flag",
    is_flag=True,
    help="Also start Docker service containers (Qdrant, Neo4j)",
)
@click.pass_context
def start(ctx: click.Context, verbose: bool, no_ui: bool, docker_flag: bool) -> None:
    """Start the Gobby daemon."""
    config = ctx.obj["config"]

    # If OS service is installed, delegate to it
    svc = get_service_status()
    if svc.get("installed"):
        _step("Starting via OS service manager...")
        result = service_start()
        if result.get("success"):
            _step(f"Start request accepted by {svc.get('platform', 'OS')} service manager")
            _step("Waiting for daemon health via service...")
            elapsed = _wait_for_daemon_health(config.daemon_port)
            if elapsed is None:
                _step("Daemon did not become healthy after service start", error=True)
                sys.exit(1)
            if not _poll_startup_progress(config.daemon_port):
                _step("Daemon did not finish startup readiness after service start", error=True)
                sys.exit(1)
            _step(f"Daemon started via {svc.get('platform', 'OS')} service")
            _step(f"Health check passed ({elapsed:.1f}s)")
            return
        _step(f"Service start failed: {result.get('error')}", error=True)
        click.echo("  Falling back to direct start...")

    gobby_dir = get_gobby_home()
    pid_file = gobby_dir / "gobby.pid"
    log_file = Path(config.telemetry.log_file).expanduser()
    error_log_file = Path(config.telemetry.log_file_error).expanduser()

    gobby_dir.mkdir(parents=True, exist_ok=True)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    error_log_file.parent.mkdir(parents=True, exist_ok=True)

    click.echo("Starting Gobby daemon...")
    click.echo("")

    # Initialize local storage
    init_local_storage()
    _step("Local storage initialized")

    # Start Docker services if compose file exists or --docker flag
    services_compose = gobby_dir / "services" / "docker-compose.yml"
    legacy_compose = gobby_dir / "services" / "neo4j" / "docker-compose.yml"
    if services_compose.exists() or legacy_compose.exists() or docker_flag:
        _services_start(gobby_dir)
        _step("Docker services started")

    # Kill existing gobby daemon processes
    killed_count = kill_all_gobby_daemons()
    if killed_count > 0:
        _step(f"Stopped {killed_count} existing process(es)")
        pid_file.unlink(missing_ok=True)
        time.sleep(2.0)

    # Check for stale PID file
    if pid_file.exists():
        try:
            with open(pid_file) as f:
                pid = int(f.read().strip())
            if _is_process_alive(pid):
                try:
                    proc = psutil.Process(pid)
                    cmdline_str = " ".join(proc.cmdline())
                    if "gobby" in cmdline_str.lower():
                        _step(f"Daemon already running (PID: {pid})", error=True)
                        sys.exit(1)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            pid_file.unlink(missing_ok=True)
        except Exception:
            pid_file.unlink(missing_ok=True)

    # Check port availability
    http_port = config.daemon_port
    ws_port = config.websocket.port

    if not is_port_available(http_port):
        if not wait_for_port_available(http_port, timeout=5.0):
            _step(f"Port {http_port} still in use", error=True)
            sys.exit(1)

    if not is_port_available(ws_port):
        if not wait_for_port_available(ws_port, timeout=5.0):
            _step(f"Port {ws_port} still in use", error=True)
            sys.exit(1)

    _step(f"Ports available (HTTP: {http_port}, WS: {ws_port})")

    # Build and launch daemon subprocess
    cmd = [sys.executable, "-m", "gobby.runner"]
    if verbose:
        cmd.append("--verbose")

    if not any(has_auth_env(cli_name) for cli_name in ("claude", "codex", "gemini")):
        click.secho(
            "warning: no Anthropic/OpenAI/Google API/provider credential env vars detected. "
            "Spawned agents may prompt for login unless the CLI has on-disk credentials.",
            fg="yellow",
        )

    with contextlib.ExitStack() as log_stack:
        log_f = log_stack.enter_context(open(log_file, "a"))
        error_log_f = log_stack.enter_context(open(error_log_file, "a"))

        try:
            process = subprocess.Popen(  # nosec B603 # cmd built from sys.executable and module path
                cmd,
                stdout=log_f,
                stderr=error_log_f,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=os.environ.copy(),
            )

            with open(pid_file, "w") as f:
                f.write(str(process.pid))

            time.sleep(1.0)

            # Check for immediate crash
            if process.poll() is not None:
                _step("Daemon process exited immediately", error=True)
                _show_error_log_tail(error_log_file)
                sys.exit(1)

            _step(f"Daemon process launched (PID: {process.pid})")

            # Wait for health check
            time.sleep(2.0)
            elapsed = _wait_for_daemon_health(http_port)
            if elapsed is not None:
                _step(f"Health check passed ({elapsed:.1f}s)")
            else:
                _step("Health check failed", error=True)
                _show_error_log_tail(error_log_file)
                sys.exit(1)

            # Poll startup progress from daemon
            if not _poll_startup_progress(http_port):
                _step("Startup readiness did not complete", error=True)
                _show_error_log_tail(error_log_file)
                sys.exit(1)

            # Spawn UI server if enabled
            ui_url = None
            if not no_ui and config.ui.enabled:
                if config.ui.mode == "dev":
                    web_dir = find_web_dir(config)
                    if web_dir:
                        ui_log = Path(config.telemetry.log_file).expanduser().parent / "ui.log"
                        ui_pid = spawn_ui_server(config.ui.host, config.ui.port, web_dir, ui_log)
                        if ui_pid:
                            ui_url = f"http://{config.ui.host}:{config.ui.port}"
                            ui_pid_file = gobby_dir / "ui.pid"
                            with open(ui_pid_file, "w") as f:
                                f.write(str(ui_pid))
                elif config.ui.mode == "production":
                    ui_url = f"http://localhost:{http_port}/"

            # Compact startup summary
            click.echo("")
            click.echo(
                format_startup_summary(
                    pid=process.pid,
                    http_port=http_port,
                    websocket_port=ws_port,
                    ui_url=ui_url,
                    ui_mode=config.ui.mode if config.ui.enabled and not no_ui else None,
                    log_files=str(log_file.parent),
                )
            )
            click.echo("")

        except SystemExit:
            raise
        except Exception as e:
            _step(f"Error starting daemon: {e}", error=True)
            sys.exit(1)


@click.command()
@click.option(
    "--docker",
    "docker_flag",
    is_flag=True,
    help="Also stop Docker service containers (Qdrant, Neo4j)",
)
@click.pass_context
def stop(ctx: click.Context, docker_flag: bool, shutdown_intent: str = "stop") -> None:
    """Stop the Gobby daemon."""
    shutdown_source = "cli_restart" if shutdown_intent == "restart" else "cli_stop"
    # If OS service is installed and running, delegate to it
    docker_stopped = False
    svc = get_service_status()
    if svc.get("installed") and svc.get("running"):
        previous_pid = _get_running_daemon_pid(svc)
        from gobby.runner_maintenance import write_shutdown_source

        write_shutdown_source(shutdown_source, intent=shutdown_intent)
        click.echo("Stopping via OS service manager...")
        result = service_stop(shutdown_intent=shutdown_intent, shutdown_source=shutdown_source)
        if result.get("success"):
            if previous_pid is not None:
                _step(f"Waiting for service-managed daemon (PID: {previous_pid}) to exit...")
            else:
                _step("Waiting for service-managed daemon to stop...")
            elapsed = _wait_for_service_stop(previous_pid)
            if elapsed is None:
                _step("Service stop returned, but daemon is still running", error=True)
                sys.exit(1)
            _step(f"Daemon stopped via {svc.get('platform', 'OS')} service ({elapsed:.1f}s)")
        else:
            click.echo(f"Service stop failed: {result.get('error')}", err=True)
            click.echo("Falling back to direct stop...")

        # Stop Docker containers if requested
        if docker_flag:
            click.echo("Stopping Docker containers...")
            _services_stop(get_gobby_home())
            docker_stopped = True

        if result.get("success"):
            sys.exit(0)

    if shutdown_intent == "restart":
        success = stop_daemon_util(
            quiet=False,
            shutdown_intent="restart",
            shutdown_source=shutdown_source,
        )
    else:
        success = stop_daemon_util(quiet=False)

    # Stop Docker containers if requested (only if not already stopped above)
    if docker_flag and not docker_stopped:
        click.echo("Stopping Docker containers...")
        _services_stop(get_gobby_home())

    sys.exit(0 if success else 1)


@click.command()
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Enable verbose debug output",
)
@click.option(
    "--no-ui",
    is_flag=True,
    help="Disable auto-starting the web UI",
)
@click.option(
    "--docker",
    "docker_flag",
    is_flag=True,
    help="Also restart Docker service containers (Qdrant, Neo4j)",
)
@click.pass_context
def restart(ctx: click.Context, verbose: bool, no_ui: bool, docker_flag: bool) -> None:
    """Restart the Gobby daemon (stop then start)."""
    setup_logging(verbose)

    try:
        ctx.invoke(stop, docker_flag=docker_flag, shutdown_intent="restart")
    except SystemExit as exc:
        code = exc.code
        if code not in (None, 0):
            sys.exit(code if isinstance(code, int) else 1)

    ctx.invoke(start, verbose=verbose, no_ui=no_ui, docker_flag=docker_flag)
    ctx.invoke(status)


@click.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show Gobby daemon operational health dashboard."""
    config = ctx.obj["config"]
    pid_file = get_gobby_home() / "gobby.pid"
    log_dir = Path(config.telemetry.log_file).expanduser().parent

    # Read PID from file, falling back to launchctl service detection
    pid: int | None = None
    if pid_file.exists():
        try:
            with open(pid_file) as f:
                pid = int(f.read().strip())
        except Exception:
            pid = None

    if pid is None:
        svc = get_service_status()
        if svc.get("running") and svc.get("pid"):
            pid = svc["pid"]
        else:
            click.echo(format_status_message(running=False))
            sys.exit(0)

    # Check if process is actually running
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        click.echo(format_status_message(running=False))
        click.echo(f"Note: Stale PID file found (PID {pid})")
        sys.exit(0)

    # Get process info for uptime
    try:
        process = psutil.Process(pid)
        uptime_seconds = time.time() - process.create_time()
        uptime_str = format_uptime(uptime_seconds)
    except Exception:
        uptime_str = None

    http_port = config.daemon_port
    websocket_port = config.websocket.port

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
                    pass
        elif ui_mode == "production":
            ui_url = f"http://localhost:{http_port}/"

    # Fetch API status data
    api_data = asyncio.run(fetch_rich_status(http_port, timeout=3.0))

    # Collect dependency/CLI version info
    from gobby.utils.deps import check_config_mismatches, collect_all_deps

    deps_info = collect_all_deps()
    config_issues = check_config_mismatches(config)

    # Build service info
    service_info: str | None = None
    svc = get_service_status()
    if svc.get("installed"):
        parts = []
        if svc.get("running"):
            parts.append("running")
        elif svc.get("enabled"):
            parts.append("enabled")
        else:
            parts.append("disabled")
        parts.append(svc.get("platform", "unknown"))
        if svc.get("mode"):
            parts.append(f"{svc['mode']} mode")
        service_info = f"installed ({', '.join(parts)})"

    message = format_status_message(
        running=True,
        pid=pid,
        uptime=uptime_str,
        http_port=http_port,
        websocket_port=websocket_port,
        service_info=service_info,
        api_data=api_data,
        ui_enabled=ui_enabled,
        ui_mode=ui_mode,
        ui_url=ui_url,
        ui_pid=ui_pid,
        log_files=str(log_dir),
        deps_info=deps_info,
        config_issues=config_issues,
    )
    click.echo(message)
    sys.exit(0)


@click.command()
@click.pass_context
def health(ctx: click.Context) -> None:
    """Quick one-line daemon health check."""
    config = ctx.obj["config"]
    http_port = config.daemon_port
    pid_file = get_gobby_home() / "gobby.pid"

    # Read PID
    pid: int | None = None
    if pid_file.exists():
        try:
            with open(pid_file) as f:
                pid = int(f.read().strip())
        except Exception:
            pass

    if pid is None:
        svc = get_service_status()
        if svc.get("running") and svc.get("pid"):
            pid = svc["pid"]

    if pid is None or not _is_process_alive(pid):
        click.echo("Gobby daemon: not running")
        sys.exit(1)

    try:
        response = httpx.get(f"http://localhost:{http_port}/api/admin/health", timeout=2.0)
        if response.status_code == 200:
            # Get uptime and memory for the one-liner
            try:
                proc = psutil.Process(pid)
                uptime_str = format_uptime(time.time() - proc.create_time())
                mem_mb = proc.memory_info().rss / (1024 * 1024)
                click.echo(
                    f"Gobby daemon: healthy (PID: {pid}, uptime: {uptime_str}, mem: {mem_mb:.0f}MB)"
                )
            except Exception:
                click.echo(f"Gobby daemon: healthy (PID: {pid})")
            sys.exit(0)
        else:
            click.echo(f"Gobby daemon: unhealthy (HTTP {response.status_code})")
            sys.exit(1)
    except (httpx.ConnectError, httpx.TimeoutException):
        click.echo(f"Gobby daemon: not responding (PID: {pid})")
        sys.exit(1)


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
