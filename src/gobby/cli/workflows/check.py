"""Validation and debugging commands for workflows."""

import json

import click
import httpx

from gobby.cli.workflows import common


@click.command("check")
@click.argument("name")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def check_workflow(ctx: click.Context, name: str, json_format: bool) -> None:
    """Validate a workflow definition — structural and semantic checks.

    Checks for unreachable steps, dead-end steps, undefined transition targets,
    undefined variable references, MCP tool conflicts, and more.

    \b
    Examples:
        gobby workflows check meeseeks-box
        gobby workflows check worker-inline --json
    """
    from gobby.utils.daemon_client import DaemonClient

    client = DaemonClient()
    try:
        result = client.call_mcp_tool(
            server_name="gobby-workflows",
            tool_name="evaluate_workflow",
            arguments={"name": name},
            timeout=15.0,
        )
    except httpx.ConnectError:
        click.echo("Error: Cannot connect to Gobby daemon. Is it running?", err=True)
        click.echo("Start with: gobby start", err=True)
        raise SystemExit(1) from None
    except httpx.HTTPStatusError as e:
        click.echo(f"Error: HTTP {e.response.status_code}: {e.response.text}", err=True)
        raise SystemExit(1) from None
    except httpx.TimeoutException:
        click.echo("Error: Request timed out. The daemon may be overloaded.", err=True)
        raise SystemExit(1) from None
    except httpx.RequestError as e:
        click.echo(f"Request error: {e}", err=True)
        raise SystemExit(1) from None
    except (click.ClickException, ValueError, RuntimeError) as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1) from None

    if json_format:
        click.echo(json.dumps(result, indent=2, default=str))
        return

    # Formatted output
    valid = result.get("valid", False)
    items = result.get("items", [])

    if valid:
        click.secho("VALID", fg="green", bold=True)
    else:
        click.secho("INVALID", fg="red", bold=True)

    click.echo(f"  Workflow: {result.get('workflow_name')}")
    click.echo(f"  Type: {result.get('workflow_type', 'unknown')}")
    variables = result.get("variables_declared")
    if variables:
        click.echo(f"  Variables: {', '.join(variables)}")
    click.echo()

    # Items
    for item in items:
        level = item.get("level", "info")
        code = item.get("code", "")
        message = item.get("message", "")

        if level == "error":
            click.secho(f"  ERROR {code}: {message}", fg="red")
        elif level == "warning":
            click.secho(f"  WARN  {code}: {message}", fg="yellow")
        else:
            click.echo(f"  info  {code}: {message}")

    # Step trace
    step_trace = result.get("step_trace", [])
    if step_trace:
        click.echo()
        click.secho("  Steps:", bold=True)
        for step in step_trace:
            click.echo(f"    {step['name']}", nl=False)
            if step.get("description"):
                click.echo(f" — {step['description']}", nl=False)
            click.echo()
            if step.get("on_enter_actions"):
                for action in step["on_enter_actions"]:
                    click.echo(f"      on_enter: {action}")
            if step.get("transitions"):
                for t in step["transitions"]:
                    click.echo(f"      -> {t['to']} when: {t['when']}")

    # Lifecycle path
    lifecycle_path = result.get("lifecycle_path", [])
    if lifecycle_path:
        click.echo(f"\n  Path: {' -> '.join(lifecycle_path)}")


@click.command("audit")
@click.option("--session", "-s", "session_id", help="Session ID (defaults to current)")
@click.option(
    "--type",
    "-t",
    "event_type",
    help="Filter by event type (tool_call, rule_eval, transition, approval)",
)
@click.option("--result", "-r", help="Filter by result (allow, block, transition)")
@click.option("--limit", "-n", default=50, help="Maximum entries to show (default: 50)")
@click.option("--verbose", "-v", is_flag=True, help="Show full context for each entry")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def audit_workflow(
    ctx: click.Context,
    session_id: str | None,
    event_type: str | None,
    result: str | None,
    limit: int,
    verbose: bool,
    json_format: bool,
) -> None:
    """View workflow audit log (explainability/debugging)."""
    from gobby.storage.hub.runtime import open_runtime_hub_database
    from gobby.storage.workflow_audit import WorkflowAuditManager

    audit_manager = WorkflowAuditManager(open_runtime_hub_database(apply_migrations=False))

    session_id = common.resolve_session_id(session_id)

    entries = audit_manager.get_entries(
        session_id=session_id,
        event_type=event_type,
        result=result,
        limit=limit,
    )

    if not entries:
        click.echo(f"No audit entries found for session {common.truncate_id(session_id)}")
        return

    if json_format:
        output = []
        for entry in entries:
            output.append(
                {
                    "id": entry.id,
                    "timestamp": entry.timestamp.isoformat(),
                    "step": entry.step,
                    "event_type": entry.event_type,
                    "tool_name": entry.tool_name,
                    "rule_id": entry.rule_id,
                    "condition": entry.condition,
                    "result": entry.result,
                    "reason": entry.reason,
                    "context": entry.context,
                }
            )
        click.echo(json.dumps(output, indent=2))
        return

    # Human-readable output
    click.echo(f"Audit log for session {common.truncate_id(session_id)} ({len(entries)} entries)\n")

    for entry in entries:
        # Format: [timestamp] RESULT event_type
        timestamp_str = entry.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        result_str = entry.result or "unknown"
        result_color = {
            "allow": "green",
            "block": "red",
            "transition": "yellow",
            "approved": "green",
            "rejected": "red",
            "denied": "red",
        }.get(result_str, "white")

        click.secho(f"[{timestamp_str}] ", nl=False, dim=True)
        click.secho(f"{result_str.upper():<10}", fg=result_color, bold=True, nl=False)
        click.secho(f" {entry.event_type:<12}", fg="cyan", nl=False)

        details = []
        if entry.step:
            details.append(f"step={entry.step}")
        if entry.tool_name:
            details.append(f"tool={entry.tool_name}")
        if entry.rule_id:
            details.append(f"rule={entry.rule_id}")
        if entry.condition:
            details.append(f"cond={entry.condition}")

        click.echo(f" {' '.join(details)}")

        if entry.reason:
            click.echo(f"  Reason: {entry.reason}")
        if entry.context and verbose:
            click.echo(f"  Context: {entry.context}")
