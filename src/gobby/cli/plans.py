"""CLI surface for DB-backed plan management."""

from __future__ import annotations

from pathlib import Path

import click

from gobby.plans.parser import parse_plan
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.plans import LocalPlanManager, PlanNotFoundError

from .utils import resolve_project_ref


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
        project_id = _project_id(project, required=True)
        if project_id is None:
            raise click.ClickException("No project context found")
        record = LocalPlanManager(db).create_plan(
            project_id=project_id,
            plan_id=resolved_plan_id,
            plan_path=plan_path,
            plan_kind=plan_kind,
            root_task_ref=resolved_root_ref,
        )
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        db.close()

    click.echo(f"Registered {record.plan_id} ({record.state})")


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
    except Exception as exc:
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


def _open_db() -> LocalDatabase:
    db = LocalDatabase()
    run_migrations(db)
    return db


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
    return None


__all__ = ["plans"]
