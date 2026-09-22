"""Claim task handler for task lifecycle.

Handles the claim_task tool registration including conflict detection,
session linking, and session variable management.
"""

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._authorization import has_delegated_agent_run
from gobby.mcp_proxy.tools.tasks._claim_activity import confirm_claiming_session_activity
from gobby.mcp_proxy.tools.tasks._context import (
    CHECKOUT_RESOLUTION_ERRORS,
    RegistryContext,
    checkout_unresolved_error,
)
from gobby.mcp_proxy.tools.tasks._errors import TaskToolErrorCode, task_error
from gobby.mcp_proxy.tools.tasks._lifecycle_paths import (
    _claimed_session_worktree_path,
    _lifecycle_checkout_root,
)
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.storage.task_affected_files import TaskAffectedFileManager
from gobby.storage.tasks import (
    AgentTaskClaimConflictError,
    TaskAlreadyClaimedError,
    TaskClosedError,
    TaskNotFoundError,
)
from gobby.tasks.state_semantics import get_claimed_session_id, is_task_closed
from gobby.workflows.claimed_task_extra_skills import build_claimed_task_extra_skill_state
from gobby.workflows.commit_guard import (
    DirtyEditOwnershipInspectionError,
    ForeignPathOwner,
    foreign_owned_dirty_paths,
)
from gobby.workflows.task_claim_state import (
    normalize_task_edited_path,
    task_edited_file_set_for_checkout,
)

logger = logging.getLogger(__name__)

_DECLARED_AFFECTED_FILE_SOURCES = frozenset({"manual", "expansion"})


def _declared_affected_paths(ctx: RegistryContext, task_id: str) -> set[str]:
    """Return exact, binding affected-file declarations for one task."""
    paths: set[str] = set()
    for annotation in TaskAffectedFileManager(ctx.task_manager.db).get_files(task_id):
        if annotation.annotation_source not in _DECLARED_AFFECTED_FILE_SOURCES:
            continue
        normalized = normalize_task_edited_path(annotation.file_path)
        if normalized is not None:
            paths.add(normalized)
    return paths


def _canonical_claim_scope_path(path: str, checkout_root: str) -> str | None:
    """Resolve a declared path to its checkout-relative canonical destination."""
    root = Path(checkout_root).resolve(strict=False)
    resolved = (root / path).resolve(strict=False)
    if not resolved.is_relative_to(root):
        return None
    return normalize_task_edited_path(resolved.relative_to(root).as_posix())


def _task_attribution_sessions(
    ctx: RegistryContext,
    task_id: str,
    claimed_by_session_id: str | None,
) -> set[str]:
    """Return sessions that may hold persisted attribution for ``task_id``."""
    session_ids = {claimed_by_session_id} if claimed_by_session_id else set()
    for row in ctx.session_task_manager.get_task_sessions(task_id):
        if not isinstance(row, Mapping):
            continue
        session_id = row.get("session_id")
        if isinstance(session_id, str) and session_id:
            session_ids.add(session_id)
    return session_ids


def _claim_scope_conflicts(
    ctx: RegistryContext,
    *,
    task_id: str,
    project_id: str,
    claimed_by_session_id: str | None,
    claimant_session_id: str,
) -> set[ForeignPathOwner]:
    """Find active same-checkout owners of a task's explicit claim scope."""
    declared_paths = _declared_affected_paths(ctx, task_id)
    attribution_sessions = _task_attribution_sessions(ctx, task_id, claimed_by_session_id)
    attribution_variables: list[dict[str, Any]] = []
    for session_id in attribution_sessions:
        try:
            variables = ctx.session_var_manager.get_variables(session_id)
        except KeyError:
            # A task owner can predate session variables; without a persisted
            # attribution record it must not supply guessed claim scope.
            continue
        if isinstance(variables, dict):
            attribution_variables.append(variables)
    has_persisted_attribution = any(
        task_id in checkouts
        for variables in attribution_variables
        if isinstance(checkouts := variables.get("task_edited_file_checkouts"), dict)
    )
    if not declared_paths and not has_persisted_attribution:
        return set()

    checkout_root = _lifecycle_checkout_root(
        ctx,
        session_id=claimant_session_id,
        project_id=project_id,
        overlay_path=_claimed_session_worktree_path(
            ctx,
            session_id=claimant_session_id,
            project_id=project_id,
        ),
    )
    if checkout_root is None:
        raise ValueError("Cannot resolve the claimant checkout for ownership inspection")

    candidate_paths = {
        canonical
        for path in declared_paths
        if (canonical := _canonical_claim_scope_path(path, checkout_root)) is not None
    }
    for variables in attribution_variables:
        candidate_paths.update(task_edited_file_set_for_checkout(variables, task_id, checkout_root))
    if not candidate_paths:
        return set()

    owners_by_path = foreign_owned_dirty_paths(
        ctx.task_manager.db,
        session_id=claimant_session_id,
        project_id=project_id,
        checkout_root=checkout_root,
        paths=candidate_paths,
    )
    return {
        owner
        for owners in owners_by_path.values()
        for owner in owners
        if owner.owner_task_id != task_id
    }


def _claim_scope_conflict_reason(conflicts: set[ForeignPathOwner]) -> str:
    """Format exact ownership evidence without inventing additional scope."""
    ordered = sorted(conflicts, key=lambda owner: (owner.path, owner.session_ref, owner.task_ref))
    return "\n".join(
        [
            "Task claim blocked: declared or attributed path(s) belong to another active task/session:",
            *[
                f"- {owner.path} — session {owner.session_ref}, task {owner.task_ref}"
                for owner in ordered
            ],
            "Ask the owner to commit or move the work to a worktree before claiming this task.",
        ]
    )


def register_claim_task(registry: InternalToolRegistry, ctx: RegistryContext) -> None:
    """Register the claim_task tool on the given registry."""

    def claim_task(
        task_id: str,
        force: bool = False,
    ) -> dict[str, Any]:
        """Claim a task for the current session.

        Sets the canonical owner and detects conflicts when another session
        has already claimed the task.

        Args:
            task_id: Task reference (#N, path, or UUID)
            force: Override existing claim by another session (default: False)

        Returns:
            Success payload with the resolved task id and title, or an error dict
            with conflict information.
        """
        from gobby.utils.session_context import get_current_session_id

        session_id = get_current_session_id()
        if not session_id:
            return task_error(
                "No session context available. Ensure session_id is set.",
                TaskToolErrorCode.SESSION_REQUIRED,
            )

        # Resolve task reference (supports #N, path, UUID formats)
        try:
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
        except TaskNotFoundError as e:
            return task_error(str(e), TaskToolErrorCode.TASK_NOT_FOUND)
        except ValueError as e:
            return {"error": str(e)}

        task = ctx.task_manager.get_task(resolved_id)
        if not task:
            return task_error(f"Task {task_id} not found", TaskToolErrorCode.TASK_NOT_FOUND)

        # Resolve session_id to UUID (accepts #N, N, UUID, or prefix)
        try:
            resolved_session_id = ctx.resolve_session_id(session_id)
        except ValueError as e:
            return {"error": f"Cannot resolve session '{session_id}': {e}"}

        # Block cross-project claiming
        try:
            session = ctx.session_manager.get(resolved_session_id)
        except Exception:
            session = None
        if session and task.project_id != session.project_id:
            return {
                "error": "Cannot claim a task from a different project",
                "task_project": task.project_id,
                "session_project": session.project_id,
            }

        if not confirm_claiming_session_activity(ctx, resolved_session_id, session):
            return task_error(
                "Current session could not be marked active; task was not claimed",
                TaskToolErrorCode.SESSION_INACTIVE,
                session_id=resolved_session_id,
            )

        # Check if already claimed by another session
        if is_task_closed(task):
            return task_error(
                f"Cannot claim task {resolved_id}: task is closed",
                TaskToolErrorCode.TASK_CLOSED,
            )

        current_owner = get_claimed_session_id(task)
        if current_owner == resolved_session_id:
            task_ref = f"#{task.seq_num}" if task.seq_num else resolved_id
            return {
                "success": True,
                "task_id": resolved_id,
                "title": task.title,
                "already_claimed": True,
                "message": (
                    f"Task {task_ref} is already claimed by this session. Continue by reading "
                    f'it with get_task(task_id="{task_ref}", brief=false); do not call '
                    "claim_task again."
                ),
            }

        delegated_claim = False
        if current_owner and current_owner != resolved_session_id and not force:
            delegated_claim = has_delegated_agent_run(
                ctx.task_manager.db,
                caller_session_id=resolved_session_id,
                task_id=resolved_id,
                owner_session_id=current_owner,
            )

        if (
            current_owner
            and current_owner != resolved_session_id
            and not force
            and not delegated_claim
        ):
            return task_error(
                "Task already claimed by another session",
                TaskToolErrorCode.TASK_CLAIM_CONFLICT,
                claimed_by=current_owner,
                message=(
                    f"Task is already claimed by session '{current_owner}'. "
                    "Use force=True to override."
                ),
            )

        try:
            scope_conflicts = _claim_scope_conflicts(
                ctx,
                task_id=resolved_id,
                project_id=task.project_id,
                claimed_by_session_id=current_owner,
                claimant_session_id=resolved_session_id,
            )
        except CHECKOUT_RESOLUTION_ERRORS as exc:
            return checkout_unresolved_error(exc)
        except DirtyEditOwnershipInspectionError as exc:
            return task_error(
                f"Cannot inspect claim-time path ownership: {exc}",
                TaskToolErrorCode.TASK_INVALID_STATUS,
            )
        except ValueError as exc:
            return task_error(str(exc), TaskToolErrorCode.TASK_INVALID_STATUS)

        if scope_conflicts:
            return task_error(
                _claim_scope_conflict_reason(scope_conflicts),
                TaskToolErrorCode.TASK_CLAIM_CONFLICT,
                conflicts=[
                    {
                        "path": owner.path,
                        "session": owner.session_ref,
                        "task": owner.task_ref,
                    }
                    for owner in sorted(
                        scope_conflicts,
                        key=lambda owner: (owner.path, owner.session_ref, owner.task_ref),
                    )
                ],
            )

        try:
            if delegated_claim:
                updated = ctx.task_manager.claim_task_for_agent(
                    resolved_id,
                    session_id=resolved_session_id,
                    expected_owner=current_owner,
                )
            else:
                updated = ctx.task_manager.claim_task_for_agent(
                    resolved_id,
                    session_id=resolved_session_id,
                    force=force,
                )
        except AgentTaskClaimConflictError as e:
            return task_error(
                str(e),
                TaskToolErrorCode.TASK_CLAIM_CONFLICT,
                claimed_task_id=e.claimed_task_id,
                claimed_task_ref=e.claimed_task_ref,
                message=str(e),
            )
        except TaskClosedError as e:
            return task_error(str(e), TaskToolErrorCode.TASK_CLOSED)
        except TaskAlreadyClaimedError as e:
            return task_error(
                "Task already claimed by another session",
                TaskToolErrorCode.TASK_CLAIM_CONFLICT,
                claimed_by=e.claimed_by,
                message=(
                    f"Task is already claimed by session '{e.claimed_by}'. "
                    "Use force=True to override."
                ),
            )
        except ValueError as e:
            return {"error": str(e)}

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
            current_vars = {**session_vars, **merge_dict}
            merge_dict.update(build_claimed_task_extra_skill_state(current_vars, ctx.task_manager))
            ctx.session_var_manager.merge_variables(resolved_session_id, merge_dict)
        except Exception as e:
            logger.debug("Best-effort session variable setting failed: %s", e)

        try:
            from gobby.sessions.title_lifecycle import update_title_for_claim

            update_title_for_claim(ctx.session_manager, resolved_session_id, updated)
        except Exception as e:
            logger.warning("Failed to update session title after claiming %s: %s", task_id, e)

        # The title travels with the claim so memory surfacing can query the
        # task's subject without a second read.
        return {"success": True, "task_id": resolved_id, "title": task.title}

    registry.register(
        name="claim_task",
        description="Claim a task for your session. Sets canonical ownership and detects conflicts if already claimed by another session.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID",
                },
                "force": {
                    "type": "boolean",
                    "description": "Override existing claim by another session (default: False)",
                    "default": False,
                },
            },
            "required": ["task_id"],
        },
        func=claim_task,
    )
