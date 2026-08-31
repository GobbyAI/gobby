"""CLI for the session-feedback review loop."""

from __future__ import annotations

from typing import Any, cast

import click
import httpx

from gobby.cli.utils_config import get_daemon_client

# The review runs inline in the daemon: one distill call (900s deadline)
# plus deterministic task filing, bounded by the cron action timeout.
_REVIEW_TIMEOUT_SECONDS = 1860.0
_STATUS_TIMEOUT_SECONDS = 30.0


@click.group("feedback")
def feedback() -> None:
    """Session-feedback review loop."""


@feedback.command("review")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Distill and render the digest without filing tasks or marking rows reviewed",
)
@click.pass_context
def feedback_review(ctx: click.Context, dry_run: bool) -> None:
    """Run one feedback review pass in the daemon and print its digest."""
    data = _request(
        ctx,
        "/feedback/review",
        method="POST",
        json_data={"dry_run": dry_run},
        timeout=_REVIEW_TIMEOUT_SECONDS,
    )
    if data.get("status") == "no_rows":
        click.echo("No unreviewed feedback rows.")
        return
    run_id = str(data.get("run_id") or "")
    click.echo(f"Review run: {run_id}")
    if dry_run:
        click.echo("Dry run — no tasks filed, no rows marked reviewed.")
    click.echo(f"Rows considered: {data.get('rows_considered', 0)}")
    click.echo(f"Tasks filed: {data.get('tasks_filed', 0)}")
    click.echo(f"Deduplicated: {data.get('deduplicated', 0)}")
    if run_id:
        run_data = _request(ctx, f"/feedback/review/{run_id}", method="GET")
        _print_digest(run_data.get("run"))


@feedback.command("digest")
@click.option("--run-id", "run_id", default=None, help="Run to print (default: the latest run)")
@click.pass_context
def feedback_digest(ctx: click.Context, run_id: str | None) -> None:
    """Print the digest of the latest (or given) review run."""
    endpoint = f"/feedback/review/{run_id}" if run_id else "/feedback/review/latest"
    data = _request(ctx, endpoint, method="GET")
    run = data.get("run")
    if not isinstance(run, dict):
        raise click.ClickException("daemon did not return a review run")
    click.echo(f"Review run: {run.get('id')}")
    click.echo(
        f"Status: {run.get('status')}  Dry run: {run.get('dry_run')}  "
        f"Rows: {run.get('rows_considered')}"
    )
    if run.get("error"):
        click.echo(f"Error: {run['error']}")
    _print_digest(run)


def _print_digest(run: Any) -> None:
    digest = run.get("digest_md") if isinstance(run, dict) else None
    click.echo()
    click.echo(str(digest) if digest else "(no digest recorded)")


def _request(
    ctx: click.Context,
    endpoint: str,
    *,
    method: str,
    json_data: dict[str, Any] | None = None,
    timeout: float = _STATUS_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    from gobby.cli.runtime import get_cli_runtime

    get_cli_runtime(ctx)
    client = get_daemon_client()
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
        raise click.ClickException(f"Daemon returned invalid JSON: {exc}") from exc

    if response.is_error:
        detail = payload.get("detail") if isinstance(payload, dict) else None
        if not detail and isinstance(payload, dict):
            detail = payload.get("error") or payload.get("message")
        raise click.ClickException(str(detail or f"Daemon returned HTTP {response.status_code}"))

    if not isinstance(payload, dict):
        raise click.ClickException("Daemon returned an unexpected response")
    return cast("dict[str, Any]", payload)
