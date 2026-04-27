"""Plan-related CLI commands."""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import click

from gobby.plans.coverage import (
    CoverageReport,
    CoverageStatus,
    MissingScopeError,
    StaleHashError,
    TaskTreeSource,
    evaluate,
)
from gobby.plans.coverage_manifest import (
    EmptyComponentError,
    IdentityCollisionError,
    PathIdentityMismatchError,
    write_manifest,
)


@click.group()
def plan() -> None:
    """Plan commands."""


@plan.command("coverage")
@click.option(
    "--plan",
    "plan_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option("--plan-id", required=True)
@click.option("--plan-hash", required=True)
@click.option("--task-tree", type=click.Choice(["db", "jsonl", "matrix-file"]), required=True)
@click.option("--root-task")
@click.option("--project-id")
@click.option("--matrix-file", type=click.Path(dir_okay=False, path_type=Path))
@click.option("--evidence")
@click.option("--manifest", type=click.Path(dir_okay=False, path_type=Path))
@click.option("--regenerate", is_flag=True)
def coverage(
    plan_path: Path,
    plan_id: str,
    plan_hash: str,
    task_tree: str,
    root_task: str | None,
    project_id: str | None,
    matrix_file: Path | None,
    evidence: str | None,
    manifest: Path | None,
    regenerate: bool,
) -> None:
    """Evaluate plan coverage."""
    try:
        report = _evaluate_for_cli(
            plan_path=plan_path,
            plan_id=plan_id,
            plan_hash=plan_hash,
            task_tree=task_tree,
            root_task=root_task,
            project_id=project_id,
            matrix_file=matrix_file,
        )
        output_path = write_manifest(
            report,
            Path.cwd(),
            regenerate=regenerate,
            manifest_path=manifest,
        )
    except StaleHashError as exc:
        _fail(str(exc), 4)
    except IdentityCollisionError as exc:
        _fail(str(exc), 5)
    except MissingScopeError as exc:
        _fail(str(exc), 6)
    except EmptyComponentError as exc:
        _fail(str(exc), 7)
    except PathIdentityMismatchError as exc:
        _fail(str(exc), 8)

    click.echo(output_path)
    _ = evidence
    raise click.exceptions.Exit(_report_exit_code(report))


def _evaluate_for_cli(
    *,
    plan_path: Path,
    plan_id: str,
    plan_hash: str,
    task_tree: str,
    root_task: str | None,
    project_id: str | None,
    matrix_file: Path | None,
) -> CoverageReport:
    if task_tree == TaskTreeSource.matrix_file.value:
        if root_task is not None or project_id is not None:
            raise MissingScopeError("matrix-file coverage does not accept root-task/project-id")
        if matrix_file is None:
            raise MissingScopeError("matrix-file coverage requires --matrix-file")
        return evaluate(
            plan=plan_path,
            plan_id=plan_id,
            plan_hash=plan_hash,
            task_tree=TaskTreeSource.matrix_file,
            matrix_file=matrix_file,
        )

    if root_task is None:
        raise MissingScopeError(f"{task_tree} coverage requires --root-task")
    if project_id is None:
        raise MissingScopeError(f"{task_tree} coverage requires --project-id")

    if task_tree == TaskTreeSource.db.value:
        return evaluate(
            plan=plan_path,
            plan_id=plan_id,
            plan_hash=plan_hash,
            task_tree=TaskTreeSource.db,
            root_task_ref=root_task,
            project_id=project_id,
        )
    return evaluate(
        plan=plan_path,
        plan_id=plan_id,
        plan_hash=plan_hash,
        task_tree=TaskTreeSource.jsonl,
        root_task_ref=root_task,
        project_id=project_id,
    )


def _report_exit_code(report: CoverageReport) -> int:
    if any(row.status is CoverageStatus.invalid for row in report.rows):
        return 3
    if any(row.status is CoverageStatus.missing for row in report.rows):
        return 2
    return 0


def _fail(message: str, code: int) -> NoReturn:
    click.echo(f"Error: {message}", err=True)
    raise click.exceptions.Exit(code)


__all__ = ["plan"]
