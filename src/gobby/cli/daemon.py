"""
Daemon management commands.
"""

import asyncio
import json
import logging
import math
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
from gobby.cli.daemon_singleton import (
    admit_direct_start,
    admit_service_start,
    format_singleton_status,
    probe_start_blocker,
    service_backend_name,
    stop_singleton_gate,
)
from gobby.config.bootstrap import BootstrapConfigError, load_bootstrap
from gobby.config.logging import (
    RUNTIME_LOG_FILENAME,
    resolved_log_path,
    resolved_logs_dir,
)
from gobby.runner_pid_file import ProbeState, probe_daemon_lock
from gobby.ui_exposure import UiExposeError, reconcile_ui_exposure
from gobby.utils.dependency_requirements import (
    collect_dependency_report,
    required_dependency_errors,
    unsupported_platform_error,
)
from gobby.utils.dev import worktree_daemon_refusal
from gobby.utils.status import fetch_rich_status, format_startup_summary, format_status_message

from ._daemon_protected_runs import clear_protected_runs, fetch_protected_runs
from ._daemon_services import (
    ServiceStartResult,
    start_managed_services,
    stop_managed_services,
)
from .installers.compose_env import resolve_compose_runtime
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
    wait_for_port_available,
)
from .utils import (
    stop_daemon as stop_daemon_util,
)
from .utils_process import get_port_listener_pid

logger = logging.getLogger(__name__)

__all__ = ["kill_all_gobby_daemons"]

SERVICE_MANAGED_STOP_TIMEOUT_SECONDS = 75.0

DAEMON_HEALTH_TIMEOUT_SECONDS = 120.0

# Readiness subsumes health: serving /api/health is one early step of subsystem
# init, so this budget must never be smaller than the health budget above.
# Subsystem startup is fail-soft and progress-terminal — a failed init records an
# error and still finishes the tracker — so an unfinished poll only ever means
# "still initializing", never "broken". Sized against measured time-to-ready,
# which reached 168s on a loaded machine while the previous 60s budget reported
# that healthy daemon as a failed start.
STARTUP_READINESS_TIMEOUT_SECONDS = 300.0


def _start_dependency_errors() -> list[str]:
    if platform_error := unsupported_platform_error():
        return [platform_error]
    gobby_home = get_gobby_home()
    try:
        bootstrap = load_bootstrap(str(gobby_home / "bootstrap.yaml"))
    except BootstrapConfigError as exc:
        return [f"Invalid bootstrap.yaml: {exc}"]
    managed_services = (
        bootstrap.datastore_mode == "local"
        and (gobby_home / "services" / "docker-compose.yml").is_file()
    )
    report = collect_dependency_report(managed_services=managed_services, include_srt=True)
    return required_dependency_errors(report)


def _services_start(gobby_home: Path) -> ServiceStartResult:
    return start_managed_services(gobby_home, resolve_runtime=resolve_compose_runtime)


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


def _reconcile_ui_exposure(daemon_port: int) -> None:
    try:
        result = reconcile_ui_exposure(daemon_port)
    except UiExposeError as exc:
        click.secho(f"warning: UI exposure reconciliation failed: {exc}", fg="yellow")
        return

    if result is not None:
        _step(f"Web UI exposed at {result.url}")


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


def _poll_startup_progress(
    http_port: int, max_wait: float = STARTUP_READINESS_TIMEOUT_SECONDS
) -> bool:
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


def _launch_direct_runner(
    claim: Any,
    pid_file: Path,
    gobby_dir: Path,
    config: Any,
    verbose: bool,
) -> None:
    """Launch the runner subprocess with the inherited singleton descriptor."""
    runtime_log_file = resolved_log_path(config.logging, RUNTIME_LOG_FILENAME)
    gobby_dir.mkdir(parents=True, exist_ok=True)
    runtime_log_file.parent.mkdir(parents=True, exist_ok=True)

    click.echo("Starting Gobby daemon...")
    click.echo("")

    hub_db = init_local_storage()
    hub_db.close()
    _step("PostgreSQL hub initialized")

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

    cmd = [sys.executable, "-m", "gobby.runner"]
    if verbose:
        cmd.append("--verbose")

    if not any(has_auth_env(cli_name) for cli_name in ("claude", "codex", "qwen")):
        click.secho(
            "warning: no Anthropic/OpenAI/Qwen API/provider credential env vars detected. "
            "Spawned agents may prompt for login unless the CLI has on-disk credentials.",
            fg="yellow",
        )

    env = os.environ.copy()
    env.update(claim.inherit_environment())
    popen_kwargs: dict[str, Any] = {
        "stdout": None,
        "stderr": None,
        "stdin": subprocess.DEVNULL,
        "start_new_session": True,
        "env": env,
    }
    if os.name == "posix":
        popen_kwargs["close_fds"] = True
        popen_kwargs["pass_fds"] = (claim.fileno(),)

    with open(runtime_log_file, "a") as runtime_log:
        popen_kwargs["stdout"] = runtime_log
        popen_kwargs["stderr"] = runtime_log
        try:
            process = subprocess.Popen(cmd, **popen_kwargs)  # nosec B603
            pid_file.write_text(str(process.pid), encoding="utf-8")
            claim.detach()

            time.sleep(1.0)
            if process.poll() is not None:
                _step("Daemon process exited immediately", error=True)
                _show_runtime_output_tail(runtime_log_file)
                sys.exit(1)

            _step(f"Daemon process launched (PID: {process.pid})")
            time.sleep(2.0)
            elapsed = _wait_for_daemon_health(http_port)
            if elapsed is not None:
                _step(f"Health check passed ({elapsed:.1f}s)")
            else:
                _step("Health check failed", error=True)
                _show_runtime_output_tail(runtime_log_file)
                sys.exit(1)

            if not _poll_startup_progress(http_port):
                _step("Startup readiness did not complete", error=True)
                _show_runtime_output_tail(runtime_log_file)
                sys.exit(1)

            _reconcile_ui_exposure(http_port)

            ui_url = None
            ui_mode_display = None
            if config.ui.enabled:
                ui_resolution = resolve_ui_mode(config)
                ui_mode_display = ui_resolution.display
                ui_port = 60889 if ui_resolution.effective == "dev" else http_port
                ui_url = f"http://localhost:{ui_port}/"

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
        except Exception as exc:
            _step(f"Error starting daemon: {exc}", error=True)
            sys.exit(1)


@click.command()
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Enable verbose debug output",
)
@click.pass_context
def start(ctx: click.Context, verbose: bool) -> None:
    """Start the Gobby daemon."""
    from gobby.cli.runtime import get_cli_runtime
    from gobby.runner_pid_file import (
        PidFileClaim,
        adopt_inherited_claim,
        cancel_service_reservation,
        convert_held_claim_to_reservation,
    )

    if refusal := worktree_daemon_refusal():
        _step(refusal, error=True)
        sys.exit(1)

    gobby_dir = get_gobby_home()
    if dependency_errors := _start_dependency_errors():
        for error in dependency_errors:
            _step(error, error=True)
        sys.exit(1)

    pid_file = gobby_dir / "gobby.pid"
    svc = get_service_status()
    claim: PidFileClaim | None = adopt_inherited_claim(pid_file)
    reserved = False
    platform = svc.get("platform")
    backend = service_backend_name(platform if isinstance(platform, str) else None)
    if claim is not None and svc.get("installed"):
        from gobby.runner_pid_file import SingletonReservationError

        try:
            convert_held_claim_to_reservation(claim, backend=backend)
        except SingletonReservationError as exc:
            _step(str(exc), error=True)
            sys.exit(1)
        claim = None
        reserved = True
    elif claim is not None:
        pass
    elif svc.get("installed"):
        admission_error = admit_service_start(pid_file, backend=backend)
        if admission_error:
            _step(admission_error, error=True)
            sys.exit(1)
        reserved = True
    else:
        blocker = probe_start_blocker(probe_daemon_lock(pid_file))
        if blocker:
            _step(blocker, error=True)
            sys.exit(1)
        claim, admission_error = admit_direct_start(pid_file)
        if admission_error or claim is None:
            _step(admission_error or "Could not claim the daemon singleton", error=True)
            sys.exit(1)

    try:
        services_result = _services_start(gobby_dir)
        if services_result.outcome == "failed":
            _step(services_result.detail, error=True)
            sys.exit(1)
        if services_result.outcome == "skipped":
            _step(services_result.detail)
        else:
            _step("Docker services started")

        config = get_cli_runtime(ctx).operational_config
        if config.agent_sandbox.enabled or config.web_chat_sandbox.enabled:
            from gobby.agents.srt_runtime import SrtRuntimeError, verify_srt_installation

            try:
                verify_srt_installation()
            except SrtRuntimeError as exc:
                _step(f"Managed SRT sandbox preflight failed: {exc}", error=True)
                sys.exit(1)

        if svc.get("installed"):
            _step("Starting via OS service manager...")
            result = service_start(reserved=True)
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
                _reconcile_ui_exposure(config.daemon_port)
                reserved = False
                return
            _step(f"Service start failed: {result.get('error')}", error=True)
            click.echo("  Falling back to direct start...")
            if reserved:
                cancel_service_reservation(pid_file)
                reserved = False
            if claim is None:
                claim, admission_error = admit_direct_start(pid_file)
                if admission_error or claim is None:
                    _step(admission_error or "Could not claim the daemon singleton", error=True)
                    sys.exit(1)

        _launch_direct_runner(claim, pid_file, gobby_dir, config, verbose)
        claim = None
    finally:
        if claim is not None:
            claim.release()
        if reserved:
            cancel_service_reservation(pid_file)


def _do_stop(
    ctx: click.Context,
    docker_flag: bool,
    shutdown_intent: str = "stop",
    *,
    force: bool = False,
    wait: bool = False,
) -> bool:
    """Stop the daemon and return whether shutdown succeeded."""
    from gobby.cli.runtime import get_cli_runtime

    if force and wait:
        raise click.UsageError("--force and --wait are mutually exclusive")

    pid_file = get_gobby_home() / "gobby.pid"
    gate, gate_error = stop_singleton_gate(pid_file)
    if gate == "refuse":
        click.echo(gate_error or "Refusing to stop a non-daemon singleton holder", err=True)
        return False
    if gate == "cancelled":
        return True

    config = get_cli_runtime(ctx).operational_config
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
    shutdown_source = "cli_restart" if shutdown_intent == "restart" else "cli_stop"
    # If OS service is installed and running, delegate to it
    docker_stopped = False
    docker_stop_succeeded = True
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
    )

    # Stop Docker containers if requested (only if not already stopped above)
    if docker_flag and not docker_stopped and config.datastore_mode != "remote":
        click.echo("Stopping Docker containers...")
        docker_stop_succeeded = _services_stop(get_gobby_home())

    return bool(success and docker_stop_succeeded)


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
    help="Interrupt an active restart-protected cron run (it resumes after the next start)",
)
@click.option(
    "--wait",
    "wait",
    is_flag=True,
    help="Defer the stop until active restart-protected cron runs finish",
)
@click.pass_context
def stop(ctx: click.Context, docker_flag: bool, force: bool, wait: bool) -> None:
    """Stop the Gobby daemon."""
    sys.exit(0 if _do_stop(ctx, docker_flag, force=force, wait=wait) else 1)


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
    help="Interrupt an active restart-protected cron run (it resumes after the restart)",
)
@click.option(
    "--wait",
    "wait",
    is_flag=True,
    help="Defer the restart until active restart-protected cron runs finish",
)
@click.pass_context
def restart(
    ctx: click.Context,
    verbose: bool,
    docker_flag: bool,
    force: bool,
    wait: bool,
) -> None:
    """Restart the Gobby daemon (stop then start)."""
    if verbose:
        setup_logging(True)

    # Check before stopping: refusing after the stop would leave no daemon.
    if refusal := worktree_daemon_refusal():
        _step(refusal, error=True)
        sys.exit(1)

    if not _do_stop(ctx, docker_flag, shutdown_intent="restart", force=force, wait=wait):
        sys.exit(1)

    ctx.invoke(start, verbose=verbose)


@click.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show Gobby daemon operational health dashboard."""
    from gobby.cli.runtime import get_cli_runtime, require_cli_database

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

    config = get_cli_runtime(ctx).operational_config
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
            require_cli_database(ctx),
            managed_services=managed_services,
        )
    except Exception as exc:
        logger.debug("Failed to collect CLI dependency status", exc_info=True)
        deps_info = {
            "dependencies": {
                "required": {
                    "status": {
                        "state": "invalid",
                        "installed_version": None,
                        "minimum_version": None,
                        "expected_version": None,
                        "path": None,
                        "error": f"Dependency status collection failed: {type(exc).__name__}",
                    }
                },
                "optional": {},
            },
            "integrations": {
                "embeddings_provider": {
                    "status": "degraded",
                    "error": type(exc).__name__,
                }
            },
        }
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
        status_details_error=status_details_error,
        process_uptime_seconds=uptime_seconds,
    )
    click.echo(message)
    for note in notes:
        click.echo(note)
    sys.exit(0)


@click.command()
@click.pass_context
def health(ctx: click.Context) -> None:
    """Quick one-line daemon health check."""
    from gobby.cli.runtime import get_cli_runtime

    pid_file = get_gobby_home() / "gobby.pid"
    probe = probe_daemon_lock(pid_file)
    if probe.state is not ProbeState.DAEMON:
        click.echo(format_singleton_status(probe))
        sys.exit(1)

    config = get_cli_runtime(ctx).operational_config
    http_port = config.daemon_port
    pid = probe.pid

    if pid is None:
        svc = get_service_status()
        if svc.get("running") and svc.get("pid"):
            pid = svc["pid"]

    if pid is None or not _is_process_alive(pid):
        click.echo("Gobby daemon: not running")
        sys.exit(1)

    try:
        response = httpx.get(f"http://localhost:{http_port}/api/health", timeout=2.0)
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
