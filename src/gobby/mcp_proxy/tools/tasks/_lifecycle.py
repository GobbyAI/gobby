"""Lifecycle operations for task management.

Provides task lifecycle tools: close, reopen, delete, and label management.
"""

import logging
import uuid
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._helpers import SKIP_REASONS
from gobby.mcp_proxy.tools.tasks._lifecycle_validation import (
    determine_close_outcome,
    gather_validation_context,
    validate_commit_requirements,
    validate_leaf_task_with_llm,
    validate_parent_task,
)
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.storage.tasks import TaskNotFoundError
from gobby.storage.worktrees import LocalWorktreeManager

logger = logging.getLogger(__name__)


def _is_uuid(value: str) -> bool:
    """Check if a string is a valid UUID (not a ref like #123)."""
    try:
        uuid.UUID(value)
        return True
    except (ValueError, TypeError):
        return False


def create_lifecycle_registry(ctx: RegistryContext) -> InternalToolRegistry:
    """Create a registry with task lifecycle tools.

    Args:
        ctx: Shared registry context

    Returns:
        InternalToolRegistry with lifecycle tools registered
    """
    registry = InternalToolRegistry(
        name="gobby-tasks-lifecycle",
        description="Task lifecycle operations",
    )

    async def close_task(
        task_id: str,
        reason: str = "completed",
        changes_summary: str | None = None,
        skip_validation: bool = False,
        session_id: str | None = None,
        override_justification: str | None = None,
        commit_sha: str | None = None,
    ) -> dict[str, Any]:
        """Close a task with validation.

        For parent tasks: automatically checks all children are closed.
        For leaf tasks: optionally validates with LLM if changes_summary provided.

        Args:
            task_id: Task reference (#N, path, or UUID)
            reason: Reason for closing. Use "duplicate", "already_implemented", "wont_fix",
                or "obsolete" to auto-skip commit check (these imply no work was done).
            changes_summary: Summary of changes made. Required for all close reasons.
                For completed tasks: describe what was changed and why.
                For no-work closes (duplicate, wont_fix, obsolete): explain why no changes were needed.
            skip_validation: Skip all validation checks
            session_id: Session ID where task is being closed (auto-links to session)
            override_justification: Why agent bypassed validation (stored for audit).
            commit_sha: Git commit SHA to link before closing. Convenience for link + close in one call.

        Returns:
            Closed task or error with validation feedback
        """
        # Resolve task reference (supports #N, path, UUID formats)
        try:
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
        except TaskNotFoundError as e:
            return {"error": str(e)}
        except ValueError as e:
            return {"error": str(e)}

        task = ctx.task_manager.get_task(resolved_id)
        if not task:
            return {"error": f"Task {task_id} not found"}

        # Link commit if provided (convenience for link + close in one call)
        if commit_sha:
            task = ctx.task_manager.link_commit(resolved_id, commit_sha)

        # Get project repo_path for git commands
        repo_path = ctx.get_project_repo_path(task.project_id)
        cwd = repo_path or "."

        # Check for linked commits (unless task type doesn't require commits)
        commit_result = validate_commit_requirements(task, reason, repo_path)
        if not commit_result.can_close:
            return {
                "success": False,
                "error": commit_result.error_type,
                "message": commit_result.message,
            }

        # Auto-skip validation for certain close reasons
        should_skip = skip_validation or reason.lower() in SKIP_REASONS

        # Resolve session_id to UUID (accepts #N, N, UUID, or prefix)
        resolved_session_id = session_id
        if session_id:
            try:
                resolved_session_id = ctx.resolve_session_id(session_id)
            except ValueError:
                pass  # Fall back to raw value if resolution fails

        # Enforce commits if session had edits
        # Only skip for explicit skip_validation, NOT for close reasons like out_of_repo
        # (if the session edited in-repo files, those need commits regardless of reason)
        if resolved_session_id and not skip_validation:
            try:
                from gobby.storage.sessions import LocalSessionManager

                session_manager = LocalSessionManager(ctx.task_manager.db)
                session = session_manager.get(resolved_session_id)

                # Check if task has commits (including the one being linked right now)
                has_commits = bool(task.commits) or bool(commit_sha)

                if session and session.had_edits and not has_commits:
                    return {
                        "success": False,
                        "error": "missing_commits_for_edits",
                        "message": (
                            "This session made edits but no commits are linked to the task. "
                            "You must commit your changes and link them to the task before closing."
                        ),
                        "suggestion": (
                            f"Commit your changes with `[{ctx.get_current_project_name() or 'project'}-#task_id]` in the message, "
                            "or pass `commit_sha` to `close_task`."
                        ),
                    }
            except Exception as e:
                # Don't block close on internal error
                logger.debug("Best-effort session edit check failed: %s", e)

        if not should_skip:
            # Check if task has children (is a parent task)
            parent_result = validate_parent_task(ctx, resolved_id)
            if not parent_result.can_close:
                response: dict[str, Any] = {
                    "success": False,
                    "error": parent_result.error_type,
                    "message": parent_result.message,
                }
                if parent_result.extra:
                    response.update(parent_result.extra)
                return response

            # Check for leaf task with validation criteria
            children = ctx.task_manager.list_tasks(parent_task_id=resolved_id, limit=1)
            is_leaf = len(children) == 0

            if is_leaf and ctx.task_validator and task.validation_criteria:
                # Gather validation context
                validation_context, raw_diff = gather_validation_context(
                    task, changes_summary, repo_path, ctx.task_manager
                )

                if validation_context:
                    # Run LLM validation
                    llm_result = await validate_leaf_task_with_llm(
                        task=task,
                        task_validator=ctx.task_validator,
                        validation_context=validation_context,
                        raw_diff=raw_diff,
                        ctx=ctx,
                        resolved_id=resolved_id,
                        validation_config=ctx.validation_config,
                    )
                    if not llm_result.can_close:
                        response = {
                            "success": False,
                            "error": llm_result.error_type,
                            "message": llm_result.message,
                        }
                        if llm_result.extra:
                            response.update(llm_result.extra)
                        return response

        # Determine close outcome
        route_to_review, store_override = determine_close_outcome(
            task, skip_validation, override_justification
        )

        # Get git commit SHA (best-effort, dynamic short format for consistency)
        from gobby.utils.git import run_git_command

        current_commit_sha = run_git_command(["git", "rev-parse", "--short", "HEAD"], cwd=cwd)

        if route_to_review:
            # Route to needs_review status instead of closing
            # Task stays in needs_review until user explicitly closes
            ctx.task_manager.update_task(
                resolved_id,
                status="needs_review",
                validation_override_reason=override_justification if store_override else None,
            )

            # Auto-link session if provided
            if resolved_session_id:
                try:
                    ctx.session_task_manager.link_task(
                        resolved_session_id, resolved_id, "needs_review"
                    )
                except Exception as e:
                    logger.debug("Best-effort session linking failed: %s", e)

            return {
                "routed_to_review": True,
                "message": (
                    "Task routed to review status. "
                    + (
                        "Reason: requires user review before closing."
                        if task.requires_user_review
                        else "Reason: validation was overridden, human review recommended."
                    )
                ),
                "task_id": resolved_id,
            }

        # All checks passed - close the task with session and commit tracking
        ctx.task_manager.close_task(
            resolved_id,
            reason=reason,
            closed_in_session_id=resolved_session_id,
            closed_commit_sha=current_commit_sha,
            validation_override_reason=override_justification if store_override else None,
        )

        # Auto-link session if provided
        if resolved_session_id:
            try:
                ctx.session_task_manager.link_task(resolved_session_id, resolved_id, "closed")
            except Exception as e:
                logger.debug("Best-effort session close linking failed: %s", e)

        # Remove closed task from claimed_tasks dict
        # This is done here because Claude Code's post-tool-use hook doesn't include
        # the tool result, so the detection_helpers can't verify close succeeded
        if resolved_session_id:
            try:
                from gobby.workflows.task_claim_state import remove_claimed_task

                session_vars = ctx.session_var_manager.get_variables(resolved_session_id)
                merge_dict = remove_claimed_task(session_vars, resolved_id)
                ctx.session_var_manager.merge_variables(resolved_session_id, merge_dict)
                logger.debug(
                    "Removed task %s from claimed_tasks for session %s",
                    resolved_id,
                    resolved_session_id,
                )
            except Exception as e:
                logger.warning(
                    "Failed to update claimed_tasks for session %s: %s",
                    resolved_session_id,
                    e,
                )

        # Reset had_edits after successful close with a linked commit
        # The commit accounts for this task's edits; subsequent tasks start clean
        if resolved_session_id and (bool(task.commits) or bool(commit_sha)):
            try:
                from gobby.storage.sessions import LocalSessionManager

                session_manager = LocalSessionManager(ctx.task_manager.db)
                session_manager.clear_had_edits(resolved_session_id)
            except Exception as e:
                logger.debug("Best-effort had_edits reset failed: %s", e)

        # Update worktree status based on closure reason (case-insensitive)
        try:
            reason_normalized = reason.lower()
            worktree_manager = LocalWorktreeManager(ctx.task_manager.db)
            wt = worktree_manager.get_by_task(resolved_id)
            if wt:
                if reason_normalized in (
                    "wont_fix",
                    "obsolete",
                    "duplicate",
                    "already_implemented",
                ):
                    worktree_manager.mark_abandoned(wt.id)
                elif reason_normalized == "completed":
                    worktree_manager.mark_merged(wt.id)
        except Exception as e:
            logger.debug("Best-effort worktree update failed during close: %s", e)

        return {}

    registry.register(
        name="close_task",
        description="Close a task. Pass commit_sha to link and close in one call: close_task(task_id, commit_sha='abc123'). Or include [project-#N] in commit message for auto-linking. Parent tasks require all children closed. Validation auto-skipped for: duplicate, already_implemented, wont_fix, obsolete, out_of_repo. Note: out_of_repo only skips LLM validation and the basic commit-linked check; commits are still required if the session edited in-repo files (session.had_edits enforcement).",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID",
                },
                "reason": {
                    "type": "string",
                    "description": 'Reason for closing. Use "duplicate", "already_implemented", "wont_fix", or "obsolete" to auto-skip validation and commit check. "out_of_repo" skips validation only; commits are still required if the session edited in-repo files.',
                    "default": "completed",
                },
                "changes_summary": {
                    "type": "string",
                    "description": "Required: summary of what was changed and why. For tasks closed without changes (duplicate, wont_fix, etc.), describe why no changes were needed.",
                },
                "skip_validation": {
                    "type": "boolean",
                    "description": (
                        "Skip LLM validation even when task has validation_criteria. "
                        "USE THIS when: validation fails due to truncated diff, validator misses context, "
                        "or you've manually verified completion. Provide override_justification explaining why."
                    ),
                    "default": False,
                },
                "session_id": {
                    "type": "string",
                    "description": "Your session ID (accepts #N, N, UUID, or prefix). Pass this to track which session closed the task.",
                    "default": None,
                },
                "override_justification": {
                    "type": "string",
                    "description": (
                        "Justification for bypassing validation. Required when skip_validation=True. "
                        "Example: 'Validation saw truncated diff - verified via git show that commit includes all changes'"
                    ),
                    "default": None,
                },
                "commit_sha": {
                    "type": "string",
                    "description": "RECOMMENDED: Git commit SHA to link and close in one call. Use this instead of separate link_commit + close_task calls.",
                    "default": None,
                },
            },
            "required": ["task_id", "changes_summary"],
        },
        func=close_task,
    )

    def reopen_task(task_id: str, reason: str | None = None) -> dict[str, Any]:
        """Reopen a task to open status.

        Works on any non-open status. Clears assignee, closed fields,
        and resets validation_fail_count.

        Args:
            task_id: Task reference (#N, path, or UUID)
            reason: Optional reason for reopening
        """
        try:
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"error": str(e)}

        try:
            ctx.task_manager.reopen_task(resolved_id, reason=reason)

            # Reactivate any associated worktrees that were marked merged/abandoned
            try:
                from gobby.storage.worktrees import WorktreeStatus

                worktree_manager = LocalWorktreeManager(ctx.task_manager.db)
                wt = worktree_manager.get_by_task(resolved_id)
                if wt and wt.status in (
                    WorktreeStatus.MERGED.value,
                    WorktreeStatus.ABANDONED.value,
                ):
                    worktree_manager.update(wt.id, status=WorktreeStatus.ACTIVE.value)
            except Exception as e:
                logger.debug("Best-effort reopen worktree update failed: %s", e)

            return {}
        except ValueError as e:
            return {"error": str(e)}

    registry.register(
        name="reopen_task",
        description="Reopen a task to open status. Works on any non-open status. Clears assignee, closed fields, and resets validation. Optionally appends a reopen reason to the description.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference to reopen: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional reason for reopening the task",
                    "default": None,
                },
            },
            "required": ["task_id"],
        },
        func=reopen_task,
    )

    def delete_task(task_id: str, cascade: bool = True, unlink: bool = False) -> dict[str, Any]:
        """Delete a task.

        By default (cascade=True), deletes subtasks and dependent tasks.
        Use unlink=True to remove dependency links but preserve dependent tasks.
        """
        try:
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"error": str(e)}

        # Get task before deleting to capture seq_num for ref
        task = ctx.task_manager.get_task(resolved_id)
        if not task:
            return {"error": f"Task {task_id} not found"}
        ref = f"#{task.seq_num}" if task.seq_num else resolved_id

        try:
            deleted = ctx.task_manager.delete_task(resolved_id, cascade=cascade, unlink=unlink)
            if not deleted:
                return {"error": f"Task {task_id} not found"}
        except ValueError as e:
            error_msg = str(e)
            if "dependent task(s)" in error_msg:
                return {
                    "success": False,
                    "error": "has_dependents",
                    "message": error_msg,
                    "suggestion": f"Use cascade=True to delete task {ref} and its dependents, "
                    f"or unlink=True to preserve dependent tasks.",
                }
            elif "has children" in error_msg:
                return {
                    "success": False,
                    "error": "has_children",
                    "message": error_msg,
                    "suggestion": f"Use cascade=True to delete task {ref} and all its subtasks.",
                }
            return {"error": error_msg}

        return {
            "ref": ref,
            "deleted_task_id": resolved_id,  # UUID at end
        }

    registry.register(
        name="delete_task",
        description="Delete a task. By default (cascade=True), deletes subtasks and dependent tasks. "
        "Set cascade=False to fail if task has children or dependents. "
        "Use unlink=True to remove dependency links but preserve dependent tasks.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID",
                },
                "cascade": {
                    "type": "boolean",
                    "description": "If True, delete subtasks and dependent tasks. Defaults to True.",
                    "default": True,
                },
                "unlink": {
                    "type": "boolean",
                    "description": "If True, remove dependency links but preserve dependent tasks. "
                    "Ignored if cascade=True.",
                    "default": False,
                },
            },
            "required": ["task_id"],
        },
        func=delete_task,
    )

    def add_label(task_id: str, label: str) -> dict[str, Any]:
        """Add a label to a task."""
        try:
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"error": str(e)}
        task = ctx.task_manager.add_label(resolved_id, label)
        if not task:
            return {"error": f"Task {task_id} not found"}
        return {}

    registry.register(
        name="add_label",
        description="Add a label to a task.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID",
                },
                "label": {"type": "string", "description": "Label to add"},
            },
            "required": ["task_id", "label"],
        },
        func=add_label,
    )

    def remove_label(task_id: str, label: str) -> dict[str, Any]:
        """Remove a label from a task."""
        try:
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"error": str(e)}
        task = ctx.task_manager.remove_label(resolved_id, label)
        if not task:
            return {"error": f"Task {task_id} not found"}
        return {}

    registry.register(
        name="remove_label",
        description="Remove a label from a task.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID",
                },
                "label": {"type": "string", "description": "Label to remove"},
            },
            "required": ["task_id", "label"],
        },
        func=remove_label,
    )

    def claim_task(
        task_id: str,
        session_id: str,
        force: bool = False,
    ) -> dict[str, Any]:
        """Claim a task for the current session.

        Combines setting the assignee and marking as in_progress in a single
        atomic operation. Detects conflicts when another session has already
        claimed the task.

        Args:
            task_id: Task reference (#N, path, or UUID)
            session_id: Session ID claiming the task
            force: Override existing claim by another session (default: False)

        Returns:
            Empty dict on success, or error dict with conflict information.
        """
        # Resolve task reference (supports #N, path, UUID formats)
        try:
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
        except TaskNotFoundError as e:
            return {"error": str(e)}
        except ValueError as e:
            return {"error": str(e)}

        task = ctx.task_manager.get_task(resolved_id)
        if not task:
            return {"error": f"Task {task_id} not found"}

        # Resolve session_id to UUID (accepts #N, N, UUID, or prefix)
        resolved_session_id = session_id
        try:
            resolved_session_id = ctx.resolve_session_id(session_id)
        except ValueError:
            pass  # Fall back to raw value if resolution fails

        # Check if already claimed by another session
        if task.assignee and task.assignee != resolved_session_id and not force:
            return {
                "success": False,
                "error": "Task already claimed by another session",
                "claimed_by": task.assignee,
                "message": f"Task is already claimed by session '{task.assignee}'. Use force=True to override.",
            }

        # Update task with assignee and status in single atomic call
        updated = ctx.task_manager.update_task(
            resolved_id,
            assignee=resolved_session_id,
            status="in_progress",
        )
        if not updated:
            return {"error": f"Failed to claim task {task_id}"}

        # Link task to session (best-effort, don't fail the claim if this fails)
        try:
            ctx.session_task_manager.link_task(resolved_session_id, resolved_id, "claimed")
        except Exception as e:
            logger.debug("Best-effort session claim linking failed: %s", e)

        # Set claimed_tasks session variable (enables Edit/Write hooks)
        # This mirrors create_task behavior in _crud.py
        try:
            from gobby.workflows.task_claim_state import add_claimed_task

            session_vars = ctx.session_var_manager.get_variables(resolved_session_id)
            ref = f"#{task.seq_num}" if task.seq_num else resolved_id
            merge_dict = add_claimed_task(session_vars, resolved_id, ref)
            ctx.session_var_manager.merge_variables(resolved_session_id, merge_dict)
        except Exception as e:
            logger.debug("Best-effort session variable setting failed: %s", e)

        return {}

    registry.register(
        name="claim_task",
        description="Claim a task for your session. Sets assignee to session_id and status to in_progress. Detects conflicts if already claimed by another session.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID",
                },
                "session_id": {
                    "type": "string",
                    "description": "Your session ID (accepts #N, N, UUID, or prefix). The session claiming the task.",
                },
                "force": {
                    "type": "boolean",
                    "description": "Override existing claim by another session (default: False)",
                    "default": False,
                },
            },
            "required": ["task_id", "session_id"],
        },
        func=claim_task,
    )

    def escalate_task(
        task_id: str,
        reason: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Escalate a task for human intervention.

        Sets status to 'escalated' with a reason and timestamp. Use when
        the task cannot be completed by the agent and needs human attention.

        Args:
            task_id: Task reference (#N, path, or UUID)
            reason: Why the task is being escalated
            session_id: Optional session ID for tracking

        Returns:
            Empty dict on success, or error dict with details.
        """
        from datetime import UTC, datetime

        try:
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"error": str(e)}

        task = ctx.task_manager.get_task(resolved_id)
        if not task:
            return {"error": f"Task {task_id} not found"}

        if task.status in ("escalated", "closed"):
            return {"error": f"Cannot escalate task with status '{task.status}'."}

        ctx.task_manager.update_task(
            resolved_id,
            status="escalated",
            escalated_at=datetime.now(UTC).isoformat(),
            escalation_reason=reason,
        )

        # Link task to session (best-effort)
        if session_id:
            resolved_session_id = session_id
            try:
                resolved_session_id = ctx.resolve_session_id(session_id)
            except ValueError:
                pass
            try:
                ctx.session_task_manager.link_task(resolved_session_id, resolved_id, "escalated")
            except Exception as e:
                logger.debug("Best-effort escalation linking failed: %s", e)

        return {}

    registry.register(
        name="escalate_task",
        description="Escalate a task for human intervention. Sets status to 'escalated'. Use when the task cannot be completed and needs human attention.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID",
                },
                "reason": {
                    "type": "string",
                    "description": "Why the task is being escalated (e.g., 'blocked by external dependency', 'needs architectural decision')",
                },
                "session_id": {
                    "type": "string",
                    "description": "Your session ID (accepts #N, N, UUID, or prefix). Optional.",
                    "default": None,
                },
            },
            "required": ["task_id", "reason"],
        },
        func=escalate_task,
    )

    def mark_task_review_approved(
        task_id: str,
        session_id: str,
        approval_notes: str | None = None,
    ) -> dict[str, Any]:
        """Approve a task after review.

        Sets status to 'review_approved', indicating the review gate has passed.
        Accepts tasks in 'needs_review', 'in_progress', or 'escalated' status.

        Args:
            task_id: Task reference (#N, path, or UUID)
            session_id: Session ID approving the task
            approval_notes: Optional notes about the approval

        Returns:
            Empty dict on success, or error dict with details.
        """
        try:
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
        except TaskNotFoundError as e:
            return {"error": str(e)}
        except ValueError as e:
            return {"error": str(e)}

        task = ctx.task_manager.get_task(resolved_id)
        if not task:
            return {"error": f"Task {task_id} not found"}

        # Validate: current status must be needs_review, in_progress, or escalated
        if task.status not in ("needs_review", "in_progress", "escalated"):
            return {
                "error": f"Cannot approve task with status '{task.status}'. "
                "Task must be in 'needs_review', 'in_progress', or 'escalated' status to approve."
            }

        # Resolve session_id
        resolved_session_id = session_id
        try:
            resolved_session_id = ctx.resolve_session_id(session_id)
        except ValueError:
            pass

        # Build update kwargs
        update_kwargs: dict[str, Any] = {"status": "review_approved"}

        # Append approval notes to description if provided
        if approval_notes:
            current_desc = task.description or ""
            approval_section = f"\n\n[Approval Notes]\n{approval_notes}"
            update_kwargs["description"] = current_desc + approval_section

        # Update task status to review_approved
        updated = ctx.task_manager.update_task(resolved_id, **update_kwargs)
        if not updated:
            return {"error": f"Failed to approve task {task_id}"}

        # Link task to session (best-effort)
        try:
            ctx.session_task_manager.link_task(resolved_session_id, resolved_id, "review_approved")
        except Exception:
            pass  # nosec B110 - best-effort linking

        return {}

    registry.register(
        name="mark_task_review_approved",
        description="Approve a task after review. Sets status to 'review_approved' (review gate passed). Accepts tasks in 'needs_review', 'in_progress', or 'escalated' status.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID",
                },
                "session_id": {
                    "type": "string",
                    "description": "Your session ID (accepts #N, N, UUID, or prefix). The session approving the task.",
                },
                "approval_notes": {
                    "type": "string",
                    "description": "Optional notes about the approval.",
                    "default": None,
                },
            },
            "required": ["task_id", "session_id"],
        },
        func=mark_task_review_approved,
    )

    def mark_task_needs_review(
        task_id: str,
        session_id: str,
        review_notes: str | None = None,
    ) -> dict[str, Any]:
        """Mark a task as ready for review.

        Sets status to 'needs_review'. Use this when work is complete
        but needs human verification before closing.

        Args:
            task_id: Task reference (#N, path, or UUID)
            session_id: Session ID marking the task for review
            review_notes: Optional notes for the reviewer

        Returns:
            Empty dict on success, or error dict with details.
        """
        # Resolve task reference (supports #N, path, UUID formats)
        try:
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
        except TaskNotFoundError as e:
            return {"error": str(e)}
        except ValueError as e:
            return {"error": str(e)}

        task = ctx.task_manager.get_task(resolved_id)
        if not task:
            return {"error": f"Task {task_id} not found"}

        # Resolve session_id to UUID (accepts #N, N, UUID, or prefix)
        resolved_session_id = session_id
        try:
            resolved_session_id = ctx.resolve_session_id(session_id)
        except ValueError:
            pass  # Fall back to raw value if resolution fails

        # Build update kwargs
        update_kwargs: dict[str, Any] = {"status": "needs_review"}

        # Append review notes to description if provided
        if review_notes:
            current_desc = task.description or ""
            review_section = f"\n\n[Review Notes]\n{review_notes}"
            update_kwargs["description"] = current_desc + review_section

        # Update task status to needs_review
        updated = ctx.task_manager.update_task(resolved_id, **update_kwargs)
        if not updated:
            return {"error": f"Failed to mark task {task_id} for review"}

        # Link task to session (best-effort, don't fail if this fails)
        try:
            ctx.session_task_manager.link_task(resolved_session_id, resolved_id, "needs_review")
        except Exception:
            pass  # nosec B110 - best-effort linking

        return {}

    registry.register(
        name="mark_task_needs_review",
        description="Mark a task as ready for review. Sets status to 'needs_review'. Use this when work is complete but needs human verification before closing.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID",
                },
                "session_id": {
                    "type": "string",
                    "description": "Your session ID (accepts #N, N, UUID, or prefix). The session marking the task for review.",
                },
                "review_notes": {
                    "type": "string",
                    "description": "Optional notes for the reviewer explaining what was done and what to verify.",
                    "default": None,
                },
            },
            "required": ["task_id", "session_id"],
        },
        func=mark_task_needs_review,
    )

    return registry
