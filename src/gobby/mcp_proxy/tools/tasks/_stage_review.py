"""Review transition MCP tools for stage manifests."""

from __future__ import annotations

from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._errors import TaskToolErrorCode, task_error
from gobby.mcp_proxy.tools.tasks._lifecycle_status import (
    _clear_prior_claim_session_variables,
    _lifecycle_value_error,
)
from gobby.mcp_proxy.tools.tasks._notifications import notify_parent_on_task_state_change
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.storage.tasks import TaskNotFoundError
from gobby.storage.tasks._stage_views import stage_state_operation_view
from gobby.tasks.state_semantics import get_claimed_session_id
from gobby.utils.session_context import get_current_session_id


def _operation_response(ctx: RegistryContext, task_id: str, stage_name: str) -> dict[str, Any]:
    stage = ctx.task_manager.stage_states.get(task_id, stage_name)
    return {
        "ok": True,
        "task_id": task_id,
        "stage": stage_state_operation_view(stage) if stage is not None else None,
    }


def _resolve_session(ctx: RegistryContext) -> str | dict[str, Any]:
    session_id = get_current_session_id()
    if not session_id:
        return task_error(
            "No session context available. Ensure session_id is set.",
            TaskToolErrorCode.SESSION_REQUIRED,
        )
    try:
        return ctx.resolve_session_id(session_id)
    except ValueError as e:
        return {"error": f"Cannot resolve session '{session_id}': {e}"}


def _resolve_task(ctx: RegistryContext, task_id: str) -> tuple[str | None, dict[str, Any] | None]:
    try:
        return resolve_task_id_for_mcp(ctx.task_manager, task_id), None
    except TaskNotFoundError as e:
        return None, task_error(str(e), TaskToolErrorCode.TASK_NOT_FOUND)
    except ValueError as e:
        return None, {"error": str(e)}


def _auto_link_session_commits(
    ctx: RegistryContext,
    *,
    task_id: str,
    project_id: str,
    session_id: str,
) -> None:
    try:
        from gobby.tasks.commits import auto_link_commits

        session = ctx.session_manager.get(session_id)
        repo_path = ctx.get_project_repo_path(project_id)
        if session:
            auto_link_commits(
                task_manager=ctx.task_manager,
                task_id=task_id,
                since=session.created_at,
                cwd=repo_path,
                project_id=project_id,
            )
    except Exception:
        pass  # nosec B110 # best-effort, SESSION_END is the backstop


def register_review_stage_tools(registry: InternalToolRegistry, ctx: RegistryContext) -> None:
    """Register stage-native review transition tools on gobby-tasks-ops."""

    def submit_for_review(
        task_id: str,
        stage_name: str,
        review_notes: str | None = None,
    ) -> dict[str, Any]:
        """Submit a stage for review."""
        session_or_error = _resolve_session(ctx)
        if isinstance(session_or_error, dict):
            return session_or_error
        resolved_session_id = session_or_error

        resolved_id, error = _resolve_task(ctx, task_id)
        if error is not None:
            return error
        assert resolved_id is not None

        task = ctx.task_manager.get_task(resolved_id)
        if not task:
            return task_error(f"Task {task_id} not found", TaskToolErrorCode.TASK_NOT_FOUND)
        prior_assignee = get_claimed_session_id(task)
        _auto_link_session_commits(
            ctx,
            task_id=resolved_id,
            project_id=task.project_id,
            session_id=resolved_session_id,
        )

        try:
            updated = ctx.task_manager.submit_for_review(
                resolved_id,
                stage_name,
                review_notes=review_notes,
                by_session_id=resolved_session_id,
            )
        except ValueError as e:
            return _lifecycle_value_error(str(e))
        if not updated:
            return {"error": f"Failed to submit stage {stage_name} on task {task_id} for review"}

        _clear_prior_claim_session_variables(
            ctx,
            resolved_id,
            prior_assignee,
            action="submit_for_review",
        )
        notify_parent_on_task_state_change(
            ctx.task_manager.db,
            resolved_id,
            "needs_review",
            task_ref=f"#{task.seq_num}" if task.seq_num else None,
        )
        try:
            ctx.session_task_manager.link_task(resolved_session_id, resolved_id, "needs_review")
        except Exception:
            pass  # nosec B110 # best-effort linking
        return _operation_response(ctx, resolved_id, stage_name)

    registry.register(
        name="submit_for_review",
        description=(
            "Submit a specific task stage for review. Transitions the stage row from "
            "in_progress to needs_review and releases ownership."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "stage_name": {"type": "string"},
                "review_notes": {"type": ["string", "null"]},
            },
            "required": ["task_id", "stage_name"],
        },
        output_schema={"type": "object"},
        func=submit_for_review,
    )

    def approve_review(
        task_id: str,
        stage_name: str,
        approval_notes: str | None = None,
        signoff_summary: str | None = None,
    ) -> dict[str, Any]:
        """Approve review on a stage."""
        session_or_error = _resolve_session(ctx)
        if isinstance(session_or_error, dict):
            return session_or_error
        resolved_session_id = session_or_error

        resolved_id, error = _resolve_task(ctx, task_id)
        if error is not None:
            return error
        assert resolved_id is not None

        task = ctx.task_manager.get_task(resolved_id)
        if not task:
            return task_error(f"Task {task_id} not found", TaskToolErrorCode.TASK_NOT_FOUND)
        prior_assignee = get_claimed_session_id(task)
        _auto_link_session_commits(
            ctx,
            task_id=resolved_id,
            project_id=task.project_id,
            session_id=resolved_session_id,
        )

        try:
            updated = ctx.task_manager.approve_review(
                resolved_id,
                stage_name,
                approval_notes=approval_notes,
                by_session_id=resolved_session_id,
            )
        except ValueError as e:
            return _lifecycle_value_error(str(e))
        if not updated:
            return {"error": f"Failed to approve review for stage {stage_name} on task {task_id}"}

        _clear_prior_claim_session_variables(
            ctx,
            resolved_id,
            prior_assignee,
            action="approve_review",
        )
        notify_parent_on_task_state_change(
            ctx.task_manager.db,
            resolved_id,
            "review_approved",
            task_ref=f"#{task.seq_num}" if task.seq_num else None,
        )
        try:
            ctx.session_task_manager.link_task(resolved_session_id, resolved_id, "review_approved")
        except Exception:
            pass  # nosec B110 # best-effort linking

        verdict = signoff_summary or (
            f"Approved #{task.seq_num}" if task.seq_num else f"Approved {task_id}"
        )
        try:
            ctx.session_var_manager.set_variable(resolved_session_id, "adversary_verdict", verdict)
        except Exception:
            pass  # nosec B110 # best-effort signoff
        return _operation_response(ctx, resolved_id, stage_name)

    registry.register(
        name="approve_review",
        description=(
            "Approve review on a specific task stage. Transitions the stage row from "
            "needs_review to review_approved and releases ownership."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "stage_name": {"type": "string"},
                "approval_notes": {"type": ["string", "null"]},
                "signoff_summary": {"type": ["string", "null"]},
            },
            "required": ["task_id", "stage_name"],
        },
        output_schema={"type": "object"},
        func=approve_review,
    )

    def reject_review(
        task_id: str,
        stage_name: str,
        rejection_notes: str | None = None,
        round_number: int | None = None,
        signoff_summary: str | None = None,
    ) -> dict[str, Any]:
        """Reject review on a stage."""
        session_or_error = _resolve_session(ctx)
        if isinstance(session_or_error, dict):
            return session_or_error
        resolved_session_id = session_or_error

        resolved_id, error = _resolve_task(ctx, task_id)
        if error is not None:
            return error
        assert resolved_id is not None

        task = ctx.task_manager.get_task(resolved_id)
        if not task:
            return task_error(f"Task {task_id} not found", TaskToolErrorCode.TASK_NOT_FOUND)
        prior_assignee = get_claimed_session_id(task)

        try:
            updated = ctx.task_manager.reject_review(
                resolved_id,
                stage_name,
                rejection_notes=rejection_notes,
                round_number=round_number,
                by_session_id=resolved_session_id,
            )
        except ValueError as e:
            return _lifecycle_value_error(str(e))
        if not updated:
            return {"error": f"Failed to reject review for stage {stage_name} on task {task_id}"}

        _clear_prior_claim_session_variables(
            ctx,
            resolved_id,
            prior_assignee,
            action="reject_review",
        )
        notify_parent_on_task_state_change(
            ctx.task_manager.db,
            resolved_id,
            "ready",
            task_ref=f"#{task.seq_num}" if task.seq_num else None,
        )
        try:
            ctx.session_task_manager.link_task(resolved_session_id, resolved_id, "review_rejected")
        except Exception:
            pass  # nosec B110 # best-effort linking

        if round_number is not None:
            stock = (
                f"Rejected #{task.seq_num} round {round_number}"
                if task.seq_num
                else f"Rejected {task_id} round {round_number}"
            )
        else:
            stock = f"Rejected #{task.seq_num}" if task.seq_num else f"Rejected {task_id}"
        verdict = signoff_summary or stock
        try:
            ctx.session_var_manager.set_variable(resolved_session_id, "adversary_verdict", verdict)
        except Exception:
            pass  # nosec B110 # best-effort signoff
        return _operation_response(ctx, resolved_id, stage_name)

    registry.register(
        name="reject_review",
        description=(
            "Reject review on a specific task stage. Returns the stage row to ready, "
            "optionally appends review findings, and can bump the planning-round label."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "stage_name": {"type": "string"},
                "rejection_notes": {"type": ["string", "null"]},
                "round_number": {"type": ["integer", "null"]},
                "signoff_summary": {"type": ["string", "null"]},
            },
            "required": ["task_id", "stage_name"],
        },
        output_schema={"type": "object"},
        func=reject_review,
    )
