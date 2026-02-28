"""
Validation MCP tools for Gobby Task System.

Extracted from tasks.py using Strangler Fig pattern.

Exposes functionality for:
- Task validation (validate_task)
- Validation status (get_validation_status, reset_validation_count)
- Validation history (get_validation_history, get_recurring_issues, clear_validation_history)
- De-escalation (de_escalate_task)
- QA loop (validate_and_fix, run_fix_attempt)

These tools are registered with the InternalToolRegistry and accessed
via the downstream proxy pattern (call_tool, list_tools, get_tool_schema).
"""

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.tasks import LocalTaskManager, TaskNotFoundError
from gobby.tasks.validation import TaskValidator
from gobby.tasks.validation_history import ValidationHistoryManager

if TYPE_CHECKING:
    from gobby.agents.runner import AgentRunner
    from gobby.storage.projects import LocalProjectManager

logger = logging.getLogger(__name__)


def create_validation_registry(
    task_manager: LocalTaskManager,
    task_validator: TaskValidator | None = None,
    project_manager: "LocalProjectManager | None" = None,
    get_project_repo_path: Any = None,
    agent_runner: "AgentRunner | None" = None,
    max_retries: int = 3,
) -> InternalToolRegistry:
    """
    Create a validation tool registry with all validation-related tools.

    Args:
        task_manager: LocalTaskManager instance
        task_validator: TaskValidator instance (optional, enables LLM validation)
        project_manager: LocalProjectManager instance (optional)
        get_project_repo_path: Callable to get repo path from project ID (optional)
        agent_runner: AgentRunner instance (optional, enables fix agent spawning)

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
            open_children = [c for c in children if c.status != "closed"]
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
                updates["status"] = "escalated"
                updates["escalated_at"] = datetime.now(UTC).isoformat()
                updates["escalation_reason"] = f"exceeded_validation_retries ({max_retries})"
                updates["validation_feedback"] = (
                    feedback_str
                    + f"\n\nExceeded max retries ({max_retries}). Escalated for human intervention."
                )

        task_manager.update_task(task.id, **updates)

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
            "use_external_validator": task.use_external_validator,
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
        description="Return an escalated task to open status after human intervention resolves the issue.",
    )
    def de_escalate_task(
        task_id: str,
        reason: str,
        reset_validation: bool = False,
    ) -> dict[str, Any]:
        """
        De-escalate a task back to open status.

        Args:
            task_id: Task reference: #N, N (seq_num), path (1.2.3), or UUID
            reason: Reason for de-escalation (required)
            reset_validation: Also reset validation fail count (default: False)

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

        if task.status != "escalated":
            return {
                "success": False,
                "error": f"Task {task_id} is not escalated (current status: {task.status})",
            }

        # Build update kwargs
        update_kwargs: dict[str, Any] = {
            "status": "open",
            "escalated_at": None,
            "escalation_reason": None,
        }

        if reset_validation:
            update_kwargs["validation_fail_count"] = 0

        updated_task = task_manager.update_task(task.id, **update_kwargs)

        return {
            "success": True,
            "task_id": updated_task.id,
            "status": updated_task.status,
            "escalated_at": updated_task.escalated_at,
            "escalation_reason": updated_task.escalation_reason,
            "de_escalation_reason": reason,
            "validation_reset": reset_validation,
        }

    @registry.tool(
        name="run_fix_attempt",
        description="Spawn a fix agent to address validation issues. Returns when the fix attempt completes.",
    )
    async def run_fix_attempt(
        task_id: str,
        issues: list[str] | None = None,
        timeout: float = 120.0,
        max_turns: int = 10,
    ) -> dict[str, Any]:
        """
        Spawn an agent to fix validation issues for a task.

        The fix agent is given the original task context plus validation
        failure details to guide its fixes.

        Args:
            task_id: Task reference: #N, N (seq_num), path (1.2.3), or UUID
            issues: List of specific issues to fix (uses validation_feedback if not provided)
            timeout: Max time for fix attempt in seconds (default: 120)
            max_turns: Max agent turns (default: 10)

        Returns:
            Dict with success status and fix details
        """
        # Resolve task reference
        try:
            resolved_task_id = resolve_task_id_for_mcp(task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"success": False, "error": f"Invalid task_id: {e}"}

        if not agent_runner:
            return {
                "success": False,
                "error": "Agent runner not configured - cannot spawn fix agent",
            }

        task = task_manager.get_task(resolved_task_id)
        if not task:
            return {"success": False, "error": f"Task not found: {task_id}"}

        # Get issues from parameter or task validation feedback
        issues_text = ""
        if issues:
            issues_text = "\n".join(f"- {issue}" for issue in issues)
        elif task.validation_feedback:
            issues_text = task.validation_feedback
        else:
            return {
                "success": False,
                "error": "No issues provided and no validation feedback on task",
            }

        # Get repo path for context
        repo_path = None
        if get_project_repo_path and task.project_id:
            repo_path = get_project_repo_path(task.project_id)

        # Build fix prompt
        fix_prompt = f"""You are fixing validation failures for a task.

## Original Task
**Title:** {task.title}
**Description:** {task.description or "No description provided."}

## Validation Criteria
{task.validation_criteria or "No specific criteria - use task description."}

## Validation Failures
{issues_text}

## Instructions
1. Read the relevant files to understand the current state
2. Fix the issues listed above
3. Ensure all validation criteria pass after your fixes
4. Do NOT create new tasks - fix the issues directly

Focus on fixing ONLY the listed issues. Do not make unrelated changes.
"""

        try:
            from gobby.agents.runner_models import AgentConfig

            config = AgentConfig(
                prompt=fix_prompt,
                parent_session_id=None,  # No parent session for fix agent
                project_id=task.project_id,
                machine_id=None,  # Will be inferred
                source="claude",
                workflow=None,  # No workflow - direct execution
                task=None,  # Don't claim the task
                mode="headless",
                timeout=timeout,
                max_turns=max_turns,
                project_path=repo_path,
            )

            # Run the fix agent
            result = await agent_runner.run(config)

            # Record the fix attempt
            iteration = (task.validation_fail_count or 0) + 1
            validation_history_manager.record_iteration(
                task_id=task.id,
                iteration=iteration,
                status="fix_attempted",
                feedback=f"Fix agent completed with status: {result.status}",
                issues=None,
                context_type="fix_agent",
                context_summary=f"Fix attempt {iteration}",
                validator_type="fix_agent",
            )

            return {
                "success": True,
                "task_id": task_id,
                "fix_status": result.status,
                "agent_output": result.output,
                "agent_turns": result.turns_used,
            }

        except Exception as e:
            logger.exception(f"Fix attempt failed for task {task_id}")
            return {
                "success": False,
                "error": f"Fix agent failed: {e!s}",
            }

    @registry.tool(
        name="validate_and_fix",
        description="Run validation loop with automatic fix attempts. Validates, spawns fix agent if needed, re-validates.",
    )
    async def validate_and_fix(
        task_id: str,
        max_retries: int = 3,
        auto_fix: bool = True,
        fix_timeout: float = 120.0,
    ) -> dict[str, Any]:
        """
        Run validation loop with automatic fix attempts.

        1. Validate task completion
        2. If failed and auto_fix=True:
           - Spawn fix agent to address issues
           - Re-validate after fix
           - Repeat up to max_retries
        3. If still failing after retries:
           - Create fix subtask with failure details
           - Mark task status = 'failed'

        Args:
            task_id: Task reference: #N, N (seq_num), path (1.2.3), or UUID
            max_retries: Maximum fix attempts before giving up (default: 3)
            auto_fix: Whether to attempt automatic fixes (default: True)
            fix_timeout: Timeout per fix attempt in seconds (default: 120)

        Returns:
            Validation result with loop history
        """
        # Resolve task reference
        try:
            resolved_task_id = resolve_task_id_for_mcp(task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"success": False, "error": f"Invalid task_id: {e}"}

        task = task_manager.get_task(resolved_task_id)
        if not task:
            return {"success": False, "error": f"Task not found: {task_id}"}

        # Check if task has children (parent tasks use child completion validation)
        children = task_manager.list_tasks(parent_task_id=task.id, limit=1)
        if children:
            # For parent tasks, just run regular validation (no fix loop)
            result = await validate_task(task.id)
            return {
                "success": True,
                "task_id": task.id,
                "is_parent_task": True,
                "validation_result": result,
            }

        loop_history: list[dict[str, Any]] = []
        current_retry = 0

        while current_retry < max_retries:
            # Run validation
            validation_result = await validate_task(task.id)
            loop_history.append(
                {
                    "iteration": current_retry + 1,
                    "action": "validate",
                    "result": validation_result,
                }
            )

            if validation_result.get("is_valid"):
                # Success! Task is closed by validate_task
                return {
                    "success": True,
                    "task_id": task.id,
                    "is_valid": True,
                    "iterations": current_retry + 1,
                    "loop_history": loop_history,
                    "message": "Task validated successfully",
                }

            # Validation failed - attempt fix if enabled and agent runner available
            if not auto_fix:
                break

            if not agent_runner:
                loop_history.append(
                    {
                        "iteration": current_retry + 1,
                        "action": "fix_skipped",
                        "reason": "Agent runner not configured",
                    }
                )
                break

            # Spawn fix agent
            fix_result = await run_fix_attempt(
                task_id=task.id,
                timeout=fix_timeout,
            )
            loop_history.append(
                {
                    "iteration": current_retry + 1,
                    "action": "fix_attempt",
                    "result": fix_result,
                }
            )

            if not fix_result.get("success"):
                # Fix agent failed to run
                logger.warning(f"Fix attempt {current_retry + 1} failed: {fix_result.get('error')}")

            current_retry += 1

        # All retries exhausted - mark as failed
        final_feedback = f"QA loop exhausted after {current_retry} fix attempts."
        if loop_history:
            last_validation = next(
                (h for h in reversed(loop_history) if h.get("action") == "validate"),
                None,
            )
            if last_validation and last_validation.get("result", {}).get("feedback"):
                final_feedback += (
                    f"\n\nLast validation feedback:\n{last_validation['result']['feedback']}"
                )

        # Create fix subtask for manual intervention
        fix_task = task_manager.create_task(
            project_id=task.project_id,
            title=f"[Manual Fix] {task.title}",
            description=f"Automatic fix attempts failed.\n\n{final_feedback}",
            parent_task_id=task.id,
            priority=1,
            task_type="bug",
        )

        # Escalate task for human intervention
        task_manager.update_task(
            task.id,
            status="escalated",
            escalated_at=datetime.now(UTC).isoformat(),
            escalation_reason=f"exceeded_fix_attempts ({current_retry})",
            validation_feedback=final_feedback,
        )

        return {
            "success": True,
            "task_id": task.id,
            "is_valid": False,
            "iterations": current_retry,
            "loop_history": loop_history,
            "fix_subtask_id": fix_task.id,
            "message": f"Validation failed after {current_retry} fix attempts. Created fix subtask: {fix_task.id}",
        }

    return registry
