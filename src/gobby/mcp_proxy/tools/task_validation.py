"""
Validation MCP tools for Gobby Task System.

Extracted from tasks.py using Strangler Fig pattern.

Exposes functionality for:
- Task validation (validate_task)
- Validation status (get_validation_status, reset_validation_count)
- Validation history (get_validation_history, get_recurring_issues, clear_validation_history)
- De-escalation (de_escalate_task)

These tools are registered with the InternalToolRegistry and accessed
via the downstream proxy pattern (call_tool, list_tools, get_tool_schema).
"""

import logging
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.tasks import LocalTaskManager, TaskNotFoundError
from gobby.tasks.state_semantics import projected_task_state
from gobby.tasks.validation import TaskValidator
from gobby.tasks.validation_history import ValidationHistoryManager

if TYPE_CHECKING:
    from gobby.storage.projects import LocalProjectManager

logger = logging.getLogger(__name__)


def create_validation_registry(
    task_manager: LocalTaskManager,
    task_validator: TaskValidator | None = None,
    project_manager: "LocalProjectManager | None" = None,
    get_project_repo_path: Any = None,
    max_retries: int = 3,
) -> InternalToolRegistry:
    """
    Create a validation tool registry with all validation-related tools.

    Args:
        task_manager: LocalTaskManager instance
        task_validator: TaskValidator instance (optional, enables LLM validation)
        project_manager: LocalProjectManager instance (optional)
        get_project_repo_path: Callable to get repo path from project ID (optional)

    Returns:
        InternalToolRegistry with all validation tools registered
    """
    # Lazy import to avoid circular dependency
    from gobby.mcp_proxy.tools.tasks import resolve_task_id_for_mcp

    registry = InternalToolRegistry(
        name="gobby-tasks-validation",
        description="Task validation tools - validate, criteria, history",
    )

    # Create helper managers
    validation_history_manager = ValidationHistoryManager(task_manager.db)

    @registry.tool(
        name="validate_task",
        description="Validate if a task is completed. Auto-gathers context from recent commits and relevant files if changes_summary not provided.",
    )
    async def validate_task(
        task_id: str,
        changes_summary: str | None = None,
        context_files: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Validate task completion.

        For parent tasks (tasks with children), validation checks if all children are closed.
        For leaf tasks, uses LLM-based validation against criteria.

        If changes_summary is not provided for leaf tasks, uses smart context gathering:
        1. Current uncommitted changes (staged + unstaged)
        2. Multi-commit window (last 10 commits)
        3. File-based analysis (reads files mentioned in criteria)

        Args:
            task_id: Task reference: #N, N (seq_num), path (1.2.3), or UUID
            changes_summary: Summary of changes made (optional - auto-gathered if not provided)
            context_files: List of file paths to read for context (optional)

        Returns:
            Validation result
        """
        # Resolve task reference
        try:
            resolved_task_id = resolve_task_id_for_mcp(task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"success": False, "error": f"Invalid task_id: {e}"}

        task = task_manager.get_task(resolved_task_id)
        if not task:
            return {"success": False, "error": f"Task not found: {task_id}"}

        # Check if task has children (is a parent task)
        children = task_manager.list_tasks(parent_task_id=task.id, limit=1000)

        if children:
            # Parent task: validate based on child completion
            open_children = [c for c in children if projected_task_state(c) != "closed"]
            all_closed = len(open_children) == 0

            from gobby.tasks.validation import ValidationResult

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
        else:
            # Leaf task: use LLM-based validation
            if not task_validator:
                raise RuntimeError("Task validation is not enabled")

            # Use provided changes_summary or auto-gather via smart context
            validation_context = changes_summary
            if not validation_context:
                from gobby.tasks.validation import get_validation_context_smart

                # Get project repo_path for git commands
                repo_path = None
                if get_project_repo_path and task.project_id:
                    repo_path = get_project_repo_path(task.project_id)

                smart_context = get_validation_context_smart(
                    task_title=task.title,
                    validation_criteria=task.validation_criteria,
                    task_description=task.description,
                    cwd=repo_path,
                )
                if smart_context:
                    validation_context = f"Validation context:\n\n{smart_context}"

            if not validation_context:
                raise ValueError(
                    "No changes found for validation. Either provide changes_summary "
                    "or ensure there are uncommitted changes or recent commits."
                )

            result = await task_validator.validate_task(
                task_id=task.id,
                title=task.title,
                description=task.description,
                changes_summary=validation_context,
                validation_criteria=task.validation_criteria,
                context_files=context_files,
                category=task.category,
            )

        # Record validation iteration to history
        # Calculate iteration number based on fail count (current fail count + 1 for this attempt)
        current_fail_count = task.validation_fail_count or 0
        iteration_number = current_fail_count + 1

        # Determine validator type and context type
        validator_type = "parent_completion" if children else "llm"
        context_type = "child_status" if children else "smart_context"
        context_summary = (
            f"{len(children)} children checked" if children else "Auto-gathered from git/files"
        )

        validation_history_manager.record_iteration(
            task_id=task.id,
            iteration=iteration_number,
            status=result.status,
            feedback=result.feedback,
            issues=None,  # ValidationResult from validation.py doesn't have issues
            context_type=context_type,
            context_summary=context_summary,
            validator_type=validator_type,
        )

        # Update validation status
        updates: dict[str, Any] = {
            "validation_status": result.status,
            "validation_feedback": result.feedback,
        }
        escalation_reason: str | None = None

        if result.status == "valid":
            # Success: Close task
            task_manager.close_task(task.id, reason="Completed via validation")
        elif result.status == "invalid":
            # Failure: Increment fail count
            current_fail_count = task.validation_fail_count or 0
            new_fail_count = current_fail_count + 1
            updates["validation_fail_count"] = new_fail_count

            feedback_str = result.feedback or "Validation failed (no feedback provided)."

            if new_fail_count < max_retries:
                # Create subtask to fix issues
                fix_task = task_manager.create_task(
                    project_id=task.project_id,
                    title=f"Fix validation failures for {task.title}",
                    description=f"Validation failed with feedback:\n{feedback_str}\n\nPlease fix the issues and re-validate.",
                    parent_task_id=task.id,
                    priority=1,  # High priority fix
                    task_type="bug",
                )
                updates["validation_feedback"] = (
                    feedback_str + f"\n\nCreated fix task: {fix_task.id}"
                )
            else:
                # Exceeded retries: Escalate the task
                escalation_reason = f"exceeded_validation_retries ({max_retries})"
                updates["validation_feedback"] = (
                    feedback_str
                    + f"\n\nExceeded max retries ({max_retries}). Escalated for human intervention."
                )

        task_manager.update_task(task.id, **updates)
        if escalation_reason is not None:
            task_manager.escalate_task(task.id, reason=escalation_reason)

        return {
            "success": True,
            "is_valid": result.status == "valid",
            "feedback": result.feedback,
            "status": result.status,
            "fail_count": updates.get("validation_fail_count", task.validation_fail_count),
        }

    @registry.tool(
        name="get_validation_status",
        description="Get validation details for a task.",
    )
    def get_validation_status(task_id: str) -> dict[str, Any]:
        """
        Get validation details.

        Args:
            task_id: Task reference: #N, N (seq_num), path (1.2.3), or UUID

        Returns:
            Validation details
        """
        # Resolve task reference
        try:
            resolved_task_id = resolve_task_id_for_mcp(task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"success": False, "error": f"Invalid task_id: {e}"}

        task = task_manager.get_task(resolved_task_id)
        if not task:
            return {"success": False, "error": f"Task not found: {task_id}"}

        return {
            "success": True,
            "task_id": task.id,
            "validation_status": task.validation_status,
            "validation_feedback": task.validation_feedback,
            "validation_criteria": task.validation_criteria,
            "validation_fail_count": task.validation_fail_count,
        }

    @registry.tool(
        name="reset_validation_count",
        description="Reset validation failure count for a task.",
    )
    def reset_validation_count(task_id: str) -> dict[str, Any]:
        """
        Reset validation failure count.

        Args:
            task_id: Task reference: #N, N (seq_num), path (1.2.3), or UUID

        Returns:
            Updated task details
        """
        # Resolve task reference
        try:
            resolved_task_id = resolve_task_id_for_mcp(task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"success": False, "error": f"Invalid task_id: {e}"}

        task = task_manager.get_task(resolved_task_id)
        if not task:
            return {"success": False, "error": f"Task not found: {task_id}"}

        updated_task = task_manager.update_task(task.id, validation_fail_count=0)
        return {
            "success": True,
            "task_id": updated_task.id,
            "validation_fail_count": updated_task.validation_fail_count,
            "message": "Validation failure count reset to 0",
        }

    @registry.tool(
        name="get_validation_history",
        description="Get full validation history for a task, including all iterations, feedback, and issues.",
    )
    def get_validation_history(task_id: str) -> dict[str, Any]:
        """
        Get validation history for a task.

        Returns all validation iterations with their status, feedback, and issues.

        Args:
            task_id: Task reference: #N, N (seq_num), path (1.2.3), or UUID

        Returns:
            Validation history with all iterations
        """
        # Resolve task reference
        try:
            resolved_task_id = resolve_task_id_for_mcp(task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"success": False, "error": f"Invalid task_id: {e}"}

        task = task_manager.get_task(resolved_task_id)
        if not task:
            return {"success": False, "error": f"Task {task_id} not found"}

        history = validation_history_manager.get_iteration_history(task.id)

        # Convert iterations to serializable format
        history_dicts = []
        for iteration in history:
            iter_dict: dict[str, Any] = {
                "iteration": iteration.iteration,
                "status": iteration.status,
                "feedback": iteration.feedback,
                "issues": [i.to_dict() for i in (iteration.issues or [])],
                "context_type": iteration.context_type,
                "context_summary": iteration.context_summary,
                "validator_type": iteration.validator_type,
                "created_at": iteration.created_at,
            }
            history_dicts.append(iter_dict)

        return {
            "success": True,
            "task_id": task_id,
            "history": history_dicts,
            "total_iterations": len(history_dicts),
        }

    @registry.tool(
        name="get_recurring_issues",
        description="Analyze validation history for recurring issues that keep appearing across iterations.",
    )
    def get_recurring_issues(
        task_id: str,
        threshold: int = 2,
    ) -> dict[str, Any]:
        """
        Get recurring issues analysis for a task.

        Finds issues that appear multiple times across validation iterations.

        Args:
            task_id: Task reference: #N, N (seq_num), path (1.2.3), or UUID
            threshold: Minimum occurrences to consider an issue recurring (default: 2)

        Returns:
            Recurring issues analysis with grouped issues and counts
        """
        # Resolve task reference
        try:
            resolved_task_id = resolve_task_id_for_mcp(task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"success": False, "error": f"Invalid task_id: {e}"}

        task = task_manager.get_task(resolved_task_id)
        if not task:
            return {"success": False, "error": f"Task {task_id} not found"}

        summary = validation_history_manager.get_recurring_issue_summary(
            task.id, threshold=threshold
        )

        has_recurring = validation_history_manager.has_recurring_issues(
            task.id, threshold=threshold
        )

        return {
            "success": True,
            "task_id": task.id,
            "recurring_issues": summary["recurring_issues"],
            "total_iterations": summary["total_iterations"],
            "has_recurring": has_recurring,
        }

    @registry.tool(
        name="clear_validation_history",
        description="Clear all validation history for a task. Use after major changes that invalidate previous feedback.",
    )
    def clear_validation_history(
        task_id: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """
        Clear validation history for a fresh start.

        Removes all validation iterations and resets the fail count.

        Args:
            task_id: Task reference: #N, N (seq_num), path (1.2.3), or UUID
            reason: Optional reason for clearing history

        Returns:
            Confirmation of cleared history
        """
        # Resolve task reference
        try:
            resolved_task_id = resolve_task_id_for_mcp(task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"success": False, "error": f"Invalid task_id: {e}"}

        task = task_manager.get_task(resolved_task_id)
        if not task:
            return {"success": False, "error": f"Task {task_id} not found"}

        # Get count before clearing for response
        history = validation_history_manager.get_iteration_history(task.id)
        iterations_count = len(history)

        # Clear history
        validation_history_manager.clear_history(task.id)

        # Also reset validation fail count
        task_manager.update_task(task.id, validation_fail_count=0)

        return {
            "success": True,
            "task_id": task.id,
            "cleared": True,
            "iterations_cleared": iterations_count,
            "reason": reason,
        }

    @registry.tool(
        name="de_escalate_task",
        description="Return an escalated task to its preserved current stage after human intervention resolves the issue.",
    )
    def de_escalate_task(
        task_id: str,
        reason: str,
        reset_validation: bool = False,
        reset_stage_attempts: bool = False,
    ) -> dict[str, Any]:
        """
        De-escalate a task to its preserved current stage.

        Args:
            task_id: Task reference: #N, N (seq_num), path (1.2.3), or UUID
            reason: Reason for de-escalation (required)
            reset_validation: Also reset validation fail count (default: False)
            reset_stage_attempts: Also reset current stage work attempts (default: False)

        Returns:
            Updated task details
        """
        # Resolve task reference
        try:
            resolved_task_id = resolve_task_id_for_mcp(task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"success": False, "error": f"Invalid task_id: {e}"}

        task = task_manager.get_task(resolved_task_id)
        if not task:
            return {"success": False, "error": f"Task {task_id} not found"}

        current_state = projected_task_state(task)
        if current_state != "escalated":
            return {
                "success": False,
                "error": f"Task {task_id} is not escalated (current state: {current_state})",
            }

        try:
            updated_task = task_manager.de_escalate_task(
                task.id,
                reason=reason,
                reset_validation=reset_validation,
                reset_stage_attempts=reset_stage_attempts,
            )
        except ValueError as e:
            return {"success": False, "error": str(e)}

        return {
            "success": True,
            "task_id": updated_task.id,
            "state": projected_task_state(updated_task),
            "escalated_at": updated_task.escalated_at,
            "escalation_reason": updated_task.escalation_reason,
            "de_escalation_reason": reason,
            "validation_reset": reset_validation,
            "stage_attempts_reset": reset_stage_attempts,
        }

    return registry
