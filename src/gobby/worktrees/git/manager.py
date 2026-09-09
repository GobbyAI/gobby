"""Public `WorktreeGitManager` facade."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from gobby.worktrees.git import _branch, _lifecycle, _locking, _merge, _status
from gobby.worktrees.git._models import GitOperationResult, WorktreeInfo, WorktreeStatus
from gobby.worktrees.git._runner import GitRunner


class WorktreeGitManager(GitRunner):
    """
    Manager for git worktree operations.

    Provides methods to create, delete, and manage git worktrees.
    All operations are performed relative to a base repository path.
    """

    async def create_worktree(
        self,
        worktree_path: str | Path,
        branch_name: str,
        base_branch: str = "main",
        create_branch: bool = True,
        use_local: bool = False,
    ) -> GitOperationResult:
        return await _lifecycle.create_worktree(
            self,
            worktree_path,
            branch_name,
            base_branch=base_branch,
            create_branch=create_branch,
            use_local=use_local,
        )

    async def delete_worktree(
        self,
        worktree_path: str | Path,
        force: bool = False,
        delete_branch: bool = False,
        force_delete_branch: bool = False,
        branch_name: str | None = None,
        base_branch: str | None = None,
        merged_into: str | None = None,
    ) -> GitOperationResult:
        return await _lifecycle.delete_worktree(
            self,
            worktree_path,
            force=force,
            delete_branch=delete_branch,
            force_delete_branch=force_delete_branch,
            branch_name=branch_name,
            base_branch=base_branch,
            merged_into=merged_into,
        )

    async def sync_from_main(
        self,
        worktree_path: str | Path,
        base_branch: str = "main",
        strategy: Literal["rebase", "merge"] = "rebase",
        source_branch: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> GitOperationResult:
        return await _lifecycle.sync_from_main(
            self,
            worktree_path,
            base_branch=base_branch,
            strategy=strategy,
            source_branch=source_branch,
            env=env,
        )

    async def get_worktree_status(
        self,
        worktree_path: str | Path,
        comparison_ref: str | None = None,
    ) -> WorktreeStatus | None:
        return await _status.get_worktree_status(self, worktree_path, comparison_ref)

    async def list_worktrees(self) -> list[WorktreeInfo]:
        return await _status.list_worktrees(self)

    async def inspect_worktree(self, worktree_path: str | Path) -> WorktreeInfo:
        return await _lifecycle.inspect_linked_worktree(self, worktree_path)

    async def prune_worktrees(self) -> GitOperationResult:
        return await _status.prune_worktrees(self)

    async def lock_worktree(
        self,
        worktree_path: str | Path,
        reason: str | None = None,
    ) -> GitOperationResult:
        return await _locking.lock_worktree(self, worktree_path, reason=reason)

    async def unlock_worktree(self, worktree_path: str | Path) -> GitOperationResult:
        return await _locking.unlock_worktree(self, worktree_path)

    async def get_default_branch(self) -> str:
        return await _branch.get_default_branch(self)

    async def get_current_branch(self) -> str | None:
        return await _branch.get_current_branch(self)

    async def has_unpushed_commits(self, branch: str | None = None) -> tuple[bool, int]:
        return await _branch.has_unpushed_commits(self, branch=branch)

    async def get_local_commit(self, branch: str) -> str | None:
        return await _branch.get_local_commit(self, branch)

    async def merge_branch(
        self,
        source_branch: str,
        target_branch: str = "main",
        push: bool = False,
    ) -> GitOperationResult:
        return await _merge.merge_branch(
            self, source_branch, target_branch=target_branch, push=push
        )
