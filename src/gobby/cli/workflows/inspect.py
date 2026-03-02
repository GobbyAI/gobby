"""Inspection commands for workflows."""

import json
import logging

import click
import yaml

from gobby.cli.workflows import common
from gobby.workflows.definitions import WorkflowDefinition

logger = logging.getLogger(__name__)


@click.command("list")
@click.option(
    "--all", "-a", "show_all", is_flag=True, help="Show all workflows including step-based"
)
@click.option("--global", "-g", "global_only", is_flag=True, help="Show only global workflows")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def list_workflows(
    ctx: click.Context, show_all: bool, global_only: bool, json_format: bool
) -> None:
    """List available workflows."""
    loader = common.get_workflow_loader()
    project_path = common.get_project_path() if not global_only else None

    # Build search directories
    search_dirs = list(loader.global_dirs)
    if project_path:
        project_dir = project_path / ".gobby" / "workflows"
        search_dirs.insert(0, project_dir)

    workflows = []
    seen_names = set()

    for search_dir in search_dirs:
        if not search_dir.exists():
            continue

        is_project = (
            search_dir == (project_path / ".gobby" / "workflows") if project_path else False
        )

        for yaml_path in search_dir.glob("*.yaml"):
            name = yaml_path.stem
            if name in seen_names:
                continue  # Project shadows global

            try:
                with open(yaml_path, encoding="utf-8") as f:
                    data = yaml.safe_load(f)

                if not data:
                    continue

                wf_enabled = data.get("enabled", True)
                description = data.get("description", "")

                # Filter by type unless --all
                if not show_all and not wf_enabled:
                    continue

                workflows.append(
                    {
                        "name": name,
                        "enabled": wf_enabled,
                        "description": description,
                        "source": "project" if is_project else "installed",
                        "path": str(yaml_path),
                    }
                )
                seen_names.add(name)

            except (OSError, yaml.YAMLError):
                logger.warning(
                    "Failed to load workflow",
                    extra={"path": str(yaml_path)},
                    exc_info=True,
                )

    if json_format:
        click.echo(json.dumps({"workflows": workflows, "count": len(workflows)}, indent=2))
        return

    if not workflows:
        click.echo("No workflows found.")
        click.echo(f"Search directories: {[str(d) for d in search_dirs]}")
        return

    click.echo(f"Found {len(workflows)} workflow(s):\n")
    for wf in workflows:
        source_tag = f"[{wf['source']}]" if wf["source"] == "project" else ""
        enabled_tag = "(enabled)" if wf["enabled"] else "(on-demand)"
        click.echo(f"  {wf['name']} {enabled_tag} {source_tag}")
        if wf["description"]:
            click.echo(f"    {wf['description'][:80]}")


@click.command("show")
@click.argument("name")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def show_workflow(ctx: click.Context, name: str, json_format: bool) -> None:
    """Show workflow details."""
    loader = common.get_workflow_loader()
    project_path = common.get_project_path()

    definition = loader.load_workflow_sync(name, project_path)
    if not definition:
        click.echo(f"Workflow '{name}' not found.", err=True)
        raise SystemExit(1)

    if json_format:
        click.echo(json.dumps(definition.model_dump(), indent=2, default=str))
        return

    click.echo(f"Workflow: {definition.name}")
    click.echo(f"Enabled: {getattr(definition, 'enabled', False)}")
    if definition.description:
        click.echo(f"Description: {definition.description}")
    if definition.version:
        click.echo(f"Version: {definition.version}")

    if definition.steps:
        click.echo(f"\nSteps ({len(definition.steps)}):")
        if isinstance(definition, WorkflowDefinition):
            for step in definition.steps:
                click.echo(f"  - {step.name}")
                if step.description:
                    click.echo(f"      {step.description}")
                if step.allowed_tools:
                    if step.allowed_tools == "all":
                        click.echo("      Allowed tools: all")
                    else:
                        tools = step.allowed_tools[:5]
                        more = (
                            f" (+{len(step.allowed_tools) - 5})"
                            if len(step.allowed_tools) > 5
                            else ""
                        )
                        click.echo(f"      Allowed tools: {', '.join(tools)}{more}")
                if step.blocked_tools:
                    click.echo(f"      Blocked tools: {', '.join(step.blocked_tools[:5])}")
        else:
            # PipelineDefinition
            for pstep in definition.steps:
                click.echo(f"  - {pstep.id}")
                if pstep.exec:
                    click.echo(f"      exec: {pstep.exec[:60]}...")
                elif pstep.prompt:
                    click.echo(f"      prompt: {pstep.prompt[:60]}...")


@click.command("status")
@click.option("--session", "-s", "session_id", help="Session ID (defaults to current)")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def workflow_status(ctx: click.Context, session_id: str | None, json_format: bool) -> None:
    """Show current workflow state for a session."""
    session_var_manager = common.get_session_var_manager()

    session_id = common.resolve_session_id(session_id)

    variables = session_var_manager.get_variables(session_id)

    if not variables:
        if json_format:
            click.echo(json.dumps({"session_id": session_id, "has_variables": False}))
        else:
            click.echo(f"No variables set for session: {common.truncate_id(session_id)}")
        return

    if json_format:
        click.echo(
            json.dumps(
                {
                    "session_id": session_id,
                    "has_variables": True,
                    "variables": variables,
                },
                indent=2,
            )
        )
        return

    click.echo(f"Session: {common.truncate_id(session_id)}")
    click.echo(f"Variables ({len(variables)}):")
    for var_name, var_value in sorted(variables.items()):
        value_display = repr(var_value) if isinstance(var_value, str) else str(var_value)
        click.echo(f"  {var_name} = {value_display}")
