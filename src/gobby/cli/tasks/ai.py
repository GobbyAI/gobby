"""
AI-powered task commands (expand, validate, suggest, etc.)
"""

import sys
from typing import Any

import click

from gobby.cli.tasks._utils import get_task_manager, resolve_task_id
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.utils.project_context import get_project_context


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
@click.option("--max-iterations", "-i", type=int, default=1, help="Max validation retry attempts")
@click.option("--external", is_flag=True, help="Use external validator agent")
@click.option("--skip-build", is_flag=True, help="Skip build verification before validation")
@click.option("--history", is_flag=True, help="Show validation history instead of validating")
@click.option("--recurring", is_flag=True, help="Show recurring issues instead of validating")
def validate_task_cmd(
    task_id: str,
    summary: str | None,
    summary_file: str | None,
    max_iterations: int,
    external: bool,
    skip_build: bool,
    history: bool,
    recurring: bool,
) -> None:
    """Validate a task.

    TASK can be: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID.

    For parent tasks (with children), validates that all children are closed.
    For leaf tasks, uses LLM-based validation against criteria.

    Use --history to view past validation iterations.
    Use --recurring to see issues that keep appearing.
    """
    import asyncio

    from gobby.config.app import load_config
    from gobby.llm import LLMService
    from gobby.tasks.validation import TaskValidator, ValidationResult
    from gobby.tasks.validation_history import ValidationHistoryManager

    manager = get_task_manager()
    resolved = resolve_task_id(manager, task_id)
    if not resolved:
        return

    # Handle --history flag: show validation history
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

    # Handle --recurring flag: show recurring issues
    if recurring:
        history_manager = ValidationHistoryManager(manager.db)
        summary_data = history_manager.get_recurring_issue_summary(resolved.id)
        has_recurring = history_manager.has_recurring_issues(resolved.id)
        click.echo(f"Recurring issues for {resolved.id}:")
        click.echo(f"  Has recurring issues: {has_recurring}")
        click.echo(f"  Total iterations: {summary_data['total_iterations']}")
        if summary_data["recurring_issues"]:
            for issue in summary_data["recurring_issues"]:
                click.echo(f"  - {issue['title']} (count: {issue['count']})")
        else:
            click.echo("  No recurring issues found.")
        return

    # Check if task has children (is a parent task)
    children = manager.list_tasks(parent_task_id=resolved.id, limit=1000)

    if children:
        # Parent task: validate based on child completion
        open_children = [c for c in children if c.status != "closed"]
        all_closed = len(open_children) == 0

        if all_closed:
            result = ValidationResult(
                status="valid",
                feedback=f"All {len(children)} child tasks are completed.",
            )
        else:
            open_titles = [f"- {c.id}: {c.title}" for c in open_children[:5]]
            remaining = len(open_children) - 5 if len(open_children) > 5 else 0
            feedback = f"{len(open_children)} of {len(children)} child tasks still open:\n"
            feedback += "\n".join(open_titles)
            if remaining > 0:
                feedback += f"\n... and {remaining} more"
            result = ValidationResult(status="invalid", feedback=feedback)

        click.echo(f"Validation Status: {result.status.upper()}")
        if result.feedback:
            click.echo(f"Feedback:\n{result.feedback}")

        # Update validation status
        updates: dict[str, Any] = {
            "validation_status": result.status,
            "validation_feedback": result.feedback,
        }
        if result.status == "valid":
            manager.close_task(resolved.id, reason="All child tasks completed")
            click.echo("Task closed.")
        manager.update_task(resolved.id, **updates)
        return

    # Leaf task: need changes summary
    changes_summary = ""
    if summary_file:
        try:
            with open(summary_file, encoding="utf-8") as f:
                changes_summary = f.read()
        except Exception as e:
            click.echo(f"Error reading summary file: {e}", err=True)
            return
    elif summary:
        changes_summary = summary
    else:
        # Prompt from stdin
        click.echo("Enter changes summary (Ctrl+D to finish):")
        changes_summary = sys.stdin.read()

    if not changes_summary.strip():
        click.echo("Error: Changes summary is required for leaf tasks.", err=True)
        return

    click.echo(f"Validating task {resolved.id}...")

    # Initialize validator
    try:
        config = load_config()
        llm_service = LLMService(config)
        validator = TaskValidator(config.gobby_tasks.validation, llm_service)
    except Exception as e:
        click.echo(f"Error initializing validator: {e}", err=True)
        return

    # Run validation
    try:
        result = asyncio.run(
            validator.validate_task(
                task_id=resolved.id,
                title=resolved.title,
                description=resolved.description,
                changes_summary=changes_summary,
                validation_criteria=resolved.validation_criteria,
            )
        )

        click.echo(f"Validation Status: {result.status.upper()}")
        if result.feedback:
            click.echo(f"Feedback:\n{result.feedback}")

        # Apply validation updates
        validation_updates: dict[str, Any] = {
            "validation_status": result.status,
            "validation_feedback": result.feedback,
        }
        MAX_RETRIES = 3

        if result.status == "valid":
            manager.close_task(resolved.id, reason="Completed via validation")
            click.echo("Task closed.")
        elif result.status == "invalid":
            current_fail_count = resolved.validation_fail_count or 0
            new_fail_count = current_fail_count + 1
            validation_updates["validation_fail_count"] = new_fail_count

            if new_fail_count < MAX_RETRIES:
                fix_task = manager.create_task(
                    project_id=resolved.project_id,
                    title=f"Fix validation failures for {resolved.title}",
                    description=f"Validation failed with feedback:\n{result.feedback}\n\nPlease fix the issues and re-validate.",
                    parent_task_id=resolved.id,
                    priority=1,
                    task_type="bug",
                )
                validation_updates["validation_feedback"] = (
                    result.feedback or ""
                ) + f"\n\nCreated fix task: {fix_task.id}"
                click.echo(f"Created fix task: {fix_task.id}")
            else:
                from datetime import UTC, datetime

                validation_updates["status"] = "escalated"
                validation_updates["escalated_at"] = datetime.now(UTC).isoformat()
                validation_updates["escalation_reason"] = (
                    f"exceeded_validation_retries ({MAX_RETRIES})"
                )
                validation_updates["validation_feedback"] = (
                    result.feedback or ""
                ) + f"\n\nExceeded max retries ({MAX_RETRIES}). Escalated for human intervention."
                click.echo("Exceeded max retries. Task ESCALATED for human intervention.")

        manager.update_task(resolved.id, **validation_updates)

    except Exception as e:
        click.echo(f"Validation error: {e}", err=True)


@click.command("complexity")
@click.argument("task_id", required=False)
@click.option("--all", "analyze_all", is_flag=True, help="Analyze all pending tasks")
@click.option("--pending", is_flag=True, help="Only analyze pending (open) tasks (use with --all)")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def complexity_cmd(
    task_id: str | None,
    analyze_all: bool,
    pending: bool,
    json_format: bool,
) -> None:
    """Analyze task complexity based on subtasks or description."""
    import json as json_mod

    manager = get_task_manager()

    if analyze_all:
        # Batch analysis
        project_ctx = get_project_context()
        project_id = project_ctx.get("id") if project_ctx else None

        status_filter = "open" if pending else None
        tasks_list = manager.list_tasks(
            project_id=project_id,
            status=status_filter,
            limit=100,
        )

        if not tasks_list:
            click.echo("No tasks found to analyze.")
            return

        results = []
        for task in tasks_list:
            result = _analyze_task_complexity(manager, task)
            results.append(result)

        if json_format:
            click.echo(json_mod.dumps(results, indent=2))
            return

        click.echo(f"Analyzed {len(results)} tasks:\n")
        for r in results:
            click.echo(
                f"  {r['task_id'][:12]} | Score: {r['complexity_score']:2}/10 | {r['title'][:50]}"
            )

    else:
        # Single task analysis
        if not task_id:
            click.echo("Error: TASK_ID required (or use --all)", err=True)
            return

        resolved = resolve_task_id(manager, task_id)
        if not resolved:
            return

        result = _analyze_task_complexity(manager, resolved)

        if json_format:
            click.echo(json_mod.dumps(result, indent=2))
            return

        click.echo(f"Task: {result['title']}")
        click.echo(f"ID: {result['task_id']}")
        click.echo(f"Complexity Score: {result['complexity_score']}/10")
        click.echo(f"Reasoning: {result['reasoning']}")
        click.echo(f"Recommended Subtasks: {result['recommended_subtasks']}")
        if result["existing_subtasks"] > 0:
            click.echo(f"Existing Subtasks: {result['existing_subtasks']}")


def _analyze_task_complexity(manager: LocalTaskManager, task: Task) -> dict[str, Any]:
    """Analyze complexity for a single task. Returns dict with results."""
    # Check for existing subtasks
    subtasks = manager.list_tasks(parent_task_id=task.id, limit=100)
    subtask_count = len(subtasks)

    # Simple heuristic-based complexity
    if subtask_count > 0:
        score = min(10, 1 + subtask_count // 2)
        reasoning = f"Task has {subtask_count} subtasks"
        recommended = subtask_count
    else:
        desc_len = len(task.description or "")
        if desc_len < 100:
            score = 2
            reasoning = "Short description, likely simple task"
            recommended = 2
        elif desc_len < 500:
            score = 5
            reasoning = "Medium description, moderate complexity"
            recommended = 5
        else:
            score = 8
            reasoning = "Long description, likely complex task"
            recommended = 10

    # Update task with complexity score
    manager.update_task(
        task.id,
        complexity_score=score,
        estimated_subtasks=recommended,
    )

    return {
        "task_id": task.id,
        "title": task.title,
        "complexity_score": score,
        "reasoning": reasoning,
        "recommended_subtasks": recommended,
        "existing_subtasks": subtask_count,
    }


@click.command("suggest")
@click.option("--type", "-t", "task_type", help="Filter by task type")
@click.option("--no-prefer-subtasks", is_flag=True, help="Don't prefer leaf tasks over parents")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def suggest_cmd(task_type: str | None, no_prefer_subtasks: bool, json_format: bool) -> None:
    """Suggest the next task to work on based on priority and readiness."""
    import json as json_mod

    manager = get_task_manager()
    prefer_subtasks = not no_prefer_subtasks

    ready_tasks = manager.list_ready_tasks(task_type=task_type, limit=50)

    if not ready_tasks:
        if json_format:
            click.echo(json_mod.dumps({"suggestion": None, "reason": "No ready tasks found"}))
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
        children = manager.list_tasks(parent_task_id=task.id, status="open", limit=1)
        is_leaf = len(children) == 0

        if prefer_subtasks and is_leaf:
            score += 25

        # Bonus for tasks with clear complexity
        if task.complexity_score and task.complexity_score <= 5:
            score += 15

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
    if best_task.complexity_score and best_task.complexity_score <= 5:
        reasons.append("manageable complexity")
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
        click.echo(json_mod.dumps(result, indent=2, default=str))
        return

    click.echo("Suggested next task:\n")
    click.echo(f"  {best_task.id}")
    click.echo(f"  {best_task.title}")
    click.echo(f"  Priority: {best_task.priority} | Status: {best_task.status}")
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
