"""Task-close preconditions and the shared close-gate evaluation."""

from __future__ import annotations

import asyncio
from typing import Literal

from gobby.mcp_proxy.tools.task_repo_paths import (
    RepoPathValidationError,
    resolve_close_worktree_root_async,
    resolve_task_repo_path,
)
from gobby.mcp_proxy.tools.tasks._close_context import (
    CloseAttributionLookupError,
    capture_attribution,
    children,
    step_requirements,
)
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_close_preview import CloseEvaluation
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.sessions.machine_scope import (
    RemoteSessionOwnershipError,
    require_local_session_ownership,
)
from gobby.storage.project_checkouts import CheckoutNotFoundError
from gobby.storage.tasks import TaskNotFoundError
from gobby.tasks.close_invariants import evaluate_close_invariants
from gobby.tasks.land_commit import LandingGitError, main_checkout_target
from gobby.tasks.state_semantics import get_claimed_session_id, is_task_closed
from gobby.utils.daemon_git import GitOk, daemon_git
from gobby.utils.session_context import get_current_session_id


async def _evaluate_close(
    ctx: RegistryContext,
    *,
    task_id: str,
    reason: str,
    commit_sha: str | None,
    project_path: str | None,
    response_detail: Literal["concise", "diagnostic"] = "concise",
    closing_session_id: str | None = None,
    superseded_by: str | None = None,
) -> CloseEvaluation:
    evaluation = CloseEvaluation(task_id, response_detail=response_detail)
    try:
        resolved_id = await asyncio.to_thread(resolve_task_id_for_mcp, ctx.task_manager, task_id)
        task = await asyncio.to_thread(ctx.task_manager.get_task, resolved_id)
    except (TaskNotFoundError, ValueError) as exc:
        return evaluation.refuse("task_not_found", str(exc))
    if task is None:
        return evaluation.refuse("task_not_found", f"Task {task_id} not found.")
    evaluation.task, evaluation.task_id = task, resolved_id
    evaluation.is_epic = task.task_type == "epic"
    session_ref = closing_session_id or get_current_session_id() or get_claimed_session_id(task)
    if not session_ref:
        return evaluation.refuse("no_session_context", "close_task requires an active session.")
    try:
        session_id = await asyncio.to_thread(ctx.resolve_session_id, session_ref)
    except ValueError as exc:
        return evaluation.refuse("session_resolution_failed", str(exc))
    evaluation.resolved_session_id = session_id
    evaluation.edit_session_id = get_claimed_session_id(task) or session_id
    session = await asyncio.to_thread(ctx.session_manager.get, session_id)
    if session is None or not session.machine_id:
        return evaluation.refuse("session_machine_missing", "The close session needs a machine_id.")
    try:
        require_local_session_ownership(session)
    except RemoteSessionOwnershipError as exc:
        return evaluation.refuse("remote_session_ownership", str(exc))
    if is_task_closed(task):
        evaluation.already_closed = True
        evaluation.commit_shas = list(task.commits or [])
        evaluation.candidate_commit_sha = task.closed_commit_sha
        return evaluation

    try:
        repo = await asyncio.to_thread(
            resolve_task_repo_path,
            task_manager=ctx.task_manager,
            project_manager=ctx.project_manager,
            task=task,
            project_path=project_path,
            machine_id=session.machine_id,
        )
        if project_path is None:
            root = await resolve_close_worktree_root_async(
                task_manager=ctx.task_manager,
                task=task,
                commit_shas=[*(task.commits or []), *([commit_sha] if commit_sha else [])],
            )
            repo = root.repo_path or repo
    except (CheckoutNotFoundError, RepoPathValidationError, ValueError) as exc:
        return evaluation.refuse("invalid_project_path", str(exc))
    if repo is None:
        return evaluation.refuse(
            "task_repo_path_unavailable", "Register the task repository first."
        )
    evaluation.repo_path = repo
    commits = list(dict.fromkeys(task.commits or []))
    if commit_sha is not None:
        if not commit_sha.strip():
            return evaluation.refuse("invalid_commit_sha", "commit_sha must not be empty.")
        resolved = await daemon_git.run(
            ("rev-parse", "--verify", "--end-of-options", f"{commit_sha}^{{commit}}"),
            cwd=repo,
        )
        if not isinstance(resolved, GitOk):
            return evaluation.refuse("invalid_commit_sha", "commit_sha must resolve to a commit.")
        candidate = resolved.stdout.strip()
        if not any(candidate.startswith(sha) or sha.startswith(candidate) for sha in commits):
            commits.append(candidate)
        evaluation.candidate_commit_sha = candidate
    elif commits:
        resolved = await daemon_git.run(
            ("rev-parse", "--verify", "--end-of-options", f"{commits[-1]}^{{commit}}"), cwd=repo
        )
        if isinstance(resolved, GitOk):
            evaluation.candidate_commit_sha = resolved.stdout.strip()
    evaluation.commit_shas = commits
    try:
        attribution = await capture_attribution(
            ctx,
            task=task,
            resolved_session_id=session_id,
            repo_path=repo,
            prospective_commit_shas=commits,
        )
    except CloseAttributionLookupError as exc:
        return evaluation.refuse("session_variable_lookup_failed", str(exc))
    child_tasks = await asyncio.to_thread(children, ctx, task.id)
    try:
        target = (
            await main_checkout_target(ctx.task_manager.db, task.project_id) if commits else None
        )
    except (LandingGitError, CheckoutNotFoundError, ValueError):
        target = None
    integration_ref = f"refs/heads/{target[1]}" if target else None
    try:
        steps = await step_requirements(ctx, task, commits)
    except ValueError as exc:
        return evaluation.refuse("step_records_unavailable", str(exc))
    evaluation.checklist = await evaluate_close_invariants(
        open_children=[child.id for child in child_tasks if not is_task_closed(child)],
        commit_shas=commits,
        reason=reason,
        attributed_checkouts=attribution.clean_checkouts,
        integration_repo_path=str(target[0]) if target else None,
        integration_ref=integration_ref,
        required_steps=steps.required,
        missing_steps=steps.missing,
        attribution_complete=attribution.complete,
        superseded_by=superseded_by,
    )
    return evaluation
