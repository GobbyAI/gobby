"""Plan-related CLI commands."""

from __future__ import annotations

import subprocess  # nosec B404 - evidence resolution shells out to local git.
from dataclasses import asdict, dataclass
from functools import cached_property
from pathlib import Path
from typing import Any, NoReturn

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
from gobby.plans.evidence import (
    EvidenceBundle,
    EvidenceRow,
    InvalidEvidenceError,
    resolve_evidence,
)
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.commits import get_task_diff


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
        evidence_rows = _resolve_evidence_rows(evidence, project_id=project_id)
        report = _evaluate_for_cli(
            plan_path=plan_path,
            plan_id=plan_id,
            plan_hash=plan_hash,
            task_tree=task_tree,
            root_task=root_task,
            project_id=project_id,
            matrix_file=matrix_file,
            evidence=evidence_rows,
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
    except InvalidEvidenceError as exc:
        _fail(str(exc), 3)

    click.echo(output_path)
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
    evidence: tuple[EvidenceRow, ...],
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
            evidence=evidence,
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
            evidence=evidence,
        )
    return evaluate(
        plan=plan_path,
        plan_id=plan_id,
        plan_hash=plan_hash,
        task_tree=TaskTreeSource.jsonl,
        root_task_ref=root_task,
        project_id=project_id,
        evidence=evidence,
    )


def _resolve_evidence_rows(spec: str | None, *, project_id: str | None) -> tuple[EvidenceRow, ...]:
    if spec is None:
        return ()
    bundle = _resolve_evidence_bundle(spec, project_id=project_id)
    return bundle.rows


def _resolve_evidence_bundle(spec: str, *, project_id: str | None) -> EvidenceBundle:
    return resolve_evidence(
        spec,
        ctx=_CliEvidenceContext(repo_root=Path.cwd(), project_id=project_id),
    )


@dataclass
class _CliEvidenceContext:
    repo_root: Path
    project_id: str | None

    @cached_property
    def task_manager(self) -> LocalTaskManager:
        db = LocalDatabase()
        run_migrations(db)
        return LocalTaskManager(db)

    def get_task_diff(self, task_ref: str) -> str:
        task_id = self._resolve_task_id(task_ref)
        return get_task_diff(task_id, self.task_manager, cwd=self.repo_root).diff

    def get_artifacts(self, task_ref: str) -> dict[str, Any] | None:
        task_id = self._resolve_task_id(task_ref)
        artifacts = self.task_manager.artifacts.get_artifacts(task_id)
        raw = asdict(artifacts)
        if raw["updated_at"] is None:
            return None
        return raw

    def get_commit_range_diff(self, range_: str) -> str:
        result = subprocess.run(  # nosec B603, B607 - fixed git argv plus caller ref.
            ["git", "-C", str(self.repo_root), "diff", range_],
            check=False,
            capture_output=True,
            text=True,
        )
        return result.stdout if result.returncode == 0 else ""

    def _resolve_task_id(self, task_ref: str) -> str:
        if self.project_id is None:
            raise InvalidEvidenceError(f"Evidence spec requires --project-id for {task_ref}")
        return self.task_manager.resolve_task_reference(task_ref, self.project_id)


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
