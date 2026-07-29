from __future__ import annotations

import json
import time
from typing import Any, cast

import click
import httpx

from gobby.cli.memory.common import _get_daemon_client
from gobby.memory.dream.storage_runs import RUN_TERMINAL_STATUSES
from gobby.storage.memories import MEMORY_TYPE_VALUES

# The trigger and status calls are plain HTTP round trips; the sweep itself
# runs asynchronously in the daemon, so neither request waits on it.
_TRIGGER_TIMEOUT_SECONDS = 30.0
_STATUS_TIMEOUT_SECONDS = 30.0
_POLL_INTERVAL_SECONDS = 2.0


@click.group("dream", invoke_without_command=True)
@click.option("--dry-run", is_flag=True, help="Build the dream plan without mutating memories")
@click.option(
    "--skip-consolidation",
    is_flag=True,
    help="Skip consolidation planning and leave candidates for review",
)
@click.option(
    "--memory-type",
    "memory_type",
    type=click.Choice(MEMORY_TYPE_VALUES),
    help="Limit the scan to a memory type",
)
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
    default=0.0,
    show_default=True,
    help="Client-side polling deadline in seconds; 0 waits until the run is terminal",
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
    """Start a memory dream sweep and watch it to a terminal status.

    The daemon runs the sweep asynchronously: the CLI prints the run ID
    immediately, polls the run every two seconds, and renders progress
    whenever the durable checkpoint changes. --timeout bounds only the
    client-side wait; stopping the wait (deadline or Ctrl-C) leaves the
    daemon run active and prints the status command to resume observation.
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
        timeout=_TRIGGER_TIMEOUT_SECONDS,
    )
    if not data.get("success"):
        raise click.ClickException(str(data.get("error", "memory dream failed")))
    run_id = str(data.get("run_id") or "")
    if not run_id:
        raise click.ClickException("daemon did not return a dream run ID")

    if data.get("coalesced"):
        click.echo(f"Coalesced onto active dream run: {run_id}")
        active = data.get("active")
        if isinstance(active, dict) and isinstance(active.get("checkpoint"), dict):
            _print_checkpoint(active["checkpoint"])
    else:
        click.echo(f"Started dream run: {run_id}")

    run = _poll_run(ctx, run_id, timeout)
    if run is None:
        # Client-side deadline expired; the daemon run stays active.
        return
    _render_terminal_run(ctx, run)


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
    checkpoint = run.get("checkpoint")
    if isinstance(checkpoint, dict):
        _print_checkpoint(checkpoint)
    _print_summary(run.get("summary"))
    _print_dry_run_actions(run.get("plan"))


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


def _poll_run(ctx: click.Context, run_id: str, timeout: float) -> dict[str, Any] | None:
    """Poll a run until terminal; None when the client-side deadline expires."""
    deadline = time.monotonic() + timeout if timeout > 0 else None
    last_checkpoint: dict[str, Any] | None = None
    try:
        while True:
            data = _request(
                ctx,
                f"/memory/dream/{run_id}",
                method="GET",
                timeout=_STATUS_TIMEOUT_SECONDS,
            )
            raw_run = data.get("run")
            run = raw_run if isinstance(raw_run, dict) else {}
            checkpoint = run.get("checkpoint")
            if isinstance(checkpoint, dict) and checkpoint != last_checkpoint:
                _print_checkpoint(checkpoint)
                last_checkpoint = checkpoint
            if str(run.get("status") or "") in RUN_TERMINAL_STATUSES:
                return run
            if deadline is not None and time.monotonic() >= deadline:
                click.echo(f"Dream run {run_id} is still running; the daemon keeps working.")
                _echo_resume_hint(run_id)
                return None
            time.sleep(_POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        click.echo()
        click.echo(f"Stopped watching; dream run {run_id} keeps running in the daemon.")
        _echo_resume_hint(run_id)
        raise click.Abort() from None


def _echo_resume_hint(run_id: str) -> None:
    click.echo(f"Resume observation with: gobby memory dream status {run_id}")


def _print_checkpoint(checkpoint: dict[str, Any]) -> None:
    parts = [
        f"[{checkpoint.get('phase', 'unknown')}]",
        f"scope={checkpoint.get('scope', 'unknown')}",
        f"pass={checkpoint.get('pass_number', 1)}",
        f"batch={checkpoint.get('batch_number', 0)}",
        f"completed={checkpoint.get('completed', 0)}",
    ]
    remaining = checkpoint.get("remaining")
    if remaining is not None:
        parts.append(f"remaining={remaining}")
    parts.append(f"mutations={checkpoint.get('mutations', 0)}")
    failure = checkpoint.get("last_dependency_failure")
    if failure:
        parts.append(f"retry={failure}")
    click.echo(" ".join(parts))


def _render_terminal_run(ctx: click.Context, run: dict[str, Any]) -> None:
    """Render a terminal run row; non-completed outcomes exit non-zero."""
    run_id = str(run.get("id") or "")
    status = str(run.get("status") or "unknown")
    raw_summary = run.get("summary")
    summary = raw_summary if isinstance(raw_summary, dict) else {}
    raw_plan = run.get("plan")
    plan = raw_plan if isinstance(raw_plan, dict) else {}
    raw_checkpoint = run.get("checkpoint")
    checkpoint = raw_checkpoint if isinstance(raw_checkpoint, dict) else {}

    if status == "completed":
        click.echo(f"Dream run {run_id} completed")
        if plan.get("aggregate") is True:
            _print_aggregate({**summary, "runs": plan.get("runs")})
        elif summary.get("skip_consolidation") is True:
            _print_inventory(plan, summary)
        else:
            _print_summary(summary)
            _print_dry_run_actions(plan)
        return

    click.echo(f"Dream run {run_id} {status}")
    stop_reason = checkpoint.get("stop_reason") or summary.get("stop_reason")
    if stop_reason:
        click.echo(f"Stop reason: {stop_reason}")
    if run.get("error"):
        click.echo(f"Error: {run['error']}")
    click.echo(
        f"Completed: {checkpoint.get('completed', 0)} candidate(s), "
        f"{checkpoint.get('mutations', 0)} mutation(s)"
    )
    remaining = checkpoint.get("remaining")
    if remaining is not None:
        click.echo(f"Remaining: {remaining} candidate(s)")
    if checkpoint.get("backlog"):
        click.echo(f"Backlog: {json.dumps(checkpoint['backlog'], sort_keys=True)}")
    ctx.exit(1)


def _print_inventory(plan: dict[str, Any], summary: dict[str, Any]) -> None:
    count = summary.get("candidates_eligible", plan.get("candidate_count", 0))
    click.echo(
        f"Inventory-only run: {count} candidate(s) eligible; "
        "consolidation skipped, candidates remain due."
    )
    ids = plan.get("candidate_ids")
    if isinstance(ids, list) and ids:
        suffix = " (truncated)" if plan.get("candidate_ids_truncated") else ""
        click.echo(f"Candidate IDs{suffix}: {', '.join(str(i) for i in ids)}")


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


def _print_dry_run_actions(plan: Any) -> None:
    if not isinstance(plan, dict) or plan.get("dry_run") is not True:
        return
    actions = plan.get("actions")
    if not isinstance(actions, list):
        return
    click.echo("Proposed actions:")
    for action in actions:
        if isinstance(action, dict):
            click.echo(json.dumps(action, sort_keys=True))
