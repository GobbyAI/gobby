"""
Pipeline import CLI command.
"""

from __future__ import annotations

import sys
from typing import Any

import click


def _facade() -> Any:
    return sys.modules["gobby.cli.pipelines"]


@click.command("import")
@click.argument("path", type=click.Path(exists=True))
@click.option(
    "-o",
    "--output",
    "output_path",
    type=click.Path(),
    help="Custom output path for converted pipeline",
)
@click.pass_context
def import_pipeline(ctx: click.Context, path: str, output_path: str | None) -> None:
    """Import an external pipeline definition and convert to Gobby format.

    Reads a pipeline file, converts it to Gobby's native format,
    and saves it to .gobby/workflows/ in the current project.

    Examples:

        gobby pipelines import ci.lobster

        gobby pipelines import deploy.lobster -o custom/path.yaml
    """
    facade = _facade()

    # Get project path
    project_path = facade.get_project_path()

    if not output_path and not project_path:
        click.echo("Not in a Gobby project. Use --output to specify destination.", err=True)
        raise SystemExit(1)

    importer = facade.LobsterImporter()
    try:
        pipeline = importer.import_file(path)
    except FileNotFoundError:
        click.echo(f"File not found: {path}", err=True)
        raise SystemExit(1) from None
    except ValueError as e:
        click.echo(f"Failed to import: {e}", err=True)
        raise SystemExit(1) from None

    # Determine output path
    if output_path:
        dest_path = facade.Path(output_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        # Save to project workflows directory
        assert project_path is not None  # guarded by check above
        workflows_dir = project_path / ".gobby" / "workflows"
        workflows_dir.mkdir(parents=True, exist_ok=True)
        dest_path = workflows_dir / f"{pipeline.name}.yaml"

    # Convert pipeline to dict for YAML serialization
    pipeline_dict: dict[str, Any] = {
        "name": pipeline.name,
        "type": "pipeline",
        "version": pipeline.version,
    }
    if pipeline.description:
        pipeline_dict["description"] = pipeline.description
    if pipeline.inputs:
        pipeline_dict["inputs"] = pipeline.inputs
    if pipeline.outputs:
        pipeline_dict["outputs"] = pipeline.outputs

    # Convert steps
    steps = []
    for step in pipeline.steps:
        step_dict: dict[str, Any] = {"id": step.id}
        if step.exec:
            step_dict["exec"] = step.exec
        if step.prompt:
            step_dict["prompt"] = step.prompt
        if step.invoke_pipeline:
            step_dict["invoke_pipeline"] = step.invoke_pipeline
        if step.condition:
            step_dict["condition"] = step.condition
        if step.input:
            step_dict["input"] = step.input
        if step.approval:
            step_dict["approval"] = {
                "required": step.approval.required,
            }
            if step.approval.message:
                step_dict["approval"]["message"] = step.approval.message
            if step.approval.timeout_seconds:
                step_dict["approval"]["timeout_seconds"] = step.approval.timeout_seconds
        if step.tools:
            step_dict["tools"] = step.tools
        steps.append(step_dict)
    pipeline_dict["steps"] = steps

    # Write YAML file
    dest_path.write_text(facade.yaml.dump(pipeline_dict, default_flow_style=False, sort_keys=False))

    click.echo(f"✓ Imported '{pipeline.name}'")
    click.echo(f"  Saved to: {dest_path}")
