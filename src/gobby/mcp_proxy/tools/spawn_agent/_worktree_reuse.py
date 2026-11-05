"""Helpers for preparing explicit reused worktrees before agent spawn."""

from __future__ import annotations

import asyncio
from typing import Any

from gobby.agents.isolation import (
    IsolationContext,
    SpawnConfig,
    get_isolation_handler,
    repair_isolation_environment,
)
from gobby.agents.worktree_reuse import (
    ReusedWorktreeSyncResult,
    sync_reused_worktree_to_base,
)
from gobby.storage.tasks import TaskArtifactManager
from gobby.worktrees.base_branch import select_worktree_base


def resolve_reused_worktree_id(worktree_storage: Any, project_id: str | None, ref: str) -> str:
    """Resolve an explicit ``worktree_id`` argument: an id or unique prefix, else a branch name.

    Raises:
        ValueError: If the reference is an ambiguous id prefix, or is neither an
            id nor the branch name of a worktree in the project.
    """
    try:
        return str(worktree_storage.resolve_reference(ref))
    except ValueError as id_error:
        if project_id:
            by_branch = worktree_storage.get_by_branch(project_id, ref.strip())
            if by_branch is not None:
                return str(by_branch.id)
        if str(id_error).startswith("Ambiguous"):
            raise
        raise ValueError(
            f"Worktree '{ref}' not found: it matched neither a worktree id nor a branch "
            "name in this project"
        ) from id_error


def refuse_foreign_task_binding(existing_worktree: Any, task_id: str | None) -> None:
    """Refuse a claiming spawn into a worktree bound to a different task.

    Same-task reuse stays allowed, so a crashed or ended seat can recover its tree.
    """
    bound_task_id = existing_worktree.task_id
    if bound_task_id and task_id and bound_task_id != task_id:
        raise RuntimeError(
            f"Cannot reuse worktree {existing_worktree.id}: it is bound to task "
            f"{bound_task_id}, not the spawn's task {task_id}"
        )


async def prepare_reused_worktree(
    *,
    existing_worktree: Any,
    git_manager: Any,
    worktree_storage: Any,
    spawn_config: SpawnConfig,
    main_repo_path: str,
) -> tuple[IsolationContext, Any]:
    """Prepare an explicit reused worktree for a new run, synced onto its base.

    Work in progress never refuses reuse, so a tree a live session still owns refuses
    here, as does a tree bound to a different task than the one this spawn claims.
    """
    if await asyncio.to_thread(worktree_storage.is_claimed_by_live_session, existing_worktree.id):
        raise RuntimeError(f"Cannot reuse claimed live worktree: {existing_worktree.id}")
    refuse_foreign_task_binding(existing_worktree, spawn_config.task_id)
    base_branch, base_commit_sha = await select_worktree_base(git_manager, spawn_config.base_branch)
    sync_result: ReusedWorktreeSyncResult = await sync_reused_worktree_to_base(
        git_manager=git_manager,
        worktree_path=existing_worktree.worktree_path,
        base_branch=base_branch,
        base_commit_sha=base_commit_sha,
    )
    await asyncio.to_thread(worktree_storage.update, existing_worktree.id, base_branch=base_branch)
    await repair_isolation_environment(
        main_repo_path=main_repo_path,
        isolated_path=existing_worktree.worktree_path,
        provider=spawn_config.provider,
        base_commit=sync_result.base_commit_sha or base_commit_sha,
        phase_timings_ms=spawn_config.phase_timings_ms,
    )

    extra = {
        "main_repo_path": main_repo_path,
        "reused_worktree": True,
        "base_branch": base_branch,
        **sync_result.isolation_extra(),
    }
    if sync_result.base_commit_sha:
        extra["base_commit_sha"] = sync_result.base_commit_sha
        if spawn_config.task_id is not None:
            await asyncio.to_thread(
                TaskArtifactManager(worktree_storage.db).set_artifacts_atomic,
                spawn_config.task_id,
                worktree_path=existing_worktree.worktree_path,
                worktree_id=existing_worktree.id,
                clone_path=None,
                clone_id=None,
                base_commit_sha=sync_result.base_commit_sha,
            )
    return (
        IsolationContext(
            cwd=existing_worktree.worktree_path,
            branch_name=existing_worktree.branch_name,
            worktree_id=existing_worktree.id,
            isolation_type="worktree",
            extra=extra,
        ),
        get_isolation_handler("none"),
    )


async def prepare_resumed_worktree(
    *,
    existing_worktree: Any,
    spawn_config: SpawnConfig,
    main_repo_path: str,
) -> tuple[IsolationContext, Any]:
    """Return a resumed seat to its worktree exactly as the seat left it.

    A resume continues in-flight work, so it neither requires a clean tree nor
    syncs with the base: uncommitted files and unlanded commits stay as they are.
    """
    await repair_isolation_environment(
        main_repo_path=main_repo_path,
        isolated_path=existing_worktree.worktree_path,
        provider=spawn_config.provider,
        phase_timings_ms=spawn_config.phase_timings_ms,
    )
    return (
        IsolationContext(
            cwd=existing_worktree.worktree_path,
            branch_name=existing_worktree.branch_name,
            worktree_id=existing_worktree.id,
            isolation_type="worktree",
            extra={
                "main_repo_path": main_repo_path,
                "reused_worktree": True,
                "base_branch": existing_worktree.base_branch,
            },
        ),
        get_isolation_handler("none"),
    )
