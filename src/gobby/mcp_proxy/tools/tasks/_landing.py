"""Landing operation tools for gobby-tasks-ops."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
from typing import Any

from gobby.code_index.context import CodeIndexContext
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import (
    CHECKOUT_RESOLUTION_ERRORS,
    RegistryContext,
    checkout_unresolved_error,
)
from gobby.mcp_proxy.tools.tasks._escalation_coordinator import coordinate_task_escalation
from gobby.storage.hub.protocol import MainCheckoutLanding
from gobby.storage.tasks import TaskNotFoundError
from gobby.tasks.land_commit import holds_task_claim, land_candidate
from gobby.tasks.landing_policy import freeze_path, read_freeze, write_freeze
from gobby.tasks.lifecycle_access import resolve_lifecycle_task
from gobby.utils.daemon_git import GitOk, daemon_git
from gobby.utils.session_context import get_current_session_id


def _change_landing_freeze(
    ctx: RegistryContext,
    common_dir: Path,
    *,
    project_id: str,
    session_id: str,
    on: bool,
    reason: str,
    task_ref: str | None,
) -> dict[str, Any]:
    """Store a freeze change the caller may make; the caller holds the landing lock."""
    stored = read_freeze(common_dir)
    if (
        stored.on
        and stored.task_id is not None
        and not holds_task_claim(ctx.task_manager.db, stored.task_id, session_id)
    ):
        return {
            "success": False,
            "error_code": "landing_freeze_bound",
            "task_id": stored.task_id,
            "error": (
                f"The landing freeze is bound to task {stored.task_id}; "
                "only its claim holder may change it"
            ),
        }
    task_id: str | None = None
    if task_ref is not None:
        try:
            task = resolve_lifecycle_task(
                ctx.task_manager,
                task_ref,
                project_id=project_id,
                caller_session_id=ctx.resolve_session_id(session_id),
                sessions=ctx.session_manager,
            )
        except (TaskNotFoundError, ValueError) as exc:
            return {"success": False, "error": str(exc)}
        if task.claimed_by_session_id != session_id:
            return {
                "success": False,
                "error_code": "landing_freeze_claim_required",
                "error": f"Binding the landing freeze to task {task.id} requires its claim",
            }
        task_id = task.id
    freeze = write_freeze(common_dir, on=on, reason=reason, session_id=session_id, task_id=task_id)
    return {"success": True, "freeze": asdict(freeze), "path": str(freeze_path(common_dir))}


def create_landing_registry(
    ctx: RegistryContext, *, code_index: CodeIndexContext | None = None
) -> InternalToolRegistry:
    """Create landing operation tools for gobby-tasks-ops."""
    registry = InternalToolRegistry(
        name="gobby-tasks-landing",
        description="Landing operation tools",
    )

    async def set_landing_freeze(
        on: bool, reason: str, task_id: str | None = None
    ) -> dict[str, Any]:
        """Set or clear the landing freeze for the calling session's project.

        task_id (with on=true) binds the freeze to that task: only the session holding
        its claim may later change or clear it, and it refuses every other landing.
        """
        session_id = get_current_session_id()
        if not session_id:
            return {"success": False, "error": "set_landing_freeze requires a calling session"}
        reason = reason.strip()
        if on and not reason:
            return {"success": False, "error": "Setting the landing freeze requires a reason"}
        if task_id is not None and not on:
            return {"success": False, "error": "task_id binds the freeze only with on=true"}
        try:
            project_id = ctx.resolve_project_from_session(session_id)
            repo_path = ctx.get_project_repo_path(
                project_id, ctx.checkout_machine_id(project_id, session_id)
            )
        except CHECKOUT_RESOLUTION_ERRORS as exc:
            return checkout_unresolved_error(exc)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        if not repo_path:
            return {"success": False, "error": f"No checkout for project {project_id}"}

        result = await daemon_git.run(
            ("rev-parse", "--path-format=absolute", "--git-common-dir"), cwd=repo_path
        )
        if not isinstance(result, GitOk) or not result.stdout.strip():
            detail = result.stderr.strip() or result.status
            return {
                "success": False,
                "error": f"Cannot resolve the git common dir of {repo_path}: {detail}",
            }
        common_dir = Path(result.stdout.strip())
        try:
            async with ctx.task_manager.db.advisory_lock(MainCheckoutLanding(project_id)):
                outcome = await asyncio.to_thread(
                    _change_landing_freeze,
                    ctx,
                    common_dir,
                    project_id=project_id,
                    session_id=session_id,
                    on=on,
                    reason=reason,
                    task_ref=task_id,
                )
        except OSError as exc:
            return {"success": False, "error": f"Cannot write the landing freeze: {exc}"}
        return outcome

    registry.register(
        name="set_landing_freeze",
        description=(
            "Set or clear the landing freeze for the calling session's project. Setting it "
            "(on=true) requires a non-empty reason. With task_id the freeze binds to that "
            "task: its claim holder alone may change it, and only that holder may land while "
            "it is set. The state, the setting or clearing session and the time are stored "
            "at <git-common-dir>/gobby/landing-freeze.json."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "on": {"type": "boolean", "description": "true sets the freeze, false clears it"},
                "reason": {"type": "string", "description": "Why the freeze is set or cleared"},
                "task_id": {
                    "type": "string",
                    "description": (
                        "Task reference or UUID to bind the freeze to (on=true only). The caller "
                        "must hold that task's claim."
                    ),
                },
            },
            "required": ["on", "reason"],
        },
        func=set_landing_freeze,
    )

    async def land_commit(task_id: str, commit_sha: str) -> dict[str, Any]:
        """Land a linked candidate under the caller's declared landing authority."""
        session_id = get_current_session_id()
        if not session_id:
            return {"landed": False, "error": "session_required"}
        try:
            project_id = ctx.resolve_project_from_session(session_id)
            task = await asyncio.to_thread(
                resolve_lifecycle_task,
                ctx.task_manager,
                task_id,
                project_id=project_id,
                caller_session_id=ctx.resolve_session_id(session_id),
                sessions=ctx.session_manager,
            )
            result = await land_candidate(
                ctx.task_manager.db,
                task=task,
                caller_session_id=session_id,
                commit_sha=commit_sha,
                code_index=code_index,
            )
            if result.get("escalated"):
                # Triage owns the conflict now: release the claimant's session claim state.
                coordinate_task_escalation(
                    ctx,
                    ctx.task_manager.get_task(task.id),
                    prior_owner_session_id=task.claimed_by_session_id,
                    session_id=session_id,
                )
            return dict(result)
        except CHECKOUT_RESOLUTION_ERRORS as exc:
            return checkout_unresolved_error(exc)
        except (TaskNotFoundError, ValueError) as exc:
            return {"landed": False, "error": str(exc)}

    registry.register(
        name="land_commit",
        description=(
            "Land a linked full commit SHA with the land_tasks capability and declared task "
            "scope. Selected task_land rules decide required attestations and review. "
            "Each attempt records its admission snapshot; later policy or evidence changes "
            "apply to later attempts. "
            "A landing freeze refuses. Disjoint integration "
            "changes land as a merge; paths the integration tip also changed and merge "
            "conflicts escalate the task to Triage. Unlanded candidates never block."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference, <project>#N, or UUID; foreign tasks require authority",
                },
                "commit_sha": {"type": "string", "description": "Full 40-character candidate SHA"},
            },
            "required": ["task_id", "commit_sha"],
        },
        func=land_commit,
    )

    return registry
