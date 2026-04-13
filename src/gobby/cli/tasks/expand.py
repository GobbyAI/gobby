"""Task expansion CLI commands."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import click

from gobby.cli.tasks._utils import get_task_manager, resolve_task_id
from gobby.config.app import load_config
from gobby.llm import LLMService
from gobby.storage.expansion_runs import LocalExpansionRunManager
from gobby.storage.sessions import LocalSessionManager
from gobby.tasks.expansion_service import ExpansionService
from gobby.utils.project_context import get_project_context
from gobby.utils.session_context import get_current_session_id


def _build_expansion_service() -> ExpansionService:
    task_manager = get_task_manager()
    config = load_config()
    llm_service = LLMService(config)
    return ExpansionService(task_manager=task_manager, llm_service=llm_service, config=config)


def _resolve_cli_session_id(raw_session_id: str | None) -> str | None:
    """Resolve a CLI-provided session reference when possible.

    When ``raw_session_id`` is explicitly supplied by the user, a resolution
    failure surfaces as a :class:`click.ClickException`. When falling back to
    the ambient ``get_current_session_id()``, failures stay silent so the
    CLI keeps working outside of a hook-aware shell.
    """
    session_ref = raw_session_id or get_current_session_id()
    if not session_ref:
        return None
    task_manager = get_task_manager()
    session_manager = LocalSessionManager(task_manager.db)
    project_ctx = get_project_context(cwd=Path.cwd())
    project_id = project_ctx.get("id") if project_ctx else None
    try:
        return session_manager.resolve_session_reference(session_ref, project_id)
    except Exception as exc:
        if raw_session_id is not None:
            raise click.ClickException(f"Cannot resolve session '{raw_session_id}': {exc}") from exc
        return None


@click.group("expand")
def expand_cmd() -> None:
    """Compile and apply task expansion runs."""
    pass


@expand_cmd.command("validate-plan")
@click.argument("plan_file")
def validate_plan_cmd(plan_file: str) -> None:
    """Validate a plan file and list detected phases."""
    service = _build_expansion_service()
    plan_path = Path(plan_file)
    if not plan_path.is_absolute():
        plan_path = Path.cwd() / plan_path
    result = service.validate_plan_file(plan_path)
    if not result["valid"]:
        for error in result["errors"]:
            click.echo(f"Error: {error}", err=True)
        raise click.ClickException("Plan validation failed")
    click.echo(f"Plan: {result['path']}")
    click.echo(f"Phases: {result['phase_count']}")
    for phase_num, title in result["phases"].items():
        click.echo(f"  {phase_num}: {title}")


@expand_cmd.command("compile")
@click.argument("task_ref")
@click.option(
    "--plan-file", default=None, help="Optional plan file path relative to the project root."
)
@click.option("--provider", default=None, help="Optional provider override.")
@click.option("--model", default=None, help="Optional model override.")
@click.option("--json-output", "json_output", is_flag=True, help="Emit JSON.")
def compile_cmd(
    task_ref: str,
    plan_file: str | None,
    provider: str | None,
    model: str | None,
    json_output: bool,
) -> None:
    """Compile a task into a stored expansion run."""
    service = _build_expansion_service()
    task_manager = service.task_manager
    task = resolve_task_id(task_manager, task_ref)
    if task is None:
        raise click.ClickException(f"Task not found: {task_ref}")

    run_manager = LocalExpansionRunManager(task_manager.db)
    run = run_manager.create(
        parent_task_id=task.id,
        project_id=task.project_id,
        triggering_session_id=_resolve_cli_session_id(None),
        input_source="plan" if plan_file else "task",
        plan_file=plan_file,
        provider=provider,
        model=model,
        options={"auto_apply": False},
    )
    run = asyncio.run(service.compile_run(run.id))
    if json_output:
        click.echo(json.dumps(run.to_dict()))
        return
    summary = run.compiled_spec or {}
    click.echo(f"Run: {run.id}")
    click.echo(f"Status: {run.status}")
    click.echo(f"Phases: {len(summary.get('phases') or [])}")
    click.echo(f"Tasks: {len(summary.get('tasks') or [])}")


@expand_cmd.command("apply")
@click.argument("run_id")
@click.option("--session-id", default=None, help="Optional session ref to attribute created tasks.")
@click.option("--json-output", "json_output", is_flag=True, help="Emit JSON.")
def apply_cmd(run_id: str, session_id: str | None, json_output: bool) -> None:
    """Apply a compiled expansion run."""
    service = _build_expansion_service()
    resolved_session_id = _resolve_cli_session_id(session_id)
    run = service.apply_run(run_id, session_id=resolved_session_id)
    if json_output:
        click.echo(json.dumps(run.to_dict()))
        return
    click.echo(f"Run: {run.id}")
    click.echo(f"Status: {run.status}")
    click.echo(f"Created tasks: {len(run.created_task_ids or [])}")


@expand_cmd.command("status")
@click.argument("run_id")
@click.option("--json-output", "json_output", is_flag=True, help="Emit JSON.")
def status_cmd(run_id: str, json_output: bool) -> None:
    """Show expansion run status."""
    service = _build_expansion_service()
    run_manager = LocalExpansionRunManager(service.task_manager.db)
    run = run_manager.get(run_id)
    if run is None:
        raise click.ClickException(f"Expansion run not found: {run_id}")
    if json_output:
        click.echo(json.dumps(run.to_dict()))
        return
    click.echo(f"Run: {run.id}")
    click.echo(f"Status: {run.status}")
    if run.error:
        click.echo(f"Error: {run.error}")
    click.echo(f"Compiled tasks: {len((run.compiled_spec or {}).get('tasks') or [])}")
    click.echo(f"Created tasks: {len(run.created_task_ids or [])}")


@expand_cmd.command("resume")
@click.argument("run_id")
@click.option("--session-id", default=None, help="Optional session ref to attribute created tasks.")
@click.option("--json-output", "json_output", is_flag=True, help="Emit JSON.")
def resume_cmd(run_id: str, session_id: str | None, json_output: bool) -> None:
    """Resume a compiled or failed expansion run."""
    service = _build_expansion_service()
    run_manager = LocalExpansionRunManager(service.task_manager.db)
    run = run_manager.get(run_id)
    if run is None:
        raise click.ClickException(f"Expansion run not found: {run_id}")
    resolved_session_id = _resolve_cli_session_id(session_id)
    if run.compiled_spec:
        run = service.apply_run(run_id, session_id=resolved_session_id)
    else:
        run = asyncio.run(service.compile_and_apply_run(run_id, session_id=resolved_session_id))
    if json_output:
        click.echo(json.dumps(run.to_dict()))
        return
    click.echo(f"Run: {run.id}")
    click.echo(f"Status: {run.status}")
    click.echo(f"Created tasks: {len(run.created_task_ids or [])}")
