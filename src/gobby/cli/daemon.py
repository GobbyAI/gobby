"""
Daemon management commands.
"""

import asyncio
import logging
import math
import os
import sys
import time
from pathlib import Path
from typing import Any

import click
import httpx
import psutil

from gobby.cli.daemon_preflight import restart_start_refusal
from gobby.cli.daemon_singleton import (
    format_singleton_status,
    stop_singleton_gate,
)
from gobby.config.logging import (
    resolved_logs_dir,
)
from gobby.runner_pid_file import ProbeState, probe_daemon_lock
from gobby.sessions.handoff_shutdown import HandoffShutdownBlocked
from gobby.utils.dependency_requirements import (
    unsupported_platform_error,
)
from gobby.utils.env import is_test_protect_enabled
from gobby.utils.status import fetch_rich_status, format_status_message

from ._daemon_handoffs import protect_pending_handoffs
from ._daemon_protected_runs import clear_protected_runs, fetch_protected_runs
from ._daemon_services import (
    ServiceStartResult,
    start_managed_services,
    stop_managed_services,
)
from .installers.compose_env import resolve_compose_runtime
from .installers.service import (
    get_service_status,
    service_stop,
)
from .ui_mode import resolve_ui_mode
from .utils import (
    _is_process_alive,
    format_uptime,
    get_gobby_home,
    kill_all_gobby_daemons,
    setup_logging,
)
from .utils import (
    stop_daemon as stop_daemon_util,
)
from .utils_process import get_port_listener_pid

logger = logging.getLogger(__name__)

__all__ = ["kill_all_gobby_daemons"]

SERVICE_MANAGED_STOP_TIMEOUT_SECONDS = 75.0

DAEMON_HEALTH_TIMEOUT_SECONDS = 120.0


def _services_start(gobby_home: Path, *, require_schema_owner: bool = False) -> ServiceStartResult:
    return start_managed_services(
        gobby_home,
        resolve_runtime=resolve_compose_runtime,
        require_schema_owner=require_schema_owner,
    )


def _services_stop(gobby_home: Path) -> bool:
    return stop_managed_services(gobby_home, resolve_runtime=resolve_compose_runtime)


def _step(msg: str, *, error: bool = False, scheduled: bool = False) -> None:
    """Print a startup/shutdown step with consistent formatting."""
    if error:
        click.echo(f"  ! {msg}", err=True)
    elif scheduled:
        click.echo(f"  ~ {msg}")
    else:
        click.echo(f"  + {msg}")


def _wait_for_daemon_health(
    http_port: int,
    *,
    timeout: float = DAEMON_HEALTH_TIMEOUT_SECONDS,
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
        response = httpx.get(f"http://localhost:{http_port}/api/health", timeout=1.0)
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


def _is_child_of(pid: int, parent_pid: int) -> bool:
    """Whether `pid` is a child of `parent_pid`, as the runner's gdaemon front door is."""
    try:
        actual_parent: int = psutil.Process(pid).ppid()
    except psutil.Error:
        return False
    return actual_parent == parent_pid


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


def _unload_service_job(
    svc: dict[str, Any],
    shutdown_intent: str,
    shutdown_source: str,
    drain_terminals: bool,
) -> bool:
    """Unload a service job with no running daemon so its manager cannot revive one."""
    click.echo(f"Unloading the {svc.get('platform', 'OS')} service so it cannot relaunch...")
    result = service_stop(
        shutdown_intent=shutdown_intent,
        shutdown_source=shutdown_source,
        drain_terminals=drain_terminals,
    )
    if result.get("success"):
        return True
    _step(f"Service unload failed: {result.get('error')}", error=True)
    return False


def _do_stop(
    ctx: click.Context,
    docker_flag: bool,
    shutdown_intent: str = "stop",
    *,
    force: bool = False,
    wait: bool = False,
    drain_terminals: bool = False,
) -> bool:
    """Stop the daemon and return whether shutdown succeeded.

    ``drain_terminals`` is the only path that takes the gterm host (and its
    native terminals) down with the daemon; by default the host survives.
    """
    from gobby.cli.runtime import get_cli_runtime

    if force and wait:
        raise click.UsageError("--force and --wait are mutually exclusive")

    shutdown_source = "cli_restart" if shutdown_intent == "restart" else "cli_stop"
    pid_file = get_gobby_home() / "gobby.pid"
    gate, gate_error = stop_singleton_gate(pid_file)
    if gate == "refuse":
        click.echo(gate_error or "Refusing to stop a non-daemon singleton holder", err=True)
        return False
    if gate == "cancelled":
        # The cancelled start's job stays loaded and would relaunch the runner.
        svc = {} if is_test_protect_enabled() else get_service_status()
        if svc.get("installed") and svc.get("enabled"):
            return _unload_service_job(svc, shutdown_intent, shutdown_source, drain_terminals)
        return True

    config = get_cli_runtime(ctx).read_only_operational_config()
    # A restart-protected cron run (nightly memory dream) holds a lease the
    # daemon reports; honor it before either stop path can kill the run.
    if not clear_protected_runs(
        config.daemon_port,
        force=force,
        wait=wait,
        step=_step,
        fetch=fetch_protected_runs,
    ):
        return False
    try:
        with protect_pending_handoffs(get_cli_runtime(ctx), force=force, wait=wait, report=_step):
            # If OS service is installed and running, delegate to it. The service
            # manager is user-global, so test protection never drives it.
            docker_stopped = False
            docker_stop_succeeded = True
            unloaded = True
            svc = {} if is_test_protect_enabled() else get_service_status()
            if svc.get("installed") and svc.get("enabled") and not svc.get("running"):
                # A crash-looping job reports loaded but not running between
                # relaunches; unload it, then stop any direct-started daemon below.
                unloaded = _unload_service_job(
                    svc, shutdown_intent, shutdown_source, drain_terminals
                )
            elif svc.get("installed") and svc.get("running"):
                previous_pid = _get_running_daemon_pid(svc)
                click.echo("Stopping via OS service manager...")
                result = service_stop(
                    shutdown_intent=shutdown_intent,
                    shutdown_source=shutdown_source,
                    drain_terminals=drain_terminals,
                )
                if result.get("success"):
                    if previous_pid is not None:
                        _step(
                            f"Waiting for service-managed daemon (PID: {previous_pid}) to exit..."
                        )
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
                    _step(
                        f"Daemon stopped via {svc.get('platform', 'OS')} service ({elapsed:.1f}s)"
                    )
                else:
                    click.echo(f"Service stop failed: {result.get('error')}", err=True)
                    click.echo("Falling back to direct stop...")

                # Stop Docker containers if requested
                if docker_flag and config.datastore_mode != "remote":
                    click.echo("Stopping Docker containers...")
                    docker_stop_succeeded = _services_stop(get_gobby_home())
                    docker_stopped = True

                if result.get("success"):
                    return docker_stop_succeeded

            success = stop_daemon_util(
                quiet=False,
                shutdown_intent=shutdown_intent,
                shutdown_source=shutdown_source,
                drain_terminals=drain_terminals,
            )

            # Stop Docker containers if requested (only if not already stopped above)
            if docker_flag and not docker_stopped and config.datastore_mode != "remote":
                click.echo("Stopping Docker containers...")
                docker_stop_succeeded = _services_stop(get_gobby_home())

            return bool(success and docker_stop_succeeded and unloaded)
    except HandoffShutdownBlocked as exc:
        _step(f"Refusing to stop: {exc}", error=True)
        return False


@click.command()
@click.option(
    "--docker",
    "docker_flag",
    is_flag=True,
    help="Also stop the managed PostgreSQL, Qdrant, and FalkorDB containers (compose stop; never removes them)",
)
@click.option(
    "--force",
    "force",
    is_flag=True,
    help=(
        "Interrupt an active restart-protected cron run and bypass unresolved session handoffs "
        "(the run resumes after the next start)"
    ),
)
@click.option(
    "--wait",
    "wait",
    is_flag=True,
    help="Wait for protected cron runs and unresolved session handoffs before stopping",
)
@click.option(
    "--terminals",
    "drain_terminals",
    is_flag=True,
    help="Also stop the gterm host and every native terminal it owns (they survive by default)",
)
@click.pass_context
def stop(
    ctx: click.Context,
    docker_flag: bool,
    force: bool,
    wait: bool,
    drain_terminals: bool,
) -> None:
    """Stop the Gobby daemon."""
    stopped = _do_stop(ctx, docker_flag, force=force, wait=wait, drain_terminals=drain_terminals)
    sys.exit(0 if stopped else 1)


@click.command()
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Enable verbose debug output",
)
@click.option(
    "--docker",
    "docker_flag",
    is_flag=True,
    help="Also restart the managed PostgreSQL, Qdrant, and FalkorDB containers",
)
@click.option(
    "--force",
    "force",
    is_flag=True,
    help=(
        "Interrupt an active restart-protected cron run and bypass unresolved session handoffs "
        "(the run resumes after the restart)"
    ),
)
@click.option(
    "--wait",
    "wait",
    is_flag=True,
    help="Wait for protected cron runs and unresolved session handoffs before restarting",
)
@click.option(
    "--terminals",
    "drain_terminals",
    is_flag=True,
    help="Also restart the gterm host, ending every native terminal it owns",
)
@click.pass_context
def restart(
    ctx: click.Context,
    verbose: bool,
    docker_flag: bool,
    force: bool,
    wait: bool,
    drain_terminals: bool,
    expected_identity: dict[str, int | str] | None = None,
) -> None:
    """Restart the Gobby daemon (stop then start)."""
    if verbose:
        setup_logging(True)

    # Check before stopping: refusing after the stop would leave no daemon.
    refusal = (
        restart_start_refusal(ctx)
        if expected_identity is None
        else restart_start_refusal(ctx, expected_identity=expected_identity)
    )
    if refusal:
        _step(f"Refusing to restart: {refusal}", error=True)
        _step("The running daemon was left alone.")
        sys.exit(1)

    if not _do_stop(
        ctx,
        docker_flag,
        shutdown_intent="restart",
        force=force,
        wait=wait,
        drain_terminals=drain_terminals,
    ):
        sys.exit(1)

    from .daemon_start import start

    ctx.invoke(start, verbose=verbose)


@click.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show Gobby daemon operational health dashboard."""
    from gobby.cli.runtime import get_cli_runtime, require_cli_database
    from gobby.storage.hub.managed import managed_grant_path
    from gobby.storage.schema_divergence import collect_installed_binary_set, collect_schema_heads

    if managed_grant_path() is not None:
        from gobby.cli.daemon_health import report_managed_health

        report_managed_health(ctx)

    binary_set = collect_installed_binary_set()
    if binary_set.mixed:
        click.echo(binary_set.describe())

    if unsupported_platform_error():
        click.echo(format_status_message(running=False, unsupported_platform=True))
        sys.exit(0)

    gobby_home = get_gobby_home()
    probe = probe_daemon_lock(gobby_home / "gobby.pid")
    if probe.state is not ProbeState.DAEMON:
        if probe.state is ProbeState.ABSENT:
            click.echo(format_status_message(running=False))
        else:
            click.echo(format_singleton_status(probe))
        sys.exit(0)

    config = get_cli_runtime(ctx).read_only_operational_config()
    log_dir = resolved_logs_dir(config.logging)

    reported_pid = _read_pid_file()
    pid_source = "PID file"
    if reported_pid is None:
        svc = get_service_status()
        if svc.get("running") and svc.get("pid"):
            reported_pid = svc["pid"]
            pid_source = "Service manager"
        else:
            click.echo(format_status_message(running=False))
            sys.exit(0)

    http_port = config.daemon_port
    reported_is_live = _is_process_alive(reported_pid)
    try:
        listener_pid = get_port_listener_pid(http_port)
    except (OSError, psutil.Error) as exc:
        logger.debug("Failed to inspect HTTP port %s ownership: %s", http_port, exc)
        listener_pid = None
    listener_is_live = (
        listener_pid is not None
        and listener_pid != reported_pid
        and _is_process_alive(listener_pid)
        and not (reported_is_live and _is_child_of(listener_pid, reported_pid))
    )
    pid = listener_pid if listener_is_live else reported_pid if reported_is_live else None

    notes: list[str] = []
    if pid_source == "PID file" and not reported_is_live:
        notes.append(f"Note: Stale PID file found (PID {reported_pid})")
    if listener_is_live:
        notes.append(
            f"Note: PID mismatch: {pid_source} reports {reported_pid}; "
            f"HTTP port {http_port} is owned by PID {listener_pid}"
        )

    if pid is None:
        click.echo(format_status_message(running=False))
        for note in notes:
            click.echo(note)
        sys.exit(0)

    # Get process info for uptime
    uptime_seconds: float | None = None
    try:
        process = psutil.Process(pid)
        observed_uptime = time.time() - process.create_time()
        if observed_uptime >= 0 and math.isfinite(observed_uptime):
            uptime_seconds = observed_uptime
            uptime_str = format_uptime(observed_uptime)
        else:
            uptime_str = None
    except Exception:
        uptime_str = None

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
            ui_pid_file = gobby_home / "ui.pid"
            if ui_pid_file.exists():
                try:
                    with open(ui_pid_file) as f:
                        _ui_pid = int(f.read().strip())
                    os.kill(_ui_pid, 0)
                    ui_pid = _ui_pid
                except (ProcessLookupError, ValueError, OSError):
                    pass

    # Fetch API status data
    status_probe = asyncio.run(fetch_rich_status(http_port, timeout=3.0))
    api_data = status_probe.api_data
    control_plane_error = None
    status_details_error = None
    if status_probe.status_failure:
        status_failure = status_probe.status_failure.describe()
        if status_probe.health_confirmed:
            status_details_error = (
                f"temporarily unavailable; {status_failure}; "
                f"fallback /api/health is healthy; PID {pid}"
            )
        else:
            health_failure = (
                status_probe.health_failure.describe()
                if status_probe.health_failure
                else "endpoint /api/health did not confirm daemon health"
            )
            control_plane_error = f"{status_failure}; {health_failure}; PID {pid}"

    # Collect dependency/CLI version info
    from gobby.utils.deps import check_config_mismatches, collect_all_deps

    try:
        managed_services = (gobby_home / "services" / "docker-compose.yml").is_file()
        deps_info = collect_all_deps(
            require_cli_database(ctx, apply_migrations=False),
            managed_services=managed_services,
        )
    except Exception as exc:
        logger.warning("Failed to collect CLI dependency status", exc_info=True)
        exception_message = " ".join(str(exc).split())[:160]
        detail = (
            f"{type(exc).__name__}: {exception_message}"
            if exception_message
            else type(exc).__name__
        )
        deps_info = {
            "dependencies": {
                "required": {
                    "status": {
                        "state": "invalid",
                        "installed_version": None,
                        "minimum_version": None,
                        "expected_version": None,
                        "path": None,
                        "error": f"Dependency status collection failed: {detail}",
                    }
                },
                "optional": {},
            },
            "integrations": {
                "embeddings_provider": {
                    "status": "degraded",
                    "error": detail,
                }
            },
        }
    config_issues = check_config_mismatches(config)

    try:
        schema_heads = collect_schema_heads(require_cli_database(ctx, apply_migrations=False))
    except Exception:
        logger.debug("Failed to collect schema heads", exc_info=True)
        schema_heads = collect_schema_heads(None)

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
        schema_heads=schema_heads,
        control_plane_error=control_plane_error,
        status_details_error=status_details_error,
        process_uptime_seconds=uptime_seconds,
    )
    click.echo(message)
    for note in notes:
        click.echo(note)
    sys.exit(0)
