"""
Pipeline run CLI commands.
"""

from __future__ import annotations

import sys
from typing import Any

import click

from gobby.utils.json_helpers import json_dumps


def _facade() -> Any:
    return sys.modules["gobby.cli.pipelines"]


@click.group("runs")
def pipeline_runs() -> None:
    """Manage pipeline run instances."""


@pipeline_runs.command("show")
@click.argument("execution_id")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def show_pipeline_run(ctx: click.Context, execution_id: str, json_format: bool) -> None:
    """Show status of a pipeline execution.

    Examples:

        gobby pipelines runs show pe-abc123

        gobby pipelines runs show pe-abc123 --json
    """
    facade = _facade()
    execution_manager = facade.get_execution_manager()

    # Fetch execution
    execution = execution_manager.get_execution(execution_id)
    if not execution:
        click.echo(f"Execution '{execution_id}' not found.", err=True)
        raise SystemExit(1)

    # Fetch step executions
    steps = execution_manager.get_steps_for_execution(execution_id)

    if json_format:
        exec_dict: dict[str, Any] = {
            "id": execution.id,
            "pipeline_name": execution.pipeline_name,
            "status": execution.status.value,
            "created_at": execution.created_at,
            "updated_at": execution.updated_at,
        }
        if execution.inputs_json:
            try:
                exec_dict["inputs"] = facade.json.loads(execution.inputs_json)
            except facade.json.JSONDecodeError:
                exec_dict["inputs"] = execution.inputs_json
        if execution.outputs_json:
            try:
                exec_dict["outputs"] = facade.json.loads(execution.outputs_json)
            except facade.json.JSONDecodeError:
                exec_dict["outputs"] = execution.outputs_json
        result: dict[str, Any] = {
            "execution": exec_dict,
            "steps": [
                {
                    "id": step.id,
                    "step_id": step.step_id,
                    "status": step.status.value,
                }
                for step in steps
            ],
        }
        click.echo(json_dumps(result, indent=2))
        return

    # Human-readable output
    click.echo(f"Execution: {execution.id}")
    click.echo(f"Pipeline: {execution.pipeline_name}")
    click.echo(f"Status: {execution.status.value}")
    click.echo(f"Created: {execution.created_at}")
    click.echo(f"Updated: {execution.updated_at}")

    if steps:
        click.echo(f"\nSteps ({len(steps)}):")
        for step in steps:
            status_icon = (
                "✓"
                if step.status.value == "completed"
                else "→"
                if step.status.value == "running"
                else "○"
            )
            click.echo(f"  {status_icon} {step.step_id} ({step.status.value})")


@click.command("approve")
@click.argument("token")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def approve_pipeline(ctx: click.Context, token: str, json_format: bool) -> None:
    """Approve a pipeline execution waiting for approval.

    Examples:

        gobby pipelines approve approval-token-xyz

        gobby pipelines approve approval-token-xyz --json
    """
    facade = _facade()
    try:
        daemon_result = facade._try_daemon_approval("approve", token)
        if daemon_result is not None:
            facade._echo_approval_result("approve", daemon_result, json_format)
            return

        executor = facade.get_pipeline_executor()
        execution = facade.asyncio.run(executor.approve(token, approved_by=None))
        facade._echo_approval_result(
            "approve", facade._pipeline_result_dict(execution), json_format
        )

    except ValueError as e:
        click.echo(f"Invalid token: {e}", err=True)
        raise SystemExit(1) from None
    except (RuntimeError, OSError) as e:
        click.echo(f"Approval failed: {e}", err=True)
        raise SystemExit(1) from None


@click.command("reject")
@click.argument("token")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def reject_pipeline(ctx: click.Context, token: str, json_format: bool) -> None:
    """Reject a pipeline execution waiting for approval.

    Examples:

        gobby pipelines reject approval-token-xyz

        gobby pipelines reject approval-token-xyz --json
    """
    facade = _facade()
    try:
        daemon_result = facade._try_daemon_approval("reject", token)
        if daemon_result is not None:
            facade._echo_approval_result("reject", daemon_result, json_format)
            return

        executor = facade.get_pipeline_executor()
        execution = facade.asyncio.run(executor.reject(token, rejected_by=None))
        facade._echo_approval_result("reject", facade._pipeline_result_dict(execution), json_format)

    except ValueError as e:
        click.echo(f"Invalid token: {e}", err=True)
        raise SystemExit(1) from None
    except (RuntimeError, OSError) as e:
        click.echo(f"Rejection failed: {e}", err=True)
        raise SystemExit(1) from None


@click.command("history")
@click.argument("name")
@click.option("--limit", default=20, help="Maximum number of executions to show")
@click.option("--offset", default=0, help="Number of leading rows to skip")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def history_pipeline(
    ctx: click.Context, name: str, limit: int, offset: int, json_format: bool
) -> None:
    """Show execution history for a pipeline.

    Examples:

        gobby pipelines history deploy

        gobby pipelines history deploy --limit 10

        gobby pipelines history deploy --limit 10 --offset 10

        gobby pipelines history deploy --json
    """
    facade = _facade()
    execution_manager = facade.get_execution_manager()

    try:
        executions = execution_manager.list_executions(
            pipeline_name=name, limit=limit, offset=offset
        )
    except ValueError as e:
        click.echo(f"Invalid pagination: {e}", err=True)
        raise SystemExit(1) from None
    raw_total = execution_manager.count_executions(pipeline_name=name)
    total = raw_total if isinstance(raw_total, int) else len(executions)

    if json_format:
        result = {
            "pipeline_name": name,
            "count": len(executions),
            "executions": [
                {
                    "id": ex.id,
                    "status": ex.status.value,
                    "created_at": ex.created_at,
                    "updated_at": ex.updated_at,
                }
                for ex in executions
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
        click.echo(json_dumps(result, indent=2))
        return

    if not executions:
        if offset > 0 and total > 0:
            click.echo(f"No executions on this page (offset={offset}, total={total}) for '{name}'.")
        else:
            click.echo(f"No executions found for pipeline '{name}'.")
        return

    click.echo(f"Execution history for '{name}' ({len(executions)} executions):\n")
    for ex in executions:
        status_icon = (
            "✓"
            if ex.status.value == "completed"
            else "✗"
            if ex.status.value == "failed"
            else "→"
            if ex.status.value == "running"
            else "○"
        )
        click.echo(f"  {status_icon} {ex.id} ({ex.status.value}) - {ex.created_at}")
    click.echo(f"\nShowing {offset + 1}–{offset + len(executions)} of {total}.")


@pipeline_runs.command("list")
@click.option(
    "--status", default=None, help="Filter by status (pending, running, completed, failed, etc.)"
)
@click.option("--name", "pipeline_name", default=None, help="Filter by pipeline definition name")
@click.option("--limit", default=20, help="Maximum number of executions per page")
@click.option("--offset", default=0, help="Number of leading rows to skip")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def list_pipeline_runs(
    ctx: click.Context,
    status: str | None,
    pipeline_name: str | None,
    limit: int,
    offset: int,
    json_format: bool,
) -> None:
    """List executions across all pipelines.

    Examples:

        gobby pipelines runs list

        gobby pipelines runs list --status running

        gobby pipelines runs list --name deploy --limit 10

        gobby pipelines runs list --limit 50 --offset 100

        gobby pipelines runs list --json
    """
    from gobby.workflows.pipeline_state import ExecutionStatus

    facade = _facade()
    execution_manager = facade.get_execution_manager()

    # Validate and convert status
    status_filter = None
    if status:
        try:
            status_filter = ExecutionStatus(status)
        except ValueError:
            valid = [s.value for s in ExecutionStatus]
            click.echo(f"Invalid status '{status}'. Valid: {', '.join(valid)}", err=True)
            raise SystemExit(1) from None

    try:
        executions = execution_manager.list_executions(
            status=status_filter,
            pipeline_name=pipeline_name,
            limit=limit,
            offset=offset,
        )
    except ValueError as e:
        click.echo(f"Invalid pagination: {e}", err=True)
        raise SystemExit(1) from None
    total = execution_manager.count_executions(status=status_filter, pipeline_name=pipeline_name)
    status_summary = execution_manager.status_summary_for_executions(pipeline_name=pipeline_name)

    if json_format:
        result: dict[str, Any] = {
            "executions": [
                {
                    "id": ex.id,
                    "pipeline_name": ex.pipeline_name,
                    "status": ex.status.value,
                    "created_at": ex.created_at,
                    "updated_at": ex.updated_at,
                }
                for ex in executions
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
            "status_summary": status_summary,
        }
        click.echo(json_dumps(result, indent=2))
        return

    if not executions:
        if offset > 0 and total > 0:
            click.echo(f"No executions on this page (offset={offset}, total={total}).")
        else:
            click.echo("No executions found.")
        return

    summary_parts = [f"{s}: {c}" for s, c in sorted(status_summary.items())]
    if summary_parts:
        click.echo(f"Status: {', '.join(summary_parts)}\n")

    for ex in executions:
        status_icon = (
            "✓"
            if ex.status.value == "completed"
            else "✗"
            if ex.status.value == "failed"
            else "→"
            if ex.status.value == "running"
            else "⏸"
            if ex.status.value == "waiting_approval"
            else "○"
        )
        click.echo(
            f"  {status_icon} {ex.id} {ex.pipeline_name} ({ex.status.value}) - {ex.created_at}"
        )
    click.echo(f"\nShowing {offset + 1}–{offset + len(executions)} of {total}.")


@click.command("search")
@click.argument("query")
@click.option("--status", default=None, help="Filter by status")
@click.option("--no-errors", is_flag=True, help="Skip searching step error text")
@click.option("--limit", default=20, help="Maximum number of results per page")
@click.option("--offset", default=0, help="Number of leading rows to skip")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def search_executions(
    ctx: click.Context,
    query: str,
    status: str | None,
    no_errors: bool,
    limit: int,
    offset: int,
    json_format: bool,
) -> None:
    """Search pipeline executions by text.

    Matches pipeline names and step error messages.

    Examples:

        gobby pipelines search deploy

        gobby pipelines search "timeout error"

        gobby pipelines search deploy --status failed

        gobby pipelines search deploy --limit 10 --offset 10

        gobby pipelines search deploy --no-errors --json
    """
    from gobby.workflows.pipeline_state import ExecutionStatus

    facade = _facade()
    execution_manager = facade.get_execution_manager()

    # Validate status
    status_filter = None
    if status:
        try:
            status_filter = ExecutionStatus(status)
        except ValueError:
            valid = [s.value for s in ExecutionStatus]
            click.echo(f"Invalid status '{status}'. Valid: {', '.join(valid)}", err=True)
            raise SystemExit(1) from None

    try:
        executions = execution_manager.search_executions(
            query=query,
            search_errors=not no_errors,
            status=status_filter,
            limit=limit,
            offset=offset,
        )
    except ValueError as e:
        click.echo(f"Invalid pagination: {e}", err=True)
        raise SystemExit(1) from None
    total = execution_manager.count_search_executions(
        query=query,
        search_errors=not no_errors,
        status=status_filter,
    )

    if json_format:
        result: dict[str, Any] = {
            "executions": [
                {
                    "id": ex.id,
                    "pipeline_name": ex.pipeline_name,
                    "status": ex.status.value,
                    "created_at": ex.created_at,
                    "updated_at": ex.updated_at,
                }
                for ex in executions
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
            "query": query,
        }
        click.echo(json_dumps(result, indent=2))
        return

    if not executions:
        if offset > 0 and total > 0:
            click.echo(f"No matches on this page (offset={offset}, total={total}) for '{query}'.")
        else:
            click.echo(f"No executions found matching '{query}'.")
        return

    click.echo(f"Matches for '{query}':\n")
    for ex in executions:
        status_icon = (
            "✓"
            if ex.status.value == "completed"
            else "✗"
            if ex.status.value == "failed"
            else "→"
            if ex.status.value == "running"
            else "⏸"
            if ex.status.value == "waiting_approval"
            else "○"
        )
        click.echo(
            f"  {status_icon} {ex.id} {ex.pipeline_name} ({ex.status.value}) - {ex.created_at}"
        )
    click.echo(f"\nShowing {offset + 1}–{offset + len(executions)} of {total}.")
