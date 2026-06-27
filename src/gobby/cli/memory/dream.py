from __future__ import annotations

from typing import Any, cast

import click
import httpx

from gobby.cli.memory.common import _get_daemon_client


@click.group("dream", invoke_without_command=True)
@click.option("--dry-run", is_flag=True, help="Build the dream plan without mutating memories")
@click.option(
    "--skip-consolidation",
    is_flag=True,
    help="Skip consolidation planning and leave candidates for review",
)
@click.option("--memory-type", "memory_type", help="Limit the scan to a memory type")
@click.option(
    "--full/--no-full",
    "full_sweep",
    default=False,
    show_default=True,
    help="Ignore the redream cooldown and sweep every active in-scope memory once",
)
@click.option(
    "--timeout",
    type=click.FloatRange(min=0.0),
    default=900.0,
    show_default=True,
    help="Maximum seconds to wait for the sweep to complete",
)
@click.pass_context
def memory_dream(
    ctx: click.Context,
    dry_run: bool,
    skip_consolidation: bool,
    memory_type: str | None,
    full_sweep: bool,
    timeout: float,
) -> None:
    """Review stale memories across every project with due memories.

    Sweeps each project that has due memories under its own truth digest and
    prints a per-project summary. The sweep runs synchronously; use --timeout to
    bound the wait.
    """
    if ctx.invoked_subcommand is not None:
        return

    data = _request(
        ctx,
        "/memory/dream",
        method="POST",
        json_data={
            "dry_run": dry_run,
            "skip_consolidation": skip_consolidation,
            "memory_type": memory_type,
            "full_sweep": full_sweep,
        },
        timeout=timeout,
    )
    if not data.get("success"):
        raise click.ClickException(str(data.get("error", "memory dream failed")))
    _print_aggregate(data)


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
    except (httpx.TransportError, ConnectionError, OSError) as exc:
        raise click.ClickException(f"Could not reach daemon: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as status_exc:
            raise click.ClickException(
                f"Daemon returned HTTP {response.status_code}: {response.text}"
            ) from status_exc
        raise click.ClickException(f"Daemon returned invalid JSON: {exc}") from exc

    if response.is_error:
        detail = payload.get("error") if isinstance(payload, dict) else None
        if not detail and isinstance(payload, dict):
            detail = payload.get("detail") or payload.get("message")
        raise click.ClickException(str(detail or f"Daemon returned HTTP {response.status_code}"))

    if not isinstance(payload, dict):
        raise click.ClickException("Daemon returned an unexpected response")
    return cast("dict[str, Any]", payload)


def _print_aggregate(data: dict[str, Any]) -> None:
    targets = data.get("targets", 0)
    completed = data.get("completed", 0)
    failed = data.get("failed", 0)
    mutations = data.get("mutations", 0)
    tail = f", {failed} failed" if failed else ""
    click.echo(f"Swept {completed}/{targets} project(s): {mutations} mutation(s) total{tail}")
    runs = data.get("runs")
    if not isinstance(runs, list):
        return
    for run in runs:
        if not isinstance(run, dict):
            continue
        scope = run.get("project_id") or "global"
        if run.get("success"):
            run_id = run.get("run_id")
            suffix = f" (run {run_id})" if run_id else ""
            click.echo(f"  - {scope}: {run.get('mutations', 0)} mutation(s){suffix}")
        else:
            click.echo(f"  - {scope}: failed — {run.get('error', 'unknown error')}")


def _print_summary(summary: Any) -> None:
    if not isinstance(summary, dict):
        return
    click.echo(f"Candidates reviewed: {summary.get('candidates_reviewed', 0)}")
    click.echo(f"Mutations: {summary.get('mutations', 0)}")
    click.echo(f"Snapshots: {summary.get('snapshots', 0)}")
    if summary.get("errors"):
        click.echo(f"Errors: {summary['errors']}")
