"""Close task handler for task lifecycle.

Handles the close_task tool registration including validation,
commit checks, session linking, and worktree status updates.
"""

import asyncio
import logging
from dataclasses import replace
from typing import Any, Literal

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.task_repo_paths import (
    RepoPathValidationError,
    resolve_task_repo_path,
)
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._escalation_coordinator import coordinate_task_escalation
from gobby.mcp_proxy.tools.tasks._lifecycle_close_preview import (
    CloseEvaluationReport,
    link_close_commit_shas,
    resolve_close_commit_shas,
)
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
from gobby.storage.tasks import TaskNotFoundError, TaskStaleStateError
from gobby.storage.verification_receipts import VerificationReceiptStore
from gobby.tasks.evidence_admission import admit_task_evidence
from gobby.tasks.state_semantics import get_claimed_session_id
from gobby.tasks.task_state_evidence import build_linked_diff_evidence
from gobby.tasks.verification_receipt_packet import build_verification_receipt_packet

logger = logging.getLogger(__name__)


def _repo_path_unavailable_error() -> dict[str, Any]:
    """Return the structured error used when close_task cannot safely run Git."""
    return {
        "success": False,
        "error": "task_repo_path_unavailable",
        "message": (
            "close_task requires a resolvable task repository path for commit operations. "
            "Configure the task project's repo_path or pass project_path."
        ),
    }


def _has_committable_edits(paths: set[str], cwd: str) -> bool:
    """Return True if any of the given repo-relative paths could ever be committed.

    Paths matched by .gitignore (e.g. a gitignored `wiki/` vault) can never produce
    a commit, so they must not trigger the commit-before-close requirement. A
    `git check-ignore` miss or error is treated as committable so real tracked-file
    edits never silently skip the requirement.
    """
    if not paths:
        return False

    from gobby.utils.git import is_path_gitignored

    return any(not is_path_gitignored(path, cwd) for path in sorted(paths))


def register_close_task(registry: InternalToolRegistry, ctx: RegistryContext) -> None:
    """Register the close_task tool on the given registry."""

    async def _close_task_once(
        task_id: str,
        reason: str = "completed",
        changes_summary: str | None = None,
        skip_validation: bool = False,
        override_justification: str | None = None,
        commit_sha: str | None = None,
        project_path: str | None = None,
        preview: bool = False,
        response_detail: Literal["concise", "diagnostic"] = "concise",
        evidence_receipt_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Close a task with validation.

        Structural parents close after all children have closed. Every non-epic leaf
        is validated against its explicit criteria and admissible task evidence.

        Args:
            task_id: Task reference (#N, path, or UUID)
            reason: Audited reason for closing.
            changes_summary: Summary of changes made. Required for leaf/standalone tasks.
                Optional for parent/epic tasks where all children are closed.
                For completed tasks: describe what was changed and why.
                For no-work closes (duplicate, wont_fix, obsolete): explain why no changes were needed.
            skip_validation: Accepted only for organizational closes; leaf validation
                cannot be bypassed.
            override_justification: Optional audit context for organizational closes.
            commit_sha: Git commit SHA to link before closing. Convenience for link + close in one call.
            project_path: Repository path that contains the commit. Optional; defaults to the
                task project's repository. Absolute paths are allowed when they resolve to an
                accessible task/project/worktree/clone repository directory.
            preview: Run the pass without mutating task or validation state.
            response_detail: Preview response detail level.
            evidence_receipt_ids: Receipt IDs to prioritize for detailed validation context.

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
        report = CloseEvaluationReport(
            task_id=resolved_id,
            response_detail=response_detail,
        )
        report.pass_gate("task_exists")

        def blocked(
            error: str,
            message: str,
            *,
            action: str | None = None,
            extra: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            if preview:
                return report.preview_response(
                    can_close=False,
                    error=error,
                    blocking_reasons=[message],
                    required_actions=[action or message],
                    extra=extra,
                )
            response: dict[str, Any] = {
                "success": False,
                "error": error,
                "message": message,
            }
            if extra:
                response.update(extra)
            return response

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
                return blocked(
                    "no_session_context",
                    "close_task requires an active session context or a previously-claimed task",
                    action="Claim the task from an active session, then retry close_task.",
                )
        report.pass_gate("session_context")

        # Get repo_path for git commands (needed before link_commit).
        try:
            repo_path = resolve_task_repo_path(
                task_manager=ctx.task_manager,
                project_manager=ctx.project_manager,
                task=task,
                project_path=project_path,
            )
        except RepoPathValidationError as e:
            return blocked(
                "invalid_project_path",
                str(e),
                action="Pass an accessible registered repository path as project_path.",
            )
        report.pass_gate("repository_path")

        # Check if this is a parent task with all children closed
        # Parent tasks (epics) are organizational containers -- no own commits needed
        children_for_parent_check = ctx.task_manager.list_tasks(parent_task_id=resolved_id, limit=1)
        is_parent_all_closed = False
        if children_for_parent_check:
            parent_result = validate_parent_task(ctx, resolved_id)
            if not parent_result.can_close:
                return blocked(
                    parent_result.error_type or "parent_validation_failed",
                    parent_result.message or "Parent task cannot close yet.",
                    action="Close every unresolved child task, then retry close_task.",
                    extra=parent_result.extra,
                )
            is_parent_all_closed = True
            report.pass_gate("children_closed")

        # Epics are organizational containers — they never require own commits,
        # changes_summary, or session-edit checks, regardless of child count.
        is_epic = task.task_type == "epic"
        skip_leaf_checks = is_parent_all_closed or is_epic
        if not skip_leaf_checks and not (task.validation_criteria or "").strip():
            return blocked(
                "missing_validation_criteria",
                "Non-epic tasks require explicit validation_criteria before they can close.",
                action="Update the task with observable validation criteria, then retry close_task.",
            )

        # Require changes_summary for non-parent closes (agents must explain what changed)
        if not skip_leaf_checks and not changes_summary:
            return blocked(
                "missing_changes_summary",
                "changes_summary is required when closing leaf/standalone tasks. "
                "Describe what was changed and why.",
                action="Pass changes_summary describing what changed and why.",
            )
        report.pass_gate("changes_summary")

        # Resolve session_id to UUID early (needed for commit and validation checks)
        resolved_session_id = session_id
        if session_id:
            try:
                resolved_session_id = ctx.resolve_session_id(session_id)
            except ValueError as e:
                return blocked(
                    "session_resolution_failed",
                    f"Cannot resolve session '{session_id}': {e}",
                    action="Retry from a registered project session.",
                )
        report.pass_gate("session_resolved")

        # Resolve target-task edit attribution for commit checks below.
        edit_session_id = get_claimed_session_id(task) or resolved_session_id
        session_vars: dict[str, Any] = {}
        if edit_session_id and not skip_leaf_checks:
            try:
                session_vars = ctx.session_var_manager.get_variables(edit_session_id)
            except (KeyError, ValueError, TypeError) as e:
                logger.warning(
                    "close_task failed to load owner session variables for task %s "
                    "from session %s: %s",
                    resolved_id,
                    edit_session_id,
                    e,
                )
                return blocked(
                    "session_variable_lookup_failed",
                    "close_task could not verify task edit attribution from the owning "
                    "session, so it cannot safely enforce commit requirements.",
                    action="Restore owner-session variables, then retry close_task.",
                )
        from gobby.workflows.task_claim_state import (
            target_task_has_edits,
            task_edited_file_set,
        )

        target_task_had_edits = target_task_has_edits(session_vars, resolved_id)
        if target_task_had_edits:
            if repo_path is None:
                repo_error = _repo_path_unavailable_error()
                return blocked(
                    str(repo_error["error"]),
                    str(repo_error["message"]),
                    action="Configure the task repository or pass project_path.",
                )
            edited_paths = task_edited_file_set(session_vars, resolved_id)
            target_task_had_edits = await asyncio.to_thread(
                _has_committable_edits,
                edited_paths,
                repo_path,
            )

        claim_started_at = _claimed_session_window_start(
            ctx,
            task=task,
            resolved_id=resolved_id,
            resolved_session_id=resolved_session_id,
        )
        commit_shas, commit_resolution_error = resolve_close_commit_shas(
            ctx.task_manager,
            task=task,
            task_id=resolved_id,
            claim_started_at=claim_started_at,
            commit_sha=commit_sha,
            cwd=repo_path,
            project_name=ctx.get_current_project_name(),
        )
        report.commit_shas = commit_shas
        if commit_resolution_error is not None:
            return blocked(
                str(commit_resolution_error["error"]),
                str(commit_resolution_error["message"]),
                action="Fix commit resolution, then retry close_task.",
            )
        report.pass_gate("commit_set_resolved")

        if not preview:
            task, commit_link_error = link_close_commit_shas(
                ctx.task_manager,
                task=task,
                commit_shas=commit_shas,
                cwd=repo_path,
            )
            if commit_link_error is not None:
                return commit_link_error
        evaluation_task = replace(task, commits=commit_shas or None)

        # Check for linked commits only when this target task has attributed edits.
        if not skip_leaf_checks and target_task_had_edits:
            commit_result = validate_commit_requirements(evaluation_task, reason, repo_path)
            if not commit_result.can_close:
                return blocked(
                    commit_result.error_type or "commit_validation_failed",
                    commit_result.message or "Linked commits do not satisfy close requirements.",
                    action="Commit all task edits and pass the resulting SHA to close_task.",
                )
        report.pass_gate("commit_requirements")

        if skip_validation and not skip_leaf_checks:
            return blocked(
                "validation_contract_not_skippable",
                "Non-epic task close cannot skip criterion-to-evidence validation.",
                action="Provide admissible evidence for every validation criterion.",
            )
        report.pass_gate("override_policy")

        should_skip = skip_leaf_checks
        validation_status: str | None = None
        validation_feedback: str | None = None
        close_extra: dict[str, Any] = {}
        validation_reset_reason = None

        # Enforce commits if the target task had edits. Structural parents and epics
        # do not represent direct implementation work.
        if not skip_leaf_checks and resolved_session_id:
            if target_task_had_edits and not commit_shas:
                return blocked(
                    "missing_commits_for_edits",
                    "This task has attributed edits but no commits are linked to it.",
                    action=(
                        "Commit the task edits with a task-linked message and pass commit_sha "
                        "to close_task."
                    ),
                )
        report.pass_gate("edits_committed")

        receipt_packet = None
        admission = None
        evidence = None
        if not skip_leaf_checks:
            assert resolved_session_id is not None
            evidence = gather_validation_context(
                evaluation_task,
                changes_summary,
                repo_path,
                ctx.task_manager,
            )
            receipt_store = VerificationReceiptStore(ctx.task_manager.db)
            verification_receipts = receipt_store.list_for_task(task.project_id, task.id)
            linked_diff_evidence = build_linked_diff_evidence(
                evaluation_task,
                session_id=resolved_session_id,
                validation_context=evidence.validation_context or "",
            )
            if linked_diff_evidence is not None:
                linked_diff_receipt = (
                    linked_diff_evidence.receipt
                    if preview
                    else receipt_store.upsert(linked_diff_evidence.write)
                )
                verification_receipts = [
                    receipt
                    for receipt in verification_receipts
                    if receipt.id != linked_diff_receipt.id
                ]
                verification_receipts.append(linked_diff_receipt)
            requested_receipt_ids = list(dict.fromkeys(evidence_receipt_ids or []))
            available_receipt_ids = {receipt.id for receipt in verification_receipts}
            missing_receipt_ids = [
                receipt_id
                for receipt_id in requested_receipt_ids
                if receipt_id not in available_receipt_ids
            ]
            if missing_receipt_ids:
                missing_text = ", ".join(missing_receipt_ids)
                return blocked(
                    "evidence_receipts_not_found",
                    f"Requested evidence receipts are not assigned to this task: {missing_text}",
                    action=(
                        "Inspect task and unassigned receipts, assign the intended receipt IDs, "
                        "then retry close_task."
                    ),
                )
            admission = admit_task_evidence(
                verification_receipts,
                task_id=task.id,
                validation_epoch=task.validation_epoch,
                validation_criteria=task.validation_criteria or "",
            )
            inadmissible_requested_ids = [
                receipt_id
                for receipt_id in requested_receipt_ids
                if receipt_id not in admission.evidence_ids
            ]
            if inadmissible_requested_ids:
                inadmissible_text = ", ".join(inadmissible_requested_ids)
                return blocked(
                    "evidence_receipts_not_admissible",
                    f"Requested receipts are stale, failed, pending, unknown, superseded, "
                    f"or untrusted: {inadmissible_text}",
                    action="Run or record fresh authoritative evidence, then retry close_task.",
                    extra={"evidence_admission": admission.audit_summary()},
                )
            explicit_reference_text = "\n".join(
                value
                for value in (
                    task.title,
                    task.description,
                    task.validation_criteria,
                    changes_summary,
                )
                if value
            )
            implicit_receipt_ids = [
                receipt.id
                for receipt in verification_receipts
                if receipt.id in explicit_reference_text
            ]
            unassigned_count = receipt_store.count_unassigned(
                task.project_id,
                resolved_session_id,
            )
            receipt_packet = build_verification_receipt_packet(
                admission.receipts,
                explicit_receipt_ids=list(
                    dict.fromkeys([*requested_receipt_ids, *implicit_receipt_ids])
                ),
                unassigned_count=unassigned_count,
            )
            report.set_receipt_packet(receipt_packet, unassigned_count=unassigned_count)
            if not should_skip:
                close_extra.update(
                    {
                        "evidence_completeness": receipt_packet.disclosure.to_dict(),
                        "evidence_admission": admission.audit_summary(),
                        "selected_evidence": dict(report.selected_evidence),
                    }
                )
            if receipt_packet.error:
                return blocked(
                    receipt_packet.error,
                    "High-risk verification receipts exceed the semantic evidence budget.",
                    action="Reduce pathological receipt command sizes, then retry close_task.",
                    extra={"evidence_completeness": report.evidence_completeness},
                )
            report.pass_gate("evidence_packet")

        if not should_skip and not skip_leaf_checks:
            # Check if task has children (is a parent task)
            parent_result = validate_parent_task(ctx, resolved_id)
            if not parent_result.can_close:
                return blocked(
                    parent_result.error_type or "parent_validation_failed",
                    parent_result.message or "Task dependencies prevent closure.",
                    action="Resolve every blocking child or dependency, then retry close_task.",
                    extra=parent_result.extra,
                )

            if ctx.task_validator is None:
                return blocked(
                    "validation_provider_unavailable",
                    "Criterion-to-evidence validation is required, but no task validator "
                    "is configured.",
                    action="Configure the task validator, then retry close_task.",
                )
            assert receipt_packet is not None
            assert admission is not None
            assert evidence is not None
            llm_result = await validate_leaf_task_with_llm(
                task=evaluation_task,
                task_validator=ctx.task_validator,
                validation_context=evidence.validation_context or changes_summary or "",
                ctx=ctx,
                resolved_id=resolved_id,
                validation_config=ctx.validation_config,
                file_context_text=evidence.file_context_text,
                verification_receipt_text=receipt_packet.text,
                admissible_evidence_ids=list(admission.evidence_ids),
                read_only=preview,
            )
            llm_result.extra = {
                **(llm_result.extra or {}),
                "evidence_completeness": report.evidence_completeness,
                "evidence_admission": admission.audit_summary(),
                "selected_evidence": dict(report.selected_evidence),
            }
            if not llm_result.can_close:
                if preview:
                    reasons = list((llm_result.extra or {}).get("blocking_reasons") or [])
                    if not reasons and llm_result.message:
                        reasons = [llm_result.message]
                    return report.preview_response(
                        can_close=False,
                        error=llm_result.error_type or "validation_failed",
                        blocking_reasons=reasons,
                        required_actions=reasons,
                        extra=llm_result.extra,
                    )
                response = {
                    "success": False,
                    "error": llm_result.error_type,
                    "message": llm_result.message,
                }
                if llm_result.extra:
                    response.update(llm_result.extra)
                return response
            validation_status = llm_result.validation_status
            validation_feedback = llm_result.validation_feedback
            validation_reset_reason = llm_result.reset_reason
            if llm_result.extra:
                close_extra.update(llm_result.extra)
        report.validation_status = validation_status or ("skipped" if should_skip else "valid")
        report.validation_feedback = validation_feedback
        report.pass_gate("semantic_validation")
        if preview:
            return report.preview_response(can_close=True)

        # Determine close outcome
        route_to_escalation, store_override = determine_close_outcome(
            task, skip_validation, override_justification
        )

        # Record the commit that actually closes the task. If the caller passed
        # an explicit commit, prefer its normalized short SHA over current HEAD.
        from gobby.utils.git import normalize_commit_sha, run_git_command

        requires_closed_commit_sha = bool(
            commit_shas or (not skip_leaf_checks and target_task_had_edits)
        )
        current_commit_sha: str | None = None
        if requires_closed_commit_sha:
            if repo_path is None:
                return _repo_path_unavailable_error()
            if commit_sha:
                current_commit_sha = normalize_commit_sha(commit_sha, cwd=repo_path)
            elif commit_shas:
                linked_commit_sha = commit_shas[-1]
                current_commit_sha = (
                    normalize_commit_sha(linked_commit_sha, cwd=repo_path) or linked_commit_sha
                )
            else:
                current_commit_sha = run_git_command(
                    ["git", "rev-parse", "--short", "HEAD"],
                    cwd=repo_path,
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
            escalated = ctx.task_manager.escalate_task(
                resolved_id,
                reason=escalation_reason,
                validation_override_reason=(override_justification if store_override else None),
            )
            coordinate_task_escalation(
                ctx,
                escalated,
                prior_owner_session_id=get_claimed_session_id(task),
                session_id=resolved_session_id,
            )

            return {
                "routed_to_escalation": True,
                "message": "Task escalated. Reason: validation was overridden and requires human review.",
                "task_id": resolved_id,
            }

        # Named validation-failure reset branches:
        # (a) a complete criterion-to-evidence verdict;
        # (b) an organizational parent/epic close; and
        # (c) manual de-escalation/reopen in storage.tasks._transitions.reopen_task.
        try:
            ctx.task_manager.close_task(
                resolved_id,
                reason=reason,
                closed_in_session_id=resolved_session_id,
                closed_commit_sha=current_commit_sha,
                validation_override_reason=override_justification if store_override else None,
                expected_updated_at=task.updated_at,
                reset_validation_fail_count=validation_reset_reason is not None,
                validation_status=validation_status,
                validation_feedback=validation_feedback,
            )
        except BootstrapLedgerMismatchError as exc:
            return exc.to_response()
        except TaskStaleStateError as exc:
            return {
                "success": False,
                "error": "stale_task_state",
                "message": str(exc),
                "stale_state": True,
            }

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
                logger.debug("Best-effort session close linking failed: %s", e)

        # Remove closed task from claimed_tasks dict. This is done here because
        # Claude Code's post-tool-use hook does not include the tool result, so
        # detection_helpers cannot verify close succeeded.
        remaining_task_edit_state: dict[str, Any] | None = None
        claim_state_merged = False
        if edit_session_id:
            try:
                from gobby.workflows.task_claim_state import remove_claimed_task

                fresh_session_vars = ctx.session_var_manager.get_variables(edit_session_id)
                merge_dict = remove_claimed_task(fresh_session_vars, resolved_id)
                remaining_task_edit_state = merge_dict.get("task_edited_files")
                ctx.session_var_manager.merge_variables(edit_session_id, merge_dict)
                claim_state_merged = True
                logger.debug(
                    "Removed task %s from claimed_tasks for session %s",
                    resolved_id,
                    edit_session_id,
                )
            except Exception as e:
                logger.warning(
                    "Failed to update claimed_tasks for session %s: %s", edit_session_id, e
                )

        # Reset had_edits after the last task-scoped edit set is accounted for.
        if (
            edit_session_id
            and (bool(task.commits) or bool(commit_sha))
            and claim_state_merged
            and not remaining_task_edit_state
        ):
            try:
                ctx.session_manager.clear_had_edits(edit_session_id)
            except Exception as e:
                logger.debug("Best-effort had_edits reset failed: %s", e)

        return {"success": True, "closed": True, **close_extra}

    async def close_task(
        task_id: str,
        reason: str = "completed",
        changes_summary: str | None = None,
        skip_validation: bool = False,
        override_justification: str | None = None,
        commit_sha: str | None = None,
        project_path: str | None = None,
        preview: bool = False,
        response_detail: Literal["concise", "diagnostic"] = "concise",
        evidence_receipt_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Evaluate close readiness and close ready tasks in one preview call."""
        async def run_once(*, read_only: bool) -> dict[str, Any]:
            return await _close_task_once(
                task_id=task_id,
                reason=reason,
                changes_summary=changes_summary,
                skip_validation=skip_validation,
                override_justification=override_justification,
                commit_sha=commit_sha,
                project_path=project_path,
                preview=read_only,
                response_detail=response_detail,
                evidence_receipt_ids=evidence_receipt_ids,
            )

        if not preview:
            return await run_once(read_only=False)

        preview_result = await run_once(read_only=True)
        if preview_result.get("can_close") is not True:
            return {
                **preview_result,
                "success": preview_result.get("success", True),
                "preview": True,
                "can_close": False,
                "closed": False,
            }

        close_result = await run_once(read_only=False)
        if close_result.get("closed") is not True:
            return {
                **preview_result,
                **close_result,
                "success": False,
                "preview": True,
                "can_close": False,
                "closed": False,
            }
        return {
            **preview_result,
            **close_result,
            "success": True,
            "preview": True,
            "can_close": True,
            "closed": True,
        }

    registry.register(
        name="close_task",
        description=(
            "Evaluate and conditionally close a task. Agent-driven leaf closes should call "
            "preview=true; blocked evaluations return actionable reasons, while ready tasks "
            "close in the same call. Pass commit_sha to link and close in one call: "
            "close_task(task_id, commit_sha='abc123'). Or include "
            "[<project_name>-#<task_number>] in commit message for auto-linking, "
            "e.g. [gobby-#123]. Parent tasks require all children closed. "
            "Every non-epic leaf requires explicit validation criteria, admissible current-epoch "
            "evidence, and a criterion-by-criterion validator verdict. Close reasons and "
            "skip_validation do not bypass that contract."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID",
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Audited reason for closing. The reason does not bypass the "
                        "criterion-to-evidence contract for non-epic leaves."
                    ),
                    "default": "completed",
                },
                "changes_summary": {
                    "type": "string",
                    "description": "Summary of what was changed and why. Required for leaf tasks and standalone closes. Optional for parent/epic tasks where all children are closed. For tasks closed without changes (duplicate, wont_fix, etc.), describe why no changes were needed.",
                },
                "skip_validation": {
                    "type": "boolean",
                    "description": (
                        "Organizational-close compatibility flag. Non-epic leaf validation "
                        "cannot be skipped and returns validation_contract_not_skippable."
                    ),
                    "default": False,
                },
                "override_justification": {
                    "type": "string",
                    "description": (
                        "Optional audit context for an organizational close. It cannot "
                        "override validation for a non-epic leaf."
                    ),
                    "default": None,
                },
                "commit_sha": {
                    "type": "string",
                    "description": "RECOMMENDED: Git commit SHA to link and close in one call. Use this instead of separate link_commit + close_task calls.",
                    "default": None,
                },
                "project_path": {
                    "type": "string",
                    "description": "Accessible repository/workspace directory that contains the commit. Optional; defaults to the current task project repository. Absolute paths are allowed when they resolve to a registered task/project/worktree/clone repository directory.",
                    "default": None,
                },
                "preview": {
                    "type": "boolean",
                    "description": (
                        "Evaluate and close when ready. Blocked evaluations return a concise "
                        "result with prospective commits and actionable failure details without "
                        "task, claim, counter, backoff, or validation-history mutation."
                    ),
                    "default": False,
                },
                "response_detail": {
                    "type": "string",
                    "enum": ["concise", "diagnostic"],
                    "description": (
                        "Preview response detail. 'concise' omits successful gates and evidence "
                        "diagnostics; 'diagnostic' includes the full evaluation packet."
                    ),
                    "default": "concise",
                },
                "evidence_receipt_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Task-assigned verification receipt IDs to prioritize for detailed "
                        "inspection. High-risk and completeness evidence remains mandatory."
                    ),
                    "default": None,
                },
            },
            "required": ["task_id"],
        },
        func=close_task,
    )


def _claimed_session_window_start(
    ctx: RegistryContext,
    task: Any,
    resolved_id: str,
    resolved_session_id: str | None,
) -> str | None:
    if not resolved_session_id or get_claimed_session_id(task) != resolved_session_id:
        return None

    try:
        rows = ctx.session_task_manager.get_task_sessions(resolved_id)
    except Exception as exc:
        logger.debug("Failed to load task session links for claim-window autolink: %s", exc)
        return None

    for row in rows:
        if not isinstance(row, dict):
            continue
        action = row.get("action") or row.get("session_action")
        session_id = row.get("session_id")
        if action != "claimed" or str(session_id) != resolved_session_id:
            continue
        return _format_git_since(row.get("created_at") or row.get("link_created_at"))

    return None


def _format_git_since(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        value = value.isoformat()
    text = str(value).strip()
    return text or None
