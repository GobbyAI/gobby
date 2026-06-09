from __future__ import annotations

import time
from typing import Any

import click
import httpx

from gobby.cli.memory.common import _get_daemon_client

_START_REQUEST_TIMEOUT_SECONDS = 15.0
_WAIT_POLL_INTERVAL_SECONDS = 2.0
_TERMINAL_STATUSES = frozenset({"completed", "failed", "reverted"})


@click.group("dream", invoke_without_command=True)
@click.option("--dry-run", is_flag=True, help="Build the dream plan without mutating memories")
@click.option("--wait", is_flag=True, help="Wait for the dream run to complete")
@click.option(
    "--skip-consolidation",
    is_flag=True,
    help="Skip consolidation planning and leave candidates for review",
)
@click.option("--memory-type", "memory_type", help="Limit the scan to a memory type")
@click.option(
    "--timeout",
    type=click.FloatRange(min=0.0),
    default=900.0,
    show_default=True,
    help="Maximum seconds to wait for completion with --wait",
)
@click.pass_context
def memory_dream(
    ctx: click.Context,
    dry_run: bool,
    wait: bool,
    skip_consolidation: bool,
    memory_type: str | None,
    timeout: float,
) -> None:
    """Review and improve stale memories."""
    if ctx.invoked_subcommand is not None:
        return

    payload = {
        "dry_run": dry_run,
        "wait": wait,
        "skip_consolidation": skip_consolidation,
        "memory_type": memory_type,
    }
    data = _request(
        ctx,
        "/memory/dream",
        method="POST",
        json_data={**payload, "wait": False},
        timeout=_START_REQUEST_TIMEOUT_SECONDS,
    )
    if not data.get("success"):
        raise click.ClickException(str(data.get("error", "memory dream failed")))
    run_id = _run_id(data)
    if not run_id:
        raise click.ClickException("memory dream did not return a run_id")

    click.echo(f"Dream run: {run_id}")
    status = _status(data)
    if status:
        click.echo(f"Status: {status}")
    if not wait:
        click.echo(f"Check status: gobby memory dream status {run_id}")
        return

    completed = _wait_for_completion(ctx, run_id, timeout=timeout, last_status=status)
    run = completed.get("run") or {}
    _print_summary(run.get("summary"))
    if run.get("status") == "failed":
        raise click.ClickException(str(run.get("error") or "memory dream failed"))


@memory_dream.command("status")
@click.argument("run_id")
@click.pass_context
def memory_dream_status(ctx: click.Context, run_id: str) -> None:
    """Show a memory dream run."""
    data = _request(ctx, f"/memory/dream/{run_id}", method="GET")
    if not data.get("success"):
        raise click.ClickException(str(data.get("error", "memory dream status failed")))
    run = data.get("run") or {}
    click.echo(f"Dream run: {run.get('id', run_id)}")
    click.echo(f"Status: {run.get('status', 'unknown')}")
    _print_summary(run.get("summary"))


@memory_dream.command("revert")
@click.argument("run_id")
@click.pass_context
def memory_dream_revert(ctx: click.Context, run_id: str) -> None:
    """Revert a completed memory dream run."""
    data = _request(ctx, f"/memory/dream/{run_id}/revert", method="POST", timeout=300.0)
    if not data.get("success"):
        raise click.ClickException(str(data.get("error", "memory dream revert failed")))
    if data.get("already_reverted"):
        click.echo(f"Dream run already reverted: {run_id}")
        return
    click.echo(f"Reverted dream run: {run_id}")
    click.echo(f"Restored rows: {data.get('restored', 0)}")
    click.echo(f"Deleted created rows: {data.get('deleted_created_memories', 0)}")


def _request(
    ctx: click.Context,
    endpoint: str,
    *,
    method: str,
    json_data: dict[str, Any] | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    client = _get_daemon_client(ctx)
    try:
        response = client.call_http_api(
            endpoint,
            method=method,
            json_data=json_data,
            timeout=timeout,
        )
        response.raise_for_status()
        return dict(response.json())
    except (httpx.HTTPError, ConnectionError, OSError, ValueError) as exc:
        raise click.ClickException(f"Could not reach daemon: {exc}") from exc


def _run_id(data: dict[str, Any]) -> str | None:
    run = data.get("run") or {}
    value = data.get("run_id") or run.get("id")
    return str(value) if value else None


def _status(data: dict[str, Any]) -> str | None:
    run = data.get("run") or {}
    value = data.get("status") or run.get("status")
    return str(value) if value else None


def _wait_for_completion(
    ctx: click.Context,
    run_id: str,
    *,
    timeout: float,
    last_status: str | None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise click.ClickException(
                f"Timed out after {timeout:g}s waiting for dream run {run_id}"
            )
        try:
            data = _request(
                ctx,
                f"/memory/dream/{run_id}",
                method="GET",
                timeout=min(_START_REQUEST_TIMEOUT_SECONDS, remaining),
            )
        except click.ClickException as exc:
            click.echo(f"Warning: failed to poll dream run {run_id}: {exc.message}", err=True)
        else:
            status = _status(data)
            if status and status != last_status:
                click.echo(f"Status: {status}")
                last_status = status
            if status in _TERMINAL_STATUSES:
                return data
        sleep_for = min(_WAIT_POLL_INTERVAL_SECONDS, max(0.0, deadline - time.monotonic()))
        time.sleep(sleep_for)


def _print_summary(summary: Any) -> None:
    if not isinstance(summary, dict):
        return
    click.echo(f"Candidates reviewed: {summary.get('candidates_reviewed', 0)}")
    click.echo(f"Mutations: {summary.get('mutations', 0)}")
    click.echo(f"Snapshots: {summary.get('snapshots', 0)}")
    if summary.get("errors"):
        click.echo(f"Errors: {summary['errors']}")
