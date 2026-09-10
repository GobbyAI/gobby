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


async def prepare_reused_worktree(
    *,
    existing_worktree: Any,
    git_manager: Any,
    worktree_storage: Any,
    spawn_config: SpawnConfig,
    main_repo_path: str,
) -> tuple[IsolationContext, Any]:
    """Prepare an explicit reused worktree for agent continuation."""
    sync_result: ReusedWorktreeSyncResult = await sync_reused_worktree_to_base(
        git_manager=git_manager,
        worktree_path=existing_worktree.worktree_path,
        base_branch=spawn_config.base_branch,
    )
    await repair_isolation_environment(
        main_repo_path=main_repo_path,
        isolated_path=existing_worktree.worktree_path,
        provider=spawn_config.provider,
    )

    extra = {"main_repo_path": main_repo_path, "reused_worktree": True}
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
