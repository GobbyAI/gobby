"""Output formatting helpers for the build CLI."""

from __future__ import annotations

import click

from gobby.build import BuildControlResult, BuildResult


def _echo_build_result(result: BuildResult) -> None:
    click.echo(f"Task: {result.task_id}")
    click.echo(f"Lifecycle: {_lifecycle_display(result)}")
    for warning in result.warnings:
        click.echo(f"Warning: {warning}", err=True)
    if result.applied_stages_skipped:
        click.echo(f"Skipped stages: {', '.join(result.applied_stages_skipped)}")
    tick = result.dispatcher_tick
    line = (
        f"Dispatcher tick: scanned={tick.scanned} executed={tick.executed} skipped={tick.skipped}"
    )
    if tick.cap_reached:
        line = f"{line} cap_reached"
    elif tick.reason:
        line = f"{line} reason={tick.reason}"
    click.echo(line)
    if tick.reason == "automation_disabled":
        click.echo("Build automation is paused. Run `gobby build resume` to re-enable it.")


def _lifecycle_display(result: BuildResult) -> str:
    if not result.manifest:
        return result.initial_lifecycle
    stage_names = [
        str(row["stage_name"])
        for row in result.manifest
        if isinstance(row, dict) and row.get("stage_name") is not None
    ]
    return " -> ".join(stage_names) if stage_names else result.initial_lifecycle


def _echo_build_control_result(result: BuildControlResult) -> None:
    state = "enabled" if result.enabled else "disabled"
    click.echo(f"Build {result.lifecycle_event.event.removeprefix('build_')}: project-scoped")
    click.echo("Task tree: none")
    click.echo(f"Build automation: {state}")
    click.echo(f"Project: {result.project_id}")
    click.echo(f"Event: {result.lifecycle_event.reason}")


def _echo_target_control_result(payload: dict[str, object]) -> None:
    action = str(payload.get("action", "<unknown>"))
    click.echo(f"Build {action}: task-scoped")
    root_task_id = str(payload.get("root_task_id", "<unknown>"))
    click.echo(f"Root task: {root_task_id}")
    affected = payload.get("affected_tasks")
    if isinstance(affected, list):
        click.echo(f"Affected tasks: {len(affected)}")
    agents = payload.get("agents")
    if isinstance(agents, list):
        click.echo(f"Agents: {len(agents)}")
    stages_reset = payload.get("stages_reset")
    if isinstance(stages_reset, int):
        click.echo(f"Stages reset: {stages_reset}")
    escalations_cleared = payload.get("escalations_cleared")
    if isinstance(escalations_cleared, int) and escalations_cleared:
        click.echo(f"Escalations cleared: {escalations_cleared}")
    dispatch_failures_reset = payload.get("dispatch_failures_reset")
    if isinstance(dispatch_failures_reset, int) and dispatch_failures_reset:
        click.echo(f"Dispatch failures reset: {dispatch_failures_reset}")
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, list):
        deleted = sum(1 for item in artifacts if isinstance(item, dict) and item.get("deleted"))
        click.echo(f"Artifacts: {len(artifacts)}" + (f" deleted={deleted}" if deleted else ""))
    blockers = payload.get("blocked_reasons")
    if isinstance(blockers, list) and blockers:
        click.echo("Blocked:")
        for reason in blockers:
            click.echo(f"  {reason}")
    tick = payload.get("dispatcher_tick")
    if isinstance(tick, dict):
        line = (
            "Dispatcher tick: "
            f"scanned={tick.get('scanned', 0)} "
            f"executed={tick.get('executed', 0)} "
            f"skipped={tick.get('skipped', 0)}"
        )
        if tick.get("cap_reached"):
            line = f"{line} cap_reached"
        elif tick.get("reason"):
            line = f"{line} reason={tick['reason']}"
        click.echo(line)
    if payload.get("dry_run"):
        click.echo("Dry run: no changes made")
