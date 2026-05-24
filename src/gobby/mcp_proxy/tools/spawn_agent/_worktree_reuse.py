"""Helpers for preparing explicit reused worktrees before agent spawn."""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from typing import Any

from gobby.agents.isolation import (
    IsolationContext,
    SpawnConfig,
    get_isolation_handler,
    repair_isolation_environment,
)
from gobby.agents.worktree_reuse import (
    ReusedWorktreeRebaseConflict,
    sync_reused_worktree_to_base,
)

logger = logging.getLogger(__name__)


async def prepare_reused_worktree(
    *,
    existing_worktree: Any,
    git_manager: Any,
    worktree_storage: Any,
    clone_manager: Any,
    clone_storage: Any,
    spawn_config: SpawnConfig,
    main_repo_path: str,
) -> tuple[IsolationContext, Any]:
    """Prepare an explicit reused worktree, falling back to a fresh retry branch on conflict."""
    try:
        sync_result = await sync_reused_worktree_to_base(
            git_manager=git_manager,
            worktree_path=existing_worktree.worktree_path,
            base_branch=spawn_config.base_branch,
        )
        await repair_isolation_environment(
            main_repo_path=main_repo_path,
            isolated_path=existing_worktree.worktree_path,
            provider=spawn_config.provider,
        )
    except ReusedWorktreeRebaseConflict as exc:
        return await _fresh_worktree_after_rebase_conflict(
            existing_worktree=existing_worktree,
            git_manager=git_manager,
            worktree_storage=worktree_storage,
            clone_manager=clone_manager,
            clone_storage=clone_storage,
            spawn_config=spawn_config,
            conflict=exc,
        )

    extra = {"main_repo_path": main_repo_path, "reused_worktree": True}
    base_commit_sha = getattr(sync_result, "base_commit_sha", None)
    if isinstance(base_commit_sha, str) and base_commit_sha:
        extra["base_commit_sha"] = base_commit_sha
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


async def _fresh_worktree_after_rebase_conflict(
    *,
    existing_worktree: Any,
    git_manager: Any,
    worktree_storage: Any,
    clone_manager: Any,
    clone_storage: Any,
    spawn_config: SpawnConfig,
    conflict: ReusedWorktreeRebaseConflict,
) -> tuple[IsolationContext, Any]:
    logger.warning(
        "Reused worktree rebase conflicted; creating fresh isolation: "
        "worktree_id=%s path=%s base_ref=%s",
        existing_worktree.id,
        existing_worktree.worktree_path,
        conflict.base_ref,
    )
    handler = get_isolation_handler(
        "worktree",
        git_manager=git_manager,
        worktree_storage=worktree_storage,
        clone_manager=clone_manager,
        clone_storage=clone_storage,
    )
    retry_config = replace(
        spawn_config,
        branch_name=f"{existing_worktree.branch_name}-retry-{uuid.uuid4().hex[:8]}",
    )
    try:
        isolation_ctx = await handler.prepare_environment(retry_config)
    except Exception as retry_error:
        try:
            await handler.cleanup_environment(retry_config)
        except Exception as cleanup_err:
            logger.warning(
                "Cleanup after fresh worktree retry failure also failed: %s",
                cleanup_err,
            )
        raise RuntimeError(
            "Failed to create fresh worktree after reused worktree rebase "
            f"conflict: {retry_error}; original conflict: {conflict}"
        ) from retry_error

    isolation_ctx.extra["reused_worktree_rebase_conflict"] = str(conflict)
    isolation_ctx.extra["reused_worktree_id"] = existing_worktree.id
    isolation_ctx.extra["reused_worktree_path"] = existing_worktree.worktree_path
    return isolation_ctx, handler
