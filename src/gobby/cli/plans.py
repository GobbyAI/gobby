"""CLI surface for DB-backed plan management."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import click
import psycopg

from gobby.code_index.storage import CodeIndexStorage
from gobby.plans.parser import PlanParseError, parse_plan
from gobby.plans.review_evidence_io import normalize_plan_path
from gobby.plans.review_evidence_models import PlanReviewEvidence, ReviewEvidenceError
from gobby.plans.review_evidence_store import (
    MAX_REVIEW_EVIDENCE_LIST_LIMIT,
    PlanReviewEvidenceStore,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.plans import LocalPlanManager, PlanNotFoundError
from gobby.storage.projects import LocalProjectManager
from gobby.tasks.expansion._validate import validate_plan_file
from gobby.utils.json_helpers import json_dumps
from gobby.utils.project_context import get_project_context

from ._plan_validation_output import emit_plan_validation_messages, raise_plan_validation_failed
from .utils import resolve_project_ref

_ROOT_TASK_REF_RE = re.compile(r"^\s*root_task_ref\s*:\s*(?P<value>.+?)\s*$")


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
    manager = LocalPlanManager(db)
    records = manager.list_plans(
        state=state,
        plan_kind=plan_kind,
        project_id=_project_id(project),
    )

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

    try:
        resolved_plan_id = plan_id or _plan_id_from_file(plan_path)
        resolved_root_ref = root_task_ref or _root_ref_from_file(plan_path)
        if resolved_root_ref is None:
            raise click.ClickException("--root-task-ref is required when it cannot be inferred")

        db = _open_db()
        project_id = cast(str, _project_id(project, required=True))
        record = LocalPlanManager(db).create_plan(
            project_id=project_id,
            plan_id=resolved_plan_id,
            plan_path=plan_path,
            plan_kind=plan_kind,
            root_task_ref=resolved_root_ref,
        )
    except click.ClickException:
        raise
    except (PlanParseError, UnicodeDecodeError, ValueError, OSError, psycopg.Error) as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Registered {record.plan_id} ({record.state})")


@plans.command("validate")
@click.argument("plan_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--project", "-p", "project_ref", help="Project context for code-index checks.")
@click.option(
    "--mode",
    type=click.Choice(["standard", "expansion"]),
    default="standard",
    show_default=True,
    help="Validation mode.",
)
def validate_plan_command(
    plan_file: Path,
    project_ref: str | None,
    mode: str,
) -> None:
    """Validate a plan file."""
    result = _validate_plan_for_cli(
        plan_file,
        project_ref,
        mode=mode,
    )
    if not result["valid"]:
        raise_plan_validation_failed(result)

    emit_plan_validation_messages({"warnings": result.get("warnings")})

    click.echo(f"Plan: {result['path']}")
    phases = result.get("phases")
    phase_items = phases.items() if isinstance(phases, dict) else ()
    phase_count = result.get("phase_count", len(phases) if isinstance(phases, dict) else 0)
    click.echo(f"Phases: {phase_count}")
    if not isinstance(phases, dict):
        click.echo("  No phase metadata available")
    for phase_num, title in phase_items:
        click.echo(f"  {phase_num}: {title}")


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

    click.echo(f"Archived {record.plan_id}: {record.plan_path}")


@plans.command("review-evidence")
@click.option("--plan", "plan_path", type=click.Path(dir_okay=False, path_type=Path))
@click.option("--open", "live_only", is_flag=True, help="Show only live evidence.")
@click.option("--json", "json_format", is_flag=True, help="Emit a stable JSON envelope.")
@click.option(
    "--limit",
    type=click.IntRange(1, MAX_REVIEW_EVIDENCE_LIST_LIMIT),
    default=50,
    show_default=True,
)
def review_evidence_command(
    plan_path: Path | None,
    live_only: bool,
    json_format: bool,
    limit: int,
) -> None:
    """List recent plan-review evidence."""

    db = _open_db()
    project_id = cast(str, _project_id(None, required=True))
    normalized_plan_path = _normalized_evidence_plan_path(db, project_id, plan_path)
    evidence = PlanReviewEvidenceStore(db).list_recent(
        project_id=project_id,
        plan_path=normalized_plan_path,
        live_only=live_only,
        limit=limit,
    )

    if json_format:
        click.echo(
            json_dumps({"evidence": [_evidence_json_row(row) for row in evidence]}, indent=2)
        )
        return
    if not evidence:
        click.echo("No plan review evidence found.")
        return
    click.echo(_render_evidence_table(evidence))


@plans.command("review-runs")
@click.argument("planning_task_ref")
def review_runs_command(planning_task_ref: str) -> None:
    """Show the expansion-QA review-runs handoff surface."""

    click.echo(
        "Run expansion QA coverage via gobby-tasks-ops:run_expansion_qa_coverage "
        f"for planning task {planning_task_ref}."
    )


def _open_db() -> HubDatabase:
    from gobby.cli.runtime import require_cli_database

    return require_cli_database()


def _normalized_evidence_plan_path(
    db: HubDatabase,
    project_id: str,
    plan_path: Path | None,
) -> str | None:
    if plan_path is None:
        return None
    project = LocalProjectManager(db).get(project_id)
    if project is None or project.repo_path is None:
        raise click.ClickException(f"Project {project_id} has no repository path")
    root = Path(project.repo_path).expanduser().resolve()
    try:
        return normalize_plan_path(root, plan_path).relative_to(root).as_posix()
    except (OSError, ReviewEvidenceError) as exc:
        raise click.ClickException(str(exc)) from exc


def _evidence_state(evidence: PlanReviewEvidence) -> str:
    if evidence.expired_at is not None:
        return "expired"
    if evidence.finalized_at is not None:
        return "finalized"
    return "live"


def _evidence_binding_kind(evidence: PlanReviewEvidence) -> str:
    return "session" if evidence.session_id is not None else "task+stage"


def _utc_iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _evidence_json_row(evidence: PlanReviewEvidence) -> dict[str, object]:
    return {
        "evidence_id": evidence.evidence_id,
        "plan_path": evidence.plan_path,
        "round": evidence.round_number,
        "binding_kind": _evidence_binding_kind(evidence),
        "session_id": evidence.session_id,
        "task_id": evidence.task_id,
        "stage": evidence.stage,
        "dispatch_run_id": evidence.dispatch_run_id,
        "state": _evidence_state(evidence),
        "lease_expires_at": _utc_iso(evidence.lease_expires_at),
        "manifest_state": evidence.manifest_state,
        "lesson_mint_status": evidence.lesson_mint_status,
        "created_at": _utc_iso(evidence.created_at),
    }


def _render_evidence_table(evidence: list[PlanReviewEvidence]) -> str:
    headers = (
        "Evidence",
        "Plan",
        "Round",
        "Binding",
        "Run",
        "State",
        "Lease",
        "Manifest",
        "Lesson",
        "Created",
    )
    body = [
        (
            row.evidence_id[:8],
            row.plan_path,
            str(row.round_number),
            _evidence_binding_kind(row),
            row.dispatch_run_id[:8] if row.dispatch_run_id else "-",
            _evidence_state(row),
            (_utc_iso(row.lease_expires_at) or "-")[:19],
            row.manifest_state or "-",
            row.lesson_mint_status or "-",
            (_utc_iso(row.created_at) or "-")[:19],
        )
        for row in evidence
    ]
    widths = [
        max(len(headers[index]), *(len(item[index]) for item in body))
        for index in range(len(headers))
    ]
    lines = [
        "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)),
        "  ".join("-" * width for width in widths),
    ]
    lines.extend(
        "  ".join(item[index].ljust(widths[index]) for index in range(len(headers)))
        for item in body
    )
    return "\n".join(lines)


def _validate_plan_for_cli(
    plan_file: Path,
    project_ref: str | None,
    *,
    mode: str,
) -> dict[str, Any]:
    plan_path = plan_file if plan_file.is_absolute() else Path.cwd() / plan_file
    structural_result = validate_plan_file(None, plan_path)
    if not structural_result.get("valid"):
        return structural_result

    require_symbol_validation = project_ref is not None or mode == "expansion"
    expected_project_id = _project_id(project_ref) if project_ref is not None else None
    project_context = get_project_context(Path.cwd())
    if project_context is None:
        result = validate_plan_file(
            None,
            plan_path,
            expected_project_id=expected_project_id,
            require_symbol_validation=require_symbol_validation,
            consumer_coverage_blocking=mode == "expansion",
        )
    else:
        db = _open_db()
        result = validate_plan_file(
            None,
            plan_path,
            project_context=project_context,
            expected_project_id=expected_project_id,
            code_index=CodeIndexStorage(db),
            require_symbol_validation=require_symbol_validation,
            consumer_coverage_blocking=mode == "expansion",
        )
    return _with_symbol_validation_warnings(result)


def _with_symbol_validation_warnings(result: dict[str, Any]) -> dict[str, Any]:
    envelope = result.get("symbol_validation")
    if not isinstance(envelope, dict):
        return result
    issues = envelope.get("issues")
    messages = (
        [
            issue["message"]
            for issue in issues
            if isinstance(issue, dict)
            and issue.get("blocking") is False
            and isinstance(issue.get("message"), str)
        ]
        if isinstance(issues, list)
        else []
    )
    result["warnings"] = list(dict.fromkeys([*result.get("warnings", []), *messages]))
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
