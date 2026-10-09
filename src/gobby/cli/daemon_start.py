"""
`gobby start`: admissions, the service or direct runner launch, and readiness.
"""

import json
import logging
import os
import subprocess  # nosec B404 # subprocess needed for daemon management
import sys
import time
from pathlib import Path
from typing import Any

import click
import httpx

from gobby.agents.spawners.auth_env import has_auth_env
from gobby.cli.daemon_singleton import (
    admit_direct_start,
    service_backend_name,
)
from gobby.config.bootstrap import BootstrapConfigError, load_bootstrap
from gobby.config.logging import RUNTIME_LOG_FILENAME, resolved_log_path
from gobby.runner_front_door import PORT_REUSE_WAIT_SECONDS
from gobby.ui_exposure import UiExposeError, reconcile_ui_exposure
from gobby.utils.dependency_requirements import (
    collect_dependency_report,
    required_dependency_errors,
    unsupported_platform_error,
)
from gobby.utils.dev import worktree_daemon_refusal
from gobby.utils.env import is_test_protect_enabled
from gobby.utils.status import format_startup_summary

from .daemon import _services_start, _step, _wait_for_daemon_health
from .installers.service import get_service_status, service_start
from .ui_mode import resolve_ui_mode
from .utils import (
    get_gobby_home,
    init_local_storage,
    is_port_available,
    wait_for_port_available,
)

logger = logging.getLogger(__name__)

# Readiness subsumes health: serving /api/health is one early step of subsystem
# init, so this budget must never be smaller than DAEMON_HEALTH_TIMEOUT_SECONDS.
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
    report = collect_dependency_report(managed_services=managed_services, include_srt=False)
    return required_dependency_errors(report)


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
        if not wait_for_port_available(http_port, host=bind_host, timeout=PORT_REUSE_WAIT_SECONDS):
            _step(f"Port {http_port} still in use", error=True)
            sys.exit(1)

    if not is_port_available(ws_port, host=bind_host):
        if not wait_for_port_available(ws_port, host=bind_host, timeout=PORT_REUSE_WAIT_SECONDS):
            _step(f"Port {ws_port} still in use", error=True)
            sys.exit(1)

    _step(f"Ports available (HTTP: {http_port}, WS: {ws_port})")

    cmd = [sys.executable, "-m", "gobby.runner"]
    if verbose:
        cmd.append("--verbose")

    if not any(has_auth_env(cli_name) for cli_name in ("claude", "codex")):
        click.secho(
            "warning: no Anthropic/OpenAI API/provider credential env vars detected. "
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
    from gobby.storage.schema_divergence import binary_set_apply_refusal

    if refusal := worktree_daemon_refusal():
        _step(refusal, error=True)
        sys.exit(1)

    if refusal := binary_set_apply_refusal():
        _step(f"Refusing to start: {refusal}", error=True)
        sys.exit(1)

    gobby_dir = get_gobby_home()
    if dependency_errors := _start_dependency_errors():
        for error in dependency_errors:
            _step(error, error=True)
        sys.exit(1)

    pid_file = gobby_dir / "gobby.pid"
    # The service manager is user-global, so test protection never drives it.
    svc = {} if is_test_protect_enabled() else get_service_status()
    platform = svc.get("platform")
    backend = service_backend_name(platform if isinstance(platform, str) else None)
    # Hold the singleton through dependency startup and the hub schema apply, so
    # this start migrates under its own claim. A service start converts the claim
    # to the launch reservation only when it hands off to the service manager.
    claim: PidFileClaim | None = adopt_inherited_claim(pid_file)
    if claim is None:
        claim, admission_error = admit_direct_start(pid_file)
        if admission_error or claim is None:
            _step(admission_error or "Could not claim the daemon singleton", error=True)
            sys.exit(1)
    reserved = False

    try:
        services_result = _services_start(gobby_dir, require_schema_owner=True)
        if services_result.outcome == "failed":
            _step(services_result.detail, error=True)
            sys.exit(1)
        if services_result.outcome == "skipped":
            _step(services_result.detail)
        else:
            _step("Docker services started")

        # The runner reconciles retired config rows and applies migrations.
        # This preflight must tolerate them so a stopped daemon can start.
        config = get_cli_runtime(ctx).read_only_operational_config()
        if config.agent_sandbox.enabled or config.web_chat_sandbox.enabled:
            from gobby.agents.srt_runtime import SrtRuntimeError, verify_srt_installation

            try:
                verify_srt_installation()
            except SrtRuntimeError as exc:
                _step(f"Managed SRT sandbox preflight failed: {exc}", error=True)
                sys.exit(1)

        if svc.get("installed"):
            from gobby.runner_pid_file import SingletonReservationError

            try:
                convert_held_claim_to_reservation(claim, backend=backend)
            except SingletonReservationError as exc:
                _step(str(exc), error=True)
                sys.exit(1)
            claim = None
            reserved = True
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
