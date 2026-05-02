"""Close task handler for task lifecycle.

Handles the close_task tool registration including validation,
commit checks, session linking, and worktree status updates.
"""

import logging
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
from gobby.mcp_proxy.tools.tasks._notifications import notify_parent_on_task_state_change
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.plans.bootstrap_ledger import BootstrapLedgerMismatchError
from gobby.storage.session_models import Session
from gobby.storage.tasks import TaskNotFoundError
from gobby.tasks.state_semantics import get_claimed_session_id

logger = logging.getLogger(__name__)


def register_close_task(registry: InternalToolRegistry, ctx: RegistryContext) -> None:
    """Register the close_task tool on the given registry."""

    async def close_task(
        task_id: str,
        reason: str = "completed",
        changes_summary: str | None = None,
        skip_validation: bool = False,
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
            changes_summary: Summary of changes made. Required for leaf/standalone tasks.
                Optional for parent/epic tasks where all children are closed.
                For completed tasks: describe what was changed and why.
                For no-work closes (duplicate, wont_fix, obsolete): explain why no changes were needed.
            skip_validation: Skip all validation checks
            override_justification: Why agent bypassed validation (stored for audit).
            commit_sha: Git commit SHA to link before closing. Convenience for link + close in one call.

        Returns:
            Closed task or error with validation feedback
        """
        from gobby.utils.session_context import get_current_session_id

        session_id = get_current_session_id()
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

        # close_task is the only task-lifecycle tool that persists a
        # *_in_session_id audit column. When the ContextVar is empty, prefer
        # the task's existing claimed_by_session_id over silently writing NULL.
        if not session_id:
            fallback_session_id = get_claimed_session_id(task)
            if fallback_session_id:
                logger.warning(
                    "close_task: no session context; falling back to task.claimed_by_session_id=%s",
                    fallback_session_id,
                )
                session_id = fallback_session_id
            else:
                return {
                    "error": "no_session_context",
                    "message": "close_task requires an active session context "
                    "or a previously-claimed task",
                }

        # Get project repo_path for git commands (needed before link_commit)
        repo_path = ctx.get_project_repo_path(task.project_id)
        cwd = repo_path or "."

        # Link commit if provided (convenience for link + close in one call)
        if commit_sha:
            try:
                task = ctx.task_manager.link_commit(resolved_id, commit_sha, cwd=cwd)
            except ValueError as e:
                return {"error": str(e)}

        # Check if this is a parent task with all children closed
        # Parent tasks (epics) are organizational containers -- no own commits needed
        children_for_parent_check = ctx.task_manager.list_tasks(parent_task_id=resolved_id, limit=1)
        is_parent_all_closed = False
        if children_for_parent_check:
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
            is_parent_all_closed = True

        # Epics are organizational containers — they never require own commits,
        # changes_summary, or session-edit checks, regardless of child count.
        is_epic = task.task_type == "epic"
        skip_leaf_checks = is_parent_all_closed or is_epic

        # Require changes_summary for non-parent closes (agents must explain what changed)
        if not skip_leaf_checks and not changes_summary:
            return {
                "success": False,
                "error": "missing_changes_summary",
                "message": "changes_summary is required when closing leaf/standalone tasks. "
                "Describe what was changed and why.",
            }

        # Resolve session_id to UUID early (needed for commit and validation checks)
        resolved_session_id = session_id
        if session_id:
            try:
                resolved_session_id = ctx.resolve_session_id(session_id)
            except ValueError as e:
                return {"error": f"Cannot resolve session '{session_id}': {e}"}

        # Resolve session for edit-awareness (used by commit checks below)
        _session: Session | None = None
        if resolved_session_id:
            try:
                _session = ctx.session_manager.get(resolved_session_id)
            except (KeyError, ValueError, TypeError) as e:
                logger.debug(f"Best-effort session lookup failed: {e}")

        # Check for linked commits (unless parent with all children closed or epic)
        # Skip the check if the session made no edits — nothing to commit
        # Tri-state: True = edits confirmed, False = no edits, None = unknown.
        # `is not False` treats unknown the same as confirmed — safer to require
        # a commit when we can't determine edit status.
        session_had_edits = _session.had_edits if _session else None
        if not skip_leaf_checks and session_had_edits is not False:
            commit_result = validate_commit_requirements(task, reason, repo_path)
            if not commit_result.can_close:
                return {
                    "success": False,
                    "error": commit_result.error_type,
                    "message": commit_result.message,
                }

        # Enforce skip_validation constraints:
        # - Cannot skip if a commit_sha is provided (you did real work, validate it)
        # - Cannot skip if the task was claimed by this session (you own it, validate it)
        # - Cannot skip without override_justification
        if skip_validation:
            if commit_sha:
                return {
                    "success": False,
                    "error": "skip_validation_with_commit",
                    "message": "Cannot skip validation when a commit_sha is provided. "
                    "If you're linking a commit, you did real work — let validation verify it.",
                }
            if not override_justification:
                return {
                    "success": False,
                    "error": "skip_validation_no_justification",
                    "message": "override_justification is required when skip_validation=True. "
                    "Explain why validation should be skipped.",
                }
            # Check if task was claimed by the calling session
            if resolved_session_id and get_claimed_session_id(task) == resolved_session_id:
                return {
                    "success": False,
                    "error": "skip_validation_own_task",
                    "message": "Cannot skip validation on a task you claimed. "
                    "You own this work — let validation verify it. "
                    "Write a detailed changes_summary instead.",
                }

        # Auto-skip validation for certain close reasons
        should_skip = skip_validation or reason.lower() in SKIP_REASONS

        # Enforce commits if session had edits
        # Only skip for explicit skip_validation, NOT for close reasons like out_of_repo
        # (if the session edited in-repo files, those need commits regardless of reason)
        # Also skip for parent tasks with all children closed (no direct edits expected)
        if not skip_leaf_checks and resolved_session_id and not skip_validation:
            # Check if task has commits (including the one being linked right now)
            has_commits = bool(task.commits) or bool(commit_sha)

            if session_had_edits is True and not has_commits:
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

        if not should_skip and not skip_leaf_checks:
            # Check if task has children (is a parent task)
            parent_result = validate_parent_task(ctx, resolved_id)
            if not parent_result.can_close:
                err_response: dict[str, Any] = {
                    "success": False,
                    "error": parent_result.error_type,
                    "message": parent_result.message,
                }
                if parent_result.extra:
                    err_response.update(parent_result.extra)
                return err_response

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
        route_to_escalation, store_override = determine_close_outcome(
            task, skip_validation, override_justification
        )

        # Record the commit that actually closes the task. If the caller passed
        # an explicit commit, prefer its normalized short SHA over current HEAD.
        from gobby.utils.git import normalize_commit_sha, run_git_command

        requires_closed_commit_sha = bool(
            commit_sha or task.commits or (not skip_leaf_checks and session_had_edits is True)
        )
        current_commit_sha: str | None = None
        if requires_closed_commit_sha:
            current_commit_sha = (
                normalize_commit_sha(commit_sha, cwd=cwd)
                if commit_sha
                else run_git_command(["git", "rev-parse", "--short", "HEAD"], cwd=cwd)
            )
            if current_commit_sha is None:
                return {
                    "success": False,
                    "error": "Could not resolve commit SHA for close - git rev-parse failed",
                }

        if route_to_escalation:
            escalation_reason = (
                "Validation override requested; human review required"
                if not override_justification
                else f"Validation override requested: {override_justification}"
            )
            ctx.task_manager.escalate_task(
                resolved_id,
                reason=escalation_reason,
                validation_override_reason=(override_justification if store_override else None),
            )

            # Auto-link session if provided
            if resolved_session_id:
                try:
                    ctx.session_task_manager.link_task(
                        resolved_session_id, resolved_id, "escalated"
                    )
                except Exception as e:
                    logger.debug(f"Best-effort session linking failed: {e}")

            notify_parent_on_task_state_change(
                ctx.task_manager.db,
                resolved_id,
                "escalated",
                task_ref=f"#{task.seq_num}" if task.seq_num else None,
            )

            return {
                "routed_to_escalation": True,
                "message": "Task escalated. Reason: validation was overridden and requires human review.",
                "task_id": resolved_id,
            }

        # All checks passed - close the task with session and commit tracking
        try:
            ctx.task_manager.close_task(
                resolved_id,
                reason=reason,
                closed_in_session_id=resolved_session_id,
                closed_commit_sha=current_commit_sha,
                validation_override_reason=override_justification if store_override else None,
            )
        except BootstrapLedgerMismatchError as exc:
            return exc.to_response()

        if is_epic and reason.lower() in {"completed", "obsolete"}:
            from gobby.hooks.event_handlers._plan import on_epic_terminal

            on_epic_terminal(
                {
                    "task_ref": f"#{task.seq_num}" if task.seq_num else resolved_id,
                    "project_id": task.project_id,
                    "status": "closed",
                    "closure_reason": reason.lower(),
                },
                db=ctx.task_manager.db,
            )

        notify_parent_on_task_state_change(
            ctx.task_manager.db,
            resolved_id,
            "closed",
            task_ref=f"#{task.seq_num}" if task.seq_num else None,
        )

        # Auto-link session if provided
        if resolved_session_id:
            try:
                ctx.session_task_manager.link_task(resolved_session_id, resolved_id, "closed")
            except Exception as e:
                logger.debug(f"Best-effort session close linking failed: {e}")

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
                    f"Removed task {resolved_id} from claimed_tasks for session {resolved_session_id}",
                )
            except Exception as e:
                logger.warning(
                    f"Failed to update claimed_tasks for session {resolved_session_id}: {e}",
                )

        # Reset had_edits after successful close with a linked commit
        # The commit accounts for this task's edits; subsequent tasks start clean
        if resolved_session_id and (bool(task.commits) or bool(commit_sha)):
            try:
                ctx.session_manager.clear_had_edits(resolved_session_id)
            except Exception as e:
                logger.debug(f"Best-effort had_edits reset failed: {e}")

        return {"success": True}

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
                    "description": "Summary of what was changed and why. Required for leaf tasks and standalone closes. Optional for parent/epic tasks where all children are closed. For tasks closed without changes (duplicate, wont_fix, etc.), describe why no changes were needed.",
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
            "required": ["task_id"],
        },
        func=close_task,
    )
