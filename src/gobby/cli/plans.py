"""CLI surface for DB-backed plan management."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import click
import psycopg

from gobby.code_index.storage import CodeIndexStorage
from gobby.plans.consumer_sweep import run_consumer_sweep
from gobby.plans.parser import PlanParseError, parse_plan
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.plans import LocalPlanManager, PlanNotFoundError
from gobby.tasks.expansion._validate import validate_plan_file

from .utils import resolve_project_ref

_ROOT_TASK_REF_RE = re.compile(r"^\s*root_task_ref\s*:\s*(?P<value>.+?)\s*$")


@dataclass(frozen=True)
class _CliCodeIndexContext:
    storage: CodeIndexStorage


@click.group("plans")
def plans() -> None:
    """Manage DB-backed plan records."""


@plans.command("list")
@click.option("--state", type=click.Choice(["active", "archived"]))
@click.option("--kind", "plan_kind", type=click.Choice(["implementation", "strategy"]))
@click.option("--project")
def list_plans_command(
    state: str | None,
    plan_kind: str | None,
    project: str | None,
) -> None:
    """List plans."""

    db = _open_db()
    try:
        manager = LocalPlanManager(db)
        records = manager.list_plans(
            state=state,
            plan_kind=plan_kind,
            project_id=_project_id(project),
        )
    finally:
        db.close()

    for record in records:
        click.echo(
            f"{record.plan_id}\t{record.state}\t{record.plan_kind}\t"
            f"{record.root_task_ref}\t{record.plan_path}"
        )


@plans.command("show")
@click.argument("plan_id")
@click.option("--project")
def show_plan_command(plan_id: str, project: str | None) -> None:
    """Show one plan."""

    db = _open_db()
    try:
        record = LocalPlanManager(db).get_plan(plan_id, project_id=_project_id(project))
    except PlanNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        db.close()

    for key, value in record.to_dict().items():
        click.echo(f"{key}: {value}")


@plans.command("register")
@click.argument("plan_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--plan-id")
@click.option("--kind", "plan_kind", default="implementation", show_default=True)
@click.option("--root-task-ref")
@click.option("--project")
def register_plan_command(
    plan_path: Path,
    plan_id: str | None,
    plan_kind: str,
    root_task_ref: str | None,
    project: str | None,
) -> None:
    """Register a plan and emit its managed coverage manifest."""

    resolved_plan_id = plan_id or _plan_id_from_file(plan_path)
    resolved_root_ref = root_task_ref or _root_ref_from_file(plan_path)
    if resolved_root_ref is None:
        raise click.ClickException("--root-task-ref is required when it cannot be inferred")

    db = _open_db()
    try:
        project_id = cast(str, _project_id(project, required=True))
        record = LocalPlanManager(db).create_plan(
            project_id=project_id,
            plan_id=resolved_plan_id,
            plan_path=plan_path,
            plan_kind=plan_kind,
            root_task_ref=resolved_root_ref,
        )
    except (PlanParseError, ValueError, OSError, psycopg.Error) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        db.close()

    click.echo(f"Registered {record.plan_id} ({record.state})")


@plans.command("validate")
@click.argument("plan_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--project", "-p", "project_ref", help="Project context for code-index checks.")
@click.option(
    "--mode",
    type=click.Choice(["standard", "expansion"]),
    default="standard",
    show_default=True,
    help="Validation mode. expansion always includes test consumers.",
)
@click.option(
    "--include-tests",
    is_flag=True,
    help="Include test files in standard-mode consumer-sweep target coverage.",
)
def validate_plan_command(
    plan_file: Path,
    project_ref: str | None,
    mode: str,
    include_tests: bool,
) -> None:
    """Validate a plan file, including semantic and consumer-sweep lint."""
    include_test_consumers = mode == "expansion" or include_tests

    result = _validate_plan_for_cli(
        plan_file,
        project_ref,
        include_tests=include_test_consumers,
    )
    if not result["valid"]:
        for error in result.get("errors", []):
            click.echo(f"Error: {error}", err=True)
        raise click.ClickException("Plan validation failed")

    click.echo(f"Plan: {result['path']}")
    phases = result.get("phases")
    phase_items = phases.items() if isinstance(phases, dict) else ()
    phase_count = result.get("phase_count", len(phases) if isinstance(phases, dict) else 0)
    click.echo(f"Phases: {phase_count}")
    if not isinstance(phases, dict):
        click.echo("  No phase metadata available")
    for phase_num, title in phase_items:
        click.echo(f"  {phase_num}: {title}")

    sweep = result.get("consumer_sweep")
    if isinstance(sweep, dict):
        if sweep.get("skipped"):
            reason = sweep.get("skip_reason") or "not available"
            click.echo(f"Consumer sweep: skipped ({reason})")
        else:
            click.echo("Consumer sweep: passed")


@plans.command("archive")
@click.argument("plan_id")
@click.option("--reason")
@click.option("--project")
def archive_plan_command(plan_id: str, reason: str | None, project: str | None) -> None:
    """Archive a plan and remove its managed coverage manifest."""

    db = _open_db()
    try:
        record = LocalPlanManager(db).archive_plan(
            plan_id,
            project_id=_project_id(project),
            reason=reason,
        )
    except (PlanNotFoundError, ValueError, OSError, psycopg.Error) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        db.close()

    click.echo(f"Archived {record.plan_id}: {record.plan_path}")


@plans.command("review-runs")
@click.argument("planning_task_ref")
def review_runs_command(planning_task_ref: str) -> None:
    """Show the expansion-QA review-runs handoff surface."""

    click.echo(
        "Run expansion QA coverage via gobby-tasks-ops:run_expansion_qa_coverage "
        f"for planning task {planning_task_ref}."
    )


def _open_db() -> HubDatabase:
    from gobby.storage.hub.runtime import open_runtime_hub_database

    return open_runtime_hub_database(apply_migrations=False)


def _validate_plan_for_cli(
    plan_file: Path,
    project_ref: str | None,
    *,
    include_tests: bool,
) -> dict[str, Any]:
    plan_path = plan_file if plan_file.is_absolute() else Path.cwd() / plan_file
    result = validate_plan_file(None, plan_path)
    if not result.get("valid"):
        return result

    project_id = resolve_project_ref(project_ref)
    try:
        plan_doc = parse_plan(plan_path, parse_mode="draft")
    except (OSError, PlanParseError) as exc:
        return {"valid": False, "errors": [f"Plan file is not contract-conforming: {exc}"]}

    db: HubDatabase | None = None
    code_index: _CliCodeIndexContext | None = None
    if project_id:
        db = _open_db()
        code_index = _CliCodeIndexContext(CodeIndexStorage(db))

    try:
        try:
            sweep = run_consumer_sweep(
                plan_doc,
                project_id=project_id,
                code_index=code_index,
                include_tests=include_tests,
            )
        except (OSError, psycopg.Error, ValueError) as exc:
            return {
                **result,
                "valid": False,
                "errors": [*result.get("errors", []), f"Consumer sweep failed: {exc}"],
            }
    finally:
        if db is not None:
            db.close()

    result["consumer_sweep"] = sweep.to_dict()
    if not sweep.valid:
        result["valid"] = False
        result["errors"] = [*result.get("errors", []), *sweep.errors]
    return result


def _project_id(project: str | None, *, required: bool = False) -> str | None:
    project_id = resolve_project_ref(project, exit_on_not_found=False)
    if project_id is None and required:
        raise click.ClickException("No project context found")
    return project_id


def _plan_id_from_file(path: Path) -> str:
    doc = parse_plan(path, parse_mode="draft")
    return doc.plan_id or path.stem


def _root_ref_from_file(path: Path) -> str | None:
    stem = path.stem
    if stem.startswith("task-"):
        token = stem.split("-", 2)[1]
        if token.isdecimal():
            return token
    text = path.read_text(encoding="utf-8")
    return _root_ref_from_metadata(text)


def _root_ref_from_metadata(text: str) -> str | None:
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            root_ref = _root_ref_from_line(line)
            if root_ref is not None:
                return root_ref

    for line in lines:
        if line.lstrip().startswith("#"):
            break
        root_ref = _root_ref_from_line(line)
        if root_ref is not None:
            return root_ref
    return None


def _root_ref_from_line(line: str) -> str | None:
    match = _ROOT_TASK_REF_RE.match(line)
    if match is None:
        return None
    value = match.group("value").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1].strip()
    return value or None


__all__ = ["plans"]
