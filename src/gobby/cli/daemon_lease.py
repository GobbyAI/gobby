"""CLI commands for single-active-daemon lease control."""

from __future__ import annotations

import json
from typing import Any

import click
import httpx

from gobby.cli.utils_config import get_daemon_client


def _request(endpoint: str, *, method: str = "POST") -> dict[str, Any]:
    client = get_daemon_client()
    response: httpx.Response
    if method == "POST":
        response = client.call_http_api(endpoint)
    else:
        response = client.call_http_api(endpoint, method=method)
    try:
        payload = response.json()
    except ValueError as exc:
        raise click.ClickException(
            f"Daemon returned HTTP {response.status_code} with invalid JSON"
        ) from exc
    if not response.is_success:
        detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
        raise click.ClickException(json.dumps(detail, sort_keys=True))
    if not isinstance(payload, dict):
        raise click.ClickException("Daemon returned a non-object lease response")
    return payload


@click.group("lease")
def lease() -> None:
    """Inspect and control single-active-daemon ownership."""


@lease.command("status")
def status() -> None:
    """Show the current lease holder."""
    click.echo(json.dumps(_request("/api/admin/lease/status", method="GET"), indent=2))


@lease.command("promote")
def promote() -> None:
    """Explicitly promote this standby when the lease is free."""
    _request("/api/admin/lease/promote")
    click.echo("Promotion accepted")


@lease.command("handoff")
def handoff() -> None:
    """Quiesce the active daemon and release ownership."""
    _request("/api/admin/lease/handoff")
    click.echo("Handoff accepted")


@lease.command("recover")
@click.option(
    "--stale-after",
    type=click.FloatRange(min=0.0),
    default=30.0,
    show_default=True,
    help="Minimum holder heartbeat age in seconds.",
)
@click.option("--yes", is_flag=True, help="Skip the destructive recovery confirmation.")
def recover(stale_after: float, yes: bool) -> None:
    """Terminate a verified stale owner and promote this standby."""
    if not yes:
        click.confirm(
            "Terminate the verified stale PostgreSQL lease backend and promote this daemon?",
            abort=True,
        )
    _request(f"/api/admin/lease/recover?stale_after_seconds={stale_after:g}")
    click.echo("Stale owner recovered; promotion accepted")


__all__ = ["lease"]
