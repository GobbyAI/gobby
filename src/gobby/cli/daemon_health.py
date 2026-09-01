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
from gobby.storage.schema_divergence import collect_schema_heads

from .installers.service import get_service_status
from .utils import _is_process_alive, format_uptime, get_gobby_home

logger = logging.getLogger(__name__)


@click.command()
@click.pass_context
def health(ctx: click.Context) -> None:
    """Quick one-line daemon health check."""
    from gobby.cli.runtime import get_cli_runtime, require_cli_database

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
