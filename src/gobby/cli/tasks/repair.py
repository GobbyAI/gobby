"""Lifecycle repair task commands."""

from __future__ import annotations

import json

import click

from gobby.cli.tasks._utils import get_task_manager, resolve_task_id
from gobby.tasks.lifecycle_repair import LifecycleRepair, LifecycleRepairResult


@click.command("repair-lifecycle")
@click.option("--task", "task_ref", help="Task reference to inspect or repair.")
@click.option("--provenance", help="Expansion provenance label to inspect or repair.")
@click.option("--apply", "apply_changes", is_flag=True, help="Apply candidate repairs.")
@click.option("--force", is_flag=True, help="Force repair of active rows for --task only.")
@click.option("--json", "json_format", is_flag=True, help="Emit machine-readable JSON.")
def repair_lifecycle_cmd(
    task_ref: str | None,
    provenance: str | None,
    apply_changes: bool,
    force: bool,
    json_format: bool,
) -> None:
    """Inspect or repair scoped historical lifecycle manifests."""

    manager = get_task_manager()
    task_id = None
    if task_ref is not None:
        task = resolve_task_id(manager, task_ref)
        if task is None:
            raise click.ClickException(f"Task '{task_ref}' not found")
        task_id = task.id

    try:
        result = LifecycleRepair(manager).run(
            task_id=task_id,
            provenance=provenance,
            apply=apply_changes,
            force=force,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    if json_format:
        click.echo(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return
    click.echo(_render_repair_result(result))


def _render_repair_result(result: LifecycleRepairResult) -> str:
    mode = "Apply" if result.apply else "Dry run"
    lines = [
        f"{mode}: {len(result.candidates)} candidate(s) for {result.scope}",
    ]
    for candidate in result.candidates:
        status = "skipped" if candidate.skipped else "applied" if candidate.applied else "candidate"
        detail = candidate.skip_reason or candidate.reason
        lines.append(f"  {candidate.ref}  {candidate.action}  {status}: {detail}")
    return "\n".join(lines)


__all__ = ["repair_lifecycle_cmd"]
