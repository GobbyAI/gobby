"""Authenticated task-close preview using the same policy as atomic storage close."""

from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import JsonValue

from gobby.mcp_proxy.tools.task_repo_paths import (
    RepoPathValidationError,
    resolve_close_worktree_root_async,
    resolve_task_repo_path,
)
from gobby.mcp_proxy.tools.tasks._close_context import (
    CloseAttributionLookupError,
    capture_attribution,
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
from gobby.tasks.close_receipts import list_close_receipts
from gobby.tasks.closure_snapshot import prepare_closure
from gobby.tasks.land_commit import LandingGitError, candidate_scope_paths, task_landing_target
from gobby.tasks.lifecycle_access import task_protection_refusal, task_scope_refusal
from gobby.tasks.state_semantics import (
    get_claimed_session_id,
    is_task_closed,
    validate_task_close_reason,
)
from gobby.utils.daemon_git import GitOk, daemon_git
from gobby.utils.git import descends_from_all, normalize_commit_sha
from gobby.utils.session_context import get_current_session_id
from gobby.workflows.agent_resolver import session_agent_definition
from gobby.workflows.task_dirty_state import task_dirty_paths_async


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
        validate_task_close_reason(reason)
    except ValueError as exc:
        return evaluation.refuse("invalid_close_reason", str(exc))
    try:
        resolved_id = await asyncio.to_thread(resolve_task_id_for_mcp, ctx.task_manager, task_id)
        task = await asyncio.to_thread(ctx.task_manager.get_task, resolved_id)
    except (TaskNotFoundError, ValueError) as exc:
        return evaluation.refuse("task_not_found", str(exc))
    if task is None:
        return evaluation.refuse("task_not_found", f"Task {task_id} not found.")
    evaluation.task, evaluation.task_id = task, resolved_id
    evaluation.is_epic = task.task_type == "epic"
    session_ref = closing_session_id or get_current_session_id()
    if not session_ref:
        return evaluation.refuse("no_session_context", "close_task requires an active session.")
    try:
        session_id = await asyncio.to_thread(ctx.resolve_session_id, session_ref)
        session = await asyncio.to_thread(ctx.session_manager.get, session_id)
        if session is None or session.project_id != task.project_id:
            return evaluation.refuse("invalid_session", "Close requires a same-project session.")
        resolved_definition = await asyncio.to_thread(
            session_agent_definition, ctx.task_manager.db, session
        )
    except ValueError as exc:
        return evaluation.refuse("session_resolution_failed", str(exc))
    definition = resolved_definition[0] if resolved_definition else None
    evaluation.resolved_session_id = session_id
    evaluation.edit_session_id = get_claimed_session_id(task) or session_id
    if is_task_closed(task):
        evaluation.already_closed = True
        evaluation.commit_shas = list(task.commits or [])
        evaluation.candidate_commit_sha = task.closed_commit_sha
        return evaluation
    if definition is not None:
        if refusal := task_scope_refusal(
            task, session_id, definition.task_close_scope, db=ctx.task_manager.db
        ):
            return evaluation.refuse("task_close_scope_forbidden", refusal)
        if definition.respect_task_protections:
            try:
                protected = await asyncio.to_thread(
                    task_protection_refusal, ctx.task_manager, task, session_id
                )
            except (OSError, ValueError) as exc:
                return evaluation.refuse("task_protection_policy_unavailable", str(exc))
            if protected:
                return evaluation.refuse("task_protected", protected)

    # Repository state is an optional observation. YAML decides whether it is required.
    repo = None
    try:
        repo = await asyncio.to_thread(
            resolve_task_repo_path,
            task_manager=ctx.task_manager,
            project_manager=ctx.project_manager,
            task=task,
            project_path=project_path,
            machine_id=session.machine_id,
        )
        if project_path is None and (task.commits or commit_sha):
            root = await resolve_close_worktree_root_async(
                task_manager=ctx.task_manager,
                task=task,
                commit_shas=[*(task.commits or []), *([commit_sha] if commit_sha else [])],
            )
            repo = root.repo_path or repo
    except (CheckoutNotFoundError, RepoPathValidationError, ValueError) as exc:
        if project_path is not None or commit_sha is not None:
            return evaluation.refuse("invalid_project_path", str(exc))
    if repo is not None:
        try:
            require_local_session_ownership(session)
        except RemoteSessionOwnershipError as exc:
            return evaluation.refuse("remote_session_ownership", str(exc))
    evaluation.repo_path = repo
    commits = list(dict.fromkeys(task.commits or []))
    if commit_sha is not None:
        if not commit_sha.strip() or repo is None:
            return evaluation.refuse(
                "invalid_commit_sha", "A commit link needs a valid repository and SHA."
            )
        resolved = await daemon_git.run(
            ("rev-parse", "--verify", "--end-of-options", f"{commit_sha}^{{commit}}"),
            cwd=repo,
        )
        if not isinstance(resolved, GitOk):
            return evaluation.refuse("invalid_commit_sha", "commit_sha must resolve to a commit.")
        candidate = resolved.stdout.strip()
        canonical = await asyncio.to_thread(normalize_commit_sha, candidate, cwd=repo)
        if canonical is None:
            return evaluation.refuse("invalid_commit_sha", "commit_sha must resolve to a commit.")
        named = [sha for sha in commits if candidate.startswith(sha) or sha.startswith(candidate)]
        others = [sha for sha in commits if sha not in named]
        if not named:
            commits.append(canonical)
        elif commits[-1] not in named and await asyncio.to_thread(
            descends_from_all, candidate, others, repo
        ):
            commits = [*others, *named]
        evaluation.candidate_commit_sha = candidate
    elif commits and repo is not None:
        resolved = await daemon_git.run(
            ("rev-parse", "--verify", "--end-of-options", f"{commits[-1]}^{{commit}}"),
            cwd=repo,
        )
        if isinstance(resolved, GitOk):
            evaluation.candidate_commit_sha = resolved.stdout.strip()
    evaluation.commit_shas = commits
    evaluation.facts = {
        "closure_intent": "explicit",
        "superseded_by": superseded_by,
        "repository_available": repo is not None,
        "commit_shas": list(commits),
        "candidate_commit_sha": evaluation.candidate_commit_sha,
        "candidate_covers_linked_commits": False,
        "attribution_complete": False,
        "task_files_clean": False,
        "dirty_checkouts": {},
        "unavailable_checkouts": [],
        "integration_ancestry": False,
        "candidate_paths": [],
        "candidate_paths_available": False,
    }
    try:
        attribution = await capture_attribution(
            ctx,
            task=task,
            resolved_session_id=session_id,
            repo_path=repo,
            prospective_commit_shas=commits,
            machine_id=session.machine_id,
        )
        if attribution.clean_checkouts:
            try:
                require_local_session_ownership(session)
            except RemoteSessionOwnershipError as exc:
                return evaluation.refuse("remote_session_ownership", str(exc))
        clean = attribution.complete
        dirty_checkouts: dict[str, JsonValue] = {}
        unavailable_checkouts: list[JsonValue] = []
        for checkout, paths in attribution.clean_checkouts.items():
            dirty = await task_dirty_paths_async(set(paths), checkout)
            if dirty is None:
                unavailable_checkouts.append(checkout)
                clean = False
            elif dirty:
                dirty_checkouts[checkout] = list[JsonValue](sorted(dirty))
                clean = False
        evaluation.facts["attribution_complete"] = attribution.complete
        evaluation.facts["task_files_clean"] = clean
        evaluation.facts["dirty_checkouts"] = dirty_checkouts
        evaluation.facts["unavailable_checkouts"] = unavailable_checkouts
    except CloseAttributionLookupError:
        pass  # Unknown facts remain false; policy chooses whether to require them.
    if repo is not None:
        if evaluation.candidate_commit_sha is not None and commits:
            evaluation.facts["candidate_covers_linked_commits"] = await asyncio.to_thread(
                descends_from_all, evaluation.candidate_commit_sha, commits, repo
            )
        try:
            target = await task_landing_target(ctx.task_manager.db, task) if commits else None
        except (LandingGitError, CheckoutNotFoundError, ValueError):
            target = None
        landed = bool(target and commits)
        if target:
            if evaluation.candidate_commit_sha is not None:
                tip = await daemon_git.run(("rev-parse", f"refs/heads/{target[1]}"), cwd=target[0])
                if isinstance(tip, GitOk):
                    try:
                        candidate_paths = await candidate_scope_paths(
                            target[0],
                            tip.stdout.strip(),
                            evaluation.candidate_commit_sha,
                            list_close_receipts(ctx.task_manager.db, task.id),
                        )
                    except LandingGitError:
                        candidate_paths = None
                    evaluation.facts["candidate_paths"] = list(candidate_paths or [])
                    evaluation.facts["candidate_paths_available"] = candidate_paths is not None
            for sha in commits:
                result = await daemon_git.run(
                    ("merge-base", "--is-ancestor", sha, f"refs/heads/{target[1]}"),
                    cwd=target[0],
                )
                if not isinstance(result, GitOk):
                    landed = False
        evaluation.facts["integration_ancestry"] = landed
    try:
        prepared = await asyncio.to_thread(
            prepare_closure,
            ctx.task_manager.db,
            task_id=resolved_id,
            session_id=session_id,
            reason=reason,
            facts=evaluation.facts,
            expected_updated_at=task.updated_at,
            prospective_commits=commits if commit_sha is not None else None,
        )
    except ValueError as exc:
        return evaluation.refuse("closure_snapshot_unavailable", str(exc))
    evaluation.decision = prepared.decision
    evaluation.facts = prepared.snapshot.facts
    return evaluation
