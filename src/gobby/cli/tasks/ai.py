"""AI-powered task suggestion command."""

import click

from gobby.cli.tasks._utils import get_task_manager
from gobby.tasks.state_semantics import current_stage_state, is_task_closed
from gobby.utils.json_helpers import json_dumps


@click.command("suggest")
@click.option("--type", "-t", "task_type", help="Filter by task type")
@click.option("--no-prefer-subtasks", is_flag=True, help="Don't prefer leaf tasks over parents")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def suggest_cmd(task_type: str | None, no_prefer_subtasks: bool, json_format: bool) -> None:
    """Suggest the next task to work on based on priority and readiness."""
    manager = get_task_manager()
    prefer_subtasks = not no_prefer_subtasks

    ready_tasks = manager.list_ready_tasks(task_type=task_type, limit=50)

    if not ready_tasks:
        if json_format:
            click.echo(json_dumps({"suggestion": None, "reason": "No ready tasks found"}))
        else:
            click.echo("No ready tasks found.")
        return

    # Score each task
    scored = []
    for task in ready_tasks:
        score = 0

        # Priority boost (1=high gets +30, 2=medium gets +20, 3=low gets +10)
        score += (4 - task.priority) * 10

        # Check if it's a leaf task (no children)
        children = manager.list_tasks(parent_task_id=task.id, closed=False, limit=1)
        is_leaf = len(children) == 0

        if prefer_subtasks and is_leaf:
            score += 25

        # Bonus for tasks with category defined
        if task.category:
            score += 10

        scored.append((task, score, is_leaf))

    # Sort by score descending
    scored.sort(key=lambda x: x[1], reverse=True)
    best_task, best_score, is_leaf = scored[0]

    reasons = []
    if best_task.priority == 1:
        reasons.append("high priority")
    if is_leaf:
        reasons.append("actionable leaf task")
    if best_task.category:
        reasons.append(f"has category ({best_task.category})")

    reason_str = f"Selected because: {', '.join(reasons) if reasons else 'best available option'}"

    if json_format:
        result = {
            "suggestion": best_task.to_dict(),
            "score": best_score,
            "reason": reason_str,
            "alternatives": [
                {"task_id": t.id, "title": t.title, "score": s} for t, s, _ in scored[1:4]
            ],
        }
        click.echo(json_dumps(result, indent=2, default=str))
        return

    click.echo("Suggested next task:\n")
    click.echo(f"  {best_task.id}")
    click.echo(f"  {best_task.title}")
    stage_state = current_stage_state(best_task) or "ready"
    if is_task_closed(best_task):
        stage_state = "closed"
    click.echo(f"  Priority: {best_task.priority} | Current Stage: {stage_state}")
    if best_task.description:
        desc_preview = best_task.description[:200]
        if len(best_task.description) > 200:
            desc_preview += "..."
        click.echo(f"\n  {desc_preview}")
    click.echo(f"\n  {reason_str}")

    if len(scored) > 1:
        click.echo("\nAlternatives:")
        for task, _score, _ in scored[1:4]:
            click.echo(f"  {task.id[:12]}: {task.title[:50]}")
