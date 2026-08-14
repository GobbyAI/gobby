"""
Pipeline definition CLI commands.
"""

from __future__ import annotations

import sys
from importlib import import_module
from typing import Any

import click

from gobby.utils.json_helpers import json_dumps
from gobby.workflows.dry_run import evaluate_pipeline_definition
from gobby.workflows.pipeline_state import ApprovalRequired

_FACADE_MODULE = "gobby.cli.pipelines"


def _facade() -> Any:
    return sys.modules.get(_FACADE_MODULE) or import_module(_FACADE_MODULE)


@click.command("list")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def list_pipelines(ctx: click.Context, json_format: bool) -> None:
    """List available pipeline definitions."""
    facade = _facade()
    loader = facade.get_workflow_loader()
    project_id = facade._get_project_id()

    discovered = loader.discover_pipelines_sync(project_id or None)

    if json_format:
        pipeline_list = []
        for wf in discovered:
            pipeline_list.append(
                {
                    "name": wf.name,
                    "description": wf.definition.description,
                    "is_project": wf.is_project,
                    "path": str(wf.path),
                    "step_count": len(wf.definition.steps),
                }
            )
        click.echo(json_dumps({"pipelines": pipeline_list, "count": len(pipeline_list)}, indent=2))
        return

    if not discovered:
        click.echo("No pipelines found.")
        return

    click.echo(f"Found {len(discovered)} pipeline(s):\n")
    for wf in discovered:
        source_tag = "[project]" if wf.is_project else ""
        step_count = len(wf.definition.steps)
        click.echo(f"  {wf.name} ({step_count} steps) {source_tag}")
        if wf.definition.description:
            click.echo(f"    {wf.definition.description[:80]}")


@click.command("show")
@click.argument("name")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def show_pipeline(ctx: click.Context, name: str, json_format: bool) -> None:
    """Show pipeline definition details."""
    facade = _facade()
    loader = facade.get_workflow_loader()
    project_id = facade._get_project_id()

    pipeline = loader.load_pipeline_sync(name, project_id or None)
    if not pipeline:
        click.echo(f"Pipeline '{name}' not found.", err=True)
        raise SystemExit(1)
    if json_format:
        pipeline_dict = {
            "name": pipeline.name,
            "description": pipeline.description,
            "steps": [
                {
                    "id": step.id,
                    "exec": step.exec,
                    "prompt": step.prompt,
                    "invoke_pipeline": step.invoke_pipeline,
                    "condition": step.condition,
                }
                for step in pipeline.steps
            ],
            "inputs": pipeline.inputs,
            "outputs": pipeline.outputs,
        }
        click.echo(json_dumps(pipeline_dict, indent=2, default=str))
        return

    click.echo(f"Pipeline: {pipeline.name}")
    if pipeline.description:
        click.echo(f"Description: {pipeline.description}")

    if pipeline.inputs:
        click.echo("\nInputs:")
        for input_name, input_def in pipeline.inputs.items():
            if not isinstance(input_def, dict):
                # Shorthand form: the value is the input's default, e.g. `provider: "claude"`.
                default_tag = f" (default: {input_def})" if input_def is not None else ""
                click.echo(f"  - {input_name}{default_tag}")
                continue
            required = input_def.get("required", False)
            req_tag = " (required)" if required else ""
            click.echo(f"  - {input_name}{req_tag}")
            if input_def.get("description"):
                click.echo(f"      {input_def['description']}")

    click.echo(f"\nSteps ({len(pipeline.steps)}):")
    for step in pipeline.steps:
        step_type = "exec" if step.exec else "prompt" if step.prompt else "pipeline"
        click.echo(f"  - {step.id} ({step_type})")
        if step.exec:
            cmd_preview = step.exec[:60] + "..." if len(step.exec) > 60 else step.exec
            click.echo(f"      {cmd_preview}")
        elif step.prompt:
            prompt_preview = step.prompt[:60] + "..." if len(step.prompt) > 60 else step.prompt
            click.echo(f"      {prompt_preview}")
        elif step.invoke_pipeline:
            click.echo(f"      invoke: {step.invoke_pipeline}")
        if step.condition:
            click.echo(f"      condition: {step.condition}")

    if pipeline.outputs:
        click.echo("\nOutputs:")
        for output_name, output_expr in pipeline.outputs.items():
            click.echo(f"  - {output_name}: {output_expr}")


@click.command("check")
@click.argument("name")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def check_pipeline(name: str, json_format: bool) -> None:
    """Validate a pipeline definition without executing it."""
    import asyncio

    facade = _facade()
    loader = facade.get_workflow_loader()
    project_id = facade._get_project_id() or None
    evaluation = asyncio.run(evaluate_pipeline_definition(name, loader, project_id))
    result = evaluation.to_dict()
    if json_format:
        click.echo(json_dumps(result, indent=2, default=str))
    else:
        if evaluation.valid:
            click.secho("VALID", fg="green", bold=True)
        else:
            click.secho("INVALID", fg="red", bold=True)
        click.echo(f"  Pipeline: {result.get('workflow_name')}")
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
    if not evaluation.valid:
        raise SystemExit(1)


@click.command("run")
@click.argument("name", required=False)
@click.option(
    "-i",
    "--input",
    "inputs",
    multiple=True,
    help="Input values as key=value (can be repeated)",
)
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def run_pipeline(
    ctx: click.Context,
    name: str | None,
    inputs: tuple[str, ...],
    json_format: bool,
) -> None:
    """Run a pipeline by name.

    Examples:

        gobby pipelines run deploy

        gobby pipelines run deploy -i env=prod -i version=1.0
    """
    pipeline: Any = None  # Will be PipelineDefinition after loading
    if not name:
        click.echo("Pipeline name is required.", err=True)
        raise SystemExit(1)

    facade = _facade()
    loader = facade.get_workflow_loader()
    project_id = facade._get_project_id()
    pipeline = loader.load_pipeline_sync(name, project_id or None)
    if not pipeline:
        click.echo(f"Pipeline '{name}' not found.", err=True)
        raise SystemExit(1)
    if not pipeline.enabled:
        click.echo(f"Pipeline '{name}' is disabled.", err=True)
        raise SystemExit(1)

    # Parse inputs
    input_dict: dict[str, str] = {}
    for input_str in inputs:
        try:
            key, value = facade.parse_input(input_str)
            input_dict[key] = value
        except click.BadParameter as e:
            click.echo(str(e), err=True)
            raise SystemExit(1) from None

    display_name = name or (pipeline.name if pipeline else None) or "pipeline"

    # Try daemon first (has MCP tool access and LLM service)
    daemon_result = facade._try_daemon_run(name, input_dict, project_id)
    if daemon_result is not None:
        status = daemon_result.get("status", "")
        if status == "waiting_approval":
            if json_format:
                click.echo(json_dumps(daemon_result, indent=2))
            else:
                click.echo(f"⏸ Pipeline '{display_name}' waiting for approval")
                click.echo(f"  Execution ID: {daemon_result.get('execution_id', '')}")
                click.echo(f"  Step: {daemon_result.get('step_id', '')}")
                click.echo(f"  Message: {daemon_result.get('message', '')}")
                token = daemon_result.get("token", "")
                click.echo(f"\nTo approve: gobby pipelines approve {token}")
                click.echo(f"To reject:  gobby pipelines reject {token}")
            return
        if json_format:
            click.echo(json_dumps(daemon_result, indent=2))
        else:
            click.echo(f"✓ Pipeline '{display_name}' completed")
            click.echo(f"  Execution ID: {daemon_result.get('execution_id', '')}")
            click.echo(f"  Status: {status}")
        return

    # Fall back to local executor (no MCP tool access)
    executor = facade.get_pipeline_executor()

    try:
        # Run the pipeline
        execution = facade.asyncio.run(
            executor.execute(
                pipeline=pipeline,
                inputs=input_dict,
                project_id=project_id,
            )
        )

        # Output result
        if json_format:
            result: dict[str, Any] = {
                "execution_id": execution.id,
                "status": execution.status.value,
                "pipeline_name": execution.pipeline_name,
            }
            if execution.outputs_json:
                try:
                    result["outputs"] = facade.json.loads(execution.outputs_json)
                except facade.json.JSONDecodeError:
                    result["outputs"] = execution.outputs_json
            click.echo(json_dumps(result, indent=2))
        else:
            click.echo(f"✓ Pipeline '{display_name}' completed")
            click.echo(f"  Execution ID: {execution.id}")
            click.echo(f"  Status: {execution.status.value}")

    except ApprovalRequired as e:
        # Pipeline paused for approval
        if json_format:
            result = {
                "execution_id": e.execution_id,
                "status": "waiting_approval",
                "step_id": e.step_id,
                "token": e.token,
                "message": e.message,
            }
            click.echo(json_dumps(result, indent=2))
        else:
            click.echo(f"⏸ Pipeline '{display_name}' waiting for approval")
            click.echo(f"  Execution ID: {e.execution_id}")
            click.echo(f"  Step: {e.step_id}")
            click.echo(f"  Message: {e.message}")
            click.echo(f"\nTo approve: gobby pipelines approve {e.token}")
            click.echo(f"To reject:  gobby pipelines reject {e.token}")

    except (RuntimeError, ValueError, OSError) as e:
        click.echo(f"Pipeline execution failed: {e}", err=True)
        raise SystemExit(1) from None
