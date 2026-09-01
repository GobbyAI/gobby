"""Review transition MCP tools for stage manifests."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from gobby.build.coordinator import summary_allows_cross_project_coordinator
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import (
    CHECKOUT_RESOLUTION_ERRORS,
    RegistryContext,
    checkout_unresolved_error,
)
from gobby.mcp_proxy.tools.tasks._dispatch_mutex_release import (
    _current_agent_dispatch_mutex_run_id,
    _release_current_agent_dispatch_mutex,
)
from gobby.mcp_proxy.tools.tasks._dispatcher_tick import schedule_dispatcher_tick
from gobby.mcp_proxy.tools.tasks._errors import TaskToolErrorCode, task_error
from gobby.mcp_proxy.tools.tasks._escalation_coordinator import (
    clear_prior_claim_session_variables,
)
from gobby.mcp_proxy.tools.tasks._lifecycle_status import _lifecycle_value_error
from gobby.mcp_proxy.tools.tasks._notifications import notify_parent_on_task_state_change
from gobby.mcp_proxy.tools.tasks._plan_review_approval import complete_plan_review_mint
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.mcp_proxy.tools.tasks._task_scope import evaluate_task_scope
from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_evidence_models import PlanReviewEvidence, ReviewEvidenceError
from gobby.plans.review_findings import FINDING_ITEM_SCHEMA
from gobby.storage.tasks import TaskNotFoundError
from gobby.storage.tasks._stage_views import stage_state_operation_view
from gobby.tasks.state_semantics import get_claimed_session_id
from gobby.utils.session_context import get_current_session_id
from gobby.workflows.task_claim_state import task_edited_file_set

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from gobby.storage.tasks import Task


def _dispatch_run_kwargs(
    ctx: RegistryContext,
    task_id: str,
    session_id: str,
) -> dict[str, Any]:
    run_id = _current_agent_dispatch_mutex_run_id(
        ctx,
        task_id=task_id,
        session_id=session_id,
    )
    return {"dispatch_run_id": run_id} if run_id is not None else {}


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
        from gobby.utils.datetime import datetime_to_required_iso

        session = ctx.session_manager.get(session_id)
        repo_path = ctx.get_project_repo_path(project_id, session.machine_id if session else None)
        if session:
            auto_link_commits(
                task_manager=ctx.task_manager,
                task_id=task_id,
                since=datetime_to_required_iso(session.created_at),
                cwd=repo_path,
                project_id=project_id,
            )
    except Exception:
        pass  # nosec B110 # best-effort, SESSION_END is the backstop


def _relay_signoff_to_build_coordinator_sync(
    ctx: RegistryContext,
    *,
    task: Task,
    task_id: str,
    stage_name: str,
    action: str,
    from_session_id: str,
    signoff_message: str,
) -> None:
    """Persist a direct P2P signoff for the coordinator of the newest build run."""
    from gobby.storage.build_history import BuildHistoryStorage
    from gobby.storage.inter_session_messages import InterSessionMessageManager

    run = BuildHistoryStorage(ctx.task_manager.db).latest_coordinated_run_for_task(
        str(task.project_id),
        task_id,
    )
    if run is None or not run.summary:
        return
    coordinator_session_id = run.summary.get("coordinator_session_id")
    if not isinstance(coordinator_session_id, str) or not coordinator_session_id:
        return

    coordinator = ctx.session_manager.get(coordinator_session_id)
    if coordinator is None:
        return
    build_project_id = str(task.project_id)
    coordinator_project_id = getattr(coordinator, "project_id", None)
    coordinator_project_id_str = str(coordinator_project_id) if coordinator_project_id else None
    if (
        coordinator_project_id_str != build_project_id
        and not summary_allows_cross_project_coordinator(
            run.summary,
            coordinator_project_id=coordinator_project_id_str,
            build_project_id=build_project_id,
        )
    ):
        logger.warning(
            "Skipping cross-project build coordinator signoff relay",
            extra={
                "task_id": task_id,
                "build_run_id": run.id,
                "coordinator_session_id": coordinator_session_id,
                "coordinator_project_id": coordinator_project_id,
                "build_project_id": build_project_id,
            },
        )
        return

    metadata = {
        "task_id": task_id,
        "task_ref": f"#{task.seq_num}" if getattr(task, "seq_num", None) else None,
        "stage_name": stage_name,
        "action": action,
        "signoff_message": signoff_message,
        "build_run_id": run.id,
        "root_task_id": run.root_task_id,
        "from_session_id": from_session_id,
    }
    InterSessionMessageManager(ctx.task_manager.db).create_message(
        from_session=from_session_id,
        to_session=coordinator_session_id,
        content=signoff_message,
        priority="high",
        message_type="message",
        metadata_json=json.dumps(metadata, default=str, sort_keys=True),
    )


def _schedule_signoff_relay(
    ctx: RegistryContext,
    *,
    task: Task,
    task_id: str,
    stage_name: str,
    action: str,
    from_session_id: str,
    signoff_message: str,
) -> None:
    try:
        _relay_signoff_to_build_coordinator_sync(
            ctx,
            task=task,
            task_id=task_id,
            stage_name=stage_name,
            action=action,
            from_session_id=from_session_id,
            signoff_message=signoff_message,
        )
    except Exception:
        logger.warning(
            "Failed to relay review signoff to build coordinator",
            extra={
                "task_id": task_id,
                "stage_name": stage_name,
                "action": action,
                "project_id": task.project_id,
            },
            exc_info=True,
        )


def register_review_stage_tools(registry: InternalToolRegistry, ctx: RegistryContext) -> None:
    """Register stage-native review transition tools on gobby-tasks-ops."""

    def submit_for_review(
        task_id: str,
        stage_name: str,
        review_notes: str | None = None,
        scope_justification: str | None = None,
    ) -> dict[str, Any]:
        """Submit a stage for review."""
        session_or_error = _resolve_session(ctx)
        if isinstance(session_or_error, dict):
            return session_or_error
        resolved_session_id = session_or_error

        resolved_id, error = _resolve_task(ctx, task_id)
        if error is not None:
            return error
        if resolved_id is None:
            raise RuntimeError("Task resolution returned neither a task ID nor an error")

        task = ctx.task_manager.get_task(resolved_id)
        if not task:
            return task_error(f"Task {task_id} not found", TaskToolErrorCode.TASK_NOT_FOUND)
        prior_owner_session_id = get_claimed_session_id(task)
        _auto_link_session_commits(
            ctx,
            task_id=resolved_id,
            project_id=task.project_id,
            session_id=resolved_session_id,
        )
        task = ctx.task_manager.get_task(resolved_id) or task
        edit_session_id = prior_owner_session_id or resolved_session_id
        session_vars = ctx.session_var_manager.get_variables(edit_session_id)
        attributed_paths = (
            task_edited_file_set(session_vars, resolved_id)
            if isinstance(session_vars, dict)
            else set()
        )
        try:
            scope = evaluate_task_scope(
                db=ctx.task_manager.db,
                task=task,
                commit_shas=task.commits or (),
                attributed_paths=attributed_paths,
                repo_path=ctx.get_project_repo_path(
                    task.project_id,
                    ctx.checkout_machine_id(task.project_id, get_current_session_id()),
                ),
                scope_justification=scope_justification,
            )
        except CHECKOUT_RESOLUTION_ERRORS as exc:
            return checkout_unresolved_error(exc)
        except RuntimeError as exc:
            return {
                "success": False,
                "error": "task_scope_unavailable",
                "message": f"Task scope cannot be evaluated: {exc}",
            }
        if not scope.accepted:
            return {
                "success": False,
                "error": "task_scope_mismatch",
                "message": scope.justification_error,
                **scope.details(),
                "required_actions": [
                    "Pass a specific scope_justification between 20 and 1000 characters."
                ],
            }
        if scope.scope_justification:
            scope_note = f"[Task Scope Justification]\n{scope.scope_justification}"
            review_notes = (
                f"{review_notes.rstrip()}\n\n{scope_note}" if review_notes else scope_note
            )
        dispatch_kwargs = _dispatch_run_kwargs(ctx, resolved_id, resolved_session_id)
        try:
            updated = ctx.task_manager.submit_for_review(
                resolved_id,
                stage_name,
                review_notes=review_notes,
                by_session_id=resolved_session_id,
                **dispatch_kwargs,
            )
        except ValueError as e:
            return _lifecycle_value_error(str(e))
        if not updated:
            return {"error": f"Failed to submit stage {stage_name} on task {task_id} for review"}

        _release_current_agent_dispatch_mutex(
            ctx,
            task_id=resolved_id,
            session_id=resolved_session_id,
            run_id=dispatch_kwargs.get("dispatch_run_id"),
        )
        clear_prior_claim_session_variables(
            ctx,
            resolved_id,
            prior_owner_session_id,
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
        schedule_dispatcher_tick(
            ctx,
            project_id=task.project_id,
            reason="submit_for_review",
        )
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
                "scope_justification": {
                    "type": ["string", "null"],
                    "minLength": 20,
                    "maxLength": 1000,
                    "description": (
                        "Required when linked or attributed paths exceed declared Targets or "
                        "manual/expansion affected-file annotations; appended to review notes."
                    ),
                },
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
        round_number: int | None = None,
        findings: list[dict[str, object]] | None = None,
        manifest_entries: list[dict[str, object]] | None = None,
        routing_decisions: dict[str, object] | None = None,
        coverage_attestation: dict[str, object] | None = None,
        evidence_id: str | None = None,
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
        if resolved_id is None:
            raise RuntimeError("Task resolution returned neither a task ID nor an error")

        task = ctx.task_manager.get_task(resolved_id)
        if not task:
            return task_error(f"Task {task_id} not found", TaskToolErrorCode.TASK_NOT_FOUND)
        if stage_name == "planning" and not evidence_id:
            return ReviewEvidenceError(
                "missing_evidence_id",
                "planning-stage approval requires evidence_id",
            ).to_dict()
        prior_owner_session_id = get_claimed_session_id(task)
        dispatch_kwargs = _dispatch_run_kwargs(ctx, resolved_id, resolved_session_id)
        replay = False
        evidence_service: PlanReviewEvidenceService | None = None
        review_evidence: PlanReviewEvidence | None = None
        if stage_name == "planning" and evidence_id:
            evidence_service = PlanReviewEvidenceService(ctx.task_manager.db)
            try:
                review_evidence = evidence_service.get_evidence(evidence_id)
                replay = review_evidence.finalized_at is not None
            except ReviewEvidenceError:
                replay = False
        approval_kwargs: dict[str, Any] = {
            "approval_notes": approval_notes,
            "by_session_id": resolved_session_id,
            **dispatch_kwargs,
        }
        if round_number is not None:
            approval_kwargs["round_number"] = round_number
        if findings is not None:
            approval_kwargs["findings"] = findings
        if manifest_entries is not None:
            approval_kwargs["manifest_entries"] = manifest_entries
        if routing_decisions is not None:
            approval_kwargs["routing_decisions"] = routing_decisions
        if coverage_attestation is not None:
            approval_kwargs["coverage_attestation"] = coverage_attestation
        if evidence_id is not None:
            approval_kwargs["evidence_id"] = evidence_id
        try:
            updated = ctx.task_manager.approve_review(
                resolved_id,
                stage_name,
                **approval_kwargs,
            )
        except ReviewEvidenceError as e:
            return e.to_dict()
        except ValueError as e:
            return _lifecycle_value_error(str(e))
        if not updated:
            return {"error": f"Failed to approve review for stage {stage_name} on task {task_id}"}
        mint_result: dict[str, object] | None = None
        if stage_name == "planning":
            if evidence_id is None:
                raise RuntimeError("planning approval passed validation without evidence_id")
            mint_result = complete_plan_review_mint(
                ctx,
                task_id=resolved_id,
                stage=stage_name,
                evidence_id=evidence_id,
                session_id=resolved_session_id,
                replay=replay,
            )
            if replay:
                if evidence_service is None:
                    raise RuntimeError("planning replay has no evidence service")
                if review_evidence is None:
                    raise RuntimeError("planning replay has no loaded review evidence")
                response = _operation_response(ctx, resolved_id, stage_name)
                response["approval_result"] = review_evidence.approval_result
                response.update(mint_result)
                return response

        _auto_link_session_commits(
            ctx,
            task_id=resolved_id,
            project_id=task.project_id,
            session_id=resolved_session_id,
        )
        _release_current_agent_dispatch_mutex(
            ctx,
            task_id=resolved_id,
            session_id=resolved_session_id,
            run_id=dispatch_kwargs.get("dispatch_run_id"),
        )
        clear_prior_claim_session_variables(
            ctx,
            resolved_id,
            prior_owner_session_id,
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
        _schedule_signoff_relay(
            ctx,
            task=task,
            task_id=resolved_id,
            stage_name=stage_name,
            action="approve_review",
            from_session_id=resolved_session_id,
            signoff_message=verdict,
        )
        schedule_dispatcher_tick(
            ctx,
            project_id=task.project_id,
            reason="approve_review",
        )
        response = _operation_response(ctx, resolved_id, stage_name)
        if evidence_service is not None and evidence_id is not None:
            try:
                response["approval_result"] = evidence_service.get_evidence(
                    evidence_id
                ).approval_result
            except ReviewEvidenceError:
                logger.warning(
                    "Planning approval succeeded but evidence %s could not be reloaded",
                    evidence_id,
                    exc_info=True,
                )
        if mint_result is not None:
            response.update(mint_result)
        return response

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
                "round_number": {"type": ["integer", "null"], "minimum": 1},
                "findings": {
                    "type": ["array", "null"],
                    "items": {"type": "object"},
                },
                "manifest_entries": {
                    "type": ["array", "null"],
                    "items": {"type": "object"},
                },
                "routing_decisions": {"type": ["object", "null"]},
                "coverage_attestation": {"type": ["object", "null"]},
                "evidence_id": {"type": ["string", "null"]},
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
        findings: list[dict[str, object]] | None = None,
        coverage_attestation: dict[str, object] | None = None,
        evidence_id: str | None = None,
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
        if resolved_id is None:
            raise RuntimeError("Task resolution returned neither a task ID nor an error")

        task = ctx.task_manager.get_task(resolved_id)
        if not task:
            return task_error(f"Task {task_id} not found", TaskToolErrorCode.TASK_NOT_FOUND)
        prior_owner_session_id = get_claimed_session_id(task)
        dispatch_kwargs = _dispatch_run_kwargs(ctx, resolved_id, resolved_session_id)
        review_kwargs: dict[str, Any] = {
            "rejection_notes": rejection_notes,
            "round_number": round_number,
            "by_session_id": resolved_session_id,
            **dispatch_kwargs,
        }
        if findings is not None:
            review_kwargs["findings"] = findings
        if coverage_attestation is not None:
            review_kwargs["coverage_attestation"] = coverage_attestation
        if evidence_id is not None:
            review_kwargs["evidence_id"] = evidence_id
        try:
            updated = ctx.task_manager.reject_review(
                resolved_id,
                stage_name,
                **review_kwargs,
            )
        except ReviewEvidenceError as e:
            return e.to_dict()
        except ValueError as e:
            return _lifecycle_value_error(str(e))
        if not updated:
            return {"error": f"Failed to reject review for stage {stage_name} on task {task_id}"}
        _auto_link_session_commits(
            ctx,
            task_id=resolved_id,
            project_id=task.project_id,
            session_id=resolved_session_id,
        )
        _release_current_agent_dispatch_mutex(
            ctx,
            task_id=resolved_id,
            session_id=resolved_session_id,
            run_id=dispatch_kwargs.get("dispatch_run_id"),
        )
        clear_prior_claim_session_variables(
            ctx,
            resolved_id,
            prior_owner_session_id,
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
        _schedule_signoff_relay(
            ctx,
            task=task,
            task_id=resolved_id,
            stage_name=stage_name,
            action="reject_review",
            from_session_id=resolved_session_id,
            signoff_message=verdict,
        )
        schedule_dispatcher_tick(
            ctx,
            project_id=task.project_id,
            reason="reject_review",
        )
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
                "findings": {
                    "type": ["array", "null"],
                    "items": FINDING_ITEM_SCHEMA,
                },
                "coverage_attestation": {"type": ["object", "null"]},
                "evidence_id": {"type": ["string", "null"]},
                "signoff_summary": {"type": ["string", "null"]},
            },
            "required": ["task_id", "stage_name"],
        },
        output_schema={"type": "object"},
        func=reject_review,
    )

    def record_plan_enhancement(
        task_id: str,
        round_number: int,
        converged: bool,
        suggestions: list[str] | None = None,
        signoff_summary: str | None = None,
    ) -> dict[str, Any]:
        """Record a constructive plan-enhancement round on the planning stage."""
        session_or_error = _resolve_session(ctx)
        if isinstance(session_or_error, dict):
            return session_or_error
        resolved_session_id = session_or_error

        resolved_id, error = _resolve_task(ctx, task_id)
        if error is not None:
            return error
        if resolved_id is None:
            raise RuntimeError("Task resolution returned neither a task ID nor an error")

        task = ctx.task_manager.get_task(resolved_id)
        if not task:
            return task_error(f"Task {task_id} not found", TaskToolErrorCode.TASK_NOT_FOUND)
        prior_owner_session_id = get_claimed_session_id(task)
        _release_current_agent_dispatch_mutex(
            ctx,
            task_id=resolved_id,
            session_id=resolved_session_id,
        )

        try:
            updated = ctx.task_manager.record_plan_enhancement(
                resolved_id,
                round_number=round_number,
                converged=converged,
                suggestions=suggestions,
                signoff_summary=signoff_summary,
                by_session_id=resolved_session_id,
            )
        except ValueError as e:
            return _lifecycle_value_error(str(e))
        if not updated:
            return {"error": f"Failed to record plan enhancement for task {task_id}"}

        has_suggestions = bool([s for s in (suggestions or []) if s and s.strip()])
        new_state = "ready" if has_suggestions and not converged else "needs_review"

        clear_prior_claim_session_variables(
            ctx,
            resolved_id,
            prior_owner_session_id,
            action="record_plan_enhancement",
        )
        notify_parent_on_task_state_change(
            ctx.task_manager.db,
            resolved_id,
            new_state,
            task_ref=f"#{task.seq_num}" if task.seq_num else None,
        )
        try:
            ctx.session_task_manager.link_task(resolved_session_id, resolved_id, new_state)
        except Exception:
            pass  # nosec B110 # best-effort linking
        schedule_dispatcher_tick(
            ctx,
            project_id=task.project_id,
            reason="record_plan_enhancement",
        )
        return _operation_response(ctx, resolved_id, "planning")

    registry.register(
        name="record_plan_enhancement",
        description=(
            "Record a constructive plan-enhancement round for the planning stage. "
            "When suggestions exist, returns the planning stage from needs_review to "
            "ready for the planner WITHOUT incrementing the adversary review budget; "
            "when converged or empty, leaves needs_review so adversary dispatch "
            "proceeds. Persists plan_enhancement_rounds_completed and "
            "plan_enhancement_converged and folds the round's suggestions into the "
            "task description idempotently."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "round_number": {"type": "integer", "minimum": 1},
                "converged": {"type": "boolean"},
                "suggestions": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                },
                "signoff_summary": {"type": ["string", "null"]},
            },
            "required": ["task_id", "round_number", "converged"],
        },
        output_schema={"type": "object"},
        func=record_plan_enhancement,
    )
