"""Agent step-instance and definition-check CLI commands."""

from __future__ import annotations

import asyncio
from typing import Any

import click

from gobby.cli.runtime import require_cli_database
from gobby.cli.utils import resolve_session_id
from gobby.utils.json_helpers import json_dumps
from gobby.utils.project_context import get_project_context
from gobby.workflows.agent_resolver import resolve_agent
from gobby.workflows.dry_run import (
    EvaluationItem,
    WorkflowEvaluation,
    evaluate_agent_definition,
)
from gobby.workflows.step_instances import AgentStepInstanceManager


def _echo_evaluation(result: dict[str, Any], json_format: bool) -> None:
    if json_format:
        click.echo(json_dumps(result, indent=2, default=str))
        return
    valid = result.get("valid", False)
    if valid:
        click.secho("VALID", fg="green", bold=True)
    else:
        click.secho("INVALID", fg="red", bold=True)
    click.echo(f"  Agent: {result.get('workflow_name')}")
    variables = result.get("variables_declared")
    if variables:
        click.echo(f"  Variables: {', '.join(variables)}")
    click.echo()
    for item in result.get("items", []):
        level = item.get("level", "info")
        code = item.get("code", "")
        message = item.get("message", "")
        if level == "error":
            click.secho(f"  ERROR {code}: {message}", fg="red")
        elif level == "warning":
            click.secho(f"  WARN  {code}: {message}", fg="yellow")
        else:
            click.echo(f"  info  {code}: {message}")
    step_trace = result.get("step_trace", [])
    if step_trace:
        click.echo()
        click.secho("  Steps:", bold=True)
        for step in step_trace:
            click.echo(f"    {step['name']}", nl=False)
            if step.get("description"):
                click.echo(f" — {step['description']}", nl=False)
            click.echo()


@click.command("steps")
@click.option("--session", "-s", "session_id", help="Session ID (defaults to current)")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def list_agent_steps(session_id: str | None, json_format: bool) -> None:
    """Show the agent-step instance for a session."""
    resolved = resolve_session_id(session_id)
    instance = AgentStepInstanceManager(require_cli_database()).get_for_session(resolved)
    if instance is None:
        if json_format:
            click.echo(json_dumps({"session_id": resolved, "has_instance": False}, indent=2))
        else:
            click.echo(f"No agent-step instance for session {resolved}")
        return

    steps = [step.name for step in instance.snapshot.steps]
    payload = {
        "session_id": resolved,
        "has_instance": True,
        "agent_name": instance.agent_name,
        "current_step": instance.current_step,
        "steps": steps,
        "exit_condition": instance.snapshot.exit_condition,
        "variables": instance.variables,
    }
    if json_format:
        click.echo(json_dumps(payload, indent=2, default=str))
        return
    click.echo(f"Session: {resolved}")
    click.echo(f"Agent: {instance.agent_name}")
    click.echo(f"Current step: {instance.current_step}")
    click.echo(f"Steps: {', '.join(steps)}")
    if instance.snapshot.exit_condition:
        click.echo(f"Exit: {instance.snapshot.exit_condition}")
    if instance.variables:
        click.echo("Variables:")
        for name, value in sorted(instance.variables.items()):
            click.echo(f"  {name} = {value!r}")


@click.command("check")
@click.argument("name")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def check_agent(name: str, json_format: bool) -> None:
    """Validate an agent definition without executing it."""
    db = require_cli_database()
    project_ctx = get_project_context()
    project_id = str(project_ctx["id"]) if project_ctx and project_ctx.get("id") else None
    agent = resolve_agent(name, db, project_id=project_id)
    if agent is None:
        missing = WorkflowEvaluation(valid=False, workflow_name=name)
        missing.items.append(
            EvaluationItem(
                layer="structure",
                level="error",
                code="AGENT_NOT_FOUND",
                message=f"Agent '{name}' not found",
            )
        )
        _echo_evaluation(missing.to_dict(), json_format)
        raise SystemExit(1)
    evaluation = asyncio.run(evaluate_agent_definition(agent))
    _echo_evaluation(evaluation.to_dict(), json_format)
    if not evaluation.valid:
        raise SystemExit(1)
