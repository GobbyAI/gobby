"""The read-only ``gobby health`` command."""

from __future__ import annotations

import logging
import sys
import time

import click
import httpx
import psutil

from gobby.cli.daemon_singleton import format_singleton_status
from gobby.runner_pid_file import ProbeState, probe_daemon_lock
from gobby.storage.hub.managed import managed_grant_path
from gobby.storage.schema_divergence import collect_schema_heads
from gobby.utils.daemon_client import DaemonAuthenticationError, DaemonClient
from gobby.utils.daemon_url import resolve_daemon_url

from .installers.service import get_service_status
from .utils import _is_process_alive, format_uptime, get_gobby_home

logger = logging.getLogger(__name__)


def report_managed_health(ctx: click.Context) -> None:
    """Report daemon API health without opening the managed seat's restricted hub."""
    from gobby.cli.runtime import get_cli_runtime

    url = resolve_daemon_url(bootstrap_path=get_cli_runtime(ctx).config_file)
    try:
        response = DaemonClient(url=url, timeout=2.0).call_http_api("/api/health", method="GET")
    except DaemonAuthenticationError:
        click.echo("Gobby daemon: authentication failed")
        ctx.exit(1)
    except httpx.RequestError:
        click.echo("Gobby daemon: not responding")
        ctx.exit(1)

    if response.status_code != 200:
        click.echo(f"Gobby daemon: unhealthy (HTTP {response.status_code})")
        ctx.exit(1)
    try:
        payload = response.json()
    except (TypeError, ValueError):
        payload = None
    if not isinstance(payload, dict) or payload.get("status") not in ("ok", "degraded"):
        click.echo("Gobby daemon: invalid health response")
        ctx.exit(1)
    if payload["status"] == "degraded":
        click.echo("Gobby daemon: degraded")
        services = payload.get("degraded_services")
        if isinstance(services, list) and all(isinstance(service, str) for service in services):
            if services:
                click.echo(f"  Degraded services: {', '.join(services)}")
        hook_runtime = payload.get("hook_runtime")
        if isinstance(hook_runtime, dict):
            for key in ("state", "detail"):
                if isinstance(hook_runtime.get(key), str):
                    click.echo(f"  Hook runtime {key}: {hook_runtime[key]}")
        ctx.exit(1)
    click.echo("Gobby daemon: healthy (daemon API)")
    ctx.exit(0)


@click.command()
@click.pass_context
def health(ctx: click.Context) -> None:
    """Quick one-line daemon health check."""
    from gobby.cli.runtime import get_cli_runtime, require_cli_database

    if managed_grant_path() is not None:
        report_managed_health(ctx)

    pid_file = get_gobby_home() / "gobby.pid"
    probe = probe_daemon_lock(pid_file)
    if probe.state is not ProbeState.DAEMON:
        click.echo(format_singleton_status(probe))
        sys.exit(1)

    config = get_cli_runtime(ctx).read_only_operational_config()
    http_port = config.daemon_port
    pid = probe.pid

    try:
        schema_heads = collect_schema_heads(require_cli_database(ctx, apply_migrations=False))
    except Exception:
        logger.debug("Failed to collect schema heads", exc_info=True)
        schema_heads = collect_schema_heads(None)
    if schema_heads.diverged:
        click.echo(f"Schema: {schema_heads.describe()}")

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
        click.echo(f"Gobby daemon: unhealthy (HTTP {response.status_code})")
        sys.exit(1)
    except (httpx.RequestError, httpx.TimeoutException):
        click.echo(f"Gobby daemon: not responding (PID: {pid})")
        sys.exit(1)
