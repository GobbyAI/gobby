"""
AI-powered task commands (expand, validate, suggest, etc.)
"""

import sys
from pathlib import Path

import click

from gobby.cli.tasks._utils import get_task_manager, resolve_task_id
from gobby.tasks.state_semantics import current_stage_state, is_task_closed
from gobby.utils.json_helpers import json_dumps


@click.command("validate")
@click.argument("task_id", metavar="TASK")
@click.option(
    "--summary", "-s", default=None, help="Changes summary text (required for leaf tasks)"
)
@click.option(
    "--file",
    "-f",
    "summary_file",
    type=click.Path(exists=True),
    help="File containing changes summary",
)
@click.option("--history", is_flag=True, help="Show validation history instead of validating")
def validate_task_cmd(
    task_id: str,
    summary: str | None,
    summary_file: str | None,
    history: bool,
) -> None:
    """Run the bounded criteria review used by the close checklist.

    TASK can be: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID.

    Parent tasks report child completion. Leaf tasks run one read-only criteria
    review; close_task owns the full checklist and validation accounting.
    """
    import asyncio

    from gobby.cli.runtime import get_cli_runtime, require_cli_database
    from gobby.llm import LLMService
    from gobby.tasks.commits import collect_commit_diff_text
    from gobby.tasks.validation import TaskValidator
    from gobby.tasks.validation_history import ValidationHistoryManager

    manager = get_task_manager()
    resolved = resolve_task_id(manager, task_id)
    if not resolved:
        raise SystemExit(1)

    if history:
        history_manager = ValidationHistoryManager(manager.db)
        iterations = history_manager.get_iteration_history(resolved.id)
        if not iterations:
            click.echo(f"No validation history for task {resolved.id}")
            return
        click.echo(f"Validation history for {resolved.id}:")
        for it in iterations:
            click.echo(f"\n  Iteration {it.iteration}: {it.status}")
            if it.feedback:
                click.echo(f"    Feedback: {it.feedback[:100]}...")
            if it.issues:
                click.echo(f"    Issues: {len(it.issues)}")
        return

    children = manager.list_tasks(parent_task_id=resolved.id, limit=1000)
    if children:
        open_children = [c for c in children if not is_task_closed(c)]
        if not open_children:
            click.echo(f"Validation Status: VALID\nAll {len(children)} child tasks are closed.")
            return
        click.echo(f"Validation Status: INVALID\n{len(open_children)} child tasks remain open:")
        for child in open_children[:5]:
            click.echo(f"- {child.id}: {child.title}")
        if len(open_children) > 5:
            click.echo(f"... and {len(open_children) - 5} more")
        return

    changes_summary = ""
    if summary_file:
        try:
            with open(summary_file, encoding="utf-8") as f:
                changes_summary = f.read()
        except (OSError, UnicodeError) as e:
            raise click.ClickException(f"Error reading summary file: {e}") from e
    elif summary:
        changes_summary = summary
    else:
        # Prompt from stdin
        click.echo("Enter changes summary (Ctrl+D to finish):")
        changes_summary = sys.stdin.read()

    if not changes_summary.strip():
        raise click.ClickException("Changes summary is required for leaf tasks.")
    if not (resolved.validation_criteria or "").strip():
        raise click.ClickException("Validation criteria are required for leaf tasks.")

    click.echo(f"Validating task {resolved.id}...")
    try:
        config = get_cli_runtime().config
        llm_service = LLMService(config)
        validator = TaskValidator(
            config.gobby_tasks.validation,
            llm_service,
            db=require_cli_database(),
        )
    except Exception as e:
        raise click.ClickException(f"Error initializing validator: {e}") from e

    try:
        diff_text = collect_commit_diff_text(resolved.commits or [], cwd=str(Path.cwd()))
        verdict = asyncio.run(
            validator.validate_task(
                task_id=resolved.id,
                title=resolved.title,
                changes_summary=changes_summary,
                validation_criteria=resolved.validation_criteria or "",
                diff_text=diff_text,
                checklist_facts={
                    "source": "cli",
                    "linked_commit_count": len(resolved.commits or []),
                    "scope": "criteria_review_only",
                },
            )
        )
    except (RuntimeError, ValueError) as e:
        raise click.ClickException(f"Validation error: {e}") from e

    click.echo(f"Validation Status: {verdict.status.upper()}")
    if verdict.feedback:
        click.echo(f"Feedback:\n{verdict.feedback}")
    for criterion in verdict.criteria:
        if not criterion.satisfied and criterion.gap:
            click.echo(f"- Criterion {criterion.index}: {criterion.gap}")
    click.echo("Use close_task to run the full checklist and apply validation accounting.")


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
