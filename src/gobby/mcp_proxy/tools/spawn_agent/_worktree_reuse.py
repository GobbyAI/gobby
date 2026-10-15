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


async def prepare_reused_worktree(
    *,
    existing_worktree: Any,
    git_manager: Any,
    worktree_storage: Any,
    spawn_config: SpawnConfig,
    main_repo_path: str,
) -> tuple[IsolationContext, Any]:
    """Prepare an explicit reused worktree for a new run, synced onto its base."""
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
        phase_timings_ms=spawn_config.phase_timings_ms,
    )

    extra = {
        "main_repo_path": main_repo_path,
        "reused_worktree": True,
        "base_branch": base_branch,
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
    rebases onto the base: uncommitted files and unlanded commits stay as they are.
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
