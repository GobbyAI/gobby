"""
Daemon management commands.
"""

import asyncio
import json
import logging
import os
import subprocess  # nosec B404 # subprocess needed for daemon management
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import click
import httpx
import psutil

from gobby.agents.spawners.auth_env import has_auth_env
from gobby.config.logging import (
    RUNTIME_LOG_FILENAME,
    UI_LOG_FILENAME,
    resolved_log_path,
    resolved_logs_dir,
)
from gobby.runner_pid_file import probe_daemon_lock
from gobby.utils.status import fetch_rich_status, format_startup_summary, format_status_message

from .installers.compose_env import ComposeEnvironmentError, resolve_compose_runtime
from .installers.service import (
    get_service_status,
    service_start,
    service_stop,
)
from .ui_mode import resolve_ui_mode
from .utils import (
    _is_process_alive,
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

SERVICE_MANAGED_STOP_TIMEOUT_SECONDS = 75.0


@dataclass(frozen=True)
class ServiceStartResult:
    outcome: Literal["success", "skipped", "failed"]
    detail: str


def _services_start(gobby_home: Path) -> ServiceStartResult:
    """Start Docker services (Qdrant, FalkorDB) via unified compose file.

    Uses Docker Compose profiles to start only installed services.
    """
    import shutil

    if not shutil.which("docker"):
        return ServiceStartResult("skipped", "Docker executable is unavailable")

    services_dir = gobby_home / "services"
    compose_file = services_dir / "docker-compose.yml"

    if not compose_file.exists():
        return ServiceStartResult("skipped", f"Compose file is missing: {compose_file}")

    try:
        runtime = resolve_compose_runtime(gobby_home)
    except ComposeEnvironmentError as exc:
        return ServiceStartResult("failed", f"Could not resolve Docker service config: {exc}")

    if not runtime.profiles:
        return ServiceStartResult("skipped", "No Docker service profiles are configured")

    cmd = ["docker", "compose", "-f", str(compose_file)]
    for profile in runtime.profiles:
        cmd.extend(["--profile", profile])
    cmd.extend(["up", "-d"])

    try:
        result = subprocess.run(  # nosec B603 # hardcoded docker command
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            env=runtime.environment,
            cwd=str(services_dir),
        )
        if result.returncode != 0:
            return ServiceStartResult(
                "failed",
                f"Docker compose up failed: {result.stderr or result.stdout}",
            )
    except subprocess.TimeoutExpired:
        return ServiceStartResult("failed", "Docker compose up timed out after 120s")
    except (OSError, subprocess.SubprocessError) as exc:
        return ServiceStartResult("failed", f"Docker compose execution failed: {exc}")
    return ServiceStartResult("success", "Docker services started")


def _services_stop(gobby_home: Path) -> None:
    """Stop all Docker services via unified compose file."""
    import shutil

    if not shutil.which("docker"):
        return

    services_dir = gobby_home / "services"
    compose_file = services_dir / "docker-compose.yml"

    if not compose_file.exists():
        return

    try:
        runtime = resolve_compose_runtime(gobby_home)
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
            env=runtime.environment,
            cwd=str(services_dir),
        )
        if result.returncode != 0:
            logger.warning("Failed to stop services: %s", result.stderr or result.stdout)
    except ComposeEnvironmentError as exc:
        logger.warning("Could not resolve config for services; skipping Docker shutdown: %s", exc)
    except subprocess.TimeoutExpired:
        logger.warning("Timed out stopping Docker services")
    except Exception as e:
        logger.warning("Failed to stop Docker services: %s", e)


def _step(msg: str, *, error: bool = False, scheduled: bool = False) -> None:
    """Print a startup/shutdown step with consistent formatting."""
    if error:
        click.echo(f"  ! {msg}", err=True)
    elif scheduled:
        click.echo(f"  ~ {msg}")
    else:
        click.echo(f"  + {msg}")


def _show_runtime_output_tail(runtime_log_file: Path, n: int = 15) -> None:
    """Show the last N lines of captured daemon process output."""
    try:
        if runtime_log_file.exists():
            lines = runtime_log_file.read_text().splitlines()
            tail = lines[-n:] if len(lines) > n else lines
            if tail:
                click.echo("")
                click.echo("  Recent runtime output:", err=True)
                for line in tail:
                    click.echo(f"    {line}", err=True)
    except Exception:
        click.echo(f"  Check runtime output: {runtime_log_file}", err=True)


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
        except (
            httpx.DecodingError,
            httpx.ProtocolError,
            httpx.TooManyRedirects,
            json.JSONDecodeError,
        ) as e:
            logger.exception("Non-retryable startup progress polling error: %s", e)
            return False
        except httpx.RequestError as e:
            logger.exception("Non-retryable startup progress request error: %s", e)
            return False
        except Exception as e:
            logger.exception("Unexpected startup progress polling error: %s", e)
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
    except httpx.TimeoutException:
        return False
    except httpx.RequestError:
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
    except (ValueError, OSError, psutil.Error) as exc:
        logger.debug("Ignoring unreadable daemon PID file %s: %s", pid_file, exc)
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


def _wait_for_service_stop(
    previous_pid: int | None,
    *,
    http_port: int,
    timeout: float = SERVICE_MANAGED_STOP_TIMEOUT_SECONDS,
    interval: float = 0.25,
) -> float | None:
    """Wait for a service-managed daemon stop to complete."""
    start = time.monotonic()
    deadline = start + timeout

    while time.monotonic() < deadline:
        previous_pid_exited = previous_pid is None or not _is_process_alive(previous_pid)
        service_stopped = not get_service_status().get("running")
        daemon_unhealthy = not _is_daemon_healthy(http_port)
        if previous_pid_exited and service_stopped and daemon_unhealthy:
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
    help="Also start Docker service containers (Qdrant, FalkorDB)",
)
@click.pass_context
def start(ctx: click.Context, verbose: bool, no_ui: bool, docker_flag: bool) -> None:
    """Start the Gobby daemon."""
    config = ctx.obj["config"]
    gobby_dir = get_gobby_home()

    # Revive managed dependencies before launchd/systemd starts the runner.
    services_compose = gobby_dir / "services" / "docker-compose.yml"
    if services_compose.exists() or docker_flag:
        services_result = _services_start(gobby_dir)
        if services_result.outcome == "success":
            _step("Docker services started")
        elif services_result.outcome == "failed":
            _step(services_result.detail, error=True)
            sys.exit(1)
        else:
            _step(f"Docker services skipped: {services_result.detail}")

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

    pid_file = gobby_dir / "gobby.pid"
    runtime_log_file = resolved_log_path(config.logging, RUNTIME_LOG_FILENAME)

    gobby_dir.mkdir(parents=True, exist_ok=True)
    runtime_log_file.parent.mkdir(parents=True, exist_ok=True)

    click.echo("Starting Gobby daemon...")
    click.echo("")

    should_kill_existing_daemons = False

    # The flock-based daemon lock is authoritative: a held lock means a live
    # daemon (flock dies with its owner), so start must fail without tearing
    # down active work; a free lock means any leftover pid file is stale.
    lock_owner = probe_daemon_lock(pid_file)
    if lock_owner is not None:
        _step(f"Daemon already running (PID: {lock_owner or 'unknown'})", error=True)
        sys.exit(1)

    if pid_file.exists():
        pid_file.unlink(missing_ok=True)
        should_kill_existing_daemons = True

    if should_kill_existing_daemons:
        killed_count = kill_all_gobby_daemons()
        if killed_count > 0:
            _step(f"Stopped {killed_count} existing process(es)")
            time.sleep(2.0)

    # Initialize runtime hub storage after services are up.
    hub_db = init_local_storage()
    hub_db.close()
    _step("PostgreSQL hub initialized")

    # Check port availability
    http_port = config.daemon_port
    ws_port = config.websocket.port
    bind_host = config.bind_host

    if not is_port_available(http_port, host=bind_host):
        if not wait_for_port_available(http_port, host=bind_host, timeout=5.0):
            _step(f"Port {http_port} still in use", error=True)
            sys.exit(1)

    if not is_port_available(ws_port, host=bind_host):
        if not wait_for_port_available(ws_port, host=bind_host, timeout=5.0):
            _step(f"Port {ws_port} still in use", error=True)
            sys.exit(1)

    _step(f"Ports available (HTTP: {http_port}, WS: {ws_port})")

    # Build and launch daemon subprocess
    cmd = [sys.executable, "-m", "gobby.runner"]
    if verbose:
        cmd.append("--verbose")

    if not any(has_auth_env(cli_name) for cli_name in ("claude", "codex", "qwen")):
        click.secho(
            "warning: no Anthropic/OpenAI/Qwen API/provider credential env vars detected. "
            "Spawned agents may prompt for login unless the CLI has on-disk credentials.",
            fg="yellow",
        )

    with open(runtime_log_file, "a") as runtime_log:
        try:
            process = subprocess.Popen(  # nosec B603 # cmd built from sys.executable and module path
                cmd,
                stdout=runtime_log,
                stderr=runtime_log,
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
                _show_runtime_output_tail(runtime_log_file)
                sys.exit(1)

            _step(f"Daemon process launched (PID: {process.pid})")

            # Wait for health check
            time.sleep(2.0)
            elapsed = _wait_for_daemon_health(http_port)
            if elapsed is not None:
                _step(f"Health check passed ({elapsed:.1f}s)")
            else:
                _step("Health check failed", error=True)
                _show_runtime_output_tail(runtime_log_file)
                sys.exit(1)

            # Poll startup progress from daemon
            if not _poll_startup_progress(http_port):
                _step("Startup readiness did not complete", error=True)
                _show_runtime_output_tail(runtime_log_file)
                sys.exit(1)

            # Spawn UI server if enabled
            ui_url = None
            ui_mode_display = None
            if not no_ui and config.ui.enabled:
                ui_resolution = resolve_ui_mode(config)
                ui_mode_display = ui_resolution.display
                ui_url = f"http://localhost:{http_port}/"
                if ui_resolution.effective == "dev":
                    web_dir = ui_resolution.source_web_dir
                    if web_dir:
                        ui_log = resolved_log_path(config.logging, UI_LOG_FILENAME)
                        ui_pid = spawn_ui_server(
                            config.ui.host,
                            config.ui.port,
                            web_dir,
                            ui_log,
                            daemon_port=http_port,
                            ws_port=ws_port,
                        )
                        if ui_pid:
                            ui_pid_file = gobby_dir / "ui.pid"
                            with open(ui_pid_file, "w") as f:
                                f.write(str(ui_pid))

            # Compact startup summary
            click.echo("")
            click.echo(
                format_startup_summary(
                    pid=process.pid,
                    http_port=http_port,
                    websocket_port=ws_port,
                    ui_url=ui_url,
                    ui_mode=ui_mode_display,
                    log_files=str(runtime_log_file.parent),
                )
            )
            click.echo("")

        except SystemExit:
            raise
        except Exception as e:
            _step(f"Error starting daemon: {e}", error=True)
            sys.exit(1)


def _do_stop(
    ctx: click.Context,
    docker_flag: bool,
    shutdown_intent: str = "stop",
) -> bool:
    """Stop the daemon and return whether shutdown succeeded."""
    config = ctx.obj["config"]
    shutdown_source = "cli_restart" if shutdown_intent == "restart" else "cli_stop"
    # If OS service is installed and running, delegate to it
    docker_stopped = False
    svc = get_service_status()
    if svc.get("installed") and svc.get("running"):
        previous_pid = _get_running_daemon_pid(svc)
        click.echo("Stopping via OS service manager...")
        result = service_stop(shutdown_intent=shutdown_intent, shutdown_source=shutdown_source)
        if result.get("success"):
            if previous_pid is not None:
                _step(f"Waiting for service-managed daemon (PID: {previous_pid}) to exit...")
            else:
                _step("Waiting for service-managed daemon to stop...")
            elapsed = _wait_for_service_stop(
                previous_pid,
                http_port=config.daemon_port,
                timeout=SERVICE_MANAGED_STOP_TIMEOUT_SECONDS,
            )
            if elapsed is None:
                _step(
                    "Service stop returned, but daemon is still running "
                    f"after {SERVICE_MANAGED_STOP_TIMEOUT_SECONDS:.0f}s",
                    error=True,
                )
                return False
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
            return True

    success = stop_daemon_util(
        quiet=False,
        shutdown_intent=shutdown_intent,
        shutdown_source=shutdown_source,
    )

    # Stop Docker containers if requested (only if not already stopped above)
    if docker_flag and not docker_stopped:
        click.echo("Stopping Docker containers...")
        _services_stop(get_gobby_home())

    return bool(success)


@click.command()
@click.option(
    "--docker",
    "docker_flag",
    is_flag=True,
    help="Also stop Docker service containers (Qdrant, FalkorDB)",
)
@click.pass_context
def stop(ctx: click.Context, docker_flag: bool) -> None:
    """Stop the Gobby daemon."""
    sys.exit(0 if _do_stop(ctx, docker_flag) else 1)


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
    help="Also restart Docker service containers (Qdrant, FalkorDB)",
)
@click.pass_context
def restart(ctx: click.Context, verbose: bool, no_ui: bool, docker_flag: bool) -> None:
    """Restart the Gobby daemon (stop then start)."""
    setup_logging(verbose)

    if not _do_stop(ctx, docker_flag, shutdown_intent="restart"):
        sys.exit(1)

    ctx.invoke(start, verbose=verbose, no_ui=no_ui, docker_flag=docker_flag)


@click.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show Gobby daemon operational health dashboard."""
    config = ctx.obj["config"]
    pid_file = get_gobby_home() / "gobby.pid"
    log_dir = resolved_logs_dir(config.logging)

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
    if not _is_process_alive(pid):
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
    ui_mode = None
    ui_url = None
    ui_pid = None

    if ui_enabled:
        ui_resolution = resolve_ui_mode(config)
        ui_mode = ui_resolution.display
        ui_url = f"http://localhost:{http_port}/"
        if ui_resolution.effective == "dev":
            ui_pid_file = get_gobby_home() / "ui.pid"
            if ui_pid_file.exists():
                try:
                    with open(ui_pid_file) as f:
                        _ui_pid = int(f.read().strip())
                    os.kill(_ui_pid, 0)
                    ui_pid = _ui_pid
                except (ProcessLookupError, ValueError, OSError):
                    pass

    # Fetch API status data
    api_data = asyncio.run(fetch_rich_status(http_port, timeout=3.0))
    control_plane_error = None
    if not api_data:
        control_plane_error = (
            f"HTTP control plane unavailable at localhost:{http_port}; "
            "PID exists but /api/admin/status did not respond"
        )

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
        control_plane_error=control_plane_error,
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
        except Exception as exc:
            logger.debug("Could not read daemon PID file for health check: %s", exc, exc_info=True)

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
            try:
                health_payload = response.json()
            except (TypeError, ValueError):
                health_payload = {}
            if isinstance(health_payload, dict) and health_payload.get("status") == "degraded":
                hook_runtime = health_payload.get("hook_runtime")
                runtime_state = (
                    hook_runtime.get("state") if isinstance(hook_runtime, dict) else "unknown"
                )
                click.echo(f"Gobby daemon: degraded (PID: {pid}, hook runtime: {runtime_state})")
                if isinstance(hook_runtime, dict) and isinstance(hook_runtime.get("detail"), str):
                    click.echo(f"  {hook_runtime['detail']}")
                sys.exit(1)
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
    except (httpx.RequestError, httpx.TimeoutException):
        click.echo(f"Gobby daemon: not responding (PID: {pid})")
        sys.exit(1)
