from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any

import click
import httpx


def _facade() -> ModuleType:
    return importlib.import_module("gobby.cli.memory")


@click.group("dream", invoke_without_command=True)
@click.option("--dry-run", is_flag=True, help="Build the dream plan without mutating memories")
@click.option("--wait", is_flag=True, help="Wait for the dream run to complete")
@click.option(
    "--skip-consolidation",
    is_flag=True,
    help="Skip consolidation planning and leave candidates for review",
)
@click.option("--memory-type", "memory_type", help="Limit the scan to a memory type")
@click.pass_context
def memory_dream(
    ctx: click.Context,
    dry_run: bool,
    wait: bool,
    skip_consolidation: bool,
    memory_type: str | None,
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
    data = _request(ctx, "/memory/dream", method="POST", json_data=payload, timeout=900.0)
    run_id = data.get("run_id") or (data.get("run") or {}).get("id")
    if not data.get("success"):
        raise click.ClickException(str(data.get("error", "memory dream failed")))
    if run_id:
        click.echo(f"Dream run: {run_id}")
    status = data.get("status") or (data.get("run") or {}).get("status")
    if status:
        click.echo(f"Status: {status}")
    _print_summary((data.get("run") or {}).get("summary"))


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
    memory_module = _facade()
    client = memory_module._get_daemon_client(ctx)
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


def _print_summary(summary: Any) -> None:
    if not isinstance(summary, dict):
        return
    click.echo(f"Candidates reviewed: {summary.get('candidates_reviewed', 0)}")
    click.echo(f"Mutations: {summary.get('mutations', 0)}")
    click.echo(f"Snapshots: {summary.get('snapshots', 0)}")
    if summary.get("errors"):
        click.echo(f"Errors: {summary['errors']}")
