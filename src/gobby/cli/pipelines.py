"""
CLI commands for managing Gobby pipelines.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import click
import yaml

from gobby.workflows.loader import WorkflowLoader
from gobby.workflows.lobster_compat import LobsterImporter
from gobby.workflows.pipeline_state import ApprovalRequired

logger = logging.getLogger(__name__)


def get_workflow_loader() -> WorkflowLoader:
    """Get workflow loader instance."""
    return WorkflowLoader()


def get_project_path() -> Path | None:
    """Get current project path if in a gobby project."""
    cwd = Path.cwd()
    if (cwd / ".gobby").exists():
        return cwd
    return None


def _get_project_id() -> str:
    """Get project ID from current project if available."""
    project_path = get_project_path()
    if not project_path:
        return ""
    project_json = project_path / ".gobby" / "project.json"
    if not project_json.exists():
        return ""
    try:
        with open(project_json) as f:
            project_data = json.load(f)
            project_id = project_data.get("id", "")
            return str(project_id) if project_id else ""
    except Exception:
        return ""


def get_pipeline_executor() -> Any:
    """Get pipeline executor instance for local CLI fallback.

    Creates a lightweight executor with template rendering support.
    MCP tool steps require the daemon; use _try_daemon_run() first.
    """
    from gobby.storage.database import LocalDatabase
    from gobby.storage.pipelines import LocalPipelineExecutionManager
    from gobby.workflows.pipeline_executor import PipelineExecutor
    from gobby.workflows.templates import TemplateEngine

    db = LocalDatabase()

    project_id = _get_project_id()
    execution_manager = LocalPipelineExecutionManager(db, project_id)

    return PipelineExecutor(
        db=db,
        execution_manager=execution_manager,
        llm_service=None,  # Not needed for exec steps
        loader=get_workflow_loader(),
        template_engine=TemplateEngine(),
    )


def _try_daemon_run(name: str, inputs: dict[str, str], project_id: str) -> dict[str, Any] | None:
    """Try to run pipeline via daemon HTTP API. Returns None if unavailable."""
    try:
        from gobby.config.app import load_config
        from gobby.utils.daemon_client import DaemonClient

        config = load_config()
        client = DaemonClient(port=config.daemon_port)
        is_healthy, _ = client.check_health()
        if not is_healthy:
            return None
        response = client.call_http_api(
            "/api/pipelines/run",
            method="POST",
            json_data={"name": name, "inputs": inputs, "project_id": project_id},
            timeout=300.0,
        )
        if response.status_code in (200, 202):
            result: dict[str, Any] = response.json()
            return result
        return None
    except Exception as e:
        logger.debug(f"Daemon run failed for {name}: {e}", exc_info=True)
        return None


def parse_input(input_str: str) -> tuple[str, str]:
    """Parse a key=value input string."""
    if "=" not in input_str:
        raise click.BadParameter(f"Input must be in 'key=value' format: {input_str}")
    key, value = input_str.split("=", 1)
    return key.strip(), value.strip()


@click.group()
def pipelines() -> None:
    """Manage Gobby pipelines."""
    pass


@pipelines.command("list")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def list_pipelines(ctx: click.Context, json_format: bool) -> None:
    """List available pipeline definitions."""
    loader = get_workflow_loader()
    project_path = get_project_path()

    discovered = loader.discover_pipeline_workflows_sync(
        project_path=str(project_path) if project_path else None
    )

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
        click.echo(json.dumps({"pipelines": pipeline_list, "count": len(pipeline_list)}, indent=2))
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


@pipelines.command("show")
@click.argument("name")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def show_pipeline(ctx: click.Context, name: str, json_format: bool) -> None:
    """Show pipeline definition details."""
    loader = get_workflow_loader()
    project_path = get_project_path()

    pipeline = loader.load_pipeline_sync(name, project_path=project_path)
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
        click.echo(json.dumps(pipeline_dict, indent=2, default=str))
        return

    click.echo(f"Pipeline: {pipeline.name}")
    if pipeline.description:
        click.echo(f"Description: {pipeline.description}")

    if pipeline.inputs:
        click.echo("\nInputs:")
        for input_name, input_def in pipeline.inputs.items():
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


@pipelines.command("run")
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

    loader = get_workflow_loader()
    project_path = get_project_path()
    pipeline = loader.load_pipeline_sync(name, project_path=project_path)
    if not pipeline:
        click.echo(f"Pipeline '{name}' not found.", err=True)
        raise SystemExit(1)

    # Parse inputs
    input_dict: dict[str, str] = {}
    for input_str in inputs:
        try:
            key, value = parse_input(input_str)
            input_dict[key] = value
        except click.BadParameter as e:
            click.echo(str(e), err=True)
            raise SystemExit(1) from None

    project_id = _get_project_id()
    display_name = name or (pipeline.name if pipeline else None) or "pipeline"

    # Try daemon first (has MCP tool access and LLM service)
    daemon_result = _try_daemon_run(name, input_dict, project_id)
    if daemon_result is not None:
        status = daemon_result.get("status", "")
        if status == "waiting_approval":
            if json_format:
                click.echo(json.dumps(daemon_result, indent=2))
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
            click.echo(json.dumps(daemon_result, indent=2))
        else:
            click.echo(f"✓ Pipeline '{display_name}' completed")
            click.echo(f"  Execution ID: {daemon_result.get('execution_id', '')}")
            click.echo(f"  Status: {status}")
        return

    # Fall back to local executor (no MCP tool access)
    executor = get_pipeline_executor()

    try:
        # Run the pipeline
        execution = asyncio.run(
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
                    result["outputs"] = json.loads(execution.outputs_json)
                except json.JSONDecodeError:
                    result["outputs"] = execution.outputs_json
            click.echo(json.dumps(result, indent=2))
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
            click.echo(json.dumps(result, indent=2))
        else:
            click.echo(f"⏸ Pipeline '{display_name}' waiting for approval")
            click.echo(f"  Execution ID: {e.execution_id}")
            click.echo(f"  Step: {e.step_id}")
            click.echo(f"  Message: {e.message}")
            click.echo(f"\nTo approve: gobby pipelines approve {e.token}")
            click.echo(f"To reject:  gobby pipelines reject {e.token}")

    except Exception as e:
        click.echo(f"Pipeline execution failed: {e}", err=True)
        raise SystemExit(1) from None


def get_execution_manager() -> Any:
    """Get pipeline execution manager instance."""
    from gobby.storage.database import LocalDatabase
    from gobby.storage.pipelines import LocalPipelineExecutionManager

    db = LocalDatabase()

    project_id = _get_project_id()
    return LocalPipelineExecutionManager(db, project_id)


@pipelines.command("status")
@click.argument("execution_id")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def status_pipeline(ctx: click.Context, execution_id: str, json_format: bool) -> None:
    """Show status of a pipeline execution.

    Examples:

        gobby pipelines status pe-abc123

        gobby pipelines status pe-abc123 --json
    """
    execution_manager = get_execution_manager()

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
                exec_dict["inputs"] = json.loads(execution.inputs_json)
            except json.JSONDecodeError:
                exec_dict["inputs"] = execution.inputs_json
        if execution.outputs_json:
            try:
                exec_dict["outputs"] = json.loads(execution.outputs_json)
            except json.JSONDecodeError:
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
        click.echo(json.dumps(result, indent=2))
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


@pipelines.command("approve")
@click.argument("token")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def approve_pipeline(ctx: click.Context, token: str, json_format: bool) -> None:
    """Approve a pipeline execution waiting for approval.

    Examples:

        gobby pipelines approve approval-token-xyz

        gobby pipelines approve approval-token-xyz --json
    """
    executor = get_pipeline_executor()

    try:
        execution = asyncio.run(executor.approve(token, approved_by=None))

        if json_format:
            result = {
                "execution_id": execution.id,
                "pipeline_name": execution.pipeline_name,
                "status": execution.status.value,
            }
            click.echo(json.dumps(result, indent=2))
        else:
            click.echo("✓ Pipeline approved")
            click.echo(f"  Execution ID: {execution.id}")
            click.echo(f"  Status: {execution.status.value}")

    except ValueError as e:
        click.echo(f"Invalid token: {e}", err=True)
        raise SystemExit(1) from None
    except Exception as e:
        click.echo(f"Approval failed: {e}", err=True)
        raise SystemExit(1) from None


@pipelines.command("reject")
@click.argument("token")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def reject_pipeline(ctx: click.Context, token: str, json_format: bool) -> None:
    """Reject a pipeline execution waiting for approval.

    Examples:

        gobby pipelines reject approval-token-xyz

        gobby pipelines reject approval-token-xyz --json
    """
    executor = get_pipeline_executor()

    try:
        execution = asyncio.run(executor.reject(token, rejected_by=None))

        if json_format:
            result = {
                "execution_id": execution.id,
                "pipeline_name": execution.pipeline_name,
                "status": execution.status.value,
            }
            click.echo(json.dumps(result, indent=2))
        else:
            click.echo("✗ Pipeline rejected")
            click.echo(f"  Execution ID: {execution.id}")
            click.echo(f"  Status: {execution.status.value}")

    except ValueError as e:
        click.echo(f"Invalid token: {e}", err=True)
        raise SystemExit(1) from None
    except Exception as e:
        click.echo(f"Rejection failed: {e}", err=True)
        raise SystemExit(1) from None


@pipelines.command("history")
@click.argument("name")
@click.option("--limit", default=20, help="Maximum number of executions to show")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def history_pipeline(ctx: click.Context, name: str, limit: int, json_format: bool) -> None:
    """Show execution history for a pipeline.

    Examples:

        gobby pipelines history deploy

        gobby pipelines history deploy --limit 10

        gobby pipelines history deploy --json
    """
    execution_manager = get_execution_manager()

    # List executions filtered by pipeline name
    executions = execution_manager.list_executions(pipeline_name=name, limit=limit)

    if json_format:
        result = {
            "pipeline_name": name,
            "executions": [
                {
                    "id": ex.id,
                    "status": ex.status.value,
                    "created_at": ex.created_at,
                    "updated_at": ex.updated_at,
                }
                for ex in executions
            ],
            "count": len(executions),
        }
        click.echo(json.dumps(result, indent=2))
        return

    if not executions:
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


@pipelines.command("executions")
@click.option(
    "--status", default=None, help="Filter by status (pending, running, completed, failed, etc.)"
)
@click.option("--pipeline", "pipeline_name", default=None, help="Filter by pipeline name")
@click.option("--limit", default=20, help="Maximum number of executions to show")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def list_executions(
    ctx: click.Context, status: str | None, pipeline_name: str | None, limit: int, json_format: bool
) -> None:
    """List executions across all pipelines.

    Examples:

        gobby pipelines executions

        gobby pipelines executions --status running

        gobby pipelines executions --pipeline deploy --limit 10

        gobby pipelines executions --json
    """
    from gobby.workflows.pipeline_state import ExecutionStatus

    execution_manager = get_execution_manager()

    # Validate and convert status
    status_filter = None
    if status:
        try:
            status_filter = ExecutionStatus(status)
        except ValueError:
            valid = [s.value for s in ExecutionStatus]
            click.echo(f"Invalid status '{status}'. Valid: {', '.join(valid)}", err=True)
            raise SystemExit(1) from None

    executions = execution_manager.list_executions(
        status=status_filter, pipeline_name=pipeline_name, limit=limit
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
            "count": len(executions),
            "status_summary": execution_manager.count_by_status(),
        }
        click.echo(json.dumps(result, indent=2))
        return

    if not executions:
        click.echo("No executions found.")
        return

    # Show status summary header
    status_counts = execution_manager.count_by_status()
    summary_parts = [f"{s}: {c}" for s, c in sorted(status_counts.items())]
    if summary_parts:
        click.echo(f"Status: {', '.join(summary_parts)}\n")

    click.echo(f"Showing {len(executions)} execution(s):\n")
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


@pipelines.command("search")
@click.argument("query")
@click.option("--status", default=None, help="Filter by status")
@click.option("--no-errors", is_flag=True, help="Skip searching step error text")
@click.option("--limit", default=20, help="Maximum number of results")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.pass_context
def search_executions(
    ctx: click.Context,
    query: str,
    status: str | None,
    no_errors: bool,
    limit: int,
    json_format: bool,
) -> None:
    """Search pipeline executions by text.

    Matches pipeline names and step error messages.

    Examples:

        gobby pipelines search deploy

        gobby pipelines search "timeout error"

        gobby pipelines search deploy --status failed

        gobby pipelines search deploy --no-errors --json
    """
    from gobby.workflows.pipeline_state import ExecutionStatus

    execution_manager = get_execution_manager()

    # Validate status
    status_filter = None
    if status:
        try:
            status_filter = ExecutionStatus(status)
        except ValueError:
            valid = [s.value for s in ExecutionStatus]
            click.echo(f"Invalid status '{status}'. Valid: {', '.join(valid)}", err=True)
            raise SystemExit(1) from None

    executions = execution_manager.search_executions(
        query=query,
        search_errors=not no_errors,
        status=status_filter,
        limit=limit,
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
            "count": len(executions),
            "query": query,
        }
        click.echo(json.dumps(result, indent=2))
        return

    if not executions:
        click.echo(f"No executions found matching '{query}'.")
        return

    click.echo(f"Found {len(executions)} execution(s) matching '{query}':\n")
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


@pipelines.command("import")
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
    # Get project path
    project_path = get_project_path()

    if not output_path and not project_path:
        click.echo("Not in a Gobby project. Use --output to specify destination.", err=True)
        raise SystemExit(1)

    importer = LobsterImporter()
    try:
        pipeline = importer.import_file(path)
    except FileNotFoundError:
        click.echo(f"File not found: {path}", err=True)
        raise SystemExit(1) from None
    except Exception as e:
        click.echo(f"Failed to import: {e}", err=True)
        raise SystemExit(1) from None

    # Determine output path
    if output_path:
        dest_path = Path(output_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        # Save to project workflows directory
        assert project_path is not None  # guarded by check on line 570
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
    dest_path.write_text(yaml.dump(pipeline_dict, default_flow_style=False, sort_keys=False))

    click.echo(f"✓ Imported '{pipeline.name}'")
    click.echo(f"  Saved to: {dest_path}")
